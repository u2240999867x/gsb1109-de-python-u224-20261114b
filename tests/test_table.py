import pytest

from hpack import InvalidTableIndex, InvalidTableStateError
from hpack.table import (
    EVICTION_INSERT,
    EVICTION_RESIZE,
    HeaderTable,
    table_entry_size,
)


class TestPackageFunctions:
    def test_table_entry_size(self):
        res = table_entry_size(b'TestName', b'TestValue')
        assert res == 49


class TestHeaderTable:
    def test_get_by_index_dynamic_table(self):
        tbl = HeaderTable()
        off = len(HeaderTable.STATIC_TABLE)
        val = (b'TestName', b'TestValue')
        tbl.add(*val)
        res = tbl.get_by_index(off + 1)
        assert res == val

    def test_get_by_index_static_table(self):
        tbl = HeaderTable()
        exp = (b':authority', b'')
        res = tbl.get_by_index(1)
        assert res == exp
        idx = len(HeaderTable.STATIC_TABLE)
        exp = (b'www-authenticate', b'')
        res = tbl.get_by_index(idx)
        assert res == exp

    def test_get_by_index_zero_index(self):
        tbl = HeaderTable()
        with pytest.raises(InvalidTableIndex):
            tbl.get_by_index(0)

    def test_get_by_index_out_of_range(self):
        tbl = HeaderTable()
        off = len(HeaderTable.STATIC_TABLE)
        tbl.add(b'TestName', b'TestValue')
        with pytest.raises(InvalidTableIndex) as e:
            tbl.get_by_index(off + 2)

        assert (
            "Invalid table index %d" % (off + 2) in str(e.value)
        )

    def test_repr(self):
        tbl = HeaderTable()
        tbl.add(b'TestName1', b'TestValue1')
        tbl.add(b'TestName2', b'TestValue2')
        tbl.add(b'TestName2', b'TestValue2')
        exp = (
            "HeaderTable(4096, False, deque(["
            "(b'TestName2', b'TestValue2'), "
            "(b'TestName2', b'TestValue2'), "
            "(b'TestName1', b'TestValue1')"
            "]))"
        )
        res = repr(tbl)
        assert res == exp

    def test_add_to_large(self):
        tbl = HeaderTable()
        # Max size to small to hold the value we specify
        tbl.maxsize = 1
        tbl.add(b'TestName', b'TestValue')
        # Table length should be 0
        assert len(tbl.dynamic_entries) == 0

    def test_search_in_static_full(self):
        tbl = HeaderTable()
        itm = (b':authority', b'')
        exp = (1, itm[0], itm[1])
        res = tbl.search(itm[0], itm[1])
        assert res == exp

    def test_search_in_static_partial(self):
        tbl = HeaderTable()
        exp = (1, b':authority', None)
        res = tbl.search(b':authority', b'NotInTable')
        assert res == exp

    def test_search_in_dynamic_full(self):
        tbl = HeaderTable()
        idx = len(HeaderTable.STATIC_TABLE) + 1
        tbl.add(b'TestName', b'TestValue')
        exp = (idx, b'TestName', b'TestValue')
        res = tbl.search(b'TestName', b'TestValue')
        assert res == exp

    def test_search_in_dynamic_partial(self):
        tbl = HeaderTable()
        idx = len(HeaderTable.STATIC_TABLE) + 1
        tbl.add(b'TestName', b'TestValue')
        exp = (idx, b'TestName', None)
        res = tbl.search(b'TestName', b'NotInTable')
        assert res == exp

    def test_search_no_match(self):
        tbl = HeaderTable()
        tbl.add(b'TestName', b'TestValue')
        res = tbl.search(b'NotInTable', b'NotInTable')
        assert res is None

    def test_maxsize_prop_getter(self):
        tbl = HeaderTable()
        assert tbl.maxsize == HeaderTable.DEFAULT_SIZE

    def test_maxsize_prop_setter(self):
        tbl = HeaderTable()
        exp = int(HeaderTable.DEFAULT_SIZE / 2)
        tbl.maxsize = exp
        assert tbl.resized is True
        assert tbl.maxsize == exp
        tbl.resized = False
        tbl.maxsize = exp
        assert tbl.resized is False
        assert tbl.maxsize == exp

    def test_size(self):
        tbl = HeaderTable()
        for i in range(3):
            tbl.add(b'TestName', b'TestValue')
        res = tbl._current_size
        assert res == 147

    def test_shrink_maxsize_is_zero(self):
        tbl = HeaderTable()
        tbl.add(b'TestName', b'TestValue')
        assert len(tbl.dynamic_entries) == 1
        tbl.maxsize = 0
        assert len(tbl.dynamic_entries) == 0

    def test_shrink_maxsize(self):
        tbl = HeaderTable()
        for i in range(3):
            tbl.add(b'TestName', b'TestValue')

        assert tbl._current_size == 147
        tbl.maxsize = 146
        assert len(tbl.dynamic_entries) == 2
        assert tbl._current_size == 98


