"""
dynamic_table_occupancy.py
~~~~~~~~~~~~~~~~~~~~~~~~~~

Time-weighted occupancy analysis for the HPACK dynamic table.

This script replays an archive of plain header blocks, where every block
carries the time it was observed, through the hpack encoder and accounts
for every byte the dynamic table holds, split into three field families:

- ``name``:     bytes of header names resident in the table,
- ``value``:    bytes of header values resident in the table,
- ``overhead``: the fixed per-entry bookkeeping cost (RFC 7541 Section 4.1).

Every insertion, eviction and capacity change is recorded at the
observation time of the header block that caused it, so the report can
attribute byte-time (bytes x residence time) to each family and derive
time-weighted average occupancies. The accounting rules are:

- the same name/value pair inserted twice occupies space twice, for as
  long as each copy stays resident;
- an entry larger than the maximum table size empties the table but is
  itself never stored, so it accrues no residence time;
- after every observation, the per-family subtotals add up to exactly the
  table's current size as tracked by the library itself.

It should be invoked as follows:

    $ python dynamic_table_occupancy.py /path/to/archive.json

The archive is a JSON object with a ``cases`` list. Each case has a
``time`` (any monotonically non-decreasing number; the sequence number is
used when omitted) and a ``headers`` list, and may carry a
``header_table_size`` to model a capacity change observed at that time:

    {
      "cases": [
        {"time": 0.0, "headers": [{":method": "GET"}, {":path": "/"}]},
        {"time": 1.5, "header_table_size": 1024, "headers": [...]}
      ],
      "end_time": 2.0
    }

``end_time`` is optional and defaults to the last observation time;
entries still resident then stop accruing byte-time (right-censoring).
"""
from __future__ import annotations

import json
import sys
from collections import Counter

from hpack.hpack import Encoder
from hpack.table import HeaderTable, table_entry_size

FAMILY_NAME = "name"
FAMILY_VALUE = "value"
FAMILY_OVERHEAD = "overhead"
FAMILIES = (FAMILY_NAME, FAMILY_VALUE, FAMILY_OVERHEAD)

#: Fixed per-entry overhead, taken from the library so the two never drift.
ENTRY_OVERHEAD = table_entry_size(b"", b"")


def _as_bytes(value):
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


class OccupancyLedger:
    """
    Records timestamped dynamic-table events per field family.

    Keeps the current per-family byte subtotals and integrates them over
    time, so byte-time and time-weighted averages can be reported for any
    observation window.
    """

    def __init__(self):
        self._subtotals = dict.fromkeys(FAMILIES, 0)
        self._byte_time = dict.fromkeys(FAMILIES, 0)
        self._residents = Counter()
        self._first_time = None
        self._last_time = None
        self.events = []
        self.event_counts = {"inserted": 0, "evicted": 0, "capacity_changed": 0}

    def _advance(self, time):
        if self._last_time is None:
            self._first_time = time
        else:
            if time < self._last_time:
                raise ValueError(
                    "observation times must be non-decreasing: "
                    f"{time} follows {self._last_time}"
                )
            delta = time - self._last_time
            for family in FAMILIES:
                self._byte_time[family] += self._subtotals[family] * delta
        self._last_time = time

    def advance(self, time):
        """
        Moves the clock to an observation time without any table event.
        """
        self._advance(time)

    def inserted(self, time, name, value):
        name, value = _as_bytes(name), _as_bytes(value)
        self._advance(time)
        self._subtotals[FAMILY_NAME] += len(name)
        self._subtotals[FAMILY_VALUE] += len(value)
        self._subtotals[FAMILY_OVERHEAD] += ENTRY_OVERHEAD
        self._residents[(name, value)] += 1
        self.event_counts["inserted"] += 1
        self.events.append({
            "time": time, "kind": "inserted", "name": name, "value": value,
        })

    def evicted(self, time, name, value):
        name, value = _as_bytes(name), _as_bytes(value)
        self._advance(time)
        if self._residents[(name, value)] <= 0:
            raise ValueError(f"evicted entry was never inserted: {(name, value)!r}")
        self._residents[(name, value)] -= 1
        self._subtotals[FAMILY_NAME] -= len(name)
        self._subtotals[FAMILY_VALUE] -= len(value)
        self._subtotals[FAMILY_OVERHEAD] -= ENTRY_OVERHEAD
        self.event_counts["evicted"] += 1
        self.events.append({
            "time": time, "kind": "evicted", "name": name, "value": value,
        })

    def capacity_changed(self, time, old_size, new_size):
        self._advance(time)
        self.event_counts["capacity_changed"] += 1
        self.events.append({
            "time": time, "kind": "capacity_changed",
            "old_size": old_size, "new_size": new_size,
        })

    def subtotals(self):
        """
        Current per-family byte subtotals, plus their ``total``.
        """
        result = dict(self._subtotals)
        result["total"] = sum(self._subtotals.values())
        return result

    def report(self, end_time=None):
        """
        Byte-time and time-weighted averages per family over the window
        from the first observation to ``end_time`` (default: the last
        observed event or checkpoint).
        """
        if end_time is not None:
            self._advance(end_time)
        duration = 0
        if self._first_time is not None:
            duration = self._last_time - self._first_time

        byte_time = dict(self._byte_time)
        byte_time["total"] = sum(self._byte_time.values())
        if duration > 0:
            averages = {
                family: self._byte_time[family] / duration for family in FAMILIES
            }
        else:
            averages = dict.fromkeys(FAMILIES, 0.0)
        averages["total"] = sum(averages[family] for family in FAMILIES)

        return {
            "start_time": self._first_time,
            "end_time": self._last_time,
            "duration": duration,
            "byte_time": byte_time,
            "time_weighted_average": averages,
            "event_counts": dict(self.event_counts),
            "final_subtotals": self.subtotals(),
        }


