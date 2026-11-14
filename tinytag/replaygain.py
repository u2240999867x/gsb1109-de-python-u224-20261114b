# SPDX-FileCopyrightText: 2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""Album ReplayGain computation from Vorbis comment tags.

Reads per-track ReplayGain tags (as found in Vorbis comments and exposed
through ``TinyTag.other``) and combines them with a cataloger's reference
loudness to work out album-wide playback gain.

Gains are expressed in decibels, peaks as linear amplitudes. The report
distinguishes the values suggested by the tags from the values that can
actually be used without clipping, and names the track that imposes the
limit. Quiet tracks cannot compensate for a loud one: the usable album
gain is bounded by the single highest peak, never by an average.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import log10

TYPE_CHECKING = False

# Lazy imports for type checking
if TYPE_CHECKING:
    from typing import List, Optional, Sequence, Tuple

    from .tinytag import TinyTag

#: Reference loudness the ReplayGain spec normalizes to, in dB SPL.
STANDARD_REFERENCE_LOUDNESS_DB = 89.0

_GAIN_TAGS = ('replaygain_track_gain', 'replaygain_album_gain')
_PEAK_TAGS = ('replaygain_track_peak', 'replaygain_album_peak')


def parse_gain_db(text: str) -> float:
    """Parse a ReplayGain gain tag value such as ``'-3.95 dB'``."""
    value = text.strip()
    if value.lower().endswith('db'):
        value = value[:-2].strip()
    return float(value)


def parse_peak(text: str) -> float:
    """Parse a ReplayGain peak tag value such as ``'0.999969'``."""
    return float(text.strip())


@dataclass
class TrackTags:
    """ReplayGain tags of a single track.

    Gains in decibels, peaks as linear amplitudes. ``None`` means the
    corresponding tag is absent.
    """

    title: str
    track_gain_db: Optional[float] = None
    track_peak: Optional[float] = None
    album_gain_db: Optional[float] = None
    album_peak: Optional[float] = None


@dataclass
class TrackGainInfo:
    """Per-track outcome of an album gain computation."""

    title: str
    suggested_track_gain_db: Optional[float]
    usable_track_gain_db: Optional[float]
    peak: Optional[float]
    limits_album_gain: bool


@dataclass
class AlbumGainReport:
    """Album-wide ReplayGain outcome.

    ``suggested_album_gains_db`` holds every distinct album gain found in
    the tags (adjusted to the cataloger's reference loudness); if the
    tracks disagree, all conflicting values are kept and
    ``album_gains_agree`` is ``False``. ``usable_album_gains_db`` holds
    the same values limited by the loudest track's peak, or ``None`` when
    peaks are missing and no clip-free promise can be made.
    """

    reference_loudness_db: float
    suggested_album_gains_db: Tuple[float, ...]
    album_gains_agree: bool
    usable_album_gains_db: Optional[Tuple[float, ...]]
    peak_limited_gain_db: Optional[float]
    limiting_track: Optional[str]
    clip_free_guaranteed: bool
    tracks: Tuple[TrackGainInfo, ...] = field(default_factory=tuple)


def _first_value(tag: TinyTag, name: str) -> Optional[str]:
    values = tag.other.get(name)
    if not values:
        return None
    return values[0]


def track_tags_from_tinytag(tag: TinyTag, title: Optional[str] = None
                            ) -> TrackTags:
    """Build :class:`TrackTags` from a parsed :class:`TinyTag`."""
    track_gain = _first_value(tag, 'replaygain_track_gain')
    track_peak = _first_value(tag, 'replaygain_track_peak')
    album_gain = _first_value(tag, 'replaygain_album_gain')
    album_peak = _first_value(tag, 'replaygain_album_peak')
    return TrackTags(
        title=title if title is not None else (tag.title or '<untitled>'),
        track_gain_db=(
            parse_gain_db(track_gain) if track_gain is not None else None),
        track_peak=parse_peak(track_peak) if track_peak is not None else None,
        album_gain_db=(
            parse_gain_db(album_gain) if album_gain is not None else None),
        album_peak=parse_peak(album_peak) if album_peak is not None else None,
    )


def _headroom_db(peak: float) -> Optional[float]:
    """Gain headroom before clipping for a linear peak amplitude.

    Returns ``None`` for a silent track (no finite limit).
    """
    if peak <= 0.0:
        return None
    return -20.0 * log10(peak)


