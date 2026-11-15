"""
Tests for forensic, best-effort decoding of damaged header blocks.
"""
import pytest

from hpack import Decoder, Encoder, HeaderTuple, NeverIndexedHeaderTuple, decode_with_recovery
from hpack.exceptions import InvalidTableSizeError
from hpack.hpack import encode_integer
from hpack.recovery import (
    EVENT_INVALID_INDEX,
    EVENT_TABLE_CLEAR,
    EVENT_TRUNCATED,
    EVENT_UNRESOLVABLE_NAME,
    PROVENANCE_POST_CLEAR_INSERT,
    PROVENANCE_POST_GAP_INSERT,
    PROVENANCE_PRE_GAP,
    REP_INDEXED,
    REP_LITERAL_INCREMENTAL,
    REP_LITERAL_NEVER_INDEXED,
    REP_LITERAL_WITHOUT_INDEXING,
    SOURCE_DYNAMIC,
    SOURCE_LITERAL,
    SOURCE_STATIC,
    RecoveredHeader,
    RecoveredNeverIndexedHeader,
    RecoveryEvent,
    RecoveryEvidence,
)


def table_size_update(size):
    field = encode_integer(size, 5)
    field[0] |= 0x20
    return bytes(field)


class TestRecoveryClassification:
    def test_static_item_after_gap_is_salvaged(self):
        result = Decoder().recover(b"\xbe\x82")

        assert result.gap_detected
        assert len(result.gap_events) == 1
        assert result.gap_events[0].kind == EVENT_INVALID_INDEX
        assert result.gap_events[0].start == 0
        assert result.gap_events[0].end == 1

        static_headers = result.static_headers
        assert len(static_headers) == 1
        header = static_headers[0]
        assert header == (b":method", b"GET")
        evidence = header.evidence
        assert evidence.representation == REP_INDEXED
        assert evidence.name_source == SOURCE_STATIC
        assert evidence.value_source == SOURCE_STATIC
        assert evidence.static_index == 2
        assert evidence.dynamic_index is None
        assert (evidence.start, evidence.end) == (1, 2)

    def test_complete_literal_after_gap_is_salvaged(self):
        block = b"\xbe\x00\x0acustom-key\x0dcustom-header"
        result = Decoder().recover(block)

        literal_headers = result.literal_headers
        assert len(literal_headers) == 1
        header = literal_headers[0]
        assert header == (b"custom-key", b"custom-header")
        evidence = header.evidence
        assert evidence.representation == REP_LITERAL_WITHOUT_INDEXING
        assert evidence.name_source == SOURCE_LITERAL
        assert evidence.value_source == SOURCE_LITERAL
        assert evidence.name_huffman is False
        assert evidence.value_huffman is False
        assert evidence.name_span == (3, 13)
        assert evidence.value_span == (14, 27)
        assert (evidence.start, evidence.end) == (1, 27)

    def test_literal_with_static_name_is_literal_value(self):
        # Never-indexed header with static name index 4 (:path), literal value.
        result = Decoder().recover(b"\xbe\x14\x0c/sample/path")

        assert len(result.literal_headers) == 1
        header = result.literal_headers[0]
        assert header == (b":path", b"/sample/path")
        evidence = header.evidence
        assert evidence.representation == REP_LITERAL_NEVER_INDEXED
        assert evidence.name_source == SOURCE_STATIC
        assert evidence.static_index == 4
        assert evidence.value_source == SOURCE_LITERAL
        assert evidence.value_span == (3, 15)
        assert isinstance(header, RecoveredNeverIndexedHeader)
        assert header.indexable is False
        assert isinstance(header.as_tuple(), NeverIndexedHeaderTuple)

    def test_huffman_literal_records_spans_and_flags(self):
        encoder = Encoder()
        wire = encoder.encode([(b"custom-key", b"custom-header")])
        result = Decoder().recover(b"\xbe" + wire)

        header = result.literal_headers[0]
        assert header == (b"custom-key", b"custom-header")
        evidence = header.evidence
        assert evidence.name_huffman is True
        assert evidence.value_huffman is True
        assert wire[evidence.name_span[0] : evidence.name_span[1]]
        assert wire[evidence.value_span[0] : evidence.value_span[1]]

    def test_dynamic_reference_only_after_reinsertion(self):
        block = b"".join(
            [
                b"\xbe",  # gap: index 62 does not exist yet
                b"\x40\x04name\x03val",  # post-gap insertion
                b"\xbe",  # index 62 now resolves to the new entry
            ],
        )
        result = Decoder().recover(block)

        dynamic_headers = result.dynamic_reference_headers
        assert len(dynamic_headers) == 1
        header = dynamic_headers[0]
        assert header == (b"name", b"val")
        evidence = header.evidence
        assert evidence.representation == REP_INDEXED
        assert evidence.name_source == SOURCE_DYNAMIC
        assert evidence.value_source == SOURCE_DYNAMIC
        assert evidence.dynamic_index == 62
        assert evidence.table_entry_provenance == PROVENANCE_POST_GAP_INSERT
        assert result.fresh_start is False

    def test_pre_gap_entry_keeps_pre_gap_provenance(self):
        decoder = Decoder()
        decoder.decode(b"\x40\x04name\x03val")  # dynamic index 62

        result = decoder.recover(b"\xbe\xbf\x82")  # 62 ok, 63 invalid, then static

        dynamic_headers = result.dynamic_reference_headers
        assert len(dynamic_headers) == 1
        assert dynamic_headers[0] == (b"name", b"val")
        assert dynamic_headers[0].evidence.table_entry_provenance == PROVENANCE_PRE_GAP
        assert result.gap_events[0].kind == EVENT_INVALID_INDEX

    def test_views_partition_all_recovered_headers(self):
        block = b"".join(
            [
                b"\xbe",
                b"\x82",  # static
                b"\x00\x0acustom-key\x0dcustom-header",  # literal
                b"\x40\x04name\x03val",  # insertion
                b"\xbe",  # dynamic reference
            ],
        )
        result = Decoder().recover(block)

        assert len(result.static_headers) == 1
        assert len(result.literal_headers) == 2
        assert len(result.dynamic_reference_headers) == 1
        assert (
            len(result.static_headers)
            + len(result.literal_headers)
            + len(result.dynamic_reference_headers)
            == len(result.headers)
        )
        assert [tuple(header) for header in result.headers] == [
            (b":method", b"GET"),
            (b"custom-key", b"custom-header"),
            (b"name", b"val"),
            (b"name", b"val"),
        ]


