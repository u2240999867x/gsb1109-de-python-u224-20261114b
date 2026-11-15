"""Unit tests for versioned, source-tagged artist alias handling."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

import pylast


def _network() -> pylast.LastFMNetwork:
    return pylast.LastFMNetwork(api_key="k", api_secret="s")


def _correction(name: str, mbid: str | None, position: int = 0) -> pylast.Correction:
    return pylast.Correction(position, name, mbid, f"https://example.test/{name}")


def _fake_get_corrections(mapping: dict[str, list[pylast.Correction]]):
    def fake(self: pylast.Artist) -> list[pylast.Correction]:
        return mapping[self.get_name()]

    return fake


def test_extract_corrections_keeps_id_url_and_index() -> None:
    # Arrange
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<lfm status="ok"><corrections>'
        '<correction index="0"><artist>'
        "<name>Guns N' Roses</name>"
        "<mbid>e0140a67-e4d1-4f13-8a01-364355bee46e</mbid>"
        "<url>https://www.last.fm/music/Guns+N%27+Roses</url>"
        "</artist></correction>"
        '<correction index="1"><artist>'
        "<name>Hollywood Rose</name>"
        "<mbid></mbid>"
        "<url>https://www.last.fm/music/Hollywood+Rose</url>"
        "</artist></correction>"
        "</corrections></lfm>"
    )
    doc = pylast._parse_response(xml)

    # Act
    corrections = pylast._extract_corrections(doc)

    # Assert
    assert len(corrections) == 2
    assert corrections[0].position == 0
    assert corrections[0].name == "Guns N' Roses"
    assert corrections[0].mbid == "e0140a67-e4d1-4f13-8a01-364355bee46e"
    assert corrections[1].position == 1
    assert corrections[1].name == "Hollywood Rose"
    assert corrections[1].mbid is None


def test_get_correction_stays_backwards_compatible() -> None:
    # Arrange
    artist = pylast.Artist("guns and roses", _network())
    corrected = [_correction("Guns N' Roses", "mbid-1")]

    # Act / Assert
    with patch.object(pylast.Artist, "get_corrections", return_value=corrected):
        assert artist.get_correction() == "Guns N' Roses"

    with patch.object(pylast.Artist, "get_corrections", return_value=[]):
        assert artist.get_correction() is None


def test_two_renames_keep_every_hop_versioned_under_one_id() -> None:
    # Arrange
    network = _network()
    mapping = {
        "Prince": [_correction("The Artist", "mbid-prince")],
        "The Artist": [
            _correction("The Artist Formerly Known As Prince", "mbid-prince")
        ],
        "The Artist Formerly Known As Prince": [
            _correction("The Artist Formerly Known As Prince", "mbid-prince")
        ],
    }
    index = pylast.ArtistAliasIndex()

    # Act
    with patch.object(pylast.Artist, "get_corrections", _fake_get_corrections(mapping)):
        result = index.import_corrections(
            pylast.Artist("Prince", network), source="lastfm", version="2026.10"
        )

    # Assert
    assert result.cycle is None
    assert [record.alias for record in result.records] == [
        "Prince",
        "The Artist",
        "The Artist Formerly Known As Prince",
    ]
    assert {record.artist_id for record in result.records} == {"mbid-prince"}
    assert all(record.source == "lastfm" for record in result.records)
    assert all(record.version == "2026.10" for record in result.records)
    assert index.aliases_for("mbid-prince") == [
        "Prince",
        "The Artist",
        "The Artist Formerly Known As Prince",
    ]
    assert index.canonical_name("mbid-prince") == (
        "The Artist Formerly Known As Prince"
    )


def test_shared_display_name_never_merges_artists() -> None:
    # Arrange
    index = pylast.ArtistAliasIndex()

    # Act
    index.add_alias(
        alias="Phoenix",
        artist_id="mbid-french",
        canonical_name="Phoenix",
        source="lastfm",
        version="2026.10",
    )
    index.add_alias(
        alias="phoenix",
        artist_id="mbid-american",
        canonical_name="Phoenix",
        source="musicbrainz",
        version="2026.10",
    )

    # Assert
    matches = index.lookup("PHOENIX")
    assert {record.artist_id for record in matches} == {
        "mbid-french",
        "mbid-american",
    }
    assert index.aliases_for("mbid-french") == ["Phoenix"]
    assert index.aliases_for("mbid-american") == ["phoenix"]
    assert index.canonical_name("mbid-french") == "Phoenix"
    assert index.canonical_name("mbid-american") == "Phoenix"


def test_old_play_without_id_keeps_pending_candidates() -> None:
    # Arrange
    index = pylast.ArtistAliasIndex()

    # Act: a correction without an authoritative id is not merged anywhere
    stored = index.add_alias(
        alias="Mystery Band",
        artist_id=None,
        canonical_name="Mystery Band?",
        source="legacy-scrobble",
        version="2026.09",
    )
    index.add_pending_play("Mystery Band", ["mbid-a", "mbid-b"])

    # Assert
    assert stored is not None
    assert index.pending_candidates("mystery band") == [
        "pending:mystery band?@legacy-scrobble/2026.09",
        "mbid-a",
        "mbid-b",
    ]
    assert index.lookup("Mystery Band") == []
    assert index.canonical_name("mbid-a") is None


def test_cycle_keeps_evidence_and_batch_continues() -> None:
    # Arrange
    network = _network()
    mapping = {
        "X": [_correction("Y", "mbid-x")],
        "Y": [_correction("X", "mbid-x")],
        "Steady": [_correction("Steady", "mbid-steady")],
    }
    index = pylast.ArtistAliasIndex()

    # Act
    with patch.object(pylast.Artist, "get_corrections", _fake_get_corrections(mapping)):
        results = index.import_many(
            [
                pylast.Artist("X", network),
                pylast.Artist("Steady", network),
            ],
            source="lastfm",
            version="2026.10",
        )

    # Assert
    assert results[0].cycle == ("X", "Y", "X")
    assert [record.alias for record in results[0].records] == ["X", "Y"]
    assert index.cycles("lastfm", "2026.10") == [("X", "Y", "X")]
    evidence = index.evidence("lastfm", "2026.10")
    assert {record.alias for record in evidence} >= {"X", "Y"}
    # The second, healthy artist was still imported
    assert len(results) == 2
    assert index.canonical_name("mbid-steady") == "Steady"


def test_failed_artist_does_not_stop_the_batch() -> None:
    # Arrange
    network = _network()
    index = pylast.ArtistAliasIndex()

    def fake(self: pylast.Artist) -> list[pylast.Correction]:
        if self.get_name() == "Broken":
            raise pylast.WSError(network, 8, "operation failed")
        return [_correction("Fixed", "mbid-fixed")]

    # Act
    with patch.object(pylast.Artist, "get_corrections", fake):
        results = index.import_many(
            [
                pylast.Artist("Broken", network),
                pylast.Artist("Typo", network),
            ],
            source="lastfm",
            version="2026.10",
        )

    # Assert
    assert len(results) == 1
    assert index.canonical_name("mbid-fixed") == "Fixed"


def test_as_of_version_returns_the_name_accepted_then() -> None:
    # Arrange
    index = pylast.ArtistAliasIndex()
    index.add_alias("Old Spelling", "mbid-1", "Old Spelling", "lastfm", "2026.09")
    index.add_alias("Old Spelling", "mbid-1", "New Spelling", "lastfm", "2026.10")
    index.add_alias("Old Spelling", "mbid-1", "Newest Spelling", "lastfm", "2026.11")

    # Act / Assert
    assert index.canonical_name("mbid-1", "2026.09") == "Old Spelling"
    assert index.canonical_name("mbid-1", "2026.10") == "New Spelling"
    assert index.canonical_name("mbid-1", "2026.11") == "Newest Spelling"
    assert index.canonical_name("mbid-1") == "Newest Spelling"

    as_of_last_month = index.lookup("Old Spelling", version="2026.10")
    assert {record.canonical_name for record in as_of_last_month} == {
        "Old Spelling",
        "New Spelling",
    }


def test_older_correction_cannot_overwrite_newer_one() -> None:
    # Arrange
    index = pylast.ArtistAliasIndex()
    index.add_alias("Name", "mbid-1", "New", "lastfm", "2026.11")

    # Act
    stale = index.add_alias("Name", "mbid-1", "Old", "lastfm", "2026.10")

    # Assert
    assert stale is None
    assert index.canonical_name("mbid-1", "2026.11") == "New"


@pytest.mark.parametrize(
    "candidate, reference, older",
    [
        ("2026.9", "2026.10", True),
        ("2026.10", "2026.10", False),
        ("2026.11", "2026.10", False),
        ("3.2.1", "3.2.2", True),
    ],
)
def test_version_ordering(candidate: str, reference: str, older: bool) -> None:
    assert pylast._is_older_version(candidate, reference) is older


def _correction_body(name: str, mbid: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<lfm status="ok"><corrections><correction index="0"><artist>'
        f"<name>{name}</name><mbid>{mbid}</mbid>"
        f"<url>https://example.test/{name}</url>"
        "</artist></correction></corrections></lfm>"
    ).encode()


def test_import_follows_corrections_over_mocked_http() -> None:
    # Arrange
    network = _network()
    bodies = {
        "Old Name": _correction_body("Middle Name", "mbid-1"),
        "Middle Name": _correction_body("New Name", "mbid-1"),
        "New Name": _correction_body("New Name", "mbid-1"),
    }

    def handler(*args, **kwargs) -> Mock:
        artist = kwargs["data"]["artist"]
        return Mock(status_code=200, read=Mock(return_value=bodies[artist]))

    post = Mock(side_effect=handler)
    index = pylast.ArtistAliasIndex()

    # Act
    with patch("httpx2.Client.post", post):
        result = index.import_corrections(
            pylast.Artist("Old Name", network),
            source="lastfm",
            version="2026.10",
        )

    # Assert
    assert post.call_count == 3
    assert result.cycle is None
    assert [record.alias for record in result.records] == [
        "Old Name",
        "Middle Name",
        "New Name",
    ]
    assert all(record.artist_id == "mbid-1" for record in result.records)
    assert index.canonical_name("mbid-1", "2026.10") == "New Name"
