import copy
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List

import pytest

import iso8583
import iso8583.specs
from iso8583.reconciliation import (
    IngestStatus,
    MatchKind,
    OriginalDataElements,
    ReconciliationError,
    ReconciliationLedger,
    RecordKind,
    ReversalStatus,
    load_messages,
    parse_original_data_elements,
    reconcile,
)

DAY = date(2026, 11, 14)


def message(**fields: Any) -> Dict[str, str]:
    base = {
        "t": "0200",
        "11": "123456",
        "32": "ACQ0001",
        "41": "TERM0001",
        "13": "1114",
        "12": "000100",
        "4": "000000010000",
    }
    base.update(fields)
    return base


def reversal(**fields: Any) -> Dict[str, str]:
    base = message(**fields)
    base["t"] = "0400"
    return base


def de90(
    stan: str = "123456",
    timestamp: str = "1113235900",
    acquirer: str = "ACQ0001",
    mti: str = "0200",
) -> str:
    return (
        mti
        + stan
        + timestamp
        + acquirer.ljust(11)
        + " " * 11
    )


def ledger(time_window: int = 3600) -> ReconciliationLedger:
    return ReconciliationLedger(time_window=time_window, date_hint=DAY)


def test_original_is_identified_by_its_local_date() -> None:
    record = OriginalDataElements.parse(de90())

    assert record is not None
    assert record.message_type == "0200"
    assert record.stan == "123456"
    assert record.transmission_datetime == "1113235900"
    assert record.acquiring_institution_id == "ACQ0001"
    assert record.is_complete
    assert record.date(DAY) == date(2026, 11, 13)
    assert record.datetime(DAY).isoformat() == "2026-11-13T23:59:00"


def test_original_data_elements_parse_separated_value() -> None:
    record = parse_original_data_elements("0200:123456:1113235900:ACQ0001:ACQ0001")

    assert record is not None
    assert record.is_complete
    assert record.stan == "123456"
    assert record.acquiring_institution_id == "ACQ0001"


def test_original_data_elements_reject_overlong_value() -> None:
    with pytest.raises(ReconciliationError, match="must not exceed 42"):
        OriginalDataElements.parse("0" * 43)


def test_original_data_elements_empty_value_is_none() -> None:
    assert OriginalDataElements.parse("") is None
    assert OriginalDataElements.parse(None) is None


def test_same_trace_number_on_two_days_is_not_a_duplicate() -> None:
    book = ledger()
    yesterday = book.ingest(message(**{"13": "1113", "12": "235900"}))
    today = book.ingest(message())

    assert yesterday.status is IngestStatus.ORIGINAL
    assert today.status is IngestStatus.ORIGINAL
    assert len(book.originals) == 2
    assert book.balance == Decimal("20000")


def test_identical_original_resend_is_ignored() -> None:
    book = ledger()
    first = book.ingest(message())
    again = book.ingest(message())

    assert first.status is IngestStatus.ORIGINAL
    assert again.status is IngestStatus.ORIGINAL_DUPLICATE
    assert len(book.originals) == 1
    assert book.balance == Decimal("10000")


def test_reversal_without_reference_does_not_use_yesterdays_trace() -> None:
    book = ledger()
    book.ingest(message(**{"13": "1113", "12": "235900"}))
    book.ingest(message(**{"13": "1114", "12": "000100"}))

    result = book.ingest(reversal(**{"13": "1114", "12": "000500"}))
    candidates = {candidate.local_date for candidate in result.candidates}

    assert result.status is IngestStatus.REVERSAL_AMBIGUOUS
    assert candidates == {date(2026, 11, 13), date(2026, 11, 14)}


def test_complete_reference_selects_only_yesterdays_original() -> None:
    book = ledger()
    yesterday = book.ingest(message(**{"13": "1113", "12": "235900"}))
    book.ingest(message(**{"13": "1114", "12": "000100"}))

    book.ingest(reversal(**{"13": "1114", "12": "000500"}))
    result = book.ingest(
        reversal(**{"13": "1114", "12": "000500", "90": de90()})
    )

    assert result.status is IngestStatus.REVERSAL_RESOLVED
    assert result.match is not None
    assert result.match.original_id == yesterday.record.id
    assert result.match.kind is MatchKind.EXACT

    yesterday_record = book.originals[0]
    today_record = book.originals[1]
    assert yesterday_record.matched_reversal_id is not None
    assert today_record.matched_reversal_id is None


