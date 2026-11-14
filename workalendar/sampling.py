"""
Inspectable monthly sampling plans.

A common contract rule is::

    sample on the second business day after the fifth Monday of the month

The rule has two separately inspectable steps:

* :class:`BaselineRule` finds the fifth Monday.  When a month has no fifth
  Monday the contract either skips the month or uses the last Monday instead.
* :class:`OffsetRule` counts business days after that baseline.  The contract
  also states whether a baseline that is a holiday counts as day zero.

Every month is evaluated to a :class:`SamplingTask`.  Months that do not
produce a sample are still returned by :meth:`SamplingPlan.evaluate_month` so
that callers can distinguish:

* :attr:`SamplingStatus.BASELINE_MISSING` - no fifth Monday and no fallback;
* :attr:`SamplingStatus.ROLLED_OVER_MONTH` - the target is in another month;
* :attr:`SamplingStatus.WINDOW_EXCLUDED` - the whole window is unavailable.

Queries for a quarter and queries for its three months return the same tasks.
Task identity never contains the query range, so merging monthly results is
the same operation as asking for the quarter directly.
"""
from __future__ import annotations

import hashlib
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import (
    Any, Dict, Iterable, Iterator, List, Optional, Tuple, Union,
)

from .core import MON, Calendar, WesternCalendar, cleaned_date


class FifthMondayPolicy(str, Enum):
    """What to do when a month has no fifth Monday."""

    SKIP = "skip"
    LAST_MONDAY = "last_monday"

    # Friendly aliases.
    SKIP_IF_MISSING = "skip"
    SKIP_MONTH = "skip"
    OMIT = "skip"
    EXCLUDE = "skip"
    USE_LAST_MONDAY = "last_monday"
    USE_LAST = "last_monday"
    LAST = "last_monday"
    LAST_AVAILABLE = "last_monday"


class DayZeroPolicy(str, Enum):
    """How a non-business baseline starts the business-day offset.

    ``INCLUDE`` treats the baseline date as day zero.  If it is a holiday the
    next business day is day one, and the second business day is day two.

    ``EXCLUDE`` does not use a holiday baseline as day zero.  Counting starts
    at the first business day on or after it.  For a baseline that is already
    a business day both policies give the same result.
    """

    INCLUDE = "include"
    EXCLUDE = "exclude"

    # Friendly aliases.
    COUNT = "include"
    COUNT_DAY_ZERO = "include"
    INCLUDE_BASELINE = "include"
    BASELINE_IS_DAY_ZERO = "include"
    DID_NOT_COUNT = "exclude"
    SKIP_DAY_ZERO = "exclude"
    EXCLUDE_BASELINE = "exclude"
    BASELINE_IS_NOT_DAY_ZERO = "exclude"


class SamplingStatus(str, Enum):
    """The outcome of evaluating one sampling month."""

    OK = "ok"
    BASELINE_MISSING = "baseline_missing"
    ROLLED_OVER_MONTH = "rolled_over_month"
    WINDOW_EXCLUDED = "window_excluded"

    # Friendly aliases.
    BASELINE_NOT_FOUND = "baseline_missing"
    BASELINE_ABSENT = "baseline_missing"
    NO_BASELINE = "baseline_missing"
    MISSING_BASELINE = "baseline_missing"
    BASELINE_DOES_NOT_EXIST = "baseline_missing"
    NO_FIFTH_MONDAY = "baseline_missing"
    MONTH_ROLLOVER = "rolled_over_month"
    ROLLOVER = "rolled_over_month"
    CROSSES_MONTH = "rolled_over_month"
    CROSSED_MONTH = "rolled_over_month"
    SHIFTED_ACROSS_MONTH = "rolled_over_month"
    CROSS_MONTH = "rolled_over_month"
    ROLLOVER_NEXT_MONTH = "rolled_over_month"
    SHIFTED_TO_NEXT_MONTH = "rolled_over_month"
    EXCLUDED_WINDOW = "window_excluded"
    EXCLUDED = "window_excluded"
    WINDOW_FULLY_EXCLUDED = "window_excluded"
    ENTIRE_WINDOW_EXCLUDED = "window_excluded"
    WHOLE_WINDOW_EXCLUDED = "window_excluded"


TaskStatus = SamplingStatus


def _coerce_enum(value: Any, enum_class: Any, boolean_names=None):
    """Coerce strings, booleans and enum members into an enum member."""
    if isinstance(value, enum_class):
        return value
    if isinstance(value, bool) and boolean_names:
        value = boolean_names[value]
    if isinstance(value, str):
        text = value.strip().lower().replace("-", "_").replace(" ", "_")
        for member in enum_class:
            if text in (member.value, member.name.lower()):
                return member
    try:
        return enum_class(value)
    except (ValueError, TypeError):
        pass
    try:
        return getattr(enum_class, str(value).upper())
    except AttributeError:
        pass
    raise ValueError(
        f"{value!r} is not a valid {enum_class.__name__} value"
    )


@dataclass(frozen=True)
class SamplingWindow:
    """A closed date interval, usually used as a blackout window."""

    start: date
    end: date
    label: Optional[str] = None

    def __post_init__(self):
        start = _as_date(self.start)
        end = _as_date(self.end)
        if start > end:
            start, end = end, start
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def contains(self, day: date) -> bool:
        day = _as_date(day)
        return self.start <= day <= self.end

    def overlaps(self, start: date, end: date) -> bool:
        start = _as_date(start)
        end = _as_date(end)
        if start > end:
            start, end = end, start
        return self.start <= end and start <= self.end

    def clip(self, start: date, end: date) -> Optional[Tuple[date, date]]:
        """Return the intersection with ``[start, end]``, if any."""
        start = _as_date(start)
        end = _as_date(end)
        if start > end:
            start, end = end, start
        if not self.overlaps(start, end):
            return None
        return max(self.start, start), min(self.end, end)

    def as_tuple(self) -> Tuple[date, date]:
        return self.start, self.end

    def as_dict(self) -> Dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "label": self.label,
        }

    def __iter__(self):
        yield self.start
        yield self.end


