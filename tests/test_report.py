"""
Tests for the sanitising collection-report export.
"""

from __future__ import annotations

import json

import pytest

from hpack import (
    EMPTY,
    EMPTY_VALUE,
    REDACTED,
    REDACTED_VALUE,
    Aggregate,
    CleanedField,
    CleanedReport,
    Decoder,
    DuplicateFieldName,
    Encoder,
    HeaderTuple,
    NeverIndexedHeaderTuple,
    ReportPolicy,
    SanitizationError,
    ValueState,
    clean_record,
    clean_records,
    sanitize_headers,
    sanitize_records,
)
from hpack.report import (
    CleanedRecord,
    ErrorSummary,
    FieldStatistics,
)

SECRET = "Bearer " + "super" + "-secret-token"  # noqa: S105


def _wire_headers(headers):
    """
    Encode headers, return the decoded HeaderTuples.

    This exercises the real never-indexed representation rather than relying
    on the caller to construct the tuple subclass by hand.
    """
    encoder = Encoder()
    decoder = Decoder()
    return decoder.decode(encoder.encode(headers))


class TestNeverIndexedDetection:
    def test_never_indexed_header_tuple_is_restricted(self):
        report = clean_record([NeverIndexedHeaderTuple("authorization", SECRET)])

        field = report.fields[0]
        assert field.restricted
        assert field.value is REDACTED
        assert field.is_redacted
        assert field.value_state == "redacted"
        assert field.name == "authorization"
        assert field.position == 1
        assert field.ordinal == 1

    def test_ordinary_header_tuple_is_not_restricted(self):
        report = clean_record([HeaderTuple("authorization", "public")])

        assert not report.fields[0].restricted
        assert report.fields[0].value == "public"

    def test_sensitive_three_tuple_is_restricted(self):
        report = clean_record([("authorization", SECRET, True)])

        assert report.fields[0].restricted
        assert report.fields[0].value is REDACTED

    def test_decoded_never_indexed_fields_remain_restricted(self):
        headers = _wire_headers([
            NeverIndexedHeaderTuple("authorization", SECRET),
            NeverIndexedHeaderTuple("cookie", "session=abcdef"),
        ])
        assert all(isinstance(header, NeverIndexedHeaderTuple) for header in headers)

        report = clean_record(headers)

        assert [field.name for field in report.fields] == ["authorization", "cookie"]
        assert all(field.restricted for field in report.fields)
        assert all(field.value is REDACTED for field in report.fields)

    def test_decoded_ordinary_fields_are_reported(self):
        headers = _wire_headers([
            HeaderTuple(":method", "GET"),
            HeaderTuple("x-trace", "abc"),
        ])

        report = clean_record(headers)

        assert [field.value for field in report.fields] == ["GET", "abc"]

    def test_same_name_may_be_ordinary_in_one_record_and_restricted_in_another(self):
        records = {
            "request-1": [("authorization", "Basic public")],
            "request-2": [NeverIndexedHeaderTuple("authorization", SECRET)],
        }

        report = clean_records(records)

        authorizations = [field for field in report.fields if field.name == "authorization"]
        assert len(authorizations) == 2
        assert authorizations[0].restricted is False
        assert authorizations[0].value == "Basic public"
        assert authorizations[1].restricted is True
        assert authorizations[1].value is REDACTED


