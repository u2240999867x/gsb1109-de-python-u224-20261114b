"""
Budgeted crawling of Last.fm-style similar-artist relationships.

The crawler is intentionally independent of the network implementation.  Pass a
``network`` to use :meth:`pylast.Artist.get_similar`, or pass a callable (or a
mapping of canned responses) to use local data.

A crawl is directed: an edge ``A -> B`` means that A's similar-artist list
contains B.  It does not imply ``B -> A``.  Every distinct target name records
the number of distinct sources that named it, and every node records both its
direct parents and the original seeds from which it was reached.

The request budget counts *attempts*, including retries.  A shared neighbour is
fetched at most once.  When the budget runs out, nodes that could not be fetched
remain :attr:`NodeState.UNVISITED`, while fetched nodes distinguish
:attr:`NodeState.EMPTY` (the API returned no neighbours) and
:attr:`NodeState.TRUNCATED` (the returned list reached the requested limit).
"""

from __future__ import annotations

import inspect
import json
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class NodeState(StrEnum):
    """State of an artist in the crawled graph.

    ``NO_NEIGHBORS`` and ``NO_NEIGHBOURS`` are aliases of ``EMPTY``;
    ``NOT_VISITED`` is an alias of ``UNVISITED``.
    """

    UNVISITED = "unvisited"
    COMPLETE = "complete"
    EMPTY = "empty"
    TRUNCATED = "truncated"
    BOUNDARY = "boundary"
    FAILED = "failed"

    NOT_VISITED = "unvisited"
    NO_NEIGHBORS = "empty"
    NO_NEIGHBOURS = "empty"


CrawlState = NodeState
ExpansionState = NodeState


@dataclass(frozen=True)
class SimilarityCandidate:
    """One name returned by a similar-artist lookup."""

    name: str
    match: float = 0.0


@dataclass
class SimilarityEdge:
    """A directed ``source -> target`` similarity relationship."""

    source: str
    target: str
    match: float = 0.0
    depth: int = 1
    named_count: int = 0
    seeds: tuple[str, ...] = ()

    @property
    def direction(self) -> tuple[str, str]:
        return self.source, self.target

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "match": self.match,
            "depth": self.depth,
            "named_count": self.named_count,
            "seeds": list(self.seeds),
        }


@dataclass
class SimilarityNode:
    """An artist discovered during a crawl."""

    name: str
    depth: int
    state: NodeState = NodeState.UNVISITED
    parents: list[str] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)
    matches: dict[str, float] = field(default_factory=dict)
    attempts: int = 0
    error: str | None = None

    @property
    def status(self) -> NodeState:
        """Alias for :attr:`state`."""
        return self.state

    @property
    def named_count(self) -> int:
        """Number of distinct sources that named this artist."""
        return len(self.parents)

    @property
    def mention_count(self) -> int:
        """Alias for :attr:`named_count`."""
        return self.named_count

    @property
    def seed_count(self) -> int:
        return len(self.seeds)

    @property
    def discovered_by(self) -> tuple[str, ...]:
        """The original seeds from which this node was reached."""
        return tuple(self.seeds)

    @property
    def reached_by_seeds(self) -> tuple[str, ...]:
        """Alias for :attr:`discovered_by`."""
        return tuple(self.seeds)

    @property
    def visited(self) -> bool:
        return self.state in {
            NodeState.COMPLETE,
            NodeState.EMPTY,
            NodeState.TRUNCATED,
        }

    @property
    def truncated(self) -> bool:
        return self.state is NodeState.TRUNCATED

    @property
    def has_neighbors(self) -> bool:
        return self.state in {NodeState.COMPLETE, NodeState.TRUNCATED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "depth": self.depth,
            "state": self.state.value,
            "parents": list(self.parents),
            "seeds": list(self.seeds),
            "named_count": self.named_count,
            "matches": dict(self.matches),
            "attempts": self.attempts,
            "error": self.error,
        }


@dataclass(frozen=True)
class RequestRecord:
    """One API attempt, in the order in which budget was claimed."""

    artist: str
    depth: int
    attempt: int
    outcome: str
    remaining_budget: int | None
    error: str | None = None
    item_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artist": self.artist,
            "depth": self.depth,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "remaining_budget": self.remaining_budget,
            "error": self.error,
            "item_count": self.item_count,
        }