def _limit_gain(gain: Optional[float], peak: Optional[float]
                ) -> Optional[float]:
    """Clamp a suggested gain so the given peak does not exceed 1.0."""
    if gain is None or peak is None:
        return None
    headroom = _headroom_db(peak)
    if headroom is None:
        return gain
    return min(gain, headroom)


def compute_album_gain(tracks: Sequence[TrackTags],
                       reference_loudness_db: float =
                       STANDARD_REFERENCE_LOUDNESS_DB) -> AlbumGainReport:
    """Compute album ReplayGain for a sequence of tagged tracks.

    ``reference_loudness_db`` is the cataloger's target loudness; tag
    values (which assume :data:`STANDARD_REFERENCE_LOUDNESS_DB`) are
    shifted by the difference. The usable album gain is bounded by the
    loudest single peak across all tracks. If any track lacks a peak,
    suggestions are still listed but no clip-free guarantee is made.
    """
    shift_db = reference_loudness_db - STANDARD_REFERENCE_LOUDNESS_DB

    track_infos: List[TrackGainInfo] = []
    loudest_title: Optional[str] = None
    loudest_peak: Optional[float] = None
    all_peaks_known = True
    for track in tracks:
        if track.track_peak is None:
            all_peaks_known = False
        elif loudest_peak is None or track.track_peak > loudest_peak:
            loudest_peak = track.track_peak
            loudest_title = track.title
        suggested_track_gain = (
            track.track_gain_db + shift_db
            if track.track_gain_db is not None else None)
        track_infos.append(TrackGainInfo(
            title=track.title,
            suggested_track_gain_db=suggested_track_gain,
            usable_track_gain_db=_limit_gain(
                suggested_track_gain, track.track_peak),
            peak=track.track_peak,
            limits_album_gain=False,
        ))

    peak_limited_gain: Optional[float] = None
    limiting_track: Optional[str] = None
    if loudest_peak is not None:
        headroom = _headroom_db(loudest_peak)
        if headroom is not None:
            peak_limited_gain = headroom
            limiting_track = loudest_title

    suggested_album_gains: List[float] = []
    for track in tracks:
        if track.album_gain_db is None:
            continue
        gain = track.album_gain_db + shift_db
        if gain not in suggested_album_gains:
            suggested_album_gains.append(gain)

    if all_peaks_known:
        usable_album_gains: Optional[Tuple[float, ...]] = tuple(
            gain if peak_limited_gain is None
            else min(gain, peak_limited_gain)
            for gain in suggested_album_gains)
    else:
        usable_album_gains = None

    if limiting_track is not None:
        for info in track_infos:
            if info.title == limiting_track and info.peak == loudest_peak:
                info.limits_album_gain = True

    return AlbumGainReport(
        reference_loudness_db=reference_loudness_db,
        suggested_album_gains_db=tuple(suggested_album_gains),
        album_gains_agree=len(suggested_album_gains) <= 1,
        usable_album_gains_db=usable_album_gains,
        peak_limited_gain_db=peak_limited_gain,
        limiting_track=limiting_track,
        clip_free_guaranteed=all_peaks_known,
        tracks=tuple(track_infos),
    )


def format_report(report: AlbumGainReport) -> str:
    """Render an :class:`AlbumGainReport` as human-readable text."""
    lines = [
        'reference loudness: {:.1f} dB'.format(report.reference_loudness_db),
    ]
    if report.suggested_album_gains_db:
        suggested = ', '.join(
            '{:+.2f} dB'.format(gain)
            for gain in report.suggested_album_gains_db)
        lines.append('suggested album gain: ' + suggested)
        if not report.album_gains_agree:
            lines.append('warning: tracks disagree on the album gain')
    else:
        lines.append('suggested album gain: <no album gain tags>')
    if report.usable_album_gains_db is not None:
        usable = ', '.join(
            '{:+.2f} dB'.format(gain)
            for gain in report.usable_album_gains_db)
        lines.append('usable album gain: ' + usable)
    else:
        lines.append(
            'usable album gain: unknown '
            '(peak tags missing, clipping cannot be ruled out)')
    if report.limiting_track is not None:
        lines.append(
            'limited by track: {} ({:+.2f} dB headroom)'.format(
                report.limiting_track,
                report.peak_limited_gain_db or 0.0))
    lines.append(
        'clip-free guarantee: '
        + ('yes' if report.clip_free_guaranteed else 'no'))
    return '\n'.join(lines)
