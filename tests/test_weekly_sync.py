from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import pylast.weekly_sync as weekly_sync

FIXTURES = Path(__file__).parent / "fixtures" / "weekly_chart_sync"


def fixture(name: str) -> Path:
    return FIXTURES / name


def test_adjacent_windows_share_a_boundary_but_not_a_play() -> None:
    # Arrange
    first = weekly_sync.Window(1674345600, 1674604800)
    second = weekly_sync.Window(1674604800, 1674950400)
    ledger = weekly_sync.WeeklyChartSync([first, second])

    # Act
    owners = ledger.partition_plays([1674345600, 1674604799, 1674604800, 1674950399])

    # Assert
    assert owners == {
        first.key: [1674345600, 1674604799],
        second.key: [1674604800, 1674950399],
    }


def test_boundary_play_is_owned_by_the_later_window() -> None:
    # Arrange
    ledger = weekly_sync.WeeklyChartSync(
        [weekly_sync.Window(100, 200), weekly_sync.Window(200, 300)]
    )

    # Act
    owner = ledger.claim_play(200)

    # Assert
    assert owner == weekly_sync.Window(200, 300)
    assert ledger.play_owner(200) == weekly_sync.Window(200, 300)
    with pytest.raises(ValueError, match="belongs to"):
        ledger.claim_play(200, weekly_sync.Window(100, 200))


def test_chart_windows_come_from_the_server_and_may_be_short() -> None:
    # Act
    windows = weekly_sync.parse_chart_list_xml(fixture("chart_list.xml").read_text())

    # Assert
    assert windows[3] == weekly_sync.Window(1674345600, 1674604800)
    assert windows[4] == weekly_sync.Window(1674604800, 1674950400)
    assert all(window.end - window.start < 7 * 86400 for window in windows[3:5])
    ledger = weekly_sync.WeeklyChartSync(windows)
    ledger.assert_disjoint()


def test_overlapping_windows_are_rejected() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="overlap"):
        weekly_sync.WeeklyChartSync(
            [weekly_sync.Window(100, 200), weekly_sync.Window(150, 250)]
        )


def test_chart_list_error_is_not_an_empty_published_list() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="not an ok"):
        weekly_sync.parse_chart_list_xml(fixture("error_body.xml").read_text())
    with pytest.raises(ValueError, match="weeklychartlist"):
        weekly_sync.parse_chart_list_xml('<?xml version="1.0"?><lfm status="ok"></lfm>')


def test_incremental_replay_discovers_a_late_window(tmp_path: Path) -> None:
    # Arrange
    ledger_path = tmp_path / "ledger.json"
    first = weekly_sync.replay_manifest(fixture("manifest.json"), apply=True)
    first.ledger.save(ledger_path)

    # Act
    second = weekly_sync.replay_ledger(fixture("manifest.json"), ledger_path)

    # Assert
    actions = {item.window.key: item.action for item in second.plan}
    assert actions["1674950400-1675209600"] == weekly_sync.ACTION_UNCHANGED
    assert actions["1673136000-1673740800"] == weekly_sync.ACTION_RETRY
    assert {
        action for key, action in actions.items() if key != "1673136000-1673740800"
    } == {weekly_sync.ACTION_UNCHANGED}
    report = second.ledger.report()
    assert report["pending"] == []
    assert report["window_status"]["1674950400-1675209600"] == "sealed_empty"
    assert report["window_status"]["1675209600-1675814400"] == "sealed"


def test_empty_body_is_pending_not_sealed_empty() -> None:
    # Arrange
    window = weekly_sync.Window(1674950400, 1675209600)
    ledger = weekly_sync.WeeklyChartSync([window])

    # Act
    result = ledger.ingest_xml(window, fixture("empty_body.xml").read_text())

    # Assert
    assert result.action == weekly_sync.ACTION_RETRY
    assert ledger.state(window).status == weekly_sync.STATUS_PENDING
    assert ledger.state(window).empty is False
    assert ledger.pending() == [window]


def test_error_body_and_missing_chart_element_stay_pending() -> None:
    window = weekly_sync.Window(1674950400, 1675209600)
    ledger = weekly_sync.WeeklyChartSync([window])

    for name in ("error_body.xml", "no_chart_element.xml"):
        result = ledger.ingest_xml(window, fixture(name).read_text())
        assert result.action == weekly_sync.ACTION_RETRY
        assert ledger.state(window).status == weekly_sync.STATUS_PENDING

    assert ledger.state(window).seen_responses == {
        "track-1674950400-1675209600-missing": "no-chart-payload"
    }


