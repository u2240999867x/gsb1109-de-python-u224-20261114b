"""Compare Last.fm geo chart snapshots with provable rank semantics.

Ranks always come from the service (the ``rank`` attribute on each
``<track>`` element and the page metadata on ``<tracks>``), never from
the position of an element in the response.  Because of that:

* ties reported by the service (several tracks sharing one rank) are
  preserved instead of being silently renumbered;
* a track absent from a stored snapshot is never assigned a made-up
  rank such as "51" — only the bound ``rank >= deepest_stored + 1`` is
  provable, and only when the snapshot has no missing pages;
* if a page in the middle of a chart was never fetched, the entries
  before and after the gap still carry their true service ranks, and
  the gap itself can be traced back to the exact page numbers that are
  missing.
"""

from __future__ import annotations

import math
import typing
from dataclasses import dataclass, field, replace
from xml.dom import minidom

from . import _extract, _number, _Request


@dataclass(frozen=True)
class ChartEntry:
    """One chart row with the rank exactly as reported by the service."""

    artist: str
    title: str
    rank: int  # service-provided rank, never an array index
    listeners: int = 0
    page: int | None = None  # 1-based source page, for provenance
    tied: bool = False  # True when the service gave this rank to another entry

    @property
    def key(self) -> tuple[str, str]:
        return (self.artist.casefold(), self.title.casefold())

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "artist": self.artist,
            "title": self.title,
            "rank": self.rank,
            "listeners": self.listeners,
            "page": self.page,
        }

    @classmethod
    def from_dict(cls, data: dict[str, typing.Any]) -> ChartEntry:
        return cls(
            artist=data["artist"],
            title=data["title"],
            rank=int(data["rank"]),
            listeners=int(data.get("listeners") or 0),
            page=data.get("page"),
        )


@dataclass(frozen=True)
class PageInfo:
    """Metadata of one fetched results page, as reported by the service."""

    page: int
    per_page: int
    total_pages: int | None = None
    total: int | None = None

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "page": self.page,
            "per_page": self.per_page,
            "total_pages": self.total_pages,
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, data: dict[str, typing.Any]) -> PageInfo:
        return cls(
            page=int(data["page"]),
            per_page=int(data["per_page"]),
            total_pages=data.get("total_pages"),
            total=data.get("total"),
        )


@dataclass(frozen=True)
class RankGap:
    """A run of intended pages that was never fetched.

    ``rank_min``/``rank_max`` bracket the ranks that may be missing; either
    may be None when there is no fetched page on that side to anchor the
    bound.  ``missing_pages`` always names the exact pages to re-fetch.
    """

    missing_pages: tuple[int, ...]
    rank_min: int | None
    rank_max: int | None


