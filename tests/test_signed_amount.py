"""Tests for trailing-sign BCD amount fields and the cross-spec samples."""

from decimal import Decimal

import pytest

import iso8583
from iso8583.signed_amount import (
    AmountStatus,
    build_signed_amount,
    parse_signed_amount,
    parse_signed_amount_nibbles,
    signed_amount_field_spec,
)

from examples.signed_amount_samples import (
    BAD_DIGIT_NIBBLES,
    BAD_DIGIT_RAW,
    NARROW_FIELD,
    NARROW_WIDTH,
    SAMPLES,
    Sample,
    WIDE_FIELD,
    WIDE_WIDTH,
    build_spec,
    check_bad_digit_rejected,
    check_sample,
    check_value_12_widths,
    run_all,
)


def test_positive_amount_views() -> None:
    amount = parse_signed_amount(b"\x00\x00\x01\x2c")
    assert amount.status == AmountStatus.POSITIVE
    assert amount.value == 12
    assert amount.is_negative is False
    assert amount.to_decimal() == Decimal("0.12")
    assert amount.to_decimal(0) == Decimal("12")
    assert amount.sign_nibble == "C"
    assert amount.digits == "0000012"


def test_negative_amount_views() -> None:
    amount = parse_signed_amount(b"\x00\x00\x01\x2d")
    assert amount.status == AmountStatus.NEGATIVE
    assert amount.value == -12
    assert amount.is_negative is True
    assert amount.to_decimal() == Decimal("-0.12")
    assert amount.sign_nibble == "D"


def test_negative_zero_is_distinct_from_normal_zero() -> None:
    negative_zero = parse_signed_amount(b"\x00\x00\x00\x0d")
    normal_zero = parse_signed_amount(b"\x00\x00\x00\x0c")
    unsigned_zero = parse_signed_amount(b"\x00\x00\x00\x0f")

    assert negative_zero.status == AmountStatus.NEGATIVE_ZERO
    assert negative_zero.is_negative is True
    assert normal_zero.status == AmountStatus.ZERO
    assert normal_zero.is_negative is False
    assert unsigned_zero.status == AmountStatus.ZERO
    assert unsigned_zero.is_negative is False

    # The numeric view collapses both zeros, the raw declaration does not.
    assert negative_zero.value == normal_zero.value == unsigned_zero.value == 0
    assert negative_zero.nibbles == "0000000D"
    assert normal_zero.nibbles == "0000000C"
    assert unsigned_zero.nibbles == "0000000F"
    assert negative_zero.nibbles != normal_zero.nibbles


def test_empty_field() -> None:
    amount = parse_signed_amount(b"")
    assert amount.status == AmountStatus.EMPTY
    assert amount.value is None
    assert amount.is_negative is None
    assert amount.to_decimal() is None
    assert amount.nibbles == ""
    assert amount.raw == b""
    assert amount.sign_nibble == ""
    assert amount.digits == ""
    assert amount.width == 0

    nibble_amount = parse_signed_amount_nibbles("")
    assert nibble_amount == amount


def test_invalid_sign_codes_have_no_numeric_view() -> None:
    for raw, nibble in [
        (b"\x00\x00\x01\x2a", "0000012A"),
        (b"\x00\x00\x01\x27", "00000127"),
        (b"\x00\x00\x01\x29", "00000129"),
    ]:
        amount = parse_signed_amount(raw)
        assert amount.status == AmountStatus.INVALID_SIGN
        assert amount.value is None
        assert amount.is_negative is None
        assert amount.to_decimal() is None
        assert amount.nibbles == nibble


def test_alternative_sign_nibbles() -> None:
    assert parse_signed_amount(b"\x00\x00\x01\x2e").status == AmountStatus.POSITIVE
    assert parse_signed_amount(b"\x00\x00\x01\x2f").status == AmountStatus.POSITIVE
    assert parse_signed_amount(b"\x00\x00\x01\x2b").status == AmountStatus.NEGATIVE


def test_nibble_entry_point_matches_bytes_entry_point() -> None:
    from_bytes = parse_signed_amount(b"\x00\x00\x01\x2d", WIDE_WIDTH)
    from_nibbles = parse_signed_amount_nibbles("0000012D", WIDE_WIDTH)
    assert from_nibbles == from_bytes
    assert from_nibbles.raw == from_bytes.raw

    lower_case = parse_signed_amount_nibbles("0000012d")
    assert lower_case.nibbles == "0000012D"
    assert lower_case == from_bytes


def test_verbatim_views_are_not_reconstructed() -> None:
    amount = parse_signed_amount(b"\x00\x00\x01\x2c")
    assert amount.to_bytes() == b"\x00\x00\x01\x2c"
    assert amount.to_nibbles() == "0000012C"
    # Frozen dataclass: the raw declaration cannot be mutated in place.
    with pytest.raises(AttributeError):
        setattr(amount, "value", 99)


def test_width_mismatch_and_odd_width_rejected() -> None:
    with pytest.raises(ValueError, match="6 nibbles wide, expecting 8"):
        parse_signed_amount(b"\x00\x01\x2c", WIDE_WIDTH)
    with pytest.raises(ValueError, match="8 nibbles wide, expecting 6"):
        parse_signed_amount_nibbles("0000012C", NARROW_WIDTH)
    with pytest.raises(ValueError, match="even nibble width"):
        parse_signed_amount_nibbles("012")
    with pytest.raises(ValueError, match="0 nibbles wide, expecting 8"):
        parse_signed_amount(b"", WIDE_WIDTH)


