import inspect
import itertools
import logging
import re
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
DEFAULT_MICRO_OFFSET_RATIO = 0.5

LIGATURES = {
    "ﬀ": "ff",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬆ": "st",
    "ﬅ": "st",
}


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

    def get_char_dir(self, upright: int) -> T_dir:
        # Note: This can be simplified and reincorporated into .merge_chars and
        # .iter_chars_to_lines once .vertical_ttb and .horizontal_ltr
        # deprecation is complete.
        if not upright and not self.vertical_ttb:
            return "btt"

        elif upright and not self.horizontal_ltr:
            return "rtl"

        return self.char_dir if upright else self.char_dir_rotated

    def merge_chars(self, ordered_chars: T_obj_list) -> T_obj:
        x0, top, x1, bottom = objects_to_bbox(ordered_chars)
        doctop_adj = ordered_chars[0]["doctop"] - ordered_chars[0]["top"]
        upright = ordered_chars[0]["upright"]
        char_dir = self.get_char_dir(upright)

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
        if direction in ("ltr", "rtl"):
            x = x_tolerance
            y = y_tolerance
            ay = prev_char["top"]
            cy = curr_char["top"]
            if direction == "ltr":
                ax = prev_char["x0"]
                bx = prev_char["x1"]
                cx = curr_char["x0"]
            else:
                ax = -prev_char["x1"]
                bx = -prev_char["x0"]
                cx = -curr_char["x1"]

        else:
            x = y_tolerance
            y = x_tolerance
            ay = prev_char["x0"]
            cy = curr_char["x0"]
            if direction == "ttb":
                ax = prev_char["top"]
                bx = prev_char["bottom"]
                cx = curr_char["top"]
            else:
                ax = -prev_char["bottom"]
                bx = -prev_char["top"]
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
        line_dir = self.line_dir if upright else self.line_dir_rotated
        char_dir = self.get_char_dir(upright)

        line_cluster_key = get_line_cluster_key(line_dir)
        char_sort_key = get_char_sort_key(char_dir)

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
        grouping_key = itemgetter("upright", *self.extra_attrs)
        grouped_chars = itertools.groupby(chars, grouping_key)

        for keyvals, char_group in grouped_chars:
            line_groups = (
                [(char_group, self.char_dir)]
                if self.use_text_flow
                else self.iter_chars_to_lines(char_group)
            )
            for line_chars, direction in line_groups:
                for word_chars in self.iter_chars_to_words(line_chars, direction):
                    yield (self.merge_chars(word_chars), word_chars)

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


def _as_color_tuple(color: Any) -> Optional[Tuple[float, ...]]:
    """Normalize a PDF color into a comparable RGB-ish tuple.

    pdfminer.six reports colors in the color space currently in effect, so the
    same visible color can be represented as ``(0,)`` (DeviceGray),
    ``(0.0, 0.0, 0.0)`` (DeviceRGB), or a DeviceCMYK tuple. Convert those
    representations to a common form before comparing duplicate draws.
    """
    if color is None:
        return None

    if isinstance(color, (int, float)):
        color = (color,)

    if not isinstance(color, (tuple, list)):
        return None

    try:
        values = tuple(float(value) for value in color)
    except (TypeError, ValueError):
        return None

    if len(values) == 1:
        return (values[0],) * 3
    if len(values) == 3:
        return values
    if len(values) == 4:
        cyan, magenta, yellow, black = values
        return (
            (1 - cyan) * (1 - black),
            (1 - magenta) * (1 - black),
            (1 - yellow) * (1 - black),
        )
    return values


def _colors_match(left: Any, right: Any, precision: float = 1e-6) -> bool:
    left_rgb = _as_color_tuple(left)
    right_rgb = _as_color_tuple(right)
    if left_rgb is None or right_rgb is None:
        return left is None and right is None
    if len(left_rgb) != len(right_rgb):
        return False
    return all(abs(a - b) <= precision for a, b in zip(left_rgb, right_rgb))


def _transforms_match(left: T_obj, right: T_obj, precision: float = 1e-6) -> bool:
    """Compare matrix scale/skew, ignoring translation and glyph position."""
    left_matrix = left.get("matrix")
    right_matrix = right.get("matrix")
    if not (
        isinstance(left_matrix, (tuple, list))
        and isinstance(right_matrix, (tuple, list))
        and len(left_matrix) >= 4
        and len(right_matrix) >= 4
    ):
        return True
    return all(
        abs(float(left_matrix[i]) - float(right_matrix[i])) <= precision
        for i in range(4)
    )


def _layers_compatible(left: T_obj, right: T_obj) -> bool:
    """Check that two draws come from the same page and compatible layers.

    A missing marked-content ID/tag is treated as compatible with an explicit
    one, because a duplicate outline pass is often wrapped in an extra
    ``/Span`` but still belongs to the same visual layer. Two *different*
    explicit layers are treated as incompatible.
    """
    left_page = left.get("page_number")
    right_page = right.get("page_number")
    if left_page is not None and right_page is not None and left_page != right_page:
        return False

    for attr in ("tag", "mcid"):
        left_value = left.get(attr)
        right_value = right.get(attr)
        if (
            left_value is not None
            and right_value is not None
            and left_value != right_value
        ):
            return False
    return True