@dataclass(frozen=True)
class FetchFailure:
    artist: str
    depth: int
    attempts: int
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artist": self.artist,
            "depth": self.depth,
            "attempts": self.attempts,
            "error": self.error,
        }


@dataclass(frozen=True)
class SimilarityResponse:
    """A response envelope for a local/replay source.

    ``truncated`` can be supplied when the source knows whether more results
    existed.  Otherwise a response containing exactly ``limit`` items is
    treated as truncated.
    """

    items: Sequence[Any] = ()
    truncated: bool | None = None
    total: int | None = None


@dataclass(frozen=True)
class ReplayScript:
    """A sequence of outcomes for successive attempts against one artist."""

    outcomes: Sequence[Any]
    repeat_last: bool = True

    def outcome_for(self, attempt: int) -> Any:
        if not self.outcomes:
            return []
        index = min(attempt - 1, len(self.outcomes) - 1)
        if attempt > len(self.outcomes) and not self.repeat_last:
            msg = "Replay script exhausted"
            raise RuntimeError(msg)
        return self.outcomes[index]


@dataclass(frozen=True)
class ReplayCall:
    artist: str
    limit: int | None
    attempt: int


class ReplaySimilaritySource:
    """A deterministic, in-memory stand-in for the similar-artist endpoint.

    Values in ``responses`` may be:

    * a sequence of names / candidates, or a :class:`SimilarityResponse`
    * an exception instance, which is raised on every attempt
    * a :class:`ReplayScript`, to script retries
    * a callable accepting ``(artist, limit, attempt)``

    An artist missing from ``responses`` returns an empty list by default,
    which makes it practical to script a small graph and stop at unknown nodes.
    Pass ``has_default=False`` to raise :class:`LookupError` for unknown artists
    instead.

    ``calls`` records every attempt, making the budget claim order auditable.
    """

    def __init__(
        self,
        responses: Mapping[str, Any] | None = None,
        *,
        default: Any = (),
        has_default: bool = True,
    ) -> None:
        self.responses = {
            _name_key(name): value for name, value in (responses or {}).items()
        }
        self.default = default
        self.has_default = has_default
        self.calls: list[ReplayCall] = []
        self._attempts: Counter[str] = Counter()

    def __call__(self, artist: str, limit: int | None = None) -> Any:
        key = _name_key(artist)
        attempt = self._attempts[key] + 1
        self._attempts[key] = attempt
        self.calls.append(ReplayCall(str(artist), limit, attempt))

        if key in self.responses:
            value = self.responses[key]
        elif self.has_default:
            value = self.default
        else:
            msg = f"No replay response for artist {artist!r}"
            raise LookupError(msg)

        if isinstance(value, ReplayScript):
            value = value.outcome_for(attempt)
        elif callable(value) and not isinstance(value, (str, bytes)):
            value = value(artist, limit, attempt)

        if isinstance(value, BaseException):
            raise value
        return value

    @property
    def requested(self) -> tuple[str, ...]:
        """Artists in the order their budget was claimed (including retries)."""
        return tuple(call.artist for call in self.calls)

    @property
    def claim_order(self) -> tuple[str, ...]:
        """Alias for :attr:`requested`."""
        return self.requested

    def attempts_for(self, artist: str) -> int:
        return self._attempts[_name_key(artist)]


LocalSimilaritySource = ReplaySimilaritySource
ReplayResponses = ReplaySimilaritySource


