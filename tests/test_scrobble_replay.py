"""
Offline replay of scrobble_many batches against stored Last.fm responses.

A player came back online with 53 queued scrobbles. These tests replay the
queue through Network.scrobble_many with a stubbed _Request.execute that
returns stored <lfm> responses, then reconcile each batch's receipts against
the rows actually sent in that batch.

Reconciliation rules under test:
- Batches are split at the API limit of 50 tracks per scrobble_many call.
- Receipts are matched per batch, by timestamp then artist/title, consuming
  duplicates in order: the same song at the same second may genuinely have
  been played twice, so (title, timestamp) is not a unique key.
- A receipt with corrected="1" still matches its submitted row, and the
  server-corrected track title is kept.
- Rows with no matching receipt are "unconfirmed"; only unconfirmed rows go
  on the retry list, never rows already confirmed accepted or ignored.
"""

from __future__ import annotations

import re
from xml.dom import minidom

import pytest

import pylast

BATCH_LIMIT = 50
QUEUE_SIZE = 53
BASE_TIMESTAMP = 1_700_000_000

# Row indices with special server behaviour in the stored responses.
MISSING_RECEIPT_ROW = 37  # batch 1 response silently drops this scrobble
IGNORED_ROW = 5  # batch 1 response reports ignoredMessage code="1"
CORRECTED_ROWS = {8: "Track 08 (Remastered)", 51: "Track 51 (Single Version)"}
DUPLICATE_SOURCE_ROW = 10  # row 11 is an exact replay of row 10


def make_offline_queue() -> list[dict]:
    """53 records queued while the player was offline."""
    queue = []
    for i in range(QUEUE_SIZE):
        queue.append(
            {
                "artist": f"Artist {i % 5}",
                "title": f"Track {i:02d}",
                "timestamp": BASE_TIMESTAMP + i * 240,
            }
        )
    # Same song, same second, genuinely played twice.
    queue[DUPLICATE_SOURCE_ROW + 1] = dict(queue[DUPLICATE_SOURCE_ROW])
    return queue


def _scrobble_xml(row: dict, *, corrected_title=None, ignored_code="0") -> str:
    corrected = "1" if corrected_title else "0"
    track_name = corrected_title or row["title"]
    return (
        "<scrobble>"
        f'<track corrected="{corrected}">{track_name}</track>'
        f'<artist corrected="0">{row["artist"]}</artist>'
        '<album corrected="0"/>'
        '<albumArtist corrected="0"/>'
        f"<timestamp>{row['timestamp']}</timestamp>"
        f'<ignoredMessage code="{ignored_code}"/>'
        "</scrobble>"
    )


def _response_xml(scrobbles: list[str], ignored: int) -> str:
    return (
        '<lfm status="ok">'
        f'<scrobbles accepted="{len(scrobbles) - ignored}" ignored="{ignored}">'
        + "".join(scrobbles)
        + "</scrobbles></lfm>"
    )


def make_stored_responses(queue: list[dict]) -> list[str]:
    """One stored response per batch: batch 1 drops a row, batch 2 is clean."""
    batch1 = []
    for i, row in enumerate(queue[:BATCH_LIMIT]):
        if i == MISSING_RECEIPT_ROW:
            continue
        batch1.append(
            _scrobble_xml(
                row,
                corrected_title=CORRECTED_ROWS.get(i),
                ignored_code="1" if i == IGNORED_ROW else "0",
            )
        )
    batch2 = [
        _scrobble_xml(row, corrected_title=CORRECTED_ROWS.get(BATCH_LIMIT + j))
        for j, row in enumerate(queue[BATCH_LIMIT:])
    ]
    return [_response_xml(batch1, ignored=1), _response_xml(batch2, ignored=0)]


def _text(node, tag: str) -> str:
    element = node.getElementsByTagName(tag)[0]
    return element.firstChild.data if element.firstChild else ""


def parse_receipts(doc) -> list[dict]:
    """Parse a stored track.scrobble response into per-scrobble receipts."""
    receipts = []
    for node in doc.getElementsByTagName("scrobble"):
        track = node.getElementsByTagName("track")[0]
        ignored = node.getElementsByTagName("ignoredMessage")[0]
        receipts.append(
            {
                "artist": _text(node, "artist"),
                "track": _text(node, "track"),
                "track_corrected": track.getAttribute("corrected") == "1",
                "timestamp": int(_text(node, "timestamp")),
                "ignored_code": ignored.getAttribute("code"),
            }
        )
    return receipts


class ReplayHarness:
    """Stubs _Request.execute: records what was sent, returns stored XML."""

    def __init__(self, stored_responses: list[str]) -> None:
        self.stored_responses = stored_responses
        self.sent_batches: list[list[dict]] = []
        self.receipts_per_batch: list[list[dict]] = []

    def fake_execute(self, request, cacheable: bool = False):
        rows: dict[int, dict] = {}
        for key, value in request.params.items():
            match = re.fullmatch(r"(artist|track|timestamp)\[(\d+)\]", key)
            if match:
                field, index = match.group(1), int(match.group(2))
                rows.setdefault(index, {})[field] = value
        batch = [rows[i] for i in sorted(rows)]
        for row in batch:
            row["timestamp"] = int(row["timestamp"])
        batch_index = len(self.sent_batches)
        self.sent_batches.append(batch)
        doc = minidom.parseString(self.stored_responses[batch_index])
        self.receipts_per_batch.append(parse_receipts(doc))
        return doc

    def install(self, monkeypatch) -> None:
        def execute(request, cacheable: bool = False):
            return self.fake_execute(request, cacheable)

        monkeypatch.setattr(pylast._Request, "execute", execute)


