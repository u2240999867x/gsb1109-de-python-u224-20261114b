# Provenance and layered calendars

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [iCal Export](ical.md) / [Contributing](contributing.md)

A business day is rarely described by a single calendar. A group calendar may
declare a Saturday a make-up workday, a branch may publish a temporary
stoppage for that same Saturday, and an employee's list may still contain a
notice that was later withdrawn. Overwriting those statements loses the
answer to "why is this day working or not?".

`workalendar.provenance` keeps every notice and explains which one was
effective for a query. It is independent of the calendar classes: a base
calendar such as `China()` can provide group defaults, while notices add the
higher layers.

## Declarations

A `Declaration` is a dated statement about one business day.

```python
>>> from datetime import date
>>> from workalendar.provenance import Declaration, SourceLevel
>>> day = date(2026, 5, 9)  # a Saturday
>>> Declaration(
...     day, True, source='group-calendar', level='group',
...     published_at='2026-04-20', label='make-up workday',
... )
Declaration(day=datetime.date(2026, 5, 9), working=True, ...
```

The main fields are:

* `day`: the business day, as a `date`, `datetime` or ISO string.
* `working`: `True` for a working day, `False` for a holiday or stoppage.
* `source` and `level`: who said it, and at what authority level.
* `published_at` / `withdrawn_at`: when the notice became effective and when
  it was retracted.
* `version`: notice version within the same source and day.
* `label`, `notice_id`, `rule`: optional audit metadata.

`SourceLevel` has three levels, from least to most specific:
`GROUP`, `BRANCH`, `PERSONAL`. Common names such as `company`, `site`,
`location`, `employee`, or `集团` / `分公司` / `个人` are accepted as aliases.

## Resolution

```python
>>> from workalendar.provenance import LayeredCalendar
>>> calendar = LayeredCalendar()
>>> calendar.declare(day, True, source='group-calendar', level='group',
...                  published_at='2026-04-20')
>>> calendar.declare(day, False, source='branch-shanghai', level='branch',
...                  published_at='2026-05-07', notice_id='B-1')
>>> calendar.is_working_day(day, as_of='2026-05-08')
False
```

The query time (`as_of`, also available as `business_day` /
`notification_time` / `publication_time` aliases) controls which notices
exist. A notice published after the query time is reported under
`not_published`, and a notice withdrawn before the query time is reported
under `withdrawn`.

The rules are:

1. Only notices published by the query time and still active participate.
2. For one source and day, the latest active `version` participates; older
   versions are `shadowed`.
3. The highest level wins: `GROUP < BRANCH < PERSONAL`.
4. Two same-level active notices that disagree leave the day `pending`. The
   result is `None` and both notices are returned in `conflicts`.
5. Without an active notice, an optional `base_calendar` supplies the
   group-level default.

## Explanations

Every query returns a `Resolution` that always exposes the winner, the
shadowed notices, the conflicts, withdrawn notices and not-yet-published
notices:

```python
>>> result = calendar.resolve(day, as_of='2026-05-08')
>>> result.status
'resolved'
>>> result.winner.source
'branch-shanghai'
>>> [item.source for item in result.shadowed]
['group-calendar']
>>> print(result.explain())
2026-05-09 (SAT): resolved
  as of: 2026-05-08T23:59:59.999999
  working day: no
  winner: branch-shanghai [branch] v1 -> not_working ...
  pending same-level conflict: none
  shadowed:
    group-calendar [group] v1 -> working ... -> overridden by higher-priority source branch-shanghai (branch)
```

`to_dict()` returns the same audit trail as JSON-friendly data. The reason
for each shadowed notice is available through `reason_for(declaration)` or
the entries of `to_dict()['shadowed']`.

## Withdrawal

`withdraw()` (alias `revoke`) records a withdrawal time. The notice is not
deleted, so older queries can still explain what was visible then.

```python
>>> calendar.withdraw('B-1', at='2026-05-08T18:00:00')
>>> calendar.is_working_day(day, as_of='2026-05-08T20:00:00')
True
```

Withdrawing the branch's temporary stoppage reveals the group calendar's
make-up Saturday again. The withdrawn notice remains listed in `withdrawn`.

## Using a calendar as the group default

```python
>>> from workalendar.asia import China
>>> calendar = LayeredCalendar(base_calendar=China())
```

The base calendar can also be selected by ISO region code:

```python
>>> calendar = LayeredCalendar(region='CN')
```

The base calendar is consulted only when no group-level notice is in force.
It is treated as a group-level declaration, so a branch stoppage shadows it
and withdrawing the stoppage makes it visible again.

## Collected records

`Declaration` accepts dictionaries with common collected-notice field names
such as `date`, `business_day`, `source_level`, `source_layer`, `published`,
`retracted_at`, `status`, and `notice_id`. This lets ingestion code pass
records straight through:

```python
>>> calendar = LayeredCalendar([
...     {'date': '2026-05-09', 'status': 'working',
...      'source': 'group-calendar', 'source_level': 'group',
...      'published_time': '2026-04-20'},
...     {'business_day': '2026-05-09', 'status': 'not_working',
...      'source': 'branch-shanghai', 'source_layer': 'branch',
...      'published': '2026-05-07', 'notice_id': 'B-1'},
... ])
```

Use `resolve()` / `explain()` at module level for one-off queries over an
iterable of declarations.

[Home](index.md) / [Basic usage](basic.md) / [Advanced usage](advanced.md) / [Class options](class-options.md) / [ISO Registry](iso-registry.md) / [iCal Export](ical.md) / [Contributing](contributing.md)
