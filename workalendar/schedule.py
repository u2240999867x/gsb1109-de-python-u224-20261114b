"""
Nominal-period (e.g. month-end) run scheduling on top of a workalendar
calendar.

A nominal period keeps its identity no matter when it is actually executed.
Two distinct periods may land on the same execution date (typically around
long holidays) but they remain two separate tasks: each one keeps its own
run identity and a resend always targets a single, explicitly chosen period.

Scheduling policy
-----------------

* ``restricted`` (the default): postpone to the next working day, but if that
  working day would fall in a later calendar month, fall back to the *last
  working day of the nominal month* instead. The run never leaves its nominal
  month.
* ``unrestricted``: postpone unconditionally to the next working day, even if
  it falls in the next month. Only some products are allowed to use it.

If a whole nominal month contains no working day at all, the restricted
fallback cannot produce a date. Rather than silently stealing a slot in the
previous month (i.e. occupying another period's execution day), a
:class:`~workalendar.exceptions.NoWorkingDayInPeriodError` is raised.
"""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from .exceptions import NoWorkingDayInPeriodError, UnknownPeriodError

RESTRICTED = "restricted"
UNRESTRICTED = "unrestricted"
POLICIES = (RESTRICTED, UNRESTRICTED)


@dataclass(frozen=True)
class ScheduledRun:
    """A single run in a schedule table.

    ``period`` is the nominal accounting period (``(year, month)``) while
    ``run_date`` is the actual execution date. They differ whenever the
    nominal anchor day is not a working day. The ``uid`` is stable and unique
    per (product, period), which is what makes two periods sharing an
    execution date independently addressable.
    """

    product: str
    period: tuple
    anchor_date: date
    run_date: date
    policy: str
    uid: str

    @property
    def year(self):
        return self.period[0]

    @property
    def month(self):
        return self.period[1]

    @property
    def carried_over(self):
        """True when the run is executed later than its nominal anchor."""
        return self.run_date > self.anchor_date

    def as_dict(self):
        return {
            "product": self.product,
            "period": self.period,
            "anchor_date": self.anchor_date,
            "run_date": self.run_date,
            "policy": self.policy,
            "uid": self.uid,
        }


class RunSchedule:
    """The run table of one product for a range of nominal periods."""

    def __init__(self, product, runs):
        self.product = product
        # Keep insertion order, indexed by period for direct lookup.
        self._runs = list(runs)
        self._index = {run.period: run for run in self._runs}
        if len(self._index) != len(self._runs):
            raise ValueError(
                f"Duplicate nominal periods for product {product!r}"
            )

    def __iter__(self):
        return iter(self._runs)

    def __len__(self):
        return len(self._runs)

    def __contains__(self, period):
        return period in self._index

    @property
    def periods(self):
        return tuple(self._index)

    def get(self, period):
        """Return the run bound to a nominal period.

        Lookup is by period, never by execution date, so that two periods
        sharing a run date cannot be confused when resending.
        """
        try:
            return self._index[period]
        except KeyError:
            raise UnknownPeriodError(period, self.product)

    def on(self, run_date):
        """All runs executed on a given date (several periods may collide)."""
        run_date = date(run_date.year, run_date.month, run_date.day)
        return [run for run in self._runs if run.run_date == run_date]

    def resend(self, period):
        """Select the single run to resend for an explicit nominal period."""
        return self.get(period)

    def as_dicts(self):
        return [run.as_dict() for run in self._runs]