class SimilarityCrawl:
    """The result of a :func:`crawl_similar_artists` call."""

    def __init__(
        self,
        *,
        seeds: Sequence[str],
        max_depth: int,
        budget: int | None,
        budget_used: int,
        nodes: dict[str, SimilarityNode],
        node_order: Sequence[str],
        edges: Sequence[SimilarityEdge],
        request_log: Sequence[RequestRecord],
        failures: Sequence[FetchFailure],
    ) -> None:
        self.seeds = tuple(seeds)
        self.max_depth = max_depth
        self.budget = budget
        self.budget_used = budget_used
        self.nodes = {nodes[key].name: nodes[key] for key in node_order}
        self._node_index = dict(nodes)
        self._node_order = list(node_order)
        self.edges = list(edges)
        self.request_log = list(request_log)
        self.failures = list(failures)

    @property
    def remaining_budget(self) -> int | None:
        if self.budget is None:
            return None
        return max(self.budget - self.budget_used, 0)

    @property
    def budget_exhausted(self) -> bool:
        return self.budget is not None and self.budget_used >= self.budget

    def _nodes_in_state(self, *states: NodeState) -> tuple[SimilarityNode, ...]:
        wanted = set(states)
        return tuple(
            self._node_index[key]
            for key in self._node_order
            if self._node_index[key].state in wanted
        )

    @property
    def visited(self) -> tuple[SimilarityNode, ...]:
        return self._nodes_in_state(
            NodeState.COMPLETE, NodeState.EMPTY, NodeState.TRUNCATED
        )

    @property
    def all(self) -> tuple[SimilarityNode, ...]:
        """All discovered nodes, in deterministic discovery order."""
        return tuple(self._node_index[key] for key in self._node_order)

    @property
    def unvisited(self) -> tuple[SimilarityNode, ...]:
        """Nodes whose lookup was eligible but blocked by the budget."""
        return self._nodes_in_state(NodeState.UNVISITED)

    @property
    def not_visited(self) -> tuple[SimilarityNode, ...]:
        """Nodes with no successful lookup, including failed requests."""
        return self._nodes_in_state(NodeState.UNVISITED, NodeState.FAILED)

    @property
    def pending(self) -> tuple[SimilarityNode, ...]:
        """Alias for :attr:`not_visited`."""
        return self.not_visited

    @property
    def empty(self) -> tuple[SimilarityNode, ...]:
        """Visited nodes whose response genuinely contained no neighbours."""
        return self._nodes_in_state(NodeState.EMPTY)

    @property
    def no_neighbors(self) -> tuple[SimilarityNode, ...]:
        return self.empty

    @property
    def no_neighbours(self) -> tuple[SimilarityNode, ...]:
        return self.empty

    @property
    def truncated(self) -> tuple[SimilarityNode, ...]:
        """Visited nodes whose response reached the requested list limit."""
        return self._nodes_in_state(NodeState.TRUNCATED)

    @property
    def complete(self) -> tuple[SimilarityNode, ...]:
        return self._nodes_in_state(NodeState.COMPLETE)

    @property
    def boundary(self) -> tuple[SimilarityNode, ...]:
        """Discovered nodes at ``max_depth``; expanding them was out of scope."""
        return self._nodes_in_state(NodeState.BOUNDARY)

    @property
    def failed(self) -> tuple[SimilarityNode, ...]:
        return self._nodes_in_state(NodeState.FAILED)

    @property
    def states(self) -> dict[str, tuple[str, ...]]:
        """Names grouped by state, deterministic in discovery order."""
        return {
            state.value: tuple(node.name for node in self._nodes_in_state(state))
            for state in NodeState
            if state.name not in {"NOT_VISITED", "NO_NEIGHBORS", "NO_NEIGHBOURS"}
        }

    @property
    def named_counts(self) -> dict[str, int]:
        """Number of distinct parents that named each target."""
        counts: Counter[str] = Counter()
        names: dict[str, str] = {}
        for edge in self.edges:
            key = _name_key(edge.target)
            counts[key] += 1
            names.setdefault(key, edge.target)
        return {names[key]: count for key, count in counts.items()}

    @property
    def by_direction(self) -> dict[tuple[str, str], SimilarityEdge]:
        """Edges keyed by the directed pair ``(source, target)``."""
        return {(edge.source, edge.target): edge for edge in self.edges}

    @property
    def edges_by_direction(self) -> dict[tuple[str, str], SimilarityEdge]:
        return self.by_direction

    @property
    def directed(self) -> dict[str, dict[str, SimilarityEdge]]:
        """Nested adjacency: ``directed[source][target] = edge``."""
        result: dict[str, dict[str, SimilarityEdge]] = {}
        for edge in self.edges:
            result.setdefault(edge.source, {})[edge.target] = edge
        return result

    @property
    def claim_order(self) -> tuple[str, ...]:
        """Artists in request-attempt order, including retries."""
        return tuple(record.artist for record in self.request_log)

    @property
    def request_order(self) -> tuple[str, ...]:
        return self.claim_order

    @property
    def claimed_artists(self) -> tuple[str, ...]:
        """Distinct artists in first-attempt order, without retries."""
        seen: set[str] = set()
        order: list[str] = []
        for record in self.request_log:
            key = _name_key(record.artist)
            if key in seen:
                continue
            seen.add(key)
            order.append(record.artist)
        return tuple(order)

    @property
    def attempts_by_artist(self) -> dict[str, int]:
        """Number of budget claims per artist, including retries."""
        return {node.name: node.attempts for node in self.all if node.attempts}

    @property
    def corpus(self) -> list[dict[str, Any]]:
        """One JSON-friendly record per directed edge."""
        return [edge.to_dict() for edge in self.edges]

    def get_node(self, name: str) -> SimilarityNode | None:
        return self._node_index.get(_name_key(name))

    def report(self) -> dict[str, Any]:
        """Summarise the three budget-exhaustion outcomes explicitly."""
        return {
            "budget": self.budget,
            "budget_used": self.budget_used,
            "remaining_budget": self.remaining_budget,
            "budget_exhausted": self.budget_exhausted,
            "unvisited": [node.name for node in self.unvisited],
            "no_neighbors": [node.name for node in self.empty],
            "truncated": [node.name for node in self.truncated],
            "complete": [node.name for node in self.complete],
            "boundary": [node.name for node in self.boundary],
            "failed": [node.name for node in self.failed],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "seeds": list(self.seeds),
            "max_depth": self.max_depth,
            "budget": self.budget,
            "budget_used": self.budget_used,
            "remaining_budget": self.remaining_budget,
            "budget_exhausted": self.budget_exhausted,
            "nodes": {node.name: node.to_dict() for node in self.nodes.values()},
            "edges": [edge.to_dict() for edge in self.edges],
            "by_direction": {
                source: {target: edge.to_dict() for target, edge in targets.items()}
                for source, targets in self.directed.items()
            },
            "named_counts": self.named_counts,
            "requests": [record.to_dict() for record in self.request_log],
            "failures": [failure.to_dict() for failure in self.failures],
            "report": self.report(),
        }

    def save_json(self, path: str | Path) -> Path:
        """Write the complete crawl as JSON."""
        destination = Path(path)
        destination.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return destination

    def save_corpus(self, path: str | Path) -> Path:
        """Write one JSONL record per directed edge."""
        destination = Path(path)
        with destination.open("w", encoding="utf-8") as stream:
            for record in self.corpus:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return destination

    def save(self, path: str | Path) -> Path:
        """Save JSONL for ``.jsonl``/``.ndjson`` paths, otherwise JSON."""
        if Path(path).suffix.lower() in {".jsonl", ".ndjson"}:
            return self.save_corpus(path)
        return self.save_json(path)


