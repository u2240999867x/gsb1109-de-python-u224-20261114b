from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

import pylast


def _loved_body(
    page: int, total_pages: int, total: int, tracks: list[tuple[str, str]]
) -> bytes:
    parts = [
        '<?xml version="1.0"?>',
        f'<lfm status="ok"><lovedtracks user="alice" page="{page}" perPage="2" '
        f'totalPages="{total_pages}" total="{total}">',
    ]
    for artist, title in tracks:
        parts.append(
            f"<track><name>{title}</name><mbid></mbid>"
            '<date uts="1700000000">16 Nov 2023</date>'
            f"<artist><name>{artist}</name><mbid></mbid></artist></track>"
        )
    parts.append("</lovedtracks></lfm>")
    return "".join(parts).encode()


def _response(body: bytes) -> Mock:
    return Mock(status_code=200, read=Mock(return_value=body))


def _connection_errors(count: int = 3) -> list[ConnectionError]:
    return [ConnectionError("boom") for _ in range(count)]


def _key(title: str, artist: str = "Artist") -> pylast.LovedTrackKey:
    return pylast.LovedTrackKey(artist=artist, title=title)


@pytest.fixture
def user() -> pylast.User:
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    return network.get_user("alice")


class TestCaptureLovedTrackSnapshot:
    def test_complete_snapshot_covers_every_page(self, user: pylast.User) -> None:
        pages = [
            _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
            _response(_loved_body(2, 2, 3, [("Artist", "T3")])),
        ]

        with patch("httpx2.Client.post", side_effect=pages):
            snapshot = user.capture_loved_track_snapshot(page_limit=2)

        assert snapshot.complete
        assert not snapshot.drifted
        assert snapshot.coverage == pylast.SNAPSHOT_COMPLETE
        assert snapshot.page_count == 2
        assert snapshot.total_pages == 2
        assert snapshot.keys == {_key("T1"), _key("T2"), _key("T3")}

    def test_drifted_snapshot_flags_pages_disagreeing_about_size(
        self, user: pylast.User
    ) -> None:
        pages = [
            _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
            _response(_loved_body(2, 2, 4, [("Artist", "T3"), ("Artist", "T4")])),
        ]

        with patch("httpx2.Client.post", side_effect=pages):
            snapshot = user.capture_loved_track_snapshot(page_limit=2)

        assert snapshot.complete
        assert snapshot.drifted
        assert snapshot.coverage == pylast.SNAPSHOT_DRIFTED
        assert snapshot.totals_seen == (3, 4)

    def test_network_failure_keeps_partial_snapshot(self, user: pylast.User) -> None:
        pages = [
            _response(_loved_body(1, 3, 5, [("Artist", "T1"), ("Artist", "T2")])),
            *_connection_errors(),
        ]

        with (
            patch("httpx2.Client.post", side_effect=pages) as post,
            patch("pylast.time.sleep"),
        ):
            snapshot = user.capture_loved_track_snapshot(page_limit=2)

        assert not snapshot.complete
        assert not snapshot.drifted
        assert snapshot.coverage == pylast.SNAPSHOT_PARTIAL
        assert snapshot.page_count == 1
        assert snapshot.total_pages == 3
        assert snapshot.keys == {_key("T1"), _key("T2")}
        # One successful page plus three retries of the failing one.
        assert post.call_count == 4

    def test_non_retryable_error_is_not_swallowed(self, user: pylast.User) -> None:
        login_required = (
            b'<?xml version="1.0"?><lfm status="failed">'
            b'<error code="17">Login required</error></lfm>'
        )
        pages = [
            _response(_loved_body(1, 2, 3, [("Artist", "T1")])),
            _response(login_required),
        ]

        with (
            patch("httpx2.Client.post", side_effect=pages),
            pytest.raises(pylast.WSError) as excinfo,
        ):
            user.capture_loved_track_snapshot(page_limit=2)

        assert excinfo.value.get_id() == "17"


