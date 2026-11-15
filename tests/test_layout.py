# -*- coding: utf-8 -*-
"""
Tests for the HPACK encoding-layout analysis module.
"""
from __future__ import annotations

import pytest

from hpack import Decoder, Encoder, FieldLayout, LayoutRecorder, parse_header_block_layout
from hpack.exceptions import HPACKDecodingError, HPACKError
from hpack.layout import (
    INDEXED,
    LITERAL_INCREMENTAL,
    LITERAL_NEVER_INDEXED,
    LITERAL_WITHOUT_INDEXING,
    TABLE_SIZE_UPDATE,
)


class TestParseHeaderBlockLayout:
    def test_indexed_field(self):
        # Indexed field, index 2 (static table): single byte 0x82.
        (field,) = parse_header_block_layout(b"\x82")
        assert field.kind == INDEXED
        assert field.span == (0, 1)
        assert field.index == (0, 1)
        assert field.name is None
        assert field.value is None

    def test_indexed_field_multibyte_index(self):
        # Indexed field, index 200: prefix saturates and a continuation byte
        # follows.
        data = b"\xff\x49"
        (field,) = parse_header_block_layout(data)
        assert field.kind == INDEXED
        assert field.span == (0, 2)
        assert field.index == (0, 2)

    def test_literal_with_incremental_indexing_literal_name(self):
        # Literal with incremental indexing, literal name "a", value "b",
        # no Huffman coding.
        data = b"\x40\x01a\x01b"
        (field,) = parse_header_block_layout(data)
        assert field.kind == LITERAL_INCREMENTAL
        assert field.span == (0, 5)
        assert field.index == (0, 1)
        assert field.name_length == (1, 2)
        assert field.name == (2, 3)
        assert field.value_length == (3, 4)
        assert field.value == (4, 5)
        assert field.name_huffman is False
        assert field.value_huffman is False
        assert data[field.name[0]:field.name[1]] == b"a"
        assert data[field.value[0]:field.value[1]] == b"b"

    def test_literal_with_indexed_name(self):
        # Literal with incremental indexing, indexed name 4, value "GET".
        data = b"\x44\x03GET"
        (field,) = parse_header_block_layout(data)
        assert field.kind == LITERAL_INCREMENTAL
        assert field.index == (0, 1)
        assert field.name_length is None
        assert field.name is None
        assert field.value_length == (1, 2)
        assert field.value == (2, 5)

    def test_literal_without_indexing(self):
        data = b"\x00\x01a\x01b"
        (field,) = parse_header_block_layout(data)
        assert field.kind == LITERAL_WITHOUT_INDEXING
        assert field.span == (0, 5)

    def test_literal_never_indexed(self):
        data = b"\x10\x01a\x01b"
        (field,) = parse_header_block_layout(data)
        assert field.kind == LITERAL_NEVER_INDEXED
        assert field.span == (0, 5)

    def test_table_size_update(self):
        (field,) = parse_header_block_layout(b"\x3f\xe1\x1f")
        assert field.kind == TABLE_SIZE_UPDATE
        assert field.span == (0, 3)
        assert field.index == (0, 3)
        assert field.value is None

    def test_huffman_flags(self):
        # Huffman-coded name and value have the high bit set on their length
        # bytes. "www.example.com" Huffman-encodes to 12 bytes.
        encoder = Encoder()
        data = encoder.encode([(b":path", b"/some-path")], huffman=True)
        fields = parse_header_block_layout(data)
        assert len(fields) == 1
        field = fields[0]
        assert field.value_huffman is True
        # The recorded span must cover exactly the compressed bytes.
        compressed = data[field.value[0]:field.value[1]]
        assert len(compressed) == field.value[1] - field.value[0]

    def test_multiple_fields_keep_source_order(self):
        encoder = Encoder()
        data = encoder.encode(
            [(b":method", b"GET"), (b":path", b"/"), (b"custom", b"value")],
            huffman=True,
        )
        fields = parse_header_block_layout(data)
        assert len(fields) == 3
        # Spans tile the block contiguously, in order.
        offset = 0
        for field in fields:
            assert field.span[0] == offset
            offset = field.span[1]
        assert offset == len(data)

    def test_round_trips_against_decoder(self):
        encoder = Encoder()
        decoder = Decoder()
        headers = [(b":method", b"GET"), (b"custom-key", b"custom-value")]
        data = encoder.encode(headers, huffman=True)
        fields = parse_header_block_layout(data)
        assert len(fields) == len(decoder.decode(data, raw=True))

    def test_truncated_block_raises(self):
        with pytest.raises(HPACKDecodingError, match="Truncated header block"):
            parse_header_block_layout(b"\x40\x01a")

    def test_truncated_string_raises(self):
        with pytest.raises(HPACKDecodingError, match="Truncated header block"):
            parse_header_block_layout(b"\x40\x05ab")

    def test_truncated_prefix_integer_raises(self):
        # The 4-bit prefix saturates and the continuation byte is missing.
        with pytest.raises(HPACKDecodingError, match="Truncated header block"):
            parse_header_block_layout(b"\x0f")

    def test_oversized_integer_raises(self):
        with pytest.raises(HPACKDecodingError, match="too long"):
            parse_header_block_layout(b"\xff\xff\xff\xff\xff\xff\x01")

    def test_empty_block(self):
        assert parse_header_block_layout(b"") == []