class SimilarityCrawler:
    """Reusable crawler configuration for budgeted similar-artist expansion."""

    def __init__(
        self,
        network: Any = None,
        *,
        source: Any = None,
        responses: Mapping[str, Any] | None = None,
        fetcher: Callable[..., Any] | None = None,
        limit: int | None = None,
        max_depth: int = 2,
        max_retries: int = 1,
        retry_on: Callable[[Exception], bool] | None = None,
    ) -> None:
        self.network = network
        if source is not None and fetcher is not None:
            msg = "Pass only one of source or fetcher"
            raise ValueError(msg)
        self.source = source if source is not None else fetcher
        self.responses = responses
        self.limit = _validate_limit(limit)
        self.max_depth = _validate_depth(max_depth)
        self.max_retries = _validate_retries(max_retries)
        self.retry_on = retry_on or (lambda _error: True)
        self._fetcher = _make_fetcher(
            network=network, source=self.source, responses=responses
        )

    def crawl(
        self,
        artists: Any = None,
        *,
        seeds: Any = None,
        budget: int | None = None,
        request_budget: int | None = None,
        limit: int | None = None,
        max_depth: int | None = None,
        depth: int | None = None,
        max_retries: int | None = None,
        retry_on: Callable[[Exception], bool] | None = None,
    ) -> SimilarityCrawl:
        if artists is not None and seeds is not None:
            msg = "Pass either artists or seeds, not both"
            raise ValueError(msg)
        given = artists if seeds is None else seeds
        if given is None:
            msg = "At least one seed artist is required"
            raise ValueError(msg)

        if budget is not None and request_budget is not None:
            msg = "Pass either budget or request_budget, not both"
            raise ValueError(msg)
        if request_budget is not None:
            budget = request_budget
        budget = _validate_budget(budget)

        if max_depth is not None and depth is not None:
            msg = "Pass either max_depth or depth, not both"
            raise ValueError(msg)
        if depth is not None:
            max_depth = depth
        depth_limit = (
            self.max_depth if max_depth is None else _validate_depth(max_depth)
        )
        item_limit = self.limit if limit is None else _validate_limit(limit)
        retries = (
            self.max_retries if max_retries is None else _validate_retries(max_retries)
        )
        retry_allowed = self.retry_on if retry_on is None else retry_on

        return _crawl(
            seeds=_normalise_seeds(given),
            fetcher=self._fetcher,
            budget=budget,
            max_depth=depth_limit,
            limit=item_limit,
            max_retries=retries,
            retry_on=retry_allowed,
        )

    run = crawl
    expand = crawl


