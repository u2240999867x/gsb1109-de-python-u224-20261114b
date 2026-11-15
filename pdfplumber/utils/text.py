import inspect
import itertools
import logging
import math
import re
import statistics
import string
from operator import itemgetter
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Match,
    Optional,
    Pattern,
    Tuple,
    Union,
)

from .._typing import T_bbox, T_dir, T_num, T_obj, T_obj_iter, T_obj_list
from .clustering import cluster_objects
from .generic import to_list
from .geometry import objects_to_bbox

logger = logging.getLogger(__name__)

DEFAULT_X_TOLERANCE = 3
DEFAULT_Y_TOLERANCE = 3
DEFAULT_X_DENSITY = 7.25
DEFAULT_Y_DENSITY = 13
DEFAULT_LINE_DIR: T_dir = "ttb"
DEFAULT_CHAR_DIR: T_dir = "ltr"

LIGATURES = {
    "ﬀ": "ff",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬆ": "st",
    "ﬅ": "st",
}


_DIR_VECTORS: Dict[T_dir, Tuple[T_num, T_num]] = {
    "ltr": (1, 0),
    "rtl": (-1, 0),
    "ttb": (0, 1),
    "btt": (0, -1),
}

NOTE_MARKER_CHARS = set("0123456789*†‡§¶^+-=")


def _vector_to_dir(vx: T_num, vy: T_num) -> T_dir:
    """Return the reading direction closest to a vector."""
    if abs(vx) >= abs(vy):
        return "ltr" if vx >= 0 else "rtl"
    return "ttb" if vy >= 0 else "btt"


def get_char_directions(char: T_obj) -> Optional[Tuple[T_dir, T_dir]]:
    """Infer a character's (char_dir, line_dir) from its text matrix.

    `char_dir` follows the matrix's x-axis (the direction in which the
    glyph advances), while `line_dir` follows the direction in which
    successive lines advance. Both are expressed in page coordinates,
    where increasing `top`/`bottom` values point downwards.
    """
    matrix = char.get("matrix")
    if not isinstance(matrix, (tuple, list)) or len(matrix) < 6:
        return None  # pragma: nocover

    a, b, c, d = (float(v) for v in matrix[:4])
    if (a == 0 and b == 0) or (c == 0 and d == 0):
        return None  # pragma: nocover
    if math.isnan(a) or math.isnan(b) or math.isnan(c) or math.isnan(d):
        return None  # pragma: nocover

    # PDF coordinates point upwards, whereas `top`/`bottom` point downwards,
    # so the y-components of both axes must be negated.
    char_dir = _vector_to_dir(a, -b)
    line_dir = _vector_to_dir(-c, d)
    return (char_dir, line_dir)


def _project_origin(char: T_obj, direction: T_dir) -> T_num:
    """Project a character's text-matrix origin onto `direction`."""
    matrix = char.get("matrix")
    if isinstance(matrix, (tuple, list)) and len(matrix) >= 6:
        vx, vy = _DIR_VECTORS[direction]
        # The matrix's translation is in PDF (bottom-up) coordinates.
        return matrix[4] * vx + (-matrix[5]) * vy

    return POSITION_KEYS[direction](char)  # pragma: nocover


def get_char_baseline(char: T_obj, axis: T_dir = "ttb") -> T_num:
    """Return a character's baseline coordinate along the given axis.

    PDF text matrices have no explicit horizontal baseline; the projected
    matrix origin is used instead. This is the value that stays stable when
    a whole page is rotated, unlike `top`/`x0`.
    """
    return _project_origin(char, axis)


def get_char_line_position(char: T_obj, line_dir: T_dir) -> T_num:
    """Return a character's position across successive lines.

    The sign follows the line direction (e.g., bottom-to-top and
    right-to-left lines have descending keys).
    """
    return _project_origin(char, line_dir)


def get_char_along_key(
    char: T_obj, char_dir: T_dir
) -> Tuple[T_num, T_num, T_num]:
    """Return a character's position along its writing direction."""
    pos = _project_origin(char, char_dir)
    return (pos, char["top"], char["x0"])


def get_char_extent(char: T_obj, direction: T_dir) -> Tuple[T_num, T_num]:
    """Return the [start, end] extent of a character's bbox along `direction`."""
    vx, vy = _DIR_VECTORS[direction]
    corners = (
        (char["x0"], char["top"]),
        (char["x1"], char["top"]),
        (char["x0"], char["bottom"]),
        (char["x1"], char["bottom"]),
    )
    projected = [x * vx + y * vy for (x, y) in corners]
    return (min(projected), max(projected))


def same_structure(char_a: T_obj, char_b: T_obj) -> bool:
    """Whether two characters belong to the same marked-content run."""
    mcid_a, mcid_b = char_a.get("mcid"), char_b.get("mcid")
    if mcid_a is not None and mcid_b is not None:
        return mcid_a == mcid_b and char_a.get("tag") == char_b.get("tag")
    return False


def _cross_position(char: T_obj, line_dir: T_dir) -> T_num:
    if line_dir in ("ttb", "btt"):
        return get_char_baseline(char, "ttb")
    return char["x0"]


