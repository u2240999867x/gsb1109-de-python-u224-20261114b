"""Match dimension annotations to measurement lines in engineering drawings.

The functions in this module are deliberately independent from PDF parsing.
They accept line segments and text annotations as plain dictionaries (or as
the objects produced by :class:`pdfplumber.Page`), which makes the matching
logic easy to use with pre-processed drawing data as well.
"""

import math
import re
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    cast,
)

from ._typing import T_num

DEFAULT_ANGLE_TOLERANCE: float = 1.0
DEFAULT_DISTANCE_TOLERANCE: float = 3.0
_EPS = 1e-9

Point = Tuple[float, float]
Segment = Tuple[Point, Point]


def _rotate_point(point: Point, degrees: float, origin: Point = (0.0, 0.0)) -> Point:
    if degrees % 360 == 0:
        return point
    radians = math.radians(degrees)
    cos, sin = math.cos(radians), math.sin(radians)
    x, y = point[0] - origin[0], point[1] - origin[1]
    return (x * cos - y * sin + origin[0], x * sin + y * cos + origin[1])


def _rotate_vector(vector: Point, degrees: float) -> Point:
    radians = math.radians(degrees)
    cos, sin = math.cos(radians), math.sin(radians)
    return (
        vector[0] * cos - vector[1] * sin,
        vector[0] * sin + vector[1] * cos,
    )


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _point_from(value: Any) -> Optional[Point]:
    if isinstance(value, dict):
        x = _as_float(value.get("x", value.get("left")))
        y = _as_float(value.get("y", value.get("top")))
        if x is not None and y is not None:
            return (x, y)
    elif isinstance(value, (tuple, list)) and len(value) >= 2:
        x = _as_float(value[0])
        y = _as_float(value[1])
        if x is not None and y is not None:
            return (x, y)
    return None


def _bbox_from(obj: Any) -> Optional[Tuple[float, float, float, float]]:
    """Return ``(x0, top, x1, bottom)`` in page coordinates."""
    if not isinstance(obj, dict):
        return None

    bbox = None
    for key in ("bbox", "text_bbox", "bounds", "rect", "box"):
        if obj.get(key) is not None:
            bbox = obj[key]
            break
    if isinstance(bbox, (tuple, list)) and len(bbox) == 4:
        if all(_as_float(v) is not None for v in bbox):
            x0, y0, x1, y1 = (float(v) for v in bbox)
            return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    width = _as_float(obj.get("width"))
    height = _as_float(obj.get("height"))
    center_x = _as_float(obj.get("x"))
    center_y = _as_float(obj.get("y"))
    if width is not None and height is not None and center_x is not None:
        if center_y is None:
            center_y = _as_float(obj.get("top"))
        if center_y is not None:
            return (
                center_x - width / 2.0,
                center_y - height / 2.0,
                center_x + width / 2.0,
                center_y + height / 2.0,
            )

    left = _as_float(obj.get("x0", obj.get("left")))
    right = _as_float(obj.get("x1", obj.get("right")))
    top = _as_float(obj.get("top"))
    bottom = _as_float(obj.get("bottom"))

    if top is None:
        top = _as_float(obj.get("y0"))
    if bottom is None:
        bottom = _as_float(obj.get("y1"))
    if left is None or right is None or top is None or bottom is None:
        return None
    return (min(left, right), min(top, bottom), max(left, right), max(top, bottom))


def _polygon_from(obj: Any) -> Optional[List[Point]]:
    if not isinstance(obj, dict):
        return None
    for key in ("corners", "bbox_points", "polygon"):
        value = obj.get(key)
        if isinstance(value, (tuple, list)) and len(value) >= 3:
            points = [_point_from(item) for item in value]
            if all(point is not None for point in points):
                return points  # type: ignore[return-value]
    return None