class TestEvictionLog:
    def test_resize_evictions_are_recorded(self):
        tbl = HeaderTable()
        for i in range(4):
            tbl.add(b'name%d' % i, b'value%d' % i)

        evicted = list(tbl.dynamic_entries)
        tbl.maxsize = 100

        log = tbl.eviction_log
        assert log, "shrinking the table must log every eviction"
        assert all(entry.reason == EVICTION_RESIZE for entry in log)
        assert [entry.sequence for entry in log] == list(range(1, len(log) + 1))
        # Entries are evicted from the oldest end of the table.
        evicted_entries = [(entry.name, entry.value) for entry in log]
        assert evicted_entries == list(reversed(evicted))[:len(log)]
        assert all(entry.size == table_entry_size(entry.name, entry.value) for entry in log)
        assert tbl.eviction_sequence == log[-1].sequence

    def test_insert_evictions_are_recorded(self):
        tbl = HeaderTable()
        tbl.maxsize = 200
        for i in range(6):
            tbl.add(b'name%d' % i, b'x' * 80)

        log = tbl.eviction_log
        assert log
        assert all(entry.reason == EVICTION_INSERT for entry in log)
        assert [entry.sequence for entry in log] == list(range(1, len(log) + 1))

    def test_oversized_insert_clears_table_with_log(self):
        tbl = HeaderTable()
        tbl.add(b'keep', b'me')
        tbl.maxsize = 50
        tbl.add(b'way-too-big-name', b'way-too-big-value')

        assert len(tbl.dynamic_entries) == 0
        assert [entry.name for entry in tbl.eviction_log] == [b'keep']
        assert tbl.eviction_log[0].reason == EVICTION_INSERT

    def test_zero_maxsize_records_resize_evictions(self):
        tbl = HeaderTable()
        tbl.add(b'a', b'b')
        tbl.add(b'c', b'd')
        tbl.maxsize = 0

        assert len(tbl.dynamic_entries) == 0
        assert {entry.reason for entry in tbl.eviction_log} == {EVICTION_RESIZE}
        assert tbl.eviction_sequence == 2

    def test_evictions_since_filters_by_sequence(self):
        tbl = HeaderTable()
        tbl.maxsize = 120
        for i in range(5):
            tbl.add(b'n%d' % i, b'x' * 60)

        checkpoint = 2
        later = tbl.evictions_since(checkpoint)
        assert later
        assert all(entry.sequence > checkpoint for entry in later)
        assert [entry.sequence for entry in later] == list(range(checkpoint + 1, tbl.eviction_sequence + 1))

    def test_ring_buffer_is_bounded_but_sequence_advances(self):
        tbl = HeaderTable(eviction_log_size=3)
        tbl.maxsize = 120
        for i in range(8):
            tbl.add(b'n%d' % i, b'x' * 60)

        assert len(tbl.eviction_log) == 3
        assert tbl.eviction_sequence > 3
        assert [entry.sequence for entry in tbl.eviction_log] == [
            tbl.eviction_sequence - 2,
            tbl.eviction_sequence - 1,
            tbl.eviction_sequence,
        ]