def has_structure_support(char: T_obj, line_chars: T_obj_iter) -> bool:
    """Whether a shared MCID or tag corroborates a char's line membership."""
    mcid, tag = char.get("mcid"), char.get("tag")
    if mcid is None and tag is None:
        return False
    for other in line_chars:
        if mcid is not None and other.get("mcid") == mcid:
            return True
        if tag is not None and other.get("tag") == tag:
            return True
    return False


def _bbox_gap(obj_a: T_obj, obj_b: T_obj) -> T_num:
    dx = max(obj_a["x0"], obj_b["x0"]) - min(obj_a["x1"], obj_b["x1"])
    dy = max(obj_a["top"], obj_b["top"]) - min(obj_a["bottom"], obj_b["bottom"])
    return math.hypot(max(dx, 0), max(dy, 0))




def get_line_cluster_key(line_dir: T_dir) -> Callable[[T_obj], T_num]:
    return {
        "ttb": lambda x: x["top"],
        "btt": lambda x: -x["bottom"],
        "ltr": lambda x: x["x0"],
        "rtl": lambda x: -x["x1"],
    }[line_dir]


def get_char_sort_key(char_dir: T_dir) -> Callable[[T_obj], Tuple[T_num, T_num]]:
    return {
        "ttb": lambda x: (x["top"], x["bottom"]),
        "btt": lambda x: (-(x["top"] + x["height"]), -x["top"]),
        "ltr": lambda x: (x["x0"], x["x0"]),
        "rtl": lambda x: (-x["x1"], -x["x0"]),
    }[char_dir]


BBOX_ORIGIN_KEYS = {
    "ttb": itemgetter(1),
    "btt": itemgetter(3),
    "ltr": itemgetter(0),
    "rtl": itemgetter(2),
}

POSITION_KEYS = {
    "ttb": itemgetter("top"),
    "btt": itemgetter("bottom"),
    "ltr": itemgetter("x0"),
    "rtl": itemgetter("x1"),
}


def validate_directions(line_dir: T_dir, char_dir: T_dir, suffix: str = "") -> None:
    valid_dirs = set(POSITION_KEYS.keys())
    if line_dir not in valid_dirs:
        raise ValueError(
            f"line_dir{suffix} must be one of {valid_dirs}, not {line_dir}"
        )
    if char_dir not in valid_dirs:
        raise ValueError(
            f"char_dir{suffix} must be one of {valid_dirs}, not {char_dir}"
        )
    if set(line_dir) == set(char_dir):
        raise ValueError(
            f"line_dir{suffix}={line_dir} is incompatible "
            f"with char_dir{suffix}={char_dir}"
        )


class TextMap:
    """
    A TextMap maps each unicode character in the text to an individual `char`
    object (or, in the case of layout-implied whitespace, `None`).
    """

    def __init__(
        self,
        tuples: List[Tuple[str, Optional[T_obj]]],
        line_dir_render: T_dir,
        char_dir_render: T_dir,
    ) -> None:
        validate_directions(line_dir_render, char_dir_render, "_render")
        self.tuples = tuples
        self.line_dir_render = line_dir_render
        self.char_dir_render = char_dir_render
        self.as_string = self.to_string()

    def to_string(self) -> str:
        cd = self.char_dir_render
        ld = self.line_dir_render

        base = "".join(map(itemgetter(0), self.tuples))

        if cd == "ltr" and ld == "ttb":
            return base
        else:
            lines = base.split("\n")
            if ld in ("btt", "rtl"):
                lines = list(reversed(lines))

            if cd == "rtl":
                lines = ["".join(reversed(line)) for line in lines]

            if ld in ("rtl", "ltr"):
                max_line_length = max(map(len, lines))
                if cd == "btt":
                    lines = [
                        (" " * (max_line_length - len(line))) + line for line in lines
                    ]
                else:
                    lines = [
                        line + (" " * (max_line_length - len(line))) for line in lines
                    ]
                return "\n".join(
                    "".join(line[i] for line in lines) for i in range(max_line_length)
                )
            else:
                return "\n".join(lines)

    def match_to_dict(
        self,
        m: Match[str],
        main_group: int = 0,
        return_groups: bool = True,
        return_chars: bool = True,
    ) -> Dict[str, Any]:
        subset = self.tuples[m.start(main_group) : m.end(main_group)]
        chars = [c for (text, c) in subset if c is not None]
        x0, top, x1, bottom = objects_to_bbox(chars)

        result = {
            "text": m.group(main_group),
            "x0": x0,
            "top": top,
            "x1": x1,
            "bottom": bottom,
        }

        if return_groups:
            result["groups"] = m.groups()

        if return_chars:
            result["chars"] = chars

        return result

    def search(
        self,
        pattern: Union[str, Pattern[str]],
        regex: bool = True,
        case: bool = True,
        return_groups: bool = True,
        return_chars: bool = True,
        main_group: int = 0,
    ) -> List[Dict[str, Any]]:
        if isinstance(pattern, Pattern):
            if regex is False:
                raise ValueError(
                    "Cannot pass a compiled search pattern *and* regex=False together."
                )
            if case is False:
                raise ValueError(
                    "Cannot pass a compiled search pattern *and* case=False together."
                )
            compiled = pattern
        else:
            if regex is False:
                pattern = re.escape(pattern)

            flags = re.I if case is False else 0
            compiled = re.compile(pattern, flags)

        gen = re.finditer(compiled, self.as_string)
        # Remove zero-length matches (can happen, e.g., with optional
        # patterns in regexes) and whitespace-only matches
        filtered = filter(lambda m: bool(m.group(main_group).strip()), gen)
        return [
            self.match_to_dict(
                m,
                return_groups=return_groups,
                return_chars=return_chars,
                main_group=main_group,
            )
            for m in filtered
        ]

    def extract_text_lines(
        self, strip: bool = True, return_chars: bool = True
    ) -> List[Dict[str, Any]]:
        """
        `strip` is analogous to Python's `str.strip()` method, and returns
        `text` attributes without their surrounding whitespace. Only
        relevant when the relevant TextMap is created with `layout` = True

        Setting `return_chars` to False will exclude the individual
        character objects from the returned text-line dicts.
        """
        if strip:
            pat = r" *([^\n]+?) *(\n|$)"
        else:
            pat = r"([^\n]+)"

        return self.search(
            pat, main_group=1, return_chars=return_chars, return_groups=False
        )