class TestRecoveryFreshStart:
    def test_explicit_clear_creates_fresh_start(self):
        block = b"".join(
            [
                b"\xbe",
                b"\x40\x04name\x03val",
                b"\xbe",
                table_size_update(0),  # explicit clear
                table_size_update(4096),
                b"\x40\x04post\x04data",
                b"\xbe",
            ],
        )
        result = Decoder().recover(block)

        assert result.fresh_start is True
        assert result.fresh_start_offset is not None
        assert len(result.clear_events) == 1
        assert result.clear_events[0].kind == EVENT_TABLE_CLEAR

        post_clear = result.dynamic_reference_headers[-1]
        assert post_clear == (b"post", b"data")
        assert post_clear.evidence.table_entry_provenance == PROVENANCE_POST_CLEAR_INSERT

    def test_post_gap_insertion_alone_is_not_fresh_start(self):
        result = Decoder().recover(b"\xbe\x40\x04name\x03val\xbe")
        assert result.fresh_start is False
        assert result.fresh_start_offset is None
        assert result.clear_events == []

    def test_zero_size_update_on_empty_table_is_still_a_fresh_start(self):
        result = Decoder().recover(b"\xbe" + table_size_update(0) + b"\x82")
        assert result.fresh_start is True
        assert result.clear_events[0].kind == EVENT_TABLE_CLEAR
        assert result.static_headers[0] == (b":method", b"GET")

    def test_non_clearing_size_update_is_not_a_fresh_start(self):
        result = Decoder().recover(b"\xbe" + table_size_update(4096) + b"\x82")
        assert result.fresh_start is False
        assert result.clear_events == []
        assert result.static_headers[0] == (b":method", b"GET")


