"""
Shift attendance accounting on top of a local calendar and time zone.

A shift is described by two *local* wall-clock punches (clock-in and
clock-out) interpreted in a given time zone.  Local punches alone are not
always enough to pin an instant down: during the daylight-saving-time
"fall back", a wall-clock time inside the repeated hour maps to two
different instants.  Such punches are reported as candidates instead of
being silently ordered.

Two labour contracts are supported:

* ``SHIFT_START`` ("whole shift belongs to the clock-in day"): the whole
  duration of a shift is booked on the calendar day on which it starts.
* ``SEGMENTED`` ("split at local midnight"): the shift is cut at every
  local midnight and each segment is booked on its own calendar day.

Whichever contract is used, the booked segments add up exactly to the
actual elapsed time of the shift, including the extra hour worked through
a fall-back transition.  Durations are always computed on the UTC
timeline, because subtracting two aware datetimes directly ignores a
change in UTC offset.
"""
from collections import namedtuple
from datetime import datetime, timedelta, timezone

try:  # pragma: no cover - import branch depends on the runtime
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    from backports.zoneinfo import ZoneInfo

from .exceptions import AmbiguousPunchError, InvalidShiftError

UTC = timezone.utc

#: The whole shift is booked on the day of the clock-in punch.
SHIFT_START = "shift_start"
#: The shift is split at each local midnight and booked per local day.
SEGMENTED = "segmented"
CONTRACTS = frozenset((SHIFT_START, SEGMENTED))

Segment = namedtuple(
    "Segment", ("start", "end", "day", "duration", "is_working_day")
)
DayBooking = namedtuple("DayBooking", ("working", "holiday"))
Settlement = namedtuple(
    "Settlement",
    (
        "start",
        "end",
        "contract",
        "segments",
        "bookings",
        "working_total",
        "holiday_total",
        "total",
    ),
)


def _coerce_tz(tz):
    if isinstance(tz, ZoneInfo):
        return tz
    if isinstance(tz, str):
        return ZoneInfo(tz)
    raise InvalidShiftError(f"Unsupported time zone: {tz!r}")


def _check_naive(when, label):
    if not isinstance(when, datetime):
        raise InvalidShiftError(
            f"{label} must be a datetime, got {type(when).__name__}"
        )
    if when.tzinfo is not None:
        raise InvalidShiftError(
            f"{label} must be a naive local datetime, got {when!r}"
        )


def _as_utc(when):
    """Return the UTC equivalent of an aware datetime.

    Comparing two aware datetimes that share the same ``tzinfo`` object
    only compares their wall-clock parts (fold is ignored), so every
    ordering check in this module goes through UTC explicitly.
    """
    return when.astimezone(UTC)


def local_candidates(when, tz):
    """Resolve a naive local punch to the instants it may denote.

    Returns a tuple of one or two aware datetimes:

    * one instant for an ordinary wall-clock time,
    * two instants for a wall-clock time inside the repeated hour of a
      fall-back transition, ordered from the first (pre-transition)
      occurrence to the second (post-transition) one.

    A wall-clock time inside a spring-forward gap never happens on the
    local clock and raises ``InvalidShiftError``.
    """
    _check_naive(when, "Punch")
    tz = _coerce_tz(tz)
    first = when.replace(tzinfo=tz)
    second = when.replace(fold=1, tzinfo=tz)
    if first.utcoffset() == second.utcoffset():
        return (first,)
    if _as_utc(first) < _as_utc(second):
        # Repeated hour: the fold=0 instant is the earlier occurrence.
        return (first, second)
    # Non-existent hour: fold=1 lands before fold=0 across the gap.
    raise InvalidShiftError(
        f"Local time {when.isoformat()} does not exist in {tz}"
    )


def _elapsed(start, end):
    """Actual elapsed time between two aware datetimes (UTC timeline)."""
    return _as_utc(end) - _as_utc(start)


def _local_midnights(start, end, tz):
    """Aware local-midnight instants strictly between start and end."""
    start_utc = _as_utc(start)
    end_utc = _as_utc(end)
    day = start.astimezone(tz).date() + timedelta(days=1)
    last_day = end.astimezone(tz).date()
    boundaries = []
    while day <= last_day:
        boundary = datetime(day.year, day.month, day.day, tzinfo=tz)
        if start_utc < _as_utc(boundary) < end_utc:
            boundaries.append(boundary)
        day += timedelta(days=1)
    return boundaries


def _split_segments(start, end, tz, calendar):
    """Split a resolved shift at local midnights, UTC-accurate."""
    points = [start]
    points.extend(_local_midnights(start, end, tz))
    points.append(end)
    segments = []
    for left, right in zip(points, points[1:]):
        if _as_utc(right) <= _as_utc(left):
            continue
        day = left.astimezone(tz).date()
        segments.append(
            Segment(
                start=left,
                end=right,
                day=day,
                duration=_elapsed(left, right),
                is_working_day=calendar.is_working_day(day),
            )
        )
    return segments


