from __future__ import annotations

import datetime as dt
from xml.parsers import expat

import pytest

import pylast
from pylast.recent_tracks import (
    OBSERVATION_AMBIGUOUS,
    OBSERVATION_PENDING,
    OBSERVATION_UNIQUE,
    RecentTracksImporter,
)


def make_page(
    tracks: list[tuple[str, str, int | None, bool]],
    *,
    page: int = 1,
    total_pages: int = 1,
    user: str = "alice",
) -> str:
    entries = []
    for artist, title, timestamp, now_playing in tracks:
        date = "" if timestamp is None else f'<date uts="{timestamp}">a date</date>'
        now_playing_attribute = ' nowplaying="true"' if now_playing else ""
        entries.append(
            f"<track{now_playing_attribute}>"
            f"<artist>{artist}</artist><name>{title}</name>"
            f"<album>An album</album>{date}</track>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<lfm status="ok"><recenttracks user="{user}" page="{page}" '
        f'perPage="50" totalPages="{total_pages}" total="{len(tracks)}">'
        + "".join(entries)
        + "</recenttracks></lfm>"
    )


def test_observation_is_kept_separate_from_confirmed_plays() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Now", None, True), ("Other", "Past", 100, False)]),
        observed_at=120,
    )

    assert [str(play.identity) for play in importer.plays] == ["Other - Past"]
    assert len(importer.observations) == 1
    observation = importer.observations[0]
    assert observation.status == OBSERVATION_PENDING
    assert observation.time_range == (100, 120)
    assert observation.candidates == ()


def test_observation_resolves_to_one_later_history_event() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Now", None, True), ("Other", "Past", 100, False)]),
        observed_at=120,
    )
    importer.add_page(
        make_page(
            [("Artist", "Now", 110, False), ("Other", "Past", 100, False)],
            page=1,
        ),
        observed_at=130,
    )

    assert importer.observations[0].status == OBSERVATION_UNIQUE
    resolved = importer.observations[0].resolved_play
    assert resolved is not None
    assert resolved.timestamp == 110
    assert len(importer.plays) == 2


def test_consecutive_same_track_plays_are_not_deduplicated_by_title() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Loop", None, True), ("Other", "Past", 100, False)]),
        observed_at=150,
    )
    importer.add_page(
        make_page(
            [
                ("Artist", "Loop", 130, False),
                ("Artist", "Loop", 120, False),
                ("Other", "Past", 100, False),
            ]
        ),
        observed_at=160,
    )

    plays = [play for play in importer.plays if play.identity.title == "Loop"]
    assert [play.timestamp for play in plays] == [120, 130]
    observation = importer.observations[0]
    assert observation.status == OBSERVATION_AMBIGUOUS
    assert [play.timestamp for play in observation.candidates] == [120, 130]


def test_observation_only_matches_tracks_with_both_artist_and_title() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Same", None, True), ("Other", "Past", 100, False)]),
        observed_at=150,
    )
    importer.add_page(
        make_page([("Different Artist", "Same", 130, False)]),
        observed_at=160,
    )

    assert importer.observations[0].status == OBSERVATION_PENDING
    assert importer.observations[0].candidates == ()


def test_observation_does_not_match_play_at_or_before_previous_event() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Same", 100, False), ("Artist", "Same", None, True)]),
        observed_at=150,
    )

    observation = importer.observations[0]
    assert observation.previous_timestamp == 100
    assert observation.status == OBSERVATION_PENDING


def test_disappearing_now_playing_does_not_become_a_play() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Vanishes", None, True), ("Other", "Past", 100, False)]),
        observed_at=120,
    )
    importer.add_page(
        make_page([("Other", "New", 130, False), ("Other", "Past", 100, False)]),
        observed_at=140,
    )

    assert [str(play.identity) for play in importer.plays] == [
        "Other - Past",
        "Other - New",
    ]
    assert len(importer.observations) == 1
    assert importer.observations[0].status == OBSERVATION_PENDING


def test_late_page_can_resolve_an_earlier_observation() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Now", None, True), ("Other", "Past", 100, False)]),
        observed_at=140,
    )
    importer.add_page(
        make_page([("Other", "New", 150, False), ("Other", "Past", 100, False)]),
        observed_at=160,
    )

    assert importer.observations[0].status == OBSERVATION_PENDING

    importer.add_page(
        make_page([("Artist", "Now", 120, False), ("Other", "Past", 110, False)]),
        observed_at=170,
    )

    observation = importer.observations[0]
    assert observation.status == OBSERVATION_UNIQUE
    assert observation.resolved_play is not None
    assert observation.resolved_play.timestamp == 120


def test_adding_pages_in_a_different_order_does_not_change_plays() -> None:
    pages = [
        (
            make_page(
                [("Artist", "Now", None, True), ("Other", "Past", 100, False)],
                page=1,
            ),
            120,
        ),
        (
            make_page(
                [("Artist", "Now", 110, False), ("Other", "Past", 100, False)],
                page=2,
            ),
            130,
        ),
    ]

    forward = RecentTracksImporter()
    for xml_page, observed_at in pages:
        forward.add_page(xml_page, observed_at=observed_at)

    backward = RecentTracksImporter()
    for xml_page, observed_at in reversed(pages):
        backward.add_page(xml_page, observed_at=observed_at)

    assert [(play.identity, play.timestamp) for play in forward.plays] == [
        (play.identity, play.timestamp) for play in backward.plays
    ]


