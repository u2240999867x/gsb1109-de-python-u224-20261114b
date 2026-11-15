"""Offline-replayable bookkeeping for weekly chart synchronisation.

Last.fm returns chart windows as ``from``/``to`` timestamp pairs.  Adjacent
windows share their boundary: one week ends where the next one starts.  A play
exactly on that timestamp belongs to the *later*, half-open window only.  This
module keeps that rule explicit so two adjacent windows can never claim the same
play, and it keeps enough state to distinguish

* a window that has not produced a usable response yet (``pending``),
* a request that returned no chart payload at all (still pending, retry),
* a window the server published and which is genuinely empty
  (``sealed_empty``),
* a window whose published view was later replaced (``revised``), with the old
  snapshot and its source retained in ``history``.

The module performs no HTTP itself.  Callers feed it raw XML from
``user.getWeeklyChartList`` and the weekly chart endpoint (or a manifest of
saved responses), which makes the entire fetch/update decision replayable
offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from xml.dom import Node, minidom
from xml.parsers.expat import ExpatError

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


# Lifecycle of one window.  "revised" does not mean a revision is still needed;
# it means the current view was replaced and history is available.  A pending
# revision is reported by reconcile() as the "update" action.
STATUS_PENDING = "pending"
STATUS_SEALED = "sealed"
STATUS_SEALED_EMPTY = "sealed_empty"
STATUS_REVISED = "revised"
STATUS_REVISED_EMPTY = "revised_empty"

SEALED_STATUSES = frozenset(
    (STATUS_SEALED, STATUS_SEALED_EMPTY, STATUS_REVISED, STATUS_REVISED_EMPTY)
)

ACTION_FETCH = "fetch"
ACTION_UPDATE = "update"
ACTION_RETRY = "retry"
ACTION_UNCHANGED = "unchanged"


@dataclass(frozen=True, order=True)
class Window:
    """A half-open chart window ``[start, end)`` in Unix seconds.

    The server controls the actual boundaries, including late-published weeks
    and periods split into short windows.  Nothing here assumes seven days.
    """

    start: int
    end: int

    @classmethod
    def from_pair(cls, start: str | int, end: str | int) -> Window:
        start_int = int(start)
        end_int = int(end)
        if start_int >= end_int:
            msg = f"invalid chart window: from={start_int} to={end_int}"
            raise ValueError(msg)
        return cls(start_int, end_int)

    @property
    def key(self) -> str:
        return f"{self.start}-{self.end}"

    def contains(self, played_at: int) -> bool:
        """Return whether a play belongs to this window.

        ``from`` is inclusive and ``to`` is exclusive.  That is the only
        assignment that gives a boundary play exactly one owner when the next
        window starts at this window's ``to``.
        """

        return self.start <= played_at < self.end

    def overlaps(self, other: Window) -> bool:
        """Return whether two windows share more than a boundary."""

        return self.start < other.end and other.start < self.end

    def as_pair(self) -> tuple[str, str]:
        return str(self.start), str(self.end)

    def __str__(self) -> str:
        return f"[{self.start}, {self.end})"


@dataclass(frozen=True)
class ChartEntry:
    """One ranked row from a weekly chart."""

    artist: str
    title: str
    playcount: int
    rank: int | None = None


@dataclass(frozen=True)
class ChartSnapshot:
    """The content of one successful chart response, independent of source."""

    window: Window
    entries: tuple[ChartEntry, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.entries


@dataclass(frozen=True)
class SourceRecord:
    """Provenance for a stored snapshot."""

    response_id: str
    retrieved_at: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"response_id": self.response_id, "retrieved_at": self.retrieved_at}

    @classmethod
    def from_dict(cls, data: Mapping[str, typing.Any]) -> SourceRecord:
        return cls(
            response_id=str(data["response_id"]),
            retrieved_at=data.get("retrieved_at"),
        )


@dataclass(frozen=True)
class StoredView:
    """A snapshot together with where it came from."""

    snapshot: ChartSnapshot
    source: SourceRecord

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "snapshot": _snapshot_to_dict(self.snapshot),
            "source": self.source.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, typing.Any]) -> StoredView:
        return cls(
            snapshot=_snapshot_from_dict(data["snapshot"]),
            source=SourceRecord.from_dict(data["source"]),
        )


@dataclass
class WindowState:
    """Current view and replacement history for one window."""

    window: Window
    status: str = STATUS_PENDING
    current: StoredView | None = None
    # Oldest first.  The original published view is the first element once a
    # revision has been applied.
    history: list[StoredView] = field(default_factory=list)
    seen_responses: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return self.current is not None and self.current.snapshot.empty

    @property
    def is_sealed(self) -> bool:
        return self.status in SEALED_STATUSES

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "window": _window_to_dict(self.window),
            "status": self.status,
            "current": self.current.to_dict() if self.current else None,
            "history": [view.to_dict() for view in self.history],
            "seen_responses": dict(sorted(self.seen_responses.items())),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, typing.Any]) -> WindowState:
        current_raw = data.get("current")
        return cls(
            window=_window_from_dict(data["window"]),
            status=data.get("status", STATUS_PENDING),
            current=StoredView.from_dict(current_raw) if current_raw else None,
            history=[StoredView.from_dict(view) for view in data.get("history", [])],
            seen_responses=dict(data.get("seen_responses", {})),
        )


@dataclass(frozen=True)
class Candidate:
    """One offline response offered for a window."""

    window: Window
    response_id: str
    snapshot: ChartSnapshot | None
    retrieved_at: str | None = None


@dataclass(frozen=True)
class PlanItem:
    """What reconcile() proposes for one window."""

    window: Window
    action: str
    reason: str
    status_before: str
    candidate: Candidate | None = None

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "window": self.window.key,
            "from": str(self.window.start),
            "to": str(self.window.end),
            "action": self.action,
            "reason": self.reason,
            "status_before": self.status_before,
        }


@dataclass(frozen=True)
class ActionResult:
    """The outcome of ingesting one response."""

    window: Window
    action: str
    status: str
    reason: str
    changed: bool


class WeeklyChartSync:
    """State machine for weekly charts, with no network dependency."""

    def __init__(
        self,
        windows: Iterable[Window] | None = None,
        *,
        claimed_plays: Iterable[int] | None = None,
    ) -> None:
        self._states: dict[str, WindowState] = {}
        self._claimed_plays: dict[int, str] = {}
        if windows:
            self.add_published_windows(windows)
        if claimed_plays:
            for timestamp in claimed_plays:
                self.claim_play(timestamp)

    # ------------------------------------------------------------------
    # Published windows
    # ------------------------------------------------------------------
    @property
    def windows(self) -> list[Window]:
        return sorted(state.window for state in self._states.values())

    def add_published_windows(self, windows: Iterable[Window]) -> None:
        """Register the windows the server currently advertises.

        Re-registering an exact window is a no-op.  Windows that overlap by
        more than a shared boundary are rejected; otherwise a transition play
        would have two possible owners.  Newly discovered windows start
        ``pending``, even if they are earlier than ones already sealed.
        """

        for window in sorted(windows):
            for existing_key, state in self._states.items():
                if window.key != existing_key and window.overlaps(state.window):
                    msg = (
                        f"chart windows overlap: {state.window} and {window}; "
                        "use the server's actual split boundaries"
                    )
                    raise ValueError(msg)
            self._states.setdefault(window.key, WindowState(window=window))

    def state(self, window: Window | tuple[str, str] | str) -> WindowState:
        return self._states[_window_key(window)]

    def states(self) -> list[WindowState]:
        """Return states in chronological window order."""

        return [self._states[key] for key in sorted(self._states)]

    def pending(self) -> list[Window]:
        """Windows without a successful chart response yet."""

        return [state.window for state in self.states() if not state.is_sealed]

    def sealed_empty(self) -> list[Window]:
        """Windows that are published, sealed, and genuinely have no rows."""

        return [state.window for state in self.states() if state.empty]

    def revised(self) -> list[Window]:
        """Windows whose current view replaced an older published view."""

        return [
            state.window
            for state in self.states()
            if state.status.startswith("revised")
        ]

    # ------------------------------------------------------------------
    # Play ownership
    # ------------------------------------------------------------------
    def owner_of(self, timestamp: int) -> Window | None:
        """Return the single window that owns ``timestamp``.

        Because windows are half-open, a timestamp equal to a shared boundary
        is owned by the window starting there.  Overlaps are rejected when
        windows are registered, so there cannot be two answers.
        """

        matches = [
            state.window for state in self.states() if state.window.contains(timestamp)
        ]
        if len(matches) > 1:
            msg = f"play {timestamp} belongs to multiple windows: {matches}"
            raise ValueError(msg)
        return matches[0] if matches else None

    def claim_play(self, timestamp: int, window: Window | None = None) -> Window:
        """Claim one play for exactly one window.

        The stored claim is immutable: re-claiming the same timestamp for the
        same window is idempotent, re-claiming it for another window is an
        error.  A timestamp outside every published window is rejected rather
        than guessed.
        """

        owner = self.owner_of(timestamp)
        if owner is None:
            msg = f"play {timestamp} is not inside any published chart window"
            raise ValueError(msg)
        if window is not None and owner.key != window.key:
            msg = f"play {timestamp} belongs to {owner}, not {window}"
            raise ValueError(msg)
        existing = self._claimed_plays.get(timestamp)
        if existing is not None and existing != owner.key:
            msg = (
                f"play {timestamp} already claimed by {existing}, "
                f"cannot also belong to {owner.key}"
            )
            raise ValueError(msg)
        self._claimed_plays[timestamp] = owner.key
        return owner

    def play_owner(self, timestamp: int) -> Window | None:
        key = self._claimed_plays.get(timestamp)
        return self._states[key].window if key is not None else None

    def claimed_plays(self) -> dict[int, str]:
        return dict(sorted(self._claimed_plays.items()))

    def partition_plays(
        self, timestamps: Iterable[int], *, claim: bool = False
    ) -> dict[str, list[int]]:
        """Group play timestamps by window key, rejecting orphans."""

        result: dict[str, list[int]] = {window.key: [] for window in self.windows}
        for timestamp in timestamps:
            owner = self.claim_play(timestamp) if claim else self.owner_of(timestamp)
            if owner is None:
                msg = f"play {timestamp} is not inside any published chart window"
                raise ValueError(msg)
            result[owner.key].append(timestamp)
        return {key: sorted(values) for key, values in result.items() if values}

    def assert_disjoint(self) -> None:
        self._assert_windows_disjoint(self.windows, require_disjoint=True)

    @staticmethod
    def _assert_windows_disjoint(
        windows: Iterable[Window], *, require_disjoint: bool
    ) -> None:
        if not require_disjoint:
            return
        ordered = sorted(windows)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.overlaps(current):
                msg = f"overlapping windows: {previous} and {current}"
                raise ValueError(msg)

    # ------------------------------------------------------------------
    # Responses
    # ------------------------------------------------------------------
    def ingest_xml(
        self,
        window: Window,
        xml: str,
        *,
        response_id: str | None = None,
        retrieved_at: str | None = None,
        kind: str = "track",
    ) -> ActionResult:
        """Ingest one raw weekly chart response.

        An empty body, malformed XML, an ``lfm`` error, or a successful document
        without a chart element is a *missing* response: the window stays
        pending and is scheduled for retry.  A successful chart element with
        zero rows is a *sealed empty* week and must not be retried.
        """

        snapshot = parse_chart_response(xml, kind=kind)
        if response_id is None:
            response_id = _default_response_id(kind, window, snapshot)
        if snapshot is None:
            return self.record_missing_response(window, response_id)
        return self.ingest_snapshot(
            window, snapshot, response_id=response_id, retrieved_at=retrieved_at
        )

    def record_missing_response(self, window: Window, response_id: str) -> ActionResult:
        """Record a response that carried no usable chart payload.

        This is the "empty response" case.  It never seals a window and never
        makes it look empty; the next run must request it again.
        """

        state = self._ensure(window)
        response_id = response_id or f"track-{window.key}-missing"
        self._remember_response(state, response_id, None)
        return ActionResult(
            window=window,
            action=ACTION_RETRY,
            status=state.status,
            reason="response contained no chart payload",
            changed=False,
        )

    def ingest_snapshot(
        self,
        window: Window,
        snapshot: ChartSnapshot,
        *,
        response_id: str | None = None,
        retrieved_at: str | None = None,
    ) -> ActionResult:
        """Store a snapshot, replacing the current view only if it differs.

        Identical content from a new response id is not a revision.  Different
        content appends the previous current view (snapshot plus source) to
        ``history`` before installing the new one.
        """

        if snapshot.window != window:
            msg = (
                f"response window {snapshot.window} does not match requested "
                f"window {window}"
            )
            raise ValueError(msg)
        self.assert_disjoint()
        state = self._ensure(window)
        response_id = response_id or _default_response_id("track", window, snapshot)
        marker = _snapshot_content_id(snapshot)
        existing_marker = state.seen_responses.get(response_id)
        if existing_marker == marker:
            return ActionResult(
                window=window,
                action=ACTION_UNCHANGED,
                status=state.status,
                reason=f"response {response_id} already ingested",
                changed=False,
            )
        self._remember_response(state, response_id, marker)
        source = SourceRecord(response_id=response_id, retrieved_at=retrieved_at)

        if state.current is None:
            state.current = StoredView(snapshot=snapshot, source=source)
            state.status = STATUS_SEALED_EMPTY if snapshot.empty else STATUS_SEALED
            return ActionResult(
                window=window,
                action=ACTION_FETCH,
                status=state.status,
                reason="first published view",
                changed=True,
            )

        if state.current.snapshot == snapshot:
            return ActionResult(
                window=window,
                action=ACTION_UNCHANGED,
                status=state.status,
                reason=(
                    f"content already stored from {state.current.source.response_id}"
                ),
                changed=False,
            )

        state.history.append(state.current)
        state.current = StoredView(snapshot=snapshot, source=source)
        state.status = STATUS_REVISED_EMPTY if snapshot.empty else STATUS_REVISED
        return ActionResult(
            window=window,
            action=ACTION_UPDATE,
            status=state.status,
            reason="replaced current view, kept previous snapshot and source",
            changed=True,
        )

    def ingest_file(
        self,
        window: Window,
        path: str | os.PathLike[str],
        *,
        response_id: str | None = None,
        retrieved_at: str | None = None,
        kind: str = "track",
    ) -> ActionResult:
        path = Path(path)
        xml = path.read_text(encoding="utf-8")
        return self.ingest_xml(
            window,
            xml,
            response_id=response_id or path.name,
            retrieved_at=retrieved_at,
            kind=kind,
        )

    # ------------------------------------------------------------------
    # Reconcile / replay
    # ------------------------------------------------------------------
    def reconcile(
        self, candidates: Mapping[Window, Sequence[Candidate]]
    ) -> list[PlanItem]:
        """Classify what each window needs without changing state.

        For each window only the last candidate is considered the latest
        published view.  The classification is:

        ``fetch``
            No sealed current view yet and a successful snapshot is available.
            A window with no candidate at all is also ``fetch`` because it
            still has to be requested.
        ``retry``
            The latest response carried no chart payload.  The window stays
            pending, or keeps its previous sealed view, and must be requested
            again.  This is the empty-response case.
        ``update``
            A current view exists and the latest successful snapshot differs.
        ``unchanged``
            A current view exists and the latest snapshot has identical content.
        """

        plan: list[PlanItem] = []
        known = {
            window.key: state for window, state in zip(self.windows, self.states())
        }
        for window in candidates:
            if window.key not in known:
                known[window.key] = WindowState(window=window)
        self._assert_windows_disjoint(
            [state.window for state in known.values()], require_disjoint=True
        )
        for state in sorted(known.values(), key=lambda item: item.window):
            latest = _latest_candidate(candidates.get(state.window, ()))
            if state.current is None:
                if latest is None:
                    plan.append(
                        PlanItem(
                            window=state.window,
                            action=ACTION_FETCH,
                            reason="no offline response available",
                            status_before=state.status,
                        )
                    )
                elif latest.snapshot is None:
                    plan.append(
                        PlanItem(
                            window=state.window,
                            action=ACTION_RETRY,
                            reason="latest response contained no chart payload",
                            status_before=state.status,
                            candidate=latest,
                        )
                    )
                else:
                    plan.append(
                        PlanItem(
                            window=state.window,
                            action=ACTION_FETCH,
                            reason="no sealed view yet",
                            status_before=state.status,
                            candidate=latest,
                        )
                    )
                continue

            if latest is None:
                plan.append(
                    PlanItem(
                        window=state.window,
                        action=ACTION_UNCHANGED,
                        reason="no newer offline response",
                        status_before=state.status,
                    )
                )
                continue

            if latest.snapshot is None:
                plan.append(
                    PlanItem(
                        window=state.window,
                        action=ACTION_RETRY,
                        reason="latest response contained no chart payload",
                        status_before=state.status,
                        candidate=latest,
                    )
                )
                continue

            if latest.snapshot == state.current.snapshot:
                plan.append(
                    PlanItem(
                        window=state.window,
                        action=ACTION_UNCHANGED,
                        reason="latest response has identical content",
                        status_before=state.status,
                        candidate=latest,
                    )
                )
                continue

            plan.append(
                PlanItem(
                    window=state.window,
                    action=ACTION_UPDATE,
                    reason="latest response differs from current view",
                    status_before=state.status,
                    candidate=latest,
                )
            )
        return plan

    def apply(self, item: PlanItem) -> ActionResult:
        """Apply one plan item."""

        state = self._ensure(item.window)
        if item.action in (ACTION_FETCH, ACTION_UPDATE):
            if item.candidate is None:
                return ActionResult(
                    window=item.window,
                    action=ACTION_FETCH,
                    status=state.status,
                    reason="no response available to apply",
                    changed=False,
                )
            if item.candidate.snapshot is None:
                msg = f"plan item {item.window.key} has no usable snapshot"
                raise ValueError(msg)
            candidate = item.candidate
            assert candidate.snapshot is not None
            return self.ingest_snapshot(
                item.window,
                candidate.snapshot,
                response_id=candidate.response_id,
                retrieved_at=candidate.retrieved_at,
            )
        if item.action == ACTION_RETRY:
            if item.candidate is not None:
                return self.record_missing_response(
                    item.window, item.candidate.response_id
                )
            return ActionResult(
                window=item.window,
                action=ACTION_RETRY,
                status=state.status,
                reason=item.reason,
                changed=False,
            )
        return ActionResult(
            window=item.window,
            action=ACTION_UNCHANGED,
            status=state.status,
            reason=item.reason,
            changed=False,
        )

    def apply_plan(self, plan: Iterable[PlanItem]) -> list[ActionResult]:
        return [self.apply(item) for item in plan]

    # ------------------------------------------------------------------
    # Persistence and reporting
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "windows": [state.to_dict() for state in self.states()],
            "claimed_plays": self.claimed_plays(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, typing.Any]) -> WeeklyChartSync:
        ledger = cls()
        for raw in data.get("windows", []):
            state = WindowState.from_dict(raw)
            ledger._states[state.window.key] = state
        for raw_timestamp, key in data.get("claimed_plays", {}).items():
            ledger._claimed_plays[int(raw_timestamp)] = str(key)
        return ledger

    def save(self, path: str | os.PathLike[str]) -> None:
        path = Path(path)
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> WeeklyChartSync:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def report(self, plan: Sequence[PlanItem] | None = None) -> dict[str, typing.Any]:
        """Machine-readable state, optionally grouped by a pending plan."""

        report: dict[str, typing.Any] = {
            "windows": [
                {
                    "window": state.window.key,
                    "from": str(state.window.start),
                    "to": str(state.window.end),
                    "status": state.status,
                    "empty": state.empty,
                    "source": (
                        state.current.source.to_dict() if state.current else None
                    ),
                    "history": [
                        {
                            "source": view.source.to_dict(),
                            "entries": len(view.snapshot.entries),
                        }
                        for view in state.history
                    ],
                }
                for state in self.states()
            ],
            "pending": [window.key for window in self.pending()],
            "sealed_empty": [window.key for window in self.sealed_empty()],
            "revised": [window.key for window in self.revised()],
            "window_status": {
                state.window.key: state.status for state in self.states()
            },
            "claimed_plays": self.claimed_plays(),
        }
        if plan is not None:
            for action in (ACTION_FETCH, ACTION_UPDATE, ACTION_RETRY, ACTION_UNCHANGED):
                report[action] = [
                    item.window.key for item in plan if item.action == action
                ]
        return report

    def _ensure(self, window: Window) -> WindowState:
        return self._states.setdefault(window.key, WindowState(window=window))

    @staticmethod
    def _remember_response(
        state: WindowState, response_id: str, content_id: str | None
    ) -> None:
        marker = content_id or "no-chart-payload"
        previous = state.seen_responses.get(response_id)
        if previous is not None and previous != marker:
            msg = (
                f"response id {response_id} reused with different content "
                f"for window {state.window.key}"
            )
            raise ValueError(msg)
        state.seen_responses[response_id] = marker


# ------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------
def parse_chart_list_xml(xml: str) -> list[Window]:
    """Parse ``user.getWeeklyChartList`` into exact, non-overlapping windows.

    Duplicate exact windows collapse.  Overlapping or inverted windows raise,
    because no half-open play attribution can make them safe.
    """

    doc = _parse_xml(xml)
    lfm = doc.getElementsByTagName("lfm")
    if not lfm or lfm[0].getAttribute("status") != "ok":
        msg = "chart list response is not an ok lfm document"
        raise ValueError(msg)
    if not doc.getElementsByTagName("weeklychartlist"):
        msg = "chart list response has no weeklychartlist element"
        raise ValueError(msg)
    windows = [
        Window.from_pair(node.getAttribute("from"), node.getAttribute("to"))
        for node in doc.getElementsByTagName("chart")
    ]

    unique: list[Window] = []
    seen: set[str] = set()
    for window in sorted(windows):
        if window.key in seen:
            continue
        if unique and unique[-1].overlaps(window):
            msg = f"overlapping chart windows: {unique[-1]} and {window}"
            raise ValueError(msg)
        seen.add(window.key)
        unique.append(window)
    return unique


# Convenience alias used by callers and older examples.
parse_chart_list = parse_chart_list_xml


def parse_chart_response(xml: str, *, kind: str = "track") -> ChartSnapshot | None:
    """Parse a weekly chart response.

    Returns ``None`` for any response that does not prove the server published
    a chart: empty body, malformed XML, ``lfm`` error, missing chart element,
    or missing boundary attributes.  A chart element with no rows returns a
    snapshot with ``empty == True``.
    """

    if not xml or not xml.strip():
        return None
    try:
        doc = _parse_xml(xml)
    except ExpatError:
        return None

    lfm = doc.getElementsByTagName("lfm")
    if not lfm or lfm[0].getAttribute("status") != "ok":
        return None

    chart_tag, row_tag, artist_tag = _chart_tags(kind)
    chart_nodes = doc.getElementsByTagName(chart_tag)
    if not chart_nodes:
        return None
    root = chart_nodes[0]
    start, end = root.getAttribute("from"), root.getAttribute("to")
    if not start or not end:
        return None
    window = Window.from_pair(start, end)

    entries = []
    for row in root.getElementsByTagName(row_tag):
        title = _text(row, "name")
        artist = _text(row, artist_tag) if artist_tag else ""
        if title == "":
            continue
        entries.append(
            ChartEntry(
                artist=artist,
                title=title,
                playcount=int(_text(row, "playcount") or 0),
                rank=_optional_int(row.getAttribute("rank")),
            )
        )
    return ChartSnapshot(window=window, entries=tuple(entries))


def parse_album_response(xml: str) -> ChartSnapshot | None:
    return parse_chart_response(xml, kind="album")


def parse_artist_response(xml: str) -> ChartSnapshot | None:
    return parse_chart_response(xml, kind="artist")


def _chart_tags(kind: str) -> tuple[str, str, str | None]:
    normalized = kind.lower()
    if normalized == "track":
        return "weeklytrackchart", "track", "artist"
    if normalized == "album":
        return "weeklyalbumchart", "album", "artist"
    if normalized == "artist":
        return "weeklyartistchart", "artist", None
    msg = f"unknown chart kind: {kind!r}"
    raise ValueError(msg)


def _parse_xml(xml: str) -> minidom.Document:
    return minidom.parseString(xml)


def _text(node, tag_name: str | None) -> str:
    if tag_name is None:
        return ""
    children = node.getElementsByTagName(tag_name)
    if not children:
        return ""
    child = children[0].firstChild
    if child is None or child.nodeType != Node.TEXT_NODE:
        return ""
    return child.data.strip()


def _optional_int(value: str) -> int | None:
    return int(value) if value else None


def _default_response_id(
    kind: str, window: Window, snapshot: ChartSnapshot | None
) -> str:
    if snapshot is None:
        return f"{kind}-{window.key}-missing"
    digest = _snapshot_content_id(snapshot)[:12]
    return f"{kind}-{window.key}-{digest}"


def _snapshot_content_id(snapshot: ChartSnapshot) -> str:
    """Stable digest of the view content, used to detect real revisions."""

    payload = json.dumps(
        [
            [entry.artist, entry.title, entry.playcount, entry.rank]
            for entry in snapshot.entries
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------
# Offline replay
# ------------------------------------------------------------------
@dataclass(frozen=True)
class ReplayResult:
    plan: list[PlanItem]
    ledger: WeeklyChartSync
    applied: bool

    def to_dict(self) -> dict[str, typing.Any]:
        return {
            "applied": self.applied,
            "plan": [item.to_dict() for item in self.plan],
            "report": self.ledger.report(self.plan),
        }


def load_replay_manifest(
    manifest_path: str | os.PathLike[str],
) -> tuple[list[Window], list[Candidate], list[Candidate]]:
    """Load a manifest containing a chart list plus baseline/incoming files.

    Paths are relative to the manifest.  The chart list is authoritative: every
    window it publishes is registered, including delayed and split windows.
    """

    manifest_path = Path(manifest_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    windows = parse_chart_list_xml((base / data["chart_list"]).read_text("utf-8"))
    baseline = [_load_candidate(entry, base) for entry in data.get("baseline", [])]
    incoming = [_load_candidate(entry, base) for entry in data.get("incoming", [])]
    return windows, baseline, incoming


def replay_ledger(
    manifest_path: str | os.PathLike[str],
    ledger_path: str | os.PathLike[str],
    *,
    apply: bool = False,
) -> ReplayResult:
    """Load a saved ledger, register newly published windows, and reconcile.

    This is the incremental form: the ledger already contains sealed windows
    from earlier runs, while ``chart_list.xml`` may now include a delayed or
    split window that was unknown before.
    """

    windows, _baseline, incoming = load_replay_manifest(manifest_path)
    ledger = WeeklyChartSync.load(ledger_path)
    ledger.add_published_windows(windows)
    plan = ledger.reconcile(_group_candidates(incoming))
    if apply:
        ledger.apply_plan(plan)
    return ReplayResult(plan=plan, ledger=ledger, applied=apply)


def replay_manifest(
    manifest_path: str | os.PathLike[str], *, apply: bool = False
) -> ReplayResult:
    """Replay baseline then classify/apply incoming responses.

    Baseline responses are ingested first, exactly as a previous successful
    run would have left them.  Incoming responses are then grouped per window;
    only the last one is the latest published view.
    """

    windows, baseline, incoming = load_replay_manifest(manifest_path)
    ledger = WeeklyChartSync(windows)
    for candidate in baseline:
        _ingest_candidate(ledger, candidate)

    grouped = _group_candidates(incoming)
    plan = ledger.reconcile(grouped)
    if apply:
        ledger.apply_plan(plan)
    return ReplayResult(plan=plan, ledger=ledger, applied=apply)


def _load_candidate(entry: Mapping[str, typing.Any], base: Path) -> Candidate:
    window = Window.from_pair(entry["window"][0], entry["window"][1])
    path = base / entry["file"]
    xml = path.read_text(encoding="utf-8")
    snapshot = parse_chart_response(xml, kind=entry.get("kind", "track"))
    if snapshot is not None and snapshot.window != window:
        msg = (
            f"{path}: response window {snapshot.window} does not match "
            f"manifest window {window}"
        )
        raise ValueError(msg)
    return Candidate(
        window=window,
        response_id=entry.get("response_id", path.name),
        snapshot=snapshot,
        retrieved_at=entry.get("retrieved_at"),
    )


def _ingest_candidate(ledger: WeeklyChartSync, candidate: Candidate) -> ActionResult:
    if candidate.snapshot is None:
        return ledger.record_missing_response(candidate.window, candidate.response_id)
    return ledger.ingest_snapshot(
        candidate.window,
        candidate.snapshot,
        response_id=candidate.response_id,
        retrieved_at=candidate.retrieved_at,
    )


def _group_candidates(
    candidates: Iterable[Candidate],
) -> dict[Window, list[Candidate]]:
    grouped: dict[Window, list[Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.window, []).append(candidate)
    return grouped


def _latest_candidate(candidates: Sequence[Candidate]) -> Candidate | None:
    return candidates[-1] if candidates else None


# ------------------------------------------------------------------
# Serialisation helpers
# ------------------------------------------------------------------
def _window_to_dict(window: Window) -> dict[str, int]:
    return {"from": window.start, "to": window.end}


def _window_from_dict(data: Mapping[str, typing.Any]) -> Window:
    return Window(int(data["from"]), int(data["to"]))


def _snapshot_to_dict(snapshot: ChartSnapshot) -> dict[str, typing.Any]:
    return {
        "window": _window_to_dict(snapshot.window),
        "entries": [
            {
                "artist": entry.artist,
                "title": entry.title,
                "playcount": entry.playcount,
                "rank": entry.rank,
            }
            for entry in snapshot.entries
        ],
    }


def _snapshot_from_dict(data: Mapping[str, typing.Any]) -> ChartSnapshot:
    return ChartSnapshot(
        window=_window_from_dict(data["window"]),
        entries=tuple(
            ChartEntry(
                artist=entry["artist"],
                title=entry["title"],
                playcount=int(entry["playcount"]),
                rank=entry.get("rank"),
            )
            for entry in data["entries"]
        ),
    )


def _window_key(window: Window | tuple[str, str] | str) -> str:
    if isinstance(window, Window):
        return window.key
    if isinstance(window, str):
        return window
    return Window.from_pair(window[0], window[1]).key


# ------------------------------------------------------------------
# CLI: python -m pylast.weekly_sync manifest.json [--apply]
# ------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay weekly chart responses and report fetch/update work."
    )
    parser.add_argument("manifest", help="JSON replay manifest")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the proposed plan before printing the report",
    )
    parser.add_argument(
        "--state",
        help="optional ledger JSON to load instead of the manifest baseline",
    )
    args = parser.parse_args(argv)

    if args.state:
        result = replay_ledger(args.manifest, args.state, apply=args.apply)
    else:
        result = replay_manifest(args.manifest, apply=args.apply)

    json.dump(result.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
