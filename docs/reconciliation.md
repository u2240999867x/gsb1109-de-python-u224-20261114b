# Advanced feature: reconciliation windows between two calendars

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [iCal Export](ical.md) / [Contributing](contributing.md)

Sometimes two offices do not share the same working calendar. A reconciliation
between a sender and a receiver may be performed under two different contracts:

* **same day**: both places are open on the same date, and the handover happens
  that day;
* **next working day**: the sender dispatches on one of *its* working days, and
  the receiver signs on *its own next working day*, strictly after dispatch.

The two calendars are kept independent. A date is never considered open on one
side because the other side is open.

## Build the pair

```python
>>> from datetime import date
>>> from workalendar.europe import France, Germany
>>> from workalendar.reconciliation import ReconciliationCalendar
>>> pair = ReconciliationCalendar(
...     France(), Germany(),
...     sender_name="France", receiver_name="Germany")
>>> pair.calendar_names
{'sender': 'France', 'receiver': 'Germany'}
```

Every window carries both calendar names, so a report can state the sender's
and the receiver's calendars on each row.

## Same-day contract

A same-day handover is a date that is a working day on both sides. Contiguous
common working days are grouped into one window:

```python
>>> pair.get_same_day_handovers(date(2018, 5, 7), date(2018, 5, 11))
[datetime.date(2018, 5, 7), datetime.date(2018, 5, 9),
 datetime.date(2018, 5, 11)]
>>> [(w.dispatch_start, w.dispatch_end)
...  for w in pair.get_same_day_windows(date(2018, 5, 7), date(2018, 5, 11))]
[(datetime.date(2018, 5, 7), datetime.date(2018, 5, 7)),
 (datetime.date(2018, 5, 9), datetime.date(2018, 5, 9)),
 (datetime.date(2018, 5, 11), datetime.date(2018, 5, 11))]
```

Here May 8th is a French holiday and May 10th is a German holiday, so the
handover days are separated.

If one side is closed, there may be **no** handover date at all in the queried
range. That is a valid result, not an error:

```python
>>> pair.get_same_day_windows(date(2018, 5, 8), date(2018, 5, 8))
[]
```

## Next-working-day contract

For every sender working day in the query range, the receipt is the receiver's
next working day. The dispatch/receipt pairs are always available even when the
receipt falls outside the queried range:

```python
>>> pair.get_dispatch_receipts(date(2018, 5, 7), date(2018, 5, 11))
[(datetime.date(2018, 5, 7), datetime.date(2018, 5, 8)),
 (datetime.date(2018, 5, 9), datetime.date(2018, 5, 11)),
 (datetime.date(2018, 5, 11), datetime.date(2018, 5, 14))]
```

Several dispatch dates may map to the same receipt. Contiguous dispatch dates
that share one receipt become a single window whose `receipt_start` and
`receipt_end` are the same date:

```python
>>> windows = pair.get_dispatch_windows(date(2018, 5, 9), date(2018, 5, 11))
>>> windows[0].dispatch_start, windows[0].dispatch_end
(datetime.date(2018, 5, 9), datetime.date(2018, 5, 9))
>>> windows[0].receipt_start, windows[0].receipt_end
(datetime.date(2018, 5, 11), datetime.date(2018, 5, 11))
```

This is intentionally **not** a same-day contract: even if both sides are open
on the dispatch date, the receiver signs on the following receiver working day.

Swapping the two calendars swaps the direction of the contract, hence the
dispatch/receipt pairs, but not the same-day handover dates.

## Both contracts at once

`get_windows()` defaults to both contracts and sorts windows by their dispatch
start; `plan()` returns the same result split by contract:

```python
>>> plan = pair.plan(date(2018, 5, 7), date(2018, 5, 11))
>>> plan.same_day, plan.next_working_day
>>> plan.by_contract("same_day")
>>> plan.by_contract("next_working_day")
```

The plan also exposes `sender_calendar` and `receiver_calendar`.

## Query boundaries

Receipt lookup is independent of the query bounds. A query that starts or ends
inside a holiday returns the same sender/receiver pairing as the enclosing
query; the receipt date is not clipped to the query interval:

```python
>>> pair.get_dispatch_receipts(date(2018, 5, 9), date(2018, 5, 9))
[(datetime.date(2018, 5, 9), datetime.date(2018, 5, 11))]
```

Splitting an interval into two adjacent sub-queries and merging their results
gives the same pairs/windows as querying the whole interval in one call.

## Contract names

The canonical names are `"same_day"` and `"next_working_day"`. `None` (or
`"both"`) means all contracts. A few readable aliases such as
`"same day handover"` or `"dispatch"` are normalised; an unknown name raises
`workalendar.exceptions.UnsupportedContract`.

## Convenience entry points

The pair has module-level shortcuts and the `CoreCalendar` base class exposes
sender-side helpers:

```python
>>> from workalendar.reconciliation import (
...     get_reconciliation_windows, get_same_day_handovers,
...     get_dispatch_receipts)
>>> France().get_reconciliation_windows(
...     Germany(), date(2018, 5, 7), date(2018, 5, 11),
...     contract="same_day")
```

Each window converts to a JSON-friendly mapping with `window.as_dict()`.

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [iCal Export](ical.md) / [Contributing](contributing.md)
