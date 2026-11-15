# Incremental settlement audit

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [iCal Export](ical.md) / [Contributing](contributing.md)

## Purpose

When a holiday calendar is corrected, re-running every settlement of the
last three years is expensive and mostly useless: most settlement dates do
not move. The `workalendar.settlement` module keeps, for each business
transaction, the inputs of its last settlement run and compares two calendar
versions to report only the transactions whose settlement date **really**
changes, with a reason.

The stored inputs of a transaction are:

* its **anchor date** (the business start date, e.g. an invoice date),
* its **offset rule** (how the settlement date is derived),
* the **calendar version** used for the last run (plus, optionally, the
  settlement date that was produced).

## Offset rules

All rules share the same `apply(anchor, calendar)` interface and can be
serialized to plain dictionaries.

```python
from datetime import date
from workalendar.settlement import (
    AddWorkingDays, MonthEndNthWorkingDay, FollowingWorkingDay,
    rule_from_dict,
)

AddWorkingDays(5)                 # 5 working days after the anchor
AddWorkingDays(-3)                # 3 working days before the anchor
MonthEndNthWorkingDay(3)          # 3rd working day counting back from month end
FollowingWorkingDay()             # anchor if it works, else next working day

rule = MonthEndNthWorkingDay(3)
assert rule_from_dict(rule.to_dict()) == rule
```

## Recording a transaction

```python
from workalendar.settlement import SettlementRecord

record = SettlementRecord(
    transaction_id="INV-2024-0001",
    anchor=date(2024, 3, 15),
    rule=MonthEndNthWorkingDay(3),
    calendar_version="v2023",
    last_settlement=date(2024, 3, 27),  # optional, what was actually settled
)
record.to_dict()  # JSON-ready, so the previous run can be persisted
```

## Keeping an old calendar version

A `CalendarSnapshot` freezes the weekend weekdays and the holiday dates
(including labels) of a calendar over the needed years. Snapshots are
immutable and JSON-serializable, so an old version stays available after the
live calendar has been corrected.

```python
from workalendar.settlement import CalendarSnapshot

snapshot = CalendarSnapshot.from_calendar(calendar, [2023, 2024, 2025], "v2023")
restored = CalendarSnapshot.from_dict(snapshot.to_dict())
```

## Running the incremental audit

```python
from workalendar.settlement import SettlementAudit

audit = SettlementAudit.from_calendars(
    old_calendar, new_calendar, records, "v2023", "v2024")
result = audit.audit(records)

for change in result.changed:
    print(
        change.transaction_id,
        change.old_settlement, "->", change.new_settlement,
        f"({change.delta_days:+d} days)",
        [reason.code for reason in change.reasons],
    )
# INV-2024-0007 2024-05-13 -> 2024-05-14 (+1 days) ['holiday_added']
```

`result.changed` only contains transactions whose settlement date moved.
`result.unchanged` lists the others -- including the case where two holidays
swap dates but cancel each other out for a given rule. Transactions whose
stored calendar version matches neither snapshot are parked in
`result.unknown_version` instead of being silently recomputed.

Each change carries one or more `ChangeReason` objects:

* `holiday_added` -- new non-working day(s), with the dates and labels,
* `holiday_removed` -- day(s) that stopped being holidays,
* `weekend_changed` -- the weekend weekday definition changed,
* `calendar_changed` -- fallback when no local change explains the move.

## Why a correction outside the query window still matters

Offset rules do not only look at days near the anchor. A month-end rule
scans the whole month: adding a holiday on March 29 moves the third working
day counted backwards from the end, even when every business anchor is in
mid-March. The audit snapshots the full year of every anchor (plus the
adjacent years, to cover year-end rules) and filters the calendar diff
through each rule's effective search interval when building reasons.

Conversely, moving a holiday from one day to another can leave a settlement
exactly unchanged. Such records appear in `unchanged`, not `changed`: the
audit compares the two computed dates rather than assuming any calendar diff
implies a settlement move.

## Reconciling against two full recomputations

`verify_audit` recomputes every record against each of the two full
calendars and compares them, record by record, with the incremental output.
It also checks the optional stored `last_settlement` against the old
calendar. An empty list means the incremental result is exactly equivalent
to running the whole history twice.

```python
from workalendar.settlement import full_recompute, verify_audit

errors = verify_audit(result, records, old_calendar, new_cal)
assert errors == []

# Equivalent reference computations, one per calendar version:
full_recompute(records, old_calendar)
full_recompute(records, new_calendar)
```