class TestLovedTrackStoreReconciliation:
    @pytest.fixture
    def store(self, tmp_path) -> pylast.LovedTrackStore:
        return pylast.LovedTrackStore(tmp_path / "loved.json", user_name="alice")

    def _sync(self, user, store, pages):
        with patch("httpx2.Client.post", side_effect=pages):
            return pylast.LovedTrackSyncer(user, store).sync(page_limit=2)

    def _partial(self, tracks):
        return [
            _response(_loved_body(1, 3, 5, tracks)),
            *_connection_errors(),
        ]

    def test_first_complete_sync_records_everything_as_added(self, user, store) -> None:
        pages = [
            _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
            _response(_loved_body(2, 2, 3, [("Artist", "T3")])),
        ]

        with patch("pylast.time.sleep"), patch("httpx2.Client.post", side_effect=pages):
            result = pylast.LovedTrackSyncer(user, store).sync(page_limit=2)

        assert result.complete
        assert not result.drifted
        assert {key.title for key in result.added} == {"T1", "T2", "T3"}
        assert result.retained == []
        assert result.removed == []
        assert result.unverified == []
        assert store.keys_with_status(pylast.LOVED_STATUS_ACTIVE) == [
            _key("T1"),
            _key("T2"),
            _key("T3"),
        ]

    def test_partial_sync_parks_unseen_tracks_as_pending(self, user, store) -> None:
        complete_pages = [
            _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
            _response(_loved_body(2, 2, 3, [("Artist", "T3")])),
        ]
        self._sync(user, store, complete_pages)

        with patch("pylast.time.sleep"):
            result = self._sync(
                user,
                store,
                self._partial([("Artist", "T1"), ("Artist", "T2")]),
            )

        assert not result.complete
        assert result.added == []
        assert result.removed == []
        assert {key.title for key in result.retained} == {"T1", "T2"}
        assert {key.title for key in result.unverified} == {"T3"}
        assert store.get_status(_key("T3")) == pylast.LOVED_STATUS_PENDING
        # The interrupted round is still kept on disk for review.
        assert store.latest_snapshot().coverage == pylast.SNAPSHOT_PARTIAL

    def test_reappearing_track_avoids_cancel_then_add_history(
        self, user, store
    ) -> None:
        complete_pages = [
            _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
            _response(_loved_body(2, 2, 3, [("Artist", "T3")])),
        ]
        self._sync(user, store, complete_pages)

        with patch("pylast.time.sleep"):
            # T3 missing from an incomplete fetch...
            self._sync(
                user,
                store,
                self._partial([("Artist", "T1"), ("Artist", "T2")]),
            )
            # ...and present again in the next incomplete fetch.
            result = self._sync(
                user,
                store,
                self._partial([("Artist", "T1"), ("Artist", "T3")]),
            )

        assert result.removed == []
        assert result.added == []
        assert {key.title for key in result.retained} == {"T1", "T3"}
        assert store.get_status(_key("T3")) == pylast.LOVED_STATUS_ACTIVE
        assert store.get_status(_key("T2")) == pylast.LOVED_STATUS_PENDING
        assert store.keys_with_status(pylast.LOVED_STATUS_REMOVED) == []

    def test_drifted_snapshot_never_confirms_removal(self, user, store) -> None:
        complete_pages = [
            _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
            _response(_loved_body(2, 2, 3, [("Artist", "T3")])),
        ]
        self._sync(user, store, complete_pages)

        drifted_pages = [
            _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T3")])),
            _response(_loved_body(2, 2, 4, [("Artist", "T4")])),
        ]
        result = self._sync(user, store, drifted_pages)

        assert result.drifted
        assert result.removed == []
        assert {key.title for key in result.unverified} == {"T2"}
        assert {key.title for key in result.added} == {"T4"}
        assert store.get_status(_key("T2")) == pylast.LOVED_STATUS_PENDING

    def test_complete_snapshot_confirms_removal_and_addition(self, user, store) -> None:
        self._sync(
            user,
            store,
            [
                _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
                _response(_loved_body(2, 2, 3, [("Artist", "T3")])),
            ],
        )

        result = self._sync(
            user,
            store,
            [
                _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T3")])),
                _response(_loved_body(2, 2, 3, [("Artist", "T4")])),
            ],
        )

        assert result.complete
        assert not result.drifted
        assert {key.title for key in result.removed} == {"T2"}
        assert {key.title for key in result.added} == {"T4"}
        assert {key.title for key in result.retained} == {"T1", "T3"}
        assert store.get_status(_key("T2")) == pylast.LOVED_STATUS_REMOVED

    def test_removal_is_reported_only_once_across_complete_syncs(
        self, user, store
    ) -> None:
        self._sync(
            user,
            store,
            [
                _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
                _response(_loved_body(2, 2, 3, [("Artist", "T3")])),
            ],
        )
        without_t2 = [
            _response(_loved_body(1, 1, 2, [("Artist", "T1"), ("Artist", "T3")])),
        ]

        first = self._sync(user, store, without_t2)
        second = self._sync(user, store, list(without_t2))

        assert {key.title for key in first.removed} == {"T2"}
        assert second.removed == []
        assert store.get_status(_key("T2")) == pylast.LOVED_STATUS_REMOVED

    def test_reloved_track_is_added_and_active_again(self, user, store) -> None:
        self._sync(
            user,
            store,
            [
                _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
                _response(_loved_body(2, 2, 3, [("Artist", "T3")])),
            ],
        )
        self._sync(
            user,
            store,
            [
                _response(_loved_body(1, 2, 2, [("Artist", "T1"), ("Artist", "T3")])),
                _response(_loved_body(2, 2, 2, [])),
            ],
        )
        assert store.get_status(_key("T2")) == pylast.LOVED_STATUS_REMOVED

        result = self._sync(
            user,
            store,
            [
                _response(_loved_body(1, 2, 4, [("Artist", "T1"), ("Artist", "T2")])),
                _response(_loved_body(2, 2, 4, [("Artist", "T3"), ("Artist", "T4")])),
            ],
        )

        assert {key.title for key in result.added} == {"T2", "T4"}
        assert store.get_status(_key("T2")) == pylast.LOVED_STATUS_ACTIVE

    def test_store_round_trips_through_disk(self, user, store) -> None:
        pages = [
            _response(_loved_body(1, 2, 3, [("Artist", "T1"), ("Artist", "T2")])),
            _response(_loved_body(2, 2, 3, [("Artist", "T3")])),
        ]
        result = self._sync(user, store, pages)

        reloaded = pylast.LovedTrackStore(store.path)

        assert reloaded.user_name == "alice"
        assert len(reloaded.snapshots) == 1
        assert (
            reloaded.latest_complete_snapshot().captured_at
            == result.snapshot.captured_at
        )
        assert reloaded.get_status(_key("T1")) == pylast.LOVED_STATUS_ACTIVE
        assert reloaded.get_status(_key("Nope")) is None
        rehydrated = reloaded.latest_snapshot().tracks[_key("T1")]
        assert rehydrated.track.title == "T1"
        assert str(rehydrated.track.artist) == "Artist"