def _glyph_extent(char: T_obj) -> Optional[float]:
    """Return a conservative glyph advance extent for the micro-offset guard.

    The ``width`` of a pdfplumber char is the glyph's horizontal extent on the
    page, which equals its advance for upright text and the vertical advance
    for quarter-rotated text. Narrow glyphs such as ``i`` therefore report a
    narrow extent and cannot absorb their real neighbor.
    """
    for key in ("width", "size"):
        value = char.get(key)
        if isinstance(value, (int, float)) and value:
            return abs(float(value))
    return None


def _evaluate_duplicate_draw(
    anchor: T_obj,
    char: T_obj,
    tolerance: T_num,
) -> T_obj:
    """Audit every signal used to decide whether two draws are duplicates.

    The returned dict has ``accepted``, a machine-readable ``reason`` and a
    ``checks`` breakdown. A draw is accepted only when all of these hold:

    - ``offset``: both ``|x0 - anchor.x0|`` and ``|doctop - anchor.doctop|``
      are within ``tolerance``.
    - ``advance``: the horizontal offset is at most
      ``DEFAULT_MICRO_OFFSET_RATIO`` of the glyph's advance. This rejects real
      adjacent glyphs such as the two characters in ``11`` even when a caller
      passes a large absolute ``tolerance``. The cap is deliberately proportional
      because a reused glyph is nudged only slightly for stroke alignment, while
      a real neighbor is a meaningful fraction of an advance away.
    - ``layer``: same page, and marked-content tag/MCID do not conflict. A
      missing tag/MCID acts as a wildcard because an outline pass is commonly
      wrapped in an extra ``/Span``.
    - ``color``: at least one of the stroke or fill colors matches after
      normalizing DeviceGray/DeviceRGB/DeviceCMYK. This permits a
      fill-plus-outline pair while rejecting two differently painted glyphs.
    - ``transform``: the first four matrix entries (scale and skew) match, so
      same-text glyphs rendered at a different size or rotation stay distinct.
    """
    try:
        delta_x = abs(float(char["x0"]) - float(anchor["x0"]))
        delta_y = abs(float(char["doctop"]) - float(anchor["doctop"]))
    except (KeyError, TypeError, ValueError):
        return {
            "accepted": False,
            "reason": "missing_geometry",
            "checks": {},
        }

    extent = _glyph_extent(anchor)
    advance_limit = (
        None if extent is None else extent * DEFAULT_MICRO_OFFSET_RATIO
    )
    stroke_match = _colors_match(
        anchor.get("stroking_color"), char.get("stroking_color")
    )
    fill_match = _colors_match(
        anchor.get("non_stroking_color"), char.get("non_stroking_color")
    )
    color_channels = tuple(
        name
        for name, matched in (
            ("stroking_color", stroke_match),
            ("non_stroking_color", fill_match),
        )
        if matched
    )
    layer_compatible = _layers_compatible(anchor, char)
    transform_match = _transforms_match(anchor, char)

    checks = {
        "offset": {
            "delta": (delta_x, delta_y),
            "tolerance": tolerance,
            "within": delta_x <= tolerance and delta_y <= tolerance,
        },
        "advance": {
            "glyph_extent": extent,
            "limit": advance_limit,
            "within": advance_limit is None or delta_x <= advance_limit,
        },
        "layer": {
            "anchor": {
                "page_number": anchor.get("page_number"),
                "tag": anchor.get("tag"),
                "mcid": anchor.get("mcid"),
            },
            "candidate": {
                "page_number": char.get("page_number"),
                "tag": char.get("tag"),
                "mcid": char.get("mcid"),
            },
            "compatible": layer_compatible,
        },
        "color": {
            "stroking_match": stroke_match,
            "non_stroking_match": fill_match,
            "matching_channels": color_channels,
            "compatible": bool(color_channels),
        },
        "transform": {
            "anchor": anchor.get("matrix"),
            "candidate": char.get("matrix"),
            "compatible": transform_match,
        },
    }

    if not transform_match:
        reason = "transform_mismatch"
    elif not checks["advance"]["within"]:
        reason = "advance_guard"
    elif not checks["offset"]["within"]:
        reason = "offset_exceeds_tolerance"
    elif not layer_compatible:
        reason = "layer_mismatch"
    elif not color_channels:
        reason = "color_mismatch"
    else:
        reason = "duplicate"

    return {
        "accepted": reason == "duplicate",
        "reason": reason,
        "checks": checks,
    }


