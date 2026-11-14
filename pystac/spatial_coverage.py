from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

#: Axis names indexed by dimension position: longitude, latitude, height, ...
AXIS_NAMES: tuple[str, ...] = ("x", "y", "z")

#: Valid longitude/latitude limits.
LON_MIN = -180.0
LON_MAX = 180.0

#: Default tolerance applied when comparing axis bounds.
DEFAULT_SEAM_TOLERANCE = 1e-9

#: Longitudes overshooting the seam by more than this many degrees are left
#: untouched so genuinely invalid values are still reported.
MAX_SEAM_OVERSHOOT = 1.0

#: Default number of piece results kept in the small sample.
DEFAULT_SAMPLE_SIZE = 10

Bbox = Sequence[float | int]


@dataclass(frozen=True)
class AxisViolation:
    """A single axis on which a piece bbox falls outside the overall bbox.

    Attributes:
        axis : Name of the axis (``"x"``, ``"y"``, ``"z"`` ...).
        bound : Which bound was exceeded. ``"min"`` means the piece extends
            below the overall minimum, ``"max"`` means it extends above the
            overall maximum, and ``"extra"`` means the overall bbox does not
            define this axis at all (e.g. a redundant height axis).
        piece_value : The offending value taken from the piece bbox.
        overall_value : The corresponding overall bbox bound, or ``None`` when
            the axis is not defined on the overall bbox.
        message : Human readable description of the violation.
    """

    axis: str
    bound: str
    piece_value: float
    overall_value: Optional[float]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "bound": self.bound,
            "piece_value": self.piece_value,
            "overall_value": self.overall_value,
            "message": self.message,
        }


@dataclass
class BboxOverlap:
    """Coverage check outcome for a single piece bbox.

    A result only describes bounding-box containment. It never claims that the
    area inside the overall bbox is fully observed; empty space inside the
    outer bbox is not treated as data.

    Attributes:
        index : Position of the piece bbox in the input list.
        bbox : The piece bbox that was checked. Longitudes sitting just past
            the +/-180 seam are wrapped into the valid longitude range.
        dimensions : Number of dimensions of the piece bbox (2 or 3).
        covered : Whether every axis of the piece is contained in the overall
            bbox. This only tests bounding-box containment; empty space inside
            the overall bbox is never treated as observed data.
        crosses_antimeridian : Whether the piece bbox itself wraps the
            antimeridian (its minimum longitude is greater than its maximum
            longitude).
        seam_joined : Whether this piece connects with another piece across
            the +/-180 longitude seam. This is descriptive metadata and does
            not change ``covered``.
        seam_partner : Index of the piece this one joins across the seam, if
            any.
        violations : Axis-level violations, empty when the piece is covered.
        missing_axes : Axes defined by the overall bbox but not by this piece;
            a missing axis is not a violation because the piece makes no claim
            about it.
    """

    index: int
    bbox: list[float]
    dimensions: int
    covered: bool
    crosses_antimeridian: bool
    seam_joined: bool = False
    seam_partner: Optional[int] = None
    violations: list[AxisViolation] = field(default_factory=list)
    missing_axes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "bbox": list(self.bbox),
            "dimensions": self.dimensions,
            "covered": self.covered,
            "crosses_antimeridian": self.crosses_antimeridian,
            "seam_joined": self.seam_joined,
            "seam_partner": self.seam_partner,
            "violations": [v.to_dict() for v in self.violations],
            "missing_axes": list(self.missing_axes),
        }


@dataclass
class CoverageCheckResult:
    """Aggregate spatial coverage check for a collection's bboxes.

    Attributes:
        overall_bbox : The first bbox, treated as the overall outer bbox.
        total_pieces : Number of detailed piece bboxes that were checked.
        covered_pieces : Number of pieces contained in the overall bbox.
        not_covered_pieces : Number of pieces with at least one violation.
        antimeridian_pieces : Number of pieces detected as crossing the
            antimeridian.
        extra_height_axis_pieces : Number of pieces carrying a height axis that
            the overall bbox does not define.
        all_covered : Whether every piece is covered.
        sample_size : Maximum number of entries retained in ``samples``.
        seam_tolerance : Tolerance used for bound and seam comparisons.
        overlaps : Full per-piece results in input order.
        samples : Small, deterministic subset of ``overlaps`` for warehousing.
    """

    overall_bbox: list[float]
    total_pieces: int
    covered_pieces: int
    not_covered_pieces: int
    antimeridian_pieces: int
    extra_height_axis_pieces: int
    all_covered: bool
    sample_size: int
    seam_tolerance: float
    overlaps: list[BboxOverlap]
    samples: list[BboxOverlap]

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_bbox": list(self.overall_bbox),
            "total_pieces": self.total_pieces,
            "covered_pieces": self.covered_pieces,
            "not_covered_pieces": self.not_covered_pieces,
            "antimeridian_pieces": self.antimeridian_pieces,
            "extra_height_axis_pieces": self.extra_height_axis_pieces,
            "all_covered": self.all_covered,
            "sample_size": self.sample_size,
            "seam_tolerance": self.seam_tolerance,
            "overlaps": [o.to_dict() for o in self.overlaps],
            "samples": [o.to_dict() for o in self.samples],
        }