class TestEmptyVersusRedacted:
    def test_ordinary_empty_value_uses_empty_marker(self):
        report = clean_record([("x-empty", "")])
        field = report.fields[0]

        assert field.value is EMPTY
        assert field.value is not REDACTED
        assert field.is_empty
        assert field.value_state == "empty"

    def test_restricted_empty_value_still_uses_redacted_marker(self):
        report = clean_record([NeverIndexedHeaderTuple("authorization", "")])
        field = report.fields[0]

        assert field.value is REDACTED
        assert field.value is not EMPTY
        assert field.is_redacted
        assert field.value_state == "redacted"

    def test_empty_and_redacted_serialise_differently(self):
        report = clean_record([
            ("x-empty", ""),
            NeverIndexedHeaderTuple("authorization", ""),
        ])

        payload = report.to_dict()
        empty_field, redacted_field = payload["records"][0]["fields"]

        assert empty_field["value_state"] == "empty"
        assert empty_field["value"] == {"kind": "empty"}
        assert redacted_field["value_state"] == "redacted"
        assert redacted_field["value"] == {"kind": "redacted"}
        assert empty_field["value"] != redacted_field["value"]

    def test_empty_aggregates_are_only_reported_when_allowed(self):
        report = clean_record(
            [NeverIndexedHeaderTuple("authorization", "")],
            policy=ReportPolicy(allowed_aggregates={Aggregate.EMPTY_COUNT}),
        )

        statistics = report.statistics[0]
        assert statistics.restricted == {"empty_count": 1}

    def test_both_markers_are_distinct_string_values(self):
        assert isinstance(EMPTY, ValueState)
        assert isinstance(REDACTED, ValueState)
        assert EMPTY != REDACTED
        assert str(EMPTY) == "empty"
        assert str(REDACTED) == "redacted"

    def test_marker_aliases(self):
        assert EMPTY_VALUE is EMPTY
        assert REDACTED_VALUE is REDACTED

    def test_field_state_aliases(self):
        empty = clean_record([("a", "")]).fields[0]
        redacted = clean_record([NeverIndexedHeaderTuple("a", SECRET)]).fields[0]

        assert empty.empty is True
        assert empty.redacted is False
        assert redacted.empty is False
        assert redacted.redacted is True

    def test_field_summary_contains_no_value(self):
        report = clean_record([
            ("authorization", "visible-value"),
            NeverIndexedHeaderTuple("authorization", SECRET),
        ])

        assert report.fields[0].summary_dict() == {
            "record_id": "record-1",
            "position": 1,
            "ordinal": 1,
            "restricted": False,
            "value_state": "value",
        }
        assert "value" not in report.fields[1].summary_dict()
        assert SECRET not in json.dumps(report.duplicates[0].summary_dict())


class TestDuplicateFieldNames:
    def test_empty_and_redacted_share_one_duplicate_list(self):
        headers = [
            ("cookie", ""),
            NeverIndexedHeaderTuple("cookie", "session=secret"),
        ]

        report = clean_record(headers)

        assert len(report.duplicates) == 1
        duplicate = report.duplicates[0]
        assert isinstance(duplicate, DuplicateFieldName)
        assert duplicate.name == "cookie"
        assert [occurrence.value for occurrence in duplicate.occurrences] == [
            EMPTY,
            REDACTED,
        ]
        assert duplicate.empty_occurrences == (duplicate.occurrences[0],)
        assert duplicate.restricted_occurrences == (duplicate.occurrences[1],)

    def test_duplicate_occurrences_encode_both_states(self):
        report = clean_record([
            ("cookie", ""),
            NeverIndexedHeaderTuple("cookie", "session=secret"),
        ])

        occurrences = report.to_dict()["duplicates"][0]["occurrences"]

        assert occurrences[0]["value"] == {"kind": "empty"}
        assert occurrences[0]["value_state"] == "empty"
        assert occurrences[1]["value"] == {"kind": "redacted"}
        assert occurrences[1]["value_state"] == "redacted"

    def test_duplicate_positions_and_ordinals_are_preserved(self):
        report = clean_record([
            ("cookie", ""),
            ("x-other", "value"),
            NeverIndexedHeaderTuple("cookie", "session=secret"),
        ])

        occurrences = report.duplicates[0].occurrences
        assert [occurrence.position for occurrence in occurrences] == [1, 3]
        assert [occurrence.ordinal for occurrence in occurrences] == [1, 2]

    def test_unique_names_are_not_duplicates(self):
        report = clean_record([("a", "1"), ("b", "2")])

        assert report.duplicates == ()
        assert all(not statistics.duplicate for statistics in report.statistics)

    def test_duplicate_detection_spans_records(self):
        report = clean_records({
            "one": [("x-id", "a")],
            "two": [("x-id", "b")],
        })

        duplicate = report.duplicates[0]
        assert [occurrence.record_id for occurrence in duplicate.occurrences] == [
            "one",
            "two",
        ]
        assert [occurrence.ordinal for occurrence in duplicate.occurrences] == [1, 2]


