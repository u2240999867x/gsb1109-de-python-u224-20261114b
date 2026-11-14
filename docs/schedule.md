# Nominal-period scheduling

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md)

This module builds *run tables* for recurring nominal accounting periods
(month-end collection and similar) on top of any workalendar calendar. A
nominal period keeps its identity even when its actual execution date is
moved, and each period gets its own stable task identifier.

## Policies

Two postponement policies are available:

* `RESTRICTED` (default): postpone to the next working day, but if that day
  would fall in a later calendar month, use the **last working day of the
  nominal month** instead. The run never leaves its nominal month.
* `UNRESTRICTED`: postpone unconditionally to the next working day, even if
  it falls in the next month. Only products explicitly allowed to use it
  should be configured this way.

```python
from datetime import date
from workalendar.europe import France
from workalendar.schedule import (
    RESTRICTED, UNRESTRICTED, PeriodScheduler, month_periods,
)

cal = France()
scheduler = PeriodScheduler(cal, policy=RESTRICTED)
run = scheduler.schedule_period(2024, 3, product="loan")
run.period       # (2024, 3) — the nominal period, always preserved
run.anchor_date  # datetime.date(2024, 3, 31) — nominal month-end
run.run_date     # datetime.date(2024, 3, 29) — actual execution date
run.uid          # 'loan:2024-03' — stable, unique task identity
```

## Building a run table

```python
periods = list(month_periods((2024, 1), (2024, 6)))
table = scheduler.build_schedule("loan", periods)
for run in table:
    print(run.uid, run.run_date)
```

Use `build_product_schedules` when several products follow different
policies:

```python
from workalendar.schedule import build_product_schedules

tables = build_product_schedules(
    cal,
    {"standard": RESTRICTED, "premium": UNRESTRICTED},
    periods,
)
```

## Colliding periods and resends

Long holidays can push two consecutive nominal periods onto the same
execution date. They still produce two independent tasks with distinct UIDs:

```python
same_day_runs = table.on(date(2024, 3, 1))  # both periods, if they collide
```

A resend always targets an explicit nominal period, never a date, so the
other period sharing that day cannot be resent by mistake:

```python
run = table.resend((2024, 2))  # selects period 2024-02 only
```

Asking for a period that is not part of the table raises
`UnknownPeriodError`.

## No silent fallback into another month

With the restricted policy, if an entire nominal month has no working day,
there is no valid slot inside the period. The scheduler raises
`NoWorkingDayInPeriodError` instead of quietly placing the run on a previous
month's working day, which would occupy another period's slot. The error
exposes `year`, `month`, `product` and `period` so callers can report the
unsolvable case explicitly.

Unrestricted products are allowed to roll over a fully closed month, but the
period identity on the resulting run is still the original nominal period.

## Extra working days and holidays

Like the core calendar methods, the scheduler accepts
`extra_working_days` and `extra_holidays`, and a custom `anchor_day` (day of
month, automatically clamped to shorter months) for schedules that are not
anchored on the month-end.