def crawl_similar_artists(
    artists: Any = None,
    *,
    seeds: Any = None,
    network: Any = None,
    source: Any = None,
    responses: Mapping[str, Any] | None = None,
    fetcher: Callable[..., Any] | None = None,
    budget: int | None = None,
    request_budget: int | None = None,
    max_depth: int = 2,
    depth: int | None = None,
    limit: int | None = None,
    max_retries: int = 1,
    retry_on: Callable[[Exception], bool] | None = None,
) -> SimilarityCrawl:
    """Expand similar artists from one or more seed artists.

    The seed has depth 0.  With the default ``max_depth=2``, the seeds and the
    depth-1 artists are queried (two rounds of expansion); depth-2 artists are
    discovered and recorded as ``BOUNDARY`` nodes.

    Exactly one of ``network``, ``source``, ``responses`` or ``fetcher`` should
    be supplied.  ``budget`` limits the total number of HTTP attempts, including
    retries.  A failure is retried at most ``max_retries`` times (default one)
    and only while budget remains.
    """
    if max_depth is not None and depth is not None:
        msg = "Pass either max_depth or depth, not both"
        raise ValueError(msg)
    depth_limit = max_depth if depth is None else depth

    if budget is not None and request_budget is not None:
        msg = "Pass either budget or request_budget, not both"
        raise ValueError(msg)
    if request_budget is not None:
        budget = request_budget

    crawler = SimilarityCrawler(
        network=network,
        source=source,
        responses=responses,
        fetcher=fetcher,
        limit=limit,
        max_depth=depth_limit,
        max_retries=max_retries,
        retry_on=retry_on,
    )
    return crawler.crawl(artists, seeds=seeds, budget=budget, limit=limit)


crawl_similar = crawl_similar_artists
expand_similar_artists = crawl_similar_artists
build_similarity_corpus = crawl_similar_artists
SimilarArtistCrawler = SimilarityCrawler
SimilarityCrawlResult = SimilarityCrawl