class WordMap:
    """
    A WordMap maps words->chars.
    """

    def __init__(self, tuples: List[Tuple[T_obj, T_obj_list]]) -> None:
        self.tuples = tuples

    def to_textmap(
        self,
        layout: bool = False,
        layout_width: T_num = 0,
        layout_height: T_num = 0,
        layout_width_chars: int = 0,
        layout_height_chars: int = 0,
        layout_bbox: T_bbox = (0, 0, 0, 0),
        x_density: T_num = DEFAULT_X_DENSITY,
        y_density: T_num = DEFAULT_Y_DENSITY,
        x_shift: T_num = 0,
        y_shift: T_num = 0,
        y_tolerance: T_num = DEFAULT_Y_TOLERANCE,
        line_dir: T_dir = DEFAULT_LINE_DIR,
        char_dir: T_dir = DEFAULT_CHAR_DIR,
        line_dir_rotated: Optional[T_dir] = None,
        char_dir_rotated: Optional[T_dir] = None,
        char_dir_render: Optional[T_dir] = None,
        line_dir_render: Optional[T_dir] = None,
        use_text_flow: bool = False,
        presorted: bool = False,
        expand_ligatures: bool = True,
    ) -> TextMap:
        """
        Given a list of (word, chars) tuples (i.e., a WordMap), return a list of
        (char-text, char) tuples (i.e., a TextMap) that can be used to mimic
        the structural layout of the text on the page(s), using the following
        approach for top-to-bottom, left-to-right text:

        - Sort the words by (top, x0) if not already sorted.

        - Cluster the words by top (taking `y_tolerance` into account), and
          iterate through them.

        - For each cluster, divide (top - y_shift) by `y_density` to calculate
          the minimum number of newlines that should come before this cluster.
          Append that number of newlines *minus* the number of newlines already
          appended, with a minimum of one.

        - Then for each cluster, iterate through each word in it. Divide each
          word's x0, minus `x_shift`, by `x_density` to calculate the minimum
          number of characters that should come before this cluster.  Append that
          number of spaces *minus* the number of characters and spaces already
          appended, with a minimum of one. Then append the word's text.

        - At the termination of each line, add more spaces if necessary to
          mimic `layout_width`.

        - Finally, add newlines to the end if necessary to mimic to
          `layout_height`.

        For other line/character directions (e.g., bottom-to-top,
        right-to-left), these steps are adjusted.
        """
        _textmap: List[Tuple[str, Optional[T_obj]]] = []

        if not len(self.tuples):
            return TextMap(
                _textmap,
                line_dir_render=line_dir_render or line_dir,
                char_dir_render=char_dir_render or char_dir,
            )

        expansions = LIGATURES if expand_ligatures else {}

        if layout:
            if layout_width_chars:
                if layout_width:
                    raise ValueError(
                        "`layout_width` and `layout_width_chars` cannot both be set."
                    )
            else:
                layout_width_chars = int(round(layout_width / x_density))

            if layout_height_chars:
                if layout_height:
                    raise ValueError(
                        "`layout_height` and `layout_height_chars` cannot both be set."
                    )
            else:
                layout_height_chars = int(round(layout_height / y_density))

            blank_line = [(" ", None)] * layout_width_chars
        else:
            blank_line = []

        num_newlines = 0

        line_cluster_key = get_line_cluster_key(line_dir)
        char_sort_key = get_char_sort_key(char_dir)

        line_position_key = POSITION_KEYS[line_dir]
        char_position_key = POSITION_KEYS[char_dir]

        y_origin = BBOX_ORIGIN_KEYS[line_dir](layout_bbox)
        x_origin = BBOX_ORIGIN_KEYS[char_dir](layout_bbox)

        words_sorted_line_dir = (
            self.tuples
            if presorted or use_text_flow
            else sorted(self.tuples, key=lambda x: line_cluster_key(x[0]))
        )

        tuples_by_line = cluster_objects(
            words_sorted_line_dir,
            lambda x: line_cluster_key(x[0]),
            y_tolerance,
            preserve_order=presorted or use_text_flow,
        )

        for i, line_tuples in enumerate(tuples_by_line):
            if layout:
                line_position = line_position_key(line_tuples[0][0])
                y_dist_raw = line_position - (y_origin + y_shift)
                adj = -1 if line_dir in ["btt", "rtl"] else 1
                y_dist = y_dist_raw * adj / y_density
            else:
                y_dist = 0
            num_newlines_prepend = max(
                # At least one newline, unless this iis the first line
                int(i > 0),
                # ... or as many as needed to get the imputed "distance" from the top
                round(y_dist) - num_newlines,
            )

            for i in range(num_newlines_prepend):
                if not len(_textmap) or _textmap[-1][0] == "\n":
                    _textmap += blank_line
                _textmap.append(("\n", None))

            num_newlines += num_newlines_prepend

            line_len = 0

            line_tuples_sorted = (
                line_tuples
                if presorted or use_text_flow
                else sorted(line_tuples, key=lambda x: char_sort_key(x[0]))
            )

            for word, chars in line_tuples_sorted:
                if layout:
                    char_position = char_position_key(word)
                    x_dist_raw = char_position - (x_origin + x_shift)
                    adj = -1 if char_dir in ["btt", "rtl"] else 1
                    x_dist = x_dist_raw * adj / x_density
                else:
                    x_dist = 0

                num_spaces_prepend = max(min(1, line_len), round(x_dist) - line_len)
                _textmap += [(" ", None)] * num_spaces_prepend
                line_len += num_spaces_prepend

                for c in chars:
                    letters = expansions.get(c["text"], c["text"])
                    for letter in letters:
                        _textmap.append((letter, c))
                        line_len += 1

            # Append spaces at end of line
            if layout:
                _textmap += [(" ", None)] * (layout_width_chars - line_len)

        # Append blank lines at end of text
        if layout:
            num_newlines_append = layout_height_chars - (num_newlines + 1)
            for i in range(num_newlines_append):
                if i > 0:
                    _textmap += blank_line
                _textmap.append(("\n", None))

            # Remove terminal newline
            if _textmap[-1] == ("\n", None):
                _textmap = _textmap[:-1]

        return TextMap(
            _textmap,
            line_dir_render=line_dir_render or line_dir,
            char_dir_render=char_dir_render or char_dir,
        )