def test_reversal_may_arrive_before_original() -> None:
    book = ledger()
    pending = book.ingest(reversal(**{"13": "1114", "12": "000100", "90": de90()}))

    assert pending.status is IngestStatus.REVERSAL_PENDING
    assert book.balance == Decimal("0")

    added = book.ingest(message(**{"13": "1113", "12": "235900"}))

    assert added.status is IngestStatus.ORIGINAL
    assert len(added.resolved) == 1
    assert book.balance == Decimal("0")


def test_same_reversal_resend_is_applied_once() -> None:
    book = ledger()
    book.ingest(message(**{"13": "1113", "12": "235900"}))
    first = book.ingest(reversal(**{"13": "1114", "12": "000100", "90": de90()}))
    again = book.ingest(reversal(**{"13": "1114", "12": "000100", "90": de90()}))

    assert first.status is IngestStatus.REVERSAL_MATCHED
    assert again.status is IngestStatus.REVERSAL_DUPLICATE
    assert book.summary()["reversed_total"] == "10000"
    assert book.balance == Decimal("0")


def test_late_reference_removes_only_the_matching_pending_case() -> None:
    book = ledger()
    first = book.ingest(message(**{"11": "111111", "12": "120000", "4": "000000005000"}))
    second = book.ingest(message(**{"11": "222222", "12": "120500", "4": "000000005000"}))
    other = book.ingest(message(**{"11": "333333", "12": "121500", "4": "000000007000"}))

    ambiguous = book.ingest(reversal(**{"11": "444444", "12": "121000", "4": "000000005000"}))
    assert ambiguous.status is IngestStatus.REVERSAL_AMBIGUOUS
    assert {candidate.id for candidate in ambiguous.candidates} == {
        first.record.id,
        second.record.id,
    }

    unresolved = book.ingest(
        reversal(**{"11": "555555", "12": "122000", "4": "000000009000"})
    )
    assert unresolved.status is IngestStatus.REVERSAL_PENDING

    resolved = book.ingest(
        reversal(
            **{
                "11": "444444",
                "12": "121000",
                "4": "000000005000",
                "90": de90(stan="222222", timestamp="1114120500"),
            }
        )
    )

    assert resolved.status is IngestStatus.REVERSAL_RESOLVED
    assert resolved.match is not None
    assert resolved.match.original_id == second.record.id
    assert book.summary()["reversed_total"] == "5000"
    assert other.record.matched_reversal_id is None
    assert unresolved.record.status is ReversalStatus.PENDING


def test_ambiguous_resend_without_reference_stays_ambiguous() -> None:
    book = ledger()
    first = book.ingest(message(**{"11": "111111", "12": "120000"}))
    second = book.ingest(message(**{"11": "222222", "12": "120500"}))

    pending = reversal(**{"11": "333333", "12": "121000"})
    initial = book.ingest(pending)
    assert initial.status is IngestStatus.REVERSAL_AMBIGUOUS

    again = book.ingest(pending)

    assert again.status is IngestStatus.REVERSAL_AMBIGUOUS
    assert {candidate.id for candidate in again.candidates} == {
        first.record.id,
        second.record.id,
    }
    assert book.summary()["reversed_total"] == "0"


def test_second_reversal_for_same_original_does_not_debit_again() -> None:
    book = ledger()
    book.ingest(message(**{"13": "1113", "12": "235900"}))
    first = book.ingest(reversal(**{"11": "000001", "90": de90()}))
    second = book.ingest(reversal(**{"11": "000002", "90": de90()}))

    assert first.status is IngestStatus.REVERSAL_MATCHED
    assert second.status is IngestStatus.REVERSAL_ALREADY_APPLIED
    assert book.summary()["reversed_total"] == "10000"
    assert book.balance == Decimal("0")


def test_reversal_without_terminal_uses_matching_reference_institution() -> None:
    book = ledger()
    added = book.ingest(message(**{"13": "1113", "12": "235900"}))
    result = book.ingest(
        reversal(
            **{
                "13": "1114",
                "12": "000100",
                "41": "",
                "90": de90(),
            }
        )
    )

    assert result.status is IngestStatus.REVERSAL_MATCHED
    assert result.match is not None
    assert result.match.original_id == added.record.id


def test_partial_reference_does_not_create_exact_match() -> None:
    book = ledger()
    book.ingest(message(**{"13": "1113", "12": "235900"}))
    book.ingest(message())

    partial = book.ingest(reversal(**{"13": "1114", "12": "000100", "90": "0200123456"}))
    assert partial.status is IngestStatus.REVERSAL_PENDING
    assert book.summary()["reversed_total"] == "0"

    complete = book.ingest(reversal(**{"13": "1114", "12": "000100", "90": de90()}))
    assert complete.status is IngestStatus.REVERSAL_RESOLVED
    assert complete.match is not None
    assert complete.match.kind is MatchKind.EXACT


