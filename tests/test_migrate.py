"""Tests for offline migration rehearsal between ISO8583 specifications."""

import copy
import typing

import iso8583
import iso8583.specs
from iso8583.migrate import (
    ENCODE_ERROR,
    LENGTH_MISMATCH,
    LENGTH_OVERFLOW,
    ROUNDTRIP_MISMATCH,
    TOO_LONG,
    UNDEFINED,
    OK,
)
import pytest


def _target_field48_binary_llvar() -> typing.Dict[str, typing.Dict[str, typing.Any]]:
    """Field 48: binary/BCD data, two-digit ASCII length header, 99 bytes."""
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["48"].update(
        data_enc="b",
        len_enc="ascii",
        len_type=2,
        max_len=99,
    )
    return spec


# fmt: off
@pytest.mark.parametrize(
    ["len_enc", "len_type", "expected_capacity"],
    [
        ("ascii", 0, None),
        ("cp500", 0, None),
        ("bcd", 0, None),
        ("b", 0, None),
        ("ascii", 1, 9),
        ("ascii", 2, 99),
        ("ascii", 3, 999),
        ("cp500", 2, 99),
        ("bcd", 1, 99),
        ("bcd", 2, 9999),
        ("b", 1, 255),
        ("b", 2, 65535),
    ],
)
# fmt: on
def test_length_header_capacity(
    len_enc: str, len_type: int, expected_capacity: typing.Optional[int]
) -> None:
    assert iso8583.length_header_capacity(len_enc, len_type) == expected_capacity


def test_field48_uses_transcoded_byte_size_not_char_count() -> None:
    """4 source characters become 2 BCD bytes; the verdict uses 2, not 4."""
    spec = _target_field48_binary_llvar()

    row = iso8583.rehearse_field("48", "1234", spec)

    assert row["status"] == OK
    assert row["encoded_bytes"] == 2
    assert row["measured"] == 2
    assert row["max_data_bytes"] == 99
    assert row["length_capacity"] == 99
    assert row["length_fits"] is True
    assert row["roundtrip_equal"] is True


def test_field48_fitting_llvar_header() -> None:
    spec = _target_field48_binary_llvar()

    row = iso8583.rehearse_field("48", "9" * 50, spec)

    assert row["status"] == OK
    assert row["encoded_bytes"] == 25
    assert row["length_fits"] is True
    assert row["roundtrip_equal"] is True


def test_field48_too_long_does_not_use_source_char_count() -> None:
    """200 source digits are 100 BCD bytes against a 99-byte target."""
    spec = _target_field48_binary_llvar()

    row = iso8583.rehearse_field("48", "8" * 200, spec)

    assert row["status"] == TOO_LONG
    assert row["encoded_bytes"] == 100
    assert row["measured"] == 100
    assert row["length_fits"] is False
    assert row["roundtrip_equal"] is None


def test_field48_undefined_is_distinct_from_too_long() -> None:
    spec = _target_field48_binary_llvar()
    message = {
        "t": "0200",
        "48": "8" * 200,
        "193": "x",
    }

    rows = iso8583.rehearse_message(message, spec)
    by_field = {row["field"]: row for row in rows}

    assert by_field["48"]["status"] == TOO_LONG
    assert by_field["48"]["encoded_bytes"] == 100
    assert by_field["193"]["status"] == UNDEFINED
    assert by_field["193"]["encoded_bytes"] is None
    assert by_field["t"]["status"] == OK


def test_field48_encode_error_does_not_block_other_fields() -> None:
    spec = _target_field48_binary_llvar()
    message = {
        "t": "0200",
        "3": "111111",
        "48": "ZZ",
        "39": "05",
    }

    rows = iso8583.rehearse_message(message, spec)
    by_field = {row["field"]: row for row in rows}

    assert by_field["48"]["status"] == ENCODE_ERROR
    assert by_field["3"]["status"] == OK
    assert by_field["39"]["status"] == OK
    assert by_field["t"]["status"] == OK


def test_field48_odd_hex_without_pad_is_encode_error() -> None:
    spec = _target_field48_binary_llvar()

    row = iso8583.rehearse_field("48", "ABC", spec)

    assert row["status"] == ENCODE_ERROR
    assert "odd-length hex data" in typing.cast(str, row["error"])


def test_field48_length_overflow_when_header_too_narrow() -> None:
    """100 BCD bytes fit max_len 200 but not the two-digit (max 99) header."""
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["48"].update(
        data_enc="b",
        len_enc="ascii",
        len_type=2,
        max_len=200,
    )

    row = iso8583.rehearse_field("48", "1" * 200, spec)

    assert row["status"] == LENGTH_OVERFLOW
    assert row["encoded_bytes"] == 100
    assert row["measured"] == 100
    assert row["max_len"] == 200
    assert row["length_capacity"] == 99
    assert row["length_fits"] is False