def test_successful_chart_with_no_rows_is_sealed_empty() -> None:
    # Arrange
    window = weekly_sync.Window(1674950400, 1675209600)
    ledger = weekly_sync.WeeklyChartSync([window])

    # Act
    result = ledger.ingest_xml(
        window, fixture("w6_sealed_empty.xml").read_text(), response_id="empty-chart"
    )

    # Assert
    assert result.action == weekly_sync.ACTION_FETCH
    assert result.status == weekly_sync.STATUS_SEALED_EMPTY
    assert ledger.state(window).empty is True
    assert ledger.sealed_empty() == [window]
    assert ledger.pending() == []


def test_empty_body_does_not_erase_an_existing_sealed_view() -> None:
    # Arrange
    window = weekly_sync.Window(1673136000, 1673740800)
    ledger = weekly_sync.WeeklyChartSync([window])
    ledger.ingest_xml(
        window, fixture("w2_original.xml").read_text(), response_id="original"
    )

    # Act
    result = ledger.ingest_xml(
        window, fixture("empty_body.xml").read_text(), response_id="later-empty"
    )

    # Assert
    assert result.action == weekly_sync.ACTION_RETRY
    assert ledger.state(window).status == weekly_sync.STATUS_SEALED
    assert ledger.state(window).current is not None
    assert len(ledger.state(window).current.snapshot.entries) == 1


def test_revision_replaces_current_view_and_keeps_original_source() -> None:
    # Arrange
    window = weekly_sync.Window(1672531200, 1673136000)
    ledger = weekly_sync.WeeklyChartSync([window])
    ledger.ingest_xml(
        window,
        fixture("w1_original.xml").read_text(),
        response_id="original",
        retrieved_at="2023-01-08T00:00:00Z",
    )

    # Act
    result = ledger.ingest_xml(
        window,
        fixture("w1_revision_a.xml").read_text(),
        response_id="revision-a",
        retrieved_at="2023-02-01T00:00:00Z",
    )

    # Assert
    assert result.action == weekly_sync.ACTION_UPDATE
    state = ledger.state(window)
    assert state.status == weekly_sync.STATUS_REVISED
    assert state.current.source.response_id == "revision-a"
    assert [entry.title for entry in state.current.snapshot.entries] == [
        "First Light",
        "Night Bus",
        "Harbour",
    ]
    assert [view.source.response_id for view in state.history] == ["original"]
    assert state.history[0].source.retrieved_at == "2023-01-08T00:00:00Z"
    assert state.history[0].snapshot.entries[0].playcount == 4


def test_second_revision_keeps_both_older_sources() -> None:
    # Arrange
    window = weekly_sync.Window(1672531200, 1673136000)
    ledger = weekly_sync.WeeklyChartSync([window])
    for name, source in (
        ("w1_original.xml", "original"),
        ("w1_revision_a.xml", "revision-a"),
    ):
        ledger.ingest_xml(window, fixture(name).read_text(), response_id=source)

    # Act
    result = ledger.ingest_xml(
        window, fixture("w1_revision_b.xml").read_text(), response_id="revision-b"
    )

    # Assert
    assert result.action == weekly_sync.ACTION_UPDATE
    state = ledger.state(window)
    assert [view.source.response_id for view in state.history] == [
        "original",
        "revision-a",
    ]
    assert state.current.source.response_id == "revision-b"
    assert state.current.snapshot.entries[0].playcount == 6


def test_identical_content_from_new_response_is_not_a_revision() -> None:
    # Arrange
    window = weekly_sync.Window(1674345600, 1674604800)
    ledger = weekly_sync.WeeklyChartSync([window])
    ledger.ingest_xml(window, fixture("w4_short.xml").read_text(), response_id="short")

    # Act
    result = ledger.ingest_xml(
        window, fixture("w4_same_content.xml").read_text(), response_id="same-content"
    )

    # Assert
    assert result.action == weekly_sync.ACTION_UNCHANGED
    assert ledger.state(window).status == weekly_sync.STATUS_SEALED
    assert ledger.state(window).history == []


def test_response_window_must_match_requested_window() -> None:
    # Arrange
    requested = weekly_sync.Window(1673136000, 1673740800)
    ledger = weekly_sync.WeeklyChartSync([requested])

    # Act / Assert
    with pytest.raises(ValueError, match="does not match requested window"):
        ledger.ingest_xml(
            requested, fixture("w4_short.xml").read_text(), response_id="wrong-window"
        )


def test_reingesting_same_response_is_idempotent() -> None:
    # Arrange
    window = weekly_sync.Window(1672531200, 1673136000)
    ledger = weekly_sync.WeeklyChartSync([window])
    xml = fixture("w1_original.xml").read_text()

    # Act
    first = ledger.ingest_xml(window, xml, response_id="original")
    second = ledger.ingest_xml(window, xml, response_id="original")

    # Assert
    assert first.changed is True
    assert second.action == weekly_sync.ACTION_UNCHANGED
    assert second.changed is False
    assert ledger.state(window).history == []