def test_amount_time_window_keeps_two_candidates_pending() -> None:
    book = ledger(time_window=900)
    first = book.ingest(message(**{"11": "111111", "12": "120000"}))
    second = book.ingest(message(**{"11": "222222", "12": "120500"}))
    book.ingest(message(**{"11": "333333", "12": "160000"}))

    result = book.ingest(reversal(**{"11": "444444", "12": "121000"}))

    assert result.status is IngestStatus.REVERSAL_AMBIGUOUS
    assert {candidate.id for candidate in result.candidates} == {
        first.record.id,
        second.record.id,
    }
    assert book.balance == Decimal("30000")


def test_amount_time_window_accepts_a_single_candidate() -> None:
    book = ledger(time_window=900)
    book.ingest(message(**{"11": "111111", "12": "120000"}))
    book.ingest(message(**{"11": "222222", "12": "160000"}))

    result = book.ingest(reversal(**{"11": "333333", "12": "120500"}))

    assert result.status is IngestStatus.REVERSAL_MATCHED
    assert result.match is not None
    assert result.match.kind is MatchKind.AMOUNT_TIME
    assert book.balance == Decimal("10000")


def test_forced_kind_overrides_message_type() -> None:
    book = ledger()
    result = book.ingest(message(), kind="reversal")

    assert result.status is IngestStatus.REVERSAL_PENDING
    assert result.record.kind is RecordKind.REVERSAL


def test_unknown_forced_kind_is_rejected() -> None:
    book = ledger()
    with pytest.raises(ValueError, match="kind must be"):
        book.ingest(message(), kind="chargeback")


def test_ingest_rejects_non_mapping() -> None:
    book = ledger()
    with pytest.raises(TypeError, match="must be a mapping"):
        book.ingest([message()])  # type: ignore[arg-type]


def test_invalid_amount_is_reported() -> None:
    book = ledger()
    with pytest.raises(ReconciliationError, match="invalid transaction amount"):
        book.ingest(message(**{"4": "not-a-number"}))


def test_negative_time_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="time_window"):
        ReconciliationLedger(time_window=-1)


def test_amount_scale_can_convert_minor_units() -> None:
    book = ReconciliationLedger(date_hint=DAY, amount_scale=2)
    result = book.ingest(message(**{"4": "000000010000"}))

    assert result.record.amount == Decimal("100.00")


def test_report_and_export_are_json_friendly() -> None:
    book = ledger()
    book.ingest(message(**{"13": "1113", "12": "235900"}))
    book.ingest(reversal(**{"90": de90()}))

    report = book.report()
    summary = report.summary()
    export = book.export()

    assert summary["original_count"] == 1
    assert summary["matched_count"] == 1
    assert summary["balance"] == "0"
    assert export["matches"][0]["kind"] == "exact"
    assert isinstance(export["records"][0]["amount"], str)


def test_reconcile_helper_ingests_in_order() -> None:
    book = reconcile(
        [message(**{"13": "1113", "12": "235900"}), reversal(**{"90": de90()})],
        date_hint=DAY,
    )

    assert book.summary()["matched_count"] == 1
    assert book.balance == Decimal("0")


def test_ingest_raw_decodes_iso8583_message() -> None:
    spec = copy.deepcopy(iso8583.specs.default_ascii)
    decoded = message(**{"2": "4111111111111111"})
    raw, _ = iso8583.encode(decoded, spec)

    book = ledger()
    result = book.ingest_raw(bytes(raw), spec)

    assert result.status is IngestStatus.ORIGINAL
    assert result.record.raw["2"] == "4111111111111111"


def test_load_messages_json_and_jsonl(tmp_path: Any) -> None:
    path = tmp_path / "sample.json"
    path.write_text(
        '{"messages": [{"t": "0200", "11": "123456", "4": 100}]}',
        encoding="utf-8",
    )
    assert load_messages(path) == [{"t": "0200", "11": "123456", "4": "100"}]

    ndjson = tmp_path / "sample.jsonl"
    ndjson.write_text('{"t": "0200", "90": null}\n', encoding="utf-8")
    assert load_messages(ndjson) == [{"t": "0200", "90": ""}]


def test_load_messages_csv(tmp_path: Any) -> None:
    path = tmp_path / "sample.csv"
    path.write_text("t,11,4\n0200,123456,100\n", encoding="utf-8")

    assert load_messages(path) == [{"t": "0200", "11": "123456", "4": "100"}]