class TestRestrictedAggregates:
    def test_no_aggregates_by_default(self):
        report = clean_record([NeverIndexedHeaderTuple("authorization", SECRET)])

        assert report.statistics[0].restricted == {}
        assert report.to_dict()["statistics"][0]["restricted"] == {}

    def test_only_selected_aggregates_are_present(self):
        report = clean_record(
            [
                NeverIndexedHeaderTuple("authorization", SECRET),
                NeverIndexedHeaderTuple("authorization", ""),
            ],
            policy=ReportPolicy(allowed_aggregates={Aggregate.COUNT}),
        )

        assert report.statistics[0].restricted == {"count": 2}

    def test_all_allowed_aggregate_values(self):
        policy = ReportPolicy(
            allowed_aggregates={
                Aggregate.COUNT,
                Aggregate.RECORD_COUNT,
                Aggregate.EMPTY_COUNT,
                Aggregate.VALUE_LENGTH_TOTAL,
                Aggregate.VALUE_LENGTH_MIN,
                Aggregate.VALUE_LENGTH_MAX,
            },
        )
        report = clean_records(
            {
                "one": [
                    NeverIndexedHeaderTuple("authorization", "abc"),
                    NeverIndexedHeaderTuple("authorization", ""),
                ],
                "two": [NeverIndexedHeaderTuple("authorization", "abcde")],
            },
            policy=policy,
        )

        assert report.statistics[0].restricted == {
            "count": 3,
            "record_count": 2,
            "empty_count": 1,
            "value_length_total": 8,
            "value_length_min": 0,
            "value_length_max": 5,
        }

    def test_aggregates_accept_strings(self):
        report = clean_record(
            [NeverIndexedHeaderTuple("authorization", SECRET)],
            policy=ReportPolicy(allowed_aggregates={"count"}),
        )

        assert report.statistics[0].restricted == {"count": 1}

    def test_aggregates_accept_a_single_member(self):
        report = clean_record(
            [NeverIndexedHeaderTuple("authorization", SECRET)],
            policy=ReportPolicy(allowed_aggregates=Aggregate.COUNT),
        )

        assert report.statistics[0].restricted == {"count": 1}

    def test_aggregates_reject_unknown_names(self):
        with pytest.raises(TypeError):
            ReportPolicy(allowed_aggregates={"value"})

    def test_aggregates_reject_non_iterables(self):
        with pytest.raises(TypeError):
            ReportPolicy(allowed_aggregates=1)  # type: ignore[arg-type]

    def test_statistics_copy_does_not_mutate_report(self):
        report = clean_record(
            [NeverIndexedHeaderTuple("authorization", SECRET)],
            policy=ReportPolicy(allowed_aggregates={"count"}),
        )

        copied = report.statistics[0].to_dict()
        copied["restricted"]["count"] = 99
        assert report.statistics[0].restricted == {"count": 1}

    def test_aggregate_labels_never_include_raw_values(self):
        report = clean_record(
            [NeverIndexedHeaderTuple("authorization", SECRET)],
            policy=ReportPolicy(allowed_aggregates=set(Aggregate)),
        )

        restricted = report.statistics[0].restricted
        assert all(type(value) is int for value in restricted.values())
        assert SECRET not in json.dumps(report.statistics_record())


