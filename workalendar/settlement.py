"""
Incremental settlement-date audit after a calendar correction.

When a holiday calendar is corrected, re-running the whole settlement history
is expensive and noisy: most settlement dates do not actually move. The tools
in this module store, for each business transaction, the inputs of its last
settlement run (anchor date, offset rule and the calendar version that was
used), compare the two calendar versions and report only the transactions
whose settlement date really changes, together with the reason why.

The incremental output is strictly equivalent to running the offset rules
against each of the two full calendars. :func:`verify_audit` cross-checks the
incremental result against two full recomputations, record by record.

Typical use::

    from workalendar.settlement import (
        SettlementRecord, AddWorkingDays, SettlementAudit,
    )

    records = [
        SettlementRecord("TX-1", date(2024, 3, 10), AddWorkingDays(5), "v1"),
    ]
    audit = SettlementAudit.from_calendars(
        old_calendar, new_calendar, records, "v1", "2")
    result = audit.audit(records)
    for change in result.changed:
        print(change.transaction_id, change.old_settlement,
              change.new_settlement, change.reasons)
"""
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import MappingProxyType

from .core import cleaned_date
from .exceptions import CalendarError

__all__ = [
    "OffsetRule", "AddWorkingDays", "MonthEndNthWorkingDay",
    "FollowingWorkingDay", "rule_from_dict",
    "CalendarSnapshot", "CalendarDiff",
    "SettlementRecord", "ChangeReason", "SettlementChange", "AuditResult",
    "SettlementAudit", "full_recompute", "verify_audit",
]

WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)

# A safety margin, in days, used when locating the calendar changes that
# explain a settlement move. It only affects the human-readable reason: the
# settlement dates themselves always come from the exact offset rules.
REASON_MARGIN = 14


# --- Offset rules ---------------------------------------------------------

class OffsetRule:
    """Base class for the settlement offset rules.

    A rule is a pure function of the anchor date and the calendar. Applying
    the very same rule with the same anchor on two calendar versions is
    therefore directly comparable.
    """

    name = "base"

    def apply(self, anchor, calendar):
        raise NotImplementedError

    def to_dict(self):
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data):
        raise NotImplementedError


class AddWorkingDays(OffsetRule):
    """Settle ``delta`` working days after (or before) the anchor."""

    name = "add_working_days"

    def __init__(self, delta):
        self.delta = int(delta)

    def apply(self, anchor, calendar):
        return calendar.add_working_days(anchor, self.delta)

    def to_dict(self):
        return {"type": self.name, "delta": self.delta}

    @classmethod
    def from_dict(cls, data):
        return cls(data["delta"])

    def __eq__(self, other):
        return isinstance(other, AddWorkingDays) and other.delta == self.delta

    def __repr__(self):
        return f"AddWorkingDays(delta={self.delta})"


class MonthEndNthWorkingDay(OffsetRule):
    """Settle on the n-th working day counting backwards from month end.

    ``nth=1`` is the last working day of the anchor's month, ``nth=3`` the
    third one from the end, and so on.
    """

    name = "month_end_nth_working_day"

    def __init__(self, nth):
        nth = int(nth)
        if nth < 1:
            raise CalendarError("nth must be a positive integer")
        self.nth = nth

    def apply(self, anchor, calendar):
        anchor = cleaned_date(anchor)
        last_day = date(
            anchor.year, anchor.month,
            monthrange(anchor.year, anchor.month)[1],
        )
        # Month end may itself be a weekend day or a holiday: walk back to
        # the last working day of the month first, then count backwards.
        while not calendar.is_working_day(last_day):
            last_day -= timedelta(days=1)
        return calendar.sub_working_days(last_day, self.nth - 1)

    def to_dict(self):
        return {"type": self.name, "nth": self.nth}

    @classmethod
    def from_dict(cls, data):
        return cls(data["nth"])

    def __eq__(self, other):
        return (
            isinstance(other, MonthEndNthWorkingDay)
            and other.nth == self.nth
        )

    def __repr__(self):
        return f"MonthEndNthWorkingDay(nth={self.nth})"


