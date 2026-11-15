"""
Tests for persisted, resumable HPACK replay state across batches whose header
table capacity changes mid-stream.

The central scenario switches the dynamic table capacity from 4K down to 1K
and back up to 2K. Checkpoints are written both immediately after the
configuration event and after the following header block. A decoder rebuilt
from either checkpoint must produce the same decoded headers and the same
eviction list as an uninterrupted run, and must never revive an entry that was
evicted while the table was held at 1K.
"""
from __future__ import annotations

import pytest

from hpack import (
    Decoder,
    Encoder,
    InvalidTableStateError,
    dumps_checkpoint,
    load_checkpoint,
    loads_checkpoint,
    save_checkpoint,
)
from hpack.table import EVICTION_RESIZE


def _large_header(index: int) -> tuple[bytes, bytes]:
    # ~236 bytes per entry, so a handful of them overflow a 1K table.
    return b"n%02d" % index, (b"v%02d" % index) + b"x" * 200


def _build_batch_blocks() -> list[bytes]:
    """
    Encodes three header blocks with capacity changes 4K -> 1K -> 2K.

    The 1K and 2K capacity settings are carried by the second and third blocks
    respectively, mirroring configuration events arriving between batches.
    """
    encoder = Encoder()
    blocks = [
        encoder.encode([_large_header(i) for i in range(12)]),
    ]

    encoder.header_table_size = 1024
    blocks.append(encoder.encode([(b"after-shrink", b"1")]))

    encoder.header_table_size = 2048
    blocks.append(encoder.encode([(b"after-grow", b"2")]))
    return blocks


def _eviction_records(decoder: Decoder):
    return [
        (entry.sequence, entry.reason, entry.name, entry.value)
        for entry in decoder.header_table.eviction_log
    ]


class TestDecoderReplay:
    def test_replay_matches_uninterrupted_run(self):
        blocks = _build_batch_blocks()

        uninterrupted = Decoder()
        expected_headers = [uninterrupted.decode(block, raw=True) for block in blocks]
        expected_entries = list(uninterrupted.header_table.dynamic_entries)
        expected_evictions = _eviction_records(uninterrupted)

        # First checkpoint: straight after the configuration event (the block
        # that shrinks the table from 4K to 1K) has been decoded.
        interrupted = Decoder()
        first_headers = interrupted.decode(blocks[0], raw=True)
        second_headers = interrupted.decode(blocks[1], raw=True)
        after_event = interrupted.snapshot()
        sequence_after_event = interrupted.header_table.eviction_sequence

        resumed = Decoder()
        resumed.restore(after_event)
        assert list(resumed.header_table.dynamic_entries) == list(interrupted.header_table.dynamic_entries)

        # Second checkpoint: after the next block, which grows the table to 2K.
        third_headers = resumed.decode(blocks[2], raw=True)
        after_block = resumed.snapshot()
        assert resumed.header_table.maxsize == 2048

        rebuilt = Decoder()
        rebuilt.restore(after_block)

        assert [first_headers, second_headers, third_headers] == expected_headers
        assert list(rebuilt.header_table.dynamic_entries) == expected_entries
        assert _eviction_records(rebuilt) == expected_evictions
        # Nothing evicted by the 2K growth itself; sequence only advanced at 1K.
        assert rebuilt.header_table.eviction_sequence == sequence_after_event

    def test_first_decode_after_restore_does_not_revive_1k_evictions(self):
        blocks = _build_batch_blocks()

        reference = Decoder()
        for block in blocks:
            reference.decode(block, raw=True)
        evicted_at_1k = {
            entry.name
            for entry in reference.header_table.eviction_log
            if entry.reason == EVICTION_RESIZE
        }
        assert evicted_at_1k

        decoder = Decoder()
        decoder.decode(blocks[0], raw=True)
        decoder.decode(blocks[1], raw=True)  # table now at 1K, evictions applied

        restored = Decoder()
        restored.restore(decoder.snapshot())
        assert restored.header_table.maxsize == 1024

        # The next block raises the capacity to 2K. Restored state carries only
        # the entries that survived the 1K shrink.
        restored.decode(blocks[2], raw=True)
        assert restored.header_table.maxsize == 2048

        present = {name for name, _value in restored.header_table.dynamic_entries}
        assert evicted_at_1k.isdisjoint(present)

    def test_eviction_lists_align_at_each_checkpoint(self):
        blocks = _build_batch_blocks()

        uninterrupted = Decoder()
        uninterrupted.decode(blocks[0], raw=True)
        uninterrupted.decode(blocks[1], raw=True)
        first_checkpoint = uninterrupted.snapshot()
        first_sequence = uninterrupted.header_table.eviction_sequence
        uninterrupted.decode(blocks[2], raw=True)
        second_checkpoint = uninterrupted.snapshot()

        resumed = Decoder()
        resumed.restore(first_checkpoint)
        parallel = Decoder()
        parallel.restore(first_checkpoint)
        assert _eviction_records(resumed) == _eviction_records(parallel)

        resumed.decode(blocks[2], raw=True)
        assert _eviction_records(resumed) == _eviction_records(uninterrupted)
        assert resumed.header_table.evictions_since(first_sequence) == uninterrupted.header_table.evictions_since(first_sequence)

        rebuilt = Decoder()
        rebuilt.restore(second_checkpoint)
        assert _eviction_records(rebuilt) == _eviction_records(uninterrupted)

    def test_replay_across_additional_blocks_stays_in_sync(self):
        blocks = _build_batch_blocks()
        encoder = Encoder()
        encoder.header_table_size = 2048
        encoder.encode([(b"after-grow", b"2")])
        tail = [
            encoder.encode([(b"tail-%d" % i, b"z" * 100)])
            for i in range(4)
        ]

        interrupted = Decoder()
        for block in blocks:
            interrupted.decode(block, raw=True)
        restored = Decoder()
        restored.restore(interrupted.snapshot())

        reference = Decoder()
        for block in [*blocks, *tail]:
            reference.decode(block, raw=True)
        for block in tail:
            restored.decode(block, raw=True)

        assert list(restored.header_table.dynamic_entries) == list(reference.header_table.dynamic_entries)
        assert _eviction_records(restored) == _eviction_records(reference)

    def test_snapshot_is_json_compatible(self):
        import json

        decoder = Decoder()
        for block in _build_batch_blocks():
            decoder.decode(block, raw=True)
        # Round-tripping through plain JSON must not lose any state.
        rebuilt = Decoder()
        rebuilt.restore(json.loads(json.dumps(decoder.snapshot())))

        assert list(rebuilt.header_table.dynamic_entries) == list(decoder.header_table.dynamic_entries)
        assert _eviction_records(rebuilt) == _eviction_records(decoder)

    def test_restore_rejects_unknown_version(self):
        state = Decoder().snapshot()
        state["version"] = 999
        with pytest.raises(InvalidTableStateError):
            Decoder().restore(state)


