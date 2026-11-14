import copy
import json
import re
import typing

import iso8583
import iso8583.specs
import pytest
from iso8583.candidates import CandidateSpec, decode_candidates


def make_spec(header: typing.Optional[str] = "HDR1") -> typing.Dict[str, typing.Any]:
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    if header is None:
        spec["h"]["max_len"] = 0
    else:
        spec["h"].update(
            data_enc="ascii",
            len_enc="ascii",
            len_type=0,
            max_len=len(header),
        )
    return spec


def make_raw(
    doc: typing.Dict[str, str],
    spec: typing.Optional[typing.Dict[str, typing.Any]] = None,
) -> bytes:
    raw, _ = iso8583.encode(dict(doc), spec or make_spec())
    return bytes(raw)


def test_single_eligible_candidate_is_selected() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(raw, {"only": spec})

    assert report.selected_name == "only"
    assert report.ambiguous is False
    assert report.common_fields["3"] == "123456"
    assert report.candidates[0].checks[0].name == "decode"
    assert report.candidates[0].checks[-1].name == "consumption"


def test_reordering_candidates_does_not_change_result() -> None:
    first = make_spec()
    second = copy.deepcopy(first)
    second["22"]["data_enc"] = "b"
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456", "22": "051"}, first)

    forward = decode_candidates(
        raw,
        [CandidateSpec("legacy", first), CandidateSpec("alternate", second)],
    )
    reverse = decode_candidates(
        raw,
        [CandidateSpec("alternate", second), CandidateSpec("legacy", first)],
    )

    assert forward.as_dict() == reverse.as_dict()
    assert forward.ambiguous is True
    assert forward.selected is None


def test_trailing_candidate_cannot_win() -> None:
    llvar = make_spec()
    fixed = copy.deepcopy(llvar)
    fixed["2"]["len_type"] = 0
    fixed["2"]["max_len"] = 10
    raw = make_raw({"h": "HDR1", "t": "0200", "2": "1234567890"}, llvar)

    report = decode_candidates(raw, {"llvar": llvar, "fixed": fixed})

    assert report.selected_name == "llvar"
    trailing = next(
        candidate for candidate in report.candidates if candidate.name == "fixed"
    )
    assert trailing.status == "trailing"
    assert trailing.eligible is False
    assert trailing.consumed < trailing.total
    assert trailing.remaining_hex == "3930"
    assert trailing.reasons == (
        "2 trailing bytes after decoded fields: 3930",
    )


def test_trailing_candidate_does_not_win_over_rejected_candidate() -> None:
    llvar = make_spec("HDR1")
    fixed = copy.deepcopy(llvar)
    fixed["2"]["len_type"] = 0
    fixed["2"]["max_len"] = 10
    raw = make_raw({"h": "HDR2", "t": "0200", "2": "1234567890"}, llvar)

    report = decode_candidates(
        raw,
        {
            "llvar": CandidateSpec("llvar", llvar, header="HDR1"),
            "fixed": CandidateSpec("fixed", fixed, header="HDR1"),
        },
    )

    assert report.selected is None
    assert report.outcome == "unresolved"
    assert report.ambiguous is False
    assert all(not candidate.eligible for candidate in report.candidates)


def test_header_filter() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    allowed = decode_candidates(raw, {"new": CandidateSpec("new", spec, header="HDR2")})
    assert allowed.selected is None
    assert allowed.candidates[0].status == "rejected"
    assert allowed.candidates[0].reasons == ("header 'HDR1' is not allowed",)

    prefix = decode_candidates(
        raw,
        {"old": CandidateSpec("old", spec, header_prefix=re.compile(r"HDR[0-9]"))},
    )
    assert prefix.selected_name == "old"


def test_header_prefix_string_and_bytes() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    string_prefix = decode_candidates(
        raw, {"text": CandidateSpec("text", spec, header_prefix="HD")}
    )
    assert string_prefix.selected_name == "text"

    bytes_prefix = decode_candidates(
        raw, {"raw": CandidateSpec("raw", spec, header_prefix=b"HD")}
    )
    assert bytes_prefix.selected_name == "raw"