class TestRecoveryResynchronization:
    def test_unresolvable_name_index_skips_whole_representation(self):
        # Incremental literal with single-byte invalid name index 62, then a
        # 15 byte literal value, then a static indexed field.
        block = b"\x7e\x0fwww.example.com\x82"
        result = Decoder().recover(block)

        assert len(result.gap_events) == 1
        assert result.gap_events[0].kind == EVENT_UNRESOLVABLE_NAME
        assert result.gap_events[0].start == 0
        assert result.gap_events[0].end == 17
        assert [tuple(header) for header in result.headers] == [(b":method", b"GET")]

    def test_truncated_block_ends_scan_but_keeps_earlier_fields(self):
        result = decode_with_recovery(b"\xbe\x82\x00\x0acustom-key")

        assert result.static_headers[0] == (b":method", b"GET")
        terminal = result.gap_events[-1]
        assert terminal.kind == EVENT_TRUNCATED
        assert terminal.start == 2

    def test_valid_block_has_no_gap(self):
        result = Decoder().recover(b"\x82\x86\x84")
        assert result.gap_detected is False
        assert result.gap_events == []
        assert result.fresh_start is False
        assert [tuple(header) for header in result.headers] == [
            (b":method", b"GET"),
            (b":scheme", b"http"),
            (b":path", b"/"),
        ]

    def test_recovery_keeps_table_state_consistent_with_strict_decode(self):
        wire = b"\x40\x04name\x03val\xbe\x82"
        strict = Decoder()
        strict.decode(wire)

        recovering = Decoder()
        recovering.recover(wire)

        assert list(strict.header_table.dynamic_entries) == list(
            recovering.header_table.dynamic_entries,
        )

    def test_oversized_table_size_update_is_terminal_event(self):
        decoder = Decoder()
        decoder.max_allowed_table_size = 127
        result = decoder.recover(table_size_update(256) + b"\x82")

        assert result.gap_events
        assert result.headers == []
        # The strict decoder must still reject the same input.
        fresh = Decoder()
        fresh.max_allowed_table_size = 127
        with pytest.raises(InvalidTableSizeError):
            fresh.decode(table_size_update(256))


class TestRecoveredHeaderInterface:
    def test_tuple_compatible_behavior(self):
        result = Decoder().recover(b"\xbe\x40\x04name\x03val")
        header = result.literal_headers[0]

        assert isinstance(header, RecoveredHeader)
        assert header[0] == b"name"
        assert header[1] == b"val"
        assert list(header) == [b"name", b"val"]
        assert len(header) == 2
        assert tuple(header) == (b"name", b"val")
        assert dict([header]) == {b"name": b"val"}
        assert hash(header) == hash((b"name", b"val"))
        assert header == HeaderTuple(b"name", b"val")
        assert header != (b"other", b"val")
        assert header.as_tuple() == (b"name", b"val")
        assert isinstance(header.as_tuple(), HeaderTuple)

    def test_incremental_literal_is_indexable(self):
        result = Decoder().recover(b"\xbe\x40\x04name\x03val")
        header = result.literal_headers[0]
        assert header.indexable is True
        assert header.evidence.representation == REP_LITERAL_INCREMENTAL