class FollowingWorkingDay(OffsetRule):
    """Settle on the anchor when it works, else on the next working day."""

    name = "following_working_day"

    def apply(self, anchor, calendar):
        anchor = cleaned_date(anchor)
        if calendar.is_working_day(anchor):
            return anchor
        return calendar.add_working_days(anchor, 1)

    def to_dict(self):
        return {"type": self.name}

    @classmethod
    def from_dict(cls, data):
        return cls()

    def __eq__(self, other):
        return isinstance(other, FollowingWorkingDay)

    def __repr__(self):
        return "FollowingWorkingDay()"


RULE_TYPES = MappingProxyType({
    AddWorkingDays.name: AddWorkingDays,
    MonthEndNthWorkingDay.name: MonthEndNthWorkingDay,
    FollowingWorkingDay.name: FollowingWorkingDay,
})


def rule_from_dict(data):
    """Rebuild an :class:`OffsetRule` from its serialized form."""
    data = dict(data)
    rule_type = data.pop("type")
    rule_class = RULE_TYPES.get(rule_type)
    if rule_class is None:
        raise CalendarError(f"Unknown offset rule type: {rule_type!r}")
    return rule_class.from_dict(data)


# --- Calendar snapshot and diff ------------------------------------------

@dataclass(frozen=True)
class CalendarSnapshot:
    """An immutable, hashable view of a calendar over a range of years.

    It captures everything an offset rule may rely on -- the weekend
    weekdays and the holiday dates with their labels -- so a past calendar
    version can be kept and re-used after the live calendar has been
    corrected.

    Querying a date whose year is not covered raises a :class:`CalendarError`
    rather than silently treating the day as a working day.
    """

    version: str
    weekend_days: frozenset
    holidays: frozenset
    # Sorted tuple of ``(date, tuple_of_labels)``.
    labels: tuple = field(default_factory=tuple)
    # Years whose holidays were fully captured.
    years: frozenset = frozenset()

    @classmethod
    def from_calendar(cls, calendar, years, version):
        """Build a snapshot of ``calendar`` for the given ``years``."""
        years = frozenset(years)
        holiday_dates = set()
        labels = {}
        for year in sorted(years):
            for day, label in calendar.holidays(year):
                holiday_dates.add(day)
                labels.setdefault(day, []).append(label)
        return cls(
            version=version,
            weekend_days=frozenset(calendar.get_weekend_days()),
            holidays=frozenset(holiday_dates),
            labels=tuple(
                (day, tuple(label_list))
                for day, label_list in sorted(labels.items())
            ),
            years=years,
        )

    def label_of(self, day):
        """Return the holiday labels recorded on ``day`` (possibly empty)."""
        for holiday, label_list in self.labels:
            if holiday == day:
                return label_list
        return ()

    def is_working_day(self, day):
        day = cleaned_date(day)
        if day.year not in self.years:
            raise CalendarError(
                f"Calendar snapshot {self.version!r} does not cover year "
                f"{day.year}; widen the snapshotted year range"
            )
        if day.weekday() in self.weekend_days:
            return False
        return day not in self.holidays

    def add_working_days(self, day, delta):
        """Mirror of :meth:`CoreCalendar.add_working_days`."""
        day = cleaned_date(day)
        remaining = abs(int(delta))
        step = timedelta(days=1 if delta >= 0 else -1)
        while remaining:
            day += step
            if self.is_working_day(day):
                remaining -= 1
        return day

    def sub_working_days(self, day, delta):
        """Mirror of :meth:`CoreCalendar.sub_working_days`."""
        return self.add_working_days(day, -abs(int(delta)))

    def to_dict(self):
        return {
            "version": self.version,
            "weekend_days": sorted(self.weekend_days),
            "holidays": sorted(day.isoformat() for day in self.holidays),
            "labels": [
                [day.isoformat(), list(label_list)]
                for day, label_list in self.labels
            ],
            "years": sorted(self.years),
        }

    @classmethod
    def from_dict(cls, data):
        labels = tuple(
            (date.fromisoformat(day), tuple(label_list))
            for day, label_list in data["labels"]
        )
        return cls(
            version=data["version"],
            weekend_days=frozenset(data["weekend_days"]),
            holidays=frozenset(
                date.fromisoformat(day) for day in data["holidays"]
            ),
            labels=labels,
            years=frozenset(data.get("years", ())),
        )


