from __future__ import annotations

import threading
from unittest.mock import Mock, patch

import pytest

import pylast


@pytest.mark.parametrize(
    ("given, expected"),
    [(True, "1"), (False, "0"), (1, "1"), (0, "0"), ("foo", "foo"), ("1", "1")],
)
def test_param_conversion(given: bool | int | str, expected: str) -> None:
    assert pylast._Request._convert_param(given) == expected


FAKE_BODY = (
    b'<?xml version="1.0"?>'
    b'<lfm status="ok"><album><userplaycount>1</userplaycount></album></lfm>'
)


def _fake_response() -> Mock:
    return Mock(status_code=200, read=Mock(return_value=FAKE_BODY))


def test_download_response_does_not_mutate_params() -> None:
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    request = pylast._Request(
        network, "album.getInfo", {"artist": "A", "album": "B", "username": "alice"}
    )
    original = dict(request.params)

    with patch("httpx2.Client.post", return_value=_fake_response()):
        request._download_response()

    assert request.params == original
    assert "username" in request.params


RECENT_TRACKS_BODY = (
    b'<?xml version="1.0"?>'
    b'<lfm status="ok">'
    b'<recenttracks user="alice" page="1" perPage="2" totalPages="1" total="1">'
    b'<track><artist mbid="">Artist</artist><name>Song</name>'
    b'<album mbid="">Album</album><date uts="1700000000">16 Nov 2023</date>'
    b"</track></recenttracks></lfm>"
)

LOGIN_REQUIRED_BODY = (
    b'<?xml version="1.0"?>'
    b'<lfm status="failed">'
    b'<error code="17">Login: User required to be logged in</error></lfm>'
)


def test_collect_nodes_raises_wserror_without_retrying() -> None:
    # Arrange
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    user = network.get_user("alice")
    response = Mock(status_code=200, read=Mock(return_value=LOGIN_REQUIRED_BODY))

    # Act
    with (
        patch("httpx2.Client.post", return_value=response) as post,
        patch("pylast.time.sleep") as sleep,
        pytest.raises(pylast.WSError) as excinfo,
    ):
        user.get_recent_tracks(limit=1)

    # Assert
    assert excinfo.value.get_id() == "17"
    assert post.call_count == 1
    assert sleep.call_count == 0


def test_collect_nodes_retries_transient_errors() -> None:
    # Arrange
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    user = network.get_user("alice")
    unavailable = Mock(status_code=503)
    ok = Mock(status_code=200, read=Mock(return_value=RECENT_TRACKS_BODY))

    # Act
    with (
        patch("httpx2.Client.post", side_effect=[unavailable, unavailable, ok]) as post,
        patch("pylast.time.sleep") as sleep,
    ):
        tracks = user.get_recent_tracks(limit=1)

    # Assert
    assert len(tracks) == 1
    assert tracks[0].track.title == "Song"
    assert post.call_count == 3
    assert sleep.call_count == 2


def test_collect_nodes_preserves_error_type_when_retries_exhausted() -> None:
    # Arrange
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    user = network.get_user("alice")
    unavailable = Mock(status_code=503)

    # Act
    with (
        patch("httpx2.Client.post", return_value=unavailable) as post,
        patch("pylast.time.sleep"),
        pytest.raises(pylast.WSError) as excinfo,
    ):
        user.get_recent_tracks(limit=1)

    # Assert
    assert excinfo.value.get_id() == 503
    assert post.call_count == 3


def test_cacheable_request_with_username_param_hits_cache_on_second_call() -> None:
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    network.enable_caching()
    album = pylast.Album("Beatles", "Abbey Road", network, username="alice")

    with patch("httpx2.Client.post", return_value=_fake_response()) as post:
        album.get_userplaycount()
        album.get_userplaycount()
        album.get_userplaycount()

    assert post.call_count == 1