@dataclass
class ChartSnapshot:
    """A stored chart: entries with service ranks plus page provenance."""

    label: str
    entries: list[ChartEntry]
    pages: dict[int, PageInfo]
    limit: int | None = None  # intended chart depth, e.g. 50 or 100
    per_page: int | None = None
    _by_key: dict[tuple[str, str], ChartEntry] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        counts: dict[int, int] = {}
        for entry in self.entries:
            counts[entry.rank] = counts.get(entry.rank, 0) + 1
        tied_ranks = {rank for rank, n in counts.items() if n > 1}
        self.entries = [
            replace(entry, tied=entry.rank in tied_ranks) for entry in self.entries
        ]
        if self.per_page is None and self.pages:
            self.per_page = next(iter(self.pages.values())).per_page
        self._by_key = {entry.key: entry for entry in self.entries}

    def get(self, artist: str, title: str) -> ChartEntry | None:
        return self._by_key.get((artist.casefold(), title.casefold()))

    def intended_pages(self) -> range:
        """Pages the snapshot was supposed to store, 1-based."""
        if self.limit is not None and self.per_page:
            return range(1, math.ceil(self.limit / self.per_page) + 1)
        if self.pages:
            return range(1, max(self.pages) + 1)
        return range(1, 1)

    def missing_pages(self) -> list[int]:
        """Intended pages that were never fetched (empty when complete)."""
        return [page for page in self.intended_pages() if page not in self.pages]

    def gaps(self) -> list[RankGap]:
        """Missing-page runs, each traceable to concrete page numbers."""
        gaps: list[RankGap] = []
        run: list[int] = []
        for page in self.intended_pages():
            if page in self.pages:
                if run:
                    gaps.append(self._make_gap(run))
                    run = []
            else:
                run.append(page)
        if run:
            gaps.append(self._make_gap(run))
        return gaps

    def _make_gap(self, run: list[int]) -> RankGap:
        before = [
            entry.rank
            for entry in self.entries
            if entry.page is not None and entry.page < run[0]
        ]
        after = [
            entry.rank
            for entry in self.entries
            if entry.page is not None and entry.page > run[-1]
        ]
        return RankGap(
            missing_pages=tuple(run),
            rank_min=max(before) + 1 if before else None,
            rank_max=min(after) - 1 if after else None,
        )

    def unseen_rank_bound(self) -> int | None:
        """Provable lower bound for the rank of any absent track.

        Returns None when absence proves nothing: the snapshot has missing
        pages within its intended range, or it stores no entries at all.
        Otherwise the bound is one past the deepest stored rank — or the
        deepest rank itself when that rank is tied, because a tie at the
        cut-off may continue beyond the stored rows.
        """
        if not self.entries or self.missing_pages():
            return None
        deepest = max(entry.rank for entry in self.entries)
        boundary_tied = any(
            entry.tied and entry.rank == deepest for entry in self.entries
        )
        return deepest if boundary_tied else deepest + 1

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "label": self.label,
            "limit": self.limit,
            "per_page": self.per_page,
            "pages": [info.to_dict() for _, info in sorted(self.pages.items())],
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, typing.Any]) -> ChartSnapshot:
        pages = {info.page: info for info in map(PageInfo.from_dict, data["pages"])}
        entries = [ChartEntry.from_dict(item) for item in data["entries"]]
        return cls(
            label=data["label"],
            entries=entries,
            pages=pages,
            limit=data.get("limit"),
            per_page=data.get("per_page"),
        )


@dataclass(frozen=True)
class ExactChange:
    """Both ranks are known, so the delta is provable."""

    artist: str
    title: str
    prev_rank: int
    curr_rank: int
    delta: int  # prev_rank - curr_rank; positive means the track climbed
    prev_tied: bool
    curr_tied: bool


@dataclass(frozen=True)
class BoundedChange:
    """One side was not stored; only a bound on the change is provable."""

    artist: str
    title: str
    kind: str  # "entered" (new in current) or "exited" (gone from current)
    known_rank: int
    unknown_rank_min: int  # lower bound for the rank on the unstored side
    delta_min: int | None  # entered: delta is provably at least this
    delta_max: int | None  # exited: delta is provably at most this


@dataclass(frozen=True)
class IndeterminateChange:
    """Seen on one side, but the other snapshot has gaps.

    Nothing about the unseen side is provable; ``reason`` names the
    missing pages so the gap can be traced back and re-fetched.
    """

    artist: str
    title: str
    seen_in: str  # "prev" or "curr"
    known_rank: int
    reason: str


@dataclass
class ChartDiff:
    """The popularity change table, split by what is provable."""

    exact: list[ExactChange]
    bounded: list[BoundedChange]
    indeterminate: list[IndeterminateChange]


def parse_geo_tracks_page(
    doc: minidom.Document,
) -> tuple[list[ChartEntry], PageInfo]:
    """Parse one geo.getTopTracks response page.

    Raises ValueError when a track carries no rank attribute — ranks must
    come from the service, never from element order in the response.
    """
    tracks_nodes = doc.getElementsByTagName("tracks")
    if not tracks_nodes:
        msg = "response contains no <tracks> element"
        raise ValueError(msg)
    tracks_node = tracks_nodes[0]

    def _attr(name: str) -> int | None:
        value = tracks_node.getAttribute(name)
        return int(value) if value else None

    page = _attr("page") or 1
    info = PageInfo(
        page=page,
        per_page=_attr("perPage") or 0,
        total_pages=_attr("totalPages"),
        total=_attr("total"),
    )

    entries = []
    for node in doc.getElementsByTagName("track"):
        rank = node.getAttribute("rank")
        if not rank:
            msg = "track element is missing the service-provided rank attribute"
            raise ValueError(msg)
        entries.append(
            ChartEntry(
                artist=_extract(node, "name", 1),
                title=_extract(node, "name"),
                rank=int(rank),
                listeners=int(_number(_extract(node, "listeners"))),
                page=page,
            )
        )
    return entries, info