def _book(segments, contract, start_day):
    bookings = {}
    if contract == SHIFT_START:
        total = sum(
            (segment.duration for segment in segments), timedelta()
        )
        is_working = segments[0].is_working_day
        bookings[start_day] = DayBooking(
            working=total if is_working else timedelta(),
            holiday=timedelta() if is_working else total,
        )
    else:
        for segment in segments:
            booking = bookings.setdefault(
                segment.day,
                DayBooking(working=timedelta(), holiday=timedelta()),
            )
            if segment.is_working_day:
                bookings[segment.day] = DayBooking(
                    working=booking.working + segment.duration,
                    holiday=booking.holiday,
                )
            else:
                bookings[segment.day] = DayBooking(
                    working=booking.working,
                    holiday=booking.holiday + segment.duration,
                )
    working_total = sum(
        (booking.working for booking in bookings.values()), timedelta()
    )
    holiday_total = sum(
        (booking.holiday for booking in bookings.values()), timedelta()
    )
    total = working_total + holiday_total
    return bookings, working_total, holiday_total, total


class ResolvedShift:
    """A shift whose two punches denote single, ordered instants."""

    def __init__(self, start, end, tz, calendar, contract):
        self.start = start
        self.end = end
        self.tz = tz
        self.calendar = calendar
        self.contract = contract
        self.segments = _split_segments(start, end, tz, calendar)
        (
            self.bookings,
            self.working_total,
            self.holiday_total,
            self.total,
        ) = _book(
            self.segments, contract, start.astimezone(tz).date()
        )

    @property
    def actual_duration(self):
        return _elapsed(self.start, self.end)

    def settle(self):
        return Settlement(
            start=self.start,
            end=self.end,
            contract=self.contract,
            segments=tuple(self.segments),
            bookings=dict(self.bookings),
            working_total=self.working_total,
            holiday_total=self.holiday_total,
            total=self.total,
        )


class Shift:
    """A pair of naive local punches in a time zone.

    ``clock_in`` and ``clock_out`` are naive ``datetime`` objects giving
    the wall-clock times recorded by the punch clock; ``tz`` is either a
    ``ZoneInfo`` object or an IANA time-zone name.  Overnight shifts are
    allowed, so the clock-out wall-clock time may be "earlier" than the
    clock-in wall-clock time.
    """

    def __init__(self, clock_in, clock_out, tz):
        _check_naive(clock_in, "clock_in")
        _check_naive(clock_out, "clock_out")
        self.clock_in = clock_in
        self.clock_out = clock_out
        self.tz = _coerce_tz(tz)
        self.start_candidates = local_candidates(clock_in, self.tz)
        self.end_candidates = local_candidates(clock_out, self.tz)
        self.ambiguities = []
        if len(self.start_candidates) > 1:
            self.ambiguities.append((clock_in, self.start_candidates))
        if len(self.end_candidates) > 1:
            self.ambiguities.append((clock_out, self.end_candidates))
        self._validate_order()

    @property
    def is_ambiguous(self):
        return bool(self.ambiguities)

    def _validate_order(self):
        if not any(
            _as_utc(start) < _as_utc(end)
            for start in self.start_candidates
            for end in self.end_candidates
        ):
            raise InvalidShiftError(
                "No ordering puts clock-in before clock-out: "
                f"{self.clock_in.isoformat()} -> "
                f"{self.clock_out.isoformat()}"
            )

    def candidates(self, calendar, contract=SEGMENTED):
        """Every coherent (clock-in, clock-out) instant combination.

        Returns one :class:`ResolvedShift` per combination whose clock-in
        instant precedes its clock-out instant.  An unambiguous shift
        yields exactly one result.
        """
        if contract not in CONTRACTS:
            raise InvalidShiftError(f"Unknown contract: {contract!r}")
        resolved = []
        for start in self.start_candidates:
            for end in self.end_candidates:
                if _as_utc(start) < _as_utc(end):
                    resolved.append(
                        ResolvedShift(
                            start, end, self.tz, calendar, contract
                        )
                    )
        return resolved

    def resolve(self, calendar, contract=SEGMENTED):
        """Resolve an unambiguous shift, or raise ``AmbiguousPunchError``."""
        if self.is_ambiguous:
            when, punch_candidates = self.ambiguities[0]
            raise AmbiguousPunchError(when, punch_candidates)
        return self.candidates(calendar, contract)[0]


def settle_shift(clock_in, clock_out, tz, calendar, contract=SEGMENTED):
    """Book one shift onto working days / holidays.

    Raises :class:`AmbiguousPunchError` when a punch falls in the repeated
    hour of a fall-back transition; use
    :func:`settle_shift_candidates` in that case to enumerate the
    possible settlements.
    """
    shift = Shift(clock_in, clock_out, tz)
    return shift.resolve(calendar, contract).settle()


def settle_shift_candidates(
    clock_in, clock_out, tz, calendar, contract=SEGMENTED
):
    """Return every possible settlement for an ambiguous shift.

    For an ordinary shift the returned list contains exactly one
    settlement, making this safe to call unconditionally.
    """
    shift = Shift(clock_in, clock_out, tz)
    return [
        resolved.settle()
        for resolved in shift.candidates(calendar, contract)
    ]
