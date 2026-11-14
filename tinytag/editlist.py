# SPDX-FileCopyrightText: 2014-2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""MP4 edit list expansion and program/media timeline lookups.

An MP4 ``elst`` atom turns source media into a program.  Its
``segment_duration`` is expressed in the movie timescale (``mvhd``), while
``media_time`` is expressed in the track timescale (``mdhd``).  This module
expands entries into records that keep both units, retains empty edits
(``media_time == -1``, inserted silence), and offers lookups in both
directions: program time to source media, and source media to program time.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from math import ceil
from os import PathLike, SEEK_END
from struct import error as StructError, unpack, unpack_from
from typing import (
    BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple, Union
)

#: Playback rates that can be mapped without guessing: normal playback and
#: a dwell (freeze frame).  Other rates are preserved, and their media range
#: is computed from the rational value, but they are marked unsupported
#: instead of being silently treated as normal playback.
SUPPORTED_MEDIA_RATES = (0.0, 1.0)

_HANDLER_TYPES = {
    'soun': 'audio',
    'vide': 'video',
    'text': 'text',
    'sbtl': 'subtitle',
    'subt': 'subtitle',
    'clcp': 'caption',
    'meta': 'metadata',
    'hint': 'hint',
}

_Source = Union[bytes, bytearray, str, PathLike, BinaryIO]


def handler_media_type(handler_type: str) -> str:
    """Return a readable media type for an MP4 handler type."""
    return _HANDLER_TYPES.get(handler_type, 'other')


def parse_movie_header(data: bytes) -> Tuple[int, int]:
    """Return ``(timescale, duration)`` from an ``mvhd``/``mdhd`` payload."""
    if not data:
        return 0, 0
    version = data[0]
    if version == 0 and len(data) >= 20:
        return unpack_from('>II', data, 12)
    if version == 1 and len(data) >= 28:
        return unpack_from('>IQ', data, 20)
    return 0, 0


def parse_track_header(data: bytes) -> int:
    """Return the track ID from a ``tkhd`` payload."""
    if not data:
        return 0
    version = data[0]
    if version == 0 and len(data) >= 16:
        return unpack_from('>I', data, 12)[0]
    if version == 1 and len(data) >= 24:
        return unpack_from('>I', data, 20)[0]
    return 0


def parse_handler_type(data: bytes) -> str:
    """Return the four-character handler type from an ``hdlr`` payload."""
    if len(data) < 12:
        return ''
    return data[8:12].decode('latin-1', 'replace')


def _raw_entry(reason: str) -> dict:
    return {
        'segment_duration': 0,
        'media_time': 0,
        'media_rate_integer': 1,
        'media_rate_fraction': 0,
        'media_rate': 1.0,
        'unexplained': reason,
    }


def parse_elst_entries(data: bytes) -> List[dict]:
    """Parse an ``elst`` atom or payload into raw edit entries.

    ``segment_duration`` stays in movie ticks and ``media_time`` in track
    ticks.  A trailing entry carrying an ``unexplained`` reason is added for
    a truncated atom or an unknown version, so the defect survives expansion.
    """
    if len(data) >= 8 and data[4:8] == b'elst':
        data = data[8:]
    if len(data) < 8:
        return [_raw_entry('edit list atom is too short')]
    version = data[0]
    if version not in (0, 1):
        return [_raw_entry(f'unsupported edit list version {version}')]
    entry_count = unpack_from('>I', data, 4)[0]
    entry_size = 12 if version == 0 else 20
    entries: List[dict] = []
    offset = 8
    while len(entries) < entry_count and offset + entry_size <= len(data):
        if version == 0:
            duration, media_time = unpack_from('>Ii', data, offset)
            rate_integer, rate_fraction = unpack_from(
                '>hh', data, offset + 8)
        else:
            duration, media_time = unpack_from('>Qq', data, offset)
            rate_integer, rate_fraction = unpack_from(
                '>hh', data, offset + 16)
        entries.append({
            'segment_duration': duration,
            'media_time': media_time,
            'media_rate_integer': rate_integer,
            'media_rate_fraction': rate_fraction,
            'media_rate': rate_integer + rate_fraction / 65536.0,
        })
        offset += entry_size
    if len(entries) < entry_count:
        entries.append(_raw_entry(
            f'truncated edit list: expected {entry_count} entries, '
            f'found {len(entries)}'))
    return entries


