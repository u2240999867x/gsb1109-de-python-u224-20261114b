import json

import iso8583
import iso8583.specs
import pytest


def partner_schema() -> dict:
    return {
        "version": "partner-2026.1",
        "children": [
            {
                "name": "01",
                "type": "data",
                "len_type": 2,
                "len_enc": "ascii",
                "max_len": 99,
                "data_enc": "ascii",
            },
            {
                "name": "02",
                "type": "group",
                "len_type": 2,
                "len_enc": "ascii",
                "max_len": 99,
                "repeat": 2,
                "children": [
                    {
                        "name": "A",
                        "type": "data",
                        "len_type": 1,
                        "len_enc": "ascii",
                        "max_len": 9,
                        "data_enc": "ascii",
                    },
                    {
                        "name": "B",
                        "type": "data",
                        "len_type": 0,
                        "max_len": 2,
                        "data_enc": "ascii",
                    },
                ],
            },
        ],
    }


def decode_payload(payload: bytes, schema: dict):
    message, _ = iso8583.encode(
        {"t": "0200", "48": payload.decode("ascii")},
        iso8583.specs.default_ascii,
    )
    return iso8583.decode(
        message,
        iso8583.specs.default_ascii,
        subitem_schema=schema,
    )


def test_subitem_table_tracks_path_order_range_and_version() -> None:
    payload = b"03ABC" + b"0411XY" + b"0412ZW"

    _, doc_enc = decode_payload(payload, partner_schema())
    table = doc_enc["48"]["subitems"]

    assert table.version == "partner-2026.1"
    assert table.field == "48"
    assert table.base_offset == 23
    assert not table.truncated
    assert [row.sequence for row in table] == list(range(1, 8))
    assert [row.path_str for row in table] == [
        "48/01",
        "48/02[1]",
        "48/02[1]/A",
        "48/02[1]/B",
        "48/02[2]",
        "48/02[2]/A",
        "48/02[2]/B",
    ]
    assert [row.occurrence for row in table] == [1, 1, 1, 1, 2, 1, 1]
    assert [row.value for row in table] == [
        "ABC",
        None,
        "1",
        "XY",
        None,
        "2",
        "ZW",
    ]


def test_subitem_offsets_link_back_to_original_bytes() -> None:
    payload = b"03ABC" + b"0411XY" + b"0412ZW"
    message, _ = iso8583.encode(
        {"t": "0200", "48": payload.decode("ascii")},
        iso8583.specs.default_ascii,
    )

    _, doc_enc = iso8583.decode(
        message,
        iso8583.specs.default_ascii,
        subitem_schema=partner_schema(),
    )
    table = doc_enc["48"]["subitems"]

    first = table.find("48/01")
    assert first is not None
    assert first.start == 23
    assert first.end == 28
    assert first.length_start == 23
    assert first.length_end == 25
    assert first.value_start == 25
    assert first.value_end == 28
    assert first.raw == b"03ABC"
    assert table.link(first) == b"03ABC"
    assert message[first.value_start : first.value_end] == b"ABC"

    second_group = table.find("48/02[2]")
    assert second_group is not None
    assert second_group.start == 34
    assert second_group.value_start == 36
    assert second_group.value_end == 40
    assert table.link(second_group) == b"0412ZW"


def test_repeated_path_lookup_with_indexes() -> None:
    _, doc_enc = decode_payload(
        b"03ABC" + b"0411XY" + b"0412ZW", partner_schema()
    )
    table = doc_enc["48"]["subitems"]

    assert table.find("48/02[2]/A").value == "2"  # type: ignore[union-attr]
    assert [row.value for row in table.find_all("48/02/B")] == ["XY", "ZW"]
    assert table.find("48/02[3]/A") is None


def test_repeat_until_end() -> None:
    schema = {
        "version": "v1",
        "children": [
            {
                "name": "T",
                "type": "data",
                "len_type": 1,
                "len_enc": "ascii",
                "max_len": 9,
                "repeat": "until_end",
            }
        ],
    }
    table = iso8583.parse_subitems(b"1A1B1C", schema)

    assert [row.path_str for row in table] == ["48/T[1]", "48/T[2]", "48/T[3]"]
    assert [row.value for row in table] == ["A", "B", "C"]
    assert table.complete


def test_nibble_lengths_use_nibble_ranges() -> None:
    schema = {
        "version": "nibble-v3",
        "children": [
            {
                "name": "N",
                "type": "data",
                "len_type": 2,
                "len_enc": "ascii",
                "len_count": "nibbles",
                "max_len": 99,
                "data_enc": "b",
                "right_pad": "0",
            }
        ],
    }
    table = iso8583.parse_subitems(b"03\x12\x30", schema, base_offset=10)
    row = table[0]

    # Two ASCII length digits count three nibbles of binary value.  The
    # right-padded zero is only a storage nibble, not part of the value.
    assert row.length_unit == "nibbles"
    assert row.declared_length == 3
    assert row.length_start == 10
    assert row.length_end == 12
    assert row.start == 10
    assert row.end == 14
    assert row.value_start == 12
    assert row.nibble_start == 24
    assert row.nibble_end == 27
    assert row.value == "123"
    assert row.value_raw == b"\x12\x30"
    assert row.pad_removed == "right"
    assert table.link(row) == b"03\x12\x30"


