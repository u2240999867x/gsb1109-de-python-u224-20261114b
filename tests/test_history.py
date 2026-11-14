import json
import os
from binascii import unhexlify

import pytest

from hpack import Decoder, Encoder, HPACKDecodingError, InvalidTableSizeError, OversizedHeaderListError
from hpack.history import (
    REQUEST,
    RESPONSE,
    BlockDecoder,
    BlockRef,
    BlockSequenceError,
    CompressionContext,
    DecodedBlock,
    DecodedField,
    FieldRow,
    HeaderBlockRef,
    Representation,
)


def encode_story(huffman=False):
    encoder = Encoder()
    blocks = [
        encoder.encode([(":method", "GET"), (":path", "/"), ("x-custom", "hello")], huffman=huffman),
        encoder.encode([(":method", "GET"), (":path", "/"), ("x-custom", "hello"), ("x-other", "world")], huffman=huffman),
        encoder.encode([("x-custom", "hello")], huffman=huffman),
    ]
    return blocks


def decode_with_batch(blocks):
    decoder = BlockDecoder()
    decoded = decoder.decode_all(
        (0, REQUEST, stream_id, sequence, data)
        for sequence, (stream_id, data) in enumerate([(1, blocks[0]), (3, blocks[1]), (5, blocks[2])])
    )
    return decoder, decoded


class TestBlockDecoderParity:
    @pytest.mark.parametrize("huffman", [False, True])
    def test_fields_match_standard_decoder(self, huffman):
        blocks = encode_story(huffman)
        standard = Decoder()
        expected = [standard.decode(block, raw=True) for block in blocks]

        _, decoded = decode_with_batch(blocks)

        actual = [[(field.name, field.value) for field in block.fields] for block in decoded]
        assert actual == [list(block) for block in expected]
        assert all(isinstance(field.name, bytes) for block in decoded for field in block.fields)

    @pytest.mark.parametrize("huffman", [False, True])
    def test_long_table_shape_and_order(self, huffman):
        blocks = encode_story(huffman)
        decoder, _ = decode_with_batch(blocks)

        rows = decoder.fields
        assert len(rows) == 3 + 4 + 1
        assert all(isinstance(row, FieldRow) for row in rows)
        assert [row.block_sequence for row in rows] == [0, 0, 0, 1, 1, 1, 1, 2]
        assert [row.field_index for row in rows[:3]] == [0, 1, 2]
        assert rows[0].stream_id == 1
        assert rows[3].stream_id == 3
        assert rows[7].stream_id == 5
        assert all(row.connection_generation == 0 for row in rows)
        assert all(row.direction == REQUEST for row in rows)

    def test_representations_are_labeled(self):
        blocks = encode_story()
        decoder, _ = decode_with_batch(blocks)
        block0, block1, block2 = decoder.blocks

        assert [field.representation for field in block0.fields] == [
            Representation.INDEXED,
            Representation.INDEXED,
            Representation.INCREMENTAL,
        ]
        assert block1.fields[2].representation is Representation.INDEXED
        assert block1.fields[3].representation is Representation.INCREMENTAL
        assert block2.fields[0].representation is Representation.INDEXED

    def test_literal_without_and_never_indexed(self):
        encoder = Encoder()
        indexed_block = encoder.encode([("x-token", "abc")], huffman=False)
        no_index_block = b"\x00\x07x-token\x03abc"
        never_index_block = b"\x10\x07x-token\x03abc"

        decoder = BlockDecoder()
        first = decoder.decode_block(indexed_block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)
        second = decoder.decode_block(no_index_block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=1)
        third = decoder.decode_block(never_index_block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=2)

        assert first.fields[0].representation is Representation.INCREMENTAL
        assert second.fields[0].representation is Representation.WITHOUT_INDEXING
        assert third.fields[0].representation is Representation.NEVER_INDEXED
        assert second.fields[0].name_ref is None and second.fields[0].value_ref is None
        assert third.fields[0].name_ref is None and third.fields[0].value_ref is None
        # Without-indexing / never-indexed fields do not populate the dynamic table.
        assert len(decoder.context(0, REQUEST).header_table.dynamic_entries) == 1