class _SampleTable:
    """Map media-time ticks to sample indices through an ``stts`` atom."""

    def __init__(self, entries: Sequence[Tuple[int, int]]) -> None:
        self._runs: List[Tuple[int, int, int, int]] = []
        self._duration = 0
        self._sample_count = 0
        for count, delta in entries:
            if count <= 0 or delta <= 0:
                continue
            self._runs.append(
                (self._duration, self._sample_count, count, delta))
            self._duration += count * delta
            self._sample_count += count

    @property
    def duration(self) -> int:
        """Total duration in media ticks."""
        return self._duration

    @property
    def sample_count(self) -> int:
        """Number of samples described by the table."""
        return self._sample_count

    def _run(self, sample: int) -> Optional[Tuple[int, int, int, int]]:
        if sample < 0 or sample >= self._sample_count:
            return None
        low, high = 0, len(self._runs) - 1
        while low <= high:
            middle = (low + high) // 2
            run = self._runs[middle]
            if sample < run[1]:
                high = middle - 1
            elif sample >= run[1] + run[2]:
                low = middle + 1
            else:
                return run
        return None

    def sample_at(self, ticks: float) -> Optional[int]:
        """Return the sample containing a media tick, if any."""
        if not self._runs or ticks < 0 or ticks >= self._duration:
            return None
        low, high = 0, len(self._runs) - 1
        while low <= high:
            middle = (low + high) // 2
            tick, sample, count, delta = self._runs[middle]
            if ticks < tick:
                high = middle - 1
            elif ticks >= tick + count * delta:
                low = middle + 1
            else:
                return sample + min(int(ticks - tick) // delta, count - 1)
        return None

    def start_tick(self, sample: int) -> Optional[int]:
        """Return the first media tick of a sample."""
        run = self._run(sample)
        if run is None:
            return None
        tick, run_sample, _count, delta = run
        return tick + (sample - run_sample) * delta

    def end_tick(self, sample: int) -> Optional[int]:
        """Return the last media tick of a sample (inclusive)."""
        run = self._run(sample)
        start = self.start_tick(sample)
        if run is None or start is None:
            return None
        return start + run[3] - 1


@dataclass
class EditRecord:
    """One expanded edit list entry.

    Presentation fields use the movie timeline; media fields use the source
    track timeline.  Seconds and raw timescale ticks are both kept, because
    the two timelines generally use different units.
    """

    index: int = 0
    track_id: int = 0
    handler_type: str = ''
    media_type: str = 'other'
    movie_timescale: int = 0
    media_timescale: int = 0
    segment_duration: int = 0
    media_time: int = 0
    media_rate: float = 1.0
    media_rate_integer: int = 1
    media_rate_fraction: int = 0
    presentation_start_ticks: int = 0
    presentation_end_ticks: int = 0
    presentation_start: float = 0.0
    presentation_end: float = 0.0
    media_start: Optional[float] = None
    media_end: Optional[float] = None
    media_start_ticks: Optional[int] = None
    media_end_ticks: Optional[float] = None
    media_sample_start: Optional[int] = None
    media_sample_end: Optional[int] = None
    is_empty: bool = False
    rate_supported: bool = True
    supported: bool = True
    unexplained: bool = False
    reason: Optional[str] = None

    @property
    def empty(self) -> bool:
        """Whether this edit inserts silence without consuming media."""
        return self.is_empty

    @property
    def silent(self) -> bool:
        """Alias for :attr:`empty`."""
        return self.is_empty

    @property
    def duration(self) -> float:
        """Program duration in seconds."""
        return self.presentation_end - self.presentation_start

    @property
    def duration_ticks(self) -> int:
        """Program duration in movie ticks."""
        return self.presentation_end_ticks - self.presentation_start_ticks

    @property
    def media_duration(self) -> Optional[float]:
        """Source-media duration in seconds, or ``None`` for silence."""
        if self.media_start is None or self.media_end is None:
            return None
        return self.media_end - self.media_start

    @property
    def media_sample_count(self) -> Optional[int]:
        """Number of source samples played by this edit."""
        if self.media_sample_start is None or self.media_sample_end is None:
            return None
        return self.media_sample_end - self.media_sample_start + 1

    @property
    def presentation_range(self) -> Tuple[float, float]:
        """Program interval as ``(start, end)`` seconds."""
        return (self.presentation_start, self.presentation_end)

    @property
    def presentation_range_ticks(self) -> Tuple[int, int]:
        """Program interval as ``(start, end)`` movie ticks."""
        return (self.presentation_start_ticks, self.presentation_end_ticks)

    @property
    def media_range(self) -> Tuple[Optional[float], Optional[float]]:
        """Source interval as ``(start, end)`` seconds."""
        return (self.media_start, self.media_end)

    @property
    def media_range_ticks(self) -> Tuple[Optional[int], Optional[float]]:
        """Source interval as ``(start, end)`` track ticks."""
        return (self.media_start_ticks, self.media_end_ticks)

    @property
    def sample_range(self) -> Tuple[Optional[int], Optional[int]]:
        """Inclusive source sample interval."""
        return (self.media_sample_start, self.media_sample_end)

    @property
    def program_range(self) -> Tuple[float, float]:
        """Alias for :attr:`presentation_range`."""
        return self.presentation_range

    def contains(self, time: float) -> bool:
        """Whether a program time in seconds falls in this edit."""
        if self.movie_timescale <= 0:
            return False
        return self.contains_ticks(time * self.movie_timescale)

    def contains_ticks(self, ticks: float) -> bool:
        """Whether a program tick falls in this edit."""
        return (self.presentation_start_ticks
                <= ticks < self.presentation_end_ticks)

    def __contains__(self, time: float) -> bool:
        return self.contains(time)

    def as_dict(self) -> Dict[str, object]:
        """Return a flat, JSON-friendly record."""
        return {
            'index': self.index,
            'track_id': self.track_id,
            'handler_type': self.handler_type,
            'media_type': self.media_type,
            'movie_timescale': self.movie_timescale,
            'media_timescale': self.media_timescale,
            'segment_duration': self.segment_duration,
            'media_time': self.media_time,
            'presentation_start': self.presentation_start,
            'presentation_end': self.presentation_end,
            'presentation_start_ticks': self.presentation_start_ticks,
            'presentation_end_ticks': self.presentation_end_ticks,
            'media_start': self.media_start,
            'media_end': self.media_end,
            'media_start_ticks': self.media_start_ticks,
            'media_end_ticks': self.media_end_ticks,
            'media_sample_start': self.media_sample_start,
            'media_sample_end': self.media_sample_end,
            'media_sample_count': self.media_sample_count,
            'media_rate': self.media_rate,
            'media_rate_integer': self.media_rate_integer,
            'media_rate_fraction': self.media_rate_fraction,
            'empty': self.is_empty,
            'rate_supported': self.rate_supported,
            'supported': self.supported,
            'unexplained': self.unexplained,
            'reason': self.reason,
        }

    to_dict = as_dict

    def __getitem__(self, key: str) -> object:
        return self.as_dict()[key]

    def get(self, key: str, default: object = None) -> object:
        """Return a record field by name, like a mapping."""
        return self.as_dict().get(key, default)

    def keys(self) -> Tuple[str, ...]:
        """Return record field names."""
        return tuple(self.as_dict())

    def items(self) -> Tuple[Tuple[str, object], ...]:
        """Return record field name/value pairs."""
        return tuple(self.as_dict().items())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EditRecord):
            return self.as_dict() == other.as_dict()
        if isinstance(other, dict):
            return self.as_dict() == other
        return NotImplemented

    __hash__ = None


