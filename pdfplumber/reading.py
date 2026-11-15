"""
Geometry-aware reading-order segmentation.

Many reports use a page layout whose top half is split into two columns,
whose bottom half is a single full-width column, and whose centered
subheadings span both columns.  The ordinary text-extraction routines in
`pdfplumber.utils.text` sort by page-global coordinates, which can place an
upper-right column *after* a lower full-width block; this module instead
treats each framed block (and each spanning band) as a local reading
boundary and only orders those boundaries relative to one another.

The main entry point is `get_reading_order`, which:

- detects the original frames/boxes on a page (or accepts user-supplied
  frame bounding boxes), distinguishing full-width spanning bands from
  side-by-side columns;
- falls back to line geometry when a page has no usable frames, building
  virtual frames from spanning text lines and column runs;
- assigns every character to the smallest enclosing frame and turns each
  frame into a `ReadingSegment` carrying its original bounding box and
  extracted text;
- orders the segments from geometric evidence alone — top-to-bottom
  across bands and left-to-right within a band — never by a page-global
  x sort;
- records `ReadingAmbiguity` positions whenever both relative orders are
  geometrically defensible, instead of guessing; and
- lets the user resolve those positions (or pin any other relation) with
  a small list of fixed precedence constraints.
"""

from dataclasses import dataclass, field
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple

from . import utils
from ._typing import T_bbox, T_num, T_obj, T_obj_list
from .utils.geometry import get_bbox_overlap
from .utils.text import DEFAULT_X_TOLERANCE, DEFAULT_Y_TOLERANCE

if TYPE_CHECKING:  # pragma: nocover
    from .page import Page

T_precedence = Sequence[Tuple[int, int]]


@dataclass
class ReadingAmbiguity:
    """
    A position between two consecutive segments whose relative order
    cannot be settled from geometric evidence.  `candidates` lists the
    segment-id orders that are both defensible.
    """

    index: int
    segment_a: int
    segment_b: int
    bbox_a: T_bbox
    bbox_b: T_bbox
    reason: str
    candidates: List[List[int]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "segment_a": self.segment_a,
            "segment_b": self.segment_b,
            "bbox_a": list(self.bbox_a),
            "bbox_b": list(self.bbox_b),
            "reason": self.reason,
            "candidates": [list(c) for c in self.candidates],
        }


@dataclass
class ReadingSegment:
    """
    A single local reading boundary: one full-width band or one column.

    `bbox` is the original frame (or virtual frame) bounding box and
    `text` is the text extracted from the characters inside it.
    """

    segment_id: int
    bbox: T_bbox
    layout: str
    frame_index: Optional[int]
    char_count: int
    chars: T_obj_list = field(repr=False)
    text: str = ""
    ambiguities: List[ReadingAmbiguity] = field(default_factory=list, repr=False)

    def to_dict(self, include_chars: bool = True) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "segment_id": self.segment_id,
            "bbox": list(self.bbox),
            "layout": self.layout,
            "frame_index": self.frame_index,
            "char_count": self.char_count,
            "text": self.text,
            "ambiguities": [a.to_dict() for a in self.ambiguities],
        }
        if include_chars:
            result["chars"] = self.chars
        return result


@dataclass
class ReadingOrder:
    """The ordered reading boundaries found on a page."""

    page: "Page"
    bbox: T_bbox
    frames: List[Dict[str, Any]]
    segments: List[ReadingSegment]
    ambiguities: List[ReadingAmbiguity]

    def to_dict(self, include_chars: bool = True) -> Dict[str, Any]:
        return {
            "page_number": self.page.page_number,
            "bbox": list(self.bbox),
            "frames": self.frames,
            "segments": [
                segment.to_dict(include_chars=include_chars)
                for segment in self.segments
            ],
            "ambiguities": [ambiguity.to_dict() for ambiguity in self.ambiguities],
        }

    def extract_text(self, separator: str = "\n\n", **kwargs: Any) -> str:
        """Extract each segment's text, in reading order, and join them."""
        return separator.join(
            utils.extract_text(segment.chars, **kwargs) for segment in self.segments
        )