class TestSerializationSafety:
    def test_full_json_never_contains_restricted_value(self):
        report = clean_records(
            {
                "request-1": [
                    HeaderTuple(":method", "GET"),
                    NeverIndexedHeaderTuple("authorization", SECRET),
                ],
            },
            policy=ReportPolicy(allowed_aggregates=set(Aggregate)),
        )

        serialized = report.to_json()
        assert SECRET not in serialized
        assert "redacted" in serialized
        assert "GET" in serialized

    def test_statistics_json_never_contains_any_header_value(self):
        report = clean_records(
            {
                "request-1": [
                    ("x-visible", "visible-value"),
                    NeverIndexedHeaderTuple("authorization", SECRET),
                ],
            },
            policy=ReportPolicy(allowed_aggregates=set(Aggregate)),
        )

        serialized = report.statistics_json()
        assert SECRET not in serialized
        assert "visible-value" not in serialized
        assert "x-visible" in serialized

    def test_statistics_record_has_no_detail_records(self):
        report = clean_record([HeaderTuple("x-visible", "visible-value")])

        record = report.statistics_record()

        assert "records" not in record
        assert "visible-value" not in json.dumps(record)
        assert record["field_names"] == ["x-visible"]
        assert record["duplicates"] == []

    def test_statistics_record_keeps_value_free_duplicate_positions(self):
        report = clean_record([
            ("cookie", ""),
            ("x-other", "visible-value"),
            NeverIndexedHeaderTuple("cookie", SECRET),
        ])

        record = report.statistics_record()
        duplicate = record["duplicates"][0]

        assert duplicate["name"] == "cookie"
        assert [occurrence["value_state"] for occurrence in duplicate["occurrences"]] == ["empty", "redacted"]
        assert [occurrence["position"] for occurrence in duplicate["occurrences"]] == [
            1,
            3,
        ]
        assert "value" not in duplicate["occurrences"][1]
        assert SECRET not in json.dumps(record)
        assert "visible-value" not in json.dumps(record)

    def test_repr_never_contains_restricted_value(self):
        report = clean_record([
            NeverIndexedHeaderTuple("authorization", SECRET),
            ("x-visible", "visible-value"),
        ])

        for field in report.fields:
            assert SECRET not in repr(field)
        assert SECRET not in repr(report)
        assert SECRET not in repr(report.duplicates)
        assert SECRET not in repr(report.statistics)

    def test_repr_marks_empty_and_redacted(self):
        report = clean_record([
            ("x-empty", ""),
            NeverIndexedHeaderTuple("authorization", SECRET),
        ])

        empty_field, redacted_field = report.fields
        assert "<empty>" in repr(empty_field)
        assert "<redacted>" in repr(redacted_field)
        assert SECRET not in repr(redacted_field)

    def test_cleaned_field_cannot_smuggle_restricted_value(self):
        with pytest.raises(ValueError):
            CleanedField(
                name="authorization",
                record_id="request-1",
                position=1,
                ordinal=1,
                restricted=True,
                value=SECRET,
            )

    def test_ordinary_field_cannot_claim_redacted(self):
        with pytest.raises(ValueError):
            CleanedField(
                name="authorization",
                record_id="request-1",
                position=1,
                ordinal=1,
                restricted=False,
                value=REDACTED,
            )


