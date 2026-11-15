from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import pylast
from pylast.search_journal import (
    ORIGIN_ORIGINAL,
    SearchJournal,
    SearchJournalError,
    SearchQuery,
    SearchResult,
)


def _body(
    *,
    for_terms: str,
    page: int,
    query_terms: str | None = None,
    role: str = "request",
    extra_queries: tuple[tuple[str, str], ...] = (),
    start_index: int = 0,
    names: tuple[str, ...] = (),
    total_results: int = 100,
    items_per_page: int = 30,
) -> bytes:
    queries = [
        f'<Query role="{role}" searchTerms="{query_terms or for_terms}" '
        f'startPage="{page}"></Query>'
    ]
    queries.extend(
        f'<Query role="{extra_role}" searchTerms="{terms}" startPage="1"></Query>'
        for extra_role, terms in extra_queries
    )
    tracks = "".join(
        f"<track><name>{name}</name><artist>Nirvana</artist>"
        f"<url>https://example.test/{name}</url><mbid></mbid>"
        f"<listeners>1</listeners></track>"
        for name in names
    )
    body = (
        f'<lfm status="ok"><results for="{for_terms}">'
        + "".join(queries)
        + f"<totalResults>{total_results}</totalResults>"
        f"<startIndex>{start_index}</startIndex>"
        f"<itemsPerPage>{items_per_page}</itemsPerPage>"
        f"<trackmatches>{tracks}</trackmatches>"
        "</results></lfm>"
    )
    return body.encode()


def _response(body: bytes) -> Mock:
    return Mock(status_code=200, read=Mock(return_value=body))


def _network(**kwargs) -> pylast.LastFMNetwork:
    return pylast.LastFMNetwork(api_key="k", api_secret="s", **kwargs)


def test_pages_record_actual_interpretation_and_ranks() -> None:
    # Arrange
    network = _network()
    search = network.search_for_track("Nirvana", "Smells Like Teen Spirit")
    page_one = _body(
        for_terms="Smells Like Teen Spirit",
        page=1,
        query_terms="Smells Like Teen Spirit",
        names=("A", "B"),
        start_index=0,
    )
    page_two = _body(
        for_terms="Smells Like Teen Spirits",
        page=2,
        query_terms="Smells Like Teen Spirits",
        names=("C", "D"),
        start_index=30,
    )

    # Act
    with patch(
        "httpx2.Client.post", side_effect=[_response(page_one), _response(page_two)]
    ):
        search.get_next_page()
        search.get_next_page()

    # Assert
    pages = search.recover().pages
    assert [page.page for page in pages] == [1, 2]
    assert [page.query.search_terms for page in pages] == [
        "Smells Like Teen Spirit",
        "Smells Like Teen Spirits",
    ]
    assert [page.ranks for page in pages] == [(1, 2), (31, 32)]
    assert pages[0].requested_terms == "Smells Like Teen Spirit"
    assert pages[1].query_changed is True
    assert pages[0].raw_response == page_one.decode()
    assert pages[1].raw_response == page_two.decode()

    # The pointer stays on the requested sequence despite the new wording.
    assert [branch.page_numbers for branch in search.recover().branches] == [[1], [2]]


def test_same_normalized_terms_from_different_filters_do_not_share_pointer() -> None:
    # Arrange
    network = _network()
    journal = SearchJournal()
    filtered = network.search_for_track("Nirvana", "Teen Spirit", journal=journal)
    other = network.search_for_track("", "Teen Spirit", journal=journal)
    body = _body(for_terms="Teen Spirit", page=1, names=("A",))

    # Act
    with patch("httpx2.Client.post", return_value=_response(body)) as post:
        filtered.get_next_page()
        post.reset_mock()
        other.get_next_page()

    # Assert
    assert post.call_count == 1
    assert filtered.journal.next_page(
        "track", filtered.search_filters, ORIGIN_ORIGINAL, filtered.requested_terms
    ) == 2
    assert other.journal.next_page(
        "track", other.search_filters, ORIGIN_ORIGINAL, other.requested_terms
    ) == 2
    assert filtered.recover().filter_key != other.recover().filter_key


def test_retrying_an_already_recorded_page_does_not_skip_the_next() -> None:
    # Arrange
    network = _network()
    search = network.search_for_track("Nirvana", "Song")
    page_one = _body(for_terms="Song", page=1, names=("A",))
    page_two = _body(for_terms="Song", page=2, names=("B",), start_index=30)

    # Act
    with patch("httpx2.Client.post", return_value=_response(page_one)) as post:
        search.get_page(1)
        search.get_page(1)
        assert post.call_count == 1

    assert search.journal.next_page(
        "track", search.search_filters, ORIGIN_ORIGINAL, search.requested_terms
    ) == 2

    with patch("httpx2.Client.post", return_value=_response(page_two)) as post:
        results = search.get_next_page()

    # Assert
    assert post.call_count == 1
    assert [track.title for track in results] == ["B"]
    assert [page.page for page in search.recover().pages] == [1, 2]