def test_left_pad_is_excluded_from_nibble_range() -> None:
    schema = {
        "version": "v1",
        "children": [
            {
                "name": "N",
                "type": "data",
                "len_type": 1,
                "len_enc": "ascii",
                "len_count": "nibbles",
                "max_len": 9,
                "data_enc": "b",
                "left_pad": "0",
            }
        ],
    }
    table = iso8583.parse_subitems(b"3" + b"\x01\x23", schema, base_offset=5)
    row = table[0]

    assert row.value == "123"
    assert row.pad_removed == "left"
    assert row.value_start == 6
    assert row.nibble_start == 13
    assert row.nibble_end == 16


def test_odd_nibbles_without_pad_drop_storage_nibble() -> None:
    schema = {
        "version": "v1",
        "children": [
            {
                "name": "N",
                "type": "data",
                "len_type": 0,
                "len_count": "nibbles",
                "max_len": 3,
                "data_enc": "b",
            }
        ],
    }
    table = iso8583.parse_subitems(b"\x12\x30", schema)
    row = table[0]

    assert row.value == "123"
    assert row.pad_removed == "storage"
    assert row.value_units == 3
    assert row.nibble_start == 0
    assert row.nibble_end == 3


def test_child_cannot_use_parent_pad_nibble() -> None:
    schema = {
        "version": "v1",
        "children": [
            {
                "name": "G",
                "type": "group",
                "len_type": 1,
                "len_enc": "ascii",
                "len_count": "nibbles",
                "max_len": 99,
                "children": [
                    {
                        "name": "X",
                        "type": "data",
                        "len_type": 0,
                        "len_count": "nibbles",
                        "max_len": 9,
                        "data_enc": "b",
                    }
                ],
            }
        ],
    }
    table = iso8583.parse_subitems(b"3\x12\x30", schema)
    group, child = table

    assert group.nibble_end == 5
    assert child.truncated
    assert child.truncation_reason == "parent_boundary"
    assert child.nibble_start == 2
    assert child.nibble_end == 5
    assert child.value == "123"


def test_group_without_length_consumes_rest_of_parent() -> None:
    schema = {
        "version": "v1",
        "children": [
            {
                "name": "G",
                "type": "group",
                "children": [
                    {
                        "name": "X",
                        "type": "data",
                        "len_type": 1,
                        "len_enc": "ascii",
                        "max_len": 9,
                        "repeat": "until_end",
                    }
                ],
            }
        ],
    }
    table = iso8583.parse_subitems(b"1A1B", schema)

    assert [row.path_str for row in table] == ["48/G", "48/G/X[1]", "48/G/X[2]"]
    assert table.find("48/G").value_end == 4  # type: ignore[union-attr]
    assert table.complete


def test_subitem_length_overflow_marks_only_group_tail_incomplete() -> None:
    # Bitmap enables fields 48 and 49.  Field 48 contains a subitem that
    # declares five bytes but only two are inside field 48.  Field 49 follows
    # as a normal fixed three-byte ISO field and must still be decoded.
    bitmap = b"0000000000018000"
    message = b"0200" + bitmap + b"004" + b"05AB" + b"840"
    schema = {
        "version": "boundary-v1",
        "children": [
            {
                "name": "99",
                "type": "data",
                "len_type": 2,
                "len_enc": "ascii",
                "max_len": 99,
                "data_enc": "ascii",
            }
        ],
    }

    doc_dec, doc_enc = iso8583.decode(
        message,
        iso8583.specs.default_ascii,
        subitem_schema=schema,
    )

    assert doc_dec["49"] == "840"
    assert doc_dec["48"] == "05AB"
    table = doc_enc["48"]["subitems"]
    row = table[0]
    assert row.truncated
    assert row.truncation_reason == "field_boundary"
    assert row.start == 23
    assert row.end == 27
    assert row.value_start == 25
    assert row.value_end == 27
    assert row.value == "AB"
    assert row.declared_length == 5
    assert table.truncated_rows == (row,)


def test_parent_group_boundary_limits_nested_child() -> None:
    schema = {
        "version": "v1",
        "children": [
            {
                "name": "G",
                "type": "group",
                "len_type": 2,
                "len_enc": "ascii",
                "max_len": 99,
                "children": [
                    {
                        "name": "X",
                        "type": "data",
                        "len_type": 2,
                        "len_enc": "ascii",
                        "max_len": 99,
                    }
                ],
            }
        ],
    }
    # Group declares four bytes but carries only "10AB"; its child declares
    # ten bytes and must stop at the group's content boundary.
    table = iso8583.parse_subitems(b"0410AB", schema)

    group = table.find("48/G")
    child = table.find("48/G/X")
    assert group is not None and child is not None
    assert child.truncated
    assert child.truncation_reason == "parent_boundary"
    assert child.end == group.value_end
    assert child.value == "AB"