def _friends_page(page: int, total_pages: int, names: list[str]) -> bytes:
    users = "".join(f"<user><name>{name}</name></user>" for name in names)
    return (
        '<?xml version="1.0"?>'
        '<lfm status="ok">'
        f'<friends user="alice" page="{page}" perPage="2" '
        f'totalPages="{total_pages}" total="4">'
        f"{users}</friends></lfm>"
    ).encode()


def _paged_post(pages: dict[str, list[str]], requested: list[str]):
    def post(url, data=None):
        page = data["page"]
        requested.append(page)
        body = _friends_page(int(page), len(pages), pages[page])
        return Mock(status_code=200, read=Mock(return_value=body))

    return post


def test_concurrent_readers_share_in_flight_page_request() -> None:
    # Arrange: two readers paging through the same endpoint at the same time
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    user = network.get_user("alice")
    pages = {"1": ["bob", "carol"], "2": ["dave", "erin"]}
    requested: list[str] = []
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    second_got_first = threading.Event()
    allow_page_2 = threading.Event()
    results: dict[str, list[str]] = {}
    errors: dict[str, Exception] = {}

    def post(url, data=None):
        page = data["page"]
        requested.append(page)
        if page == "1":
            fetch_started.set()
            assert release_fetch.wait(timeout=10)
        else:
            assert allow_page_2.wait(timeout=10)
        body = _friends_page(int(page), 2, pages[page])
        return Mock(status_code=200, read=Mock(return_value=body))

    def read_first() -> None:
        try:
            results["first"] = [
                friend.get_name()
                for friend in user.get_friends(limit=None, stream=True)
            ]
        except Exception as e:  # pragma: no cover
            errors["first"] = e

    def read_second() -> None:
        try:
            names = []
            for friend in user.get_friends(limit=None, stream=True):
                names.append(friend.get_name())
                second_got_first.set()
            results["second"] = names
        except Exception as e:  # pragma: no cover
            errors["second"] = e

    # Act
    with patch("httpx2.Client.post", side_effect=post):
        first = threading.Thread(target=read_first)
        first.start()
        assert fetch_started.wait(timeout=10)
        second = threading.Thread(target=read_second)
        second.start()
        release_fetch.set()
        # Keep the first reader inside the shared source until the second
        # reader has attached and received its first item from page 1.
        assert second_got_first.wait(timeout=10)
        allow_page_2.set()
        first.join(timeout=10)
        second.join(timeout=10)

    # Assert: identical pages were fetched once, both readers got everything
    assert not errors
    assert results["first"] == ["bob", "carol", "dave", "erin"]
    assert results["second"] == ["bob", "carol", "dave", "erin"]
    assert requested == ["1", "2"]


def test_early_exit_keeps_shared_pages_alive_for_other_reader() -> None:
    # Arrange: a preview reader stops early while a full scrape continues
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    user = network.get_user("alice")
    pages = {"1": ["a", "b"], "2": ["c", "d"]}
    requested: list[str] = []

    # Act
    with patch("httpx2.Client.post", side_effect=_paged_post(pages, requested)):
        preview = user.get_friends(limit=None, stream=True)
        full = user.get_friends(limit=None, stream=True)
        assert next(preview).get_name() == "a"
        full_iter = iter(full)
        assert next(full_iter).get_name() == "a"
        # The preview stops early: pages it fetched must stay available
        preview.close()
        rest = [friend.get_name() for friend in full_iter]

    # Assert: the full scrape is undisturbed, each page fetched only once
    assert rest == ["b", "c", "d"]
    assert requested == ["1", "2"]


def test_no_more_pages_fetched_once_all_readers_stop() -> None:
    # Arrange
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    user = network.get_user("alice")
    pages = {"1": ["a", "b"], "2": ["c", "d"]}
    requested: list[str] = []

    # Act: both readers stop after the first page
    with patch("httpx2.Client.post", side_effect=_paged_post(pages, requested)):
        first = user.get_friends(limit=None, stream=True)
        second = user.get_friends(limit=None, stream=True)
        assert next(first).get_name() == "a"
        assert next(second).get_name() == "a"
        first.close()
        second.close()

    # Assert: with no readers left, page 2 is never fetched
    assert requested == ["1"]