def fetch_geo_chart_snapshot(
    network,
    country: str,
    *,
    location: str | None = None,
    limit: int = 100,
    per_page: int = 50,
    label: str | None = None,
    cacheable: bool = True,
) -> ChartSnapshot:
    """Fetch ``limit`` rows of a geo chart, one request per page.

    Every entry keeps the service-provided rank and the page it came from,
    so completeness can always be traced back to the original pages.
    """
    pages: dict[int, PageInfo] = {}
    entries: list[ChartEntry] = []
    for page in range(1, math.ceil(limit / per_page) + 1):
        params: dict[str, typing.Any] = {
            "country": country,
            "limit": per_page,
            "page": page,
        }
        if location:
            params["location"] = location
        doc = _Request(network, "geo.getTopTracks", params).execute(cacheable)
        page_entries, info = parse_geo_tracks_page(doc)
        pages[info.page] = info
        entries.extend(page_entries)
    return ChartSnapshot(
        label=label or f"{country} top {limit}",
        entries=entries,
        pages=pages,
        limit=limit,
        per_page=per_page,
    )


def _gap_reason(snapshot: ChartSnapshot) -> str:
    missing = ", ".join(str(page) for page in snapshot.missing_pages())
    return f"{snapshot.label} is missing page(s) {missing}; absence proves nothing"


def diff_snapshots(prev: ChartSnapshot, curr: ChartSnapshot) -> ChartDiff:
    """Build the popularity change table for two stored chart snapshots.

    Tracks present in both snapshots yield ExactChange rows.  Tracks seen
    on only one side yield BoundedChange rows when the other snapshot is
    complete enough to prove a bound, or IndeterminateChange rows when
    that snapshot has missing pages.
    """
    exact: list[ExactChange] = []
    bounded: list[BoundedChange] = []
    indeterminate: list[IndeterminateChange] = []

    prev_bound = prev.unseen_rank_bound()
    curr_bound = curr.unseen_rank_bound()

    keys = prev._by_key.keys() | curr._by_key.keys()
    for key in keys:
        before = prev._by_key.get(key)
        after = curr._by_key.get(key)

        if before and after:
            exact.append(
                ExactChange(
                    artist=after.artist,
                    title=after.title,
                    prev_rank=before.rank,
                    curr_rank=after.rank,
                    delta=before.rank - after.rank,
                    prev_tied=before.tied,
                    curr_tied=after.tied,
                )
            )
        elif after is not None:
            if prev_bound is None:
                indeterminate.append(
                    IndeterminateChange(
                        artist=after.artist,
                        title=after.title,
                        seen_in="curr",
                        known_rank=after.rank,
                        reason=_gap_reason(prev),
                    )
                )
            else:
                bounded.append(
                    BoundedChange(
                        artist=after.artist,
                        title=after.title,
                        kind="entered",
                        known_rank=after.rank,
                        unknown_rank_min=prev_bound,
                        delta_min=prev_bound - after.rank,
                        delta_max=None,
                    )
                )
        elif before is not None:
            if curr_bound is None:
                indeterminate.append(
                    IndeterminateChange(
                        artist=before.artist,
                        title=before.title,
                        seen_in="prev",
                        known_rank=before.rank,
                        reason=_gap_reason(curr),
                    )
                )
            else:
                bounded.append(
                    BoundedChange(
                        artist=before.artist,
                        title=before.title,
                        kind="exited",
                        known_rank=before.rank,
                        unknown_rank_min=curr_bound,
                        delta_min=None,
                        delta_max=before.rank - curr_bound,
                    )
                )

    exact.sort(key=lambda change: (change.curr_rank, change.prev_rank))
    bounded.sort(key=lambda change: change.known_rank)
    indeterminate.sort(key=lambda change: change.known_rank)
    return ChartDiff(exact=exact, bounded=bounded, indeterminate=indeterminate)
