"""Tests for iso8583.catalog() field cataloging."""

import copy
import json
import typing

import iso8583
import iso8583.specs
import pytest


def _bitmap(*des: int, offset: int = 0) -> bytes:
    """Return an ASCII hex bitmap with the given data elements enabled."""
    data = bytearray(8)
    for de in des:
        ordinal = de - offset - 1
        data[ordinal // 8] |= 0x80 >> (ordinal % 8)
    return data.hex().upper().encode("ascii")


def _build_spec() -> typing.Dict[str, typing.Dict[str, typing.Any]]:
    """Return ASCII specification with deterministic field lengths for tests."""
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["h"]["len_type"] = 0
    spec["h"]["max_len"] = 0

    spec["2"]["len_type"] = 2
    spec["2"]["max_len"] = 19
    spec["2"]["data_enc"] = "ascii"
    spec["2"]["len_enc"] = "ascii"

    spec["3"]["len_type"] = 0
    spec["3"]["max_len"] = 6
    spec["3"]["data_enc"] = "ascii"

    spec["4"]["len_type"] = 0
    spec["4"]["max_len"] = 12
    spec["4"]["data_enc"] = "ascii"

    spec["39"]["len_type"] = 0
    spec["39"]["max_len"] = 2
    spec["39"]["data_enc"] = "ascii"

    spec["66"]["len_type"] = 0
    spec["66"]["max_len"] = 1
    spec["66"]["data_enc"] = "ascii"

    spec["102"]["len_type"] = 2
    spec["102"]["max_len"] = 28
    spec["102"]["data_enc"] = "ascii"
    spec["102"]["len_enc"] = "ascii"

    spec["130"] = {
        "data_enc": "ascii",
        "len_enc": "ascii",
        "len_type": 0,
        "max_len": 4,
        "desc": "Tertiary Test Field",
    }

    return spec


def test_catalog_primary_field_ranges() -> None:
    spec = _build_spec()
    # t=0200, bitmap declares DE 2, DE 3, DE 4, DE 39
    s = b"0200" + _bitmap(2, 3, 4, 39) + b"041234" + b"111111" + b"000000012345" + b"00"

    found = iso8583.catalog(s, spec)

    assert found.size == len(s)
    assert found.consumed == len(s)
    assert found.complete is True
    assert found.truncated is False
    assert found.remaining.is_empty
    assert found.bitmaps_complete is True

    assert found["2"].bitmap.de == 2
    assert found["2"].bitmap.layer_name == "primary"
    assert found["2"].bitmap.byte == 0
    assert found["2"].bitmap.bit == 2
    assert found["2"].bitmap.mask == 0x40
    assert found["2"].length_prefix == iso8583.DataRange(20, 22)
    assert found["2"].data_range == iso8583.DataRange(22, 26)
    assert found["2"].declared_length == 4
    assert found["2"].value == "1234"

    assert found["3"].length_prefix.is_empty
    assert found["3"].data_range == iso8583.DataRange(26, 32)
    assert found["3"].value == "111111"

    assert found["4"].data_range == iso8583.DataRange(32, 44)
    assert found["4"].value == "000000012345"

    assert found["39"].data_range == iso8583.DataRange(44, 46)
    assert found["39"].value == "00"

    assert found.data_range("39") == iso8583.DataRange(44, 46)
    assert found.data_range(39) == iso8583.DataRange(44, 46)
    assert found.bitmap_position("39").ordinal == 39
    assert found[39].end == 46


def test_catalog_business_name_lookup() -> None:
    spec = _build_spec()
    s = b"0200" + _bitmap(2, 3, 4, 39) + b"041234" + b"111111" + b"000000012345" + b"00"

    found = iso8583.catalog(s, spec)

    assert found.by_name("Primary Account Number (PAN)")[0].field == "2"
    assert found.by_name("Amount, Transaction")[0].data_range == iso8583.DataRange(
        32, 44
    )
    assert found.by_name("Response Code")[0].value == "00"
    assert found.by_name("No Such Field") == ()
    # Names are not unique in the starter specification. Reserved fields
    # share one name and are all returned.
    assert found.by_name("Reserved for Private Use") == ()


def test_catalog_secondary_bitmap_is_not_a_business_field() -> None:
    spec = _build_spec()
    # Primary: DE1 only. Secondary: DE66 + DE102. Then both field values.
    s = (
        b"0200"
        + b"8000000000000000"
        + _bitmap(66, 102, offset=64)
        + b"A"
        + b"06"
        + b"111111"
    )

    found = iso8583.catalog(s, spec)

    assert found.bitmaps[0].layer_name == "primary"
    assert found.bitmaps[1].layer_name == "secondary"
    assert found.bitmaps[1].trigger_de == 1
    assert found.bitmaps[1].enabled == (66, 102)

    # DE 1 announced the secondary bitmap and is not a business field.
    assert "1" not in found
    assert "65" not in found

    assert found["66"].bitmap.layer == 1
    assert found["66"].bitmap.layer_name == "secondary"
    assert found["66"].bitmap.byte == 0
    assert found["66"].bitmap.bit == 2
    assert found["66"].data_range == iso8583.DataRange(36, 37)
    assert found["66"].value == "A"

    assert found["102"].bitmap.byte == 4
    assert found["102"].bitmap.bit == 6
    assert found["102"].length_prefix == iso8583.DataRange(37, 39)
    assert found["102"].data_range == iso8583.DataRange(39, 45)
    assert found["102"].value == "111111"


def test_catalog_tertiary_bitmap_is_not_a_business_field() -> None:
    spec = _build_spec()
    s = (
        b"0200"
        + b"8000000000000000"
        + b"8000000000000000"
        + b"4000000000000000"
        + b"ABCD"
    )

    found = iso8583.catalog(s, spec)

    assert len(found.bitmaps) == 3
    tertiary = found.bitmaps[2]
    assert tertiary.layer == 2
    assert tertiary.layer_name == "tertiary"
    assert tertiary.trigger_de == 65
    assert tertiary.enabled == (130,)

    # DE 65 announced the tertiary bitmap and is not a business field.
    assert "65" not in found

    assert found["130"].bitmap.layer == 2
    assert found["130"].bitmap.layer_name == "tertiary"
    assert found["130"].bitmap.ordinal == 2
    assert found["130"].bitmap.byte == 0
    assert found["130"].bitmap.bit == 2
    assert found["130"].data_range == iso8583.DataRange(52, 56)
    assert found["130"].value == "ABCD"

    # Only business fields are reported. The two bitmap indicator bits are
    # reported as bitmap layers instead.
    assert [entry.field for entry in found.fields] == ["130"]
    assert [layer.layer_name for layer in found.bitmaps] == [
        "primary",
        "secondary",
        "tertiary",
    ]


def test_catalog_zero_length_declared_differs_from_absent() -> None:
    spec = _build_spec()
    # DE 2 is on, its LLVAR length prefix is "00".
    s = b"0200" + b"4000000000000000" + b"00"

    found = iso8583.catalog(s, spec)

    assert "2" in found
    assert found["2"].declared_length == 0
    assert found["2"].is_empty is True
    assert found["2"].data_range == iso8583.DataRange(22, 22)
    assert found["2"].value == ""

    # Same message with DE 2 off.
    absent = iso8583.catalog(b"0200" + b"0000000000000000", spec)

    assert "2" not in absent
    assert len(absent) == 0


def test_catalog_truncated_final_bitmap_keeps_prior_declarations() -> None:
    spec = _build_spec()
    # Primary complete (DE1). Secondary complete (DE65, DE66, DE102).
    # Only 8 of the 16 tertiary bitmap bytes are present. The bytes after
    # them must not be used to pad the tertiary bitmap.
    s = (
        b"0210"
        + b"8000000000000000"
        + _bitmap(65, 66, 102, offset=64)
        + b"A01BA01B"
    )

    found = iso8583.catalog(s, spec)

    assert found.bitmaps_complete is False
    assert found.truncated is True
    assert found.complete is False

    primary, secondary, tertiary = found.bitmaps
    assert primary.complete is True
    assert secondary.complete is True
    assert tertiary.complete is False
    assert tertiary.start == 36
    assert tertiary.end == 36
    assert tertiary.available_length == 8
    assert tertiary.expected_length == 16
    assert tertiary.range.is_empty
    assert tertiary.available_range == iso8583.DataRange(36, 44)
    assert tertiary.enabled == ()

    # The declarations made by the complete layers survive, including their
    # bitmap positions. The byte range cannot be determined because the
    # boundary between bitmap bytes and business data is unknown.
    assert [entry.field for entry in found.fields] == ["66", "102"]
    assert found["66"].bitmap.layer_name == "secondary"
    assert found["102"].bitmap.layer_name == "secondary"
    assert found["66"].located is False
    assert found["66"].data_range is None
    assert found["102"].located is False
    assert found["102"].data_range is None

    # The short layer consumed nothing and none of the remaining bytes were
    # mistaken for bitmap padding or for business data.
    assert found.consumed == 36
    assert found.remaining == iso8583.DataRange(36, 44)


def test_catalog_truncated_final_bitmap_strict() -> None:
    spec = _build_spec()
    s = (
        b"0210"
        + b"8000000000000000"
        + _bitmap(65, 66, 102, offset=64)
        + b"80000000"
    )

    with pytest.raises(iso8583.CatalogError) as e:
        iso8583.catalog(s, spec, strict=True)

    assert e.value.args[0] == "Field data is 8 bytes, expecting 16: field 1 pos 36"
    assert e.value.field == "1"
    assert e.value.pos == 36
    assert e.value.s == s


def test_catalog_short_primary_and_secondary_keeps_primary_declarations() -> None:
    spec = _build_spec()
    # Primary declares DE3 and DE1. Only 12 of the 16 secondary bitmap bytes
    # are present and must not be padded.
    s = b"0200" + _bitmap(1, 3) + b"111111" + b"ABCDEF"

    found = iso8583.catalog(s, spec)

    assert [layer.layer_name for layer in found.bitmaps] == [
        "primary",
        "secondary",
    ]
    assert found.bitmaps[0].complete is True
    assert found.bitmaps[1].complete is False
    assert found.bitmaps[1].start == 20
    assert found.bitmaps[1].end == 20
    assert found.bitmaps[1].available_length == 12

    # DE3 was declared by the complete primary layer and is kept, but its
    # byte range is unknown because the short secondary bitmap consumed
    # nothing.
    assert [entry.field for entry in found.fields] == ["3"]
    assert found["3"].located is False
    assert found["3"].data_range is None
    assert found.consumed == 20
    assert found.remaining == iso8583.DataRange(20, 32)


def test_catalog_truncated_field_data() -> None:
    spec = _build_spec()
    # DE2 declares 10 bytes but only 4 are present.
    s = b"0200" + b"4000000000000000" + b"10" + b"1234"

    found = iso8583.catalog(s, spec)

    assert found.truncated is True
    assert found.complete is False
    assert found["2"].truncated is True
    assert found["2"].declared_length == 10
    assert found["2"].length_prefix == iso8583.DataRange(20, 22)
    assert found["2"].data_range == iso8583.DataRange(22, 26)
    assert found["2"].value is None
    # The bytes that are present belong to DE2 and are consumed; only the
    # missing tail is recorded as truncated.
    assert found.consumed == 26
    assert found.remaining.is_empty

    with pytest.raises(iso8583.CatalogError) as e:
        iso8583.catalog(s, spec, strict=True)
    assert e.value.args[0] == "Field data is 4 bytes, expecting 10: field 2 pos 22"


def test_catalog_truncated_length_prefix_keeps_later_declarations() -> None:
    spec = _build_spec()
    # DE2 and DE3 are declared. DE2's 2-byte length prefix is empty.
    s = b"0200" + _bitmap(2, 3)

    found = iso8583.catalog(s, spec)

    assert [entry.field for entry in found.fields] == ["2", "3"]
    assert found["2"].length_prefix == iso8583.DataRange(20, 20)
    assert found["2"].data_range is None
    assert found["2"].declared_length is None
    assert found["2"].truncated is True
    # Later declarations do not become business data ranges.
    assert found["3"].located is False
    assert found["3"].data_range is None
    assert found.consumed == 20
    assert found.remaining.is_empty


def test_catalog_bitmap_encodings() -> None:
    spec = _build_spec()
    spec["p"]["data_enc"] = "b"
    s = b"0200" + b"\x40\x00\x00\x00\x00\x00\x00\x00" + b"04" + b"1234"

    found = iso8583.catalog(s, spec)

    assert found.bitmaps[0].start == 4
    assert found.bitmaps[0].end == 12
    assert found["2"].length_prefix == iso8583.DataRange(12, 14)
    assert found["2"].data_range == iso8583.DataRange(14, 18)


def test_catalog_secondary_bitmap_encoding() -> None:
    spec = _build_spec()
    spec["1"]["data_enc"] = "b"
    s = (
        b"0200"
        + b"8000000000000000"
        + bytes.fromhex(_bitmap(66, 102, offset=64).decode())
        + b"A"
        + b"02"
        + b"34"
    )

    found = iso8583.catalog(s, spec)

    assert found.bitmaps[1].start == 20
    assert found.bitmaps[1].end == 28
    assert found["66"].data_range == iso8583.DataRange(28, 29)
    assert found["102"].length_prefix == iso8583.DataRange(29, 31)
    assert found["102"].data_range == iso8583.DataRange(31, 33)


def test_catalog_header_offsets() -> None:
    spec = _build_spec()
    spec["h"]["max_len"] = 3
    s = b"XYZ" + b"0200" + b"4000000000000000" + b"04" + b"1234"

    found = iso8583.catalog(s, spec)

    assert found.bitmaps[0].start == 7
    assert found.bitmaps[0].end == 23
    assert found["2"].length_prefix == iso8583.DataRange(23, 25)
    assert found["2"].data_range == iso8583.DataRange(25, 29)
    assert found["2"].value == "1234"


def test_catalog_nibbles() -> None:
    spec = _build_spec()
    spec["2"]["data_enc"] = "b"
    spec["2"]["len_enc"] = "bcd"
    spec["2"]["len_type"] = 1
    spec["2"]["len_count"] = "nibbles"
    spec["2"]["max_len"] = 19
    spec["2"]["right_pad"] = "0"

    s = b"0200" + b"4000000000000000" + b"\x03" + b"\x12\x30"

    found = iso8583.catalog(s, spec)

    assert found["2"].declared_length == 3
    assert found["2"].length_count == "nibbles"
    assert found["2"].length_prefix == iso8583.DataRange(20, 21)
    assert found["2"].data_range == iso8583.DataRange(21, 23)
    assert found["2"].value == "123"


def test_catalog_bitmap_ascii_is_case_insensitive() -> None:
    spec = _build_spec()
    upper = iso8583.catalog(
        b"0200" + b"400A000000000000" + b"04" + b"1234", spec
    )
    lower = iso8583.catalog(
        b"0200" + b"400a000000000000" + b"04" + b"1234", spec
    )

    assert upper.bitmaps[0].enabled == (2, 13, 15)
    assert upper.bitmaps[0].complete is True
    assert lower.bitmaps[0].enabled == upper.bitmaps[0].enabled
    assert lower["2"].data_range == upper["2"].data_range


def test_catalog_error_for_non_hex_bitmap() -> None:
    spec = _build_spec()
    with pytest.raises(iso8583.CatalogError) as e:
        iso8583.catalog(b"0200" + b"nothexbitmapdata!", spec)
    assert e.value.args[0] == "Failed to decode field, non-hex data: field p pos 4"


def test_catalog_unknown_field_in_spec() -> None:
    spec = _build_spec()
    del spec["39"]

    # Non-strict: the declaration is reported, but it cannot be located.
    found = iso8583.catalog(b"0200" + _bitmap(39) + b"00", spec)
    assert [entry.field for entry in found.fields] == ["39"]
    assert found["39"].known is False
    assert found["39"].located is False
    assert found["39"].name == ""

    # Strict: the specification gap is an error.
    with pytest.raises(iso8583.CatalogError) as e:
        iso8583.catalog(b"0200" + _bitmap(39) + b"00", spec, strict=True)
    assert e.value.args[0] == "Field is not defined in the specification: field 39 pos 20"


def test_catalog_input_type() -> None:
    spec = _build_spec()
    with pytest.raises(TypeError, match="must be bytes or bytearray, not dict"):
        iso8583.catalog({}, spec)  # type: ignore
    with pytest.raises(TypeError, match="must be bytes or bytearray, not str"):
        iso8583.catalog("0200", spec)  # type: ignore


def test_catalog_as_dict_is_json_compatible() -> None:
    spec = _build_spec()
    s = b"0200" + _bitmap(2, 3, 4, 39) + b"041234" + b"111111" + b"000000012345" + b"00"

    found = iso8583.catalog(s, spec)
    dumped = json.dumps(found.as_dict())

    assert isinstance(dumped, str)
    data = json.loads(dumped)
    assert data["complete"] is True
    assert data["fields"][0]["field"] == "2"
    assert data["fields"][0]["bitmap"]["ordinal"] == 2
    assert data["fields"][0]["data_range"] == {"start": 22, "end": 26}
    assert data["bitmaps"][0]["layer_name"] == "primary"


def test_catalog_matches_decode_for_complete_message() -> None:
    spec = _build_spec()
    s = b"0200" + _bitmap(2, 3, 4, 39) + b"041234" + b"111111" + b"000000012345" + b"00"

    decoded, _ = iso8583.decode(s, spec)
    found = iso8583.catalog(s, spec)

    for entry in found.fields:
        assert entry.value == decoded[entry.field]
        assert entry.data_range is not None
        assert entry.data_range.length == len(decoded[entry.field])