def _crawl(
    *,
    seeds: Sequence[str],
    fetcher: Callable[[str, int | None], Any],
    budget: int | None,
    max_depth: int,
    limit: int | None,
    max_retries: int,
    retry_on: Callable[[Exception], bool],
) -> SimilarityCrawl:
    nodes: dict[str, SimilarityNode] = {}
    node_order: list[str] = []
    edges: list[SimilarityEdge] = []
    edge_keys: set[tuple[str, str]] = set()
    adjacency: dict[str, list[str]] = {}
    queue: deque[str] = deque()
    request_log: list[RequestRecord] = []
    failures: list[FetchFailure] = []
    budget_used = 0
    seed_rank = {_name_key(seed_name): index for index, seed_name in enumerate(seeds)}

    def remaining() -> int | None:
        if budget is None:
            return None
        return max(budget - budget_used, 0)

    def add_seed(node: SimilarityNode, seed_name: str) -> bool:
        seed_key = _name_key(seed_name)
        if any(_name_key(existing) == seed_key for existing in node.seeds):
            return False
        node.seeds.append(seed_name)
        node.seeds.sort(key=lambda name: seed_rank.get(_name_key(name), len(seeds)))
        return True

    def propagate_seeds(source_key: str) -> None:
        pending: deque[str] = deque([source_key])
        while pending:
            parent_key = pending.popleft()
            parent = nodes[parent_key]
            for child_key in adjacency.get(parent_key, ()):
                child = nodes.get(child_key)
                if child is None:
                    continue
                changed = False
                for seed_name in parent.seeds:
                    if add_seed(child, seed_name):
                        changed = True
                if changed:
                    pending.append(child_key)

    def ensure_node(name: str, depth: int) -> tuple[str, SimilarityNode]:
        key = _name_key(name)
        if key not in nodes:
            node = SimilarityNode(name=name, depth=depth)
            nodes[key] = node
            node_order.append(key)
        return key, nodes[key]

    def add_parent(node: SimilarityNode, parent: SimilarityNode) -> bool:
        for existing in node.parents:
            if _name_key(existing) == _name_key(parent.name):
                node.matches.setdefault(parent.name, 0.0)
                return False
        node.parents.append(parent.name)
        return True

    def process_candidates(
        parent: SimilarityNode, candidates: Sequence[SimilarityCandidate]
    ) -> None:
        parent_key = _name_key(parent.name)
        child_depth = parent.depth + 1
        for candidate in candidates:
            key, child = ensure_node(candidate.name, child_depth)
            child.matches.setdefault(parent.name, candidate.match)

            add_parent(child, parent)
            for seed_name in parent.seeds:
                add_seed(child, seed_name)

            edge_key = (parent_key, key)
            if edge_key not in edge_keys:
                edge_keys.add(edge_key)
                adjacency.setdefault(parent_key, []).append(key)
                edges.append(
                    SimilarityEdge(
                        source=parent.name,
                        target=child.name,
                        match=candidate.match,
                        depth=child_depth,
                        seeds=tuple(parent.seeds),
                    )
                )

            propagate_seeds(parent_key)

            if child_depth < max_depth and child.state is NodeState.UNVISITED:
                if key not in queue:
                    queue.append(key)
            elif child_depth >= max_depth and child.state is NodeState.UNVISITED:
                child.state = NodeState.BOUNDARY

    for seed_name in seeds:
        key, node = ensure_node(seed_name, 0)
        add_seed(node, seed_name)
        if max_depth == 0:
            node.state = NodeState.BOUNDARY
        else:
            queue.append(key)

    stop = False
    while queue and not stop:
        key = queue.popleft()
        node = nodes[key]
        if node.state is not NodeState.UNVISITED:
            continue

        if budget is not None and budget_used >= budget:
            stop = True
            break

        attempts = 0
        while True:
            if budget is not None and budget_used >= budget:
                stop = True
                break

            attempts += 1
            node.attempts += 1
            budget_used += 1
            try:
                raw_response = fetcher(node.name, limit)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                request_log.append(
                    RequestRecord(
                        artist=node.name,
                        depth=node.depth,
                        attempt=attempts,
                        outcome="error",
                        remaining_budget=remaining(),
                        error=message,
                    )
                )
                can_retry = attempts <= max_retries and retry_on(error)
                if can_retry and (budget is None or budget_used < budget):
                    continue

                node.state = NodeState.FAILED
                node.error = message
                failures.append(
                    FetchFailure(
                        artist=node.name,
                        depth=node.depth,
                        attempts=attempts,
                        error=message,
                    )
                )
                if budget is not None and budget_used >= budget:
                    stop = True
                break

            try:
                candidates, truncated = _normalise_response(raw_response, limit)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                request_log.append(
                    RequestRecord(
                        artist=node.name,
                        depth=node.depth,
                        attempt=attempts,
                        outcome="error",
                        remaining_budget=remaining(),
                        error=message,
                    )
                )
                can_retry = attempts <= max_retries and retry_on(error)
                if can_retry and (budget is None or budget_used < budget):
                    continue
                node.state = NodeState.FAILED
                node.error = message
                failures.append(
                    FetchFailure(
                        artist=node.name,
                        depth=node.depth,
                        attempts=attempts,
                        error=message,
                    )
                )
                if budget is not None and budget_used >= budget:
                    stop = True
                break

            request_log.append(
                RequestRecord(
                    artist=node.name,
                    depth=node.depth,
                    attempt=attempts,
                    outcome="ok",
                    remaining_budget=remaining(),
                    item_count=len(candidates),
                )
            )
            process_candidates(node, candidates)
            if not candidates:
                node.state = NodeState.EMPTY
            elif truncated:
                node.state = NodeState.TRUNCATED
            else:
                node.state = NodeState.COMPLETE
            break

        if budget is not None and budget_used >= budget:
            stop = True

    named_counts: Counter[str] = Counter()
    for edge in edges:
        named_counts[_name_key(edge.target)] += 1
    for edge in edges:
        edge.named_count = named_counts[_name_key(edge.target)]

    return SimilarityCrawl(
        seeds=seeds,
        max_depth=max_depth,
        budget=budget,
        budget_used=budget_used,
        nodes=nodes,
        node_order=node_order,
        edges=edges,
        request_log=request_log,
        failures=failures,
    )


