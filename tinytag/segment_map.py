# SPDX-FileCopyrightText: 2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""Map MPEG slice files back to the master samples they encode.

Two delivery styles are supported:

* ``SEGMENTED``: every slice is encoded on its own, so each file carries
  its own encoder delay at the head and its own tail padding.
* ``WHOLE_THEN_SPLIT``: the master is encoded once and the stream is cut
  on MPEG frame boundaries; only the first file owns the stream delay
  and only the last file owns the stream padding.

All source windows are half-open ``[start, end)`` intervals indexed in
master (original) PCM samples.  An endpoint of ``None`` means it cannot
be determined from the frame counts and fill information at hand.
Sample *values* (for example two identical edge samples) never count as
evidence of overlap: only the structural interval indices decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import List, Optional, Sequence, Tuple

# MPEG samples per frame, indexed like ``_MPEG._SAMPLES_PER_FRAME`` in
# tinytag.py: [mpeg_id][layer_id], layer id 0 is reserved.
SAMPLES_PER_FRAME_TABLE = (
    (0, 576, 1152, 384),   # MPEG Version 2.5
    (0, 0, 0, 0),          # reserved
    (0, 576, 1152, 384),   # MPEG Version 2
    (0, 1152, 1152, 384),  # MPEG Version 1
)

MPEG1_LAYER3_SAMPLES = 1152
MPEG2_LAYER3_SAMPLES = 576

# Default LAME gapless delay for MPEG-1 Layer III (576 MD3 samples plus
# 576 encoder samples). MPEG-2/2.5 encoders commonly delay 576.
DEFAULT_ENCODER_DELAY = MPEG1_LAYER3_SAMPLES


class EncodingMode:
    """Delivery styles a slice manifest can be annotated with."""

    SEGMENTED = 'segmented'
    WHOLE_THEN_SPLIT = 'whole-then-split'
    WAREHOUSE = 'warehouse-deduction'
    ALL = (SEGMENTED, WHOLE_THEN_SPLIT)


class Relation:
    """Structural relation between two half-open windows."""

    OVERLAP = 'overlap'
    ADJACENT = 'adjacent'
    GAP = 'gap'
    INDETERMINATE = 'indeterminate'


class WarehouseAnchor:
    """Where the warehouse puts a slice after stripping its fill."""

    CUMULATIVE = 'cumulative'
    FRAME_GRID = 'frame-grid'


@dataclass(frozen=True)
class SampleWindow:
    """Half-open master sample interval ``[start, end)``.

    ``None`` endpoints are kept explicit so callers can list which end
    cannot be determined instead of silently guessing.
    """

    start: Optional[int]
    end: Optional[int]

    def __post_init__(self) -> None:
        if self.start is not None and self.start < 0:
            raise ValueError('window start must not be negative')
        if (self.start is not None and self.end is not None
                and self.end < self.start):
            raise ValueError('window end precedes start')

    @property
    def is_fully_known(self) -> bool:
        return self.start is not None and self.end is not None

    @property
    def sample_count(self) -> Optional[int]:
        if not self.is_fully_known:
            return None
        return self.end - self.start  # type: ignore[operator]

    def indeterminable_ends(self) -> Tuple[str, ...]:
        return tuple(
            end_name for end_name in ('start', 'end')
            if getattr(self, end_name) is None
        )

    def relation_to(self, other: 'SampleWindow') -> str:
        """Classify two windows from indices alone.

        Edge sample values are deliberately not consulted: equal values
        at a shared boundary are a coincidence, not proof of overlap.
        """
        if not (self.is_fully_known and other.is_fully_known):
            return Relation.INDETERMINATE
        if (self.start < other.end
                and other.start < self.end):  # type: ignore[operator]
            return Relation.OVERLAP
        if self.end == other.start or other.end == self.start:
            return Relation.ADJACENT
        return Relation.GAP