def _axis_name(axis_index: int) -> str:
    if axis_index < len(AXIS_NAMES):
        return AXIS_NAMES[axis_index]
    return f"axis{axis_index}"


def _validate_bbox(bbox: Bbox, label: str) -> list[float]:
    if not isinstance(bbox, Sequence) or isinstance(bbox, (str, bytes)):
        raise TypeError(f"{label} must be a sequence of numbers")
    values = list(bbox)
    if len(values) < 4 or len(values) % 2 != 0:
        raise ValueError(
            f"{label} must have an even number of values >= 4 "
            f"(2 values per dimension), got {len(values)}"
        )
    for value in values:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"{label} must contain only numbers, got {value!r}")
    return [float(value) for value in values]


def _normalize_longitude(value: float) -> float:
    """Wrap a longitude just past +/-180 back into the valid range.

    Only small overshoots of the antimeridian are wrapped. Values further
    away are returned unchanged so they are reported as out-of-bounds.
    """
    if LON_MAX < value <= LON_MAX + MAX_SEAM_OVERSHOOT:
        return value - 360.0
    if LON_MIN - MAX_SEAM_OVERSHOOT <= value < LON_MIN:
        return value + 360.0
    return value


def _crosses_antimeridian(bbox: Sequence[float]) -> bool:
    # Bboxes store all minimum values first, then the maximum values, so the
    # longitude maximum sits at index n (n = number of dimensions).
    return bbox[0] > bbox[len(bbox) // 2]


def _violation(
    axis: str,
    bound: str,
    piece_value: float,
    overall_value: Optional[float],
) -> AxisViolation:
    message = (
        f"piece {axis} {bound} bound {piece_value} falls outside overall "
        f"bound {overall_value}"
    )
    return AxisViolation(
        axis=axis,
        bound=bound,
        piece_value=piece_value,
        overall_value=overall_value,
        message=message,
    )


def _spans_full_globe(x_min: float, x_max: float, tolerance: float) -> bool:
    return x_min <= LON_MIN + tolerance and x_max >= LON_MAX - tolerance


def _check_longitude(
    piece: Sequence[float],
    overall: Sequence[float],
    tolerance: float,
    piece_crosses: bool,
    overall_crosses: bool,
) -> list[AxisViolation]:
    piece_min, piece_max = piece[0], piece[len(piece) // 2]
    overall_min, overall_max = overall[0], overall[len(overall) // 2]
    violations: list[AxisViolation] = []

    if overall_crosses:
        if piece_crosses:
            # Both wrap the seam, so compare their eastern and western
            # branches independently.
            if piece_min < overall_min - tolerance:
                violations.append(_violation("x", "min", piece_min, overall_min))
            if piece_max > overall_max + tolerance:
                violations.append(_violation("x", "max", piece_max, overall_max))
        else:
            # A non-wrapping piece is covered when it sits on either branch.
            on_east_branch = piece_min >= overall_min - tolerance
            on_west_branch = piece_max <= overall_max + tolerance
            if not on_east_branch and not on_west_branch:
                # The piece falls into the uncovered gap between the two
                # branches, so both bounds are reported against their branch.
                violations.append(_violation("x", "min", piece_min, overall_min))
                violations.append(_violation("x", "max", piece_max, overall_max))
    elif piece_crosses:
        # A wrapping piece always fits in longitude when the overall bbox
        # spans the full globe without wrapping itself.
        if not _spans_full_globe(overall_min, overall_max, tolerance):
            violations.append(_violation("x", "min", piece_min, overall_min))
            violations.append(_violation("x", "max", piece_max, overall_max))
    else:
        if piece_min < overall_min - tolerance:
            violations.append(_violation("x", "min", piece_min, overall_min))
        if piece_max > overall_max + tolerance:
            violations.append(_violation("x", "max", piece_max, overall_max))

    return violations


def _check_plain_axis(
    axis_index: int,
    piece: Sequence[float],
    overall: Sequence[float],
    tolerance: float,
) -> list[AxisViolation]:
    axis = _axis_name(axis_index)
    # All minimum values precede all maximum values in a STAC bbox.
    piece_min = piece[axis_index]
    piece_max = piece[len(piece) // 2 + axis_index]
    overall_min = overall[axis_index]
    overall_max = overall[len(overall) // 2 + axis_index]
    violations: list[AxisViolation] = []
    if piece_min < overall_min - tolerance:
        violations.append(_violation(axis, "min", piece_min, overall_min))
    if piece_max > overall_max + tolerance:
        violations.append(_violation(axis, "max", piece_max, overall_max))
    return violations


def check_bbox_coverage(
    overall_bbox: Bbox,
    piece_bbox: Bbox,
    *,
    seam_tolerance: float = DEFAULT_SEAM_TOLERANCE,
    index: int = 0,
) -> BboxOverlap:
    """Check one piece bbox against the overall outer bbox.

    Longitude comparisons are antimeridian aware: bboxes whose minimum
    longitude is greater than their maximum longitude are treated as wrapping
    the +/-180 seam, and pieces on either branch are accepted accordingly.

    Args:
        overall_bbox : The overall outer bbox (2D or 3D).
        piece_bbox : The detailed piece bbox (2D or 3D).
        seam_tolerance : Tolerance for bound and seam comparisons.
        index : Position of the piece within the full piece list.

    Returns:
        BboxOverlap: Per-axis containment result for this piece.
    """
    overall = _validate_bbox(overall_bbox, "overall_bbox")
    piece_raw = _validate_bbox(piece_bbox, "piece_bbox")

    overall_dimensions = len(overall) // 2
    piece_dimensions = len(piece_raw) // 2

    # Longitudes stored just past the seam (e.g. 180.00001) are wrapped so a
    # split granule is recognized instead of being rejected outright.
    piece = list(piece_raw)
    piece[0] = _normalize_longitude(piece[0])
    piece[piece_dimensions] = _normalize_longitude(piece[piece_dimensions])

    piece_crosses = _crosses_antimeridian(piece)
    overall_crosses = _crosses_antimeridian(overall)

    violations: list[AxisViolation] = []
    shared_dimensions = min(overall_dimensions, piece_dimensions)
    for axis_index in range(shared_dimensions):
        if axis_index == 0:
            violations.extend(
                _check_longitude(
                    piece,
                    overall,
                    seam_tolerance,
                    piece_crosses,
                    overall_crosses,
                )
            )
        else:
            violations.extend(
                _check_plain_axis(axis_index, piece, overall, seam_tolerance)
            )

    # A height (or further) axis present on the piece but absent from the
    # overall bbox is a redundant axis, not silent coverage.
    for axis_index in range(shared_dimensions, piece_dimensions):
        axis = _axis_name(axis_index)
        axis_min = piece[axis_index]
        axis_max = piece[piece_dimensions + axis_index]
        violations.append(
            AxisViolation(
                axis=axis,
                bound="extra",
                piece_value=axis_min,
                overall_value=None,
                message=(
                    f"piece defines {axis} axis [{axis_min}, {axis_max}] "
                    "that is not part of the overall bbox"
                ),
            )
        )

    missing_axes = [
        _axis_name(axis_index)
        for axis_index in range(piece_dimensions, overall_dimensions)
    ]

    return BboxOverlap(
        index=index,
        bbox=piece,
        dimensions=piece_dimensions,
        covered=not violations,
        crosses_antimeridian=piece_crosses,
        violations=violations,
        missing_axes=missing_axes,
    )


def _x_max(overlap: BboxOverlap) -> float:
    return overlap.bbox[overlap.dimensions]


def _y_min(overlap: BboxOverlap) -> float:
    return overlap.bbox[1]


def _y_max(overlap: BboxOverlap) -> float:
    return overlap.bbox[overlap.dimensions + 1]


def _touches_east_seam(overlap: BboxOverlap, tolerance: float) -> bool:
    if overlap.crosses_antimeridian:
        return True
    return abs(_x_max(overlap) - LON_MAX) <= tolerance


def _touches_west_seam(overlap: BboxOverlap, tolerance: float) -> bool:
    if overlap.crosses_antimeridian:
        return True
    return abs(overlap.bbox[0] - LON_MIN) <= tolerance


def _latitude_ranges_overlap(
    first: BboxOverlap, second: BboxOverlap, tolerance: float
) -> bool:
    return min(_y_max(first), _y_max(second)) + tolerance >= max(
        _y_min(first), _y_min(second)
    )


def _join_seam_pieces(
    overlaps: Sequence[BboxOverlap],
    overall_bbox: Sequence[float],
    tolerance: float,
) -> None:
    """Annotate pairs of pieces connecting at the +/-180 seam.

    Only overall bboxes that wrap the seam or span the full globe can host a
    seam connection. A pair is annotated when one clean piece reaches the
    +180 seam, another reaches the -180 seam and their latitude ranges
    overlap. Pieces that already wrap the seam themselves are not paired.
    The annotation never clears a violation, so a y or z overflow is still
    reported for the offending piece.
    """
    overall_crosses = _crosses_antimeridian(overall_bbox)
    overall_x_max = overall_bbox[len(overall_bbox) // 2]
    if not overall_crosses and not _spans_full_globe(
        overall_bbox[0], overall_x_max, tolerance
    ):
        return

    candidates = [
        overlap
        for overlap in overlaps
        if overlap.covered and not overlap.crosses_antimeridian
    ]
    east_pieces = [
        overlap for overlap in candidates if _touches_east_seam(overlap, tolerance)
    ]
    west_pieces = [
        overlap for overlap in candidates if _touches_west_seam(overlap, tolerance)
    ]

    paired: set[int] = set()
    for east in east_pieces:
        if east.index in paired:
            continue
        for west in west_pieces:
            if west.index in paired or not _latitude_ranges_overlap(
                east, west, tolerance
            ):
                continue
            east.seam_joined = True
            west.seam_joined = True
            east.seam_partner = west.index
            west.seam_partner = east.index
            paired.add(east.index)
            paired.add(west.index)
            break


def _select_samples(
    overlaps: Sequence[BboxOverlap], sample_size: int
) -> list[BboxOverlap]:
    if sample_size <= 0:
        return []
    if len(overlaps) <= sample_size:
        return list(overlaps)

    not_covered = [overlap for overlap in overlaps if not overlap.covered]
    covered = [overlap for overlap in overlaps if overlap.covered]
    samples = not_covered[:sample_size]
    remaining = sample_size - len(samples)
    samples.extend(covered[:remaining])
    return samples


def check_spatial_coverage(
    overall_bbox: Bbox,
    piece_bboxes: Sequence[Bbox],
    *,
    seam_tolerance: float = DEFAULT_SEAM_TOLERANCE,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> CoverageCheckResult:
    """Check detailed piece bboxes against one overall outer bbox.

    The first bbox of a collection's ``SpatialExtent`` is typically the overall
    outer bbox while the remaining bboxes describe detailed pieces. This
    function verifies that every piece stays within the overall bbox without
    treating empty space inside it as observed data.

    It recognizes three classes of problems:

    * pieces extending beyond the overall bbox on the x or y axis;
    * pieces crossing the antimeridian, including two pieces that connect
      across the +/-180 seam, which may pass together;
    * redundant height axes, including a piece whose 2D footprint is contained
      but whose z bounds exceed the overall 3D bbox.

    Args:
        overall_bbox : The overall outer bbox (2D or 3D).
        piece_bboxes : Detailed piece bboxes to check.
        seam_tolerance : Tolerance for bound and seam comparisons.
        sample_size : Maximum number of per-piece results kept in ``samples``.
            Not-covered pieces are retained first.

    Returns:
        CoverageCheckResult: Aggregate counts, full per-piece results and a
        small sample suitable for database ingestion.
    """
    overall = _validate_bbox(overall_bbox, "overall_bbox")
    if not isinstance(piece_bboxes, Sequence) or isinstance(piece_bboxes, (str, bytes)):
        raise TypeError("piece_bboxes must be a sequence of bboxes")

    overlaps = [
        check_bbox_coverage(
            overall,
            _validate_bbox(piece_bbox, f"piece_bboxes[{index}]"),
            seam_tolerance=seam_tolerance,
            index=index,
        )
        for index, piece_bbox in enumerate(piece_bboxes)
    ]

    _join_seam_pieces(overlaps, overall, seam_tolerance)

    covered_pieces = sum(1 for overlap in overlaps if overlap.covered)
    not_covered_pieces = len(overlaps) - covered_pieces
    antimeridian_pieces = sum(1 for overlap in overlaps if overlap.crosses_antimeridian)
    extra_height_axis_pieces = sum(
        1
        for overlap in overlaps
        if any(violation.bound == "extra" for violation in overlap.violations)
    )

    return CoverageCheckResult(
        overall_bbox=overall,
        total_pieces=len(overlaps),
        covered_pieces=covered_pieces,
        not_covered_pieces=not_covered_pieces,
        antimeridian_pieces=antimeridian_pieces,
        extra_height_axis_pieces=extra_height_axis_pieces,
        all_covered=not_covered_pieces == 0,
        sample_size=sample_size,
        seam_tolerance=seam_tolerance,
        overlaps=overlaps,
        samples=_select_samples(overlaps, sample_size),
    )