def _make_fetcher(
    *,
    network: Any,
    source: Any,
    responses: Mapping[str, Any] | None,
) -> Callable[[str, int | None], Any]:
    provided = sum(value is not None for value in (network, source, responses))
    if provided > 1:
        msg = "Pass only one of network, source, responses or fetcher"
        raise ValueError(msg)

    if responses is not None:
        replay = ReplaySimilaritySource(responses)
        return lambda artist, limit: replay(artist, limit)
    if source is not None:
        if isinstance(source, Mapping):
            replay = ReplaySimilaritySource(source)
            return lambda artist, limit: replay(artist, limit)
        if hasattr(source, "get_similar") and not callable(source):
            method = source.get_similar
            return lambda artist, limit: _call_limit_signature(method, artist, limit)
        if callable(source):
            return lambda artist, limit: _call_source(source, artist, limit)
        msg = "source must be callable or a mapping of responses"
        raise TypeError(msg)
    if network is not None:
        if not hasattr(network, "get_artist"):
            msg = "network must provide get_artist(name)"
            raise TypeError(msg)

        def fetch_from_network(artist: str, limit: int | None) -> Any:
            return _call_limit_signature(
                network.get_artist(artist).get_similar, artist, limit
            )

        return fetch_from_network

    msg = "Pass one of network, source, responses or fetcher"
    raise ValueError(msg)


def _call_limit_signature(
    method: Callable[..., Any], artist: str, limit: int | None
) -> Any:
    if limit is None:
        return method()
    return method(limit=limit)


def _call_source(source: Callable[..., Any], artist: str, limit: int | None) -> Any:
    try:
        signature = inspect.signature(source)
    except (TypeError, ValueError):
        return source(artist, limit)

    parameters = list(signature.parameters.values())
    supports_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    )
    supports_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )
    accepts_limit_keyword = supports_kwargs or any(
        parameter.name == "limit"
        and parameter.kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        for parameter in parameters
    )
    accepts_two_positional = (
        supports_varargs
        or sum(
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            for parameter in parameters
        )
        >= 2
    )

    if accepts_limit_keyword:
        return source(artist, limit=limit)
    if accepts_two_positional:
        return source(artist, limit)
    return source(artist)


def _normalise_seeds(seeds: Any) -> tuple[str, ...]:
    if isinstance(seeds, (str, bytes)) or not isinstance(seeds, Iterable):
        values = [seeds]
    else:
        values = list(seeds)

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _candidate_name(value)
        key = _name_key(name)
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
    if not result:
        msg = "At least one seed artist is required"
        raise ValueError(msg)
    return tuple(result)


