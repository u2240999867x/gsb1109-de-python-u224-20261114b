"""
Tests for the lossless archive migration helpers.
"""

from __future__ import annotations

import pytest

import hpack
from hpack.archive import UTF8_INVALID, UTF8_OK, ArchivedField, decode_for_archive, restore_header_block


def encode(headers):
    encoder = hpack.Encoder()
    return encoder.encode(headers)


class TestDecodeForArchive:
    def test_invalid_utf8_no_longer_aborts_batch(self):
        headers = [
            (b":method", b"GET"),
            (b"x-good", b"fine"),
            (b"x-bad", b"\xff\xfe"),
            (b"x-after", b"still here"),
        ]
        data = encode(headers)

        with pytest.raises(hpack.HPACKDecodingError):
            hpack.Decoder().decode(data)

        fields = decode_for_archive(hpack.Decoder(), data)
        assert [f.business_key for f in fields] == [(n, v) for n, v in headers]

    def test_text_view_status_and_error_positions(self):
        headers = [
            (b"x-good", "héllo".encode()),
            (b"x-bad", b"a\xffb\xfe"),
            (b"bad\xffname", b"v"),
        ]
        fields = decode_for_archive(hpack.Decoder(), encode(headers))
        good, bad, bad_name = fields

        assert good.value_text == "héllo"
        assert good.value_status == UTF8_OK
        assert good.value_error_positions == ()

        assert bad.value_text == "a�b�"
        assert bad.value_status == UTF8_INVALID
        assert bad.value_error_positions == (1, 3)

        assert bad_name.name_text == "bad�name"
        assert bad_name.name_status == UTF8_INVALID
        assert bad_name.name_error_positions == (3,)
        assert bad_name.value_status == UTF8_OK

    def test_truncated_multibyte_sequence_reports_position(self):
        # A lone continuation of a truncated multi-byte sequence at the end.
        fields = decode_for_archive(hpack.Decoder(), encode([(b"x", b"ok\xe4\xb8")]))
        (field,) = fields
        assert field.value_text == "ok�"
        assert field.value_status == UTF8_INVALID
        assert field.value_error_positions == (2,)

    def test_empty_fields_are_valid(self):
        fields = decode_for_archive(hpack.Decoder(), encode([(b"", b"")]))
        (field,) = fields
        assert field.name_text == ""
        assert field.value_text == ""
        assert field.name_status == UTF8_OK
        assert field.value_status == UTF8_OK
        assert field.name_error_positions == ()
        assert field.value_error_positions == ()

    def test_replacement_char_does_not_share_business_key_with_bad_bytes(self):
        legit = ("x".encode(), "�".encode())
        bad = (b"x", b"\xff")
        fields = decode_for_archive(hpack.Decoder(), encode([legit, bad]))
        legit_field, bad_field = fields

        # The text views are indistinguishable...
        assert legit_field.value_text == bad_field.value_text == "�"
        # ...but the decode statuses and business keys are not.
        assert legit_field.value_status == UTF8_OK
        assert bad_field.value_status == UTF8_INVALID
        assert legit_field.business_key != bad_field.business_key

    def test_round_trip_is_lossless_for_valid_and_invalid_fields(self):
        headers = [
            (b":path", b"/"),
            hpack.NeverIndexedHeaderTuple(b"authorization", b"s3cr\xffet"),
            (b"x-bad", b"\x80\x81"),
            (b"x-good", "naïve".encode()),
        ]
        data = encode(headers)

        fields = decode_for_archive(hpack.Decoder(), data)
        restored = restore_header_block(fields)

        assert restored == hpack.Decoder().decode(data, raw=True)
        assert isinstance(restored[1], hpack.NeverIndexedHeaderTuple)
        assert not restored[1].indexable
        assert restored[0].indexable

        # Re-encoding the restored fields decodes to the same bytes again.
        redecoded = hpack.Decoder().decode(encode(restored), raw=True)
        assert [tuple(h) for h in redecoded] == [tuple(h) for h in restored]

    def test_rerun_preserves_duplicate_order_and_count(self):
        headers = [
            (b"set-cookie", b"a=1"),
            (b"set-cookie", b"b=2"),
            (b"set-cookie", b"a=1"),
            (b"x-dup", b"\xff"),
            (b"x-dup", b"\xff"),
        ]
        data = encode(headers)

        first_run = decode_for_archive(hpack.Decoder(), data)
        second_run = decode_for_archive(hpack.Decoder(), data)

        assert first_run == second_run
        assert [f.business_key for f in first_run] == [(n, v) for n, v in headers]
        assert len(first_run) == len(headers)


class TestArchivedField:
    def test_defaults_to_indexable(self):
        field = ArchivedField(name=b"a", value=b"b")
        assert field.indexable
        assert isinstance(field.to_header_tuple(), hpack.HeaderTuple)
        assert not isinstance(field.to_header_tuple(), hpack.NeverIndexedHeaderTuple)

    def test_never_indexed_round_trip(self):
        field = ArchivedField(name=b"a", value=b"b", indexable=False)
        restored = field.to_header_tuple()
        assert isinstance(restored, hpack.NeverIndexedHeaderTuple)
        assert tuple(restored) == (b"a", b"b")
