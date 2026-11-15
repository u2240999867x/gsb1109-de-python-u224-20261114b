"""Offline import of Last.fm ``user.getRecentTracks`` pages.

The regular :meth:`pylast.User.get_recent_tracks` API returns only timestamped
scrobbles.  A recent-tracks page can also contain a currently playing track,
which has no ``date``/``uts`` value and therefore cannot be treated as a
playback yet.  This module keeps those provisional sightings separate from
confirmed plays and reconciles them when later pages arrive.
"""

from __future__ import annotations

import datetime as dt
import html
import os
import time
from dataclasses import dataclass
from typing import Any
from xml.dom import minidom

OBSERVATION_PENDING = "pending"
OBSERVATION_UNIQUE = "unique"
OBSERVATION_AMBIGUOUS = "ambiguous"

__all__ = [
    "ConfirmedPlay",
    "NowPlayingObservation",
    "OBSERVATION_AMBIGUOUS",
    "OBSERVATION_PENDING",
    "OBSERVATION_UNIQUE",
    "RecentTracksImporter",
    "RecentTracksPage",
    "TrackIdentity",
    "import_recent_tracks_pages",
    "parse_recent_tracks_page",
]


@dataclass(frozen=True, eq=False)
class TrackIdentity:
    """The stable identity of a track: artist plus title.

    Album and MBID values are metadata, not identity: the same recording can
    appear with a different album (for example on a compilation), and either
    value may be absent.  Comparing only titles is not enough because two
    different artists can use the same title.

    Comparison mirrors :class:`pylast.Track`: names are case-insensitive and
    surrounding whitespace is ignored.
    """

    artist: str
    title: str

    @property
    def key(self) -> tuple[str, str]:
        """A normalized key suitable for dictionaries and comparisons."""

        return self.artist.strip().casefold(), self.title.strip().casefold()

    def __eq__(self, other) -> bool:
        if not isinstance(other, TrackIdentity):
            return NotImplemented
        return self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __str__(self) -> str:
        return f"{self.artist} - {self.title}"


@dataclass(frozen=True)
class ConfirmedPlay:
    """A timestamped playback from the recent-tracks history."""

    identity: TrackIdentity
    timestamp: int
    playback_date: str | None = None
    album: str | None = None
    page: int | None = None
    track: Any = None

    @property
    def artist(self) -> str:
        return self.identity.artist

    @property
    def title(self) -> str:
        return self.identity.title

    @property
    def key(self) -> tuple[tuple[str, str], int]:
        """A playback is identified by its track and start time."""

        return self.identity.key, self.timestamp

    def __str__(self) -> str:
        return f"{self.identity} @ {self.timestamp}"


@dataclass
class NowPlayingObservation:
    """A provisional now-playing sighting without a playback timestamp.

    ``observed_at`` is the time at which the page containing the now-playing
    entry was collected.  ``previous_timestamp`` is the timestamp of the
    closest older confirmed event that was adjacent to the now-playing entry.
    A matching play must therefore start after ``previous_timestamp`` and no
    later than ``observed_at``.
    """

    identity: TrackIdentity
    observed_at: int
    previous_timestamp: int | None = None
    album: str | None = None
    page: int | None = None
    track: Any = None
    candidates: tuple[ConfirmedPlay, ...] = ()

    @property
    def time_range(self) -> tuple[int | None, int]:
        """The possible start-time range, as ``(exclusive lower, upper]``."""

        return self.previous_timestamp, self.observed_at

    @property
    def status(self) -> str:
        """``pending`` with no match, ``unique`` with one, or ``ambiguous``."""

        if not self.candidates:
            return OBSERVATION_PENDING
        if len(self.candidates) == 1:
            return OBSERVATION_UNIQUE
        return OBSERVATION_AMBIGUOUS

    @property
    def resolved_play(self) -> ConfirmedPlay | None:
        """The confirmed playback, but only when the attribution is unique."""

        return self.candidates[0] if len(self.candidates) == 1 else None

    @property
    def attributed_play(self) -> ConfirmedPlay | None:
        """Alias for :attr:`resolved_play`."""

        return self.resolved_play

    @property
    def possible_plays(self) -> tuple[ConfirmedPlay, ...]:
        """All confirmed plays that could belong to this observation."""

        return self.candidates

    @property
    def is_unique(self) -> bool:
        return len(self.candidates) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.candidates) > 1

    @property
    def is_pending(self) -> bool:
        return not self.candidates

    @property
    def is_resolved(self) -> bool:
        return bool(self.candidates)