class TestHeaderTableSnapshot:
    def test_snapshot_restore_roundtrip(self):
        tbl = HeaderTable()
        for i in range(4):
            tbl.add(b'name%d' % i, b'value%d' % i)
        tbl.maxsize = 200  # forces evictions and flips the resized flag

        restored = HeaderTable()
        restored.restore(tbl.snapshot())

        assert list(restored.dynamic_entries) == list(tbl.dynamic_entries)
        assert restored.maxsize == tbl.maxsize
        assert restored._current_size == tbl._current_size
        assert restored.resized == tbl.resized
        assert restored.eviction_sequence == tbl.eviction_sequence
        assert list(restored.eviction_log) == list(tbl.eviction_log)

    def test_snapshot_is_json_serialisable(self):
        import json

        tbl = HeaderTable()
        tbl.add(b'name', b'value')
        tbl.maxsize = 0
        # TypedDict content must consist solely of JSON-friendly primitives.
        json.dumps(tbl.snapshot())

    def test_restore_keeps_evicted_entries_dead_after_growth(self):
        # The core replay guarantee: values evicted while the table is at 1K
        # must not be revived when the capacity later grows back to 2K.
        tbl = HeaderTable()
        for i in range(12):
            tbl.add(b'n%02d' % i, (b'v%02d' % i) + b'x' * 200)

        tbl.maxsize = 1024
        evicted_at_1k = {entry.name for entry in tbl.eviction_log if entry.reason == EVICTION_RESIZE}
        assert evicted_at_1k

        restored = HeaderTable()
        restored.restore(tbl.snapshot())
        restored.maxsize = 2048

        surviving = {name for name, _value in restored.dynamic_entries}
        assert evicted_at_1k.isdisjoint(surviving)
        # The capacity growth itself evicts nothing and changes no entries.
        assert restored._current_size == tbl._current_size
        assert list(restored.dynamic_entries) == list(tbl.dynamic_entries)

    def test_restore_rejects_unknown_version(self):
        state = HeaderTable().snapshot()
        state['version'] = 999
        with pytest.raises(InvalidTableStateError):
            HeaderTable().restore(state)

    def test_restore_rejects_inconsistent_size(self):
        state = HeaderTable().snapshot()
        state['current_size'] = state['current_size'] + 10
        with pytest.raises(InvalidTableStateError):
            HeaderTable().restore(state)

    def test_restore_after_ring_rotation_continues_sequence(self):
        tbl = HeaderTable(eviction_log_size=3)
        tbl.maxsize = 120
        for i in range(8):
            tbl.add(b'n%d' % i, b'x' * 60)
        sequence_before = tbl.eviction_sequence

        restored = HeaderTable()
        restored.restore(tbl.snapshot())
        assert restored.eviction_sequence == sequence_before
        assert list(restored.eviction_log) == list(tbl.eviction_log)

        restored.add(b'fresh', b'x' * 60)
        assert restored.eviction_log[-1].sequence == sequence_before + 1

    def test_restore_rejects_bad_base64(self):
        state = HeaderTable().snapshot()
        state['dynamic_entries'] = [['not-base64!', 'also-bad!']]
        with pytest.raises(InvalidTableStateError):
            HeaderTable().restore(state)

    def test_restore_rejects_non_monotonic_eviction_sequence(self):
        tbl = HeaderTable()
        tbl.maxsize = 120
        for i in range(4):
            tbl.add(b'n%d' % i, b'x' * 60)
        state = tbl.snapshot()
        state['evictions'][1]['sequence'] = state['evictions'][0]['sequence']
        with pytest.raises(InvalidTableStateError):
            HeaderTable().restore(state)

    def test_restore_rejects_negative_sizes(self):
        state = HeaderTable().snapshot()
        state['maxsize'] = -1
        with pytest.raises(InvalidTableStateError):
            HeaderTable().restore(state)

        state = HeaderTable().snapshot()
        state['eviction_log_size'] = -1
        with pytest.raises(InvalidTableStateError):
            HeaderTable().restore(state)