def test_field48_bcd_length_header_overflow() -> None:
    """One BCD length byte holds two decimal digits, i.e. capacity 99."""
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["48"].update(
        data_enc="b",
        len_enc="bcd",
        len_type=1,
        max_len=200,
    )

    row = iso8583.rehearse_field("48", "1" * 200, spec)

    assert row["status"] == LENGTH_OVERFLOW
    assert row["length_capacity"] == 99
    assert row["encoded_bytes"] == 100
    assert row["length_fits"] is False


def test_field48_roundtrip_mismatch_is_detected() -> None:
    """shift_jis encodes U+00A5 into 0x5C, which decodes back to a backslash."""
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["48"].update(
        data_enc="shift_jis",
        len_enc="ascii",
        len_type=2,
        max_len=99,
    )

    row = iso8583.rehearse_field("48", "¥", spec)

    assert row["status"] == ROUNDTRIP_MISMATCH
    assert row["encoded_bytes"] == 1
    assert row["length_fits"] is True
    assert row["roundtrip_equal"] is False


def test_ebcdic_field_roundtrips_with_single_byte_size() -> None:
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["48"].update(
        data_enc="cp500",
        len_enc="cp500",
        len_type=2,
        max_len=99,
    )

    row = iso8583.rehearse_field("48", "AB", spec)

    assert row["status"] == OK
    assert row["encoded_bytes"] == 2
    assert row["length_capacity"] == 99
    assert row["roundtrip_equal"] is True


def test_fixed_field_length_mismatch() -> None:
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["39"].update(data_enc="ascii", len_type=0, max_len=2)

    ok_row = iso8583.rehearse_field("39", "05", spec)
    bad_row = iso8583.rehearse_field("39", "050", spec)

    assert ok_row["status"] == OK
    assert ok_row["length_fits"] is None
    assert bad_row["status"] == LENGTH_MISMATCH
    assert bad_row["encoded_bytes"] == 3
    assert bad_row["roundtrip_equal"] is None


def test_nibble_field_odd_length_with_left_pad() -> None:
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["48"].update(
        data_enc="b",
        len_enc="ascii",
        len_type=2,
        max_len=10,
        len_count="nibbles",
        left_pad="0",
    )

    row = iso8583.rehearse_field("48", "123", spec)

    assert row["status"] == OK
    assert row["encoded_bytes"] == 2
    assert row["measured"] == 3
    assert row["length_unit"] == "nibbles"
    assert row["length_fits"] is True
    assert row["roundtrip_equal"] is True


def test_nibble_field_too_long_measured_in_nibbles() -> None:
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["48"].update(
        data_enc="b",
        len_enc="ascii",
        len_type=2,
        max_len=2,
        len_count="nibbles",
        left_pad="0",
    )

    row = iso8583.rehearse_field("48", "1234", spec)

    assert row["status"] == TOO_LONG
    assert row["encoded_bytes"] == 2
    assert row["measured"] == 4
    assert row["max_data_bytes"] == 1


def test_rehearse_batch_keeps_messages_and_fields_independent() -> None:
    spec = _target_field48_binary_llvar()
    messages = [
        {"t": "0200", "48": "8" * 200, "39": "05"},
        {"t": "0210", "48": "1234", "193": "x"},
    ]

    batches = iso8583.rehearse_batch(messages, spec)

    first = {row["field"]: row for row in batches[0]}
    second = {row["field"]: row for row in batches[1]}

    assert first["48"]["status"] == TOO_LONG
    assert first["39"]["status"] == OK
    assert second["48"]["status"] == OK
    assert second["48"]["encoded_bytes"] == 2
    assert second["193"]["status"] == UNDEFINED


def test_rehearse_message_field_filter_and_ordering() -> None:
    spec = _target_field48_binary_llvar()
    message = {"t": "0200", "3": "111111", "48": "1234", "39": "05"}

    rows = iso8583.rehearse_message(message, spec, fields=["48", "39"])

    assert [row["field"] for row in rows] == ["39", "48"]
    assert all(row["status"] == OK for row in rows)


def test_rehearse_message_survives_broken_field_specification() -> None:
    """A malformed target spec for one field must not abort the whole message."""
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["39"] = {}
    message = {"t": "0200", "39": "05", "3": "111111"}

    rows = iso8583.rehearse_message(message, spec)
    by_field = {row["field"]: row for row in rows}

    assert by_field["39"]["status"] == ENCODE_ERROR
    assert "Unexpected rehearsal failure" in typing.cast(str, by_field["39"]["error"])
    assert by_field["3"]["status"] == OK