class TestProvenance:
    def test_static_reference_has_no_origin_block(self):
        blocks = encode_story()
        decoder, _ = decode_with_batch(blocks)
        method_field = decoder.blocks[0].fields[0]

        ref = method_field.name_ref
        assert isinstance(ref, HeaderBlockRef)
        assert ref.table_kind == "static"
        assert ref.table_index == 2
        assert ref.block is None
        assert ref.field_index is None
        assert method_field.value_ref is ref

    def test_dynamic_reference_points_at_introducing_block(self):
        blocks = encode_story()
        decoder, _ = decode_with_batch(blocks)
        referenced = decoder.blocks[1].fields[2]

        ref = referenced.name_ref
        assert referenced.value_ref is ref
        assert ref.table_kind == "dynamic"
        assert (ref.name, ref.value) == (b"x-custom", b"hello")
        assert ref.block == BlockRef(
            connection_generation=0,
            direction=REQUEST,
            stream_id=1,
            sequence=0,
        )
        assert ref.field_index == 2

    def test_indexed_name_literal_only_references_the_name(self):
        encoder = Encoder()
        first = encoder.encode([("x-shared", "one")], huffman=False)
        block = encoder.encode([("x-shared", "two")], huffman=False)

        decoder = BlockDecoder()
        decoder.decode_block(first, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)
        decoded = decoder.decode_block(block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=1)
        field = decoded.fields[0]

        assert (field.name, field.value) == (b"x-shared", b"two")
        assert field.representation is Representation.INCREMENTAL
        assert field.name_ref is not None
        assert field.name_ref.table_kind == "dynamic"
        assert field.name_ref.block.sequence == 0
        assert field.value_ref is None

    def test_provenance_survives_dynamic_table_eviction(self):
        encoder = Encoder()
        seed = encoder.encode([("x-evict", "seed")], huffman=False)

        context = CompressionContext(0, REQUEST)
        first = context.decode_block(seed, stream_id=1, sequence=0)
        assert first.fields[0].representation is Representation.INCREMENTAL

        # Shrink the dynamic table below the entry size, forcing eviction.
        context.decode_block(b"\x21", stream_id=1, sequence=1)  # table size update to 1
        assert len(context.header_table.dynamic_entries) == 0

        # The reference captured before eviction is still a complete snapshot.
        assert (first.fields[0].name, first.fields[0].value) == (b"x-evict", b"seed")

        # Grow the table back and re-introduce the entry from a new block.
        context.decode_block(b"\x3f\xe1\x1f", stream_id=1, sequence=2)  # size update to 4096
        context.decode_block(seed, stream_id=1, sequence=3)
        assert len(context.header_table.dynamic_entries) == 1

        # Index 62 is the first dynamic table entry.
        referenced = context.decode_block(b"\xbe", stream_id=1, sequence=4)
        ref = referenced.fields[0].name_ref
        assert referenced.fields[0].representation is Representation.INDEXED
        assert ref.table_kind == "dynamic"
        assert ref.table_index == 62
        assert ref.block == BlockRef(0, REQUEST, 1, 3)
        assert ref.field_index == 0

    def test_each_reference_snapshots_entry_bytes(self):
        encoder = Encoder()
        seed = encoder.encode([("x-snap", "v1")], huffman=False)
        reuse = encoder.encode([("x-snap", "v1")], huffman=False)

        decoder = BlockDecoder()
        decoder.decode_block(seed, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)
        decoded = decoder.decode_block(reuse, connection_generation=0, direction=REQUEST, stream_id=1, sequence=1)

        ref = decoded.fields[0].name_ref
        assert (ref.name, ref.value) == (b"x-snap", b"v1")
        assert isinstance(decoded, DecodedBlock)
        assert all(isinstance(field, DecodedField) for field in decoded.fields)