@dataclass
class EditTrack:
    """Edit list for one MP4 track."""

    track_id: int = 0
    handler_type: str = ''
    media_type: str = 'other'
    media_timescale: int = 0
    media_duration: int = 0
    movie_timescale: int = 0
    records: Tuple[EditRecord, ...] = ()
    warnings: Tuple[str, ...] = ()

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> EditRecord:
        return self.records[index]

    @property
    def timescale(self) -> int:
        """Track (media) timescale."""
        return self.media_timescale

    @property
    def duration(self) -> int:
        """Track duration in media ticks."""
        return self.media_duration

    @property
    def entries(self) -> List[EditRecord]:
        """Return the expanded records as a list."""
        return list(self.records)

    @property
    def supported(self) -> bool:
        """Whether every edit in this track is supported."""
        return all(record.supported for record in self.records)

    @property
    def empty_records(self) -> List[EditRecord]:
        """Return the empty edits in this track."""
        return [record for record in self.records if record.is_empty]

    @property
    def unexplained_records(self) -> List[EditRecord]:
        """Return edits that could not be interpreted safely."""
        return [record for record in self.records if record.unexplained]

    def records_at(self, time: float) -> List[EditRecord]:
        """Return edits covering a program time in seconds."""
        return [record for record in self.records if record.contains(time)]

    def records_for_media(self,
                          start: float,
                          end: Optional[float] = None
                          ) -> List[EditRecord]:
        """Return edits whose media interval overlaps a seconds range."""
        return _records_for_media(self.records, start, end)

    def as_dict(self) -> Dict[str, object]:
        """Return a JSON-friendly track description."""
        return {
            'track_id': self.track_id,
            'handler_type': self.handler_type,
            'media_type': self.media_type,
            'media_timescale': self.media_timescale,
            'media_duration': self.media_duration,
            'movie_timescale': self.movie_timescale,
            'records': [record.as_dict() for record in self.records],
            'warnings': list(self.warnings),
        }

    to_dict = as_dict


def _overlaps(record: EditRecord,
              start: float,
              end: Optional[float]) -> bool:
    if record.is_empty or record.media_start is None \
            or record.media_end is None:
        return False
    low, high = sorted((record.media_start, record.media_end))
    if high <= low:
        return False
    if end is None or end == start:
        return low <= start < high
    query_low, query_high = sorted((start, end))
    return low < query_high and high > query_low


def _records_for_media(records: Iterable[EditRecord],
                       start: float,
                       end: Optional[float] = None) -> List[EditRecord]:
    return [record for record in records if _overlaps(record, start, end)]