@dataclass(frozen=True)
class SliceFile:
    """One row of the slice source manifest.

    The manifest only states frame counts and the fill the warehouse is
    told to deduct. ``enc_delay`` / ``end_padding`` of ``None`` mean the
    corresponding fill is not recorded. The optional edge values carry
    sample content for the adjacency demonstration and never influence
    the structural mapping.
    """

    name: str
    frame_count: int
    enc_delay: Optional[int] = None
    end_padding: Optional[int] = None
    samples_per_frame: int = MPEG1_LAYER3_SAMPLES
    head_edge_value: Optional[int] = None
    tail_edge_value: Optional[int] = None

    def __post_init__(self) -> None:
        if self.frame_count <= 0:
            raise ValueError('frame_count must be positive')
        if self.samples_per_frame <= 0:
            raise ValueError('samples_per_frame must be positive')
        for fill_name in ('enc_delay', 'end_padding'):
            fill = getattr(self, fill_name)
            if fill is not None and fill < 0:
                raise ValueError(f'{fill_name} must not be negative')
        if (self.enc_delay is not None
                and self.end_padding is not None
                and self.enc_delay + self.end_padding
                > self.pcm_sample_count):
            raise ValueError('fill exceeds decoded PCM sample count')

    @property
    def pcm_sample_count(self) -> int:
        """Samples a decoder emits for this file (frames * spf)."""
        return self.frame_count * self.samples_per_frame


@dataclass(frozen=True)
class SegmentMapping:
    """Back-reference of one slice file to master sample coordinates."""

    name: str
    encoding_mode: str
    frame_count: int
    pcm_sample_count: int
    true_sample_count: Optional[int]
    source_window: SampleWindow
    indeterminable_ends: Tuple[Tuple[str, str], ...] = field(
        default_factory=tuple)


@dataclass(frozen=True)
class CoverageReport:
    """Result of checking mapped windows against the master extent."""

    master_window: SampleWindow
    windows: Tuple[SampleWindow, ...]
    gaps: Tuple[SampleWindow, ...]
    overlaps: Tuple[SampleWindow, ...]
    has_indeterminate_ends: bool

    @property
    def complete(self) -> bool:
        return (not self.has_indeterminate_ends and not self.gaps
                and not self.overlaps and self.master_window.is_fully_known)