@dataclass(frozen=True)
class RecentTracksPage:
    """The parsed contents of one recent-tracks XML page."""

    user: str | None
    page: int | None
    per_page: int | None
    total_pages: int | None
    total: int | None
    observed_at: int | None
    plays: tuple[ConfirmedPlay, ...]
    observations: tuple[NowPlayingObservation, ...]


@dataclass(frozen=True)
class _ParsedTrack:
    identity: TrackIdentity
    album: str | None
    playback_date: str | None
    timestamp: int | None
    now_playing: bool


class RecentTracksImporter:
    """Import recent-tracks XML pages and reconcile now-playing entries.

    Pages can be added in any order.  Confirmed plays are deduplicated by
    ``(artist, title, timestamp)``, not by title alone, so two consecutive
    plays of the same track remain two plays.  An observation is re-evaluated
    whenever a page is added, which also handles a page that arrives late.
    """

    def __init__(self, network=None) -> None:
        self.network = network
        self._plays: dict[tuple[tuple[str, str], int], ConfirmedPlay] = {}
        self._observations: dict[
            tuple[tuple[str, str], int], NowPlayingObservation
        ] = {}
        self._pages: list[RecentTracksPage] = []

    @property
    def plays(self) -> tuple[ConfirmedPlay, ...]:
        """All confirmed plays, in chronological order."""

        return tuple(
            sorted(self._plays.values(), key=lambda play: (play.timestamp, play.key))
        )

    @property
    def confirmed_plays(self) -> tuple[ConfirmedPlay, ...]:
        """Alias for :attr:`plays`."""

        return self.plays

    @property
    def observations(self) -> tuple[NowPlayingObservation, ...]:
        """All provisional observations, ordered by collection time."""

        return tuple(
            sorted(
                self._observations.values(),
                key=lambda observation: (
                    observation.observed_at,
                    observation.identity.key,
                ),
            )
        )

    @property
    def pending_observations(self) -> tuple[NowPlayingObservation, ...]:
        """Observations for which no matching confirmed play is known yet."""

        return tuple(
            observation
            for observation in self.observations
            if observation.status == OBSERVATION_PENDING
        )

    @property
    def ambiguous_observations(self) -> tuple[NowPlayingObservation, ...]:
        """Observations with more than one possible confirmed play."""

        return tuple(
            observation
            for observation in self.observations
            if observation.status == OBSERVATION_AMBIGUOUS
        )

    @property
    def pages(self) -> tuple[RecentTracksPage, ...]:
        """Pages in the order in which they were added."""

        return tuple(self._pages)

    def add_page(
        self,
        xml: str | bytes | minidom.Document,
        observed_at: float | dt.datetime | None = None,
        *,
        page: int | None = None,
    ) -> RecentTracksPage:
        """Import one recent-tracks XML page.

        ``observed_at`` is the collection time of the page.  It is required to
        put a bounded time range around a now-playing entry; when omitted it
        defaults to the current time.  A datetime without a timezone is
        interpreted as UTC.
        """

        doc = self._parse_xml(xml)
        metadata = self._page_metadata(doc)
        if page is not None:
            metadata["page"] = page

        parsed_tracks = [
            self._parse_track(track_node)
            for track_node in doc.getElementsByTagName("track")
        ]

        has_now_playing = any(track.now_playing for track in parsed_tracks)
        page_observed_at = None
        if has_now_playing:
            page_observed_at = (
                int(time.time())
                if observed_at is None
                else self._to_timestamp(observed_at)
            )

        imported_plays: list[ConfirmedPlay] = []
        for parsed in parsed_tracks:
            if parsed.now_playing or parsed.timestamp is None:
                continue

            play = ConfirmedPlay(
                identity=parsed.identity,
                timestamp=parsed.timestamp,
                playback_date=parsed.playback_date,
                album=parsed.album,
                page=metadata["page"],
                track=self._make_track(parsed.identity),
            )
            imported_plays.append(self._store_play(play))

        imported_observations: list[NowPlayingObservation] = []
        page_timestamps = [
            parsed.timestamp for parsed in parsed_tracks if parsed.timestamp is not None
        ]
        for parsed in parsed_tracks:
            if not parsed.now_playing:
                continue

            assert page_observed_at is not None
            adjacent = [
                timestamp
                for timestamp in page_timestamps
                if timestamp < page_observed_at
            ]
            observation = self._store_observation(
                parsed=parsed,
                observed_at=page_observed_at,
                page=metadata["page"],
                adjacent_timestamp=max(adjacent) if adjacent else None,
            )
            imported_observations.append(observation)

        self._refresh_observations()

        result = RecentTracksPage(
            user=metadata["user"],
            page=metadata["page"],
            per_page=metadata["per_page"],
            total_pages=metadata["total_pages"],
            total=metadata["total"],
            observed_at=page_observed_at,
            plays=tuple(sorted(imported_plays, key=lambda play: play.timestamp)),
            observations=tuple(imported_observations),
        )
        self._pages.append(result)
        return result

    def add_xml(
        self,
        xml: str | bytes | minidom.Document,
        observed_at: float | dt.datetime | None = None,
        *,
        page: int | None = None,
    ) -> RecentTracksPage:
        """Alias for :meth:`add_page`."""

        return self.add_page(xml, observed_at=observed_at, page=page)

    def add_file(
        self,
        path: str | os.PathLike[str],
        observed_at: float | dt.datetime | None = None,
        *,
        page: int | None = None,
    ) -> RecentTracksPage:
        """Read a page from ``path`` and import it."""

        with open(path, "rb") as page_file:
            return self.add_page(page_file.read(), observed_at=observed_at, page=page)

    def refresh(self) -> None:
        """Re-evaluate every observation against all known plays."""

        self._refresh_observations()

    def _store_play(self, play: ConfirmedPlay) -> ConfirmedPlay:
        existing = self._plays.get(play.key)
        if existing is None:
            self._plays[play.key] = play
            return play

        if existing.album is None and play.album is not None:
            replacement = ConfirmedPlay(
                identity=existing.identity,
                timestamp=existing.timestamp,
                playback_date=existing.playback_date or play.playback_date,
                album=play.album,
                page=existing.page,
                track=existing.track or play.track,
            )
            self._plays[play.key] = replacement
            return replacement

        return existing

    def _store_observation(
        self,
        parsed: _ParsedTrack,
        observed_at: int,
        page: int | None,
        adjacent_timestamp: int | None,
    ) -> NowPlayingObservation:
        observation_key = (parsed.identity.key, observed_at)
        existing = self._observations.get(observation_key)
        if existing is not None:
            return existing

        observation = NowPlayingObservation(
            identity=parsed.identity,
            observed_at=observed_at,
            previous_timestamp=adjacent_timestamp,
            album=parsed.album,
            page=page,
            track=self._make_track(parsed.identity),
        )
        self._observations[observation_key] = observation
        return observation

    def _refresh_observations(self) -> None:
        plays = self.plays
        for observation in self._observations.values():
            latest_other = max(
                (
                    play.timestamp
                    for play in plays
                    if play.identity.key != observation.identity.key
                    and play.timestamp < observation.observed_at
                ),
                default=None,
            )
            bounds = [
                timestamp
                for timestamp in (observation.previous_timestamp, latest_other)
                if timestamp is not None
            ]
            lower = max(bounds) if bounds else None
            observation.previous_timestamp = lower
            observation.candidates = tuple(
                play
                for play in plays
                if play.identity.key == observation.identity.key
                and (lower is None or play.timestamp > lower)
                and play.timestamp <= observation.observed_at
            )

    def _make_track(self, identity: TrackIdentity):
        if self.network is None:
            return None

        from . import Track

        return Track(identity.artist, identity.title, self.network)

    @staticmethod
    def _to_timestamp(value: float | dt.datetime) -> int:
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt.UTC)
            return int(value.timestamp())
        return int(value)

    @staticmethod
    def _parse_xml(xml: Any) -> minidom.Document:
        if isinstance(xml, minidom.Document):
            return xml
        if hasattr(xml, "read"):
            xml = xml.read()
        if isinstance(xml, bytes):
            return minidom.parseString(xml)
        return minidom.parseString(str(xml).encode("utf-8"))

    @staticmethod
    def _page_metadata(doc: minidom.Document) -> dict[str, Any]:
        nodes = doc.getElementsByTagName("recenttracks")
        node = nodes[0] if nodes else None

        def attribute(name: str, *alternate_names: str, integer: bool = False):
            if node is None:
                return None
            for candidate in (name, *alternate_names):
                if not node.hasAttribute(candidate):
                    continue
                value = node.getAttribute(candidate)
                if not integer:
                    return value
                try:
                    return int(value)
                except ValueError:
                    return None
            return None

        return {
            "user": attribute("user"),
            "page": attribute("page", integer=True),
            "per_page": attribute("perPage", "perpage", integer=True),
            "total_pages": attribute("totalPages", "totalpages", integer=True),
            "total": attribute("total", integer=True),
        }

    @staticmethod
    def _parse_track(track_node: minidom.Element) -> _ParsedTrack:
        artist = _extract_text(track_node, "artist")
        title = _extract_text(track_node, "name")
        if artist is None or title is None:
            msg = "recent-tracks entry is missing an artist or track name"
            raise ValueError(msg)

        album = _extract_text(track_node, "album")
        now_playing = track_node.hasAttribute("nowplaying")

        date_nodes = track_node.getElementsByTagName("date")
        playback_date = _node_text(date_nodes[0]) if date_nodes else None
        timestamp = None
        if not now_playing:
            if not date_nodes or not date_nodes[0].hasAttribute("uts"):
                msg = f"recent-tracks entry for {artist!r} - {title!r} has no timestamp"
                raise ValueError(msg)
            try:
                timestamp = int(date_nodes[0].getAttribute("uts"))
            except ValueError as error:
                msg = (
                    f"recent-tracks entry for {artist!r} - {title!r} has an "
                    f"invalid timestamp: {date_nodes[0].getAttribute('uts')!r}"
                )
                raise ValueError(msg) from error

        return _ParsedTrack(
            identity=TrackIdentity(artist=artist, title=title),
            album=album,
            playback_date=playback_date,
            timestamp=timestamp,
            now_playing=now_playing,
        )