@dataclass
class EditList:
    """Expanded edit list and bidirectional program/media lookups.

    ``records`` belongs to the primary (usually audio) track.  ``tracks``
    keeps every track with an edit list, so a video track using a different
    timescale is not lost.
    """

    records: Tuple[EditRecord, ...] = ()
    tracks: Tuple[EditTrack, ...] = ()
    movie_timescale: int = 0
    warnings: Tuple[str, ...] = ()
    supported_media_rates: Tuple[float, ...] = SUPPORTED_MEDIA_RATES
    primary_track: Optional[EditTrack] = None

    @classmethod
    def from_bytes(cls, data: bytes) -> Optional['EditList']:
        """Parse MP4 bytes, returning ``None`` without an edit list."""
        return _read_edit_list_from(BytesIO(data))

    @classmethod
    def from_file(cls, source: _Source) -> Optional['EditList']:
        """Read an edit list from a path or file-like object."""
        return read_edit_list(source)

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> EditRecord:
        return self.records[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EditList):
            return (self.records == other.records
                    and self.movie_timescale == other.movie_timescale)
        if isinstance(other, (list, tuple)):
            return self.table == [
                value.as_dict() if isinstance(value, EditRecord) else value
                for value in other
            ]
        return NotImplemented

    __hash__ = None

    @property
    def primary(self) -> Optional[EditTrack]:
        """Return the primary (usually audio) track."""
        return self.primary_track

    @property
    def entries(self) -> List[EditRecord]:
        """Return the primary track's edit records."""
        return list(self.records)

    edits = entries

    @property
    def all_records(self) -> List[EditRecord]:
        """Return records from every track, not just the primary one."""
        records: List[EditRecord] = []
        for track in self.tracks:
            records.extend(track.records)
        return records

    @property
    def track_count(self) -> int:
        """Number of tracks that carry an edit list."""
        return len(self.tracks)

    @property
    def table(self) -> List[Dict[str, object]]:
        """Return the program/media table as flat dictionaries."""
        return [record.as_dict() for record in self.records]

    def to_table(self) -> List[Dict[str, object]]:
        """Alias for :attr:`table`."""
        return self.table

    as_table = to_table

    @property
    def empty_records(self) -> List[EditRecord]:
        """Return all empty (silence) edits in the primary track."""
        return [record for record in self.records if record.is_empty]

    silence = empty_records
    silence_records = empty_records

    @property
    def unsupported_records(self) -> List[EditRecord]:
        """Return primary-track edits that are not fully supported."""
        return [record for record in self.records if not record.supported]

    @property
    def unexplained_records(self) -> List[EditRecord]:
        """Return primary-track edits whose structure is unexplained."""
        return [record for record in self.records if record.unexplained]

    @property
    def supported(self) -> bool:
        """Whether every edit in the primary track is supported."""
        return all(record.supported for record in self.records)

    @property
    def duration(self) -> float:
        """Program duration in seconds, including silence."""
        if not self.records:
            return 0.0
        return (self.records[-1].presentation_end
                - self.records[0].presentation_start)

    @property
    def media_duration(self) -> float:
        """Program duration excluding empty edits, in seconds."""
        return sum(record.duration for record in self.records
                   if not record.is_empty)

    @property
    def silent_duration(self) -> float:
        """Total inserted silence in seconds."""
        return sum(record.duration for record in self.records
                   if record.is_empty)

    @property
    def coverage(self) -> Tuple[float, float]:
        """Overall program interval in seconds."""
        if not self.records:
            return (0.0, 0.0)
        return (self.records[0].presentation_start,
                self.records[-1].presentation_end)

    def validate(self) -> List[str]:
        """Return every warning for the primary track's edits."""
        warnings = list(self.warnings)
        if any(record.unexplained for record in self.records):
            warnings.append('edit list contains unexplained entries')
        return warnings

    def records_at(self, time: float) -> List[EditRecord]:
        """Return edits covering a program time in seconds."""
        return [record for record in self.records if record.contains(time)]

    presentation_at = records_at
    program_at = records_at
    find = records_at
    lookup = records_at
    query = records_at

    def records_at_ticks(self, ticks: float) -> List[EditRecord]:
        """Return edits covering a program tick."""
        return [record for record in self.records
                if record.contains_ticks(ticks)]

    def record_at(self, time: float) -> Optional[EditRecord]:
        """Return the first edit covering a program time."""
        records = self.records_at(time)
        return records[0] if records else None

    def records_for_presentation(self,
                                 start: float,
                                 end: Optional[float] = None
                                 ) -> List[EditRecord]:
        """Return edits overlapping a program range in seconds."""
        if end is None:
            return self.records_at(start)
        query_start, query_end = sorted((start, end))
        return [record for record in self.records
                if record.presentation_start < query_end
                and record.presentation_end > query_start]

    def silence_at(self, time: float) -> bool:
        """Whether a program time is covered only by empty edits."""
        covering = self.records_at(time)
        return bool(covering) and all(record.is_empty
                                     for record in covering)

    is_silent = silence_at

    def silence_in(self,
                   start: float,
                   end: Optional[float] = None) -> bool:
        """Whether a program range is entirely silence."""
        records = self.records_for_presentation(start, end)
        return bool(records) and all(record.is_empty for record in records)

    def media_position_ticks(self, time: float) -> Optional[float]:
        """Return the source media tick at a program time.

        ``None`` means silence, an empty edit, or a time outside every
        non-empty edit.
        """
        for record in self.records_at(time):
            if record.is_empty or record.movie_timescale <= 0:
                continue
            if record.media_start_ticks is None:
                continue
            offset = (time * record.movie_timescale
                      - record.presentation_start_ticks)
            return record.media_start_ticks + (
                offset * record.media_timescale
                / record.movie_timescale * record.media_rate)
        return None

    def media_position(self, time: float) -> Optional[float]:
        """Return the source media time in seconds at a program time."""
        ticks = self.media_position_ticks(time)
        if ticks is None:
            return None
        for record in self.records_at(time):
            if not record.is_empty and record.media_timescale > 0:
                return ticks / record.media_timescale
        return None

    def media_sample(self, time: float) -> Optional[int]:
        """Return the source sample index at a program time."""
        ticks = self.media_position_ticks(time)
        track = self.primary_track
        table = getattr(track, '_sample_table', None) if track else None
        if ticks is None or table is None:
            return None
        return table.sample_at(ticks)

    def records_for_media(self,
                          start: float,
                          end: Optional[float] = None
                          ) -> List[EditRecord]:
        """Return edits that play a source-media range in seconds."""
        return _records_for_media(self.records, start, end)

    media_range = records_for_media
    find_media = records_for_media
    lookup_media = records_for_media
    reverse_lookup = records_for_media
    query_media = records_for_media

    def records_for_media_ticks(self,
                                start: float,
                                end: Optional[float] = None
                                ) -> List[EditRecord]:
        """Return edits that play a source-media range in track ticks."""
        query_end = start if end is None else end
        matches = []
        for record in self.records:
            if record.media_start_ticks is None \
                    or record.media_end_ticks is None \
                    or record.media_end_ticks <= record.media_start_ticks:
                continue
            low = record.media_start_ticks
            high = record.media_end_ticks
            if query_end == start:
                hit = low <= start < high
            else:
                hit = low < query_end and high > start
            if hit:
                matches.append(record)
        return matches

    def records_for_media_samples(self,
                                  start: int,
                                  end: Optional[int] = None
                                  ) -> List[EditRecord]:
        """Return edits that play an inclusive source-sample range."""
        query_end = start if end is None else end
        matches = []
        for record in self.records:
            if record.media_sample_start is None \
                    or record.media_sample_end is None:
                continue
            low = record.media_sample_start
            high = record.media_sample_end
            if query_end == start:
                hit = low <= start <= high
            else:
                hit = low <= query_end and high >= start
            if hit:
                matches.append(record)
        return matches

    def presentations_for_media(self,
                                start: float,
                                end: Optional[float] = None
                                ) -> List[Tuple[float, float]]:
        """Return distinct program ranges that play a media range."""
        ranges: List[Tuple[float, float]] = []
        for record in self.records_for_media(start, end):
            if record.presentation_range not in ranges:
                ranges.append(record.presentation_range)
        return ranges

    matches_for_media = presentations_for_media
    find_presentations = presentations_for_media

    def media_to_presentation(self,
                              start: float,
                              end: Optional[float] = None
                              ) -> List[Tuple[float, float]]:
        """Return program ranges playing a source-media range."""
        return self.presentations_for_media(start, end)

    def record_for_index(self, index: int) -> Optional[EditRecord]:
        """Return the primary-track edit with a given index."""
        for record in self.records:
            if record.index == index:
                return record
        return None

    def track(self, track_id: int) -> Optional[EditTrack]:
        """Return the edit list of a specific track ID."""
        for track in self.tracks:
            if track.track_id == track_id:
                return track
        return None

    def media_index_at(self, time: float) -> Optional[int]:
        """Return the row index covering a program time."""
        record = self.record_at(time)
        return record.index if record is not None else None

    @property
    def media_ranges(self) -> List[Tuple[Optional[float], Optional[float]]]:
        """Return every non-empty source-media range."""
        return [record.media_range for record in self.records
                if not record.is_empty]

    @property
    def presentation_ranges(self) -> List[Tuple[float, float]]:
        """Return every non-empty program range."""
        return [record.presentation_range for record in self.records
                if not record.is_empty]

    def as_dict(self) -> Dict[str, object]:
        """Return a JSON-friendly edit list description."""
        return {
            'movie_timescale': self.movie_timescale,
            'supported_media_rates': list(self.supported_media_rates),
            'records': [record.as_dict() for record in self.records],
            'tracks': [track.as_dict() for track in self.tracks],
            'warnings': list(self.warnings),
        }

    to_dict = as_dict