BlackoutWindow = SamplingWindow


def _as_window(value: Any) -> SamplingWindow:
    if isinstance(value, SamplingWindow):
        return value
    if isinstance(value, (tuple, list)) and len(value) in (2, 3):
        return SamplingWindow(*value)
    if hasattr(value, "start") and hasattr(value, "end"):
        return SamplingWindow(
            value.start, value.end, getattr(value, "label", None)
        )
    raise TypeError(
        "a window must be a SamplingWindow, a (start, end[, label]) "
        f"sequence, or expose start/end attributes; got {value!r}"
    )


def _as_date(value: Any) -> date:
    """Accept dates, datetimes, and ISO date strings."""
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    return cleaned_date(value)


def _as_date_tuple(values: Any, name: str) -> Tuple[Any, ...]:
    """Normalize a single date or a group of dates into a tuple."""
    if values is None:
        return ()
    if isinstance(values, (str, date)):
        return (values,)
    if isinstance(values, dict):
        return tuple(values.values())
    try:
        return tuple(values)
    except TypeError:
        raise TypeError(f"{name} must be a date or an iterable of dates")


def _as_window_list(values: Any) -> Tuple[SamplingWindow, ...]:
    """Normalize one window, or an iterable of windows, into a tuple."""
    if values is None:
        return ()
    if isinstance(values, SamplingWindow):
        return (values,)
    if isinstance(values, (tuple, list)) and len(values) in (2, 3):
        # A single (start, end[, label]) window, rather than a sequence of
        # windows.  Date objects are never iterated here.
        if not isinstance(values[0], (tuple, list, SamplingWindow)):
            return (SamplingWindow(*values),)
    return tuple(_as_window(value) for value in values)


def _as_month_list(values: Any) -> Tuple[Tuple[int, int], ...]:
    """Normalize one month, or an iterable of months, into tuples."""
    if values is None:
        return ()
    if isinstance(values, (str, date, SamplingTask)):
        return (_parse_month(values),)
    if isinstance(values, (tuple, list)) and len(values) == 2:
        if not isinstance(values[0], (tuple, list)):
            return (_parse_month(*values),)
    return tuple(
        _parse_month(*item) if isinstance(item, (tuple, list))
        else _parse_month(item)
        for item in values
    )


def _parse_month(year: Any, month: Any = None) -> Tuple[int, int]:
    """Normalize many common month spellings to ``(year, month)``."""
    if isinstance(year, date):
        year, month = year.year, year.month
    elif isinstance(year, SamplingTask):
        year, month = year.year, year.month
    elif isinstance(year, str):
        text = year.strip().upper().replace("/", "-")
        if "Q" in text:
            parsed_year, parsed_month = _parse_quarter(text)[:2]
            year = parsed_year
            if month is None:
                month = parsed_month
        else:
            parts = text.split("-")
            if len(parts) != 2:
                raise ValueError(f"cannot parse month from {year!r}")
            year = int(parts[0])
            if month is None:
                month = int(parts[1])
    elif month is None and isinstance(year, (tuple, list)) and len(year) == 2:
        year, month = year
    if month is None:
        raise ValueError("month is required")
    year, month = int(year), int(month)
    if not 1 <= month <= 12:
        raise ValueError(f"month must be between 1 and 12, got {month}")
    return year, month


def _parse_quarter(year: Any, quarter: Any = None) -> Tuple[int, int, int]:
    """Return ``(year, first_month, last_month)`` for a quarter."""
    year, quarter = _normalize_quarter(year, quarter)
    first_month = (quarter - 1) * 3 + 1
    return year, first_month, first_month + 2


def _normalize_quarter(year: Any, quarter: Any = None) -> Tuple[int, int]:
    """Return ``(year, quarter_number)`` for common quarter spellings."""
    # Also accept the quarter-first spelling: (2, 2021) or ("Q2", 2021).
    if (isinstance(year, str) and year.strip().upper().startswith("Q")
            and quarter is not None and not isinstance(quarter, str)):
        year, quarter = quarter, year
    elif (isinstance(year, int) and 1 <= year <= 4
            and isinstance(quarter, int) and quarter > 4):
        year, quarter = quarter, year
    if isinstance(year, str) and quarter is None:
        text = year.strip().upper().replace("/", "-")
        if "Q" not in text:
            raise ValueError(f"cannot parse quarter from {year!r}")
        year_part, quarter_part = text.split("Q", 1)
        year = year_part.strip("-")
        quarter = quarter_part.strip("-")
    elif (isinstance(year, (tuple, list))
          and len(year) == 2 and quarter is None):
        year, quarter = year
        if (isinstance(year, int) and 1 <= year <= 4
                and isinstance(quarter, int) and quarter > 4):
            year, quarter = quarter, year
    if quarter is None:
        raise ValueError("quarter is required")
    year = int(year)
    if isinstance(quarter, str):
        quarter = int(quarter.strip().upper().lstrip("Q"))
    quarter = int(quarter)
    if not 1 <= quarter <= 4:
        raise ValueError(f"quarter must be between 1 and 4, got {quarter}")
    return year, quarter


def _quarter_months(year: Any, quarter: Any = None) -> range:
    _, first_month, last_month = _parse_quarter(year, quarter)
    return range(first_month, last_month + 1)


@dataclass(frozen=True)
class BaselineResolution:
    """The result of the baseline step for one month."""

    year: int
    month: int
    baseline: Optional[date]
    kind: Optional[str]
    used_fallback: bool
    reason: str

    @property
    def missing(self) -> bool:
        return self.baseline is None