def test_reconcile_dry_run_does_not_mutate_ledger() -> None:
    # Arrange
    windows, baseline, incoming = weekly_sync.load_replay_manifest(
        fixture("manifest.json")
    )
    ledger = weekly_sync.WeeklyChartSync(windows)
    for candidate in baseline:
        weekly_sync._ingest_candidate(ledger, candidate)
    before = ledger.to_dict()

    # Act
    plan = ledger.reconcile(weekly_sync._group_candidates(incoming))

    # Assert
    assert plan
    assert ledger.to_dict() == before


def test_response_id_cannot_be_reused_with_different_content() -> None:
    # Arrange
    window = weekly_sync.Window(1672531200, 1673136000)
    ledger = weekly_sync.WeeklyChartSync([window])
    ledger.ingest_xml(
        window, fixture("w1_original.xml").read_text(), response_id="same-id"
    )

    # Act / Assert
    with pytest.raises(ValueError, match="reused with different content"):
        ledger.ingest_xml(
            window, fixture("w1_revision_a.xml").read_text(), response_id="same-id"
        )


def test_offline_replay_classifies_pending_and_update_work() -> None:
    # Act
    result = weekly_sync.replay_manifest(fixture("manifest.json"))

    # Assert
    plan = {item.window.key: item.action for item in result.plan}
    assert plan == {
        "1672531200-1673136000": weekly_sync.ACTION_UPDATE,
        "1673136000-1673740800": weekly_sync.ACTION_RETRY,
        "1673740800-1674345600": weekly_sync.ACTION_FETCH,
        "1674345600-1674604800": weekly_sync.ACTION_UNCHANGED,
        "1674604800-1674950400": weekly_sync.ACTION_FETCH,
        "1674950400-1675209600": weekly_sync.ACTION_FETCH,
        "1675209600-1675814400": weekly_sync.ACTION_FETCH,
    }
    report = result.ledger.report(result.plan)
    assert report["pending"] == [
        "1673740800-1674345600",
        "1674604800-1674950400",
        "1674950400-1675209600",
        "1675209600-1675814400",
    ]
    assert report["sealed_empty"] == []
    assert report["update"] == ["1672531200-1673136000"]


def test_offline_replay_apply_updates_only_the_current_view() -> None:
    # Act
    result = weekly_sync.replay_manifest(fixture("manifest.json"), apply=True)

    # Assert
    report = result.ledger.report()
    assert report["window_status"]["1672531200-1673136000"] == (
        weekly_sync.STATUS_REVISED
    )
    assert report["window_status"]["1673136000-1673740800"] == (
        weekly_sync.STATUS_SEALED
    )
    assert report["window_status"]["1674950400-1675209600"] == (
        weekly_sync.STATUS_SEALED_EMPTY
    )
    state = result.ledger.state((1672531200, 1673136000))
    assert state.current.source.response_id == "incoming-w1-revision-a"
    assert [view.source.response_id for view in state.history] == [
        "baseline-w1-original"
    ]
    assert report["sealed_empty"] == ["1674950400-1675209600"]


def test_offline_replay_second_revision_retains_history() -> None:
    # Act
    result = weekly_sync.replay_manifest(fixture("revision_manifest.json"), apply=True)

    # Assert
    updates = [
        item.window.key
        for item in result.plan
        if item.action == weekly_sync.ACTION_UPDATE
    ]
    assert updates == ["1672531200-1673136000"]
    state = result.ledger.state((1672531200, 1673136000))
    assert [view.source.response_id for view in state.history] == [
        "baseline-w1-original",
        "incoming-w1-revision-a",
    ]


def test_ledger_round_trips_through_json(tmp_path: Path) -> None:
    # Arrange
    result = weekly_sync.replay_manifest(fixture("manifest.json"), apply=True)
    path = tmp_path / "ledger.json"

    # Act
    result.ledger.save(path)
    loaded = weekly_sync.WeeklyChartSync.load(path)

    # Assert
    assert loaded.to_dict() == result.ledger.to_dict()
    state = loaded.state((1672531200, 1673136000))
    assert state.current is not None
    assert state.history[0].source.response_id == "baseline-w1-original"


def test_cli_replays_without_apply() -> None:
    # Act
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pylast.weekly_sync",
            str(fixture("manifest.json")),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    # Assert
    payload = json.loads(completed.stdout)
    assert payload["applied"] is False
    assert payload["report"]["update"] == ["1672531200-1673136000"]
    assert payload["report"]["window_status"]["1673740800-1674345600"] == "pending"
    assert payload["report"]["window_status"]["1674950400-1675209600"] == "pending"