def test_readers_with_limit_mid_page_keep_solo_order() -> None:
    # Arrange: duplicates span pages, the preview limit lands mid page 2
    pages = {"1": ["a", "b"], "2": ["c", "d"]}
    requested: list[str] = []

    def read_all(user, limit):
        return [
            friend.get_name() for friend in user.get_friends(limit=limit, stream=True)
        ]

    # Act: interleave a limited preview with a full scrape on one source
    shared_network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    shared_user = shared_network.get_user("alice")
    with patch("httpx2.Client.post", side_effect=_paged_post(pages, requested)):
        preview = shared_user.get_friends(limit=3, stream=True)
        full = shared_user.get_friends(limit=None, stream=True)
        shared_preview = []
        shared_full = []
        for _ in range(3):
            shared_preview.append(next(preview).get_name())
            shared_full.append(next(full).get_name())
        shared_full.extend(friend.get_name() for friend in full)
        preview.close()

    # Solo runs on separate networks for comparison
    solo_network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    solo_user = solo_network.get_user("alice")
    with patch("httpx2.Client.post", side_effect=_paged_post(pages, [])):
        solo_preview = read_all(solo_user, 3)
        solo_full = read_all(solo_user, None)

    # Assert: each reader saw exactly what it would have seen alone
    assert shared_preview == solo_preview == ["a", "b", "c"]
    assert shared_full == solo_full == ["a", "b", "c", "d"]
    # ... while each page was fetched only once for both readers
    assert requested == ["1", "2"]


def test_failed_page_is_retried_by_a_later_reader() -> None:
    # Arrange: page 2 keeps failing, then recovers
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    user = network.get_user("alice")
    pages = {"1": ["a", "b"], "2": ["c", "d"]}
    unavailable = Mock(status_code=503)
    page_2_attempts = 0

    def post(url, data=None):
        nonlocal page_2_attempts
        page = data["page"]
        if page == "2":
            page_2_attempts += 1
            if page_2_attempts <= 3:
                return unavailable
        body = _friends_page(int(page), 2, pages[page])
        return Mock(status_code=200, read=Mock(return_value=body))

    # Act & Assert: the first reader exhausts its retries on page 2
    with (
        patch("httpx2.Client.post", side_effect=post),
        patch("pylast.time.sleep"),
        pytest.raises(pylast.WSError) as excinfo,
    ):
        user.get_friends(limit=None)
    assert excinfo.value.get_id() == 503

    # A later reader gets its own attempt instead of the cached failure
    with (
        patch("httpx2.Client.post", side_effect=post),
        patch("pylast.time.sleep"),
    ):
        names = [friend.get_name() for friend in user.get_friends(limit=None)]
    assert names == ["a", "b", "c", "d"]


def test_shared_page_failure_reaches_every_reader() -> None:
    # Arrange: page 2 always fails, two readers wait on it
    network = pylast.LastFMNetwork(api_key="k", api_secret="s")
    user = network.get_user("alice")
    pages = {"1": ["a", "b"], "2": ["c", "d"]}
    unavailable = Mock(status_code=503)

    def post(url, data=None):
        page = data["page"]
        if page == "2":
            return unavailable
        body = _friends_page(int(page), 2, pages[page])
        return Mock(status_code=200, read=Mock(return_value=body))

    errors: dict[str, Exception] = {}

    def read(key: str) -> None:
        try:
            user.get_friends(limit=None)
        except Exception as e:
            errors[key] = e

    # Act
    with (
        patch("httpx2.Client.post", side_effect=post),
        patch("pylast.time.sleep"),
    ):
        threads = [
            threading.Thread(target=read, args=(key,)) for key in ("first", "second")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    # Assert: each reader keeps its own failure state
    assert set(errors) == {"first", "second"}
    for error in errors.values():
        assert isinstance(error, pylast.WSError)
        assert error.get_id() == 503