def _node_text(node: minidom.Element) -> str | None:
    if node.firstChild is None:
        return None
    data = getattr(node.firstChild, "data", None)
    if data is None:
        return None
    return html.unescape(data.strip())


def _extract_text(node: minidom.Element, tag_name: str) -> str | None:
    nodes = node.getElementsByTagName(tag_name)
    if not nodes:
        return None
    return _node_text(nodes[0])


def parse_recent_tracks_page(
    xml: str | bytes | minidom.Document,
    observed_at: float | dt.datetime | None = None,
) -> RecentTracksPage:
    """Parse one page without keeping it in an importer."""

    return RecentTracksImporter().add_page(xml, observed_at=observed_at)


def import_recent_tracks_pages(
    pages: Any,
    observed_at: float | dt.datetime | None = None,
    *,
    network=None,
) -> RecentTracksImporter:
    """Import several pages, each with its own optional collection time.

    ``pages`` may contain XML documents, ``(xml, observed_at)`` pairs, or
    paths to XML files.  A single ``observed_at`` may be supplied for pages
    that are not paired with their own value.
    """

    importer = RecentTracksImporter(network=network)
    for entry in pages:
        page_observed_at = observed_at
        if isinstance(entry, tuple) and len(entry) == 2:
            entry, page_observed_at = entry

        if isinstance(entry, os.PathLike):
            importer.add_file(entry, observed_at=page_observed_at)
        elif isinstance(entry, str) and not entry.lstrip().startswith("<"):
            importer.add_file(entry, observed_at=page_observed_at)
        else:
            importer.add_page(entry, observed_at=page_observed_at)
    return importer