@dataclass(frozen=True)
class BaselineRule:
    """Resolve the fifth-Monday baseline, or apply the contract fallback."""

    ordinal: int = 5
    weekday: int = MON
    missing_policy: FifthMondayPolicy = FifthMondayPolicy.SKIP

    def __post_init__(self):
        object.__setattr__(self, "ordinal", int(self.ordinal))
        object.__setattr__(self, "weekday", int(self.weekday))
        if self.ordinal < 1:
            raise ValueError("ordinal must be at least 1")
        object.__setattr__(
            self,
            "missing_policy",
            _coerce_enum(
                self.missing_policy,
                FifthMondayPolicy,
                {True: FifthMondayPolicy.SKIP,
                 False: FifthMondayPolicy.LAST_MONDAY},
            ),
        )

    def resolve(self, calendar: Any, year: Any, month: Any = None):
        year, month = _parse_month(year, month)
        nth_weekday = getattr(
            calendar, "get_nth_weekday_in_month",
            Calendar.get_nth_weekday_in_month,
        )
        last_weekday = getattr(
            calendar, "get_last_weekday_in_month",
            Calendar.get_last_weekday_in_month,
        )
        baseline = nth_weekday(
            year, month, self.weekday, self.ordinal
        )
        if baseline is not None:
            return BaselineResolution(
                year=year,
                month=month,
                baseline=baseline,
                kind="fifth_monday",
                used_fallback=False,
                reason=f"the month has a fifth Monday ({baseline})",
            )
        if self.missing_policy is FifthMondayPolicy.SKIP:
            return BaselineResolution(
                year=year,
                month=month,
                baseline=None,
                kind=None,
                used_fallback=False,
                reason="the month has no fifth Monday; skipped by policy",
            )
        fallback = last_weekday(year, month, self.weekday)
        return BaselineResolution(
            year=year,
            month=month,
            baseline=fallback,
            kind="last_monday",
            used_fallback=True,
            reason=(
                "the month has no fifth Monday; using the last Monday "
                f"({fallback})"
            ),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "weekday": self.weekday,
            "weekday_name": "Monday",
            "missing_policy": self.missing_policy.value,
            "when_missing": self.missing_policy.value,
        }

    def describe(self) -> str:
        if self.missing_policy is FifthMondayPolicy.SKIP:
            fallback = "skip the month"
        else:
            fallback = "use the last Monday"
        return (
            f"Use the {self.ordinal}th Monday of the month; if it does not "
            f"exist, {fallback}."
        )


@dataclass(frozen=True)
class OffsetRule:
    """Count business days after the baseline."""

    business_days: int = 2
    day_zero_policy: DayZeroPolicy = DayZeroPolicy.INCLUDE

    def __post_init__(self):
        try:
            business_days = int(self.business_days)
        except (TypeError, ValueError):
            raise ValueError("business_days must be an integer")
        if business_days < 0:
            raise ValueError("business_days must be zero or positive")
        object.__setattr__(self, "business_days", business_days)
        object.__setattr__(
            self,
            "day_zero_policy",
            _coerce_enum(
                self.day_zero_policy,
                DayZeroPolicy,
                {True: DayZeroPolicy.INCLUDE,
                 False: DayZeroPolicy.EXCLUDE},
            ),
        )

    @property
    def offset(self) -> int:
        return self.business_days

    @property
    def count_day_zero(self) -> bool:
        return self.day_zero_policy is DayZeroPolicy.INCLUDE

    def as_dict(self) -> Dict[str, Any]:
        return {
            "business_days": self.business_days,
            "offset": self.business_days,
            "day_zero_policy": self.day_zero_policy.value,
            "count_day_zero": self.count_day_zero,
        }

    def describe(self) -> str:
        if self.count_day_zero:
            day_zero = (
                "A non-business baseline still counts as day zero."
            )
        else:
            day_zero = (
                "A non-business baseline is not day zero; counting starts "
                "at the next business day."
            )
        return (
            f"Count {self.business_days} business day(s) after the "
            f"baseline. {day_zero}"
        )