@dataclass(frozen=True)
class CalendarDiff:
    """The day-level differences between two calendar versions."""

    old_version: str
    new_version: str
    added: frozenset
    removed: frozenset
    old_weekend_days: frozenset
    new_weekend_days: frozenset

    @property
    def weekend_changed(self):
        return self.old_weekend_days != self.new_weekend_days

    @property
    def affected_dates(self):
        """All dates whose working/non-working status changed."""
        return self.added | self.removed

    @property
    def is_empty(self):
        return not self.added and not self.removed and not self.weekend_changed


# --- Records and audit results -------------------------------------------

@dataclass
class SettlementRecord:
    """A business transaction with the inputs of its last settlement run."""

    transaction_id: str
    anchor: date
    rule: OffsetRule
    calendar_version: str
    last_settlement: date = None

    def __post_init__(self):
        self.anchor = cleaned_date(self.anchor)
        if self.last_settlement is not None:
            self.last_settlement = cleaned_date(self.last_settlement)

    def to_dict(self):
        return {
            "transaction_id": self.transaction_id,
            "anchor": self.anchor.isoformat(),
            "rule": self.rule.to_dict(),
            "calendar_version": self.calendar_version,
            "last_settlement": (
                self.last_settlement.isoformat()
                if self.last_settlement is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            transaction_id=data["transaction_id"],
            anchor=date.fromisoformat(data["anchor"]),
            rule=rule_from_dict(data["rule"]),
            calendar_version=data["calendar_version"],
            last_settlement=(
                date.fromisoformat(data["last_settlement"])
                if data.get("last_settlement") else None
            ),
        )


@dataclass(frozen=True)
class ChangeReason:
    """A human-readable explanation of why a settlement date moved."""

    code: str
    message: str
    dates: tuple = ()
    labels: tuple = ()


@dataclass(frozen=True)
class SettlementChange:
    """A transaction whose settlement date differs between the two versions."""

    transaction_id: str
    anchor: date
    rule: OffsetRule
    old_settlement: date
    new_settlement: date
    reasons: tuple = ()

    @property
    def delta_days(self):
        return (self.new_settlement - self.old_settlement).days


@dataclass(frozen=True)
class AuditResult:
    """The full incremental audit output.

    ``changed`` only holds transactions whose settlement date really moved;
    ``unchanged`` holds the other transaction ids (including cases where two
    holidays swapped but cancelled each other out). ``all_settlements`` keeps
    every processed transaction with both dates so the output can be matched
    against two full recomputations.
    """

    old_version: str
    new_version: str
    changed: tuple = ()
    unchanged: tuple = ()
    unknown_version: tuple = ()
    all_settlements: MappingProxyType = None
    calendar_diff: CalendarDiff = None

    def to_dict(self):
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "changed": [
                {
                    "transaction_id": change.transaction_id,
                    "anchor": change.anchor.isoformat(),
                    "rule": change.rule.to_dict(),
                    "old_settlement": change.old_settlement.isoformat(),
                    "new_settlement": change.new_settlement.isoformat(),
                    "delta_days": change.delta_days,
                    "reasons": [
                        {
                            "code": reason.code,
                            "message": reason.message,
                            "dates": [day.isoformat() for day in reason.dates],
                            "labels": list(reason.labels),
                        }
                        for reason in change.reasons
                    ],
                }
                for change in self.changed
            ],
            "unchanged": list(self.unchanged),
            "unknown_version": list(self.unknown_version),
            "all_settlements": {
                transaction_id: [old_day.isoformat(), new_day.isoformat()]
                for transaction_id, (old_day, new_day)
                in self.all_settlements.items()
            },
        }