class TestErrors:
    def test_invalid_header_value_is_not_echoed(self):
        report = clean_record([
            NeverIndexedHeaderTuple("authorization", SECRET),
            ("bad", object()),
        ])

        assert len(report.errors) == 1
        error = report.errors[0]
        assert error.code == "invalid_header_value"
        assert error.field_name == "bad"
        assert error.position == 2
        assert "object" not in error.message

        serialized = report.statistics_json()
        assert SECRET not in serialized
        assert "invalid_header_value" in serialized

    def test_error_summary_for_malformed_record(self):
        report = clean_records({"request-1": "not-a-header-list"})

        assert report.records == ()
        assert report.errors[0].code == "invalid_record_headers"
        assert report.errors[0].record_id == "request-1"

    def test_collect_errors_false_raises_safe_error(self):
        with pytest.raises(SanitizationError) as raised:
            clean_record(
                [("bad", object())],
                policy=ReportPolicy(collect_errors=False),
            )

        error = raised.value
        assert error.error.code == "invalid_header_value"
        assert error.summary() == error.to_dict()
        assert "object" not in str(error)

    def test_raising_mode_does_not_echo_bad_value(self):
        with pytest.raises(SanitizationError) as raised:
            clean_record(
                [("authorization", object())],
                policy=ReportPolicy(collect_errors=False),
            )

        assert SECRET not in str(raised.value)
        assert "object" not in str(raised.value)

    def test_invalid_sensitive_flag_is_an_error(self):
        report = clean_record([("authorization", SECRET, "yes")])

        assert report.records[0].fields == ()
        assert report.errors[0].code == "invalid_sensitive_flag"

    def test_invalid_header_shape_is_an_error(self):
        report = clean_record([("a", "b", "c", "d")])

        assert report.errors[0].code == "invalid_header"

    def test_non_tuple_header_is_an_error(self):
        report = clean_record(["authorization: secret"])

        assert report.errors[0].code == "invalid_header"

    def test_invalid_header_name_is_an_error(self):
        report = clean_record([(object(), "value")])

        assert report.errors[0].code == "invalid_header_name"

    def test_non_utf8_name_is_an_error(self):
        report = clean_record([(b"\xff", "value")])

        assert report.errors[0].code == "invalid_header_name"

    def test_non_utf8_value_is_an_error_without_value(self):
        report = clean_record([(b"authorization", b"\xff\xfe")])

        assert report.errors[0].code == "invalid_header_value"

    def test_non_utf8_record_id_is_an_error(self):
        report = clean_records({"request-1": [("a", "b")], b"\xff": [("c", "d")]})

        assert report.errors[0].code == "invalid_record_id"
        assert report.errors[0].record_id is None
        assert [record.record_id for record in report.records] == [
            "request-1",
            "record-2",
        ]

    def test_invalid_records_container_is_an_error(self):
        report = clean_records("not-records")

        assert report.errors[0].code == "invalid_records"
        assert report.records == ()

    def test_header_iteration_error_is_contained(self):
        def broken():
            yield ("a", "b")
            raise RuntimeError("boom")

        report = clean_record(broken())

        assert report.errors[0].code == "headers_iteration_error"
        assert "boom" not in report.errors[0].message
        assert [field.value for field in report.fields] == ["b"]

    def test_record_iteration_error_is_contained(self):
        def broken():
            yield ("request-1", [("a", "b")])
            raise RuntimeError("boom")

        report = clean_records(broken())

        assert report.errors[0].code == "records_iteration_error"
        assert "boom" not in report.errors[0].message
        assert [field.value for field in report.fields] == ["b"]

    def test_mapping_iteration_error_is_contained(self):
        class BrokenMapping(dict):
            def items(self):
                yield "request-1", [("a", "b")]
                raise RuntimeError("boom")

        report = clean_records(BrokenMapping())

        assert report.errors[0].code == "records_iteration_error"
        assert "boom" not in report.errors[0].message
        assert [field.value for field in report.fields] == ["b"]

    def test_non_string_record_headers_is_an_error(self):
        report = clean_records({"request-1": 1})

        assert report.errors[0].code == "invalid_record_headers"

    def test_non_text_mapping_key_is_an_error(self):
        report = clean_records({1: [("a", "b")]})

        assert report.errors[0].code == "invalid_record_id"
        assert report.records[0].record_id == "record-1"

    def test_raising_mode_stops_mapping_bad_key(self):
        with pytest.raises(SanitizationError) as raised:
            clean_records({1: [("a", "b")]}, policy=ReportPolicy(collect_errors=False))

        assert raised.value.error.code == "invalid_record_id"

    def test_raising_mode_stops_sequence_bad_key(self):
        with pytest.raises(SanitizationError) as raised:
            clean_records(
                [(b"\xff", [("a", "b")])],
                policy=ReportPolicy(collect_errors=False),
            )

        assert raised.value.error.code == "invalid_record_id"

    def test_sequence_pair_bad_key_is_reported(self):
        report = clean_records([(b"\xff", [("a", "b")])])

        assert report.errors[0].code == "invalid_record_id"
        assert report.records[0].record_id == "record-1"