@dataclass(frozen=True)
class SamplingRule:
    """The two inspectable steps of a monthly sampling rule."""

    baseline_rule: BaselineRule = BaselineRule()
    offset_rule: OffsetRule = OffsetRule()

    def __init__(
        self,
        offset: int = 2,
        missing_policy: Union[FifthMondayPolicy, str, None] = None,
        day_zero_policy: Union[DayZeroPolicy, str, bool, None] = None,
        baseline_rule: Optional[BaselineRule] = None,
        offset_rule: Optional[OffsetRule] = None,
        **kwargs: Any,
    ):
        if "missing_fifth_monday" in kwargs:
            missing_policy = kwargs.pop("missing_fifth_monday")
        if "missing_fifth_monday_policy" in kwargs:
            missing_policy = kwargs.pop("missing_fifth_monday_policy")
        if "fifth_monday_policy" in kwargs:
            missing_policy = kwargs.pop("fifth_monday_policy")
        if "fifth_monday" in kwargs:
            missing_policy = kwargs.pop("fifth_monday")
        if "fifth_monday_missing" in kwargs:
            missing_policy = kwargs.pop("fifth_monday_missing")
        if "no_fifth_monday" in kwargs:
            missing_policy = kwargs.pop("no_fifth_monday")
        if "missing" in kwargs:
            missing_policy = kwargs.pop("missing")
        if "on_missing" in kwargs:
            missing_policy = kwargs.pop("on_missing")
        if "when_missing" in kwargs:
            missing_policy = kwargs.pop("when_missing")
        if "fallback" in kwargs:
            missing_policy = kwargs.pop("fallback")
        if "use_last_monday" in kwargs:
            missing_policy = kwargs.pop("use_last_monday")
        if "fallback_to_last_monday" in kwargs:
            missing_policy = kwargs.pop("fallback_to_last_monday")
        if "day_zero" in kwargs:
            day_zero_policy = kwargs.pop("day_zero")
        if "zero_day" in kwargs:
            day_zero_policy = kwargs.pop("zero_day")
        if "count_day_zero" in kwargs:
            day_zero_policy = kwargs.pop("count_day_zero")
        if "include_day_zero" in kwargs:
            day_zero_policy = kwargs.pop("include_day_zero")
        if "holiday_is_day_zero" in kwargs:
            day_zero_policy = kwargs.pop("holiday_is_day_zero")
        if "holiday_counts_as_day_zero" in kwargs:
            day_zero_policy = kwargs.pop("holiday_counts_as_day_zero")
        if "count_holiday_as_day_zero" in kwargs:
            day_zero_policy = kwargs.pop("count_holiday_as_day_zero")
        if "count_baseline_as_day_zero" in kwargs:
            day_zero_policy = kwargs.pop("count_baseline_as_day_zero")
        if "baseline_is_day_zero" in kwargs:
            day_zero_policy = kwargs.pop("baseline_is_day_zero")
        if "holiday_policy" in kwargs:
            day_zero_policy = kwargs.pop("holiday_policy")
        if "baseline_holiday_policy" in kwargs:
            day_zero_policy = kwargs.pop("baseline_holiday_policy")
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(
                f"unexpected SamplingRule argument(s): {unexpected}"
            )
        if baseline_rule is None:
            if missing_policy is None:
                missing_policy = FifthMondayPolicy.SKIP
            baseline_rule = BaselineRule(missing_policy=missing_policy)
        if offset_rule is None:
            if day_zero_policy is None:
                day_zero_policy = DayZeroPolicy.INCLUDE
            offset_rule = OffsetRule(
                business_days=offset,
                day_zero_policy=day_zero_policy,
            )
        if not isinstance(baseline_rule, BaselineRule):
            raise TypeError("baseline_rule must be a BaselineRule")
        if not isinstance(offset_rule, OffsetRule):
            raise TypeError("offset_rule must be an OffsetRule")
        object.__setattr__(self, "baseline_rule", baseline_rule)
        object.__setattr__(self, "offset_rule", offset_rule)

    @property
    def offset(self) -> int:
        return self.offset_rule.business_days

    @property
    def business_days(self) -> int:
        return self.offset_rule.business_days

    @property
    def missing_policy(self) -> FifthMondayPolicy:
        return self.baseline_rule.missing_policy

    @property
    def fifth_monday_policy(self) -> FifthMondayPolicy:
        return self.baseline_rule.missing_policy

    @property
    def missing_fifth_monday(self) -> FifthMondayPolicy:
        return self.baseline_rule.missing_policy

    @property
    def day_zero_policy(self) -> DayZeroPolicy:
        return self.offset_rule.day_zero_policy

    @property
    def day_zero(self) -> bool:
        """Whether a non-business baseline is counted as day zero."""
        return self.offset_rule.count_day_zero

    @property
    def count_day_zero(self) -> bool:
        return self.offset_rule.count_day_zero

    @property
    def fifth_monday(self) -> int:
        return self.baseline_rule.ordinal

    @property
    def fifth_monday_base(self) -> str:
        return self.baseline_rule.missing_policy.value

    @property
    def baseline(self) -> BaselineRule:
        """The baseline (fifth-Monday) step."""
        return self.baseline_rule

    @property
    def base_rule(self) -> BaselineRule:
        return self.baseline_rule

    @property
    def offset_step(self) -> OffsetRule:
        """The business-day offset step."""
        return self.offset_rule

    @property
    def steps(self) -> Tuple[BaselineRule, OffsetRule]:
        """The two rule steps, in evaluation order."""
        return self.baseline_rule, self.offset_rule

    @property
    def signature(self) -> str:
        return (
            f"fifth-monday/{self.missing_policy.value}/"
            f"offset-{self.offset}/{self.day_zero_policy.value}"
        )

    def as_dict(self) -> Dict[str, Any]:
        # The flat keys are convenient for callers storing simple records;
        # the nested keys show the two rule steps explicitly.
        return {
            "offset": self.offset,
            "missing_policy": self.missing_policy.value,
            "day_zero_policy": self.day_zero_policy.value,
            "count_day_zero": self.count_day_zero,
            "baseline": self.baseline_rule.as_dict(),
            "offset_rule": self.offset_rule.as_dict(),
        }

    def describe(self) -> str:
        return (
            f"{self.baseline_rule.describe()} "
            f"{self.offset_rule.describe()}"
        )

    def explain(self) -> str:
        return self.describe()

    def inspect(self) -> Dict[str, Any]:
        """Return the two steps as separate, readable descriptions."""
        return {
            "baseline": {
                **self.baseline_rule.as_dict(),
                "description": self.baseline_rule.describe(),
            },
            "offset": {
                **self.offset_rule.as_dict(),
                "description": self.offset_rule.describe(),
            },
        }


MonthlyFifthMondayRule = SamplingRule


