"""Recover named data points from a vector-PDF line chart.

The target use case is a printed annual-report chart where every indicator
is drawn in the same color and series identity is carried only by dash
style (dotted vs. long dash).  The extractor therefore:

* reads legend sample lines and associates each dash array with the label
  text next to it;
* matches plotted paths to a series by dash array, ignoring the dash phase
  so that subpaths starting at different phases still join one series;
* keeps independent stroked subpaths separate, so a genuine measurement gap
  is never bridged or interpolated;
* leaves segments unnamed when their style matches no unique legend entry
  (for example the shared-style tails after two series cross), while still
  returning the identifiable samples before the crossing;
* keeps references to the original page objects every point came from.

Only standard pdfplumber page objects are used, so the page itself is left
untouched and remains the source of truth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Sequence, Tuple

from ._typing import T_num, T_obj, T_point

if TYPE_CHECKING:
    from .page import Page

# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegendEntry:
    """One legend sample line and the label next to it."""

    name: str
    dash_array: Tuple[float, ...]
    phase: float
    sample: T_obj


@dataclass(frozen=True)
class AxisTick:
    value: float
    coord: float


@dataclass(frozen=True)
class LinearScale:
    """Linear mapping between PDF coordinates and chart-data coordinates."""

    axis: Literal["x", "y"]
    ticks: Tuple[AxisTick, ...]
    slope: float
    intercept: float

    @classmethod
    def from_ticks(
        cls, axis: Literal["x", "y"], ticks: Sequence[AxisTick]
    ) -> "LinearScale":
        if len(ticks) < 2:
            raise ValueError("at least two axis ticks are required")
        ordered = sorted(ticks, key=lambda tick: tick.coord)
        (first, last) = (ordered[0], ordered[-1])
        span = last.coord - first.coord
        if span == 0:
            raise ValueError("axis ticks share one coordinate")
        slope = (last.value - first.value) / span
        intercept = first.value - slope * first.coord
        scale = cls(axis, tuple(ordered), slope, intercept)
        for tick in ordered:
            if not math.isclose(
                scale.to_data(tick.coord), tick.value, rel_tol=0, abs_tol=1e-6
            ):
                raise ValueError("axis ticks are not collinear")
        return scale

    def to_data(self, coord: T_num) -> float:
        return self.intercept + self.slope * float(coord)

    def to_coord(self, value: T_num) -> float:
        return (float(value) - self.intercept) / self.slope


@dataclass
class DataPoint:
    """One recovered vertex, mapped back onto the chart's data scales."""

    series: Optional[str]
    x_value: float
    y_value: float
    x: float
    y: float
    connected_to_next: bool
    status: Literal["named", "ambiguous"]
    ambiguity_reason: Optional[str]
    source: T_obj
    source_ref: str
    subpath_index: int
    point_index: int
    candidate_series: Optional[Tuple[str, ...]] = None

    def as_record(self) -> Dict[str, Any]:
        return {
            "series": self.series,
            "status": self.status,
            "candidate_series": list(self.candidate_series)
            if self.candidate_series
            else None,
            "x": self.x_value,
            "y": self.y_value,
            "connected_to_next": self.connected_to_next,
            "ambiguity_reason": self.ambiguity_reason,
            "source": self.source_ref,
        }


@dataclass
class AmbiguousSegment:
    """A stroked subpath whose series cannot be named from the legend."""

    dash_array: Tuple[float, ...]
    phase: float
    reason: str
    points: List[DataPoint]
    source: T_obj
    source_ref: str
    subpath_index: int
    candidate_series: Tuple[str, ...]


@dataclass
class ChartExtraction:
    legend: List[LegendEntry]
    x_scale: LinearScale
    y_scale: LinearScale
    series_points: Dict[str, List[DataPoint]]
    ambiguous_segments: List[AmbiguousSegment]
    gaps: Dict[str, List[float]]
    frame: T_obj
    sources: List[T_obj] = field(default_factory=list)

    @property
    def points(self) -> List[DataPoint]:
        points: List[DataPoint] = []
        for named in self.series_points.values():
            points.extend(named)
        for segment in self.ambiguous_segments:
            points.extend(segment.points)
        return sorted(points, key=lambda point: (point.x, point.y))

    def to_records(self) -> List[Dict[str, Any]]:
        return [point.as_record() for point in self.points]


