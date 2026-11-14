import copy
import json
import pickle
import typing

import iso8583
import iso8583.specs
import pytest


def _spec() -> typing.Any:
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    spec["t"]["data_enc"] = "ascii"
    spec["p"]["data_enc"] = "ascii"
    spec["2"].update(
        {"data_enc": "ascii", "len_enc": "ascii", "len_type": 2, "max_len": 6}
    )
    spec["3"].update({"data_enc": "ascii", "len_type": 0, "max_len": 6})
    return spec


# Bitmap 0x60 enables fields 2 (LLVAR) and 3 (fixed).
_PREFIX = b"0200" + b"6000000000000000"
_FIELD_2 = b"06123456"
_FIELD_3 = b"ABCDEF"


def _decode_error(s: bytes, spec: typing.Any) -> iso8583.DecodeError:
    with pytest.raises(iso8583.DecodeError) as exc:
        iso8583.decode(s, spec=spec)
    return exc.value


def test_truncated_length_and_truncated_payload_are_distinct() -> None:
    spec = _spec()

    length_error = _decode_error(_PREFIX + b"1", spec)
    payload_error = _decode_error(_PREFIX + b"06123", spec)

    assert length_error.diagnostic.kind == "truncated_field_length"
    assert length_error.diagnostic.part == "length"
    assert payload_error.diagnostic.kind == "truncated_field_data"
    assert payload_error.diagnostic.part == "data"
    assert length_error.diagnostic.completed_range == {"start": 0, "end": 20}
    assert payload_error.diagnostic.completed_range == {"start": 0, "end": 22}


def test_truncated_length_reports_error_and_undetermined_ranges() -> None:
    spec = _spec()
    s = _PREFIX + b"1"

    error = _decode_error(s, spec)
    diagnostic = error.diagnostic

    assert diagnostic["error"] == {
        "part": "length",
        "kind": "truncated_field_length",
        "start": 20,
        "end": 21,
        "expected_end": 22,
    }
    assert diagnostic["undetermined"] == {"start": 20, "end": 21}
    field_2 = diagnostic.fields["2"]
    assert field_2["parts"]["length"] == {
        "start": 20,
        "end": 21,
        "expected_end": 22,
        "status": "error",
    }
    assert field_2["parts"]["data"] == {
        "start": 21,
        "end": 21,
        "expected_end": None,
        "status": "not_reached",
    }


def test_truncated_payload_keeps_length_header_valid() -> None:
    spec = _spec()
    s = _PREFIX + b"06123"

    diagnostic = _decode_error(s, spec).diagnostic

    assert diagnostic["error"] == {
        "part": "data",
        "kind": "truncated_field_data",
        "start": 22,
        "end": 25,
        "expected_end": 28,
    }
    assert diagnostic["undetermined"] == {"start": 22, "end": 25}
    field_2 = diagnostic.fields["2"]
    assert field_2["declared_length"] == 6
    assert field_2["parts"]["length"] == {
        "start": 20,
        "end": 22,
        "expected_end": 22,
        "status": "ok",
    }
    assert field_2["parts"]["data"] == {
        "start": 22,
        "end": 25,
        "expected_end": 28,
        "status": "error",
    }


def test_completed_fields_remain_auditable_after_later_error() -> None:
    spec = _spec()
    s = _PREFIX + _FIELD_2 + b"AB"

    diagnostic = _decode_error(s, spec).diagnostic

    assert diagnostic.completed_range == {"start": 0, "end": 28}
    field_2 = diagnostic.fields["2"]
    assert field_2["status"] == "ok"
    assert field_2["value"] == "123456"
    assert field_2["parts"] == {
        "length": {
            "start": 20,
            "end": 22,
            "expected_end": 22,
            "status": "ok",
        },
        "data": {
            "start": 22,
            "end": 28,
            "expected_end": 28,
            "status": "ok",
        },
    }
    assert diagnostic.fields["3"]["status"] == "error"


def test_secret_field_keeps_positions_but_not_value() -> None:
    spec = _spec()
    spec["2"]["sensitive"] = True
    s = _PREFIX + _FIELD_2 + b"AB"

    error = _decode_error(s, spec)
    diagnostic = error.diagnostic.to_dict()

    field_2 = diagnostic["fields"]["2"]
    assert field_2["redacted"] is True
    assert "value" not in field_2
    assert field_2["start"] == 20
    assert field_2["end"] == 28
    assert "123456" not in json.dumps(diagnostic)
    assert b"123456" not in bytes(error.s)
    assert "2" not in error.doc_dec
    assert "2" not in error.doc_enc
    assert error.redactions == [{"field": "2", "start": 20, "end": 28}]