def test_following_a_correction_gets_its_own_pages() -> None:
    # Arrange
    network = _network()
    search = network.search_for_track("Nirvana", "Smels Like Teen Spirit")
    page_one = _body(
        for_terms="Smels Like Teen Spirit",
        page=1,
        query_terms="Smells Like Teen Spirit",
        extra_queries=(
            ("correction", "Smells Like Teen Spirit"),
            ("suggestion", "Nirvana - Smells Like Teen Spirit"),
        ),
        names=("A",),
    )
    correction_page = _body(
        for_terms="Smells Like Teen Spirit",
        page=1,
        names=("B",),
    )

    # Act
    with patch("httpx2.Client.post", return_value=_response(page_one)):
        search.get_next_page()
    corrected = search.follow_suggestion("Smells Like Teen Spirit")
    with patch("httpx2.Client.post", return_value=_response(correction_page)):
        corrected.get_next_page()

    # Assert
    recovery = search.recover()
    assert recovery.original_pages == (1,)
    assert recovery.correction_pages == (1,)
    assert recovery.next_page == 2

    explanation = search.explain()
    assert [row["page"] for row in explanation["pages"]] == [1, 1]
    assert {row["source"] for row in explanation["pages"]} == {
        "original_query",
        "correction_suggestion",
    }
    assert explanation["original_query"]["pages"] == [1]
    assert explanation["correction_branches"][0]["search_terms"] == (
        "Smells Like Teen Spirit"
    )
    suggestions = {
        item["search_terms"]: item for item in explanation["suggested_corrections"]
    }
    assert set(suggestions) == {
        "Smells Like Teen Spirit",
        "Nirvana - Smells Like Teen Spirit",
    }
    assert suggestions["Smells Like Teen Spirit"]["applied"] is True
    assert suggestions["Smells Like Teen Spirit"]["applied_branches"]
    assert suggestions["Nirvana - Smells Like Teen Spirit"]["applied"] is False