def test_bitmap_range_filter() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    narrow = decode_candidates(
        raw, {"narrow": CandidateSpec("narrow", spec, allowed_fields={2})}
    )
    assert narrow.selected is None
    assert narrow.candidates[0].status == "rejected"
    assert narrow.candidates[0].reasons == (
        "fields outside allowed bitmap range: 3; allowed 2",
    )

    wide = decode_candidates(
        raw, {"wide": CandidateSpec("wide", spec, allowed_fields=["2", "3"])}
    )
    assert wide.selected_name == "wide"


def test_required_and_forbidden_fields() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    missing = decode_candidates(
        raw, {"missing": CandidateSpec("missing", spec, required_fields=["39"])}
    )
    assert missing.candidates[0].reasons == ("required fields missing: 39",)

    forbidden = decode_candidates(
        raw, {"forbidden": CandidateSpec("forbidden", spec, forbidden_fields=["3"])}
    )
    assert forbidden.candidates[0].reasons == ("forbidden fields present: 3",)


def test_field_validation() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "ABCDEF"})

    regex = decode_candidates(
        raw,
        {
            "regex": CandidateSpec(
                "regex", spec, validators={"3": re.compile(r"[0-9]+")}
            )
        },
    )
    assert regex.candidates[0].reasons == ("field 3 failed validation",)

    exact = decode_candidates(
        raw, {"exact": CandidateSpec("exact", spec, validators={"3": "ABCDEF"})}
    )
    assert exact.selected_name == "exact"

    choices = decode_candidates(
        raw, {"choices": CandidateSpec("choices", spec, validators={"3": ["ABCDEF"]})}
    )
    assert choices.selected_name == "choices"


def test_common_fields_and_meaning_divergence() -> None:
    legacy = make_spec()
    modern = make_spec()
    modern["22"]["data_enc"] = "b"
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456", "22": "051"}, legacy)

    report = decode_candidates(raw, {"legacy": legacy, "modern": modern})

    assert report.selected is None
    assert report.ambiguous is True
    assert report.outcome == "ambiguous"
    assert report.common_fields == {
        "h": "HDR1",
        "t": "0200",
        "p": "2000040000000000",
        "3": "123456",
    }
    divergence = report.divergences[0]
    assert divergence.field == "22"
    assert divergence.kind == "meaning"
    assert divergence.values["legacy"] == "051"
    assert divergence.descriptions["legacy"] == "Point-of-Service Entry Mode"


def test_same_meaning_candidates_have_no_divergence() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(
        raw,
        {
            "first": CandidateSpec("first", spec, allowed_fields={2, 3}),
            "second": CandidateSpec("second", spec, allowed_fields={3}),
        },
    )

    assert report.selected is None
    assert report.ambiguous is True
    assert report.divergences == ()
    assert report.common_fields == {
        "h": "HDR1",
        "t": "0200",
        "p": "2000000000000000",
        "3": "123456",
    }


def test_priority_is_semantic_tie_breaker() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    candidates = {
        "alpha": CandidateSpec("alpha", spec),
        "zeta": CandidateSpec("zeta", spec, priority=1),
    }
    report = decode_candidates(raw, candidates)
    assert report.selected_name == "zeta"

    reversed_report = decode_candidates(raw, dict(reversed(list(candidates.items()))))
    assert reversed_report.selected_name == "zeta"


def test_equal_priority_remains_ambiguous() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(
        raw,
        {
            "alpha": CandidateSpec("alpha", spec, priority=1),
            "zeta": CandidateSpec("zeta", spec, priority=1),
        },
    )

    assert report.selected is None
    assert report.ambiguous is True


def test_decode_error_is_reported_not_raised() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})
    broken = copy.deepcopy(spec)
    broken["3"]["max_len"] = 2

    report = decode_candidates(raw, {"fine": spec, "broken": broken})

    assert report.selected_name == "fine"
    broken_result = next(
        candidate for candidate in report.candidates if candidate.name == "broken"
    )
    assert broken_result.status == "trailing"
    assert broken_result.eligible is False
    assert "4 trailing bytes" in broken_result.reasons[0]