def _entry_dict(entry) -> dict:
    """Accept raw dictionaries, parsed entries, or tuple-style edits."""
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, EditRecord):
        return {
            'segment_duration': entry.segment_duration,
            'media_time': entry.media_time,
            'media_rate_integer': entry.media_rate_integer,
            'media_rate_fraction': entry.media_rate_fraction,
            'media_rate': entry.media_rate,
        }
    duration, media_time, *rest = entry
    integer = rest[0] if rest else 1
    fraction = rest[1] if len(rest) > 1 else 0
    return {
        'segment_duration': duration,
        'media_time': media_time,
        'media_rate_integer': integer,
        'media_rate_fraction': fraction,
        'media_rate': integer + fraction / 65536.0,
    }


def build_edit_record(index: int,
                      entry,
                      movie_timescale: int,
                      media_timescale: int,
                      track_id: int = 0,
                      handler_type: str = '',
                      track_duration: int = 0,
                      presentation_start_ticks: int = 0,
                      sample_table: Optional[_SampleTable] = None
                      ) -> EditRecord:
    """Expand one raw edit entry into an :class:`EditRecord`."""
    entry = _entry_dict(entry)
    segment_duration = int(entry.get('segment_duration', 0))
    media_time = int(entry.get('media_time', 0))
    media_rate = float(entry.get('media_rate', 1.0))
    rate_integer = int(entry.get('media_rate_integer', 1))
    rate_fraction = int(entry.get('media_rate_fraction', 0))
    is_empty = media_time == -1
    rate_supported = media_rate in SUPPORTED_MEDIA_RATES

    structural: List[str] = []
    if entry.get('unexplained'):
        structural.append(str(entry['unexplained']))
    if movie_timescale <= 0:
        structural.append('movie timescale is missing')
    if segment_duration < 0:
        structural.append('negative segment duration')
    if not is_empty and media_time < 0:
        structural.append('negative media time')

    presentation_end_ticks = presentation_start_ticks + segment_duration
    if movie_timescale > 0:
        presentation_start = presentation_start_ticks / movie_timescale
        presentation_end = presentation_end_ticks / movie_timescale
    else:
        presentation_start = presentation_end = 0.0

    media_start = media_end = None
    media_start_ticks = media_end_ticks = None
    sample_start = sample_end = None

    if not is_empty and media_time >= 0:
        if media_timescale <= 0:
            structural.append('media timescale is missing')
        elif movie_timescale > 0:
            media_start_ticks = media_time
            if media_rate == 0.0:
                media_end_ticks = _dwell_end(media_time, sample_table)
            else:
                media_end_ticks = media_time + (
                    segment_duration * media_timescale
                    / movie_timescale * media_rate)
            media_start = media_start_ticks / media_timescale
            media_end = media_end_ticks / media_timescale
            if track_duration > 0 and media_time > track_duration:
                structural.append('media time is past the track duration')
            if (sample_table is not None and segment_duration > 0
                    and media_end_ticks > media_time):
                sample_start = sample_table.sample_at(media_time)
                sample_end = sample_table.sample_at(
                    _last_tick(media_end_ticks, track_duration, sample_table))
                if sample_start is None and media_time == 0:
                    structural.append('sample table does not cover media')

    reasons = []
    if not rate_supported:
        reasons.append(f'unsupported media rate {media_rate:g}')
    reasons.extend(structural)

    return EditRecord(
        index=index,
        track_id=track_id,
        handler_type=handler_type,
        media_type=handler_media_type(handler_type),
        movie_timescale=movie_timescale,
        media_timescale=media_timescale,
        segment_duration=segment_duration,
        media_time=media_time,
        media_rate=media_rate,
        media_rate_integer=rate_integer,
        media_rate_fraction=rate_fraction,
        presentation_start_ticks=presentation_start_ticks,
        presentation_end_ticks=presentation_end_ticks,
        presentation_start=presentation_start,
        presentation_end=presentation_end,
        media_start=media_start,
        media_end=media_end,
        media_start_ticks=media_start_ticks,
        media_end_ticks=media_end_ticks,
        media_sample_start=sample_start,
        media_sample_end=sample_end,
        is_empty=is_empty,
        rate_supported=rate_supported,
        supported=rate_supported and not structural,
        unexplained=bool(structural),
        reason='; '.join(reasons) if reasons else None,
    )