class WordExtractor:
    def __init__(
        self,
        x_tolerance: T_num = DEFAULT_X_TOLERANCE,
        y_tolerance: T_num = DEFAULT_Y_TOLERANCE,
        x_tolerance_ratio: Union[int, float, None] = None,
        y_tolerance_ratio: Union[int, float, None] = None,
        keep_blank_chars: bool = False,
        use_text_flow: bool = False,
        vertical_ttb: bool = True,  # Should vertical words be read top-to-bottom?
        horizontal_ltr: bool = True,  # Should words be read left-to-right?
        line_dir: T_dir = DEFAULT_LINE_DIR,
        char_dir: T_dir = DEFAULT_CHAR_DIR,
        line_dir_rotated: Optional[T_dir] = None,
        char_dir_rotated: Optional[T_dir] = None,
        extra_attrs: Optional[List[str]] = None,
        split_at_punctuation: Union[bool, str] = False,
        expand_ligatures: bool = True,
    ):
        self.x_tolerance = x_tolerance
        self.y_tolerance = y_tolerance
        self.x_tolerance_ratio = x_tolerance_ratio
        self.y_tolerance_ratio = y_tolerance_ratio
        self.keep_blank_chars = keep_blank_chars
        self.use_text_flow = use_text_flow
        self.horizontal_ltr = horizontal_ltr
        self.vertical_ttb = vertical_ttb
        if vertical_ttb is False:
            logger.warning(
                "vertical_ttb is deprecated and will be removed;"
                " use line_dir/char_dir instead."
            )
        if horizontal_ltr is False:
            logger.warning(
                "horizontal_ltr is deprecated and will be removed;"
                " use line_dir/char_dir instead."
            )
        self.line_dir = line_dir
        self.char_dir = char_dir
        # Default is to "flip" the directions for rotated text
        self.line_dir_rotated = line_dir_rotated or char_dir
        self.char_dir_rotated = char_dir_rotated or line_dir
        validate_directions(self.line_dir, self.char_dir)
        validate_directions(self.line_dir_rotated, self.char_dir_rotated, "_rotated")
        self.extra_attrs = [] if extra_attrs is None else extra_attrs

        # Note: string.punctuation = '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'
        self.split_at_punctuation = (
            string.punctuation
            if split_at_punctuation is True
            else (split_at_punctuation or "")
        )

        self.expansions = LIGATURES if expand_ligatures else {}

    def split_note_markers(self, chars: T_obj_list) -> Tuple[T_obj_list, T_obj_list]:
        """Separate note markers written across the body's direction.

        A marker qualifies when it (a) advances perpendicular to the body
        text, (b) is smaller than that text, (c) sits physically adjacent to
        it, and (d) carries note-like glyphs. Shared structure (MCID or tag)
        with an adjacent body character overrides the separation, as does the
        marker already belonging to a same-direction run.
        """
        if len(chars) < 2:
            return (list(chars), [])

        sizes = [c.get("size") or c["height"] for c in chars if not c["text"].isspace()]
        if not sizes:
            return (list(chars), [])
        base_size = statistics.median(sizes)
        body_chars = [
            c
            for c in chars
            if not c["text"].isspace()
            and (c.get("size") or c["height"]) >= base_size * 0.8
            and get_char_directions(c) is not None
        ]
        direction_votes = [
            get_char_directions(c)[0]  # type: ignore[index]
            for c in (body_chars or chars)
            if not c["text"].isspace() and get_char_directions(c) is not None
        ]
        if not direction_votes:
            return (list(chars), [])
        body_char_dir = statistics.mode(direction_votes)
        perpendicular = {
            "ltr": ("ttb", "btt"),
            "rtl": ("ttb", "btt"),
            "ttb": ("ltr", "rtl"),
            "btt": ("ltr", "rtl"),
        }[body_char_dir]
        body = [
            c
            for c in chars
            if get_char_directions(c) is not None
            and get_char_directions(c)[0] not in perpendicular  # type: ignore[index]
        ]
        if base_size <= 0:
            return (list(chars), [])

        adjacency = max(self.x_tolerance, self.y_tolerance, 0.5 * base_size)
        candidates = [
            c
            for c in chars
            if get_char_directions(c) is not None
            and get_char_directions(c)[0] in perpendicular  # type: ignore[index]
            and (c.get("size") or c["height"]) < base_size * 0.9
            and not c["text"].isspace()
            and all(ch in NOTE_MARKER_CHARS for ch in c["text"])
        ]

        markers: T_obj_list = []
        for char in candidates:
            char_dir = get_char_directions(char)[0]  # type: ignore[index]
            same_run = [
                other
                for other in chars
                if other is not char
                and (other.get("size") or other["height"]) >= base_size * 0.8
                and get_char_directions(other) is not None
                and get_char_directions(other)[0] == char_dir  # type: ignore[index]
                and _bbox_gap(char, other) <= adjacency
            ]
            if same_run:
                continue
            adjacent_body = [
                other
                for other in body
                if not other["text"].isspace()
                and _bbox_gap(char, other) <= adjacency
            ]
            if not adjacent_body:
                continue
            if any(has_structure_support(char, [other]) for other in adjacent_body):
                continue
            markers.append(char)

        if body_char_dir in ("ltr", "rtl"):
            small = [
                c
                for c in chars
                if get_char_directions(c) is not None
                and get_char_directions(c)[0] == body_char_dir  # type: ignore[index]
                and (c.get("size") or c["height"]) < base_size * 0.85
                and not c["text"].isspace()
                and all(ch in NOTE_MARKER_CHARS for ch in c["text"])
            ]
            for column in cluster_objects(
                small, lambda c: c["x0"], max(self.x_tolerance, 1)
            ):
                for stack in self._stacked_runs(column, base_size):
                    top = min(c["top"] for c in stack)
                    bottom = max(c["bottom"] for c in stack)
                    adjacent_body = [
                        other
                        for other in body
                        if not other["text"].isspace()
                        and other["bottom"] >= top - adjacency
                        and other["top"] <= bottom + adjacency
                        and min(
                            abs(other["x1"] - stack[0]["x0"]),
                            abs(other["x0"] - stack[0]["x1"]),
                        )
                        <= adjacency
                    ]
                    if not adjacent_body:
                        continue
                    if any(
                        has_structure_support(char, adjacent_body) for char in stack
                    ):
                        continue
                    markers.extend(stack)

        marker_ids = {id(c) for c in markers}
        kept = [c for c in chars if id(c) not in marker_ids]
        return (kept, markers)

    def _stacked_runs(
        self, column: T_obj_list, base_size: T_num
    ) -> List[T_obj_list]:
        ordered = sorted(column, key=lambda c: c["top"])
        runs: List[T_obj_list] = []
        run: T_obj_list = []
        for char in ordered:
            if run:
                gap = char["top"] - max(c["bottom"] for c in run)
                if gap > max(self.y_tolerance, 0.5 * base_size):
                    runs.append(run)
                    run = []
            run.append(char)
        if run:
            runs.append(run)

        stacked: List[T_obj_list] = []
        for run in runs:
            if len(run) < 2:
                continue
            width = max(c["x1"] for c in run) - min(c["x0"] for c in run)
            height = max(c["bottom"] for c in run) - min(c["top"] for c in run)
            if height > width * 1.5:
                stacked.append(run)
        return stacked

    def get_char_dir(
        self, upright: int, direction: Optional[Tuple[T_dir, T_dir]] = None
    ) -> T_dir:
        # Note: This can be simplified and reincorporated into .merge_chars and
        # .iter_chars_to_lines once .vertical_ttb and .horizontal_ltr
        # deprecation is complete.
        if direction is not None:
            char_dir, _ = direction
            if char_dir in ("ltr", "rtl"):
                if self.horizontal_ltr is False:
                    return "rtl"
                return char_dir
            if self.vertical_ttb is False:
                return "btt"
            return char_dir

        if not upright and not self.vertical_ttb:
            return "btt"

        elif upright and not self.horizontal_ltr:
            return "rtl"

        return self.char_dir if upright else self.char_dir_rotated

    def merge_chars(self, ordered_chars: T_obj_list) -> T_obj:
        x0, top, x1, bottom = objects_to_bbox(ordered_chars)
        doctop_adj = ordered_chars[0]["doctop"] - ordered_chars[0]["top"]
        upright = ordered_chars[0]["upright"]
        direction = get_char_directions(ordered_chars[0])
        char_dir = self.get_char_dir(upright, direction)

        word = {
            "text": "".join(
                self.expansions.get(c["text"], c["text"] or "") for c in ordered_chars
            ),
            "x0": x0,
            "x1": x1,
            "top": top,
            "doctop": top + doctop_adj,
            "bottom": bottom,
            "upright": upright,
            "height": bottom - top,
            "width": x1 - x0,
            "direction": char_dir,
        }

        for key in self.extra_attrs:
            word[key] = ordered_chars[0][key]

        return word

    def char_begins_new_word(
        self,
        prev_char: T_obj,
        curr_char: T_obj,
        direction: T_dir,
        x_tolerance: T_num,
        y_tolerance: T_num,
    ) -> bool:
        """This method takes several factors into account to determine if
        `curr_char` represents the beginning of a new word:

        - Whether the text is "upright" (i.e., non-rotated)
        - Whether the user has specified that horizontal text runs
          left-to-right (default) or right-to-left, as represented by
          self.horizontal_ltr
        - Whether the user has specified that vertical text the text runs
          top-to-bottom (default) or bottom-to-top, as represented by
          self.vertical_ttb
        - The x0, top, x1, and bottom attributes of prev_char and
          curr_char
        - The self.x_tolerance and self.y_tolerance settings. Note: In
          this case, x/y refer to those directions for non-rotated text.
          For vertical text, they are flipped. A more accurate terminology
          might be "*intra*line character distance tolerance" and
          "*inter*line character distance tolerance"

        An important note: The *intra*line distance is measured from the
        *end* of the previous character to the *beginning* of the current
        character, while the *inter*line distance is measured from the
        *top* of the previous character to the *top* of the next
        character. The reasons for this are partly repository-historical,
        and partly logical, as successive text lines' bounding boxes often
        overlap slightly (and we don't want that overlap to be interpreted
        as the two lines being the same line).

        The upright-ness of the character determines the attributes to
        compare, while horizontal_ltr/vertical_ttb determine the direction
        of the comparison.
        """
        # Note: Due to the grouping step earlier in the process,
        # curr_char["upright"] will always equal prev_char["upright"].
        prev_direction = get_char_directions(prev_char)
        curr_direction = get_char_directions(curr_char)
        if (
            prev_direction is not None
            and curr_direction is not None
            and prev_direction != curr_direction
        ):
            return not same_structure(prev_char, curr_char)

        if direction in ("ltr", "rtl"):
            x = x_tolerance
            y = y_tolerance
            ay = get_char_baseline(prev_char, "ttb")
            cy = get_char_baseline(curr_char, "ttb")
            if direction == "ltr":
                ax, bx = get_char_extent(prev_char, "ltr")
                cx, _ = get_char_extent(curr_char, "ltr")
            else:
                ax, bx = get_char_extent(prev_char, "rtl")
                cx, _ = get_char_extent(curr_char, "rtl")

        else:
            x = y_tolerance
            y = x_tolerance
            ay = prev_char["x0"]
            cy = curr_char["x0"]
            if direction == "ttb":
                ax, bx = prev_char["top"], prev_char["bottom"]
                cx = curr_char["top"]
            else:
                ax, bx = -prev_char["bottom"], -prev_char["top"]
                cx = -curr_char["bottom"]

        return bool(
            # Intraline test
            (cx < ax)
            or (cx > bx + x)
            # Interline test
            or abs(cy - ay) > y
        )

    def iter_chars_to_words(
        self,
        ordered_chars: T_obj_iter,
        direction: T_dir,
    ) -> Generator[T_obj_list, None, None]:
        current_word: T_obj_list = []

        def start_next_word(
            new_char: Optional[T_obj],
        ) -> Generator[T_obj_list, None, None]:
            nonlocal current_word

            if current_word:
                yield current_word

            current_word = [] if new_char is None else [new_char]

        xt = self.x_tolerance
        xtr = self.x_tolerance_ratio
        yt = self.y_tolerance
        ytr = self.y_tolerance_ratio

        for char in ordered_chars:
            text = char["text"]
            if not self.keep_blank_chars and text.isspace():
                yield from start_next_word(None)

            elif text in self.split_at_punctuation:
                yield from start_next_word(char)
                yield from start_next_word(None)

            elif current_word and self.char_begins_new_word(
                current_word[-1],
                char,
                direction,
                x_tolerance=(xt if xtr is None else xtr * current_word[-1]["size"]),
                y_tolerance=(yt if ytr is None else ytr * current_word[-1]["size"]),
            ):
                yield from start_next_word(char)

            else:
                current_word.append(char)

        # Finally, after all chars processed
        if current_word:
            yield current_word

    def iter_chars_to_lines(
        self, chars: T_obj_iter
    ) -> Generator[Tuple[T_obj_list, T_dir], None, None]:
        chars = list(chars)
        upright = chars[0]["upright"]
        direction = get_char_directions(chars[0])
        if direction is not None:
            _, matrix_line_dir = direction
            char_dir = self.get_char_dir(upright, direction)
            line_dir = matrix_line_dir
        else:  # pragma: nocover
            line_dir = self.line_dir if upright else self.line_dir_rotated
            char_dir = self.get_char_dir(upright)

        def line_cluster_key(char: T_obj) -> T_num:
            if line_dir in ("ltr", "rtl"):
                return get_line_cluster_key(line_dir)(char)
            return get_char_line_position(char, line_dir)

        def char_sort_key(char: T_obj) -> Tuple[T_num, T_num, T_num]:
            return get_char_along_key(char, char_dir)

        # Cluster by line
        subclusters = cluster_objects(
            chars,
            line_cluster_key,
            (self.y_tolerance if line_dir in ("ttb", "btt") else self.x_tolerance),
        )

        for sc in subclusters:
            # Sort within line
            chars_sorted = sorted(sc, key=char_sort_key)
            yield (chars_sorted, char_dir)

    def iter_extract_tuples(
        self, chars: T_obj_iter
    ) -> Generator[Tuple[T_obj, T_obj_list], None, None]:
        chars, note_markers = self.split_note_markers(list(chars))

        def grouping_key(char: T_obj) -> Tuple[Any, ...]:
            return (
                char["upright"],
                get_char_directions(char),
                *(char[attr] for attr in self.extra_attrs),
            )

        grouped_chars = itertools.groupby(chars, grouping_key)

        for keyvals, char_group in grouped_chars:
            char_group = list(char_group)
            line_groups = (
                [(char_group, self.get_char_dir(char_group[0]["upright"], keyvals[1]))]
                if self.use_text_flow
                else self.iter_chars_to_lines(char_group)
            )
            for line_chars, direction in line_groups:
                for word_chars in self.iter_chars_to_words(line_chars, direction):
                    yield (self.merge_chars(word_chars), word_chars)

        for marker_chars in self.group_note_marker_words(note_markers):
            word = self.merge_chars(marker_chars)
            word["is_note_marker"] = True
            yield (word, marker_chars)

    def group_note_marker_words(self, markers: T_obj_list) -> List[T_obj_list]:
        """Split separated note-marker glyphs into individual words."""
        if not markers:
            return []
        words: T_obj_list = []
        remaining = list(markers)
        while remaining:
            first = remaining[0]
            direction = get_char_directions(first) or ("ltr", "ttb")
            line_dir = direction[1]
            same_line = [
                c
                for c in remaining
                if get_char_directions(c) == direction
                and abs(
                    _cross_position(c, line_dir) - _cross_position(first, line_dir)
                )
                <= max(self.y_tolerance, 1)
            ]
            if get_char_directions(first) is None:
                same_line = sorted(same_line, key=lambda c: (c["x0"], c["top"]))
            else:
                same_line = sorted(
                    same_line, key=lambda c: get_char_along_key(c, direction[0])
                )
            words.append(same_line)
            remaining = [c for c in remaining if c not in same_line]
        return words

    def extract_wordmap(self, chars: T_obj_iter) -> WordMap:
        return WordMap(list(self.iter_extract_tuples(chars)))

    def extract_words(
        self, chars: T_obj_list, return_chars: bool = False
    ) -> T_obj_list:
        if return_chars:
            return list(
                {**word, "chars": word_chars}
                for word, word_chars in self.iter_extract_tuples(chars)
            )
        else:
            return list(word for word, word_chars in self.iter_extract_tuples(chars))