class TestContextIsolation:
    @pytest.fixture
    def two_blocks(self):
        encoder = Encoder()
        first = encoder.encode([(":method", "GET"), ("x-a", "1")], huffman=False)
        second = encoder.encode([("x-a", "1")], huffman=False)
        return first, second

    def test_opposite_directions_keep_separate_tables(self, two_blocks):
        first, second = two_blocks
        decoder = BlockDecoder()
        decoder.decode_block(first, connection_generation=0, direction=REQUEST, stream_id=7, sequence=0)

        request_context = decoder.context(0, REQUEST)
        response_context = decoder.context(0, RESPONSE)
        assert request_context is not response_context
        assert len(request_context.header_table.dynamic_entries) == 1
        assert len(response_context.header_table.dynamic_entries) == 0

        # The dynamic reference in `second` only resolves within the context
        # whose dictionary actually contains the entry.
        request_context.decode_block(second, stream_id=7, sequence=1)
        with pytest.raises(HPACKDecodingError):
            response_context.decode_block(second, stream_id=7, sequence=0)

    def test_reconnect_generations_keep_separate_tables(self, two_blocks):
        first, second = two_blocks
        decoder = BlockDecoder()
        decoder.decode_block(first, connection_generation=0, direction=REQUEST, stream_id=7, sequence=0)

        assert decoder.context(0, REQUEST) is not decoder.context(1, REQUEST)
        decoder.context(0, REQUEST).decode_block(second, stream_id=7, sequence=1)
        with pytest.raises(HPACKDecodingError):
            decoder.context(1, REQUEST).decode_block(second, stream_id=7, sequence=0)

    def test_streams_share_one_directional_dictionary(self, two_blocks):
        first, second = two_blocks
        decoder = BlockDecoder()
        decoder.decode_block(first, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)
        # Same context, different stream: the earlier entry must resolve.
        decoded = decoder.decode_block(second, connection_generation=0, direction=REQUEST, stream_id=99, sequence=1)
        assert (decoded.fields[0].name, decoded.fields[0].value) == (b"x-a", b"1")
        assert decoded.fields[0].name_ref.block.stream_id == 1

    def test_interleaving_two_connections_does_not_change_fields(self):
        encoder_a = Encoder()
        encoder_b = Encoder()
        a0 = encoder_a.encode([(":method", "GET"), ("x-a", "1")], huffman=False)
        a1 = encoder_a.encode([("x-a", "1"), ("x-b", "2")], huffman=False)
        b0 = encoder_b.encode([(":method", "POST"), ("x-a", "9")], huffman=False)
        b1 = encoder_b.encode([("x-a", "9")], huffman=False)

        interleaved = [
            (0, REQUEST, 7, 0, a0),
            (1, REQUEST, 7, 0, b0),
            (0, REQUEST, 7, 1, a1),
            (1, REQUEST, 7, 1, b1),
        ]
        swapped = [
            (1, REQUEST, 7, 0, b0),
            (0, REQUEST, 7, 0, a0),
            (1, REQUEST, 7, 1, b1),
            (0, REQUEST, 7, 1, a1),
        ]

        first = BlockDecoder()
        second = BlockDecoder()
        first.decode_all(interleaved)
        second.decode_all(swapped)

        def signature(decoder):
            return [
                (
                    row.connection_generation,
                    row.direction,
                    row.stream_id,
                    row.block_sequence,
                    row.field_index,
                    row.name,
                    row.value,
                    row.representation,
                    None if row.name_ref is None else row.name_ref.table_index,
                )
                for row in decoder.fields
            ]

        assert signature(first) == signature(second)
        # Deterministic ordering is by (generation, direction, sequence), not arrival.
        assert [row.connection_generation for row in first.fields[:3]] == [0, 0, 0]

    def test_arrival_order_does_not_affect_long_table(self):
        # Two independent connections, each with internally ordered blocks.
        # Swapping which connection's blocks arrive first must not change the
        # deterministically ordered long table.
        encoder_a = Encoder()
        encoder_b = Encoder()
        connection_a = [
            (0, REQUEST, 1, 0, encoder_a.encode([(":method", "GET"), ("x-a", "1")], huffman=False)),
            (0, REQUEST, 1, 1, encoder_a.encode([("x-a", "1")], huffman=False)),
        ]
        connection_b = [
            (1, REQUEST, 1, 0, encoder_b.encode([(":method", "POST"), ("x-b", "2")], huffman=False)),
            (1, REQUEST, 1, 1, encoder_b.encode([("x-b", "2")], huffman=False)),
        ]

        first = BlockDecoder()
        first.decode_all(connection_a + connection_b)
        second = BlockDecoder()
        second.decode_all(connection_b + connection_a)

        def signature(decoder):
            return [
                (
                    row.connection_generation,
                    row.direction,
                    row.block_sequence,
                    row.field_index,
                    row.name,
                    row.value,
                )
                for row in decoder.fields
            ]

        assert signature(first) == signature(second)