def required_frame_count(
    true_sample_count: int,
    encoder_delay: int,
    samples_per_frame: int = MPEG1_LAYER3_SAMPLES,
) -> Tuple[int, int]:
    """Return ``(frame_count, end_padding)`` for an independent encode.

    The encoder prepends ``encoder_delay`` fill samples and pads the
    tail until every output frame holds ``samples_per_frame`` samples.
    """
    if true_sample_count < 0:
        raise ValueError('true_sample_count must not be negative')
    if encoder_delay < 0:
        raise ValueError('encoder_delay must not be negative')
    frames = max(1, -(-(true_sample_count + encoder_delay)
                      // samples_per_frame))
    end_padding = (
        frames * samples_per_frame - true_sample_count - encoder_delay)
    return frames, end_padding


def _mapping(
    slice_file: SliceFile,
    mode: str,
    window: SampleWindow,
    true_count: Optional[int],
    reasons: Sequence[Tuple[str, str]],
) -> SegmentMapping:
    return SegmentMapping(
        name=slice_file.name,
        encoding_mode=mode,
        frame_count=slice_file.frame_count,
        pcm_sample_count=slice_file.pcm_sample_count,
        true_sample_count=true_count,
        source_window=window,
        indeterminable_ends=tuple(reasons),
    )


def _map_segmented(files: Sequence[SliceFile]) -> List[SegmentMapping]:
    """Independent encodes: true length per file, laid out back to back."""
    mappings: List[SegmentMapping] = []
    offset = 0
    prefix_known = True
    for slice_file in files:
        pcm = slice_file.pcm_sample_count
        reasons: List[Tuple[str, str]] = []
        if (slice_file.enc_delay is None
                or slice_file.end_padding is None):
            true_count: Optional[int] = None
        else:
            true_count = (
                pcm - slice_file.enc_delay - slice_file.end_padding)
        if prefix_known:
            start: Optional[int] = offset
        else:
            start = None
            reasons.append((
                'start',
                'preceding segment length unknown; cumulative offset '
                'indeterminate',
            ))
        if true_count is None:
            end: Optional[int] = None
            reasons.append((
                'end',
                'encoder delay or end padding missing; segment length '
                'indeterminate',
            ))
        elif not prefix_known:
            end = None
            reasons.append((
                'end',
                'cumulative offset indeterminate due to preceding segment',
            ))
        else:
            end = offset + true_count
            offset = end
        if true_count is None or not prefix_known:
            prefix_known = False
        mappings.append(_mapping(
            slice_file, EncodingMode.SEGMENTED,
            SampleWindow(start, end), true_count, reasons))
    return mappings


def _stream_constants(
    files: Sequence[SliceFile],
) -> Tuple[int, int, Optional[int], Optional[int]]:
    samples_per_frame = files[0].samples_per_frame
    for slice_file in files[1:]:
        if slice_file.samples_per_frame != samples_per_frame:
            raise ValueError(
                'whole-stream slices must share one samples_per_frame')
    total_pcm = sum(
        slice_file.pcm_sample_count for slice_file in files)
    return (
        samples_per_frame,
        total_pcm,
        files[0].enc_delay,
        files[-1].end_padding,
    )


def _map_whole(files: Sequence[SliceFile]) -> List[SegmentMapping]:
    """One encode cut on frame boundaries, mapped through decoder coords."""
    _, total_pcm, stream_delay, stream_padding = _stream_constants(files)
    last_index = len(files) - 1
    mappings: List[SegmentMapping] = []
    decoded_cursor = 0
    for index, slice_file in enumerate(files):
        dec_start = decoded_cursor
        dec_end = dec_start + slice_file.pcm_sample_count
        reasons: List[Tuple[str, str]] = []
        if stream_delay is None:
            start: Optional[int] = None
            end: Optional[int] = None
            reasons.append((
                'start',
                'stream encoder delay missing; master origin '
                'indeterminate',
            ))
            reasons.append((
                'end',
                'stream encoder delay missing; master origin '
                'indeterminate',
            ))
        else:
            real_dec_start = max(dec_start, stream_delay)
            start = real_dec_start - stream_delay
            if index == last_index:
                if stream_padding is None:
                    end = None
                    reasons.append((
                        'end',
                        'stream end padding missing; tail extent '
                        'indeterminate',
                    ))
                else:
                    real_dec_end = min(
                        dec_end, total_pcm - stream_padding)
                    end = real_dec_end - stream_delay
            else:
                end = dec_end - stream_delay
        true_count = (
            None if start is None or end is None else end - start)
        mappings.append(_mapping(
            slice_file, EncodingMode.WHOLE_THEN_SPLIT,
            SampleWindow(start, end), true_count, reasons))
        decoded_cursor = dec_end
    return mappings


def map_slices(
    files: Sequence[SliceFile],
    encoding_mode: str,
) -> List[SegmentMapping]:
    """Map each slice to the master samples its decoded PCM really covers.

    This is the structural ground truth: for ``WHOLE_THEN_SPLIT`` the
    head fill of internal slices is ignored because those frames hold
    real master samples.
    """
    if encoding_mode == EncodingMode.SEGMENTED:
        return _map_segmented(files)
    if encoding_mode == EncodingMode.WHOLE_THEN_SPLIT:
        return _map_whole(files)
    raise ValueError(f'unknown encoding mode: {encoding_mode!r}')


def validate_whole_manifest(files: Sequence[SliceFile]) -> List[str]:
    """Report fill declarations that are illegal for a whole-stream cut."""
    issues: List[str] = []
    last_index = len(files) - 1
    for index, slice_file in enumerate(files):
        if index > 0 and slice_file.enc_delay:
            issues.append(
                f'{slice_file.name}: head fill {slice_file.enc_delay} '
                'declared on an internal slice; whole-stream internal '
                'frames contain master samples only')
        if index < last_index and slice_file.end_padding:
            issues.append(
                f'{slice_file.name}: tail fill '
                f'{slice_file.end_padding} declared before the final '
                'slice; only the stream tail is padded')
    return issues


def stamp_like_independent_files(
    files: Sequence[SliceFile],
    encoder_delay: int,
) -> List[SliceFile]:
    """Give every slice the fill record an independent encode gets.

    This mirrors the mistaken manifest in the story: internal slices
    of a whole-stream cut are stamped with a per-file encoder delay,
    so the warehouse deducts real master samples at every splice. The
    slices consist of whole frames, so independent-file accounting
    records zero tail fill on them; the genuine stream padding stays
    on the final slice.
    """
    if encoder_delay < 0:
        raise ValueError('encoder_delay must not be negative')
    last_index = len(files) - 1
    return [
        replace(
            slice_file,
            enc_delay=encoder_delay,
            end_padding=(
                slice_file.end_padding if index == last_index else 0),
        )
        for index, slice_file in enumerate(files)
    ]


def warehouse_retrieve(
    files: Sequence[SliceFile],
    stream_encoder_delay: Optional[int] = None,
    anchor: str = WarehouseAnchor.CUMULATIVE,
) -> List[SegmentMapping]:
    """Apply the warehouse rule mechanically, file by file.

    The rule is delivery-agnostic: for each file it strips the declared
    head and tail fill from the decoder output, then lays the retained
    samples back. It is exact for independent encodes. For a
    whole-stream cut whose internal slices carry bogus per-file delay
    stamps it removes genuine master samples, leaving a gap at every
    splice.

    ``anchor`` selects how retained windows get master coordinates:

    * ``CUMULATIVE``: windows are laid end to end, so the losses show
      up as cumulative drift and a shortfall at the tail.
    * ``FRAME_GRID``: each slice keeps the position its frame grid
      implies (requires the stream encoder delay), so the losses show
      up as explicit gaps at the splices.

    Endpoints that cannot be derived stay ``None`` and are reported in
    ``indeterminable_ends``.
    """
    if anchor not in (WarehouseAnchor.CUMULATIVE,
                      WarehouseAnchor.FRAME_GRID):
        raise ValueError(f'unknown warehouse anchor: {anchor!r}')
    mappings: List[SegmentMapping] = []
    offset = 0
    decoded_cursor = 0
    prefix_known = True
    for slice_file in files:
        reasons: List[Tuple[str, str]] = []
        if (slice_file.enc_delay is None
                or slice_file.end_padding is None):
            true_count: Optional[int] = None
        else:
            true_count = (
                slice_file.pcm_sample_count
                - slice_file.enc_delay - slice_file.end_padding)
        if anchor == WarehouseAnchor.FRAME_GRID:
            if stream_encoder_delay is None or true_count is None:
                start: Optional[int] = None
                end: Optional[int] = None
                reasons.append((
                    'start',
                    'stream delay or per-file fill missing; retained '
                    'window indeterminate',
                ))
                reasons.append((
                    'end',
                    'stream delay or per-file fill missing; retained '
                    'window indeterminate',
                ))
            else:
                start = (
                    decoded_cursor + slice_file.enc_delay
                    - stream_encoder_delay)
                end = (
                    decoded_cursor + slice_file.pcm_sample_count
                    - slice_file.end_padding - stream_encoder_delay)
            decoded_cursor += slice_file.pcm_sample_count
        else:
            if prefix_known:
                start = offset
            else:
                start = None
                reasons.append((
                    'start',
                    'preceding retained length unknown; cumulative '
                    'offset indeterminate',
                ))
            if true_count is None:
                end = None
                reasons.append((
                    'end',
                    'head or tail fill missing; retained length '
                    'indeterminate',
                ))
            elif not prefix_known:
                end = None
                reasons.append((
                    'end',
                    'cumulative offset indeterminate due to preceding '
                    'slice',
                ))
            else:
                end = offset + true_count
                offset = end
            if true_count is None or not prefix_known:
                prefix_known = False
        mappings.append(_mapping(
            slice_file, EncodingMode.WAREHOUSE,
            SampleWindow(start, end), true_count, reasons))
    return mappings


def check_coverage(
    mappings: Sequence[SegmentMapping],
    master_true_samples: Optional[int] = None,
) -> CoverageReport:
    """Check mapped windows for full master coverage, gaps and overlaps.

    Windows with undeterminable endpoints are excluded from concrete
    gap/overlap math; their presence is signalled through
    ``has_indeterminate_ends`` so no interval is invented around them.
    """
    windows = tuple(mapping.source_window for mapping in mappings)
    known = [window for window in windows if window.is_fully_known]
    indeterminate = len(known) != len(windows)
    gaps: List[SampleWindow] = []
    overlaps: List[SampleWindow] = []
    master_end = master_true_samples
    if master_end is None and known and windows[-1].is_fully_known:
        master_end = known[-1].end
    if windows and windows[0].is_fully_known and windows[0].start > 0:
        gaps.append(SampleWindow(0, windows[0].start))
    for previous, current in zip(windows, windows[1:]):
        if not (previous.is_fully_known and current.is_fully_known):
            continue
        if current.start < previous.end:  # type: ignore[operator]
            overlap_end = min(current.end, previous.end)
            overlaps.append(SampleWindow(
                current.start, overlap_end))  # type: ignore[arg-type]
        elif current.start > previous.end:  # type: ignore[operator]
            gaps.append(
                SampleWindow(previous.end, current.start))
    if master_end is not None and windows and windows[-1].is_fully_known:
        tail_end = windows[-1].end
        if tail_end < master_end:  # type: ignore[operator]
            gaps.append(SampleWindow(tail_end, master_end))
        elif tail_end > master_end:
            overlaps.append(SampleWindow(master_end, tail_end))
    master_window = SampleWindow(
        0, master_end if master_end is not None else None)
    return CoverageReport(
        master_window=master_window,
        windows=windows,
        gaps=tuple(gaps),
        overlaps=tuple(overlaps),
        has_indeterminate_ends=indeterminate,
    )