def _polygon_bbox(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _polygon_axis(points: Sequence[Point]) -> Optional[Point]:
    """Return the direction of a polygon's longest edge."""
    best: Optional[Point] = None
    best_length = 0.0
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        dx = next_point[0] - point[0]
        dy = next_point[1] - point[1]
        length = math.hypot(dx, dy)
        if length > best_length + _EPS:
            best = (dx / length, dy / length)
            best_length = length
    return best


def _oriented_corners(
    bbox: Tuple[float, float, float, float], direction: Point
) -> List[Point]:
    """Approximate an oriented text box from an axis-aligned bounding box."""
    center = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
    dx, dy = direction
    length = math.hypot(dx, dy)
    if length < _EPS:
        return _bbox_corners(bbox)
    dx, dy = dx / length, dy / length
    px, py = -dy, dx
    projections = [
        ((corner[0] - center[0]) * dx + (corner[1] - center[1]) * dy,
         (corner[0] - center[0]) * px + (corner[1] - center[1]) * py)
        for corner in _bbox_corners(bbox)
    ]
    along = [value[0] for value in projections]
    across = [value[1] for value in projections]
    half_along = (max(along) - min(along)) / 2.0
    half_across = (max(across) - min(across)) / 2.0
    return [
        (
            center[0] + dx * sx * half_along + px * sy * half_across,
            center[1] + dy * sx * half_along + py * sy * half_across,
        )
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    ]


def _anchor_from(obj: Any) -> Optional[Point]:
    for key in ("anchor", "origin", "position", "point"):
        point = _point_from(obj.get(key)) if isinstance(obj, dict) else None
        if point is not None:
            return point
    if isinstance(obj, dict):
        x = _as_float(obj.get("x"))
        y = _as_float(obj.get("y"))
        if x is not None and y is not None:
            return (x, y)
    return None


def segment_endpoints(line: Any) -> Segment:
    """Extract the endpoints of a line segment.

    Accepts pdfplumber line/edge dictionaries, dictionaries with explicit
    ``p0``/``p1`` (or ``start``/``end``) keys, and two-point tuples.
    """
    if not isinstance(line, dict) and not isinstance(line, (tuple, list)):
        attrs = {
            key: getattr(line, key)
            for key in ("x0", "x1", "top", "bottom", "pts", "path", "p0", "p1")
            if hasattr(line, key)
        }
        if attrs:
            line = attrs

    if isinstance(line, (tuple, list)) and len(line) == 2:
        p0 = _point_from(line[0])
        p1 = _point_from(line[1])
        if p0 is not None and p1 is not None:
            return (p0, p1)

    if not isinstance(line, dict):
        raise ValueError(f"Cannot interpret line segment: {line!r}")

    for start_key, end_key in (("p0", "p1"), ("start", "end")):
        if start_key in line and end_key in line:
            p0 = _point_from(line[start_key])
            p1 = _point_from(line[end_key])
            if p0 is not None and p1 is not None:
                return (p0, p1)

    pts = line.get("pts")
    if isinstance(pts, (tuple, list)) and len(pts) >= 2:
        p0 = _point_from(pts[0])
        p1 = _point_from(pts[-1])
        if p0 is not None and p1 is not None:
            return (p0, p1)

    path = line.get("path")
    if isinstance(path, (tuple, list)) and path:
        path_points: List[Point] = []
        for item in path:
            if isinstance(item, (tuple, list)):
                for value in item[1:]:
                    point = _point_from(value)
                    if point is not None:
                        path_points.append(point)
        if len(path_points) >= 2:
            return (path_points[0], path_points[-1])

    x0 = _as_float(line.get("x0", line.get("left")))
    x1 = _as_float(line.get("x1", line.get("right")))
    top = _as_float(line.get("top"))
    bottom = _as_float(line.get("bottom"))
    if top is None:
        top = _as_float(line.get("y0"))
    if bottom is None:
        bottom = _as_float(line.get("y1"))
    if None not in (x0, x1, top, bottom):
        return ((x0, top), (x1, bottom))  # type: ignore[return-value]

    raise ValueError(f"Cannot interpret line segment: {line!r}")


def segment_angle(line: Any) -> float:
    """Return the undirected angle of a segment, in degrees and in [0, 180)."""
    (x0, y0), (x1, y1) = segment_endpoints(line)
    return math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0


def _angle_difference(a: float, b: float) -> float:
    """Smallest difference between two undirected angles, in degrees."""
    diff = abs((a - b) % 180.0)
    return min(diff, 180.0 - diff)


_DIRECTION_STRINGS: Dict[str, Point] = {
    "ltr": (1.0, 0.0),
    "rtl": (-1.0, 0.0),
    "ttb": (0.0, 1.0),
    "btt": (0.0, -1.0),
}


def _direction_from_matrix(matrix: Any) -> Optional[Point]:
    if isinstance(matrix, (tuple, list)) and len(matrix) >= 4:
        a = _as_float(matrix[0])
        b = _as_float(matrix[1])
        if a is None or b is None:
            return None
        # pdfplumber exposes page coordinates with the y-axis pointing down,
        # while PDF text matrices use the PDF user-space y-axis (pointing up).
        return (a, -b)
    return None


def _is_mirrored(text: Any) -> bool:
    if not isinstance(text, dict):
        return False
    if bool(text.get("mirrored", text.get("mirror", False))):
        return True
    matrix = text.get("matrix")
    if matrix is None and isinstance(text.get("chars"), (tuple, list)):
        chars = text["chars"]
        if chars and isinstance(chars[0], dict):
            matrix = chars[0].get("matrix")
    if isinstance(matrix, (tuple, list)) and len(matrix) >= 4:
        raw_values = [_as_float(value) for value in matrix[:4]]
        values = raw_values
        if all(value is not None for value in values):
            a, b, c, d = cast(
                Tuple[float, float, float, float], tuple(values)
            )
            return (a * d - b * c) < 0
    return False


def text_direction(text: Any, rotation: float = 0.0) -> Point:
    """Return a unit vector for the reading direction of a text annotation.

    Page coordinates use y pointing down, matching pdfplumber. Explicit
    ``direction``/``vector`` values are interpreted in page coordinates.
    ``angle`` and ``rotation`` are measured from +x towards +y, in degrees.
    """
    direction: Optional[Point] = None

    if isinstance(text, dict):
        explicit_angle = False
        vector = text.get("vector")
        if isinstance(vector, (tuple, list, dict)):
            direction = _point_from(vector)

        if direction is None:
            angle = _as_float(text.get("angle"))
            if angle is None:
                angle = _as_float(text.get("rotation"))
            if angle is None:
                angle = _as_float(text.get("text_angle"))
            if angle is None:
                angle = _as_float(text.get("orientation"))
            if angle is not None:
                radians = math.radians(angle)
                direction = (math.cos(radians), math.sin(radians))
                explicit_angle = True

        if direction is None:
            polygon = _polygon_from(text)
            if polygon is not None:
                direction = _polygon_axis(polygon)

        if direction is None:
            matrix = text.get("matrix")
            if matrix is None and isinstance(text.get("chars"), (tuple, list)):
                chars = text["chars"]
                if chars and isinstance(chars[0], dict):
                    matrix = chars[0].get("matrix")
            direction = _direction_from_matrix(matrix)

        if direction is None:
            raw_direction = text.get("direction")
            if isinstance(raw_direction, str):
                direction = _DIRECTION_STRINGS.get(raw_direction.lower())

        if direction is None:
            bbox = _bbox_from(text)
            if bbox is not None:
                width = bbox[2] - bbox[0]
                height = bbox[3] - bbox[1]
                direction = (1.0, 0.0) if width >= height else (0.0, 1.0)
            elif bool(text.get("upright", True)):
                direction = (1.0, 0.0)
            else:
                direction = (0.0, 1.0)

        # A mirrored text baseline is reflected about its anchor's vertical
        # axis, so an explicitly stated direction theta reads as 180 - theta.
        if (
            explicit_angle
            and direction is not None
            and bool(text.get("mirrored", text.get("mirror", False)))
        ):
            direction = (-direction[0], direction[1])

    if direction is None:
        direction = (1.0, 0.0)

    if rotation:
        direction = _rotate_vector(direction, rotation)

    length = math.hypot(*direction)
    if length < _EPS:
        return (1.0, 0.0)
    return (direction[0] / length, direction[1] / length)


def text_angle(text: Any, rotation: float = 0.0) -> float:
    """Return the undirected text reading angle, in degrees and in [0, 180)."""
    dx, dy = text_direction(text, rotation=rotation)
    return round(math.degrees(math.atan2(dy, dx)) % 180.0, 12)


_NUMBER_RE = re.compile(
    r"[-+]?(?:"
    r"(?P<whole>\d+)\s+(?P<num>\d+)\s*/\s*(?P<den>\d+)"
    r"|(?P<fnum>\d+)\s*/\s*(?P<fden>\d+)"
    r"|(?P<dec>\d+(?:[.,]\d+)?|[.,]\d+)"
    r")"
)

_UNIT_RE = re.compile(
    r"\s*(?P<unit>"
    r"millimet(?:er|re)s?|mm|centimet(?:er|re)s?|cm|decimet(?:er|re)s?|dm|"
    r"kilomet(?:er|re)s?|km|met(?:er|re)s?|m|"
    r"inches|inch|in|feet|foot|ft|degrees?|degree|deg|"
    r"毫米|厘米|英寸|米|度|°|(?!\d)\"|(?!\d)'"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_UNIT_ALIASES = {
    "millimeter": "mm",
    "millimeters": "mm",
    "millimetre": "mm",
    "millimetres": "mm",
    "centimeter": "cm",
    "centimeters": "cm",
    "centimetre": "cm",
    "centimetres": "cm",
    "decimeter": "dm",
    "decimeters": "dm",
    "decimetre": "dm",
    "decimetres": "dm",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "inch": "in",
    "inches": "in",
    '"': "in",
    "foot": "ft",
    "feet": "ft",
    "ft": "ft",
    "'": "ft",
    "degree": "deg",
    "degrees": "deg",
    "deg": "deg",
    "°": "deg",
    "毫米": "mm",
    "厘米": "cm",
    "米": "m",
    "英寸": "in",
    "度": "deg",
}

_PREFIXES = (
    ("%%c", "diameter", "Ø"),
    ("%%C", "diameter", "Ø"),
    ("Ø", "diameter", "Ø"),
    ("⌀", "diameter", "Ø"),
    ("Φ", "diameter", "Ø"),
    ("φ", "diameter", "Ø"),
    ("R", "radius", "R"),
    ("r", "radius", "R"),
)

_COUNT_RE = re.compile(r"^\s*(?P<count>\d{1,2})X\s*")

_ANNOTATION_RE = re.compile(
    r"^\s*(?:\d{1,2}X\s*)?(?:%%[cC]|Ø|⌀|Φ|φ|R\s*)?"
    r"[-+]?(?:\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:[.,]\d+)?|[.,]\d+)"
    r"(?:\s*±\s*\d+(?:[.,]\d+)?"
    r"|\s*[+\-]\s*\d+(?:[.,]\d+)?(?:\s*/\s*[+\-]?\s*\d+(?:[.,]\d+)?)?)?"
    r"(?:\s*(?:mm|cm|dm|m|km|in|inch|inches|ft|feet|foot|deg|°|\"|'))?"
    r"(?:\s*(?:H|h|IT)\s*\d{1,2})?"
    r"(?:\s+(?:REF|TYP|MAX|MIN|THRU|CBORE|CSK|DP|DEEP))?"
    r"\s*$",
    re.IGNORECASE,
)


class ParsedDimension(Tuple[Optional[float], str]):
    """A ``(value, unit)`` tuple with parsed annotation details attached."""

    value: Optional[float]
    unit: str
    text: str
    prefix: str
    kind: str
    tolerance: Optional[float]
    quantity: Optional[int]

    def __new__(
        cls,
        value: Optional[float],
        unit: str,
        text: str = "",
        prefix: str = "",
        kind: str = "linear",
        tolerance: Optional[float] = None,
        count: Optional[int] = None,
    ) -> "ParsedDimension":
        obj = super().__new__(cls, (value, unit))
        obj.value = value
        obj.unit = unit
        obj.text = text
        obj.prefix = prefix
        obj.kind = kind
        obj.tolerance = tolerance
        obj.quantity = count
        return obj


def parse_dimension(text: Any) -> ParsedDimension:
    """Parse a dimension annotation into value, unit, prefix and kind."""
    if not isinstance(text, str):
        if isinstance(text, dict):
            text = str(text.get("text", ""))
        else:
            text = str(text)

    raw = text.strip().replace("−", "-").replace("–", "-")
    prefix = ""
    kind = "linear"
    count: Optional[int] = None
    count_match = _COUNT_RE.match(raw)
    if count_match is not None:
        count = int(count_match.group("count"))
        raw = raw[count_match.end() :]
    for symbol, parsed_kind, canonical in _PREFIXES:
        if symbol == "r" and raw[:2].lower() == "rd":
            continue
        if raw.startswith(symbol):
            prefix = canonical
            kind = parsed_kind
            raw = raw[len(symbol) :].lstrip()
            break

    match = _NUMBER_RE.search(raw)
    value: Optional[float] = None
    end = 0
    if match is not None:
        end = match.end()
        if match.group("whole") is not None:
            den = _as_float(match.group("den"))
            value = float(match.group("whole"))
            if den:
                value += float(match.group("num")) / den
        elif match.group("fnum") is not None:
            den = _as_float(match.group("fden"))
            if den:
                value = float(match.group("fnum")) / den
        else:
            decimal = match.group("dec")
            value = _as_float(decimal.replace(",", "."))

    unit = ""
    unit_match = _UNIT_RE.match(raw, end)
    if unit_match is None:
        balanced_tolerance = re.match(
            r"\s*(?:±\s*)?[+\-]?\s*\d+(?:[.,]\d+)?"
            r"(?:\s*/\s*[+\-]?\s*\d+(?:[.,]\d+)?)?",
            raw[end:],
        )
        if balanced_tolerance is not None:
            unit_match = _UNIT_RE.match(raw, end + balanced_tolerance.end())
    if unit_match is not None:
        unit = unit_match.group("unit")
        unit = _UNIT_ALIASES.get(unit.lower(), unit.lower())
        if unit == "deg":
            kind = "angle"
    if unit == "" and "°" in raw:
        unit = "deg"
        kind = "angle"

    if "%%d" in text or "%%D" in text:
        unit = "deg"
        kind = "angle"

    tolerance: Optional[float] = None
    tolerance_match = re.search(r"±\s*([-+]?\d+(?:[.,]\d+)?)", raw)
    if tolerance_match is not None:
        tolerance = _as_float(tolerance_match.group(1).replace(",", "."))

    return ParsedDimension(value, unit, text, prefix, kind, tolerance, count)


def parse_dimension_text(text: Any) -> Tuple[Optional[float], str]:
    """Return ``(value, unit)`` for a dimension annotation."""
    parsed = parse_dimension(text)
    return (parsed.value, parsed.unit)


def collect_dimension_texts(page: Any) -> List[Dict[str, Any]]:
    """Collect numeric text lines from a pdfplumber-like page."""
    texts: List[Dict[str, Any]] = []
    for text_line in page.extract_text_lines(return_chars=True):
        raw_text = text_line.get("text", "")
        parsed = parse_dimension(raw_text)
        if parsed.value is None:
            continue
        if parsed.unit == "" and parsed.prefix == "":
            continue
        annotation = raw_text.strip()
        if not _ANNOTATION_RE.match(annotation):
            continue
        if len(annotation) > 40:
            continue
        chars = text_line.get("chars") or []
        if chars and isinstance(chars[0], dict) and chars[0].get("matrix"):
            text_line = {**text_line, "matrix": chars[0]["matrix"]}
        texts.append(text_line)
    return texts


def _text_geometry(text: Any, rotation: float = 0.0) -> Optional[Dict[str, Any]]:
    if isinstance(text, str):
        text = {"text": text}
        if rotation:
            text["angle"] = rotation
    if isinstance(text, (tuple, list)) and text and isinstance(text[0], str):
        raw = text[0]
        rest = list(text[1:])
        text_dict: Dict[str, Any] = {"text": raw}
        if rest and isinstance(rest[0], (tuple, list)) and len(rest[0]) == 4:
            text_dict["bbox"] = rest[0]
            rest = rest[1:]
        elif len(rest) >= 2 and all(_as_float(value) is not None for value in rest[:2]):
            text_dict["anchor"] = (rest[0], rest[1])
            rest = rest[2:]
        if rest:
            angle = _as_float(rest[0])
            if angle is not None:
                text_dict["angle"] = angle
        text = text_dict
    elif isinstance(text, (tuple, list)) and len(text) >= 3:
        if _as_float(text[0]) is not None and _as_float(text[1]) is not None:
            text_dict = {"text": str(text[2]), "anchor": (text[0], text[1])}
            if len(text) > 3 and _as_float(text[3]) is not None:
                text_dict["angle"] = text[3]
            text = text_dict
    if not isinstance(text, (tuple, list, dict)):
        attrs = {
            key: getattr(text, key)
            for key in (
                "text",
                "value",
                "x0",
                "x1",
                "top",
                "bottom",
                "bbox",
                "matrix",
                "angle",
                "upright",
                "chars",
            )
            if hasattr(text, key)
        }
        if not attrs:
            return None
        text = attrs

    if "text" not in text and "value" not in text:
        for key in ("string", "label", "content", "annotation"):
            if isinstance(text.get(key), str):
                text = {**text, "text": text[key]}
                break

    polygon = _polygon_from(text)
    bbox = _polygon_bbox(polygon) if polygon is not None else _bbox_from(text)
    anchor = _anchor_from(text)
    if bbox is None and anchor is not None:
        bbox = (anchor[0], anchor[1], anchor[0], anchor[1])
    if bbox is None:
        return None

    if anchor is None:
        anchor = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)

    if rotation:
        corners = _bbox_corners(bbox) if polygon is None else list(polygon)
        corners = [_rotate_point(point, rotation) for point in corners]
        polygon = corners
        bbox = _polygon_bbox(corners)
        anchor = _rotate_point(anchor, rotation)

    raw_text = text.get("text")
    if raw_text is None:
        raw_text = text.get("value")
    raw_text = "" if raw_text is None else str(raw_text)

    parsed = parse_dimension(raw_text)
    explicit_value = _as_float(text.get("value"))
    if parsed.value is None and explicit_value is not None:
        parsed = ParsedDimension(
            explicit_value,
            parsed.unit,
            raw_text,
            parsed.prefix,
            parsed.kind,
            parsed.tolerance,
            parsed.quantity,
        )
    explicit_unit = text.get("unit")
    if parsed.unit == "" and isinstance(explicit_unit, str):
        parsed = ParsedDimension(
            parsed.value,
            _UNIT_ALIASES.get(explicit_unit.lower(), explicit_unit.lower()),
            raw_text,
            parsed.prefix,
            parsed.kind,
            parsed.tolerance,
            parsed.quantity,
        )
    explicit_block = _as_float(text.get("block"))
    if explicit_block is not None and parsed.value is None:
        parsed = ParsedDimension(
            0.0,
            parsed.unit,
            raw_text,
            parsed.prefix,
            parsed.kind,
            parsed.tolerance,
            parsed.quantity,
        )

    direction = text_direction(text, rotation=rotation)
    angle = text_angle(text, rotation=rotation)
    if polygon is None:
        angle_mod = angle % 90.0
        if min(angle_mod, 90.0 - angle_mod) > 1e-6:
            polygon = _oriented_corners(bbox, direction)

    return {
        "text": raw_text,
        "bbox": bbox,
        "corners": polygon if polygon is not None else _bbox_corners(bbox),
        "anchor": anchor,
        "direction": direction,
        "angle": angle,
        "mirrored": _is_mirrored(text),
        "parsed": parsed,
    }


def _line_geometry(line: Any, rotation: float = 0.0) -> Optional[Dict[str, Any]]:
    try:
        p0, p1 = segment_endpoints(line)
    except (TypeError, ValueError):
        return None
    if rotation:
        p0 = _rotate_point(p0, rotation)
        p1 = _rotate_point(p1, rotation)
    dx = p1[0] - p0[0]
    dy = p1[1] - p0[1]
    length = math.hypot(dx, dy)
    if length < _EPS:
        return None
    ux, uy = dx / length, dy / length
    return {
        "p0": p0,
        "p1": p1,
        "length": length,
        "unit": (ux, uy),
        "angle": math.degrees(math.atan2(dy, dx)) % 180.0,
    }


def _project(point: Point, origin: Point, unit: Point) -> float:
    return (point[0] - origin[0]) * unit[0] + (point[1] - origin[1]) * unit[1]


def _interval_overlap(
    a: Tuple[float, float], b: Tuple[float, float], tolerance: float = 0.0
) -> float:
    """Length of the overlap between two closed intervals, with slack."""
    lo = max(a[0], b[0] - tolerance)
    hi = min(a[1], b[1] + tolerance)
    return max(0.0, hi - lo)


def _signed_perpendicular_distance(
    point: Point, origin: Point, unit: Point
) -> float:
    return (point[0] - origin[0]) * unit[1] - (point[1] - origin[1]) * unit[0]


def _polygon_line_distance(
    points: Sequence[Point], origin: Point, unit: Point
) -> float:
    """Distance between a convex polygon and an infinite line.

    The line crosses the polygon when its signed normal offsets span zero,
    in which case the distance is zero. Otherwise the nearest corner wins.
    """
    offsets = [
        _signed_perpendicular_distance(point, origin, unit) for point in points
    ]
    if min(offsets) <= 0 <= max(offsets):
        return 0.0
    return min(abs(offset) for offset in offsets)


def _bbox_corners(bbox: Tuple[float, float, float, float]) -> List[Point]:
    x0, y0, x1, y1 = bbox
    return [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]


def _match_metrics(
    text_info: Dict[str, Any],
    line_info: Dict[str, Any],
    angle_tolerance: float,
    distance_tolerance: float,
    min_projection_overlap: float,
) -> Optional[Dict[str, Any]]:
    angle_error = _angle_difference(text_info["angle"], line_info["angle"])
    if angle_error > angle_tolerance + _EPS:
        return None

    corners = text_info["corners"]
    origin = line_info["p0"]
    unit = line_info["unit"]
    length = line_info["length"]

    projections = [_project(corner, origin, unit) for corner in corners]
    text_lo, text_hi = min(projections), max(projections)
    anchor_t = _project(text_info["anchor"], origin, unit)
    distance = _polygon_line_distance(corners, origin, unit)

    if distance > distance_tolerance + _EPS:
        return None
    if anchor_t < -distance_tolerance - _EPS:
        return None
    if anchor_t > length + distance_tolerance + _EPS:
        return None
    if text_hi < -distance_tolerance - _EPS:
        return None
    if text_lo > length + distance_tolerance + _EPS:
        return None

    text_projection_length = text_hi - text_lo
    overlap = _interval_overlap((text_lo, text_hi), (0.0, length), distance_tolerance)
    if text_projection_length <= _EPS:
        overlap_ratio = 1.0
    else:
        if overlap <= _EPS:
            return None
        denominator = max(min(text_projection_length, length), _EPS)
        overlap_ratio = min(1.0, overlap / denominator)
    if overlap_ratio < min_projection_overlap - _EPS:
        return None

    center_t = anchor_t
    endpoint_slack = min(center_t, length - center_t)
    return {
        "angle_error": angle_error,
        "distance": distance,
        "center_t": center_t,
        "projection_overlap": overlap_ratio,
        "projection_overlap_length": overlap,
        "endpoint_slack": endpoint_slack,
        "evidence": (
            angle_error,
            distance,
            center_t,
            overlap,
        ),
    }


def evidence_matches(
    a: Dict[str, Any],
    b: Dict[str, Any],
    angle_tolerance: float,
    distance_tolerance: float,
) -> bool:
    """Whether two matches carry the same text-to-line evidence."""
    angle_slack = max(1e-9, angle_tolerance * 1e-6)
    distance_slack = max(1e-9, distance_tolerance * 1e-6)
    return bool(
        abs(a["angle_error"] - b["angle_error"]) <= angle_slack
        and abs(a["distance"] - b["distance"]) <= distance_slack
        and abs(a["center_t"] - b["center_t"]) <= distance_slack
        and abs(a["projection_overlap_length"] - b["projection_overlap_length"])
        <= distance_slack
    )


_ALIAS_KWARGS = {
    "angle_tol": "angle_tolerance",
    "angle_tolerance_deg": "angle_tolerance",
    "angle_tolerance_degrees": "angle_tolerance",
    "max_angle_diff": "angle_tolerance",
    "distance_tol": "distance_tolerance",
    "dist_tolerance": "distance_tolerance",
    "max_distance": "distance_tolerance",
    "distance_tolerance_points": "distance_tolerance",
    "overlap_tolerance": "min_projection_overlap",
}


def build_dimension_index(
    lines: Optional[Iterable[Any]] = None,
    texts: Optional[Iterable[Any]] = None,
    angle_tolerance: float = DEFAULT_ANGLE_TOLERANCE,
    distance_tolerance: float = DEFAULT_DISTANCE_TOLERANCE,
    *,
    min_projection_overlap: float = 0.0,
    include_unmatched: bool = False,
    require_value: bool = False,
    rotation: float = 0.0,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Return compatible ``(dimension text, line segment)`` correspondences.

    A correspondence is kept when:

    * the text reading axis and the line axis differ by no more than
      ``angle_tolerance`` degrees (undirected; 180-degree reversals match);
    * the gap between the text box and the line is no more than
      ``distance_tolerance`` page units;
    * the text projects onto the line (``projection_overlap``), with its
      center landing inside the line's endpoint range, allowing
      ``distance_tolerance`` of slack at each end.

    Parallel lines sharing the same evidence are not collapsed: one record is
    returned for each line. Results are ordered by input text index and then
    input line index, so rotating the whole page cannot reorder matches.

    ``rotation`` rotates both texts and lines into a common frame before
    comparison. ``lines`` and ``texts`` may also be supplied as keyword
    aliases (``segments``/``measurement_lines`` and
    ``annotations``/``dimension_texts``), or as a single mapping containing
    those keys.
    """
    page = kwargs.pop("page", None)
    if page is None and hasattr(lines, "extract_text_lines"):
        page = lines
    if page is not None:
        if lines is page or lines is None:
            lines = page.lines
        if texts is None:
            texts = collect_dimension_texts(page)
    for alias, canonical in _ALIAS_KWARGS.items():
        if alias in kwargs:
            if canonical == "angle_tolerance":
                angle_tolerance = kwargs.pop(alias)
            elif canonical == "distance_tolerance":
                distance_tolerance = kwargs.pop(alias)
            else:
                min_projection_overlap = kwargs.pop(alias)
    if "segments" in kwargs or "measurement_lines" in kwargs:
        lines = kwargs.pop("segments", kwargs.pop("measurement_lines", lines))
    for rotation_key in ("page_rotation", "rotate_degrees", "coordinate_rotation"):
        if rotation_key in kwargs:
            rotation = kwargs.pop(rotation_key)
    for text_key in (
        "annotations",
        "text_annotations",
        "text_objects",
        "labels",
        "dimension_texts",
    ):
        if text_key in kwargs:
            texts = kwargs.pop(text_key)
            break
    if texts is None:
        if isinstance(lines, dict):
            bundle = lines
            for line_key in ("lines", "segments", "geometry"):
                if line_key in bundle:
                    lines = bundle[line_key]
                    break
            else:
                raise ValueError("No line segments found in the input mapping")
            for text_key in ("texts", "text", "dimensions", "annotations"):
                if text_key in bundle:
                    texts = bundle[text_key]
                    break
            else:
                raise ValueError("No dimension texts found in the input mapping")
        else:
            raise ValueError("texts must be provided (or included in a mapping)")
    if kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TypeError(f"Unexpected keyword argument(s): {unexpected}")

    if angle_tolerance < 0:
        raise ValueError("angle_tolerance must be non-negative")
    if distance_tolerance < 0:
        raise ValueError("distance_tolerance must be non-negative")
    if not 0 <= min_projection_overlap <= 1:
        raise ValueError("min_projection_overlap must be between 0 and 1")

    if lines is None:
        raise ValueError("lines must be provided")
    line_list = list(lines)
    text_list = list(texts)
    line_infos = [_line_geometry(line, rotation=rotation) for line in line_list]

    records: List[Dict[str, Any]] = []
    for text_index, text in enumerate(text_list):
        text_info = _text_geometry(text, rotation=rotation)
        if text_info is None:
            continue

        parsed = text_info["parsed"]
        if require_value and parsed.value is None:
            continue

        candidates: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
        for line_index, line_info in enumerate(line_infos):
            if line_info is None:
                continue
            metrics = _match_metrics(
                text_info,
                line_info,
                angle_tolerance,
                distance_tolerance,
                min_projection_overlap,
            )
            if metrics is not None:
                candidates.append((line_index, line_info, metrics))

        if not candidates and include_unmatched:
            records.append(
                {
                    "text": text_info["text"],
                    "raw_text": text_info["text"],
                    "value": parsed.value,
                    "unit": parsed.unit,
                    "prefix": parsed.prefix,
                    "kind": parsed.kind,
                    "tolerance": parsed.tolerance,
                    "text_index": text_index,
                    "text_anchor": text_info["anchor"],
                    "line": None,
                    "object": None,
                    "line_index": None,
                    "line_angle": None,
                    "angle": text_info["angle"],
                    "text_bbox": text_info["bbox"],
                    "lines": [],
                    "group": [],
                    "group_size": 0,
                    "matched": False,
                }
            )
            continue

        evidence_groups: List[List[int]] = []
        group_for: Dict[int, List[int]] = {}
        metrics_by_index = {
            line_index: metrics for line_index, _info, metrics in candidates
        }
        for line_index, _line_info, metrics in candidates:
            for group in evidence_groups:
                reference = metrics_by_index[group[0]]
                if evidence_matches(
                    metrics,
                    reference,
                    angle_tolerance,
                    distance_tolerance,
                ):
                    group.append(line_index)
                    group_for[line_index] = group
                    break
            else:
                group = [line_index]
                evidence_groups.append(group)
                group_for[line_index] = group

        for line_index, line_info, metrics in candidates:
            group = group_for[line_index]
            line = line_list[line_index]
            record = {
                "text": text_info["text"],
                "raw_text": text_info["text"],
                "value": parsed.value,
                "unit": parsed.unit,
                "prefix": parsed.prefix,
                "kind": parsed.kind,
                "tolerance": parsed.tolerance,
                "text_index": text_index,
                "text_bbox": text_info["bbox"],
                "text_anchor": text_info["anchor"],
                "angle": text_info["angle"],
                "line": line,
                "object": line,
                "line_index": line_index,
                "line_angle": line_info["angle"],
                "line_endpoints": (line_info["p0"], line_info["p1"]),
                "angle_error": metrics["angle_error"],
                "distance": metrics["distance"],
                "projection_overlap": metrics["projection_overlap"],
                "projection_overlap_length": metrics["projection_overlap_length"],
                "endpoint_slack": metrics["endpoint_slack"],
                "evidence": metrics["evidence"],
                "group": group,
                "group_size": len(group),
                "matched": True,
            }
            record["lines"] = [line_list[i] for i in group]
            records.append(record)

    records.sort(key=lambda r: (r["text_index"], r.get("line_index", -1)))
    return records


class DimensionIndex:
    """A small container around the records returned by ``build_dimension_index``."""

    def __init__(
        self,
        records: Optional[Iterable[Any]] = None,
        texts: Optional[Iterable[Any]] = None,
        angle_tolerance: float = DEFAULT_ANGLE_TOLERANCE,
        distance_tolerance: float = DEFAULT_DISTANCE_TOLERANCE,
        **kwargs: Any,
    ):
        if texts is not None:
            self.records = build_dimension_index(
                records or [],
                texts,
                angle_tolerance=angle_tolerance,
                distance_tolerance=distance_tolerance,
                **kwargs,
            )
        else:
            self.records = list(records or [])

    @classmethod
    def build(cls, *args: Any, **kwargs: Any) -> "DimensionIndex":
        return cls(build_dimension_index(*args, **kwargs))

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        return self.records[index]

    @property
    def matches(self) -> List[Dict[str, Any]]:
        return [record for record in self.records if record.get("matched", True)]

    def by_text(self, text: str) -> List[Dict[str, Any]]:
        return [record for record in self.records if record["text"] == text]

    def by_value(self, value: T_num) -> List[Dict[str, Any]]:
        return [record for record in self.records if record["value"] == value]

    def by_unit(self, unit: str) -> List[Dict[str, Any]]:
        return [record for record in self.records if record["unit"] == unit]

    def groups(self) -> List[List[Dict[str, Any]]]:
        grouped: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
        for record in self.records:
            if not record.get("matched", True):
                continue
            key = (record["text_index"], record["evidence"])
            grouped.setdefault(key, []).append(record)
        return list(grouped.values())

    def to_list(self) -> List[Dict[str, Any]]:
        return self.records

    def to_pairs(self) -> List[Tuple[str, Any]]:
        return [
            (record["text"], record["line"])
            for record in self.records
            if record.get("matched", True)
        ]


def dimension_index(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def build_index(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def index_dimensions(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def find_dimensions(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def match_dimensions(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def match(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def extract_dimensions(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def find(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def link_dimensions(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def extract_dimension_index(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias for :func:`build_dimension_index`."""
    return build_dimension_index(*args, **kwargs)


def build_dimension_pairs(*args: Any, **kwargs: Any) -> List[Tuple[str, Any]]:
    """Return ``(text, line)`` pairs from :func:`build_dimension_index`."""
    return [
        (record["text"], record["line"])
        for record in build_dimension_index(*args, **kwargs)
        if record.get("matched", True)
    ]


def dimension_pairs(*args: Any, **kwargs: Any) -> List[Tuple[str, Any]]:
    return build_dimension_pairs(*args, **kwargs)


def match_dimension_pairs(*args: Any, **kwargs: Any) -> List[Tuple[str, Any]]:
    return build_dimension_pairs(*args, **kwargs)


__all__ = [
    "DEFAULT_ANGLE_TOLERANCE",
    "DEFAULT_DISTANCE_TOLERANCE",
    "DimensionIndex",
    "ParsedDimension",
    "build_dimension_index",
    "build_dimension_pairs",
    "build_index",
    "collect_dimension_texts",
    "dimension_index",
    "dimension_pairs",
    "extract_dimensions",
    "extract_dimension_index",
    "find",
    "find_dimensions",
    "index_dimensions",
    "link_dimensions",
    "match_dimensions",
    "match_dimension_pairs",
    "match",
    "parse_dimension",
    "parse_dimension_text",
    "segment_angle",
    "segment_endpoints",
    "text_angle",
    "text_direction",
]
