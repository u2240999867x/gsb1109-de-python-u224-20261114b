# SPDX-FileCopyrightText: 2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""Broadcast Wave Format (BWF) recording ledger.

Reconstructs a continuous recording ledger from the bext metadata of
consecutive broadcast audio reels ("tape rolls").

Each reel carries a TimeReference measured in samples since local
midnight of its OriginationDate. Because the next reel may have been
recorded at a different sample rate, every point on the shared timeline
is represented as an exact :class:`fractions.Fraction` of a second.

Origination days are never inferred from a decreasing sample counter:
a smaller TimeReference usually means a re-recording within the same
day, not midnight rollover. Reels without an origination date can only
be placed by enumerating candidate day sequences inside an explicitly
given date window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from typing import Any, List, Optional, Sequence, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .tinytag import TinyTag

_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class Reel:
    """One broadcast audio reel and the raw data needed to place it."""

    filename: Optional[str]
    day: Optional[date]
    time_reference: int
    num_samples: int
    samplerate: int

    @classmethod
    def from_tag(cls,
                 tag: TinyTag,
                 day: Optional[date] = None) -> Reel:
        """Build a reel from a tinytag tag of a BWF wave file.

        ``day`` overrides the bext OriginationDate when the caller has
        reliable external collection metadata.
        """
        time_reference = tag.bwf_time_reference
        num_samples = tag.bwf_num_samples
        samplerate = tag.samplerate
        missing = [
            name for name, value in (
                ('bwf_time_reference', time_reference),
                ('bwf_num_samples', num_samples),
                ('samplerate', samplerate),
            ) if value is None
        ]
        if missing:
            raise ValueError(
                'Tag is missing BWF data required for a reel: '
                + ', '.join(missing))
        if samplerate <= 0 or num_samples < 0:
            raise ValueError('Reel sample rate must be positive')
        return cls(
            filename=tag.filename,
            day=day if day is not None else tag.bwf_origination_date,
            time_reference=time_reference,
            num_samples=num_samples,
            samplerate=samplerate,
        )


@dataclass(frozen=True)
class ReelEntry:
    """A reel placed on the continuous, second-based timeline."""

    reel: Reel
    day: date
    start: Fraction
    end: Fraction


@dataclass(frozen=True)
class ReelRelation:
    """Timeline relationship between two consecutive reels.

    ``delta`` is the next reel's start minus the previous reel's end,
    in exact seconds. A positive value is a gap, a negative value an
    overlap, zero means the reels are contiguous.
    """

    previous: ReelEntry
    current: ReelEntry
    delta: Fraction

    @property
    def kind(self) -> str:
        if self.delta > 0:
            return 'gap'
        if self.delta < 0:
            return 'overlap'
        return 'contiguous'

    @property
    def gap(self) -> Fraction:
        return self.delta if self.delta > 0 else Fraction(0)

    @property
    def overlap(self) -> Fraction:
        return -self.delta if self.delta < 0 else Fraction(0)


@dataclass(frozen=True)
class ReelLedger:
    """Reels ordered on a shared timeline plus their pairwise relations."""

    entries: List[ReelEntry] = field(default_factory=list)
    relations: List[ReelRelation] = field(default_factory=list)

    @property
    def reels(self) -> List[Reel]:
        return [entry.reel for entry in self.entries]


def _coerce_reel(reel: Any) -> Reel:
    if isinstance(reel, Reel):
        return reel
    return Reel.from_tag(reel)


def _require_day(day: Optional[date]) -> date:
    if day is None:
        raise ValueError('Reel is missing an origination date')
    return day


def _entry_for(reel: Reel, day: date) -> ReelEntry:
    start = (Fraction(day.toordinal() * _SECONDS_PER_DAY)
             + Fraction(reel.time_reference, reel.samplerate))
    end = start + Fraction(reel.num_samples, reel.samplerate)
    return ReelEntry(reel=reel, day=day, start=start, end=end)