class TestEncoderReplay:
    def test_pending_table_size_changes_survive_snapshot(self):
        encoder = Encoder()
        encoder.encode([(b"seed", b"entry")])
        encoder.header_table_size = 1024
        encoder.header_table_size = 2048
        assert encoder.table_size_changes == [1024, 2048]

        restored = Encoder()
        restored.restore(encoder.snapshot())
        assert restored.table_size_changes == [1024, 2048]

        # The restored encoder still owes both capacity changes to the next
        # block, and emits them in the original order.
        restored_block = restored.encode([(b"next", b"header")])
        uninterrupted = Encoder()
        uninterrupted.encode([(b"seed", b"entry")])
        uninterrupted.header_table_size = 1024
        uninterrupted.header_table_size = 2048
        expected_block = uninterrupted.encode([(b"next", b"header")])

        assert restored_block == expected_block
        assert restored.table_size_changes == []

    def test_restore_rejects_unknown_version(self):
        state = Encoder().snapshot()
        state["version"] = 999
        with pytest.raises(InvalidTableStateError):
            Encoder().restore(state)


class TestCheckpointPersistence:
    def test_decoder_checkpoint_roundtrip_on_disk(self, tmp_path):
        decoder = Decoder()
        for block in _build_batch_blocks():
            decoder.decode(block, raw=True)

        path = tmp_path / "decoder-checkpoint.json"
        save_checkpoint(path, decoder)
        rebuilt = load_checkpoint(path)

        assert isinstance(rebuilt, Decoder)
        assert list(rebuilt.header_table.dynamic_entries) == list(decoder.header_table.dynamic_entries)
        assert _eviction_records(rebuilt) == _eviction_records(decoder)

    def test_encoder_checkpoint_roundtrip_in_memory(self):
        encoder = Encoder()
        encoder.header_table_size = 1024
        rebuilt = loads_checkpoint(dumps_checkpoint(encoder))

        assert isinstance(rebuilt, Encoder)
        assert rebuilt.table_size_changes == [1024]

    def test_corrupt_checkpoints_are_rejected(self):
        with pytest.raises(InvalidTableStateError):
            loads_checkpoint("not-json")
        with pytest.raises(InvalidTableStateError):
            loads_checkpoint('{"kind": "unknown", "state": {}}')
        with pytest.raises(InvalidTableStateError):
            loads_checkpoint('{"kind": "decoder", "state": {"version": 42}}')

    def test_non_coder_object_is_rejected(self):
        with pytest.raises(TypeError):
            dumps_checkpoint(object())