@dataclass(frozen=True)
class SamplingTask:
    """One evaluated month of a sampling plan.

    A record is produced for every month, including rejected months.  Use
    :attr:`date` or :meth:`is_valid` to distinguish an actual task from an
    explanatory record.
    """

    contract_id: Optional[str]
    year: int
    month: int
    baseline: Optional[date]
    baseline_kind: Optional[str]
    target_date: Optional[date]
    status: SamplingStatus
    reason: str
    window_start: date
    window_end: date
    rule: SamplingRule
    used_fallback: bool = False
    rolled_over_month: bool = False
    window_excluded: bool = False
    baseline_is_working_day: Optional[bool] = None
    baseline_is_holiday: Optional[bool] = None
    baseline_holiday_label: Optional[str] = None
    day_zero_date: Optional[date] = None
    day_zero_was_baseline: Optional[bool] = None

    @property
    def date(self) -> Optional[date]:
        """The scheduled sampling date, or ``None`` for a rejected month."""
        if self.status in (
            SamplingStatus.OK, SamplingStatus.ROLLED_OVER_MONTH
        ):
            return self.target_date
        return None

    @property
    def scheduled_date(self) -> Optional[date]:
        return self.date

    @property
    def task_date(self) -> Optional[date]:
        return self.date

    @property
    def scheduled(self) -> Optional[date]:
        return self.date

    @property
    def target(self) -> Optional[date]:
        """The computed target, even when the month is excluded."""
        return self.target_date

    @property
    def base_date(self) -> Optional[date]:
        return self.baseline

    @property
    def adjusted_date(self) -> Optional[date]:
        return self.target_date

    @property
    def status_code(self) -> str:
        return self.status.value

    @property
    def reason_code(self) -> str:
        return self.status.value

    @property
    def status_label(self) -> str:
        return self.status.value

    @property
    def baseline_on_holiday(self) -> bool:
        return bool(self.baseline_is_holiday)

    @property
    def baseline_exists(self) -> bool:
        return self.baseline is not None

    @property
    def has_baseline(self) -> bool:
        return self.baseline is not None

    @property
    def skipped(self) -> bool:
        return self.baseline_missing

    @property
    def rollover(self) -> bool:
        return self.rolled_over_month

    @property
    def window(self) -> Tuple[date, date]:
        return self.window_start, self.window_end

    @property
    def skip_reason(self) -> Optional[str]:
        return self.reason if self.baseline_missing else None

    @property
    def exclusion_reason(self) -> Optional[str]:
        return self.reason if self.window_excluded else None

    @property
    def is_valid(self) -> bool:
        return self.date is not None

    @property
    def valid(self) -> bool:
        return self.is_valid

    @property
    def accepted(self) -> bool:
        return self.is_valid

    @property
    def rejected(self) -> bool:
        return not self.is_valid

    @property
    def baseline_missing(self) -> bool:
        return self.status is SamplingStatus.BASELINE_MISSING

    @property
    def crosses_month(self) -> bool:
        return self.rolled_over_month

    @property
    def excluded(self) -> bool:
        return self.window_excluded

    @property
    def statuses(self) -> Tuple[SamplingStatus, ...]:
        """All applicable outcomes, most important first."""
        statuses: List[SamplingStatus] = []
        if self.baseline_missing:
            statuses.append(SamplingStatus.BASELINE_MISSING)
        if self.window_excluded:
            statuses.append(SamplingStatus.WINDOW_EXCLUDED)
        if self.rolled_over_month:
            statuses.append(SamplingStatus.ROLLED_OVER_MONTH)
        if not statuses:
            statuses.append(SamplingStatus.OK)
        return tuple(statuses)

    @property
    def task_id(self) -> str:
        """A stable task identity that does not depend on the query range."""
        contract = self.contract_id if self.contract_id is not None else "*"
        baseline = self.baseline.isoformat() if self.baseline else "-"
        target = self.target_date.isoformat() if self.target_date else "-"
        return "|".join((
            str(contract),
            f"{self.year:04d}-{self.month:02d}",
            self.baseline_kind or "none",
            baseline,
            target,
            self.status.value,
        ))

    @property
    def identity(self) -> str:
        return self.task_id

    @property
    def id(self) -> str:
        return self.task_id

    @property
    def uid(self) -> str:
        return self.task_id

    @property
    def fingerprint(self) -> str:
        """A compact digest of :attr:`task_id`."""
        return hashlib.sha1(
            self.task_id.encode("utf-8")
        ).hexdigest()[:20]

    @property
    def period(self) -> Tuple[int, int]:
        return self.year, self.month

    @property
    def period_key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "contract_id": self.contract_id,
            "year": self.year,
            "month": self.month,
            "baseline": self.baseline,
            "baseline_kind": self.baseline_kind,
            "baseline_is_working_day": self.baseline_is_working_day,
            "baseline_is_holiday": self.baseline_is_holiday,
            "baseline_holiday_label": self.baseline_holiday_label,
            "target_date": self.target_date,
            "date": self.date,
            "status": self.status.value,
            "statuses": [item.value for item in self.statuses],
            "reason": self.reason,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "used_fallback": self.used_fallback,
            "rolled_over_month": self.rolled_over_month,
            "window_excluded": self.window_excluded,
            "day_zero_date": self.day_zero_date,
            "day_zero_was_baseline": self.day_zero_was_baseline,
            "rule": self.rule.as_dict(),
        }

    def explain(self) -> str:
        """Return a one-line, human-readable explanation of the outcome."""
        if self.status is SamplingStatus.BASELINE_MISSING:
            return f"{self.period_key}: {self.reason}"
        if self.status is SamplingStatus.WINDOW_EXCLUDED:
            return f"{self.period_key}: {self.reason}"
        if self.status is SamplingStatus.ROLLED_OVER_MONTH:
            return (
                f"{self.period_key}: baseline {self.baseline} + "
                f"{self.rule.offset} business day(s) -> {self.date}; "
                "the task crossed into the next month"
            )
        return (
            f"{self.period_key}: baseline {self.baseline} + "
            f"{self.rule.offset} business day(s) -> {self.date}"
        )