class TestSequencingAndErrors:
    def test_duplicate_sequence_rejected(self):
        encoder = Encoder()
        block = encoder.encode([(":method", "GET")], huffman=False)
        decoder = BlockDecoder()
        decoder.decode_block(block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)
        with pytest.raises(BlockSequenceError):
            decoder.decode_block(block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)

    def test_out_of_order_sequence_rejected(self):
        encoder = Encoder()
        block = encoder.encode([(":method", "GET")], huffman=False)
        context = CompressionContext(0, REQUEST)
        context.decode_block(block, stream_id=1, sequence=1)
        with pytest.raises(BlockSequenceError):
            context.decode_block(block, stream_id=1, sequence=0)

    def test_invalid_dynamic_index_raises(self):
        decoder = BlockDecoder()
        with pytest.raises(HPACKDecodingError):
            decoder.decode_block(b"\xbe", connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)

    def test_zero_index_raises(self):
        decoder = BlockDecoder()
        with pytest.raises(HPACKDecodingError):
            decoder.decode_block(b"\x80", connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)

    def test_truncated_block_raises(self):
        decoder = BlockDecoder()
        with pytest.raises(HPACKDecodingError):
            decoder.decode_block(b"\x40\x0aabc", connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)

    def test_truncated_literal_value_raises(self):
        decoder = BlockDecoder()
        # Incremental literal with an indexed name (static index 4) but a value
        # length that runs past the end of the block.
        with pytest.raises(HPACKDecodingError):
            decoder.decode_block(b"\x44\x05ab", connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)

    def test_entry_larger_than_table_clears_dynamic_table(self):
        decoder = BlockDecoder()
        # Shrink to one byte, then add an entry whose 34-byte size cannot fit.
        block = b"\x21\x40\x01a\x01b"
        decoded = decoder.decode_block(block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)
        assert [(field.name, field.value) for field in decoded.fields] == [(b"a", b"b")]
        assert len(decoder.context(0, REQUEST).header_table.dynamic_entries) == 0

    def test_resize_to_zero_clears_dynamic_table(self):
        encoder = Encoder()
        seed = encoder.encode([("x-zero", "v")], huffman=False)
        context = CompressionContext(0, REQUEST)
        context.decode_block(seed, stream_id=1, sequence=0)
        assert len(context.header_table.dynamic_entries) == 1

        context.decode_block(b"\x20", stream_id=1, sequence=1)  # table size update to 0
        assert len(context.header_table.dynamic_entries) == 0
        assert context.header_table.maxsize == 0

    def test_table_size_update_is_recorded_and_enforced(self):
        encoder = Encoder()
        encoder.header_table_size = 256
        block = encoder.encode([("x-size", "v")], huffman=False)

        decoder = BlockDecoder(max_allowed_table_size=4096)
        decoded = decoder.decode_block(block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)
        assert decoded.table_size_updates == (256,)
        assert decoder.context(0, REQUEST).header_table.maxsize == 256

        strict = BlockDecoder(max_allowed_table_size=128)
        with pytest.raises(InvalidTableSizeError):
            strict.decode_block(block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)

    def test_oversized_header_list_rejected(self):
        encoder = Encoder()
        block = encoder.encode([("x-big", "x" * 200)], huffman=False)
        decoder = BlockDecoder(max_header_list_size=64)
        with pytest.raises(OversizedHeaderListError):
            decoder.decode_block(block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)

    def test_table_size_update_after_header_rejected(self):
        encoder = Encoder()
        block = encoder.encode([(":method", "GET")], huffman=False) + b"\x3f\x21"
        decoder = BlockDecoder()
        with pytest.raises(HPACKDecodingError):
            decoder.decode_block(block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)


