import json
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "utils")
)

from dynamic_table_occupancy import (  # noqa: E402
    ENTRY_OVERHEAD,
    InstrumentedHeaderTable,
    OccupancyLedger,
    normalize_headers,
    replay_archive,
)
from hpack.table import table_entry_size  # noqa: E402


@pytest.fixture
def ledger():
    return OccupancyLedger()


@pytest.fixture
def table(ledger):
    return InstrumentedHeaderTable(ledger)


class TestOccupancyLedger:
    def test_single_entry_byte_time(self, ledger):
        ledger.inserted(0.0, b"abc", b"de")
        ledger.evicted(2.0, b"abc", b"de")

        report = ledger.report()

        assert report["byte_time"] == {
            "name": 3 * 2,
            "value": 2 * 2,
            "overhead": ENTRY_OVERHEAD * 2,
            "total": (3 + 2 + ENTRY_OVERHEAD) * 2,
        }
        assert report["duration"] == 2.0
        assert report["time_weighted_average"] == {
            "name": 3.0,
            "value": 2.0,
            "overhead": float(ENTRY_OVERHEAD),
            "total": float(3 + 2 + ENTRY_OVERHEAD),
        }

    def test_residence_extends_to_end_time(self, ledger):
        ledger.inserted(1.0, b"a", b"b")
        report = ledger.report(end_time=4.0)

        size = 1 + 1 + ENTRY_OVERHEAD
        assert report["byte_time"]["total"] == size * 3
        assert report["duration"] == 3.0
        assert report["final_subtotals"]["total"] == size

    def test_evicting_unknown_entry_raises(self, ledger):
        ledger.inserted(0.0, b"a", b"b")
        with pytest.raises(ValueError, match="never inserted"):
            ledger.evicted(1.0, b"a", b"c")

    def test_non_monotonic_time_raises(self, ledger):
        ledger.inserted(2.0, b"a", b"b")
        with pytest.raises(ValueError, match="non-decreasing"):
            ledger.evicted(1.0, b"a", b"b")

    def test_empty_ledger_reports_zeroes(self, ledger):
        report = ledger.report()
        assert report["duration"] == 0
        assert report["byte_time"]["total"] == 0
        assert report["time_weighted_average"]["total"] == 0.0


class TestInstrumentedHeaderTable:
    def test_duplicate_entries_occupy_simultaneously(self, ledger, table):
        size = table_entry_size(b"x-a", b"1")

        table.clock = 0.0
        table.add(b"x-a", b"1")
        table.add(b"x-a", b"1")

        # Both copies are resident at once.
        assert ledger.subtotals()["total"] == 2 * size
        assert table._current_size == 2 * size

        # Shrink to a single copy: only the oldest is evicted.
        table.clock = 1.0
        table.maxsize = size
        assert ledger.subtotals()["total"] == size == table._current_size

        report = ledger.report(end_time=3.0)
        # Two copies for [0, 1), one copy for [1, 3).
        assert report["byte_time"]["total"] == 2 * size * 1 + size * 2
        assert report["time_weighted_average"]["total"] == 4 * size / 3

    def test_oversized_entry_never_resident(self, ledger, table):
        small = table_entry_size(b"a", b"b")
        table.clock = 0.0
        table.maxsize = 64
        table.add(b"a", b"b")

        table.clock = 1.0
        table.add(b"n" * 50, b"v" * 50)  # 132 bytes > 64: clears the table

        assert table._current_size == 0
        assert len(table.dynamic_entries) == 0
        assert ledger.subtotals()["total"] == 0
        assert ledger.event_counts == {
            "inserted": 1, "evicted": 1, "capacity_changed": 1,
        }

        report = ledger.report(end_time=5.0)
        # Only the small entry accrued byte-time, and only for [0, 1).
        assert report["byte_time"]["total"] == small

    def test_capacity_change_evicts_and_is_recorded(self, ledger, table):
        table.clock = 0.0
        table.add(b"x-one", b"1")
        table.add(b"x-two", b"2")

        table.clock = 2.0
        table.maxsize = table_entry_size(b"x-two", b"2")

        assert [e["kind"] for e in ledger.events] == [
            "inserted", "inserted", "capacity_changed", "evicted",
        ]
        event = ledger.events[2]
        assert (event["old_size"], event["new_size"]) == (4096, table.maxsize)
        # The oldest entry was evicted; the newest remains.
        assert ledger.events[3]["name"] == b"x-one"
        assert ledger.subtotals()["total"] == table._current_size

    def test_zero_capacity_clears_table(self, ledger, table):
        table.clock = 0.0
        table.add(b"x-one", b"1")
        table.clock = 1.0
        table.maxsize = 0

        assert table._current_size == 0
        assert ledger.subtotals()["total"] == 0


