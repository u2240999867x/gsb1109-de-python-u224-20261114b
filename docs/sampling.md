# Sampling plans

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [Sampling plans](sampling.md) / [iCal Export](ical.md) / [Contributing](contributing.md)

A sampling plan describes a contract rule such as:

> Sample on the second business day after the fifth Monday of the month.

The rule is split into two inspectable steps. This makes it possible to audit
why a month produced a sample, why it was skipped, and whether the result
crossed into another month.

## The two rule steps

```python
>>> from workalendar.core import MON
>>> from workalendar.sampling import (
...     BaselineRule, OffsetRule, FifthMondayPolicy, DayZeroPolicy,
...     SamplingRule,
... )
>>> rule = SamplingRule(
...     baseline_rule=BaselineRule(
...         ordinal=5,
...         weekday=MON,
...         missing_policy=FifthMondayPolicy.LAST_MONDAY,
...     ),
...     offset_rule=OffsetRule(
...         business_days=2,
...         day_zero_policy=DayZeroPolicy.INCLUDE,
...     ),
... )
>>> rule.describe()
'Use the 5th Monday of the month; if it does not exist, use the last Monday. Count 2 business day(s) after the baseline. A non-business baseline still counts as day zero.'
>>> rule.inspect()['baseline']['missing_policy']
'last_monday'
```

`BaselineRule` resolves the fifth Monday. When the month has no fifth Monday,
`missing_policy` applies one of two contract variants:

* `FifthMondayPolicy.SKIP` skips the month.
* `FifthMondayPolicy.LAST_MONDAY` uses the last Monday of the month.

`OffsetRule` counts business days after the baseline. `day_zero_policy`
states how a baseline that is a holiday is handled:

* `DayZeroPolicy.INCLUDE` (the default) counts the holiday baseline as day
  zero. The next business day is day one, and the second business day is day
  two.
* `DayZeroPolicy.EXCLUDE` does not count a holiday baseline as day zero.
  Counting starts at the first business day on or after it.

For a baseline that is already a business day, both policies produce the same
date.

## Evaluating a month

```python
>>> from datetime import date
>>> from workalendar.usa import UnitedStates
>>> from workalendar.sampling import SamplingPlan, SamplingStatus
>>> plan = SamplingPlan(UnitedStates(), contract_id="contract-42")
>>> task = plan.evaluate_month(2023, 5)
>>> task.status
<SamplingStatus.OK: 'ok'>
>>> task.baseline
datetime.date(2023, 5, 29)
>>> task.date
datetime.date(2023, 5, 31)
>>> task.baseline_holiday_label
'Memorial Day'
```

The same rule applied to May 2021 in the US produces a different outcome:

```python
>>> task = plan.evaluate_month(2021, 5)
>>> task.status is SamplingStatus.ROLLED_OVER_MONTH
True
>>> task.date
datetime.date(2021, 6, 2)
```

`evaluate_month()` always returns a record. Inspect `task.status` and
`task.explain()` to distinguish the four outcomes:

| Status | Meaning |
| --- | --- |
| `OK` | The sample date is inside the month. |
| `BASELINE_MISSING` | There is no fifth Monday, and the contract skips it. |
| `ROLLED_OVER_MONTH` | The offset crossed into the next month. The task is still valid. |
| `WINDOW_EXCLUDED` | The whole sampling window was excluded. |

For the missing-baseline and excluded-window outcomes, `task.date` is
`None`. For a rollover, `task.date` is the valid date in the next month.

## Windows and exclusions

A plan may exclude dates, date ranges, or whole months:

```python
>>> from workalendar.sampling import SamplingWindow
>>> plan = SamplingPlan(
...     UnitedStates(),
...     excluded_windows=[
...         SamplingWindow(date(2021, 5, 1), date(2021, 5, 31)),
...     ],
... )
>>> task = plan.evaluate_month(2021, 5)
>>> task.status is SamplingStatus.WINDOW_EXCLUDED
True
```

Internally the plan checks the month's window (its first through last day).
If no business day in that window is available, the whole window is
excluded. If only part of the window is excluded, the plan still evaluates
the month normally; only an excluded target date invalidates the task.

`active_from` and `active_until` clip the contract period. Months fully
outside it are reported as `WINDOW_EXCLUDED`; months partially outside it are
evaluated over their remaining days.

## Quarter and month queries agree

`tasks_for_month()` returns zero or one accepted task.
`tasks_for_quarter()` returns accepted tasks for the quarter, and is exactly
equivalent to querying each month separately and merging:

```python
>>> from workalendar.sampling import merge_tasks
>>> monthly = merge_tasks(
...     plan.tasks_for_month(2021, month) for month in (4, 5, 6)
... )
>>> quarterly = plan.tasks_for_quarter(2021, 2)
>>> [(t.task_id, t.date) for t in monthly] == [
...     (t.task_id, t.date) for t in quarterly
... ]
True
```

The task identity contains the contract, period, baseline kind, baseline
date, target date, and status. It never contains the query range, so the
same task has the same identity whether it was reached through a month, a
quarter, or a year query.

Use `evaluate_quarter()` when rejected months also need to be inspected;
it returns one record per month, in month order.

## Multiple contracts

`SamplingSchedule` runs several plans and merges their tasks:

```python
>>> from workalendar.sampling import SamplingSchedule
>>> schedule = SamplingSchedule([plan, other_plan])
>>> tasks = schedule.tasks_for_quarter(2021, 2)
```

Merged results are de-duplicated by `task_id` and sorted by date.