def test_observation_observed_at_accepts_utc_datetime() -> None:
    importer = RecentTracksImporter()
    observed_at = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.UTC)

    importer.add_page(
        make_page([("Artist", "Now", None, True)]),
        observed_at=observed_at,
    )

    assert importer.observations[0].observed_at == int(observed_at.timestamp())


def test_observation_time_range_is_closed_upper_and_open_lower() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Now", None, True), ("Other", "Past", 100, False)]),
        observed_at=120,
    )
    importer.add_page(
        make_page(
            [
                ("Artist", "Now", 120, False),
                ("Artist", "Now", 100, False),
            ]
        ),
        observed_at=130,
    )

    candidates = importer.observations[0].candidates
    assert [play.timestamp for play in candidates] == [120]


def test_bad_xml_and_missing_timestamp_raise_value_error() -> None:
    importer = RecentTracksImporter()

    with pytest.raises(ValueError, match="no timestamp"):
        importer.add_page(make_page([("Artist", "Song", None, False)]))

    with pytest.raises(expat.ExpatError):
        importer.add_page("not xml")


def test_top_level_exports() -> None:
    assert pylast.RecentTracksImporter is RecentTracksImporter
    assert pylast.TrackIdentity("Artist", "Song") == pylast.TrackIdentity(
        " artist ", "SONG"
    )


def test_duplicate_page_import_is_idempotent() -> None:
    page = make_page([("Other", "New", 130, False), ("Other", "Past", 100, False)])
    importer = RecentTracksImporter()

    importer.add_page(page, observed_at=140)
    importer.add_page(page, observed_at=140)

    assert [str(play) for play in importer.plays] == [
        "Other - Past @ 100",
        "Other - New @ 130",
    ]


def test_bytes_input_and_lowercase_page_attributes() -> None:
    page = (
        make_page([("Other", "Past", 100, False), ("Other", "New", 130, False)])
        .replace("totalPages", "totalpages")
        .encode()
    )

    imported = pylast.parse_recent_tracks_page(page)

    assert imported.total_pages == 1
    assert [play.timestamp for play in imported.plays] == [100, 130]


def test_late_page_turns_unique_attribution_into_two_possibilities() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Now", None, True), ("Other", "Past", 100, False)]),
        observed_at=140,
    )
    importer.add_page(
        make_page([("Artist", "Now", 120, False), ("Other", "Past", 100, False)]),
        observed_at=150,
    )

    assert importer.observations[0].status == OBSERVATION_UNIQUE

    importer.add_page(
        make_page(
            [
                ("Artist", "Now", 120, False),
                ("Artist", "Now", 110, False),
                ("Other", "Past", 100, False),
            ]
        ),
        observed_at=160,
    )

    observation = importer.observations[0]
    assert observation.status == OBSERVATION_AMBIGUOUS
    assert observation.resolved_play is None
    assert [play.timestamp for play in observation.possible_plays] == [110, 120]
    assert len(importer.plays) == 3


def test_later_other_track_limits_the_observation_window() -> None:
    importer = RecentTracksImporter()

    importer.add_page(
        make_page([("Artist", "Now", None, True), ("Other", "Past", 100, False)]),
        observed_at=150,
    )
    importer.add_page(
        make_page([("Artist", "Now", 110, False), ("Other", "Next", 130, False)]),
        observed_at=160,
    )

    observation = importer.observations[0]
    assert observation.possible_plays == ()
    assert observation.time_range == (130, 150)


def test_import_many_pages_with_per_page_observation_times() -> None:
    importer = pylast.import_recent_tracks_pages(
        [
            (
                make_page(
                    [("Artist", "Now", None, True), ("Other", "Past", 100, False)]
                ),
                140,
            ),
            (
                make_page(
                    [("Artist", "Now", 120, False), ("Other", "Past", 100, False)]
                ),
                150,
            ),
        ]
    )

    assert importer.observations[0].status == OBSERVATION_UNIQUE
    assert importer.observations[0].resolved_play.timestamp == 120


def test_add_file(tmp_path) -> None:
    page_path = tmp_path / "page.xml"
    page_path.write_text(make_page([("Other", "Past", 100, False)]), encoding="utf-8")

    importer = RecentTracksImporter()
    imported = importer.add_file(page_path)

    assert imported.user == "alice"
    assert [play.timestamp for play in importer.plays] == [100]


def test_parse_rejects_entries_missing_identity() -> None:
    xml = (
        '<lfm status="ok"><recenttracks totalpages="1">'
        '<track><name>Only a title</name><date uts="100">date</date></track>'
        "</recenttracks></lfm>"
    )

    with pytest.raises(ValueError, match="missing an artist or track name"):
        pylast.parse_recent_tracks_page(xml)