# --- The audit itself -----------------------------------------------------

class SettlementAudit:
    """Compare two calendar versions and derive the settlement impact."""

    def __init__(self, old_snapshot, new_snapshot):
        self.old = old_snapshot
        self.new = new_snapshot
        self._diff = self._build_diff()

    @staticmethod
    def _required_years(records):
        """Years that must be snapshotted for the rules to run exactly.

        A rule can walk into the previous or next year (year-end anchors,
        long offsets), so one year on each side is included.
        """
        years = set()
        for record in records:
            anchor = cleaned_date(record.anchor)
            years.update((anchor.year - 1, anchor.year, anchor.year + 1))
        return years

    @classmethod
    def from_calendars(cls, old_calendar, new_calendar, records,
                       old_version, new_version):
        """Snapshot both calendars over every year the records may touch."""
        years = cls._required_years(records)
        old_snapshot = CalendarSnapshot.from_calendar(
            old_calendar, years, old_version)
        new_snapshot = CalendarSnapshot.from_calendar(
            new_calendar, years, new_version)
        return cls(old_snapshot, new_snapshot)

    def _build_diff(self):
        return CalendarDiff(
            old_version=self.old.version,
            new_version=self.new.version,
            added=frozenset(self.new.holidays - self.old.holidays),
            removed=frozenset(self.old.holidays - self.new.holidays),
            old_weekend_days=self.old.weekend_days,
            new_weekend_days=self.new.weekend_days,
        )

    @property
    def calendar_diff(self):
        return self._diff

    @staticmethod
    def _rule_interval(record, old_settlement, new_settlement):
        """Conservative date interval in which the rule looks for workdays."""
        anchor = cleaned_date(record.anchor)
        rule = record.rule
        if isinstance(rule, AddWorkingDays):
            margin = timedelta(days=abs(rule.delta) + REASON_MARGIN)
            if rule.delta > 0:
                return anchor, max(old_settlement, new_settlement) + margin
            if rule.delta < 0:
                return min(old_settlement, new_settlement) - margin, anchor
            return anchor, anchor
        if isinstance(rule, MonthEndNthWorkingDay):
            first_day = anchor.replace(day=1)
            last_day = date(
                anchor.year, anchor.month,
                monthrange(anchor.year, anchor.month)[1],
            )
            margin = timedelta(days=rule.nth + REASON_MARGIN)
            return first_day - margin, last_day + margin
        if isinstance(rule, FollowingWorkingDay):
            return anchor, max(old_settlement, new_settlement) + \
                timedelta(days=REASON_MARGIN)
        return min(anchor, old_settlement, new_settlement), \
            max(anchor, old_settlement, new_settlement)

    def _build_reasons(self, record, old_settlement, new_settlement):
        start, end = self._rule_interval(
            record, old_settlement, new_settlement)
        reasons = []

        added = sorted(
            day for day in self._diff.added if start <= day <= end
        )
        if added:
            reasons.append(ChangeReason(
                code="holiday_added",
                message=(
                    "new non-working day(s) on "
                    + ", ".join(day.isoformat() for day in added)
                ),
                dates=tuple(added),
                labels=tuple(
                    label for day in added
                    for label in self.new.label_of(day)
                ),
            ))

        removed = sorted(
            day for day in self._diff.removed if start <= day <= end
        )
        if removed:
            reasons.append(ChangeReason(
                code="holiday_removed",
                message=(
                    "day(s) no longer on holiday on "
                    + ", ".join(day.isoformat() for day in removed)
                ),
                dates=tuple(removed),
                labels=tuple(
                    label for day in removed
                    for label in self.old.label_of(day)
                ),
            ))

        if self._diff.weekend_changed:
            old_names = sorted(
                WEEKDAY_NAMES[day] for day in self._diff.old_weekend_days
            )
            new_names = sorted(
                WEEKDAY_NAMES[day] for day in self._diff.new_weekend_days
            )
            reasons.append(ChangeReason(
                code="weekend_changed",
                message=(
                    f"weekend definition changed from {old_names} "
                    f"to {new_names}"
                ),
            ))

        if not reasons:
            reasons.append(ChangeReason(
                code="calendar_changed",
                message=(
                    "settlement moved due to a calendar change outside the "
                    f"scanned interval {start.isoformat()}.."
                    f"{end.isoformat()}"
                ),
            ))
        return tuple(reasons)

    def audit(self, records):
        """Run the incremental audit over ``records``."""
        changed = []
        unchanged = []
        unknown_version = []
        all_settlements = {}

        for record in records:
            anchor = cleaned_date(record.anchor)
            if record.calendar_version not in (
                    self.old.version, self.new.version):
                unknown_version.append(record.transaction_id)
                continue

            old_settlement = record.rule.apply(anchor, self.old)
            new_settlement = record.rule.apply(anchor, self.new)
            all_settlements[record.transaction_id] = (
                old_settlement, new_settlement)

            if old_settlement == new_settlement:
                unchanged.append(record.transaction_id)
                continue

            reasons = self._build_reasons(
                record, old_settlement, new_settlement)
            changed.append(SettlementChange(
                transaction_id=record.transaction_id,
                anchor=anchor,
                rule=record.rule,
                old_settlement=old_settlement,
                new_settlement=new_settlement,
                reasons=reasons,
            ))

        return AuditResult(
            old_version=self.old.version,
            new_version=self.new.version,
            changed=tuple(changed),
            unchanged=tuple(unchanged),
            unknown_version=tuple(unknown_version),
            all_settlements=MappingProxyType(dict(all_settlements)),
            calendar_diff=self._diff,
        )