class TestLayoutRecorder:
    def test_fragment_boundaries_do_not_matter(self):
        encoder = Encoder()
        data = encoder.encode(
            [(b":method", b"GET"), (b":path", b"/index.html"), (b"custom", b"value")],
            huffman=True,
        )
        expected = parse_header_block_layout(data)

        # Every possible single split point must produce the same layouts.
        for cut in range(len(data) + 1):
            recorder = LayoutRecorder()
            recorder.feed(data[:cut])
            recorder.feed(data[cut:])
            assert recorder.finish() == expected
            assert recorder.reassembled() == expected

    def test_byte_at_a_time_feeding(self):
        encoder = Encoder()
        data = encoder.encode([(b"custom-key", b"custom-value")], huffman=True)
        recorder = LayoutRecorder()
        for byte in data:
            recorder.feed(bytes([byte]))
        assert recorder.finish() == parse_header_block_layout(data)

    def test_pending_bytes_kept_until_block_ends(self):
        # A literal field whose value length integer is split across
        # fragments: nothing is judged incomplete until finish().
        data = b"\x40\x01a\x7f\x01" + b"x" * 0x80  # value length 0x7f + 1
        recorder = LayoutRecorder()
        assert recorder.feed(data[:4]) == []
        assert recorder.pending == data[:4]
        assert recorder.feed(data[4:5]) == []
        parsed = recorder.feed(data[5:])
        assert len(parsed) == 1
        assert recorder.pending == b""
        assert recorder.finish() == parsed

    def test_finish_with_pending_bytes_raises(self):
        recorder = LayoutRecorder()
        recorder.feed(b"\x40\x01a\x05xy")
        with pytest.raises(HPACKDecodingError, match="Truncated header block"):
            recorder.finish()

    def test_split_multibyte_index(self):
        # An indexed field whose index continuation byte arrives in a later
        # fragment stays pending, not incomplete.
        recorder = LayoutRecorder()
        assert recorder.feed(b"\xff") == []
        assert recorder.pending == b"\xff"
        parsed = recorder.feed(b"\x49")
        assert parsed == [FieldLayout(kind=INDEXED, span=(0, 2), index=(0, 2))]
        assert recorder.finish() == parsed

    def test_split_table_size_update(self):
        recorder = LayoutRecorder()
        assert recorder.feed(b"\x3f\xe1") == []
        parsed = recorder.feed(b"\x1f")
        assert parsed == [FieldLayout(kind=TABLE_SIZE_UPDATE, span=(0, 3), index=(0, 3))]
        assert recorder.finish() == parsed

    def test_feed_after_finish_raises(self):
        recorder = LayoutRecorder()
        recorder.feed(b"\x82")
        recorder.finish()
        with pytest.raises(HPACKError, match="after the header block has ended"):
            recorder.feed(b"\x82")

    def test_fields_property_returns_copies(self):
        recorder = LayoutRecorder()
        recorder.feed(b"\x82")
        fields = recorder.fields
        assert fields == [FieldLayout(kind=INDEXED, span=(0, 1), index=(0, 1))]
        fields.clear()
        assert len(recorder.fields) == 1

    def test_ended_property(self):
        recorder = LayoutRecorder()
        assert recorder.ended is False
        recorder.finish()
        assert recorder.ended is True

    def test_reassembled_matches_incremental_fields(self):
        encoder = Encoder()
        blocks = [
            encoder.encode([(b":method", b"GET")]),
            encoder.encode([(b":method", b"GET"), (b"custom", b"v" * 300)]),
        ]
        data = b"".join(blocks)
        recorder = LayoutRecorder()
        # Feed in uneven chunks that cut through continuation bytes.
        for chunk in (data[:1], data[1:3], data[3:7], data[7:]):
            recorder.feed(chunk)
        incremental = recorder.finish()
        assert recorder.reassembled() == incremental
        assert recorder.reassembled() == parse_header_block_layout(data)