class TestRecoveryFailureModes:
    def test_truncated_string_is_a_truncated_event(self):
        # Literal name declaring five bytes with none of them present.
        result = decode_with_recovery(b"\x82\x00\x05")

        assert [tuple(header) for header in result.headers] == [(b":method", b"GET")]
        event = result.gap_events[-1]
        assert event.kind == EVENT_TRUNCATED
        assert event.start == 1
        assert event.end == 2

    def test_missing_value_byte_is_a_truncated_event(self):
        # Never-indexed literal with a valid static name but no value at all.
        result = decode_with_recovery(b"\xbe\x14")

        assert result.headers == []
        event = result.gap_events[-1]
        assert event.kind == EVENT_TRUNCATED
        assert event.start == 1

    def test_invalid_huffman_is_a_malformed_event(self):
        # Literal name of one Huffman-encoded byte that is never a valid code.
        result = decode_with_recovery(b"\xbe\x00\x81\xff")

        assert result.headers == []
        event = result.gap_events[-1]
        assert event.kind == "malformed_representation"
        assert event.start == 1

    def test_eviction_keeps_provenance_aligned(self):
        decoder = Decoder()
        decoder.header_table_size = 100
        block = (
            b"\x40\x03foo\x03bar"
            b"\x40\x03foo\x03baz"
            b"\x40\x03foo\x03qux"
            b"\xc0"  # dynamic index 64 was evicted
            b"\xbe"  # dynamic index 62 still resolves
        )
        result = decoder.recover(block)

        assert result.gap_events[-1].kind == EVENT_INVALID_INDEX
        surviving = result.dynamic_reference_headers
        assert surviving == [(b"foo", b"qux")]
        assert surviving[0].evidence.table_entry_provenance == PROVENANCE_PRE_GAP

    def test_dynamic_name_literal_is_classified_as_dynamic(self):
        block = b"".join(
            [
                b"\x40\x04name\x03val",  # insertion at dynamic index 62
                b"\xbe\x7e\x03new",  # gap, then incremental literal, name index 62
            ],
        )
        result = Decoder().recover(block)

        dynamic = result.dynamic_reference_headers
        assert [tuple(header) for header in dynamic] == [
            (b"name", b"val"),
            (b"name", b"new"),
        ]
        assert dynamic[1].evidence.name_source == SOURCE_DYNAMIC
        assert dynamic[1].evidence.value_source == SOURCE_LITERAL
        assert dynamic[1].evidence.dynamic_index == 62
        # A field depending on a dynamic name is not a complete literal.
        assert dynamic[1] not in result.literal_headers

    def test_oversized_insertion_clears_table_and_marks_fresh_start(self):
        decoder = Decoder()
        decoder.header_table_size = 10  # any real entry exceeds this size

        result = decoder.recover(b"\xbe\x40\x04name\x03val")

        assert result.gap_detected
        assert result.fresh_start is True
        assert result.clear_events[0].kind == EVENT_TABLE_CLEAR
        assert len(decoder.header_table.dynamic_entries) == 0
        assert [tuple(header) for header in result.headers] == [(b"name", b"val")]


class TestRecoveryValueObjects:
    def test_evidence_equality_and_repr(self):
        left = RecoveryEvidence(REP_INDEXED, 0, 1, SOURCE_STATIC, SOURCE_STATIC, static_index=2)
        right = RecoveryEvidence(REP_INDEXED, 0, 1, SOURCE_STATIC, SOURCE_STATIC, static_index=2)
        different = RecoveryEvidence(REP_INDEXED, 0, 1, SOURCE_STATIC, SOURCE_STATIC, static_index=3)

        assert left == right
        assert left != different
        assert left != object()
        assert "RecoveryEvidence" in repr(left)

    def test_event_equality_and_repr(self):
        left = RecoveryEvent(EVENT_INVALID_INDEX, "bad", 0, 1)
        right = RecoveryEvent(EVENT_INVALID_INDEX, "bad", 0, 1)

        assert left == right
        assert left != RecoveryEvent(EVENT_INVALID_INDEX, "bad", 0, 2)
        assert left != object()
        assert "RecoveryEvent" in repr(left)

    def test_header_compared_with_non_tuple_is_not_equal(self):
        result = Decoder().recover(b"\xbe\x82")
        header = result.headers[0]

        assert header != 42
        assert header.__eq__(42) is NotImplemented
        assert header != "not a header"

    def test_header_ne(self):
        result = Decoder().recover(b"\xbe\x82")
        header = result.headers[0]

        assert header != (b":method", b"POST")
        assert not (header != (b":method", b"GET"))
        assert header.__ne__(42) is NotImplemented

    def test_header_repr_mentions_value_and_evidence(self):
        result = Decoder().recover(b"\xbe\x82")
        rendered = repr(result.headers[0])

        assert "RecoveredHeader" in rendered
        assert b":method".decode() in rendered or ":method" in rendered
        assert "evidence=" in rendered