def _dwell_end(media_time: int,
               sample_table: Optional[_SampleTable]) -> float:
    """Return the media end tick for a freeze-frame (rate ``0.0``)."""
    if sample_table is not None:
        sample = sample_table.sample_at(media_time)
        if sample is not None:
            end = sample_table.end_tick(sample)
            if end is not None:
                return float(end + 1)
    return float(media_time + 1)


def _last_tick(media_end_ticks: float,
               track_duration: int,
               sample_table: _SampleTable) -> float:
    last = ceil(media_end_ticks) - 1
    if track_duration > 0:
        last = min(last, track_duration - 1)
    if sample_table.duration > 0:
        last = min(last, sample_table.duration - 1)
    return last


def build_edit_track(entries: Sequence[dict],
                     movie_timescale: int,
                     media_timescale: int,
                     track_id: int = 0,
                     handler_type: str = '',
                     media_duration: int = 0,
                     sample_table: Optional[_SampleTable] = None
                     ) -> EditTrack:
    """Expand a track's raw entries into an :class:`EditTrack`."""
    records: List[EditRecord] = []
    warnings: List[str] = []
    presentation_start_ticks = 0
    for index, entry in enumerate(entries):
        record = build_edit_record(
            index, entry, movie_timescale, media_timescale, track_id,
            handler_type, media_duration, presentation_start_ticks,
            sample_table)
        records.append(record)
        presentation_start_ticks = record.presentation_end_ticks
        if record.reason and record.reason not in warnings:
            warnings.append(record.reason)
    track = EditTrack(
        track_id=track_id,
        handler_type=handler_type,
        media_type=handler_media_type(handler_type),
        media_timescale=media_timescale,
        media_duration=media_duration,
        movie_timescale=movie_timescale,
        records=tuple(records),
        warnings=tuple(warnings),
    )
    if sample_table is not None:
        track._sample_table = sample_table  # type: ignore[attr-defined]
    return track