def _dedupe_bboxes(bboxes: List[T_bbox]) -> List[T_bbox]:
    seen: Set[T_bbox] = set()
    unique: List[T_bbox] = []
    for bbox in bboxes:
        if bbox not in seen:
            seen.add(bbox)
            unique.append(bbox)
    return unique


def _contains(outer: T_bbox, inner: T_bbox) -> bool:
    overlap = get_bbox_overlap(outer, inner)
    return overlap is not None and overlap == inner and inner != outer


def _remove_container_frames(frames: List[T_bbox]) -> List[T_bbox]:
    """Drop frames that fully enclose another frame, e.g. an outer page border."""
    return [
        frame
        for frame in frames
        if not any(_contains(frame, other) for other in frames if other != frame)
    ]


def _bbox_area(bbox: T_bbox) -> T_num:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def _y_overlap(a: T_bbox, b: T_bbox) -> T_num:
    return min(a[3], b[3]) - max(a[1], b[1])


def _point_in_bbox(
    point: Tuple[T_num, T_num], bbox: T_bbox, tolerance: T_num = 0
) -> bool:
    x, y = point
    return (
        bbox[0] - tolerance <= x <= bbox[2] + tolerance
        and bbox[1] - tolerance <= y <= bbox[3] + tolerance
    )


def _x_overlap(a: T_bbox, b: T_bbox) -> T_num:
    return min(a[2], b[2]) - max(a[0], b[0])


def _has_side_peer(index: int, frames: List[T_bbox], y_tolerance: T_num) -> bool:
    """True if another frame sits beside this one (overlapping y, disjoint x)."""
    frame = frames[index]
    narrowest_width = min(f[2] - f[0] for f in frames)
    for other_idx, other in enumerate(frames):
        if other_idx == index:
            continue
        if _y_overlap(frame, other) <= y_tolerance:
            continue
        if _x_overlap(frame, other) < 0.5 * narrowest_width:
            return True
    return False


def _detect_frame_bboxes(
    page: "Page",
    frame_min_width_ratio: float,
    frame_min_height_ratio: float,
) -> List[T_bbox]:
    min_width = page.width * frame_min_width_ratio
    min_height = page.height * frame_min_height_ratio
    bboxes = [
        (rect["x0"], rect["top"], rect["x1"], rect["bottom"])
        for rect in page.rects
        if rect["width"] >= min_width and rect["height"] >= min_height
    ]
    bboxes = _dedupe_bboxes(bboxes)
    return _remove_container_frames(bboxes)


def _line_words(line_chars: T_obj_list, x_tolerance: T_num) -> T_obj_list:
    return sorted(
        utils.extract_words(line_chars, x_tolerance=x_tolerance),
        key=itemgetter("x0"),
    )


