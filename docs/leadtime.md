# Lead-time dashboard

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [iCal Export](ical.md) / [Contributing](contributing.md)

The `workalendar.leadtime` module measures order lead times in working days,
including orders whose clock may be paused by contract.

## Measure one order

```python
>>> from datetime import date
>>> from workalendar.europe import France
>>> from workalendar.leadtime import LeadTimeOrder, PausePeriod, compute_lead_time
>>> order = LeadTimeOrder(
...     reference="ORD-42",
...     started_on=date(2024, 4, 29),
...     sla_working_days=5,
...     delivered_on=date(2024, 5, 6),
... )
>>> result = compute_lead_time(order, as_of=date(2024, 5, 10), calendar=France())
>>> result.elapsed_working_days
4
>>> result.overdue
False
```

The start date is excluded from the elapsed count, following the same
convention as `Calendar.get_working_days_delta()`. Pass `include_start=True`
to count it when it is a working day.

## Pauses

A pause stops the clock from its start date. By default its end date is the
resume date and is a normal working day:

```python
>>> order = LeadTimeOrder(
...     started_on=date(2024, 4, 29),
...     sla_working_days=5,
...     pauses=[PausePeriod(date(2024, 5, 2), date(2024, 5, 6))],
... )
>>> compute_lead_time(order, date(2024, 5, 10), calendar=France()).elapsed_working_days
4
```

Use `PausePeriod(start, end, end_inclusive=True)` (or the
`pause_end_inclusive=True` option) when the contract states that the end date
is paused as well. A pause without an end date is still active.

Overlapping or adjacent pauses are merged before their working days are
subtracted, so a day is never removed twice. Weekends and holidays inside a
pause never reduce the elapsed count; only working days do.

## Observation cutoff

Everything is measured at the observation cutoff date:

* a delivery dated after the cutoff leaves the order in transit;
* a pause release dated after the cutoff is ignored, so the pause remains
  active through the cutoff;
* moving the cutoff forward over a weekend or holiday does not add a working
  day to an order that is still in transit.

## Dashboard summary

```python
>>> from workalendar.leadtime import lead_time_dashboard
>>> dashboard = lead_time_dashboard(orders, as_of=date(2024, 5, 10), calendar=France())
>>> dashboard.total_count
...
>>> dashboard.overdue_count
...
>>> dashboard.overdue_working_days
...
>>> dashboard.can_still_be_on_time_count
...
```

`LeadTimeDashboard` also exposes `delivered_count`,
`delivered_on_time_count`, `delivered_late_count`, `in_transit_count`,
`still_on_time_count` and the matching `*_orders` tuples. `as_dict()` returns a
serialisable summary, and summary keys are also reachable with
`dashboard["overdue_count"]`.

The same helpers are available directly on any calendar instance:
`cal.get_lead_time_elapsed(...)`, `cal.get_lead_time(order, as_of)` and
`cal.get_lead_time_dashboard(orders, as_of)`. The `workalendar.sla` module
re-exports the same public names.

An order counts as `overdue` when its elapsed working days exceed its SLA
allowance. Exactly reaching the allowance is not overdue. An order that is
still in transit and not yet overdue counts as `can_still_be_on_time`.

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [iCal Export](ical.md) / [Contributing](contributing.md)