# --- Full recomputation helpers ------------------------------------------

def full_recompute(records, calendar):
    """Recompute every settlement against one (full) calendar.

    Returns a ``{transaction_id: settlement_date}`` mapping. It is the
    reference implementation the incremental audit is checked against.
    """
    settlements = {}
    for record in records:
        anchor = cleaned_date(record.anchor)
        settlements[record.transaction_id] = record.rule.apply(
            anchor, calendar)
    return settlements


def verify_audit(result, records, old_calendar, new_calendar):
    """Cross-check an incremental result against two full recomputations.

    Returns a list of human-readable mismatch descriptions; an empty list
    means every record matches the full computation on both calendar
    versions, including the records reported as unchanged.
    """
    records_by_id = {
        record.transaction_id: record for record in records
    }
    expected_old = full_recompute(records, old_calendar)
    expected_new = full_recompute(records, new_calendar)
    errors = []

    for transaction_id, (old_day, new_day) in result.all_settlements.items():
        record = records_by_id[transaction_id]
        if expected_old[transaction_id] != old_day:
            errors.append(
                f"{transaction_id}: old settlement {old_day} differs from "
                f"full recomputation {expected_old[transaction_id]}"
            )
        if expected_new[transaction_id] != new_day:
            errors.append(
                f"{transaction_id}: new settlement {new_day} differs from "
                f"full recomputation {expected_new[transaction_id]}"
            )
        if record.last_settlement is not None \
                and record.last_settlement != old_day:
            errors.append(
                f"{transaction_id}: stored last settlement "
                f"{record.last_settlement} does not match the old calendar "
                f"recomputation {old_day}"
            )

    changed_ids = {change.transaction_id for change in result.changed}
    for transaction_id, (old_day, new_day) in result.all_settlements.items():
        actually_changed = old_day != new_day
        if actually_changed != (transaction_id in changed_ids):
            errors.append(
                f"{transaction_id}: changed/unchanged classification is "
                "inconsistent with the recomputed dates"
            )
    return errors