def extract_words(
    chars: T_obj_list, return_chars: bool = False, **kwargs: Any
) -> T_obj_list:
    return WordExtractor(**kwargs).extract_words(chars, return_chars)


def get_word_candidates(
    words: T_obj_list,
    marker: T_obj,
    horizontal: bool = True,
    tolerance: T_num = DEFAULT_Y_TOLERANCE,
) -> T_obj_list:
    """Return body-text words that a standalone note marker may annotate.

    Candidates are searched in reading order: the closest word ending
    before the marker on its line, then the next word beginning after it,
    and then adjacent lines. The result list is ordered by distance.
    """
    if not horizontal:
        return []
    if marker.get("direction") in ("ttb", "btt"):
        direction = ("ltr", "ttb")
    else:
        direction = None
    marker_center_y = (marker["top"] + marker["bottom"]) / 2
    body = [
        word
        for word in words
        if word is not marker
        and not word.get("is_note_marker")
        and (direction is None or word["direction"] == direction[0])
    ]
    words_with_distance = [
        (
            word,
            min(
                abs(marker["x0"] - word["x1"]),
                abs(word["x0"] - marker["x1"]),
            ),
        )
        for word in body
    ]
    same_line = [
        (word, distance)
        for (word, distance) in words_with_distance
        if abs((word["top"] + word["bottom"]) / 2 - marker_center_y) <= tolerance
    ]
    same_line.sort(key=lambda pair: pair[1])
    if same_line:
        return [word for (word, distance) in same_line]

    # Markers often sit slightly above or below their body line; fall back
    # to the closest horizontal words, with a preference for those whose
    # vertical span still overlaps the marker's line band.
    ranked = sorted(
        words_with_distance,
        key=lambda pair: (
            min(
                abs(marker["top"] - pair[0]["bottom"]),
                abs(pair[0]["top"] - marker["bottom"]),
            ),
            pair[1],
        ),
    )
    return [word for (word, distance) in ranked[:3]]