def test_spec_secret_alias_variants_are_respected() -> None:
    for key in ("sensitive", "secret", "masked", "is_sensitive"):
        spec = _spec()
        spec["2"][key] = True

        diagnostic = iso8583.decode_diagnostic(_PREFIX + _FIELD_2, spec)

        assert diagnostic.fields["2"]["redacted"] is True
        assert "123456" not in json.dumps(diagnostic.to_dict())


def test_secret_field_error_message_contains_no_business_value() -> None:
    spec = _spec()
    spec["2"]["sensitive"] = True
    s = _PREFIX + b"06" + b"\xffSECRETVALUE"

    error = _decode_error(s, spec)

    assert "SECRETVALUE" not in str(error)
    assert "SECRETVALUE" not in error.msg
    assert b"SECRETVALUE" not in bytes(error.s)
    assert "SECRETVALUE" not in json.dumps(error.to_dict())


def test_secret_field_truncated_does_not_leak_partial_payload() -> None:
    spec = _spec()
    spec["2"]["secret"] = True
    s = _PREFIX + b"06SECR"

    error = _decode_error(s, spec)

    assert b"SECR" not in bytes(error.s)
    assert b"SECR" not in bytes(
        error.diagnostic.to_dict()["fields"]["2"].get("value", b"")
    )
    assert error.diagnostic.kind == "truncated_field_data"
    assert error.diagnostic.fields["2"]["parts"]["length"]["status"] == "ok"
    assert b"*" in bytes(error.s)


def test_secret_field_header_truncation_masks_following_payload() -> None:
    spec = _spec()
    spec["2"]["sensitive"] = True
    s = _PREFIX + b"1" + b"SECRETPAYLOAD"

    error = _decode_error(s, spec)

    assert error.diagnostic.kind == "invalid_field_length"
    assert b"SECRETPAYLOAD" not in bytes(error.s)
    assert b"RETPAYLOAD" not in bytes(error.s)


def test_sensitive_fields_argument_redacts_without_spec_change() -> None:
    spec = _spec()
    s = _PREFIX + b"06" + b"\xffSECRETVALUE"

    with pytest.raises(iso8583.DecodeError) as exc:
        iso8583.decode(s, spec, sensitive_fields=["2"])

    error = exc.value
    assert b"SECRETVALUE" not in bytes(error.s)
    assert error.diagnostic.fields["2"]["redacted"] is True
    assert "SECRETVALUE" not in json.dumps(error.to_dict())


def test_error_diagnostic_is_json_and_pickle_safe() -> None:
    spec = _spec()
    spec["2"]["sensitive"] = True
    error = _decode_error(_PREFIX + b"1", spec)

    payload = json.dumps(error.to_dict())
    assert "truncated_field_length" in payload
    assert error.diagnostic.to_dict() == json.loads(payload)["diagnostic"]

    clone = pickle.loads(pickle.dumps(error))
    assert clone.diagnostic == error.diagnostic
    assert clone.args[0] == error.args[0]
    assert clone.doc_dec == error.doc_dec
    assert clone.doc_enc == error.doc_enc


def test_decode_diagnostic_never_raises_and_matches_error_diagnostic() -> None:
    spec = _spec()
    s = _PREFIX + b"06123"

    assert iso8583.decode_diagnostic(s, spec).to_dict() == (
        _decode_error(s, spec).diagnostic.to_dict()
    )


def test_decode_diagnostic_success_reports_ok_and_redacts() -> None:
    spec = _spec()
    spec["2"]["sensitive"] = True
    s = _PREFIX + _FIELD_2 + _FIELD_3

    diagnostic = iso8583.decode_diagnostic(s, spec)

    assert diagnostic.status == "ok"
    assert diagnostic.completed_range == {"start": 0, "end": len(s)}
    assert diagnostic.fields["2"]["redacted"] is True
    assert "value" not in diagnostic.fields["2"]
    assert diagnostic.fields["3"]["value"] == "ABCDEF"
    assert "123456" not in json.dumps(diagnostic.to_dict())


def test_decode_diagnostic_type_error_is_preserved() -> None:
    with pytest.raises(
        TypeError, match="Encoded ISO8583 data must be bytes or bytearray, not dict"
    ):
        iso8583.decode_diagnostic({}, _spec())  # type: ignore


def test_diagnostic_object_serializes_directly() -> None:
    spec = _spec()
    s = _PREFIX + b"06123"

    diagnostic = iso8583.decode_diagnostic(s, spec)

    assert diagnostic.completed_end == 22
    assert diagnostic.error_position == 22
    assert diagnostic.undetermined_start == 22
    assert json.loads(diagnostic.to_json()) == diagnostic.to_dict()
