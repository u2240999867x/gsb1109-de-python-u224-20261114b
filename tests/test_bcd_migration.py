"""Test migration of historical BCD fields between padding dialects."""

import copy

import pytest

import iso8583
import iso8583.specs
from iso8583.bcd_migration import BcdAmbiguityError, BcdMigrationError


def _legacy() -> iso8583.BcdSide:
    # Right-padded odd nibbles; the length prefix counts the pad nibble.
    return iso8583.BcdSide("legacy", pad_side="right", length_counts_pad=True)


def _partner() -> iso8583.BcdSide:
    # Left-padded odd nibbles; the length prefix counts valid nibbles only.
    return iso8583.BcdSide("partner", pad_side="left", length_counts_pad=False)


def test_side_rejects_invalid_configuration() -> None:
    with pytest.raises(BcdMigrationError, match="Invalid pad_side"):
        iso8583.BcdSide("x", pad_side="middle", length_counts_pad=True)

    with pytest.raises(BcdMigrationError, match="Invalid pad"):
        iso8583.BcdSide("x", pad_side="left", length_counts_pad=True, pad="GG")


def test_convert_01230_keeps_width_and_uses_each_side_rules() -> None:
    conversion = iso8583.convert_field("01230", _legacy(), _partner())

    assert conversion.value == "01230"
    assert conversion.width == 5

    source = conversion.source
    assert source.hex_data == "012300"
    assert source.valid_nibbles == 5
    assert source.padded_nibbles == 6
    assert source.pad_added is True
    assert source.length == 6  # legacy counts the right pad nibble
    assert source.length_bytes == b"\x06"
    assert source.data_bytes == b"\x01\x23\x00"
    assert source.wire == b"\x06\x01\x23\x00"

    target = conversion.target
    assert target.hex_data == "001230"
    assert target.valid_nibbles == 5
    assert target.padded_nibbles == 6
    assert target.pad_added is True
    assert target.length == 5  # new side counts valid nibbles only
    assert target.length_bytes == b"\x05"
    assert target.data_bytes == b"\x00\x12\x30"
    assert target.wire == b"\x05\x00\x12\x30"


def test_convert_single_zero_keeps_width_and_uses_each_side_rules() -> None:
    conversion = iso8583.convert_field("0", _legacy(), _partner())

    assert conversion.value == "0"
    assert conversion.width == 1

    assert conversion.source.hex_data == "00"
    assert conversion.source.length == 2
    assert conversion.source.wire == b"\x02\x00"

    assert conversion.target.hex_data == "00"
    assert conversion.target.length == 1
    assert conversion.target.wire == b"\x01\x00"


def test_even_value_needs_no_pad_on_either_side() -> None:
    conversion = iso8583.convert_field("1234", _legacy(), _partner())

    assert conversion.source.hex_data == "1234"
    assert conversion.target.hex_data == "1234"
    assert conversion.source.pad_added is False
    assert conversion.target.pad_added is False
    # Even width means no pad nibble, so both length rules report the same count.
    assert conversion.source.length == 4
    assert conversion.target.length == 4


# fmt: off
@pytest.mark.parametrize(
    ["pad_side", "counts_pad", "expected_hex", "expected_length"],
    [
        ("left",  True,  "0123", 4),
        ("left",  False, "0123", 3),
        ("right", True,  "1230", 4),
        ("right", False, "1230", 3),
    ],
)
# fmt: on
def test_padding_and_length_rules_are_independent(
    pad_side: str, counts_pad: bool, expected_hex: str, expected_length: int
) -> None:
    side = iso8583.BcdSide("x", pad_side=pad_side, length_counts_pad=counts_pad)
    encoding = iso8583.encode_side("123", side)

    assert encoding.hex_data == expected_hex
    assert encoding.valid_nibbles == 3
    assert encoding.padded_nibbles == 4
    assert encoding.length == expected_length


def test_custom_pad_digit() -> None:
    side = iso8583.BcdSide("x", pad_side="right", length_counts_pad=True, pad="F")
    encoding = iso8583.encode_side("123", side)

    assert encoding.hex_data == "123F"
    assert encoding.wire == b"\x04\x12\x3F"


@pytest.mark.parametrize("value", ["01230", "0", "123", "7", "00", "007", "F", "0F0"])
def test_convert_round_trips_bitwise_in_both_directions(value: str) -> None:
    legacy = _legacy()
    partner = _partner()

    forward = iso8583.convert_field(value, legacy, partner)
    backward = iso8583.convert_field(forward.value, partner, legacy)

    assert forward.value == value
    assert backward.value == value
    assert backward.source_wire == forward.target_wire
    assert backward.target_wire == forward.source_wire


def test_decode_partner_side_recovers_full_width() -> None:
    partner = _partner()

    assert iso8583.decode_side(bytes.fromhex("001230"), 5, partner).value == "01230"
    assert iso8583.decode_side(bytes.fromhex("00"), 1, partner).value == "0"
    assert iso8583.decode_side(bytes.fromhex("0123"), 4, partner).value == "0123"


def test_decode_legacy_even_field_has_no_ambiguity() -> None:
    legacy = _legacy()
    encoding = iso8583.decode_side(bytes.fromhex("1234"), 4, legacy)

    assert encoding.value == "1234"
    assert encoding.pad_added is False
    assert encoding.length == 4