class TestDiffLovedTrackSnapshots:
    def _snapshot(self, titles, complete, drifted=False):
        network = pylast.LastFMNetwork(api_key="k", api_secret="s")
        tracks = {
            _key(title): pylast.LovedTrack(
                pylast.Track("Artist", title, network), "16 Nov 2023", "1700000000"
            )
            for title in titles
        }
        return pylast.LovedTrackSnapshot(
            captured_at="now",
            tracks=tracks,
            complete=complete,
            drifted=drifted,
        )

    def test_first_snapshot_treats_everything_as_added(self) -> None:
        current = self._snapshot(["T1", "T2"], complete=True)

        diff = pylast.diff_loved_track_snapshots(None, current)

        assert diff is not None
        assert diff.added == {_key("T1"), _key("T2")}
        assert diff.retained == frozenset()
        assert diff.removed == frozenset()

    def test_incomplete_snapshot_reports_unverified_instead_of_removed(self) -> None:
        previous = self._snapshot(["T1", "T2", "T3"], complete=True)
        current = self._snapshot(["T1"], complete=False)

        diff = pylast.diff_loved_track_snapshots(previous, current)

        assert diff.removed == frozenset()
        assert diff.unverified == {_key("T2"), _key("T3")}
        assert diff.retained == {_key("T1")}

    def test_drifted_snapshot_does_not_confirm_removals(self) -> None:
        previous = self._snapshot(["T1", "T2"], complete=True)
        current = self._snapshot(["T1"], complete=True, drifted=True)

        diff = pylast.diff_loved_track_snapshots(previous, current)

        assert diff.removed == frozenset()
        assert diff.unverified == {_key("T2")}

    def test_complete_snapshot_confirms_removals(self) -> None:
        previous = self._snapshot(["T1", "T2"], complete=True)
        current = self._snapshot(["T1", "T3"], complete=True)

        diff = pylast.diff_loved_track_snapshots(previous, current)

        assert diff.added == {_key("T3")}
        assert diff.retained == {_key("T1")}
        assert diff.removed == {_key("T2")}
        assert diff.unverified == frozenset()
