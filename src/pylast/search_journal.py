"""Durable, branch-aware records for paginated searches.

A search is not a single query.  The service can interpret page 2 with a
different normalised query than page 1, and a partially collected search may be
handed to a later process.  This module records the original input, the query
the service actually interpreted on every page, the response order and ranks,
and the raw response body in an append-only journal.

Three concepts keep pagination safe:

``filter_key``
    Identifies the original filters.  Page pointers are scoped to this key, so
    two different filter sets that normalise to the same words never advance
    one another.
``origin``
    Distinguishes pages fetched for the original query from pages fetched after
    following a correction suggestion.  Each origin has its own page pointer,
    so following a suggestion never consumes a page of the original search.
``branch``
    Records the query the service actually used for the page.  When a page is
    interpreted differently, it is filed under a new branch while the page
    pointer of the sequence continues from the highest recorded page.

Pointers are always derived as ``max(recorded page) + 1``, never incremented
before a request.  Recording a page is idempotent, so retrying a page that is
already stored cannot skip the following page.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Self

ORIGIN_ORIGINAL = "original"
ORIGIN_CORRECTION = "correction"
ORIGINS = (ORIGIN_ORIGINAL, ORIGIN_CORRECTION)

_JOURNAL_TYPE = "search_page"
_JOURNAL_VERSION = 1


class SearchJournalError(Exception):
    """Raised when a search journal cannot be read or written."""


@dataclass(frozen=True)
class SearchQuery:
    """One query interpretation returned by the search service."""

    role: str = "request"
    search_terms: str | None = None
    start_page: int | None = None

    @property
    def normalized_role(self) -> str:
        return self.role.strip().casefold() or "request"

    @property
    def is_correction(self) -> bool:
        """Whether this interpretation is a correction suggestion."""

        role = self.normalized_role
        return "correct" in role or "suggest" in role

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "search_terms": self.search_terms,
            "start_page": self.start_page,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SearchQuery:
        start_page = value.get("start_page")
        return cls(
            role=str(value.get("role") or "request"),
            search_terms=_optional_text(value.get("search_terms")),
            start_page=start_page if isinstance(start_page, int) else None,
        )


@dataclass(frozen=True)
class SearchResult:
    """A result together with its original response ranking."""

    rank: int
    position: int
    name: str | None = None
    artist: str | None = None
    url: str | None = None
    mbid: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "position": self.position,
            "name": self.name,
            "artist": self.artist,
            "url": self.url,
            "mbid": self.mbid,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SearchResult:
        return cls(
            rank=_int_or_zero(value.get("rank")),
            position=_int_or_zero(value.get("position")),
            name=_optional_text(value.get("name")),
            artist=_optional_text(value.get("artist")),
            url=_optional_text(value.get("url")),
            mbid=_optional_text(value.get("mbid")),
        )


@dataclass(frozen=True)
class SearchPage:
    """Everything needed to re-inspect one page of a search."""

    kind: str
    filters: dict[str, str]
    filter_key: str
    sequence_key: str
    branch_id: str
    origin: str
    parent_id: str | None
    page: int
    query: SearchQuery
    queries: tuple[SearchQuery, ...]
    results: tuple[SearchResult, ...]
    raw_response: str
    request_params: dict[str, str]
    requested_terms: str | None = None
    total_results: int | None = None
    items_per_page: int | None = None
    start_index: int | None = None
    recorded_at: float = 0.0

    @property
    def normalized_query(self) -> str | None:
        """The query terms the service actually used for this page."""

        return self.query.search_terms

    @property
    def query_changed(self) -> bool:
        """Whether the service interpreted the page with different words."""

        if self.normalized_query is None or self.requested_terms is None:
            return False
        return _term_key(self.normalized_query) != _term_key(self.requested_terms)

    @property
    def is_correction(self) -> bool:
        return self.origin == ORIGIN_CORRECTION

    @property
    def corrections(self) -> tuple[SearchQuery, ...]:
        """Correction suggestions carried by this response."""

        return tuple(query for query in self.queries if query.is_correction)

    @property
    def ranks(self) -> tuple[int, ...]:
        return tuple(result.rank for result in self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": _JOURNAL_VERSION,
            "type": _JOURNAL_TYPE,
            "kind": self.kind,
            "filters": dict(self.filters),
            "filter_key": self.filter_key,
            "sequence_key": self.sequence_key,
            "branch_id": self.branch_id,
            "origin": self.origin,
            "parent_id": self.parent_id,
            "page": self.page,
            "query": self.query.to_dict(),
            "queries": [query.to_dict() for query in self.queries],
            "results": [result.to_dict() for result in self.results],
            "raw_response": self.raw_response,
            "request_params": dict(self.request_params),
            "requested_terms": self.requested_terms,
            "total_results": self.total_results,
            "items_per_page": self.items_per_page,
            "start_index": self.start_index,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SearchPage:
        queries = tuple(
            SearchQuery.from_dict(item)
            for item in _mapping_sequence(value.get("queries"))
        )
        results = tuple(
            SearchResult.from_dict(item)
            for item in _mapping_sequence(value.get("results"))
        )
        raw_query = value.get("query")
        query = (
            SearchQuery.from_dict(raw_query)
            if isinstance(raw_query, Mapping)
            else SearchQuery()
        )
        if not queries:
            queries = (query,)
        origin = str(value.get("origin") or ORIGIN_ORIGINAL)
        if origin not in ORIGINS:
            origin = ORIGIN_ORIGINAL
        return cls(
            kind=str(value.get("kind") or ""),
            filters=_string_mapping(value.get("filters")),
            filter_key=str(value.get("filter_key") or ""),
            sequence_key=str(value.get("sequence_key") or ""),
            branch_id=str(value.get("branch_id") or ""),
            origin=origin,
            parent_id=_optional_text(value.get("parent_id")),
            page=_int_or_zero(value.get("page")),
            query=query,
            queries=queries,
            results=results,
            raw_response=str(value.get("raw_response") or ""),
            request_params=_string_mapping(value.get("request_params")),
            requested_terms=_optional_text(value.get("requested_terms")),
            total_results=_optional_int(value.get("total_results")),
            items_per_page=_optional_int(value.get("items_per_page")),
            start_index=_optional_int(value.get("start_index")),
            recorded_at=_optional_float(value.get("recorded_at")) or 0.0,
        )

    def summary(self) -> dict[str, object]:
        return {
            "page": self.page,
            "origin": self.origin,
            "source": (
                "correction_suggestion"
                if self.origin == ORIGIN_CORRECTION
                else "original_query"
            ),
            "role": self.query.role,
            "search_terms": self.normalized_query,
            "requested_terms": self.requested_terms,
            "query_changed": self.query_changed,
            "ranks": list(self.ranks),
            "result_count": len(self.results),
        }


@dataclass
class SearchBranch:
    """Pages that share one origin and one actual query interpretation."""

    branch_id: str
    kind: str
    filter_key: str
    sequence_key: str
    filters: dict[str, str]
    query: SearchQuery
    origin: str
    parent_id: str | None
    pages: list[SearchPage] = field(default_factory=list)

    @property
    def page_numbers(self) -> list[int]:
        return sorted(page.page for page in self.pages)

    @property
    def next_page(self) -> int:
        return max(self.page_numbers, default=0) + 1

    def to_dict(self) -> dict[str, object]:
        return {
            "branch_id": self.branch_id,
            "origin": self.origin,
            "parent_id": self.parent_id,
            "role": self.query.role,
            "search_terms": self.query.search_terms,
            "pages": self.page_numbers,
            "next_page": self.next_page,
        }


@dataclass
class _Suggestion:
    """Accumulates the pages where a correction suggestion was seen."""

    role: str
    search_terms: str | None
    seen_on_pages: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class SearchSequence:
    """The pagination pointer of one filter set, origin and request terms."""

    kind: str
    filters: dict[str, str]
    filter_key: str
    origin: str
    requested_terms: str | None
    sequence_key: str
    pages: tuple[SearchPage, ...]

    @property
    def next_page(self) -> int:
        return max((page.page for page in self.pages), default=0) + 1

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "requested_terms": self.requested_terms,
            "pages": sorted(page.page for page in self.pages),
            "next_page": self.next_page,
        }


@dataclass(frozen=True)
class SearchRecovery:
    """The resumable state and provenance of one original filter set."""

    kind: str
    filters: dict[str, str]
    filter_key: str
    sequences: tuple[SearchSequence, ...]
    branches: tuple[SearchBranch, ...]
    pages: tuple[SearchPage, ...]

    @property
    def next_page(self) -> int:
        """The next page of the original query."""

        for sequence in self.sequences:
            if sequence.origin == ORIGIN_ORIGINAL:
                return sequence.next_page
        return 1

    @property
    def original_pages(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    page.page
                    for page in self.pages
                    if page.origin == ORIGIN_ORIGINAL
                }
            )
        )

    @property
    def correction_pages(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    page.page
                    for page in self.pages
                    if page.origin == ORIGIN_CORRECTION
                }
            )
        )

    @property
    def original_query(self) -> str | None:
        for page in self.pages:
            if page.origin == ORIGIN_ORIGINAL:
                return page.query.search_terms
        return None

    @property
    def original_requested_terms(self) -> str | None:
        for sequence in self.sequences:
            if sequence.origin == ORIGIN_ORIGINAL:
                return sequence.requested_terms
        return None

    def explain(self) -> dict[str, object]:
        """Describe which pages belong to the original query or a correction."""

        original_branches = [
            branch for branch in self.branches if branch.origin == ORIGIN_ORIGINAL
        ]
        correction_branches = [
            branch for branch in self.branches if branch.origin == ORIGIN_CORRECTION
        ]

        suggestions: dict[tuple[str, str], _Suggestion] = {}
        for page in self.pages:
            for query in page.corrections:
                key = (query.normalized_role, _term_key(query.search_terms) or "")
                entry = suggestions.setdefault(
                    key,
                    _Suggestion(
                        role=query.role,
                        search_terms=query.search_terms,
                    ),
                )
                entry.seen_on_pages.add(page.page)

        applied: dict[str, list[str]] = {}
        for branch in correction_branches:
            terms_key = _term_key(branch.query.search_terms)
            if terms_key:
                applied.setdefault(terms_key, []).append(branch.branch_id)

        suggestion_list = []
        for (_, terms), entry in suggestions.items():
            branches = applied.get(terms, [])
            suggestion_list.append(
                {
                    "role": entry.role,
                    "search_terms": entry.search_terms,
                    "seen_on_pages": sorted(entry.seen_on_pages),
                    "applied": bool(branches),
                    "applied_branches": branches,
                }
            )

        return {
            "kind": self.kind,
            "original_input": dict(self.filters),
            "filter_key": self.filter_key,
            "original_query": {
                "requested_terms": self.original_requested_terms,
                "search_terms": self.original_query,
                "pages": list(self.original_pages),
                "branches": [branch.to_dict() for branch in original_branches],
            },
            "correction_branches": [
                branch.to_dict() for branch in correction_branches
            ],
            "suggested_corrections": suggestion_list,
            "sequences": [sequence.to_dict() for sequence in self.sequences],
            "next_page": self.next_page,
            "pages": [page.summary() for page in self.pages],
        }

    def describe(self) -> str:
        return json.dumps(self.explain(), ensure_ascii=False, indent=2)


class SearchJournal:
    """An append-only collection of search pages that can be resumed.

    Passing ``path`` makes the journal durable.  A journal without a path still
    records page provenance and ranks for the lifetime of its searches.  One
    journal can be shared by several searches; records are keyed by the
    original filters and never merge pages from different filter sets.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._pages: list[SearchPage] = []
        self._pages_by_filter: dict[str, list[SearchPage]] = {}
        self._pages_by_key: dict[tuple[str, int], SearchPage] = {}
        self._branches: dict[str, SearchBranch] = {}
        self._branch_order: list[str] = []
        self._filters: dict[str, dict[str, str]] = {}
        self._kinds: dict[str, str] = {}
        self._sequence_pages: dict[str, list[SearchPage]] = {}
        self._sequence_order: list[str] = []

        if self.path is not None:
            self._load()

    @classmethod
    def open(cls, path: str | os.PathLike[str] | None = None) -> SearchJournal:
        return cls(path)

    def __len__(self) -> int:
        return len(self._pages)

    def __repr__(self) -> str:
        return f"SearchJournal(path={str(self.path)!r}, pages={len(self._pages)})"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def pages(self) -> tuple[SearchPage, ...]:
        return tuple(self._pages)

    @property
    def branches(self) -> tuple[SearchBranch, ...]:
        return tuple(self._branches[branch_id] for branch_id in self._branch_order)

    def sequence_key(
        self,
        kind: str,
        filters: Mapping[str, object],
        origin: str = ORIGIN_ORIGINAL,
        requested_terms: str | None = None,
    ) -> str:
        return _sequence_key(
            _filter_key(kind, filters), origin, requested_terms
        )

    def next_page(
        self,
        kind: str,
        filters: Mapping[str, object],
        origin: str = ORIGIN_ORIGINAL,
        requested_terms: str | None = None,
    ) -> int:
        """Return the first page after the stored pages of this sequence."""

        pages = self._sequence_pages.get(
            self.sequence_key(kind, filters, origin, requested_terms), ()
        )
        return max((page.page for page in pages), default=0) + 1

    def pages_for(
        self,
        kind: str,
        filters: Mapping[str, object],
        origin: str | None = None,
        requested_terms: str | None = None,
        branch_id: str | None = None,
    ) -> tuple[SearchPage, ...]:
        pages = list(self._pages_by_filter.get(_filter_key(kind, filters), ()))
        if origin is not None:
            pages = [page for page in pages if page.origin == origin]
        if requested_terms is not None:
            terms_key = _term_key(requested_terms)
            pages = [
                page
                for page in pages
                if _term_key(page.requested_terms) == terms_key
            ]
        if branch_id is not None:
            pages = [page for page in pages if page.branch_id == branch_id]
        return tuple(pages)

    def branches_for(
        self,
        kind: str,
        filters: Mapping[str, object],
        origin: str | None = None,
    ) -> tuple[SearchBranch, ...]:
        filter_key = _filter_key(kind, filters)
        branches = [
            branch for branch in self.branches if branch.filter_key == filter_key
        ]
        if origin is not None:
            branches = [branch for branch in branches if branch.origin == origin]
        return tuple(branches)

    def recovery(
        self, kind: str, filters: Mapping[str, object]
    ) -> SearchRecovery:
        filter_key = _filter_key(kind, filters)
        pages = tuple(self._pages_by_filter.get(filter_key, ()))
        branches = tuple(
            branch for branch in self.branches if branch.filter_key == filter_key
        )
        sequences = tuple(
            self._sequence(key, self._sequence_pages[key])
            for key in self._sequence_order
            if self._sequence_pages[key]
            and self._sequence_pages[key][0].filter_key == filter_key
        )
        return SearchRecovery(
            kind=kind,
            filters=dict(self._filters.get(filter_key, _string_mapping(filters))),
            filter_key=filter_key,
            sequences=sequences,
            branches=branches,
            pages=pages,
        )

    def explain(self, kind: str, filters: Mapping[str, object]) -> dict[str, object]:
        return self.recovery(kind, filters).explain()

    def raw_response(
        self,
        kind: str,
        filters: Mapping[str, object],
        page: int,
        origin: str = ORIGIN_ORIGINAL,
        requested_terms: str | None = None,
    ) -> str | None:
        for recorded in self.pages_for(
            kind, filters, origin, requested_terms
        ):
            if recorded.page == page:
                return recorded.raw_response
        return None

    def page(
        self,
        kind: str,
        filters: Mapping[str, object],
        page: int,
        origin: str = ORIGIN_ORIGINAL,
        requested_terms: str | None = None,
    ) -> SearchPage | None:
        for recorded in self.pages_for(
            kind, filters, origin, requested_terms
        ):
            if recorded.page == page:
                return recorded
        return None

    def record_page(
        self,
        *,
        kind: str,
        filters: Mapping[str, object],
        page: int,
        query: SearchQuery | None = None,
        queries: Sequence[SearchQuery] = (),
        results: Sequence[SearchResult] = (),
        request_params: Mapping[str, object] | None = None,
        raw_response: str = "",
        origin: str = ORIGIN_ORIGINAL,
        requested_terms: str | None = None,
        total_results: int | None = None,
        items_per_page: int | None = None,
        start_index: int | None = None,
        recorded_at: float | None = None,
    ) -> SearchPage:
        """Record a page, returning the existing record on a retry.

        The record is written to disk before the in-memory indexes change, so a
        failed write leaves the sequence pointer on the same page.  Recording
        the same page twice returns the first record and changes no pointer.
        """

        if origin not in ORIGINS:
            msg = f"Unknown search origin: {origin!r}"
            raise ValueError(msg)
        if page < 1:
            msg = "Search pages are numbered from 1"
            raise ValueError(msg)

        string_filters = _string_mapping(filters)
        filter_key = _filter_key(kind, string_filters)
        terms = requested_terms or _requested_terms(string_filters)
        effective_query = query or SearchQuery(
            role="request", search_terms=terms, start_page=page
        )
        query_list = tuple(queries)
        if effective_query not in query_list:
            query_list = (effective_query, *query_list)
        sequence_key = _sequence_key(filter_key, origin, terms)

        with self._lock:
            branch = self._branch_for(
                kind,
                string_filters,
                filter_key,
                sequence_key,
                effective_query,
                origin,
            )
            key = (branch.branch_id, page)
            if existing := self._pages_by_key.get(key):
                return existing

            page_record = SearchPage(
                kind=kind,
                filters=string_filters,
                filter_key=filter_key,
                sequence_key=sequence_key,
                branch_id=branch.branch_id,
                origin=origin,
                parent_id=branch.parent_id,
                page=page,
                query=effective_query,
                queries=query_list,
                results=tuple(results),
                raw_response=raw_response,
                request_params=_string_mapping(request_params or {}),
                requested_terms=terms,
                total_results=total_results,
                items_per_page=items_per_page,
                start_index=start_index,
                recorded_at=recorded_at if recorded_at is not None else time.time(),
            )
            self._append(page_record)
            self._register(page_record)
            return page_record

    def merge(self, other: SearchJournal) -> None:
        """Copy unrecorded pages from another journal.

        Pages already present are left untouched, so merging a temporary
        journal into a newly opened durable one cannot rewind a pointer.
        """

        for page in other.pages:
            with self._lock:
                if (page.branch_id, page.page) in self._pages_by_key:
                    continue
                self._append(page)
                self._register(page)

    def close(self) -> None:
        """Release the journal.

        Records are flushed and fsynced on every append, so there is no open
        handle.  The method allows use as a context manager.
        """

    def _branch_for(
        self,
        kind: str,
        filters: dict[str, str],
        filter_key: str,
        sequence_key: str,
        query: SearchQuery,
        origin: str,
    ) -> SearchBranch:
        branch_id = _branch_id(sequence_key, query)
        if existing := self._branches.get(branch_id):
            return existing

        sequence_pages = self._sequence_pages.get(sequence_key)
        parent_id = (
            sequence_pages[-1].branch_id if sequence_pages else None
        )
        if parent_id is None and origin == ORIGIN_CORRECTION:
            # A correction branch descends from the original query branch it
            # is meant to refine, even when that branch recorded no page yet.
            for existing_branch in self._branches.values():
                if (
                    existing_branch.filter_key == filter_key
                    and existing_branch.origin == ORIGIN_ORIGINAL
                ):
                    parent_id = existing_branch.branch_id
        branch = SearchBranch(
            branch_id=branch_id,
            kind=kind,
            filter_key=filter_key,
            sequence_key=sequence_key,
            filters=filters,
            query=query,
            origin=origin,
            parent_id=parent_id,
        )
        self._branches[branch_id] = branch
        self._branch_order.append(branch_id)
        return branch

    def _register(self, page: SearchPage) -> None:
        key = (page.branch_id, page.page)
        if key in self._pages_by_key:
            return
        self._pages.append(page)
        self._pages_by_key[key] = page
        self._pages_by_filter.setdefault(page.filter_key, []).append(page)
        self._filters.setdefault(page.filter_key, dict(page.filters))
        self._kinds.setdefault(page.filter_key, page.kind)

        branch = self._branches.get(page.branch_id)
        if branch is None:
            branch = SearchBranch(
                branch_id=page.branch_id,
                kind=page.kind,
                filter_key=page.filter_key,
                sequence_key=page.sequence_key,
                filters=dict(page.filters),
                query=page.query,
                origin=page.origin,
                parent_id=page.parent_id,
            )
            self._branches[page.branch_id] = branch
            self._branch_order.append(page.branch_id)
        if not any(existing.page == page.page for existing in branch.pages):
            branch.pages.append(page)

        sequence_pages = self._sequence_pages.setdefault(page.sequence_key, [])
        sequence_pages.append(page)
        if page.sequence_key not in self._sequence_order:
            self._sequence_order.append(page.sequence_key)

    def _sequence(
        self, sequence_key: str, pages: Sequence[SearchPage]
    ) -> SearchSequence:
        first = pages[0] if pages else None
        if first is None:
            raise KeyError(sequence_key)
        return SearchSequence(
            kind=first.kind,
            filters=dict(first.filters),
            filter_key=first.filter_key,
            origin=first.origin,
            requested_terms=first.requested_terms,
            sequence_key=sequence_key,
            pages=tuple(pages),
        )

    def _append(self, page: SearchPage) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(page.to_dict(), ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        try:
            prefix = b""
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("rb") as reader:
                    reader.seek(-1, os.SEEK_END)
                    if reader.read(1) != b"\n":
                        prefix = b"\n"
            with self.path.open("ab") as stream:
                if prefix:
                    stream.write(prefix)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            msg = f"Unable to write search journal {self.path}: {error}"
            raise SearchJournalError(msg) from error

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        if not self.path.is_file():
            msg = f"Search journal is not a file: {self.path}"
            raise SearchJournalError(msg)

        raw = self.path.read_bytes()
        offset = 0
        for raw_line in raw.splitlines(keepends=True):
            line_offset = offset
            offset += len(raw_line)
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                if offset >= len(raw) and not raw_line.endswith(b"\n"):
                    # A torn final line is what an interrupted append leaves
                    # behind; drop it so the journal can be resumed.
                    self._truncate(line_offset)
                    break
                message = (
                    f"Invalid search journal record at byte {line_offset}"
                )
                raise SearchJournalError(message) from error
            if not isinstance(record, Mapping):
                message = f"Invalid search journal record at byte {line_offset}"
                raise SearchJournalError(message)
            if record.get("type") != _JOURNAL_TYPE:
                message = (
                    f"Unsupported search journal record at byte {line_offset}"
                )
                raise SearchJournalError(message)
            version = record.get("version")
            if version not in (None, _JOURNAL_VERSION):
                message = f"Unsupported search journal version {version!r}"
                raise SearchJournalError(message)
            self._register(SearchPage.from_dict(record))

    def _truncate(self, size: int) -> None:
        if self.path is None:
            return
        with self.path.open("r+b") as stream:
            stream.truncate(size)


def _filter_key(kind: str, filters: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {"kind": kind, "filters": _string_mapping(filters)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _sequence_key(
    filter_key: str, origin: str, requested_terms: str | None
) -> str:
    canonical = json.dumps(
        {"filter": filter_key, "origin": origin, "terms": _term_key(requested_terms)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _branch_id(sequence_key: str, query: SearchQuery) -> str:
    canonical = json.dumps(
        {
            "sequence": sequence_key,
            "role": query.normalized_role,
            "search_terms": _term_key(query.search_terms),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _requested_terms(filters: Mapping[str, str]) -> str | None:
    for key in ("track", "album", "artist"):
        if key in filters:
            return filters[key]
    return next(iter(filters.values()), None)


def _term_key(terms: str | None) -> str | None:
    if terms is None:
        return None
    normalized = " ".join(terms.split()).casefold()
    return normalized or None


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _int_or_zero(value: object) -> int:
    return _optional_int(value) or 0
