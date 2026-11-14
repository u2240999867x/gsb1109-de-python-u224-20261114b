"""
Lead-time (SLA) clock helpers.

This module turns a calendar into a simple lead-time dashboard.  An order
starts its clock on a working day, may be paused several times, and is either
still in transit or has been signed for at the observation date.

Working-day elapsed time is always computed *as of* the observation date:

* a delivery dated after the observation date is not used early,
* a pause release dated after the observation date is not used early,
* paused working days are removed from the elapsed count,
* overlapping pause periods are merged so they are only removed once.

The elapsed count follows the same convention as
:meth:`workalendar.core.CoreCalendar.get_working_days_delta`: the start date
itself is not counted unless ``include_start=True`` is requested.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Mapping, Optional, Tuple, Union

from .core import WesternCalendar, cleaned_date, daterange


DateLike = Union[date, datetime]
_MISSING = object()


class LeadTimeState(str, Enum):
    """Settlement state of an order at the observation date."""

    DELIVERED_ON_TIME = "delivered_on_time"
    DELIVERED_LATE = "delivered_late"
    OVERDUE = "overdue"
    IN_TRANSIT = "in_transit"


@dataclass(frozen=True)
class PausePeriod:
    """A period during which the lead-time clock is stopped.

    ``end`` is the date the clock resumes.  With the default
    ``end_inclusive=False`` the resume date itself is a normal working day and
    only the days before it are paused.  Set ``end_inclusive=True`` when the
    contract states the pause includes its end date instead.

    When ``end`` is ``None`` the clock is still paused and remains paused
    through the observation date.
    """

    start: DateLike
    end: Optional[DateLike] = None
    end_inclusive: bool = False

    def __post_init__(self):
        start = cleaned_date(self.start)
        end = cleaned_date(self.end) if self.end is not None else None
        if end is not None and end < start:
            raise ValueError("A pause cannot end before it starts.")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def started_on(self):
        return self.start

    @property
    def resumed_on(self):
        return self.end

    @property
    def paused_through(self):
        """Last paused day, inclusive."""
        if self.end is None:
            return None
        if self.end_inclusive:
            return self.end
        return self.end - timedelta(days=1)


@dataclass(frozen=True)
class LeadTimeOrder:
    """An order whose working-day lead time is being measured."""

    started_on: DateLike
    sla_working_days: int
    reference: Optional[str] = None
    delivered_on: Optional[DateLike] = None
    pauses: Tuple[PausePeriod, ...] = field(default_factory=tuple)

    def __post_init__(self):
        started_on = cleaned_date(self.started_on)
        delivered_on = (
            cleaned_date(self.delivered_on)
            if self.delivered_on is not None
            else None
        )
        if self.sla_working_days < 0:
            raise ValueError("The SLA allowance cannot be negative.")
        if delivered_on is not None and delivered_on < started_on:
            raise ValueError("An order cannot be delivered before it starts.")
        pauses = tuple(_coerce_pause(pause) for pause in self.pauses)
        object.__setattr__(self, "started_on", started_on)
        object.__setattr__(self, "delivered_on", delivered_on)
        object.__setattr__(self, "pauses", pauses)

    @property
    def start(self):
        return self.started_on

    @property
    def allowed_working_days(self):
        return self.sla_working_days

    @property
    def signed_on(self):
        return self.delivered_on

    @property
    def pause_periods(self):
        return self.pauses


@dataclass(frozen=True)
class LeadTimeResult:
    """Measured lead time for one order at one observation date."""

    reference: Optional[str]
    started_on: date
    sla_working_days: int
    delivered_on: Optional[date]
    is_delivered: bool
    elapsed_working_days: int
    remaining_working_days: int
    overdue_working_days: int
    overdue: bool
    delivered_on_time: bool
    can_still_be_on_time: bool

    @property
    def state(self):
        if self.is_delivered:
            if self.overdue:
                return LeadTimeState.DELIVERED_LATE
            return LeadTimeState.DELIVERED_ON_TIME
        if self.overdue:
            return LeadTimeState.OVERDUE
        return LeadTimeState.IN_TRANSIT

    @property
    def is_in_transit(self):
        return not self.is_delivered

    @property
    def still_on_time(self):
        return self.can_still_be_on_time

    def __getitem__(self, key):
        return self.as_dict()[key]

    @property
    def overdue_days(self):
        return self.overdue_working_days

    def as_dict(self):
        return {
            "reference": self.reference,
            "started_on": self.started_on,
            "sla_working_days": self.sla_working_days,
            "delivered_on": self.delivered_on,
            "is_delivered": self.is_delivered,
            "elapsed_working_days": self.elapsed_working_days,
            "remaining_working_days": self.remaining_working_days,
            "overdue_working_days": self.overdue_working_days,
            "overdue": self.overdue,
            "delivered_on_time": self.delivered_on_time,
            "can_still_be_on_time": self.can_still_be_on_time,
            "state": self.state.value,
        }


@dataclass(frozen=True)
class LeadTimeDashboard:
    """A collection of lead-time results observed on the same date."""

    as_of: DateLike
    results: Tuple[LeadTimeResult, ...]

    def __post_init__(self):
        object.__setattr__(self, "as_of", cleaned_date(self.as_of))
        object.__setattr__(self, "results", tuple(self.results))

    @property
    def orders(self):
        return self.results

    @property
    def total_count(self):
        return len(self.results)

    @property
    def overdue_orders(self):
        return tuple(result for result in self.results if result.overdue)

    @property
    def overdue_count(self):
        return len(self.overdue_orders)

    @property
    def can_still_be_on_time_orders(self):
        return tuple(
            result for result in self.results
            if result.can_still_be_on_time
        )

    @property
    def can_still_be_on_time_count(self):
        return len(self.can_still_be_on_time_orders)

    @property
    def overdue_working_days(self):
        return sum(result.overdue_working_days for result in self.results)

    @property
    def overdue_days(self):
        return self.overdue_working_days

    @property
    def delivered_orders(self):
        return tuple(result for result in self.results if result.is_delivered)

    @property
    def delivered_count(self):
        return len(self.delivered_orders)

    @property
    def delivered_on_time_orders(self):
        return tuple(
            result for result in self.results
            if result.delivered_on_time
        )

    @property
    def delivered_on_time_count(self):
        return len(self.delivered_on_time_orders)

    @property
    def delivered_late_orders(self):
        return tuple(
            result for result in self.results
            if result.state is LeadTimeState.DELIVERED_LATE
        )

    @property
    def delivered_late_count(self):
        return len(self.delivered_late_orders)

    @property
    def in_transit_orders(self):
        return tuple(result for result in self.results if result.is_in_transit)

    @property
    def in_transit_count(self):
        return len(self.in_transit_orders)

    @property
    def still_on_time_orders(self):
        return self.can_still_be_on_time_orders

    @property
    def still_on_time_count(self):
        return self.can_still_be_on_time_count

    @property
    def possible_on_time_count(self):
        return self.can_still_be_on_time_count

    def summary(self):
        return self.as_dict()

    def __getitem__(self, key):
        return self.as_dict()[key]

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self):
        return len(self.results)

    def as_dict(self):
        return {
            "as_of": self.as_of,
            "total_count": self.total_count,
            "overdue_count": self.overdue_count,
            "overdue_working_days": self.overdue_working_days,
            "can_still_be_on_time_count": self.can_still_be_on_time_count,
            "delivered_count": self.delivered_count,
            "delivered_on_time_count": self.delivered_on_time_count,
            "delivered_late_count": self.delivered_late_count,
            "in_transit_count": self.in_transit_count,
            "orders": [result.as_dict() for result in self.results],
        }


def _first(mapping, names, default=_MISSING):
    for name in names:
        if name in mapping:
            return mapping[name]
    if default is _MISSING:
        raise ValueError(
            "Missing required order field; expected one of: "
            + ", ".join(names)
        )
    return default


def _coerce_pause(pause):
    if isinstance(pause, PausePeriod):
        return pause
    if isinstance(pause, Mapping):
        start = _first(
            pause,
            ("start", "started_on", "paused_on", "paused_at", "from"),
        )
        end = _first(
            pause,
            ("end", "resumed_on", "resumed_at", "released_on", "until",
             "to"),
            default=None,
        )
        end_inclusive = _first(
            pause, ("end_inclusive", "includes_end"), default=False)
        return PausePeriod(
            start=start, end=end, end_inclusive=bool(end_inclusive))
    if isinstance(pause, (tuple, list)):
        if len(pause) == 1:
            return PausePeriod(start=pause[0])
        if len(pause) == 2:
            return PausePeriod(start=pause[0], end=pause[1])
        if len(pause) == 3:
            return PausePeriod(
                start=pause[0], end=pause[1],
                end_inclusive=bool(pause[2]))
        raise ValueError(
            "A pause tuple must contain one, two or three items.")
    raise TypeError("Unsupported pause period: {!r}".format(pause))


def _coerce_order(order):
    if isinstance(order, LeadTimeOrder):
        return order
    if not isinstance(order, Mapping):
        raise TypeError("Unsupported order: {!r}".format(order))

    started_on = _first(
        order,
        ("started_on", "start", "created_on", "created", "order_date",
         "placed_on"),
    )
    sla_working_days = _first(
        order,
        ("sla_working_days", "allowed_working_days", "allowed", "sla_days",
         "target_working_days", "deadline_working_days"),
    )
    reference = _first(
        order, ("reference", "id", "order_id"), default=None)
    delivered_on = _first(
        order,
        ("delivered_on", "delivered_at", "signed_on", "signed_at",
         "received_on", "received_at", "delivery_date", "signed_date"),
        default=None,
    )
    pauses = _first(
        order,
        ("pauses", "pause_periods", "pause_intervals", "suspensions"),
        default=(),
    )
    return LeadTimeOrder(
        started_on=started_on,
        sla_working_days=sla_working_days,
        reference=reference,
        delivered_on=delivered_on,
        pauses=tuple(pauses or ()),
    )


def _count_working_days(calendar, first, last,
                        extra_working_days=None, extra_holidays=None):
    if first > last:
        return 0
    count = 0
    for day in daterange(first, last):
        if calendar.is_working_day(
                day,
                extra_working_days=extra_working_days,
                extra_holidays=extra_holidays):
            count += 1
    return count


def _as_pause(pause, end_inclusive):
    pause = _coerce_pause(pause)
    if pause.end is not None and end_inclusive and not pause.end_inclusive:
        pause = PausePeriod(
            start=pause.start, end=pause.end, end_inclusive=True)
    return pause


def _merged_pause_ranges(pauses, first, last):
    """Return inclusive, merged date ranges covered by pauses."""
    ranges = []
    for pause in pauses:
        pause = _coerce_pause(pause)
        range_first = max(pause.start, first)
        paused_through = pause.paused_through
        if paused_through is None:
            range_last = last
        else:
            range_last = min(paused_through, last)
        if range_first <= range_last:
            ranges.append((range_first, range_last))

    if not ranges:
        return ()

    ranges.sort()
    merged = [ranges[0]]
    for range_first, range_last in ranges[1:]:
        current_first, current_last = merged[-1]
        if range_first <= current_last + timedelta(days=1):
            merged[-1] = (current_first, max(current_last, range_last))
        else:
            merged.append((range_first, range_last))
    return tuple(merged)


def elapsed_working_days(start, end, calendar=None, pauses=None,
                         include_start=False, pause_end_inclusive=False,
                         extra_working_days=None, extra_holidays=None):
    """Count working days in ``(start, end]``, excluding paused days.

    ``pauses`` accepts :class:`PausePeriod` objects, ``(start, end)`` tuples,
    or mappings.  Pause periods may overlap, may start before ``start`` or run
    past ``end``; they are clipped to the measured range and merged before
    their working days are subtracted.

    A pause without an end date is considered active through ``end``.  By
    default a pause with an end date excludes the days before that end date;
    the end date itself is a normal working day.  Set
    ``pause_end_inclusive=True`` to treat the end date as paused too.
    """
    start = cleaned_date(start)
    end = cleaned_date(end)
    if end < start:
        return 0

    calendar = calendar or WesternCalendar()
    pauses = tuple(
        _as_pause(pause, pause_end_inclusive) for pause in pauses or ())

    first = start if include_start else start + timedelta(days=1)
    if first > end:
        return 0

    elapsed = _count_working_days(
        calendar,
        first,
        end,
        extra_working_days=extra_working_days,
        extra_holidays=extra_holidays,
    )
    paused = 0
    for range_first, range_last in _merged_pause_ranges(pauses, first, end):
        paused += _count_working_days(
            calendar,
            range_first,
            range_last,
            extra_working_days=extra_working_days,
            extra_holidays=extra_holidays,
        )
    return max(0, elapsed - paused)


def compute_lead_time(order, as_of, calendar=None, include_start=False,
                      pause_end_inclusive=False,
                      extra_working_days=None, extra_holidays=None):
    """Measure one order's lead time at ``as_of``.

    Deliveries dated after ``as_of`` leave the order in transit.  A pause
    release dated after ``as_of`` leaves the pause active through ``as_of``.
    """
    order = _coerce_order(order)
    as_of = cleaned_date(as_of)

    is_delivered = (
        order.delivered_on is not None
        and order.delivered_on <= as_of
    )
    effective_end = order.delivered_on if is_delivered else as_of
    if effective_end < order.started_on:
        effective_end = order.started_on

    elapsed = elapsed_working_days(
        order.started_on,
        effective_end,
        calendar=calendar,
        pauses=order.pauses,
        include_start=include_start,
        pause_end_inclusive=pause_end_inclusive,
        extra_working_days=extra_working_days,
        extra_holidays=extra_holidays,
    )
    remaining = order.sla_working_days - elapsed
    overdue = elapsed > order.sla_working_days
    delivered_on_time = is_delivered and not overdue
    can_still_be_on_time = (not is_delivered) and not overdue

    return LeadTimeResult(
        reference=order.reference,
        started_on=order.started_on,
        sla_working_days=order.sla_working_days,
        delivered_on=order.delivered_on,
        is_delivered=is_delivered,
        elapsed_working_days=elapsed,
        remaining_working_days=remaining,
        overdue_working_days=max(0, -remaining),
        overdue=overdue,
        delivered_on_time=delivered_on_time,
        can_still_be_on_time=can_still_be_on_time,
    )


def lead_time_dashboard(orders, as_of, calendar=None, include_start=False,
                        pause_end_inclusive=False,
                        extra_working_days=None, extra_holidays=None):
    """Build a lead-time dashboard for ``orders`` observed at ``as_of``.

    ``orders`` may be an iterable of :class:`LeadTimeOrder` objects or
    mappings.  A single mapping is treated as a one-order dashboard.
    """
    if isinstance(orders, Mapping):
        orders = (orders,)
    results = tuple(
        compute_lead_time(
            order,
            as_of,
            calendar=calendar,
            include_start=include_start,
            pause_end_inclusive=pause_end_inclusive,
            extra_working_days=extra_working_days,
            extra_holidays=extra_holidays,
        )
        for order in orders
    )
    return LeadTimeDashboard(as_of=as_of, results=results)


def summarize_lead_times(orders, as_of, calendar=None, **kwargs):
    """Return the dashboard summary as a plain dictionary."""
    return lead_time_dashboard(
        orders, as_of, calendar=calendar, **kwargs).as_dict()


@dataclass(frozen=True)
class LeadTimeClock:
    """A reusable calendar configuration for lead-time measurements."""

    calendar: object = field(default_factory=WesternCalendar)
    include_start: bool = False
    pause_end_inclusive: bool = False

    def elapsed(self, start, end, pauses=None, extra_working_days=None,
                extra_holidays=None):
        return elapsed_working_days(
            start,
            end,
            calendar=self.calendar,
            pauses=pauses,
            include_start=self.include_start,
            pause_end_inclusive=self.pause_end_inclusive,
            extra_working_days=extra_working_days,
            extra_holidays=extra_holidays,
        )

    def measure(self, order, as_of, extra_working_days=None,
                extra_holidays=None):
        return compute_lead_time(
            order,
            as_of,
            calendar=self.calendar,
            include_start=self.include_start,
            pause_end_inclusive=self.pause_end_inclusive,
            extra_working_days=extra_working_days,
            extra_holidays=extra_holidays,
        )

    def dashboard(self, orders, as_of, extra_working_days=None,
                  extra_holidays=None):
        return lead_time_dashboard(
            orders,
            as_of,
            calendar=self.calendar,
            include_start=self.include_start,
            pause_end_inclusive=self.pause_end_inclusive,
            extra_working_days=extra_working_days,
            extra_holidays=extra_holidays,
        )


# Friendly short aliases for callers building order fixtures.
Order = LeadTimeOrder
Pause = PausePeriod
LeadTime = LeadTimeResult
Dashboard = LeadTimeDashboard
analyze_order = compute_lead_time
order_lead_time = compute_lead_time
dashboard = lead_time_dashboard


__all__ = (
    "LeadTimeState",
    "PausePeriod",
    "LeadTimeOrder",
    "LeadTimeResult",
    "LeadTimeDashboard",
    "LeadTimeClock",
    "elapsed_working_days",
    "compute_lead_time",
    "lead_time_dashboard",
    "summarize_lead_times",
    "Order",
    "Pause",
    "LeadTime",
    "Dashboard",
    "analyze_order",
    "order_lead_time",
    "dashboard",
)