def _build_from_days(reels: Sequence[Reel],
                     days: Sequence[date]) -> ReelLedger:
    # The input order is the physical reel chain order and is preserved:
    # a same-day re-recording starts earlier on the timeline but still
    # follows the reel it replaced, and that adjacency must be reported.
    entries = [
        _entry_for(reel, day) for reel, day in zip(reels, days)
    ]
    relations = [
        ReelRelation(
            previous=entries[index],
            current=entries[index + 1],
            delta=entries[index + 1].start - entries[index].end,
        )
        for index in range(len(entries) - 1)
    ]
    return ReelLedger(entries=entries, relations=relations)


def _check_known_days(reels: Sequence[Reel]) -> None:
    previous_day: Optional[date] = None
    for reel in reels:
        if reel.day is None:
            continue
        if previous_day is not None and reel.day < previous_day:
            raise ValueError(
                'Known origination dates are not in chronological order: '
                f'{previous_day.isoformat()} before '
                f'{reel.day.isoformat()}')
        previous_day = reel.day


def _check_window(window_start: Optional[date],
                  window_end: Optional[date]) -> None:
    if window_start is None or window_end is None:
        raise ValueError(
            'A date window (window_start and window_end) is required '
            'when reels lack an origination date')
    if window_end < window_start:
        raise ValueError('window_end must not be before window_start')


def build_ledger(reels: Sequence[Any]) -> ReelLedger:
    """Build a ledger from reels with known origination dates.

    Every reel must carry its collection day, either from the bext
    OriginationDate or from an explicit override. Days are taken as
    given; a decreasing TimeReference never advances the day, so a
    same-day re-recording is reported as an overlap instead of being
    joined across midnight.
    """
    coerced_reels = [_coerce_reel(reel) for reel in reels]
    undated = [
        reel.filename or f'reel {index}'
        for index, reel in enumerate(coerced_reels)
        if reel.day is None
    ]
    if undated:
        raise ValueError(
            'Cannot build a unique ledger without origination dates: '
            + ', '.join(undated)
            + '. Use candidate_ledgers() with a date window instead.')
    _check_known_days(coerced_reels)
    days = [_require_day(reel.day) for reel in coerced_reels]
    return _build_from_days(coerced_reels, days)


def _enumerate_day_sequences(reels: Sequence[Reel],
                             window_start: date,
                             window_end: date
                             ) -> List[Tuple[date, ...]]:
    known = {index: reel.day for index, reel in enumerate(reels)
             if reel.day is not None}
    for day in known.values():
        if day < window_start or day > window_end:
            raise ValueError(
                f'Known origination date {day.isoformat()} lies outside '
                'the given date window')

    sequences: List[Tuple[date, ...]] = []
    assignment: List[date] = []

    def next_day(day: date) -> date:
        return date.fromordinal(day.toordinal() + 1)

    def fill(position: int, lower: date) -> None:
        if position == len(reels):
            sequences.append(tuple(assignment))
            return
        upper = min(
            (known[index] for index in sorted(known)
             if index > position),
            default=window_end,
        )
        if lower > upper:
            return
        if position in known:
            anchor = known[position]
            if lower <= anchor <= upper:
                assignment.append(anchor)
                fill(position + 1, anchor)
                assignment.pop()
            return
        day = lower
        while day <= upper:
            assignment.append(day)
            fill(position + 1, day)
            assignment.pop()
            day = next_day(day)

    fill(0, window_start)
    return list(dict.fromkeys(sequences))


def candidate_ledgers(reels: Sequence[Any],
                      window_start: date,
                      window_end: date) -> List[ReelLedger]:
    """Enumerate every ledger consistent with an explicit date window.

    Use this when at least one reel lacks an origination date. Each
    non-decreasing day sequence that keeps known dates as anchors and
    stays within the window produces one candidate ledger. No day is
    ever inferred from sample counter behavior.
    """
    _check_window(window_start, window_end)
    coerced_reels = [_coerce_reel(reel) for reel in reels]
    _check_known_days(coerced_reels)
    sequences = _enumerate_day_sequences(
        coerced_reels, window_start, window_end)
    if not sequences:
        raise ValueError(
            'No day assignment fits the reels inside the given window')
    return [_build_from_days(coerced_reels, days) for days in sequences]