TEXTMAP_KWARGS = inspect.signature(WordMap.to_textmap).parameters.keys()
WORD_EXTRACTOR_KWARGS = inspect.signature(WordExtractor).parameters.keys()


def chars_to_textmap(chars: T_obj_list, **kwargs: Any) -> TextMap:
    kwargs.update(
        {
            "presorted": True,
            "layout_bbox": kwargs.get("layout_bbox") or objects_to_bbox(chars),
        }
    )

    extractor = WordExtractor(
        **{k: kwargs[k] for k in WORD_EXTRACTOR_KWARGS if k in kwargs}
    )
    wordmap = extractor.extract_wordmap(chars)
    textmap = wordmap.to_textmap(
        **{k: kwargs[k] for k in TEXTMAP_KWARGS if k in kwargs}
    )
    return textmap


def extract_text(
    chars: T_obj_list,
    line_dir_render: Optional[T_dir] = None,
    char_dir_render: Optional[T_dir] = None,
    **kwargs: Any,
) -> str:
    chars = to_list(chars)
    if len(chars) == 0:
        return ""

    if kwargs.get("layout"):
        textmap_kwargs = {
            **kwargs,
            **{"line_dir_render": line_dir_render, "char_dir_render": char_dir_render},
        }
        return chars_to_textmap(chars, **textmap_kwargs).as_string
    else:
        extractor = WordExtractor(
            **{k: kwargs[k] for k in WORD_EXTRACTOR_KWARGS if k in kwargs}
        )
        words = extractor.extract_words(chars)

        line_dir_render = line_dir_render or extractor.line_dir
        char_dir_render = char_dir_render or extractor.char_dir

        line_cluster_key = get_line_cluster_key(extractor.line_dir)

        x_tolerance = kwargs.get("x_tolerance", DEFAULT_X_TOLERANCE)
        y_tolerance = kwargs.get("y_tolerance", DEFAULT_Y_TOLERANCE)

        lines = cluster_objects(
            words,
            line_cluster_key,
            y_tolerance if line_dir_render in ("ttb", "btt") else x_tolerance,
        )

        return TextMap(
            [
                (char, None)
                for char in (
                    "\n".join(" ".join(word["text"] for word in line) for line in lines)
                )
            ],
            line_dir_render=line_dir_render,
            char_dir_render=char_dir_render,
        ).as_string