def test_plain_spec_iterables_are_named_by_content() -> None:
    spec = make_spec()
    other = copy.deepcopy(spec)
    other["2"]["len_type"] = 0
    other["2"]["max_len"] = 10
    raw = make_raw({"h": "HDR1", "t": "0200", "2": "1234567890"}, spec)

    forward = decode_candidates(raw, [spec, other])
    reverse = decode_candidates(raw, [other, copy.deepcopy(spec)])

    assert forward.as_dict() == reverse.as_dict()
    assert forward.selected is not None
    assert all(
        candidate.name.startswith("spec-") for candidate in forward.candidates
    )


def test_plain_spec_mapping_is_one_candidate() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(raw, spec)

    assert len(report.candidates) == 1
    assert report.selected is not None


def test_report_is_json_compatible() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(raw, {"one": spec})

    payload = json.loads(json.dumps(report.as_dict()))
    assert payload["selected"] == "one"
    assert payload["outcome"] == "selected"
    assert payload["common_fields"]["3"] == "123456"


def test_duplicate_candidate_names_are_rejected() -> None:
    spec = make_spec()
    other = copy.deepcopy(spec)
    other["3"]["max_len"] = 2
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    with pytest.raises(ValueError, match="Duplicate candidate name"):
        decode_candidates(
            raw,
            [
                CandidateSpec("same", spec),
                CandidateSpec("same", other),
            ],
        )


def test_input_types_are_validated() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    with pytest.raises(TypeError, match="bytes or bytearray"):
        decode_candidates("text", {"one": spec})  # type: ignore

    with pytest.raises(ValueError, match="At least one candidate"):
        decode_candidates(raw, [])

    with pytest.raises(TypeError, match="mapping or an iterable"):
        decode_candidates(raw, 1)  # type: ignore


def test_tuple_pairs_are_accepted() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(raw, [("named", spec)])

    assert report.selected_name == "named"


def test_destination_is_reported_not_used_for_arbitration() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(
        raw,
        {
            "alpha": CandidateSpec("alpha", spec, destination="old"),
            "zeta": CandidateSpec("zeta", spec, destination="new"),
        },
    )

    assert report.selected is None
    assert report.ambiguous is True
    assert report.common_destination is None
    assert report.as_dict()["selected_destination"] is None
    assert {candidate.destination for candidate in report.candidates} == {
        "old",
        "new",
    }


def test_shared_destination_across_ambiguous_candidates() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(
        raw,
        {
            "alpha": CandidateSpec("alpha", spec, destination="same"),
            "zeta": CandidateSpec("zeta", spec, destination="same"),
        },
    )

    assert report.ambiguous is True
    assert report.common_destination == "same"


def test_explain_contains_filter_trail() -> None:
    llvar = make_spec()
    fixed = make_spec()
    fixed["2"]["len_type"] = 0
    fixed["2"]["max_len"] = 10
    raw = make_raw({"h": "HDR1", "t": "0200", "2": "1234567890"}, llvar)

    text = decode_candidates(raw, {"llvar": llvar, "fixed": fixed}).explain()

    assert "selected: llvar" in text
    assert "[fixed] trailing" in text
    assert "consumption: failed" in text
    assert "consumption: ok" in text


def test_regex_prefix_uses_match_not_fullmatch() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(
        raw,
        {
            "prefix": CandidateSpec(
                "prefix", spec, header_prefix=re.compile(r"HDR")
            )
        },
    )

    assert report.selected_name == "prefix"


def test_validator_exception_rejects_only_that_candidate() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    def boom(value: str) -> bool:
        raise RuntimeError("bad rule")

    report = decode_candidates(
        raw,
        {
            "fine": spec,
            "broken": CandidateSpec("broken", spec, validators={"3": boom}),
        },
    )

    assert report.selected_name == "fine"
    broken = next(c for c in report.candidates if c.name == "broken")
    assert broken.reasons == ("field 3 failed validation",)


def test_candidate_definition_mapping_form() -> None:
    spec = make_spec()
    raw = make_raw({"h": "HDR1", "t": "0200", "3": "123456"})

    report = decode_candidates(
        raw,
        [
            {
                "name": "defined",
                "spec": spec,
                "header": "HDR1",
                "allowed_fields": [3],
                "destination": "tbl",
            }
        ],
    )

    assert report.selected_name == "defined"
    assert report.selected is not None
    assert report.selected.destination == "tbl"