class InstrumentedHeaderTable(HeaderTable):
    """
    A HeaderTable that reports every residency change to an OccupancyLedger.

    Set ``clock`` to the observation time of the header block currently
    being processed; every event caused by that block is stamped with it.
    The mutation paths mirror HeaderTable exactly, so the ledger always
    agrees with the table's own size accounting.
    """

    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger
        self.clock = 0.0

    def add(self, name, value):
        size = table_entry_size(name, value)
        if size > self._maxsize:
            # Too big to ever fit: the table is cleared and the new entry
            # itself is never stored, so it must not accrue residence time.
            self._evict_all()
        else:
            self.dynamic_entries.appendleft((name, value))
            self._current_size += size
            self.ledger.inserted(self.clock, name, value)
            self._shrink()

    def _shrink(self):
        while self._current_size > self._maxsize:
            name, value = self.dynamic_entries.pop()
            self._current_size -= table_entry_size(name, value)
            self.ledger.evicted(self.clock, name, value)

    def _evict_all(self):
        while self.dynamic_entries:
            name, value = self.dynamic_entries.pop()
            self.ledger.evicted(self.clock, name, value)
        self._current_size = 0

    @property
    def maxsize(self):
        return self._maxsize

    @maxsize.setter
    def maxsize(self, newmax):
        newmax = int(newmax)
        oldmax = self._maxsize
        self._maxsize = newmax
        self.resized = newmax != oldmax
        if self.resized:
            self.ledger.capacity_changed(self.clock, oldmax, newmax)
        if newmax <= 0:
            self._evict_all()
        elif oldmax > newmax:
            self._shrink()


def normalize_headers(headers):
    """
    Accepts headers as a list of single-pair objects (the usual archive
    form), a list of (name, value) pairs, or a single object, and returns
    a list of (name, value) tuples.
    """
    if isinstance(headers, dict):
        headers = [headers]
    pairs = []
    for item in headers:
        if isinstance(item, dict):
            pairs.extend(item.items())
        else:
            name, value = item
            pairs.append((name, value))
    return pairs


def replay_archive(cases, end_time=None, huffman=True, ledger=None):
    """
    Replays archived header blocks through an Encoder and returns the
    occupancy report for the dynamic table.

    Each case may define ``time`` (default: its sequence number),
    ``headers`` and optionally ``header_table_size``. After every case the
    ledger's subtotals are checked against the table's own current size.
    """
    if ledger is None:
        ledger = OccupancyLedger()
    table = InstrumentedHeaderTable(ledger)
    encoder = Encoder()
    encoder.header_table = table

    timeline = []
    for seqno, case in enumerate(cases):
        time = case.get("time", seqno)
        table.clock = time

        table_size = case.get("header_table_size")
        if table_size is not None:
            encoder.header_table_size = table_size

        encoder.encode(normalize_headers(case["headers"]), huffman=huffman)
        ledger.advance(time)

        subtotals = ledger.subtotals()
        actual_size = table._current_size
        if subtotals["total"] != actual_size:
            raise AssertionError(
                f"ledger/table mismatch at seqno {seqno} (time {time}): "
                f"families total {subtotals['total']} != table size {actual_size}"
            )
        timeline.append({
            "seqno": seqno,
            "time": time,
            "subtotals": subtotals,
            "table_size": actual_size,
        })

    report = ledger.report(end_time)
    report["timeline"] = timeline
    return report


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        archive = json.load(f)
    report = replay_archive(archive["cases"], end_time=archive.get("end_time"))
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