class SamplingPlan:
    """Evaluate a monthly fifth-Monday sampling rule.

    Parameters
    ----------
    calendar:
        A Workalendar calendar instance or class used for working-day
        calculations.
    rule:
        A :class:`SamplingRule`.  Rule keyword arguments may be passed
        directly to the plan as a convenience.
    contract_id:
        Optional identifier included in every task identity.
    excluded_windows / excluded_dates / excluded_months:
        Explicit exclusions.  A month with no available business day in its
        window is reported as ``WINDOW_EXCLUDED``.
    active_from / active_until:
        Optional contract period.  Months outside it are excluded.
    """

    def __init__(
        self,
        calendar: Any = None,
        rule: Optional[SamplingRule] = None,
        contract_id: Optional[str] = None,
        excluded_windows: Optional[Iterable[Any]] = None,
        excluded_dates: Optional[Iterable[Any]] = None,
        excluded_months: Optional[Iterable[Any]] = None,
        active_from: Any = None,
        active_until: Any = None,
        extra_working_days: Optional[Iterable[Any]] = None,
        extra_holidays: Optional[Iterable[Any]] = None,
        **kwargs: Any,
    ):
        # Accept SamplingPlan(rule) as well as SamplingPlan(calendar).
        if calendar is not None and not self._is_calendar(calendar):
            if isinstance(calendar, SamplingRule) and rule is None:
                rule, calendar = calendar, None
            else:
                raise TypeError(
                    "calendar must be a workalendar Calendar instance or "
                    f"class, got {calendar!r}"
                )
        if rule is not None and self._is_calendar(rule):
            calendar, rule = rule, calendar
        if "windows" in kwargs:
            excluded_windows = kwargs.pop("windows")
        if "blackout_windows" in kwargs:
            excluded_windows = kwargs.pop("blackout_windows")
        if "closed_windows" in kwargs:
            excluded_windows = kwargs.pop("closed_windows")
        if "excluded_periods" in kwargs:
            excluded_windows = kwargs.pop("excluded_periods")
        if "blackout_dates" in kwargs:
            excluded_dates = kwargs.pop("blackout_dates")
        if "blackout_months" in kwargs:
            excluded_months = kwargs.pop("blackout_months")
        if rule is None:
            rule = SamplingRule(**kwargs)
        elif kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(
                f"unexpected SamplingPlan argument(s): {unexpected}"
            )
        if not isinstance(rule, SamplingRule):
            raise TypeError(f"rule must be a SamplingRule, got {rule!r}")

        if calendar is None:
            calendar = WesternCalendar()
        if isinstance(calendar, type):
            calendar = calendar()
        if not self._is_calendar(calendar):
            raise TypeError(
                "calendar must be a workalendar Calendar instance or class"
            )

        self.calendar = calendar
        self.rule = rule
        self.contract_id = contract_id
        self.excluded_windows = _as_window_list(excluded_windows)
        self.excluded_dates = frozenset(
            _as_date(day) for day in _as_date_tuple(
                excluded_dates, "excluded_dates"
            )
        )
        self.excluded_months = frozenset(
            _as_month_list(excluded_months)
        )
        self.active_from = (
            _as_date(active_from) if active_from is not None else None
        )
        self.active_until = (
            _as_date(active_until) if active_until is not None else None
        )
        self.extra_working_days = tuple(
            _as_date(day) for day in _as_date_tuple(
                extra_working_days, "extra_working_days"
            )
        )
        self.extra_holidays = tuple(
            _as_date(day) for day in _as_date_tuple(
                extra_holidays, "extra_holidays"
            )
        )

    @staticmethod
    def _is_calendar(value: Any) -> bool:
        required = ("is_working_day", "add_working_days")
        return all(hasattr(value, name) for name in required)

    @property
    def missing_policy(self) -> FifthMondayPolicy:
        return self.rule.missing_policy

    @property
    def day_zero_policy(self) -> DayZeroPolicy:
        return self.rule.day_zero_policy

    @property
    def offset(self) -> int:
        return self.rule.offset

    def _is_working_day(self, day: date) -> bool:
        return self.calendar.is_working_day(
            day,
            extra_working_days=self.extra_working_days or None,
            extra_holidays=self.extra_holidays or None,
        )

    def _add_working_days(self, day: date, delta: int) -> date:
        return self.calendar.add_working_days(
            day,
            delta,
            extra_working_days=self.extra_working_days or None,
            extra_holidays=self.extra_holidays or None,
        )

    def _month_bounds(self, year: int, month: int) -> Tuple[date, date]:
        return (
            date(year, month, 1),
            date(year, month, monthrange(year, month)[1]),
        )

    def _is_explicitly_excluded(self, day: date) -> bool:
        if day in self.excluded_dates:
            return True
        return any(window.contains(day) for window in self.excluded_windows)

    def _outside_active_period(self, day: date) -> bool:
        if self.active_from is not None and day < self.active_from:
            return True
        if self.active_until is not None and day > self.active_until:
            return True
        return False

    def window_for_month(
        self, year: Any, month: Any = None
    ) -> Tuple[date, date]:
        """Return the effective (possibly clipped) window for a month."""
        year, month = _parse_month(year, month)
        start, end = self._month_bounds(year, month)
        if self.active_from is not None:
            start = max(start, self.active_from)
        if self.active_until is not None:
            end = min(end, self.active_until)
        return start, end

    def window_is_excluded(self, year: Any, month: Any = None) -> bool:
        """Whether the whole monthly window is unavailable."""
        year, month = _parse_month(year, month)
        if (year, month) in self.excluded_months:
            return True
        start, end = self.window_for_month(year, month)
        if start > end:
            return True
        day = start
        while day <= end:
            if (not self._is_explicitly_excluded(day)
                    and not self._outside_active_period(day)
                    and self._is_working_day(day)):
                return False
            day += timedelta(days=1)
        return True

    def _next_business_day(self, day: date) -> date:
        while not self._is_working_day(day):
            day += timedelta(days=1)
        return day

    def _day_zero(self, baseline: date) -> Tuple[date, bool]:
        """Return ``(day_zero_date, used_baseline)`` for the offset step."""
        if (self.rule.day_zero_policy is DayZeroPolicy.EXCLUDE
                and not self._is_working_day(baseline)):
            return self._next_business_day(baseline), False
        return baseline, True

    def _target_after(self, baseline: date) -> date:
        """Compute the offset, honouring the day-zero policy."""
        day_zero, used_baseline = self._day_zero(baseline)
        if used_baseline:
            return self._add_working_days(baseline, self.rule.offset)
        return self._add_working_days(day_zero, self.rule.offset)

    def _baseline_details(self, baseline: Optional[date]):
        if baseline is None:
            return None, None, None
        is_working_day = self._is_working_day(baseline)
        is_holiday = False
        label = None
        is_holiday_method = getattr(
            self.calendar, "is_holiday", None
        )
        if callable(is_holiday_method):
            try:
                is_holiday = bool(is_holiday_method(baseline))
            except (NotImplementedError, AttributeError):
                is_holiday = not is_working_day
        else:
            is_holiday = not is_working_day
        if is_holiday:
            label_method = getattr(
                self.calendar, "get_holiday_label", None
            )
            if callable(label_method):
                try:
                    label = label_method(baseline)
                except (NotImplementedError, AttributeError):
                    label = None
        return is_working_day, is_holiday, label

    def evaluate_month(self, year: Any, month: Any = None) -> SamplingTask:
        """Evaluate one month and return its task record.

        The record is returned even when the month is skipped or excluded;
        inspect :attr:`SamplingTask.status` and :attr:`SamplingTask.date`.
        """
        year, month = _parse_month(year, month)
        start, end = self.window_for_month(year, month)
        window_excluded = self.window_is_excluded(year, month)

        resolution = self.rule.baseline_rule.resolve(
            self.calendar, year, month
        )
        if resolution.missing:
            return SamplingTask(
                contract_id=self.contract_id,
                year=year,
                month=month,
                baseline=None,
                baseline_kind=None,
                target_date=None,
                status=SamplingStatus.BASELINE_MISSING,
                reason=resolution.reason,
                window_start=start,
                window_end=end,
                rule=self.rule,
                window_excluded=window_excluded,
            )

        baseline = resolution.baseline
        target = self._target_after(baseline)
        day_zero, used_baseline = self._day_zero(baseline)
        is_working_day, is_holiday, holiday_label = (
            self._baseline_details(baseline)
        )
        rolled = (target.year, target.month) != (year, month)
        target_excluded = (
            self._is_explicitly_excluded(target)
            or self._outside_active_period(target)
            or (target.year, target.month) in self.excluded_months
        )

        if window_excluded or target_excluded:
            if window_excluded:
                reason = "every day in the sampling window is excluded"
            else:
                reason = f"target date {target} is excluded"
            return SamplingTask(
                contract_id=self.contract_id,
                year=year,
                month=month,
                baseline=baseline,
                baseline_kind=resolution.kind,
                target_date=target,
                status=SamplingStatus.WINDOW_EXCLUDED,
                reason=reason,
                window_start=start,
                window_end=end,
                rule=self.rule,
                used_fallback=resolution.used_fallback,
                rolled_over_month=rolled,
                window_excluded=True,
                baseline_is_working_day=is_working_day,
                baseline_is_holiday=is_holiday,
                baseline_holiday_label=holiday_label,
                day_zero_date=day_zero,
                day_zero_was_baseline=used_baseline,
            )

        if rolled:
            reason = (
                f"the offset crossed from {year:04d}-{month:02d} into "
                f"{target.year:04d}-{target.month:02d}"
            )
            status = SamplingStatus.ROLLED_OVER_MONTH
        else:
            reason = "the offset stays inside the sampling month"
            status = SamplingStatus.OK

        return SamplingTask(
            contract_id=self.contract_id,
            year=year,
            month=month,
            baseline=baseline,
            baseline_kind=resolution.kind,
            target_date=target,
            status=status,
            reason=reason,
            window_start=start,
            window_end=end,
            rule=self.rule,
            used_fallback=resolution.used_fallback,
            rolled_over_month=rolled,
            window_excluded=False,
            baseline_is_working_day=is_working_day,
            baseline_is_holiday=is_holiday,
            baseline_holiday_label=holiday_label,
            day_zero_date=day_zero,
            day_zero_was_baseline=used_baseline,
        )

    # Inspection-friendly aliases.
    month_result = evaluate_month
    inspect_month = evaluate_month
    plan_month = evaluate_month
    rule_for_month = evaluate_month

    def explain_month(self, year: Any, month: Any = None) -> str:
        return self.evaluate_month(year, month).explain()

    def records_for_month(
        self, year: Any, month: Any = None
    ) -> List[SamplingTask]:
        return [self.evaluate_month(year, month)]

    def evaluate_quarter(
        self, year: Any, quarter: Any = None
    ) -> List[SamplingTask]:
        """Evaluate all three months, including rejected records."""
        year, quarter = _normalize_quarter(year, quarter)
        return [
            self.evaluate_month(year, month)
            for month in _quarter_months(year, quarter)
        ]

    inspect_quarter = evaluate_quarter
    quarter_results = evaluate_quarter
    plan_quarter = evaluate_quarter

    def tasks_for_month(
        self, year: Any, month: Any = None
    ) -> List[SamplingTask]:
        """Return accepted tasks for one month (zero or one)."""
        result = self.evaluate_month(year, month)
        return [result] if result.is_valid else []

    for_month = tasks_for_month
    month_tasks = tasks_for_month
    monthly_tasks = tasks_for_month
    query_month = tasks_for_month

    def tasks_for_quarter(
        self, year: Any, quarter: Any = None
    ) -> List[SamplingTask]:
        """Return accepted tasks for a quarter, date-sorted.

        This is exactly equivalent to calling :meth:`tasks_for_month` for
        each month in the quarter and merging the results.
        """
        return merge_tasks(
            self.tasks_for_month(year, month)
            for month in _quarter_months(year, quarter)
        )

    for_quarter = tasks_for_quarter
    quarter_tasks = tasks_for_quarter
    quarterly_tasks = tasks_for_quarter
    query_quarter = tasks_for_quarter

    def tasks_in_quarter(
        self, quarter: Any, year: Any = None
    ) -> List[SamplingTask]:
        """Alias accepting ``(quarter,)`` or ``(quarter, year)`` order."""
        if year is not None:
            return self.tasks_for_quarter(year, quarter)
        return self.tasks_for_quarter(quarter)

    quarter_tasks_in = tasks_in_quarter

    def tasks(
        self,
        year: Any = None,
        month: Any = None,
        quarter: Any = None,
        start: Any = None,
        end: Any = None,
    ) -> List[SamplingTask]:
        """Dispatch to the appropriate query.

        ``tasks(year)``, ``tasks(year, month)``, ``tasks(year, quarter=2)``
        and ``tasks(start=..., end=...)`` are all supported.
        """
        if start is not None or end is not None:
            if start is None or end is None:
                raise ValueError("both start and end are required")
            return self.tasks_between(start, end)
        if quarter is not None:
            return self.tasks_for_quarter(year, quarter)
        if year is None:
            raise ValueError("year or a date range is required")
        if month is None:
            return self.tasks_for_year(year)
        return self.tasks_for_month(year, month)

    get_tasks = tasks
    query_tasks = tasks

    def tasks_between(self, start: Any, end: Any) -> List[SamplingTask]:
        """Return accepted tasks for all months intersecting a range."""
        start = _as_date(start)
        end = _as_date(end)
        if start > end:
            start, end = end, start
        tasks: List[SamplingTask] = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            tasks.extend(self.tasks_for_month(year, month))
            month += 1
            if month == 13:
                year, month = year + 1, 1
        return merge_tasks(tasks)

    query = tasks_between
    tasks_for_period = tasks_between

    def tasks_for_year(self, year: int) -> List[SamplingTask]:
        return merge_tasks(
            self.tasks_for_month(year, month)
            for month in range(1, 13)
        )

    year_tasks = tasks_for_year

    def records_for_year(self, year: int) -> List[SamplingTask]:
        """Evaluate all twelve months, including rejected records."""
        return [
            self.evaluate_month(year, month)
            for month in range(1, 13)
        ]

    def iter_tasks(self, start: Any, end: Any) -> Iterator[SamplingTask]:
        yield from self.tasks_between(start, end)

    def merge(self, tasks: Iterable[SamplingTask]) -> List[SamplingTask]:
        return merge_tasks(tasks)

    def merge_records(
        self, records: Iterable[SamplingTask]
    ) -> List[SamplingTask]:
        """Merge evaluated records, including rejected ones, without sorting.

        Unlike :func:`merge_tasks`, rejected records are preserved.  This is
        convenient for auditing a quarter or a year.
        """
        merged: List[SamplingTask] = []
        seen = set()
        for record in _flatten_tasks([records]):
            if record.task_id in seen:
                continue
            seen.add(record.task_id)
            merged.append(record)
        return merged

    @staticmethod
    def merge_tasks(tasks: Iterable[SamplingTask]) -> List[SamplingTask]:
        return merge_tasks(tasks)