def build_edit_list(tracks: Sequence[EditTrack],
                    movie_timescale: int) -> EditList:
    """Select the primary track and wrap all tracks in an edit list."""
    if not tracks:
        return EditList(movie_timescale=movie_timescale)
    primary = next(
        (track for track in tracks
         if track.handler_type == 'soun' and track.records),
        next((track for track in tracks if track.records), tracks[0])
    )
    warnings: List[str] = []
    for track in tracks:
        warnings.extend(track.warnings)
    return EditList(
        records=primary.records,
        tracks=tuple(tracks),
        movie_timescale=movie_timescale,
        warnings=tuple(warnings),
        primary_track=primary,
    )


def parse_edit_list(data: bytes,
                    movie_timescale: int = 1000,
                    media_timescale: int = 1000,
                    track_id: int = 0,
                    handler_type: str = 'soun',
                    media_duration: int = 0) -> EditList:
    """Expand a standalone ``elst`` atom or payload into an ``EditList``."""
    track = build_edit_track(
        parse_elst_entries(data), movie_timescale, media_timescale,
        track_id, handler_type, media_duration)
    return build_edit_list([track], movie_timescale)


def parse_elst(data: bytes,
               movie_timescale: int = 1000,
               media_timescale: int = 1000,
               track_id: int = 0,
               handler_type: str = 'soun',
               media_duration: int = 0) -> EditList:
    """Alias for :func:`parse_edit_list`."""
    return parse_edit_list(
        data, movie_timescale, media_timescale, track_id, handler_type,
        media_duration)


def _iter_atoms(file_obj: BinaryIO,
                start: int,
                end: int) -> Iterable[Tuple[bytes, int, int]]:
    """Yield ``(type, payload_start, payload_end)`` for atoms in a range."""
    position = start
    while position + 8 <= end:
        file_obj.seek(position)
        header = file_obj.read(8)
        if len(header) < 8:
            return
        atom_size = unpack('>I', header[:4])[0]
        atom_type = header[4:8]
        header_size = 8
        if atom_size == 1:
            extended = file_obj.read(8)
            if len(extended) < 8:
                return
            atom_size = unpack('>Q', extended)[0]
            header_size = 16
        elif atom_size == 0:
            atom_size = end - position
        if atom_size < header_size or position + atom_size > end:
            return
        yield atom_type, position + header_size, position + atom_size
        position += atom_size


def _read_bytes(file_obj: BinaryIO, start: int, end: int) -> bytes:
    file_obj.seek(start)
    return file_obj.read(max(0, end - start))


def _children(file_obj: BinaryIO, start: int, end: int
              ) -> Dict[bytes, List[Tuple[int, int]]]:
    children: Dict[bytes, List[Tuple[int, int]]] = {}
    for atom_type, payload_start, payload_end in _iter_atoms(
            file_obj, start, end):
        children.setdefault(atom_type, []).append(
            (payload_start, payload_end))
    return children


def _parse_stts(data: bytes) -> Optional[_SampleTable]:
    if len(data) < 8:
        return None
    entry_count = unpack_from('>I', data, 4)[0]
    entries = []
    offset = 8
    for _index in range(entry_count):
        if offset + 8 > len(data):
            break
        entries.append(unpack_from('>II', data, offset))
        offset += 8
    return _SampleTable(entries) if entries else None


@dataclass
class _TrackScan:
    track_id: int = 0
    handler_type: str = ''
    media_timescale: int = 0
    media_duration: int = 0
    entries: Tuple[dict, ...] = ()
    has_edit_list: bool = False
    sample_table: Optional[_SampleTable] = None