def _normalise_response(
    raw: Any, limit: int | None
) -> tuple[list[SimilarityCandidate], bool]:
    truncated: bool | None = None
    total: int | None = None

    if isinstance(raw, SimilarityResponse):
        items = raw.items
        truncated = raw.truncated
        total = raw.total
    elif isinstance(raw, Mapping) and any(
        key in raw for key in ("items", "results", "similar", "neighbours")
    ):
        items = raw.get(
            "items",
            raw.get("results", raw.get("similar", raw.get("neighbours"))),
        )
        truncated = raw.get("truncated")
        if truncated is None:
            truncated = raw.get("has_more")
        total = raw.get("total")
    else:
        items = raw

    if items is None:
        items = []
    if isinstance(items, (str, bytes)):
        items = [items]
    if isinstance(items, Mapping):
        items = [items]
    try:
        raw_items = list(items)
    except TypeError:
        raw_items = [items]

    candidates: list[SimilarityCandidate] = []
    seen: set[str] = set()
    for item in raw_items:
        candidate = _coerce_candidate(item)
        key = _name_key(candidate.name)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)

    if total is not None:
        try:
            total_count = int(total)
        except (TypeError, ValueError):
            total_count = None
        if total_count is not None and total_count > len(candidates):
            truncated = True

    if truncated is None:
        truncated = limit is not None and len(candidates) >= limit

    return candidates, bool(truncated)


def _coerce_candidate(raw: Any) -> SimilarityCandidate:
    if isinstance(raw, SimilarityCandidate):
        return raw
    if isinstance(raw, str):
        return SimilarityCandidate(raw.strip())

    if isinstance(raw, Mapping):
        name = raw.get("name", raw.get("artist", raw.get("target")))
        if name is None:
            msg = f"Candidate mapping has no name: {raw!r}"
            raise ValueError(msg)
        return SimilarityCandidate(
            _candidate_name(name),
            _candidate_match(raw.get("match", raw.get("score", 0.0))),
        )

    if hasattr(raw, "item") and not isinstance(raw, (str, bytes)):
        item = raw.item
        name = _candidate_name(item)
        match = getattr(raw, "match", 0.0)
        return SimilarityCandidate(name, _candidate_match(match))

    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        first, second = raw
        if isinstance(first, str):
            return SimilarityCandidate(_candidate_name(first), _candidate_match(second))

    name = _candidate_name(raw)
    return SimilarityCandidate(name, 0.0)


def _candidate_name(value: Any) -> str:
    if isinstance(value, str):
        name = value
    elif hasattr(value, "get_name"):
        name = value.get_name()
    elif hasattr(value, "name"):
        name = value.name
    else:
        msg = f"Cannot determine artist name from {value!r}"
        raise TypeError(msg)

    name = str(name).strip()
    if not name:
        msg = "Artist name cannot be empty"
        raise ValueError(msg)
    return name


def _candidate_match(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        msg = f"Invalid similarity match value: {value!r}"
        raise ValueError(msg) from error


def _name_key(name: Any) -> str:
    return str(name).strip().casefold()


def _validate_budget(budget: int | None) -> int | None:
    if budget is None:
        return None
    if isinstance(budget, bool) or not isinstance(budget, int):
        msg = "budget must be a non-negative integer or None"
        raise TypeError(msg)
    if budget < 0:
        msg = "budget must be a non-negative integer or None"
        raise ValueError(msg)
    return budget


def _validate_depth(depth: int) -> int:
    if isinstance(depth, bool) or not isinstance(depth, int):
        msg = "max_depth must be a non-negative integer"
        raise TypeError(msg)
    if depth < 0:
        msg = "max_depth must be a non-negative integer"
        raise ValueError(msg)
    return depth


def _validate_retries(retries: int) -> int:
    if isinstance(retries, bool) or not isinstance(retries, int):
        msg = "max_retries must be a non-negative integer"
        raise TypeError(msg)
    if retries < 0:
        msg = "max_retries must be a non-negative integer"
        raise ValueError(msg)
    return retries


def _validate_limit(limit: int | None) -> int | None:
    if limit is None or limit == 0:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int):
        msg = "limit must be a positive integer, 0 or None"
        raise TypeError(msg)
    if limit < 0:
        msg = "limit must be a positive integer, 0 or None"
        raise ValueError(msg)
    return limit