MonthlySamplingPlan = SamplingPlan
ContractSamplingPlan = SamplingPlan


def _flatten_tasks(groups: Iterable[Any]) -> Iterator[SamplingTask]:
    for group in groups:
        if isinstance(group, SamplingTask):
            yield group
        else:
            yield from _flatten_tasks(group)


def merge_tasks(
    *task_groups: Iterable[SamplingTask]
) -> List[SamplingTask]:
    """Flatten, de-duplicate, and date-sort task iterables.

    De-duplication uses :attr:`SamplingTask.task_id`, which does not contain
    query-range information.  Consequently a quarterly query and the merge of
    the corresponding monthly queries produce identical lists.
    """
    tasks: List[SamplingTask] = []
    seen = set()
    for task in _flatten_tasks(task_groups):
        if task.task_id in seen:
            continue
        seen.add(task.task_id)
        tasks.append(task)
    tasks.sort(key=lambda task: (task.date or date.max, task.task_id))
    return tasks


merge_sampling_tasks = merge_tasks


class SamplingSchedule:
    """A collection of :class:`SamplingPlan` objects (one per contract)."""

    def __init__(self, plans: Iterable[SamplingPlan] = ()):
        self.plans = list(plans)

    def add(self, plan: SamplingPlan) -> "SamplingSchedule":
        self.plans.append(plan)
        return self

    def tasks_for_month(
        self, year: Any, month: Any = None
    ) -> List[SamplingTask]:
        return merge_tasks(
            plan.tasks_for_month(year, month) for plan in self.plans
        )

    for_month = tasks_for_month
    monthly_tasks = tasks_for_month

    def tasks_for_quarter(
        self, year: Any, quarter: Any = None
    ) -> List[SamplingTask]:
        return merge_tasks(
            plan.tasks_for_quarter(year, quarter) for plan in self.plans
        )

    for_quarter = tasks_for_quarter
    quarterly_tasks = tasks_for_quarter

    def evaluate_month(
        self, year: Any, month: Any = None
    ) -> List[SamplingTask]:
        return [
            plan.evaluate_month(year, month) for plan in self.plans
        ]

    def evaluate_quarter(
        self, year: Any, quarter: Any = None
    ) -> List[SamplingTask]:
        records: List[SamplingTask] = []
        for plan in self.plans:
            records.extend(plan.evaluate_quarter(year, quarter))
        return records

    def tasks_between(self, start: Any, end: Any) -> List[SamplingTask]:
        return merge_tasks(
            plan.tasks_between(start, end) for plan in self.plans
        )

    query = tasks_between