def test_journal_survives_reopen_and_resumes_without_skipping(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "search.jsonl"
    page_one = _body(for_terms="Song", page=1, names=("A",))
    page_two = _body(for_terms="Song", page=2, names=("B",), start_index=30)
    page_three = _body(for_terms="Song", page=3, names=("C",), start_index=60)

    first = _network()
    first.enable_search_journal(path)
    search = first.search_for_track("Nirvana", "Song")
    with patch("httpx2.Client.post", return_value=_response(page_one)):
        search.get_next_page()
    with patch("httpx2.Client.post", return_value=_response(page_two)):
        search.get_next_page()

    # Act: a new process picks up the same journal.
    second = _network()
    second.enable_search_journal(path)
    resumed = second.search_for_track("Nirvana", "Song")
    with patch("httpx2.Client.post", return_value=_response(page_three)) as post:
        results = resumed.get_next_page()

    # Assert
    assert post.call_count == 1
    assert [track.title for track in results] == ["C"]
    assert [page.page for page in resumed.recover().pages] == [1, 2, 3]
    assert resumed.get_raw_response(1) == page_one.decode()
    assert resumed.get_raw_response(3) == page_three.decode()


def test_durable_journal_keeps_pages_of_two_filter_sets_apart(
    tmp_path: Path,
) -> None:
    # Arrange
    path = tmp_path / "search.jsonl"
    journal = SearchJournal(path)
    journal.record_page(
        kind="track",
        filters={"track": "Teen Spirit", "artist": "Nirvana"},
        page=1,
        query=SearchQuery(search_terms="Teen Spirit"),
        results=(SearchResult(rank=1, position=1, name="A"),),
        raw_response="<one/>",
    )
    journal.record_page(
        kind="track",
        filters={"track": "Teen Spirit", "artist": ""},
        page=1,
        query=SearchQuery(search_terms="Teen Spirit"),
        results=(SearchResult(rank=1, position=1, name="B"),),
        raw_response="<two/>",
    )

    # Act
    reopened = SearchJournal(path)

    # Assert
    assert len(reopened) == 2
    nirvana = reopened.recovery("track", {"track": "Teen Spirit", "artist": "Nirvana"})
    anyone = reopened.recovery("track", {"track": "Teen Spirit", "artist": ""})
    assert nirvana.next_page == 2
    assert anyone.next_page == 2
    assert nirvana.filter_key != anyone.filter_key
    assert nirvana.pages[0].raw_response == "<one/>"
    assert anyone.pages[0].raw_response == "<two/>"


def test_journal_repairs_a_torn_final_record(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "search.jsonl"
    journal = SearchJournal(path)
    journal.record_page(
        kind="track",
        filters={"track": "Song", "artist": "Nirvana"},
        page=1,
        raw_response="<ok/>",
    )
    with path.open("ab") as stream:
        stream.write(b'{"version":1,"type":"search_page","pa')

    # Act
    reopened = SearchJournal(path)

    # Assert
    assert len(reopened) == 1
    recovery = reopened.recovery("track", {"track": "Song", "artist": "Nirvana"})
    assert recovery.next_page == 2
    new_page = reopened.record_page(
        kind="track",
        filters={"track": "Song", "artist": "Nirvana"},
        page=2,
        raw_response="<ok2/>",
    )
    assert new_page.page == 2
    assert len(SearchJournal(path)) == 2


def test_journal_rejects_corrupt_interior_record(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "search.jsonl"
    path.write_text(
        '{"version": 1, "type": "search_page"}\nnot json\n',
        encoding="utf-8",
    )

    # Act / Assert
    with pytest.raises(SearchJournalError):
        SearchJournal(path)


def test_record_is_idempotent_and_keeps_first_response() -> None:
    # Arrange
    journal = SearchJournal()

    # Act
    first = journal.record_page(
        kind="album",
        filters={"album": "Nevermind"},
        page=1,
        raw_response="<first/>",
        recorded_at=1.0,
    )
    second = journal.record_page(
        kind="album",
        filters={"album": "Nevermind"},
        page=1,
        raw_response="<second/>",
        recorded_at=2.0,
    )

    # Assert
    assert second is first
    assert first.raw_response == "<first/>"
    assert journal.recovery("album", {"album": "Nevermind"}).next_page == 2


def test_query_roles_and_correction_detection() -> None:
    assert SearchQuery(role="request", search_terms="A").is_correction is False
    assert SearchQuery(role="correction", search_terms="B").is_correction is True
    assert SearchQuery(role="Suggestion", search_terms="C").is_correction is True


def test_album_and_artist_search_use_the_shared_journal(tmp_path: Path) -> None:
    # Arrange
    album_body = (
        b'<?xml version="1.0"?><lfm status="ok"><results for="Nevermind">'
        b'<Query role="request" searchTerms="Nevermind" startPage="1"></Query>'
        b"<totalResults>1</totalResults><startIndex>0</startIndex>"
        b"<itemsPerPage>50</itemsPerPage><albummatches><album>"
        b"<name>Nevermind</name><artist>Nirvana</artist><url>u</url>"
        b"<image size=\"small\">i</image></album></albummatches></results></lfm>"
    )
    artist_body = (
        b'<?xml version="1.0"?><lfm status="ok"><results for="Nirvana">'
        b'<Query role="request" searchTerms="Nirvana" startPage="1"></Query>'
        b"<totalResults>1</totalResults><startIndex>0</startIndex>"
        b"<itemsPerPage>30</itemsPerPage><artistmatches><artist>"
        b"<name>Nirvana</name><listeners>1</listeners></artist>"
        b"</artistmatches></results></lfm>"
    )
    path = tmp_path / "shared.jsonl"
    network = _network()
    network.enable_search_journal(path)

    # Act
    with patch(
        "httpx2.Client.post",
        side_effect=[_response(album_body), _response(artist_body)],
    ):
        albums = network.search_for_album("Nevermind").get_next_page()
        artists = network.search_for_artist("Nirvana").get_next_page()

    # Assert
    assert albums[0].title == "Nevermind"
    assert albums[0].info["image"] == ["i"]
    assert artists[0].name == "Nirvana"
    reopened = SearchJournal(path)
    assert len(reopened) == 2
    assert reopened.recovery("album", {"album": "Nevermind"}).original_pages == (1,)
    assert reopened.recovery("artist", {"artist": "Nirvana"}).original_pages == (1,)


def test_explain_serialises_deterministically() -> None:
    # Arrange
    journal = SearchJournal()
    journal.record_page(
        kind="track",
        filters={"track": "Song", "artist": "Nirvana"},
        page=1,
        query=SearchQuery(search_terms="Song"),
        results=(SearchResult(rank=1, position=1, name="A"),),
    )

    # Act
    text = journal.recovery(
        "track", {"track": "Song", "artist": "Nirvana"}
    ).describe()

    # Assert
    assert json.loads(text)["original_query"]["pages"] == [1]
    assert "original_input" in text