class TestReplayArchive:
    ARCHIVE = [
        {"time": 0.0, "headers": [{"x-a": "1"}]},
        {"time": 2.0, "headers": [{"x-b": "22"}]},
        {"time": 3.0, "header_table_size": 40, "headers": [{"x-c": "3"}]},
        {"time": 7.0, "headers": [{"x-c": "3"}]},
    ]

    def test_byte_time_and_averages(self):
        report = replay_archive(self.ARCHIVE)

        # Hand-computed residences:
        #   [0, 2): (x-a, 1)  -> 36 bytes
        #   [2, 3): + (x-b, 22) -> 73 bytes
        #   t = 3: shrink to 40 evicts (x-a, 1); inserting (x-c, 3)
        #          evicts (x-b, 22); 36 bytes remain
        #   [3, 7): (x-c, 3) -> 36 bytes
        assert report["byte_time"] == {
            "name": 3 * 2 + 6 * 1 + 3 * 4,
            "value": 1 * 2 + 3 * 1 + 1 * 4,
            "overhead": 32 * 2 + 64 * 1 + 32 * 4,
            "total": 36 * 2 + 73 * 1 + 36 * 4,
        }
        assert report["duration"] == 7.0
        assert report["time_weighted_average"]["total"] == (
            36 * 2 + 73 + 36 * 4) / 7
        assert report["event_counts"] == {
            "inserted": 3, "evicted": 2, "capacity_changed": 1,
        }

    def test_subtotals_match_table_size_at_every_observation(self):
        report = replay_archive(self.ARCHIVE)
        for point in report["timeline"]:
            assert point["subtotals"]["total"] == point["table_size"]
        assert [p["table_size"] for p in report["timeline"]] == [
            36, 73, 36, 36,
        ]

    def test_family_averages_sum_to_total(self):
        report = replay_archive(self.ARCHIVE)
        averages = report["time_weighted_average"]
        assert averages["name"] + averages["value"] + averages["overhead"] \
            == averages["total"]

    def test_default_times_are_sequence_numbers(self):
        report = replay_archive([{"headers": [{"x-a": "1"}]}])
        assert report["duration"] == 0
        assert report["time_weighted_average"]["total"] == 0.0
        assert report["final_subtotals"]["total"] == table_entry_size(
            b"x-a", b"1")

    def test_real_fixture_story(self):
        fixture = os.path.join(
            os.path.dirname(__file__),
            "test_fixtures", "raw-data", "story_00.json",
        )
        with open(fixture, encoding="utf-8") as f:
            story = json.load(f)

        cases = [
            dict(case, time=float(seqno))
            for seqno, case in enumerate(story["cases"])
        ]
        report = replay_archive(cases)

        assert report["event_counts"]["inserted"] > 0
        for point in report["timeline"]:
            assert point["subtotals"]["total"] == point["table_size"]
        byte_time = report["byte_time"]
        assert byte_time["name"] + byte_time["value"] \
            + byte_time["overhead"] == byte_time["total"]
        assert report["time_weighted_average"]["total"] == (
            byte_time["total"] / report["duration"]
        )


class TestNormalizeHeaders:
    def test_single_key_objects(self):
        assert normalize_headers([{"a": "1"}, {"b": "2"}]) == [
            ("a", "1"), ("b", "2"),
        ]

    def test_pairs_and_plain_dict(self):
        assert normalize_headers([("a", "1"), ["b", "2"]]) == [
            ("a", "1"), ("b", "2"),
        ]
        assert normalize_headers({"a": "1"}) == [("a", "1")]