def collate_line(
    line_chars: T_obj_list,
    tolerance: T_num = DEFAULT_X_TOLERANCE,
) -> str:
    coll = ""
    last_x1 = None
    for char in sorted(line_chars, key=itemgetter("x0")):
        if (last_x1 is not None) and (char["x0"] > (last_x1 + tolerance)):
            coll += " "
        last_x1 = char["x1"]
        coll += char["text"]
    return coll


def extract_text_simple(
    chars: T_obj_list,
    x_tolerance: T_num = DEFAULT_X_TOLERANCE,
    y_tolerance: T_num = DEFAULT_Y_TOLERANCE,
) -> str:
    clustered = cluster_objects(chars, itemgetter("doctop"), y_tolerance)
    return "\n".join(collate_line(c, x_tolerance) for c in clustered)


def dedupe_chars(
    chars: T_obj_list,
    tolerance: T_num = 1,
    extra_attrs: Optional[Tuple[str, ...]] = ("fontname", "size"),
) -> T_obj_list:
    """
    Removes duplicate chars — those sharing the same text and positioning
    (within `tolerance`) as other characters in the set. Use extra_args to
    be more restrictive with the properties shared by the matching chars.
    """
    key = itemgetter(*("upright", "text"), *(extra_attrs or tuple()))
    pos_key = itemgetter("doctop", "x0")

    def yield_unique_chars(chars: T_obj_list) -> Generator[T_obj, None, None]:
        sorted_chars = sorted(chars, key=key)
        for grp, grp_chars in itertools.groupby(sorted_chars, key=key):
            for y_cluster in cluster_objects(
                list(grp_chars), itemgetter("doctop"), tolerance
            ):
                for x_cluster in cluster_objects(
                    y_cluster, itemgetter("x0"), tolerance
                ):
                    yield sorted(x_cluster, key=pos_key)[0]

    deduped = yield_unique_chars(chars)
    return sorted(deduped, key=chars.index)