FIXTURE_ROOT = os.path.join(os.path.dirname(__file__), "test_fixtures")


def _story_files():
    files = []
    for directory in sorted(os.listdir(FIXTURE_ROOT)):
        path = os.path.join(FIXTURE_ROOT, directory)
        if not os.path.isdir(path) or directory == "raw-data":
            continue
        for name in sorted(os.listdir(path)):
            files.append(pytest.param(os.path.join(path, name), id=f"{directory}/{name}"))
    return files


class TestFixtureParity:
    @pytest.mark.parametrize("story_path", _story_files())
    def test_matches_standard_decoder_on_stories(self, story_path):
        with open(story_path, encoding="utf-8") as handle:
            story = json.load(handle)
        if story.get("draft") != 9:
            pytest.skip("fixture targets a different HPACK draft")

        standard = Decoder()
        batch = BlockDecoder()
        for case in story["cases"]:
            wire = unhexlify(case["wire"])
            table_size = case.get("header_table_size")
            if table_size is not None:
                standard.header_table_size = table_size
                batch.context(0, REQUEST).max_allowed_table_size = table_size

            expected = standard.decode(wire, raw=True)
            decoded = batch.decode_block(
                wire,
                connection_generation=0,
                direction=REQUEST,
                stream_id=1,
                sequence=case["seqno"],
            )
            assert [(field.name, field.value) for field in decoded.fields] == [tuple(pair) for pair in expected]


class TestContainerBehavior:
    def test_blocks_property_and_dunder_helpers(self):
        blocks = encode_story()
        decoder, decoded = decode_with_batch(blocks)

        assert list(decoder) == list(decoder.blocks) == list(decoded)
        assert len(decoder) == 3
        assert [block.ref.sequence for block in decoder.blocks] == [0, 1, 2]
        assert all(isinstance(block, DecodedBlock) for block in decoder)

    def test_contexts_grouped_by_generation_and_direction(self):
        encoder = Encoder()
        block = encoder.encode([(":method", "GET")], huffman=False)
        decoder = BlockDecoder()
        decoder.decode_block(block, connection_generation=0, direction=REQUEST, stream_id=1, sequence=0)
        decoder.decode_block(block, connection_generation=0, direction=RESPONSE, stream_id=1, sequence=0)
        decoder.decode_block(block, connection_generation=1, direction=REQUEST, stream_id=1, sequence=0)

        assert sorted(decoder.contexts) == [(0, REQUEST), (0, RESPONSE), (1, REQUEST)]
        assert decoder.context(0, REQUEST) is decoder.contexts[(0, REQUEST)]

    def test_block_ref_immutable(self):
        ref = BlockRef(0, REQUEST, 1, 0)
        with pytest.raises(AttributeError):
            ref.sequence = 2  # type: ignore[misc]