def test_non_hex_and_non_decimal_nibbles_rejected() -> None:
    with pytest.raises(ValueError, match="non-hex"):
        parse_signed_amount_nibbles("0000012X")
    with pytest.raises(ValueError, match="non-decimal digit"):
        parse_signed_amount(BAD_DIGIT_RAW, WIDE_WIDTH)
    with pytest.raises(ValueError, match="non-decimal digit"):
        parse_signed_amount_nibbles(BAD_DIGIT_NIBBLES, WIDE_WIDTH)


def test_input_type_validation() -> None:
    with pytest.raises(TypeError):
        parse_signed_amount("0000012C")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_signed_amount_nibbles(b"0000012C")  # type: ignore[arg-type]


def test_decimal_places_validation() -> None:
    amount = parse_signed_amount(b"\x00\x00\x01\x2c")
    with pytest.raises(ValueError):
        amount.to_decimal(-1)


def test_build_signed_amount() -> None:
    assert build_signed_amount("0000012", "C", WIDE_WIDTH) == b"\x00\x00\x01\x2c"
    assert build_signed_amount("0000000", "D", WIDE_WIDTH) == b"\x00\x00\x00\x0d"
    assert build_signed_amount("00012", "C", NARROW_WIDTH) == b"\x00\x01\x2c"

    with pytest.raises(ValueError, match="even nibble width"):
        build_signed_amount("012", "C", 3)
    with pytest.raises(ValueError, match="expecting 8"):
        build_signed_amount("012", "C", WIDE_WIDTH)
    with pytest.raises(ValueError, match="non-decimal"):
        build_signed_amount("000001A", "C", WIDE_WIDTH)
    with pytest.raises(ValueError, match="Unrecognized sign nibble"):
        build_signed_amount("0000012", "A", WIDE_WIDTH)


def test_signed_amount_field_spec() -> None:
    spec = signed_amount_field_spec(WIDE_WIDTH, "Amount, Transaction (signed BCD)")
    assert spec == {
        "data_enc": "b",
        "len_enc": "ascii",
        "len_type": 0,
        "max_len": WIDE_WIDTH,
        "len_count": "nibbles",
        "desc": "Amount, Transaction (signed BCD)",
    }
    with pytest.raises(ValueError, match="even nibble width"):
        signed_amount_field_spec(7, "odd")


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample.sample_id)
def test_every_cross_spec_sample(sample: Sample) -> None:
    spec = build_spec(sample.field, sample.width, sample.description)
    check_sample(spec, sample)


def test_value_12_keeps_distinct_bit_strings() -> None:
    check_value_12_widths()

    wide_spec = build_spec(WIDE_FIELD, WIDE_WIDTH, "wide")
    narrow_spec = build_spec(NARROW_FIELD, NARROW_WIDTH, "narrow")

    wide_message, _ = iso8583.encode({"t": "0200", WIDE_FIELD: "0000012C"}, wide_spec)
    narrow_message, _ = iso8583.encode(
        {"t": "0200", NARROW_FIELD: "00012C"}, narrow_spec
    )

    wide_decoded, _ = iso8583.decode(bytes(wide_message), wide_spec)
    narrow_decoded, _ = iso8583.decode(bytes(narrow_message), narrow_spec)

    # Same numeric value, different business widths and different bit strings.
    assert wide_decoded[WIDE_FIELD] == "0000012C"
    assert narrow_decoded[NARROW_FIELD] == "00012C"
    assert wide_message[-WIDE_WIDTH // 2 :] != narrow_message[-NARROW_WIDTH // 2 :]


def test_invalid_sign_round_trips_verbatim() -> None:
    # An illegal sign code still survives encode/decode byte identical.
    spec = build_spec(WIDE_FIELD, WIDE_WIDTH, "wide")
    message, _ = iso8583.encode({"t": "0200", WIDE_FIELD: "0000012A"}, spec)
    decoded, encoded = iso8583.decode(bytes(message), spec)
    assert decoded[WIDE_FIELD] == "0000012A"
    assert encoded[WIDE_FIELD]["data"] == b"\x00\x00\x01\x2a"


def test_negative_zero_round_trips_verbatim() -> None:
    spec = build_spec(WIDE_FIELD, WIDE_WIDTH, "wide")
    message, _ = iso8583.encode({"t": "0200", WIDE_FIELD: "0000000D"}, spec)
    negative_zero_nibbles = parse_signed_amount(
        b"\x00\x00\x00\x0d"
    ).to_nibbles()
    rebuilt, _ = iso8583.encode(
        {"t": "0200", WIDE_FIELD: negative_zero_nibbles},
        spec,
    )
    assert bytes(rebuilt) == bytes(message)


def test_bad_digit_rejected() -> None:
    check_bad_digit_rejected()


def test_run_all_returns_reading_per_sample() -> None:
    readings = run_all()
    assert [reading.sample_id for reading in readings] == [
        sample.sample_id for sample in SAMPLES
    ]
    empty = next(reading for reading in readings if reading.status == "empty")
    assert empty.raw_hex == "-"
    assert empty.value == "-"
    invalid = next(reading for reading in readings if reading.status == "invalid_sign")
    assert invalid.value == "-"
    negative_zero = next(
        reading for reading in readings if reading.status == "negative_zero"
    )
    assert negative_zero.value == "0"
    assert negative_zero.sign_nibble == "D"