class TestReportShape:
    def test_records_keep_input_order_and_positions(self):
        report = clean_records({
            "one": [("a", "1"), ("b", "2")],
            "two": [("c", "3")],
        })

        assert [record.record_id for record in report.records] == ["one", "two"]
        assert [field.position for field in report.fields] == [1, 2, 1]

    def test_bare_header_lists_get_generated_ids(self):
        report = clean_records([[("a", "1")], [("b", "2")]])

        assert [record.record_id for record in report.records] == [
            "record-1",
            "record-2",
        ]

    def test_record_pairs_are_supported(self):
        report = clean_records([("request-1", [("a", "1")])])

        assert report.records[0].record_id == "request-1"

    def test_statistics_count_ordinary_and_empty_values(self):
        report = clean_record([("x", ""), ("x", "value"), ("x", "")])

        statistics = report.statistics[0]
        assert isinstance(statistics, FieldStatistics)
        assert statistics.occurrences == 3
        assert statistics.ordinary_occurrences == 3
        assert statistics.empty_occurrences == 2
        assert statistics.duplicate

    def test_statistics_by_name(self):
        report = clean_record([("a", "1"), ("b", "2")])

        by_name = report.statistics_by_name()
        assert set(by_name) == {"a", "b"}
        assert by_name["a"].occurrences == 1

    def test_cleaned_report_types(self):
        report = clean_record([("a", "1")])

        assert isinstance(report, CleanedReport)
        assert isinstance(report.records[0], CleanedRecord)
        assert isinstance(report.errors, tuple)

    def test_error_summary_dataclass(self):
        error = ErrorSummary("code", "message", "record", "field", 1)

        assert error.to_dict() == {
            "code": "code",
            "message": "message",
            "record_id": "record",
            "field_name": "field",
            "position": 1,
        }

    def test_policy_evaluated_once(self):
        policy = ReportPolicy(allowed_aggregates={"count"})

        assert policy.allowed_aggregates == {Aggregate.COUNT}
        assert policy.restricted_aggregates == policy.allowed_aggregates
        with pytest.raises(TypeError):
            ReportPolicy(collect_errors="yes")  # type: ignore[arg-type]

    def test_cleaned_report_fields_and_restricted_fields(self):
        report = clean_record([
            ("a", "1"),
            NeverIndexedHeaderTuple("b", SECRET),
        ])

        assert [field.name for field in report.fields] == ["a", "b"]
        assert [field.name for field in report.restricted_fields] == ["b"]
        assert report.duplicate_field_names is report.duplicates
        assert report.field_statistics is report.statistics

    def test_aliases(self):
        assert isinstance(sanitize_headers([("a", "1")]), CleanedReport)
        assert isinstance(sanitize_records({"one": [("a", "1")]}), CleanedReport)
        assert clean_record([("a", "1")]).to_dict() == sanitize_headers([("a", "1")]).to_dict()

    def test_byte_values_are_decoded(self):
        report = clean_record([(b"authorization", SECRET.encode())])

        assert report.fields[0].name == "authorization"
        assert report.fields[0].value == SECRET

    def test_byte_record_id_is_decoded(self):
        report = clean_records({b"request-1": [("a", "1")]})

        assert report.records[0].record_id == "request-1"

    def test_header_tuple_with_wrong_arity_is_an_error(self):
        report = clean_record([HeaderTuple("a")])

        assert report.errors[0].code == "invalid_header"

    def test_report_to_json_round_trips(self):
        report = clean_record([HeaderTuple("a", "1")])

        assert json.loads(report.to_json()) == report.to_dict()
        assert json.loads(report.to_json(indent=2)) == report.to_dict()
        assert json.loads(report.statistics_json(indent=2)) == report.statistics_record()

    def test_report_policy_defaults_are_isolated(self):
        first = ReportPolicy()
        second = ReportPolicy(allowed_aggregates={"count"})

        assert first.allowed_aggregates == frozenset()
        assert second.allowed_aggregates == frozenset({Aggregate.COUNT})

    def test_error_report_is_json_safe(self):
        report = clean_record([("bad", object())])

        assert json.loads(report.statistics_json())["errors"] == [
            {
                "code": "invalid_header_value",
                "message": "header field value must be UTF-8 bytes or str",
                "record_id": "record-1",
                "field_name": "bad",
                "position": 1,
            },
        ]