def _draw_provenance(index: int, char: T_obj) -> T_obj:
    keys = (
        "page_number",
        "text",
        "fontname",
        "size",
        "upright",
        "x0",
        "top",
        "doctop",
        "x1",
        "bottom",
        "adv",
        "width",
        "height",
        "matrix",
        "tag",
        "mcid",
        "stroking_color",
        "non_stroking_color",
    )
    return {"index": index, **{key: char.get(key) for key in keys}}


def dedupe_char_groups(
    chars: T_obj_list,
    tolerance: T_num = 1,
    extra_attrs: Optional[Tuple[str, ...]] = ("fontname", "size"),
    include_candidates: bool = True,
) -> List[T_obj]:
    """Find duplicate character draws, returning auditable group records.

    Characters are grouped by glyph (``text``, ``upright`` and any
    ``extra_attrs``), then anchored on the first remaining draw. Every
    additional draw must pass all checks in ``_evaluate_duplicate_draw``
    against the *anchor*: within ``tolerance`` on both axes, within half a
    glyph advance, on a compatible layer, and sharing at least one paint
    color. Because comparisons are made against the anchor rather than the
    previous character, a chain such as ``x=0, 0.9, 1.8`` (with
    ``tolerance=1``) splits into ``[0, 0.9]`` and ``[1.8]`` instead of
    collapsing all three characters.

    Each group record lists the kept draw and every removed draw (with its
    ``index``, geometry, layer, colors, and full ``checks`` audit). When
    ``include_candidates`` is true, nearby draws that were rejected for this
    anchor are also recorded, so a reviewer can see why they stayed.
    """
    key = itemgetter(*("upright", "text"), *(extra_attrs or tuple()))
    indexed = list(enumerate(chars))
    groups: List[T_obj] = []

    for glyph, glyph_chars in itertools.groupby(
        sorted(indexed, key=lambda item: (key(item[1]), item[0])),
        key=lambda item: key(item[1]),
    ):
        ordered = sorted(
            glyph_chars,
            key=lambda item: (item[1]["doctop"], item[1]["x0"], item[0]),
        )
        remaining = ordered
        while remaining:
            anchor_index, anchor = remaining[0]
            members: List[Tuple[int, T_obj, T_obj]] = [
                (anchor_index, anchor, {"accepted": True, "reason": "anchor"})
            ]
            candidates: List[T_obj] = []
            rest = []
            for position, (char_index, char) in enumerate(remaining[1:], start=1):
                if char["doctop"] - anchor["doctop"] > tolerance:
                    rest.extend(remaining[position:])
                    break
                evaluation = _evaluate_duplicate_draw(anchor, char, tolerance)
                if evaluation["accepted"]:
                    members.append((char_index, char, evaluation))
                else:
                    rest.append((char_index, char))
                    if include_candidates:
                        candidates.append(
                            {
                                "index": char_index,
                                "draw": _draw_provenance(char_index, char),
                                "reason": evaluation["reason"],
                                "checks": evaluation["checks"],
                            }
                        )
            remaining = rest

            if len(members) == 1 and not candidates:
                continue

            record = {
                "glyph": dict(
                    zip(("upright", "text") + tuple(extra_attrs or ()), glyph)
                ),
                "tolerance": tolerance,
                "draws": [_draw_provenance(i, c) for i, c, _ in members],
                "kept_index": anchor_index,
                "removed": [
                    {
                        "index": char_index,
                        "draw": _draw_provenance(char_index, char),
                        "reason": evaluation["reason"],
                        "checks": evaluation["checks"],
                    }
                    for char_index, char, evaluation in members[1:]
                ],
                "candidates": candidates,
            }
            groups.append({"anchor_index": anchor_index, "record": record})

    return groups


def dedupe_chars(
    chars: T_obj_list,
    tolerance: T_num = 1,
    extra_attrs: Optional[Tuple[str, ...]] = ("fontname", "size"),
    include_provenance: bool = True,
) -> T_obj_list:
    """
    Removes duplicate chars — those sharing the same glyph, a micro-offset
    (within `tolerance` on both axes), a compatible drawing layer, and a
    matching paint color. Use extra_attrs to be more restrictive with the
    properties shared by the matching chars.

    When `include_provenance` is true, each kept char that absorbed duplicate
    draws carries a ``"dedupe"`` record. The record contains the glyph key,
    the tolerance, the full list of draws (kept first), and for each removed
    draw its source index, geometry, layer, colors, rejection reason, and
    per-signal ``checks``.
    """
    groups = dedupe_char_groups(
        chars, tolerance, extra_attrs, include_candidates=False
    )
    records = {
        group["anchor_index"]: group["record"]
        for group in groups
        if group["record"]["removed"]
    }
    removed = {
        entry["index"] for group in groups for entry in group["record"]["removed"]
    }

    deduped = []
    for index, char in enumerate(chars):
        if index in removed:
            continue
        record = records.get(index)
        if record is None or not include_provenance:
            deduped.append(char)
        else:
            kept = dict(char)
            kept["dedupe"] = record
            deduped.append(kept)
    return deduped