# ---------------------------------------------------------------------------
# Style helpers
# ---------------------------------------------------------------------------


def normalize_color(color: Any) -> Optional[Tuple[float, ...]]:
    """Normalize a pdfplumber color to a float tuple.

    Grayscale colors arrive as a plain number; RGB/CMYK colors as tuples.
    """

    if color is None:
        return None
    if isinstance(color, (int, float)):
        return (float(color),)
    return tuple(float(channel) for channel in color)


def colors_match(
    color: Any, target: Sequence[float], tolerance: float = 1e-3
) -> bool:
    normalized = normalize_color(color)
    target_tuple = tuple(float(channel) for channel in target)
    if normalized is None or len(normalized) != len(target_tuple):
        return False
    return all(
        abs(channel - expected) <= tolerance
        for channel, expected in zip(normalized, target_tuple)
    )


def dash_signature(
    dash: Any,
) -> Tuple[Tuple[float, ...], float]:
    """Return ``(dash_array, phase)`` from a pdfplumber ``dash`` attribute.

    Solid lines carry an empty array and are returned as such rather than
    treated as a dash style.
    """

    if not dash:
        return ((), 0.0)
    array, phase = dash
    return tuple(float(value) for value in array), float(phase)


def dash_arrays_match(
    left: Sequence[float], right: Sequence[float], tolerance: float = 1e-3
) -> bool:
    """Compare dash arrays on rhythm, deliberately ignoring the phase.

    A path that resumes after a gap typically restarts the dash pattern at
    another phase; the on/off rhythm still identifies the series.
    """

    if len(left) != len(right):
        return False
    return all(
        abs(a_value - b_value) <= tolerance
        for a_value, b_value in zip(left, right)
    )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _frame_candidates(page: "Page") -> List[T_obj]:
    frames = [
        obj
        for obj in page.rects
        if obj.get("stroke") and obj.get("width", 0) > 50 and obj.get("height", 0) > 50
    ]
    return sorted(frames, key=lambda obj: obj["width"] * obj["height"], reverse=True)


def detect_frame(page: "Page") -> T_obj:
    """Pick the large stroked rectangle that encloses the plot area."""

    frames = _frame_candidates(page)
    if not frames:
        raise ValueError("no plot frame (large stroked rectangle) found")
    return frames[0]


def _is_numeric_word(word: T_obj) -> bool:
    text = word["text"].replace(",", "").replace("%", "")
    try:
        float(text)
        return True
    except ValueError:
        return False


def _word_value(word: T_obj) -> float:
    return float(word["text"].replace(",", "").replace("%", ""))


def detect_axis_ticks(
    page: "Page", frame: T_obj, axis: Literal["x", "y"]
) -> Tuple[AxisTick, ...]:
    """Pair numeric tick labels with the short tick marks on the frame.

    The frame geometry and tick marks give exact coordinates; the nearby
    text words give the data values.  Labels are matched to marks by
    proximity rather than assumed spacing.
    """

    words = [word for word in page.extract_words() if _is_numeric_word(word)]
    ticks: List[AxisTick] = []

    if axis == "y":
        labels = [word for word in words if word["x1"] <= frame["x0"] + 1]
        for word in labels:
            mid_y = (word["top"] + word["bottom"]) / 2
            marks = [
                line
                for line in page.lines
                # y-axis tick marks are short horizontal lines on the frame.
                if abs(line["top"] - line["bottom"]) <= 0.5
                and max(line["x0"], line["x1"]) <= frame["x0"] + 1
                and abs(((line["top"] + line["bottom"]) / 2) - mid_y) < 6
            ]
            if not marks:
                continue
            mark = min(
                marks,
                key=lambda line: abs(
                    ((line["top"] + line["bottom"]) / 2) - mid_y
                ),
            )
            # Top-down coordinate: larger values sit lower on the page, so
            # the linear scale naturally gets a negative slope.
            coord = (mark["top"] + mark["bottom"]) / 2
            ticks.append(AxisTick(_word_value(word), coord))
    else:
        labels = [word for word in words if word["top"] >= frame["bottom"] - 1]
        for word in labels:
            mid_x = (word["x0"] + word["x1"]) / 2
            marks = [
                line
                for line in page.lines
                # x-axis tick marks are short vertical lines on the frame.
                if abs(line["x0"] - line["x1"]) <= 0.5
                and min(line["top"], line["bottom"]) >= frame["bottom"] - 1
                and abs(((line["x0"] + line["x1"]) / 2) - mid_x) < 8
            ]
            if not marks:
                continue
            mark = min(
                marks,
                key=lambda line: abs(((line["x0"] + line["x1"]) / 2) - mid_x),
            )
            coord = (mark["x0"] + mark["x1"]) / 2
            ticks.append(AxisTick(_word_value(word), coord))

    if len(ticks) < 2:
        raise ValueError(f"fewer than two {axis}-axis ticks detected")
    return tuple(sorted(ticks, key=lambda tick: tick.value))