SamplingBook = SamplingSchedule
Schedule = SamplingSchedule


def monthly_tasks(
    calendar: Any, year: Any, month: Any = None, **kwargs: Any
) -> List[SamplingTask]:
    """Convenience wrapper for a one-month :class:`SamplingPlan` query."""
    return SamplingPlan(calendar, **kwargs).tasks_for_month(year, month)


def quarterly_tasks(
    calendar: Any, year: Any, quarter: Any = None, **kwargs: Any
) -> List[SamplingTask]:
    """Convenience wrapper for a quarterly :class:`SamplingPlan` query."""
    return SamplingPlan(calendar, **kwargs).tasks_for_quarter(year, quarter)


tasks_for_month = monthly_tasks
tasks_for_quarter = quarterly_tasks


__all__ = [
    "BaselineResolution",
    "BaselineRule",
    "BlackoutWindow",
    "ContractSamplingPlan",
    "DayZeroPolicy",
    "FifthMondayPolicy",
    "MonthlyFifthMondayRule",
    "MonthlySamplingPlan",
    "OffsetRule",
    "SamplingBook",
    "SamplingPlan",
    "SamplingRule",
    "SamplingSchedule",
    "SamplingStatus",
    "SamplingTask",
    "SamplingWindow",
    "Schedule",
    "TaskStatus",
    "merge_sampling_tasks",
    "merge_tasks",
    "monthly_tasks",
    "quarterly_tasks",
    "tasks_for_month",
    "tasks_for_quarter",
]