def _scan_track(file_obj: BinaryIO, start: int, end: int) -> _TrackScan:
    scan = _TrackScan()
    children = _children(file_obj, start, end)
    if b'tkhd' in children:
        payload_start, payload_end = children[b'tkhd'][0]
        scan.track_id = parse_track_header(
            _read_bytes(file_obj, payload_start, payload_end))
    if b'edts' in children:
        for edts_start, edts_end in children[b'edts']:
            edts = _children(file_obj, edts_start, edts_end)
            if b'elst' in edts:
                payload_start, payload_end = edts[b'elst'][0]
                scan.entries = tuple(parse_elst_entries(
                    _read_bytes(file_obj, payload_start, payload_end)))
                scan.has_edit_list = True
                break
    if b'mdia' in children:
        for mdia_start, mdia_end in children[b'mdia']:
            mdia = _children(file_obj, mdia_start, mdia_end)
            if b'mdhd' in mdia:
                payload_start, payload_end = mdia[b'mdhd'][0]
                scan.media_timescale, scan.media_duration = \
                    parse_movie_header(
                        _read_bytes(file_obj, payload_start, payload_end))
            if b'hdlr' in mdia:
                payload_start, payload_end = mdia[b'hdlr'][0]
                scan.handler_type = parse_handler_type(
                    _read_bytes(file_obj, payload_start, payload_end))
            if b'minf' not in mdia or not scan.has_edit_list:
                continue
            for minf_start, minf_end in mdia[b'minf']:
                minf = _children(file_obj, minf_start, minf_end)
                if b'stbl' not in minf:
                    continue
                for stbl_start, stbl_end in minf[b'stbl']:
                    stbl = _children(file_obj, stbl_start, stbl_end)
                    if b'stts' in stbl:
                        payload_start, payload_end = stbl[b'stts'][0]
                        scan.sample_table = _parse_stts(
                            _read_bytes(file_obj, payload_start, payload_end))
    return scan


def _read_edit_list(file_obj: BinaryIO,
                    file_size: int) -> Optional[EditList]:
    top = _children(file_obj, 0, file_size)
    if b'moov' not in top:
        return None
    movie_timescale = 0
    scans: List[_TrackScan] = []
    for moov_start, moov_end in top[b'moov']:
        children = _children(file_obj, moov_start, moov_end)
        if b'mvhd' in children and not movie_timescale:
            payload_start, payload_end = children[b'mvhd'][0]
            movie_timescale, _duration = parse_movie_header(
                _read_bytes(file_obj, payload_start, payload_end))
        for trak_start, trak_end in children.get(b'trak', []):
            scans.append(_scan_track(file_obj, trak_start, trak_end))
    tracks = [
        build_edit_track(
            scan.entries, movie_timescale, scan.media_timescale,
            scan.track_id, scan.handler_type, scan.media_duration,
            scan.sample_table)
        for scan in scans if scan.has_edit_list
    ]
    return build_edit_list(tracks, movie_timescale) if tracks else None


def _read_edit_list_from(file_obj: BinaryIO) -> Optional[EditList]:
    try:
        file_obj.seek(0, SEEK_END)
        file_size = file_obj.tell()
        file_obj.seek(0)
        return _read_edit_list(file_obj, file_size)
    except (OSError, ValueError, IndexError, StructError):
        return None


def read_edit_list(source: _Source) -> Optional[EditList]:
    """Read an edit list from an MP4 path, bytes, or file-like object.

    Returns ``None`` when the source has no ``elst`` atom or cannot be
    scanned.  A path is opened for reading; an open file object is left open.
    """
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        if b'ftyp' in data[:64] or b'moov' in data[:4096]:
            return _read_edit_list_from(BytesIO(data))
        return parse_edit_list(data)
    if isinstance(source, (str, PathLike)):
        try:
            with open(source, 'rb') as file_obj:
                return _read_edit_list_from(file_obj)
        except OSError:
            return None
    return _read_edit_list_from(source)


def expand_edit_list(source: _Source,
                     movie_timescale: int = 1000,
                     media_timescale: int = 1000,
                     track_id: int = 0,
                     handler_type: str = 'soun',
                     media_duration: int = 0) -> Optional[EditList]:
    """Expand an MP4 source or raw ``elst`` data into an edit list."""
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        is_mp4 = b'ftyp' in data[:64] or b'moov' in data[:4096]
        is_elst = data[4:8] == b'elst' or (
            len(data) >= 8 and data[0] in (0, 1)
            and unpack_from('>I', data, 4)[0] < 100000)
        if not is_mp4 and not is_elst:
            return None
        if not is_mp4:
            return parse_edit_list(
                data, movie_timescale, media_timescale, track_id,
                handler_type, media_duration)
    return read_edit_list(source)


def edit_list_table(source: _Source) -> List[Dict[str, object]]:
    """Return a flat edit list table for a path, bytes, or file object."""
    edit_list = expand_edit_list(source)
    return edit_list.table if edit_list is not None else []


#: Compatibility alias for an expanded edit list entry.
EditEntry = EditRecord
EditListEntry = EditRecord

__all__ = (
    'EditEntry', 'EditList', 'EditListEntry', 'EditRecord', 'EditTrack',
    'SUPPORTED_MEDIA_RATES', 'build_edit_list', 'build_edit_record',
    'build_edit_track', 'edit_list_table', 'expand_edit_list',
    'handler_media_type', 'parse_edit_list', 'parse_elst',
    'parse_elst_entries', 'parse_handler_type', 'parse_movie_header',
    'parse_track_header', 'read_edit_list',
)