# ---------------------------------------------------------------------------
# Legend parsing
# ---------------------------------------------------------------------------


def _object_ref(page: "Page", obj: T_obj) -> str:
    object_type = obj["object_type"]
    siblings = page.objects.get(object_type, [])
    try:
        index = siblings.index(obj)
    except ValueError:  # pragma: no cover - objects always come from the page
        index = -1
    return f"page{page.page_number}:{object_type}[{index}]"


def parse_legend(
    page: "Page",
    frame: T_obj,
    target_color: Sequence[float],
    color_tolerance: float,
    legend_bbox: Optional[Tuple[float, float, float, float]] = None,
) -> List[LegendEntry]:
    """Associate target-color sample lines above the frame with label text."""

    words = page.extract_words()
    entries: List[LegendEntry] = []
    for line in page.lines:
        if not colors_match(line.get("stroking_color"), target_color, color_tolerance):
            continue
        in_legend_region = (
            line["bottom"] < frame["top"]
            if legend_bbox is None
            else (
                legend_bbox[0] <= line["x0"]
                and line["x1"] <= legend_bbox[2]
                and legend_bbox[1] <= line["top"]
                and line["bottom"] <= legend_bbox[3]
            )
        )
        if not in_legend_region:
            continue
        dash_array, phase = dash_signature(line.get("dash"))
        if not dash_array:
            continue
        mid_top = (line["top"] + line["bottom"]) / 2
        candidates = [
            word
            for word in words
            if word["x0"] >= line["x1"] - 1
            and abs(((word["top"] + word["bottom"]) / 2) - mid_top) < 14
        ]
        if not candidates:
            continue
        label = min(candidates, key=lambda word: word["x0"] - line["x1"])
        entries.append(
            LegendEntry(
                name=label["text"],
                dash_array=dash_array,
                phase=phase,
                sample=line,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Path walking
# ---------------------------------------------------------------------------


def split_subpaths(obj: T_obj) -> List[List[T_point]]:
    """Split an object's original path into its independent subpaths.

    A new ``m`` (moveto) begins a subpath; gaps between subpaths are real
    breaks in the drawing and must never be stitched together.
    """

    subpaths: List[List[T_point]] = []
    current: List[T_point] = []
    for command in obj.get("path", []):
        operator = command[0]
        if operator == "m":
            if current:
                subpaths.append(current)
            current = [(float(command[1][0]), float(command[1][1]))]
        elif operator == "l":
            current.append((float(command[1][0]), float(command[1][1])))
        elif operator == "c":
            points = command[1]
            current.append((float(points[-2]), float(points[-1])))
        elif operator == "h":
            if current:
                current.append(current[0])
    if current:
        subpaths.append(current)
    if not subpaths and obj.get("pts"):
        subpaths = [[(float(x), float(y)) for x, y in obj["pts"]]]
    return subpaths


def _snap_x_value(
    x: float, x_scale: LinearScale, snap_tolerance: float
) -> Tuple[float, float]:
    """Snap a vertex onto the nearest x tick value.

    Returns the tick data value and its exact PDF coordinate.
    """

    nearest = min(x_scale.ticks, key=lambda tick: abs(tick.coord - x))
    if abs(nearest.coord - x) > snap_tolerance:
        raise ValueError(
            f"vertex x={x} is {abs(nearest.coord - x):.2f}pt from the nearest "
            f"tick ({nearest.coord}); refusing to guess its x value"
        )
    return nearest.value, nearest.coord


def _continuation_candidates(
    first_x: float,
    series_points: Dict[str, List["DataPoint"]],
    x_scale: LinearScale,
) -> Tuple[str, ...]:
    """Series whose last named point sits directly before an unnamed segment.

    Geometry can show which series *could* continue into a segment, but it is
    never used to assign a name: when more than one series qualifies, the
    segment stays ambiguous.  A series qualifies only if its rightmost named
    point is on the tick immediately preceding the segment's first tick, so
    distant or backward-running series are not offered as candidates.
    """

    tick_values = [tick.value for tick in x_scale.ticks]
    try:
        first_index = tick_values.index(first_x)
    except ValueError:
        return ()
    if first_index == 0:
        return ()
    preceding = tick_values[first_index - 1]
    candidates = [
        name
        for name, points in series_points.items()
        if points and max(point.x_value for point in points) == preceding
    ]
    return tuple(sorted(candidates))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def extract_chart(
    page: "Page",
    target_color: Sequence[float],
    *,
    frame: Optional[T_obj] = None,
    legend_bbox: Optional[Tuple[float, float, float, float]] = None,
    x_ticks: Optional[Sequence[AxisTick]] = None,
    y_ticks: Optional[Sequence[AxisTick]] = None,
    color_tolerance: float = 1e-3,
    snap_tolerance: float = 3.0,
) -> ChartExtraction:
    """Extract named data points from target-color dashed paths.

    Parameters
    ----------
    page:
        A ``pdfplumber.Page``.
    target_color:
        The stroking color shared by the indicators, as an RGB tuple.
    frame:
        Optional pre-identified plot-frame rectangle; detected when omitted.
    legend_bbox:
        Optional ``(x0, top, x1, bottom)`` box holding the legend; defaults to
        the region above the frame.
    x_ticks, y_ticks:
        Explicit tick mappings; detected from tick marks and labels when
        omitted.
    """

    resolved_frame = frame if frame is not None else detect_frame(page)
    legend = parse_legend(
        page, resolved_frame, target_color, color_tolerance, legend_bbox
    )
    if not legend:
        raise ValueError("no legend samples in the target color were found")

    resolved_x_ticks = (
        tuple(x_ticks)
        if x_ticks is not None
        else detect_axis_ticks(page, resolved_frame, "x")
    )
    resolved_y_ticks = (
        tuple(y_ticks)
        if y_ticks is not None
        else detect_axis_ticks(page, resolved_frame, "y")
    )
    x_scale = LinearScale.from_ticks("x", resolved_x_ticks)
    y_scale = LinearScale.from_ticks("y", resolved_y_ticks)

    # One dash array may legitimately map to several legend entries; only a
    # unique mapping allows a segment to be named.
    style_to_names: Dict[Tuple[float, ...], List[str]] = {}
    for entry in legend:
        style_to_names.setdefault(entry.dash_array, []).append(entry.name)

    series_points: Dict[str, List[DataPoint]] = {
        entry.name: [] for entry in legend
    }
    ambiguous_segments: List[AmbiguousSegment] = []
    sources: List[T_obj] = []

    plotted = [
        obj
        for obj in (page.curves + page.lines)
        if colors_match(obj.get("stroking_color"), target_color, color_tolerance)
        and obj["x0"] >= resolved_frame["x0"] - 1
        and obj["x1"] <= resolved_frame["x1"] + 1
        and obj["top"] >= resolved_frame["top"] - 1
        and obj["bottom"] <= resolved_frame["bottom"] + 1
    ]

    for obj in plotted:
        sources.append(obj)

    def classify(
        obj: T_obj,
    ) -> Tuple[Tuple[float, ...], float, bool, Optional[str], Optional[str]]:
        dash_array, phase = dash_signature(obj.get("dash"))
        matched_names = [
            name
            for style, names in style_to_names.items()
            if dash_arrays_match(style, dash_array, color_tolerance)
            for name in names
        ]
        unique_match = len(set(matched_names)) == 1
        series_name = matched_names[0] if unique_match else None
        if unique_match:
            reason = None
        elif not matched_names:
            reason = "dash style matches no legend sample"
        else:
            reason = (
                f"dash style is shared by {len(set(matched_names))} legend "
                "entries; continuation cannot be named"
            )
        return dash_array, phase, unique_match, series_name, reason

    # First pass: name everything the legend identifies uniquely.
    for obj in plotted:
        _dash_array, _phase, unique_match, series_name, _reason = classify(obj)
        if not unique_match:
            continue
        assert series_name is not None
        object_ref = _object_ref(page, obj)
        for subpath_index, subpath in enumerate(split_subpaths(obj)):
            last_index = len(subpath) - 1
            for point_index, (x, y) in enumerate(subpath):
                x_value, tick_x = _snap_x_value(x, x_scale, snap_tolerance)
                series_points[series_name].append(
                    DataPoint(
                        series=series_name,
                        x_value=x_value,
                        y_value=y_scale.to_data(y),
                        x=tick_x,
                        y=y,
                        connected_to_next=point_index < last_index,
                        status="named",
                        ambiguity_reason=None,
                        source=obj,
                        source_ref=object_ref,
                        subpath_index=subpath_index,
                        point_index=point_index,
                    )
                )

    # Second pass: unnamed segments stay ambiguous, but record which named
    # series geometrically could continue into each one.
    for obj in plotted:
        dash_array, phase, unique_match, _series_name, reason = classify(obj)
        if unique_match:
            continue
        object_ref = _object_ref(page, obj)
        for subpath_index, subpath in enumerate(split_subpaths(obj)):
            first_x_value, _ = _snap_x_value(
                subpath[0][0], x_scale, snap_tolerance
            )
            candidate_series = _continuation_candidates(
                first_x_value, series_points, x_scale
            )
            segment_points: List[DataPoint] = []
            last_index = len(subpath) - 1
            for point_index, (x, y) in enumerate(subpath):
                x_value, tick_x = _snap_x_value(x, x_scale, snap_tolerance)
                point = DataPoint(
                    series=None,
                    x_value=x_value,
                    y_value=y_scale.to_data(y),
                    x=tick_x,
                    y=y,
                    connected_to_next=point_index < last_index,
                    status="ambiguous",
                    ambiguity_reason=reason,
                    source=obj,
                    source_ref=object_ref,
                    subpath_index=subpath_index,
                    point_index=point_index,
                    candidate_series=candidate_series or None,
                )
                segment_points.append(point)
            ambiguous_segments.append(
                AmbiguousSegment(
                    dash_array=dash_array,
                    phase=phase,
                    reason=reason or "ambiguous",
                    points=segment_points,
                    source=obj,
                    source_ref=object_ref,
                    subpath_index=subpath_index,
                    candidate_series=candidate_series,
                )
            )

    gaps = _detect_gaps(series_points, x_scale)
    return ChartExtraction(
        legend=legend,
        x_scale=x_scale,
        y_scale=y_scale,
        series_points=series_points,
        ambiguous_segments=ambiguous_segments,
        gaps=gaps,
        frame=resolved_frame,
        sources=sources,
    )


def _detect_gaps(
    series_points: Dict[str, List[DataPoint]], x_scale: LinearScale
) -> Dict[str, List[float]]:
    """Report x tick values missing inside a series' observed span.

    Only ticks between the first and last recovered vertex count: a gap is
    an internal hole, not the region beyond where a series stopped. Nothing
    is interpolated.
    """

    tick_values = [tick.value for tick in x_scale.ticks]
    gaps: Dict[str, List[float]] = {}
    for name, points in series_points.items():
        observed = {point.x_value for point in points}
        if not observed:
            continue
        lower, upper = min(observed), max(observed)
        missing = [
            value
            for value in tick_values
            if lower < value < upper and value not in observed
        ]
        if missing:
            gaps[name] = missing
    return gaps