def _detect_gutter_lanes(
    baselines: List[T_obj_list], x_tolerance: T_num
) -> List[Tuple[T_num, T_num]]:
    """
    Detect column gutters as vertical lanes that are blank on most
    baselines.  Random inter-word gaps in justified text move from line
    to line and so never form a persistent lane, whereas a real column
    gutter recurs at the same horizontal position.
    """
    line_gaps: List[List[Tuple[T_num, T_num]]] = []
    spans: List[Tuple[T_num, T_num]] = []
    for line_chars in baselines:
        words = _line_words(line_chars, x_tolerance)
        if len(words) < 2:
            continue
        gaps: List[Tuple[T_num, T_num]] = []
        previous = words[0]
        for word in words[1:]:
            if word["x0"] - previous["x1"] > x_tolerance:
                gaps.append((previous["x1"], word["x0"]))
            previous = word
        if gaps:
            line_gaps.append(gaps)
            spans.append((words[0]["x0"], words[-1]["x1"]))
    if not line_gaps:
        return []

    # Recurrence (below) is what distinguishes a column gutter from a
    # random wide word gap; the width floor only filters 1-2px noise.
    min_lane_width = x_tolerance * 3
    first_bin = int(min(span[0] for span in spans))
    last_bin = int(max(span[1] for span in spans)) + 1
    gap_bin_count = [0] * (last_bin - first_bin)
    for gaps in line_gaps:
        for left, right in gaps:
            start = max(0, int(left) - first_bin)
            end = min(len(gap_bin_count), int(right) - first_bin)
            for index in range(start, end):
                gap_bin_count[index] += 1

    # A real gutter is blank on at least half of the gap-bearing lines.
    # Require at least two such lines so a single wide gap cannot invent
    # a column.
    recurrence = max(2.0, -(-len(line_gaps) // 2))
    lanes: List[Tuple[T_num, T_num]] = []
    index = 0
    while index < len(gap_bin_count):
        if gap_bin_count[index] < recurrence:
            index += 1
            continue
        end = index
        while end < len(gap_bin_count) and gap_bin_count[end] >= recurrence:
            end += 1
        lane_left = float(first_bin + index)
        lane_right = float(first_bin + end)
        if lane_right - lane_left >= min_lane_width:
            lanes.append((lane_left, lane_right))
        index = end
    return lanes


def _split_line_at_lanes(
    words: T_obj_list,
    lanes: List[Tuple[T_num, T_num]],
    x_tolerance: T_num,
) -> List[T_obj_list]:
    """Split words into pieces wherever a gutter lane separates them."""
    if not words:
        return []
    pieces: List[T_obj_list] = [[words[0]]]
    previous = words[0]
    for word in words[1:]:
        separated = any(
            previous["x1"] <= lane_left + x_tolerance
            and word["x0"] >= lane_right - x_tolerance
            for lane_left, lane_right in lanes
        )
        if separated:
            pieces.append([word])
        else:
            pieces[-1].append(word)
        previous = word
    return pieces


def _virtual_frames_from_lines(
    chars: T_obj_list,
    x_tolerance: T_num,
    y_tolerance: T_num,
) -> List[T_bbox]:
    """
    Build frame-like bboxes from line geometry when no frames exist.

    Recurring blank vertical lanes define the column gutters; baselines
    that straddle a gutter split into columns. Lines above or below the
    column region, and in-region lines that straddle a gutter, become
    full-width spanning bands.
    """
    baselines = utils.cluster_objects(chars, itemgetter("top"), y_tolerance)
    if not baselines:
        return []

    lanes = _detect_gutter_lanes(baselines, x_tolerance)
    if not lanes:
        # No column evidence whatsoever: one full-width reading boundary.
        return [utils.objects_to_bbox(chars)]

    line_pieces: List[List[T_obj_list]] = []
    multi_column_lines: List[int] = []
    for index, line_chars in enumerate(baselines):
        pieces = _split_line_at_lanes(
            _line_words(line_chars, x_tolerance), lanes, x_tolerance
        )
        line_pieces.append(pieces)
        if len(pieces) > 1:
            multi_column_lines.append(index)

    # Derive column x-ranges from the recurring lane-split pieces.
    column_pieces: List[List[T_obj]] = []
    for index in multi_column_lines:
        pieces = line_pieces[index]
        while len(column_pieces) < len(pieces):
            column_pieces.append([])
        for column_index, piece in enumerate(pieces):
            column_pieces[column_index].extend(piece)
    column_ranges = sorted(
        (utils.objects_to_bbox(piece) for piece in column_pieces),
        key=lambda bbox: bbox[0],
    )
    region_top = min(
        word["top"]
        for index in multi_column_lines
        for piece in line_pieces[index]
        for word in piece
    )
    region_bottom = max(
        word["bottom"]
        for index in multi_column_lines
        for piece in line_pieces[index]
        for word in piece
    )

    def crosses_gutter(bbox: T_bbox) -> bool:
        return any(
            bbox[0] <= lane_left and bbox[2] >= lane_right
            for lane_left, lane_right in lanes
        )

    def column_for(bbox: T_bbox) -> int:
        center = (bbox[0] + bbox[2]) / 2

        def distance_to_column(idx: int) -> T_num:
            column_center = (column_ranges[idx][0] + column_ranges[idx][2]) / 2
            return abs(center - column_center)

        return min(range(len(column_ranges)), key=distance_to_column)

    def union(bboxes: List[T_bbox]) -> T_bbox:
        return utils.merge_bboxes(bboxes)

    median_line_height = sorted(
        (utils.objects_to_bbox(b)[3] - utils.objects_to_bbox(b)[1])
        for b in baselines
    )[len(baselines) // 2]
    band_gap_limit = max(y_tolerance * 4, median_line_height * 2)

    # Lines outside the column region, and in-region lines that cross a
    # gutter, are full-width bands. Everything else accumulates into one
    # track per column, even when a column is briefly empty.
    frames: List[T_bbox] = []
    column_runs: List[List[T_bbox]] = [[] for _ in column_ranges]
    band_run: List[T_bbox] = []

    def flush_band() -> None:
        if band_run:
            frames.append(union(band_run))
            band_run.clear()

    def flush_columns() -> None:
        for run in column_runs:
            if run:
                frames.append(union(run))
                run.clear()

    for index, pieces in enumerate(line_pieces):
        line_bbox = utils.objects_to_bbox(baselines[index])
        above_region = line_bbox[3] <= region_top - y_tolerance
        below_region = line_bbox[1] >= region_bottom + y_tolerance
        # A multi-piece baseline is a column line by definition even
        # though its full bbox crosses the gutter; only an alone line
        # that straddles a gutter is treated as a spanning band.
        alone = len(pieces) == 1
        is_band = above_region or below_region or (
            alone and crosses_gutter(line_bbox)
        )

        if is_band:
            flush_columns()
            if band_run and line_bbox[1] - band_run[-1][3] > band_gap_limit:
                flush_band()
            band_run.append(line_bbox)
        elif len(pieces) > 1:
            flush_band()
            for column_index, piece in enumerate(pieces):
                column_runs[column_index].append(utils.objects_to_bbox(piece))
        else:
            flush_band()
            column_runs[column_for(line_bbox)].append(line_bbox)
    flush_columns()
    flush_band()
    return _remove_container_frames(_dedupe_bboxes(frames))


def _classify_frames(
    frames: List[T_bbox], y_tolerance: T_num
) -> Tuple[List[T_bbox], List[T_bbox]]:
    """
    Split frames into full-width spanning bands and side-by-side columns.

    A frame is a column only when another frame sits beside it; every
    other frame is a spanning band that interrupts the column flow.
    """
    spanning: List[T_bbox] = []
    columns: List[T_bbox] = []
    for index, frame in enumerate(frames):
        if _has_side_peer(index, frames, y_tolerance):
            columns.append(frame)
        else:
            spanning.append(frame)
    spanning.sort(key=lambda b: (b[1], b[0]))
    columns.sort(key=lambda b: (b[1], b[0]))
    return spanning, columns


def _frame_order(frames: List[T_bbox], y_tolerance: T_num) -> List[int]:
    """
    Order frame indices into reading bands: top-to-bottom between bands
    and left-to-right inside each band.  Never uses a page-global x sort,
    so an upper-right column always stays ahead of a lower full band.
    """
    remaining = set(range(len(frames)))
    ordered: List[int] = []
    by_top = sorted(range(len(frames)), key=lambda i: (frames[i][1], frames[i][0]))
    for idx in by_top:
        if idx not in remaining:
            continue
        anchor = frames[idx]
        anchor_height = anchor[3] - anchor[1]
        band = [
            j
            for j in remaining
            if _y_overlap(anchor, frames[j])
            >= min(anchor_height, frames[j][3] - frames[j][1]) * 0.5
        ]
        band.sort(key=lambda j: (frames[j][0], frames[j][1]))
        ordered.extend(band)
        remaining.difference_update(band)
    return ordered


def _assign_chars(
    chars: T_obj_list, frames: List[T_bbox], x_tolerance: T_num
) -> List[T_obj_list]:
    buckets: List[T_obj_list] = [[] for _ in frames]
    if not frames:
        return buckets
    areas = [_bbox_area(frame) for frame in frames]
    for char in chars:
        center = ((char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2)
        containing = [
            idx
            for idx, frame in enumerate(frames)
            if _point_in_bbox(center, frame, x_tolerance)
        ]
        if containing:
            target = min(containing, key=lambda idx: areas[idx])
        else:
            # Fall back to the greatest bbox overlap, then nearest center.
            def distance_score(idx: int) -> Tuple[T_num, T_num]:
                frame = frames[idx]
                overlap = get_bbox_overlap(
                    (char["x0"], char["top"], char["x1"], char["bottom"]), frame
                )
                overlap_area = _bbox_area(overlap) if overlap is not None else 0
                frame_center = ((frame[0] + frame[2]) / 2, (frame[1] + frame[3]) / 2)
                distance = (center[0] - frame_center[0]) ** 2 + (
                    center[1] - frame_center[1]
                ) ** 2
                return (-overlap_area, distance)

            target = min(range(len(frames)), key=distance_score)
        buckets[target].append(char)
    return buckets


def _relation(a: T_bbox, b: T_bbox, y_tolerance: T_num) -> Tuple[str, str]:
    """
    Classify the geometric relation between two frame bboxes.

    Returns `("certain", reason)` when top-to-bottom or left-to-right
    ordering is the only defensible reading, and `("ambiguous", reason)`
    when both orders remain geometrically possible.  The test is
    symmetric in its two arguments.
    """
    y_overlap = _y_overlap(a, b)
    x_overlap = _x_overlap(a, b)
    min_height = min(a[3] - a[1], b[3] - b[1])

    # Vertically disjoint bands: the upper frame is read first.
    if y_overlap <= y_tolerance:
        return (
            "certain",
            "vertically separated bands; the upper frame precedes the lower",
        )
    # Horizontally disjoint and substantially aligned: side-by-side.
    if x_overlap <= 0 and y_overlap >= 0.5 * min_height:
        return (
            "certain",
            "side-by-side within one band; the left frame precedes the right",
        )
    # Horizontally disjoint but barely aligned: stacking or columns?
    if x_overlap <= 0:
        return (
            "ambiguous",
            "the frames are horizontally disjoint but only slightly overlap"
            " vertically, so stacked and side-by-side placement are both possible",
        )
    # Overlapping in both dimensions: neither rule settles the order.
    return (
        "ambiguous",
        "the frames overlap both horizontally and vertically, so neither the"
        " top-to-bottom nor the left-to-right rule settles the order",
    )


def _find_ambiguities(
    segments: List[ReadingSegment], y_tolerance: T_num
) -> List[ReadingAmbiguity]:
    ambiguities: List[ReadingAmbiguity] = []
    for position, (first, second) in enumerate(
        zip(segments, segments[1:]), start=1
    ):
        certainty, reason = _relation(first.bbox, second.bbox, y_tolerance)
        if certainty != "ambiguous":
            continue
        ambiguity = ReadingAmbiguity(
            index=position,
            segment_a=first.segment_id,
            segment_b=second.segment_id,
            bbox_a=first.bbox,
            bbox_b=second.bbox,
            reason=reason,
            candidates=[
                [first.segment_id, second.segment_id],
                [second.segment_id, first.segment_id],
            ],
        )
        ambiguities.append(ambiguity)
        first.ambiguities.append(ambiguity)
        second.ambiguities.append(ambiguity)
    return ambiguities


def _apply_precedence(
    default_order: List[int], precedence: T_precedence
) -> List[int]:
    """
    Reorder the geometrically-ordered segment ids to satisfy the fixed
    `(earlier, later)` constraints while keeping every other relation in
    its geometric default position.
    """
    ids = set(default_order)
    predecessors: Dict[int, Set[int]] = {segment_id: set() for segment_id in ids}
    for earlier, later in precedence:
        if earlier not in ids or later not in ids:
            raise ValueError(
                f"Precedence constraint references unknown segment id: "
                f"({earlier}, {later})"
            )
        if earlier == later:
            raise ValueError(f"Segment {earlier} cannot precede itself")
        predecessors[later].add(earlier)

    rank = {segment_id: idx for idx, segment_id in enumerate(default_order)}
    emitted: Set[int] = set()
    ordered: List[int] = []
    remaining = set(ids)
    while remaining:
        available = [
            segment_id
            for segment_id in remaining
            if predecessors[segment_id] <= emitted
        ]
        if not available:
            raise ValueError("Precedence constraints contain a cycle")
        chosen = min(available, key=lambda segment_id: rank[segment_id])
        ordered.append(chosen)
        emitted.add(chosen)
        remaining.remove(chosen)
    return ordered


def get_reading_order(
    page: "Page",
    frames: Optional[Sequence[T_bbox]] = None,
    frame_min_width_ratio: float = 0.1,
    frame_min_height_ratio: float = 0.02,
    x_tolerance: T_num = DEFAULT_X_TOLERANCE,
    y_tolerance: T_num = DEFAULT_Y_TOLERANCE,
    precedence: T_precedence = (),
    **text_settings: Any,
) -> ReadingOrder:
    """
    Segment a page into reading boundaries and order them.

    Args:
        page: The `pdfplumber.Page` to analyze.
        frames: Optional explicit frame bounding boxes
            `(x0, top, x1, bottom)`.  When omitted, frames are detected
            from the page's rectangles, falling back to text-line
            geometry.
        frame_min_width_ratio: Minimum frame width, as a fraction of the
            page width, for a rectangle to count as a frame.
        frame_min_height_ratio: Minimum frame height, as a fraction of
            the page height.  Kept low so thin spanning bands survive.
        x_tolerance / y_tolerance: Character clustering tolerances, also
            used when judging geometric relations.
        precedence: Optional fixed ordering constraints as
            `(earlier_segment_id, later_segment_id)` pairs.  Segment ids
            follow the geometric default order and remain stable when
            the constraints are applied.
        **text_settings: Keyword arguments forwarded to
            `pdfplumber.utils.extract_text` for each segment.
    """
    page_bbox: T_bbox = (page.bbox[0], page.bbox[1], page.bbox[2], page.bbox[3])
    chars = list(page.chars)

    if frames is None:
        detected = _detect_frame_bboxes(
            page, frame_min_width_ratio, frame_min_height_ratio
        )
        frame_bboxes = detected or _virtual_frames_from_lines(
            chars,
            x_tolerance,
            y_tolerance,
        )
        frame_source = "detected" if detected else "virtual"
    else:
        frame_bboxes = _remove_container_frames(_dedupe_bboxes(list(frames)))
        frame_source = "explicit"

    spanning_frames, _column_frames = _classify_frames(frame_bboxes, y_tolerance)
    spanning_set = set(spanning_frames)

    frame_records: List[Dict[str, Any]] = [
        {
            "frame_index": idx,
            "bbox": list(frame),
            "source": frame_source,
            "layout": "full_width" if frame in spanning_set else "column",
        }
        for idx, frame in enumerate(frame_bboxes)
    ]

    if not frame_bboxes:
        # No frames at all: treat the whole page as one reading boundary.
        frame_bboxes = [page_bbox]
        frame_records = [
            {
                "frame_index": None,
                "bbox": list(page_bbox),
                "source": "page",
                "layout": "full_width",
            }
        ]
        spanning_set = {page_bbox}

    buckets = _assign_chars(chars, frame_bboxes, x_tolerance)
    ordered_indices = _frame_order(frame_bboxes, y_tolerance)

    segments: List[ReadingSegment] = []
    for segment_id, frame_idx in enumerate(ordered_indices, start=1):
        frame = frame_bboxes[frame_idx]
        frame_chars = sorted(buckets[frame_idx], key=itemgetter("top", "x0"))
        text = utils.extract_text(frame_chars, **text_settings)
        segments.append(
            ReadingSegment(
                segment_id=segment_id,
                bbox=frame,
                layout="full_width" if frame in spanning_set else "column",
                frame_index=frame_idx if frame_source != "page" else None,
                char_count=len(frame_chars),
                chars=frame_chars,
                text=text,
            )
        )

    if precedence:
        constrained = _apply_precedence(
            [segment.segment_id for segment in segments], precedence
        )
        by_id = {segment.segment_id: segment for segment in segments}
        segments = [by_id[segment_id] for segment_id in constrained]

    # Ambiguity positions always refer to the final, possibly
    # precedence-adjusted ordering.
    ambiguities = _find_ambiguities(segments, y_tolerance)

    return ReadingOrder(
        page=page,
        bbox=page_bbox,
        frames=frame_records,
        segments=segments,
        ambiguities=ambiguities,
    )