class PeriodScheduler:
    """Compute run dates while preserving nominal period identity.

    :param calendar: a workalendar ``Calendar`` instance (must implement
        ``is_working_day``).
    :param policy: ``RESTRICTED`` (default) or ``UNRESTRICTED``.
    :param anchor_day: day of month used as the nominal anchor, e.g. the last
        day of the month when ``None`` (month-end collection).
    :param extra_working_days: forwarded to ``calendar.is_working_day``.
    :param extra_holidays: forwarded to ``calendar.is_working_day``.
    """

    def __init__(self, calendar, policy=RESTRICTED, anchor_day=None,
                 extra_working_days=None, extra_holidays=None):
        if policy not in POLICIES:
            raise ValueError(
                f"Unknown policy {policy!r}, expected one of {POLICIES}"
            )
        self.calendar = calendar
        self.policy = policy
        self.anchor_day = anchor_day
        self.extra_working_days = extra_working_days
        self.extra_holidays = extra_holidays

    def _is_working_day(self, day):
        return self.calendar.is_working_day(
            day,
            extra_working_days=self.extra_working_days,
            extra_holidays=self.extra_holidays,
        )

    def anchor_date(self, year, month):
        """Nominal anchor date for a period (defaults to month-end)."""
        last_day = monthrange(year, month)[1]
        if self.anchor_day is None:
            day = last_day
        else:
            day = min(self.anchor_day, last_day)
        return date(year, month, day)

    def _last_working_day_of_month(self, year, month):
        last_day = monthrange(year, month)[1]
        candidate = date(year, month, last_day)
        while not self._is_working_day(candidate):
            candidate -= timedelta(days=1)
            if candidate.month != month:
                return None
        return candidate

    def _next_working_day(self, anchor):
        candidate = anchor
        # The anchor itself is a valid execution date when workable.
        while not self._is_working_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def schedule_period(self, year, month, product=None):
        """Compute the :class:`ScheduledRun` of one nominal period.

        Restricted policy: postpone to the next working day, unless it crosses
        into a later month — in which case the last working day of the nominal
        month is used instead. Unrestricted policy: postpone unconditionally.
        """
        anchor = self.anchor_date(year, month)
        if self.policy == UNRESTRICTED:
            run_date = self._next_working_day(anchor)
        else:
            following = self._next_working_day(anchor)
            if following.month == month and following.year == year:
                run_date = following
            else:
                # Crossing the month boundary: stay inside the nominal month.
                run_date = self._last_working_day_of_month(year, month)
                if run_date is None:
                    raise NoWorkingDayInPeriodError(
                        year, month, product=product, period=(year, month)
                    )
        uid = build_uid(product, (year, month))
        return ScheduledRun(
            product=product,
            period=(year, month),
            anchor_date=anchor,
            run_date=run_date,
            policy=self.policy,
            uid=uid,
        )

    def build_schedule(self, product, periods):
        """Build the ordered run table for a product and ``(year, month)``
        periods."""
        runs = [
            self.schedule_period(year, month, product=product)
            for year, month in periods
        ]
        return RunSchedule(product, runs)


def build_uid(product, period):
    """Stable, unique identity for a (product, nominal period) pair."""
    year, month = period
    if product is None:
        return f"{year:04d}-{month:02d}"
    return f"{product}:{year:04d}-{month:02d}"


def month_periods(start, end):
    """Yield every ``(year, month)`` from ``start`` to ``end`` inclusive.

    ``start`` and ``end`` may be either ``(year, month)`` tuples or
    ``date`` instances.
    """
    if isinstance(start, date):
        start = (start.year, start.month)
    if isinstance(end, date):
        end = (end.year, end.month)
    year, month = start
    end_year, end_month = end
    if (year, month) > (end_year, end_month):
        raise ValueError("start period must be before or equal to end period")
    while (year, month) <= (end_year, end_month):
        yield (year, month)
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1


def build_product_schedules(calendar, product_policies, periods,
                            anchor_day=None,
                            extra_working_days=None,
                            extra_holidays=None):
    """Build run tables for several products with their own policy.

    :param product_policies: mapping of ``product name -> policy``.
    :param periods: iterable of ``(year, month)`` nominal periods.
    :returns: dict of ``product name -> RunSchedule``.
    """
    schedules = {}
    periods = list(periods)
    for product, policy in product_policies.items():
        scheduler = PeriodScheduler(
            calendar,
            policy=policy,
            anchor_day=anchor_day,
            extra_working_days=extra_working_days,
            extra_holidays=extra_holidays,
        )
        schedules[product] = scheduler.build_schedule(product, periods)
    return schedules