def test_decode_legacy_odd_field_with_non_pad_nibble_is_business_data() -> None:
    legacy = _legacy()
    # The right nibble is 'F', not the configured '0' pad, so it is data.
    encoding = iso8583.decode_side(bytes.fromhex("123F"), 4, legacy)

    assert encoding.value == "123F"
    assert encoding.valid_nibbles == 4


def test_decode_legacy_ambiguous_trailing_zero_raises_and_lists_missing_specs() -> None:
    legacy = _legacy()

    with pytest.raises(BcdAmbiguityError) as excinfo:
        iso8583.decode_side(bytes.fromhex("012300"), 6, legacy)

    error = excinfo.value
    assert error.hex_data == "012300"
    assert error.reported_length == 6
    assert error.side is legacy
    assert len(error.missing_specs) == 3
    assert all(isinstance(item, str) and item for item in error.missing_specs)


def test_decode_legacy_ambiguous_field_resolved_explicitly() -> None:
    legacy = _legacy()

    as_padded = iso8583.decode_side(
        bytes.fromhex("012300"), 6, legacy, assume_pad=True
    )
    as_business = iso8583.decode_side(
        bytes.fromhex("012300"), 6, legacy, assume_pad=False
    )

    assert as_padded.value == "01230"
    assert as_business.value == "012300"


def test_decode_legacy_left_pad_ambiguous_first_nibble() -> None:
    left_legacy = iso8583.BcdSide(
        "left-legacy", pad_side="left", length_counts_pad=True
    )

    with pytest.raises(BcdAmbiguityError):
        iso8583.decode_side(bytes.fromhex("0123"), 4, left_legacy)

    assert (
        iso8583.decode_side(
            bytes.fromhex("0123"), 4, left_legacy, assume_pad=True
        ).value
        == "123"
    )
    assert (
        iso8583.decode_side(
            bytes.fromhex("0123"), 4, left_legacy, assume_pad=False
        ).value
        == "0123"
    )


def test_decode_ambiguity_is_avoided_when_pad_nibble_differs() -> None:
    f_pad = iso8583.BcdSide("f", pad_side="right", length_counts_pad=True, pad="F")

    # Ends in 'F' which is the pad; still ambiguous between pad and business 'F'.
    with pytest.raises(BcdAmbiguityError):
        iso8583.decode_side(bytes.fromhex("123F"), 4, f_pad)

    # Ends in '0' which is not the 'F' pad, so the field is unambiguously data.
    assert iso8583.decode_side(bytes.fromhex("1230"), 4, f_pad).value == "1230"


def test_decode_validates_packet() -> None:
    partner = _partner()
    legacy = _legacy()

    with pytest.raises(BcdMigrationError, match="bytes or bytearray"):
        iso8583.decode_side("00", 1, partner)  # type: ignore[arg-type]

    with pytest.raises(BcdMigrationError, match="must not be negative"):
        iso8583.decode_side(bytes.fromhex("00"), -1, partner)

    with pytest.raises(BcdMigrationError, match="must not be empty"):
        iso8583.decode_side(b"", 0, partner)

    with pytest.raises(BcdMigrationError, match="expecting 6 for 5 valid nibbles"):
        iso8583.decode_side(bytes.fromhex("0123"), 5, partner)

    with pytest.raises(BcdMigrationError, match="length prefix reports"):
        iso8583.decode_side(bytes.fromhex("012300"), 5, legacy)

    with pytest.raises(BcdMigrationError, match="Expected left pad"):
        iso8583.decode_side(bytes.fromhex("1230"), 3, partner)


def test_encode_rejects_invalid_value() -> None:
    with pytest.raises(BcdMigrationError, match="must be str"):
        iso8583.encode_side(123, _partner())  # type: ignore[arg-type]

    with pytest.raises(BcdMigrationError, match="must not be empty"):
        iso8583.encode_side("", _partner())

    with pytest.raises(BcdMigrationError, match="non-hex data"):
        iso8583.encode_side("12G", _partner())


def test_encode_odd_value_without_pad_rule_fails() -> None:
    even_only = iso8583.BcdSide("even", pad_side="none", length_counts_pad=False)

    with pytest.raises(BcdMigrationError, match="defines no pad"):
        iso8583.encode_side("123", even_only)

    assert iso8583.encode_side("1234", even_only).hex_data == "1234"


def test_converted_field_matches_library_bcd_nibble_dialect() -> None:
    # The new side must be re-encodable through the regular iso8583 encoder
    # configured for nibbles with a left pad and reproduce the same data bytes.
    spec = copy.deepcopy(iso8583.specs.default)
    spec["t"]["data_enc"] = "ascii"
    spec["p"]["data_enc"] = "ascii"
    spec["2"] = {
        "data_enc": "b",
        "len_enc": "bcd",
        "len_type": 1,
        "len_count": "nibbles",
        "left_pad": "0",
        "max_len": 10,
        "desc": "partner BCD field",
    }

    conversion = iso8583.convert_field("01230", _legacy(), _partner())
    wire, _ = iso8583.encode({"t": "0200", "2": "01230"}, spec)

    assert bytes(wire)[-len(conversion.target.wire) :] == conversion.target.wire