def test_max_len_violation_does_not_hide_later_siblings() -> None:
    schema = {
        "version": "v1",
        "children": [
            {
                "name": "A",
                "type": "data",
                "len_type": 1,
                "len_enc": "ascii",
                "max_len": 2,
            },
            {
                "name": "B",
                "type": "data",
                "len_type": 1,
                "len_enc": "ascii",
                "max_len": 9,
            },
        ],
    }
    table = iso8583.parse_subitems(b"4WXYZ1Q", schema)

    first, second = table
    assert first.truncated
    assert first.truncation_reason == "max_length"
    assert first.value == "WXYZ"
    assert not second.truncated
    assert second.value == "Q"


def test_invalid_length_prefix_is_lenient_by_default_and_strict_on_request() -> None:
    schema = {
        "version": "v1",
        "children": [
            {
                "name": "X",
                "type": "data",
                "len_type": 2,
                "len_enc": "ascii",
                "max_len": 99,
            }
        ],
    }

    table = iso8583.parse_subitems(b"XXdata", schema)
    assert table.truncated
    assert table[0].truncation_reason == "length_prefix"
    assert table[0].value is None

    with pytest.raises(iso8583.SubitemError, match="Failed to decode subitem length"):
        iso8583.parse_subitems(b"XXdata", schema, strict=True)


def test_unknown_data_encoding_is_reported_in_strict_mode() -> None:
    schema = {
        "version": "v1",
        "children": [
            {
                "name": "X",
                "type": "data",
                "len_type": 1,
                "len_enc": "ascii",
                "max_len": 9,
                "data_enc": "not-a-codec",
            }
        ],
    }
    table = iso8583.parse_subitems(b"1A", schema)
    assert table[0].value is None

    with pytest.raises(iso8583.SubitemError, match="Unknown data encoding"):
        iso8583.parse_subitems(b"1A", schema, strict=True)


def test_schema_validation_rejects_ambiguous_schemas() -> None:
    with pytest.raises(iso8583.SubitemSchemaError, match="non-empty 'version'"):
        iso8583.parse_subitems(b"", {"children": []})

    with pytest.raises(iso8583.SubitemSchemaError, match="must have a name"):
        iso8583.parse_subitems(b"", {"version": "v1", "children": [{}]})

    with pytest.raises(iso8583.SubitemSchemaError, match="max_len is required"):
        iso8583.parse_subitems(
            b"",
            {
                "version": "v1",
                "children": [
                    {"name": "X", "len_type": 1, "max_len": None},
                ],
            },
        )

    with pytest.raises(iso8583.SubitemSchemaError, match="unique"):
        iso8583.parse_subitems(
            b"",
            {
                "version": "v1",
                "children": [
                    {"name": "X", "len_type": 1, "max_len": 9},
                    {"name": "X", "len_type": 1, "max_len": 9},
                ],
            },
        )


def test_schema_configured_on_field_spec_and_absent_field() -> None:
    spec = dict(iso8583.specs.default_ascii)
    spec["48"] = dict(spec["48"])
    spec["48"]["subitems"] = {
        "version": "spec-v1",
        "children": [
            {
                "name": "X",
                "type": "data",
                "len_type": 1,
                "len_enc": "ascii",
                "max_len": 9,
            }
        ],
    }

    message, _ = iso8583.encode({"t": "0200", "48": "1A"}, spec)
    _, doc_enc = iso8583.decode(message, spec)
    assert doc_enc["48"]["subitems"][0].value == "A"

    message, _ = iso8583.encode({"t": "0200"}, spec)
    _, doc_enc = iso8583.decode(message, spec)
    assert "subitems" not in doc_enc["48"] if "48" in doc_enc else True
    assert "48" not in doc_enc


def test_decode_subitems_helper_and_json_serialization() -> None:
    message, _ = iso8583.encode(
        {"t": "0200", "48": "03ABC"},
        iso8583.specs.default_ascii,
    )
    table = iso8583.decode_subitems(
        message,
        iso8583.specs.default_ascii,
        {
            "version": "json-v1",
            "children": [
                {
                    "name": "01",
                    "type": "data",
                    "len_type": 2,
                    "len_enc": "ascii",
                    "max_len": 99,
                }
            ],
        },
    )

    assert table[0].value == "ABC"
    payload = table.to_dicts()
    assert payload[0]["path"] == "48/01"
    assert payload[0]["sequence"] == 1
    assert payload[0]["version"] == "json-v1"
    assert payload[0]["raw_hex"] == "3033414243"
    assert json.loads(json.dumps(payload))[0]["value"] == "ABC"


def test_non_bytes_input_is_rejected() -> None:
    with pytest.raises(TypeError, match="bytes or bytearray"):
        iso8583.parse_subitems(  # type: ignore[arg-type]
            "text", {"version": "v1", "children": []}
        )
