# Sequential station scheduling

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [iCal Export](ical.md) / [Contributing](contributing.md)

`add_working_days()` answers "what date is five working days after today?".
`workalendar.scheduling` answers the opposite question: given a final
deadline and several stations that hand an order to each other, what is the
latest date the first station can receive it?

Each station can use a different calendar. Each station also declares its
processing duration and whether its contract counts the receipt day as the
first processing day.

## Build a chain

```python
from datetime import date
from workalendar.europe import France
from workalendar.usa import NewYork
from workalendar.scheduling import Station, StationChain

chain = StationChain([
    Station('甲', France(), 2, include_handover_day=True),
    Station('乙', NewYork(), 3, include_handover_day=False),
    Station('丙', France(), 2, include_handover_day=True),
])
```

- `processing_days` counts working days, not calendar days.
- `include_handover_day=True` means the receiving station can count the
  receipt day itself if that day is working for that station.
- With `include_handover_day=False`, processing starts on the next working
  day, so the receipt day may be a rest day.

## Work backwards from the deadline

`latest_schedule()` runs a backward pass over the chain, then a forward pass
from the resulting first receipt date. The forward pass produces the dates
each station actually occupies.

```python
>>> schedule = chain.latest_schedule(date(2026, 11, 20))
>>> schedule.submission_date
datetime.date(2026, 11, 13)
>>> schedule.completion_date
datetime.date(2026, 11, 20)
>>> schedule.feasible
True
```

Each step exposes the receipt date, completion date, and the actual working
dates occupied:

```python
>>> for step in schedule.steps:
...     print(step.name, step.handover, step.completion,
...           step.working_days_count)
甲 2026-11-13 2026-11-16 2
乙 2026-11-16 2026-11-19 3
丙 2026-11-19 2026-11-20 2
```

| Station | Receives | Completes | Working days used |
|:--------|---------:|----------:|------------------:|
| 甲 | 2026-11-13 | 2026-11-16 | 2 (Fri, Mon) |
| 乙 | 2026-11-16 | 2026-11-19 | 3 (Tue, Wed, Thu) |
| 丙 | 2026-11-19 | 2026-11-20 | 2 (Thu, Fri) |

`step.working_days` returns the concrete dates rather than only the count.
This is useful when a public holiday falls inside the interval.

## Holiday gaps and the latest still-feasible date

When a station does not count its receipt day, several consecutive receipt
dates can map to the same first processing day. `latest_handover()` and
`latest_schedule()` return the latest calendar date that is still feasible,
not the first date of the gap.

For example, if Monday is the next working day, a one-day station receives
on Friday, Saturday, or Sunday and still completes on Monday. The latest
feasible receipt date is Sunday:

```python
>>> station = Station('甲', France(), 1, include_handover_day=False)
>>> station.latest_handover(date(2026, 11, 23))
datetime.date(2026, 11, 22)
```

`latest_submission_date()` is a shortcut when only the first receipt date is
needed:

```python
>>> chain.latest_submission_date(date(2026, 11, 20))
datetime.date(2026, 11, 13)
```

## Shift the plan one day later

`shift_latest_submission()` moves the latest submission forward by a number
of calendar days, then runs the whole chain forward again. It reports both
per-station slippage and the final-deadline result:

```python
>>> result = chain.shift_latest_submission(date(2026, 11, 20), days=1)
>>> result.shifted.submission_date
datetime.date(2026, 11, 14)
>>> result.deadline_missed
True
>>> result.first_late_station.name
'丙'
```

- `late_stations` lists every station whose completion is after the final
  deadline; `first_late_station` is the first of them in chain order.
- `behind_schedule_stations` lists stations that finish later than the
  receipt/completion allocation from the original backward plan.
  `first_behind_schedule_station` is the first of those.
- `deadline_missed` is `True` when the final station completes after the
  deadline.

The two notions differ: a station can fall behind its original allocation
while a later station is still able to absorb the delay before the final
deadline, or a later station can be the first one to miss the deadline.

## Functional helpers

The same operations are available without constructing a `StationChain`
explicitly:

```python
from workalendar.scheduling import (
    forward_schedule, latest_schedule, latest_submission_date,
)

latest_submission_date(stations, deadline)
latest_schedule(stations, deadline)
forward_schedule(stations, submission_date, deadline=deadline)
```

`StationChain.forward_schedule()` accepts an optional
`allowed_completions` list; when provided, the result records which stations
beat their allocated completion date and which fell behind.

## Validation

Construction rejects invalid input with `SchedulingError`:

- an empty chain, or one containing objects that are not `Station`;
- a missing station name, or a negative/non-integer duration;
- a calendar without a callable `is_working_day()`;
- a zero or non-integer shift.

A forward pass also verifies that each station really occupies exactly
`processing_days` working dates; if the calendar has no usable working days,
the operation raises `SchedulingError` instead of looping indefinitely.

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [iCal Export](ical.md) / [Contributing](contributing.md)