def reconcile(sent: list[dict], receipts: list[dict]) -> dict[int, dict]:
    """Match one batch's receipts to its sent rows.

    Receipts are pooled by timestamp and consumed in request order, so two
    identical plays of the same song at the same second each consume their
    own receipt. A corrected track title still matches its submitted row.
    Returns {batch_row_index: receipt} for matched rows only.
    """
    pool: dict[int, list[dict]] = {}
    for receipt in receipts:
        pool.setdefault(receipt["timestamp"], []).append(receipt)

    matched = {}
    for index, row in enumerate(sent):
        candidates = pool.get(row["timestamp"], [])
        for receipt in candidates:
            title_matches = receipt["track"] == row["track"] or (
                receipt["track_corrected"] and receipt["artist"] == row["artist"]
            )
            if receipt["artist"] == row["artist"] and title_matches:
                candidates.remove(receipt)
                matched[index] = receipt
                break
    return matched


def replay_and_reconcile(monkeypatch) -> tuple[list[dict], list[dict]]:
    """Replay the offline queue; return (report, retry_list)."""
    queue = make_offline_queue()
    harness = ReplayHarness(make_stored_responses(queue))
    harness.install(monkeypatch)

    network = pylast.LastFMNetwork(api_key="dummy", api_secret="dummy")
    network.scrobble_many(queue)

    report = []
    for batch_index, sent in enumerate(harness.sent_batches):
        matched = reconcile(sent, harness.receipts_per_batch[batch_index])
        for row_index, row in enumerate(sent):
            record = queue[batch_index * BATCH_LIMIT + row_index]
            receipt = matched.get(row_index)
            if receipt is None:
                status = "unconfirmed"
            elif receipt["ignored_code"] != "0":
                status = "ignored"
            else:
                status = "accepted"
            report.append(
                {
                    "index": batch_index * BATCH_LIMIT + row_index,
                    "artist": record["artist"],
                    "title": record["title"],
                    "timestamp": record["timestamp"],
                    "status": status,
                    "corrected_title": (
                        receipt["track"]
                        if receipt is not None and receipt["track_corrected"]
                        else None
                    ),
                }
            )
    retry_list = [entry for entry in report if entry["status"] == "unconfirmed"]
    return report, retry_list


@pytest.fixture
def replay(monkeypatch):
    return replay_and_reconcile(monkeypatch)


class TestScrobbleReplay:
    def test_batches_respect_api_limit(self, monkeypatch) -> None:
        queue = make_offline_queue()
        harness = ReplayHarness(make_stored_responses(queue))
        harness.install(monkeypatch)
        network = pylast.LastFMNetwork(api_key="dummy", api_secret="dummy")

        network.scrobble_many(queue)

        assert [len(batch) for batch in harness.sent_batches] == [50, 3]

    def test_every_record_has_exactly_one_status(self, replay) -> None:
        report, _ = replay
        assert len(report) == QUEUE_SIZE
        assert {entry["status"] for entry in report} <= {
            "accepted",
            "ignored",
            "unconfirmed",
        }
        statuses = [entry["status"] for entry in report]
        assert statuses.count("accepted") == 51
        assert statuses.count("ignored") == 1
        assert statuses.count("unconfirmed") == 1

    def test_ignored_row_is_explicitly_ignored(self, replay) -> None:
        report, _ = replay
        assert report[IGNORED_ROW]["status"] == "ignored"

    def test_missing_receipt_row_is_unconfirmed(self, replay) -> None:
        report, _ = replay
        assert report[MISSING_RECEIPT_ROW]["status"] == "unconfirmed"

    def test_duplicate_same_song_same_second_both_confirmed(self, replay) -> None:
        report, _ = replay
        first = report[DUPLICATE_SOURCE_ROW]
        second = report[DUPLICATE_SOURCE_ROW + 1]
        assert (first["title"], first["timestamp"]) == (
            second["title"],
            second["timestamp"],
        )
        assert first["status"] == second["status"] == "accepted"

    def test_server_corrected_titles_are_kept(self, replay) -> None:
        report, _ = replay
        corrected = {
            entry["index"]: entry["corrected_title"]
            for entry in report
            if entry["corrected_title"] is not None
        }
        assert corrected == CORRECTED_ROWS

    def test_retry_list_contains_only_unconfirmed_rows(self, replay) -> None:
        report, retry_list = replay
        assert [entry["index"] for entry in retry_list] == [MISSING_RECEIPT_ROW]
        confirmed = {
            (entry["artist"], entry["title"], entry["timestamp"])
            for entry in report
            if entry["status"] != "unconfirmed"
        }
        for entry in retry_list:
            assert entry["status"] == "unconfirmed"
        assert not any(
            (entry["artist"], entry["title"], entry["timestamp"]) in confirmed
            and entry["status"] != "unconfirmed"
            for entry in retry_list
        )