def test_load_messages_unsupported_suffix(tmp_path: Any) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ReconciliationError, match="unsupported sample format"):
        load_messages(path)


def test_sample_scenario_is_stable_under_resends() -> None:
    """The full sample flow: prior-day reuse, ambiguity, fill-in, resend."""

    previous = message(**{"13": "1113", "12": "235900"})
    current = message(**{"13": "1114", "12": "000100"})
    pending = reversal(**{"11": "000001", "13": "1114", "12": "000500"})
    completed = reversal(
        **{"11": "000001", "13": "1114", "12": "000500", "90": de90()}
    )

    book = ledger()
    results: List[Any] = [
        book.ingest(previous),
        book.ingest(current),
        book.ingest(pending),
        book.ingest(completed),
        book.ingest(completed),
    ]

    assert [result.status for result in results] == [
        IngestStatus.ORIGINAL,
        IngestStatus.ORIGINAL,
        IngestStatus.REVERSAL_AMBIGUOUS,
        IngestStatus.REVERSAL_RESOLVED,
        IngestStatus.REVERSAL_DUPLICATE,
    ]
    assert book.summary()["reversed_total"] == "10000"
    assert book.summary()["balance"] == "10000"
    assert book.originals[0].matched_reversal_id is not None
    assert book.originals[1].matched_reversal_id is None


def test_de7_only_message_uses_transmission_date() -> None:
    book = ledger()
    result = book.ingest(
        {
            "t": "0200",
            "11": "123456",
            "32": "ACQ0001",
            "41": "TERM0001",
            "7": "1113235900",
            "4": "000000010000",
        }
    )

    assert result.record.local_date == date(2026, 11, 13)
    assert result.record.local_datetime is not None
    assert result.record.local_datetime.strftime("%H%M%S") == "235900"


def test_de7_can_resolve_to_the_previous_month() -> None:
    book = ReconciliationLedger(date_hint=date(2026, 1, 1))
    result = book.ingest(
        {
            "t": "0200",
            "11": "123456",
            "32": "ACQ0001",
            "41": "TERM0001",
            "7": "1231235900",
            "4": "000000010000",
        }
    )

    assert result.record.local_date == date(2025, 12, 31)


def test_winning_time_window_can_cross_midnight() -> None:
    book = ledger()
    book.ingest(message(**{"11": "111111", "13": "1113", "12": "235900"}))
    book.ingest(message(**{"11": "222222", "13": "1115", "12": "120000"}))

    result = book.ingest(reversal(**{"11": "333333", "13": "1114", "12": "000100"}))

    assert result.status is IngestStatus.REVERSAL_MATCHED
    assert result.match is not None
    assert result.match.kind is MatchKind.AMOUNT_TIME


def test_reference_year_uses_message_reference_not_derived_local_date() -> None:
    book = ReconciliationLedger(date_hint=date(2026, 1, 1))
    book.ingest(
        {
            "t": "0200",
            "11": "123456",
            "32": "ACQ0001",
            "41": "TERM0001",
            "7": "1231235900",
            "4": "000000010000",
        }
    )

    result = book.ingest(
        {
            "t": "0400",
            "11": "000001",
            "32": "ACQ0001",
            "41": "TERM0001",
            "7": "0101000500",
            "4": "000000010000",
            "90": de90(timestamp="1231235900"),
        }
    )

    assert result.status is IngestStatus.REVERSAL_MATCHED
    assert result.match is not None
    assert result.match.kind is MatchKind.EXACT


def test_stale_terminal_does_not_block_complete_reference() -> None:
    book = ledger()
    added = book.ingest(message(**{"13": "1113", "12": "235900"}))
    result = book.ingest(
        reversal(
            **{
                "13": "1114",
                "12": "000500",
                "41": "STALE999",
                "90": de90(),
            }
        )
    )

    assert result.status is IngestStatus.REVERSAL_MATCHED
    assert result.match is not None
    assert result.match.original_id == added.record.id


def test_duplicate_trace_and_date_without_terminal_stays_ambiguous() -> None:
    book = ledger()
    first = book.ingest(message(**{"13": "1113", "12": "235900"}))
    second = book.ingest(
        message(**{"13": "1113", "12": "120000", "41": "TERM0002"})
    )

    result = book.ingest(
        reversal(**{"13": "1114", "12": "000500", "41": "", "90": de90()})
    )

    assert result.status is IngestStatus.REVERSAL_AMBIGUOUS
    assert {candidate.id for candidate in result.candidates} == {
        first.record.id,
        second.record.id,
    }
