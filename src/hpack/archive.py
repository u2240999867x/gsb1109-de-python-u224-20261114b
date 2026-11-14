"""
Planning which HPACK artefacts to archive so targeted header blocks can be
replayed in a blank environment.

A connection produces an ordered sequence of raw header blocks.  A *checkpoint*
is the complete dynamic-table state immediately *before* one of those blocks;
it can replace every block that came before it.  Raw blocks and checkpoints
each have an archive cost, and blocks or checkpoints shared by several targets
are paid for once.

The planner solves that selection exactly.  It also replays a selection through
:class:`hpack.Decoder`, so a plan can be proven to restore every target field,
and the index-shift failures caused by "irrelevant" insertions are exposed as
concrete counterexamples.

Terminology:

``block i``
    The ``i``-th raw header block of the connection, ``0 <= i < n``.

``checkpoint p``
    A snapshot of the dynamic table *before* block ``p`` is decoded.  It can
    start a replay at position ``p`` for the price of its own archive cost.

``source``
    Where a target starts replaying from: position ``0`` (blank environment,
    free) or the position of an archived checkpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import itertools
from typing import Iterable, Sequence

from .hpack import Decoder
from .table import HeaderTable, TableState

Field = tuple[bytes, bytes]


@dataclass(frozen=True)
class Checkpoint:
    """
    An existing dynamic-table checkpoint available for archiving.

    :param position: Position of the state; the checkpoint describes the table
        immediately before ``blocks[position]`` is decoded.
    :param stored_size: Number of bytes needed to archive the checkpoint.
    :param state: The captured table state.  It is needed for replay
        verification but not for the cost-only planning pass.
    """

    position: int
    stored_size: int
    state: TableState | None = None

    def __post_init__(self) -> None:
        if self.position < 0:
            msg = "Checkpoint positions must be non-negative"
            raise ValueError(msg)
        if self.stored_size < 0:
            msg = "Checkpoint sizes must be non-negative"
            raise ValueError(msg)


@dataclass(frozen=True)
class Target:
    """
    A header block that must be recoverable, together with the fields it has to
    produce.

    :param block: Index of the target block.  Its raw bytes are always needed:
        a checkpoint stores table state, never the block itself.
    :param fields: The exact ``(name, value)`` pairs the block must decode to,
        in order.  Empty means "cost planning only, do not verify contents".
    """

    block: int
    fields: tuple[Field, ...] = ()

    def __post_init__(self) -> None:
        if self.block < 0:
            msg = "Target block indexes must be non-negative"
            raise ValueError(msg)


@dataclass(frozen=True)
class TargetSource:
    """
    The interval that must be replayed to recover one target.
    """

    target: int
    source: int
    first_block: int
    last_block: int


@dataclass(frozen=True)
class ArchivePlan:
    """
    A concrete selection of archived material.

    :param block_indices: Raw blocks to archive, ascending.  Every target block
        is present.
    :param checkpoint_positions: Checkpoints to archive, ascending.
    :param total_size: Sum of the archived block and checkpoint sizes.  Shared
        blocks and checkpoints are counted once.
    :param sources: For every target, the replay interval that recovers it.
    """

    block_indices: tuple[int, ...]
    checkpoint_positions: tuple[int, ...]
    total_size: int
    sources: tuple[TargetSource, ...]

    @property
    def blocks(self) -> int:
        """
        Number of archived raw blocks.
        """
        return len(self.block_indices)

    @property
    def checkpoints(self) -> int:
        """
        Number of archived checkpoints.
        """
        return len(self.checkpoint_positions)


class ArchivePlanner:
    """
    Exact minimum-cost archive planner.

    :param block_sizes: Archive size of every raw block, in connection order.
    :param checkpoints: Existing checkpoints that may be archived.
    :param targets: Blocks that must be recoverable.

    A solution partitions the targets (in connection order) into replay groups.
    A group starting at target ``j`` uses one source ``p`` in
    ``(previous target, target j]`` -- or the blank start for the first group --
    and pays the checkpoint plus every block ``p..last target of the group``.
    Because ``p`` is strictly after the previous target, groups never overlap,
    so shared prefixes are naturally counted once.
    """

    def __init__(
        self,
        block_sizes: Sequence[int],
        checkpoints: Iterable[Checkpoint],
        targets: Iterable[Target],
    ) -> None:
        self.block_sizes = tuple(int(size) for size in block_sizes)
        if any(size < 0 for size in self.block_sizes):
            msg = "Block sizes must be non-negative"
            raise ValueError(msg)

        self.checkpoints: dict[int, int] = {}
        self.checkpoint_states: dict[int, TableState] = {}
        for checkpoint in checkpoints:
            if checkpoint.position > len(self.block_sizes):
                msg = "Checkpoint position is past the end of the connection"
                raise ValueError(msg)
            previous = self.checkpoints.get(checkpoint.position)
            if previous is not None and previous != checkpoint.stored_size:
                msg = "Conflicting sizes for one checkpoint position"
                raise ValueError(msg)
            self.checkpoints[checkpoint.position] = checkpoint.stored_size
            if checkpoint.state is not None:
                self.checkpoint_states[checkpoint.position] = checkpoint.state

        self.targets = tuple(sorted(targets, key=lambda target: target.block))
        self._positions = [target.block for target in self.targets]
        if len(set(self._positions)) != len(self._positions):
            msg = "Target block indexes must be unique"
            raise ValueError(msg)
        for block in self._positions:
            if not 0 <= block < len(self.block_sizes):
                msg = "Target block index is outside the connection"
                raise ValueError(msg)

        self._prefix = [0]
        for size in self.block_sizes:
            self._prefix.append(self._prefix[-1] + size)

    def _group_source(self, first: int) -> tuple[int, int] | None:
        """
        Cheapest ``(position, checkpoint size - prefix[position])`` for a group
        starting at target index ``first``.

        The source must lie after the previous target and no later than this
        group's first target.  The very first group may instead start blank at
        position 0, which is free.
        """
        previous = self._positions[first - 1] if first else -1
        limit = self._positions[first]

        best: tuple[int, int] | None = None
        if first == 0:
            best = (0, 0)
        for position, stored_size in self.checkpoints.items():
            if previous < position <= limit:
                candidate = (position, stored_size - self._prefix[position])
                if best is None or candidate[1] < best[1]:
                    best = candidate
        return best

    def _solve(self) -> tuple[int, list[int], list[int]]:
        """
        Solve the selection problem.

        ``prefix_cost[i]`` is the cheapest way to recover the first ``i``
        targets.  Returns ``(total, argmin, source)``; for a prefix ending at
        target index ``end - 1``, ``argmin[end]`` is the first target index of
        its last group and ``source[argmin[end]]`` is that group's source.

        The first group always has the blank source available, so a solution
        always exists once the inputs validate.
        """
        count = len(self._positions)
        if count == 0:
            return 0, [], []

        prefix_cost = [0.0] * (count + 1)
        argmin = [0] * (count + 1)
        source = [0] * count

        best_prefix = 0.0
        best_start = 0

        for end in range(1, count + 1):
            first = end - 1
            group = self._group_source(first)
            if group is not None:
                position, start_cost = group
                value = prefix_cost[first] + start_cost
                if value < best_prefix:
                    best_prefix = value
                    best_start = first
                    source[first] = position

            argmin[end] = best_start
            prefix_cost[end] = best_prefix + self._prefix[self._positions[end - 1] + 1]

        return int(prefix_cost[count]), argmin, source

    def plan(self) -> ArchivePlan:
        """
        Return the cheapest archive that restores every target.
        """
        total, argmin, source = self._solve()
        selected_blocks: set[int] = set()
        selected_checkpoints: set[int] = set()
        sources: list[TargetSource] = []

        end = len(self._positions)
        while end > 0:
            first = argmin[end]
            last = end - 1
            interval_source = source[first]
            selected_blocks.update(range(interval_source, self._positions[last] + 1))
            if interval_source:
                selected_checkpoints.add(interval_source)
            for index in range(first, last + 1):
                sources.append(
                    TargetSource(
                        target=self._positions[index],
                        source=interval_source,
                        first_block=interval_source,
                        last_block=self._positions[index],
                    )
                )
            end = first

        sources.sort(key=lambda item: item.target)
        return ArchivePlan(
            block_indices=tuple(sorted(selected_blocks)),
            checkpoint_positions=tuple(sorted(selected_checkpoints)),
            total_size=total,
            sources=tuple(sources),
        )

    def explain(self, plan: ArchivePlan) -> str:
        """
        Explain the cost of ``plan`` and why no cheaper selection exists.
        """
        lines = [
            "targets: " + ", ".join(str(target.block) for target in self.targets),
            "blocks: " + ", ".join(f"{index}({self.block_sizes[index]}B)" for index in plan.block_indices),
            "checkpoints: " + (
                ", ".join(f"{position}({self.checkpoints[position]}B)" for position in plan.checkpoint_positions) or "none"
            ),
        ]
        for item in plan.sources:
            origin = "blank" if item.source == 0 else f"checkpoint {item.source}"
            lines.append(f"target {item.target}: replay {item.first_block}..{item.target} from {origin}")

        block_cost = sum(self.block_sizes[index] for index in plan.block_indices)
        checkpoint_cost = sum(self.checkpoints[position] for position in plan.checkpoint_positions)
        lines.append(f"cost: {block_cost}B blocks + {checkpoint_cost}B checkpoints = {plan.total_size}B")
        lines.append(
            "minimal: every solution splits the targets into groups, each group starts "
            "at a source in (previous target, first target] and pays that source's "
            "checkpoint plus blocks source..last target; the dynamic programme "
            "minimises over every split and every admissible source and reached "
            f"{plan.total_size}B, so no selection can be smaller."
        )
        return "\n".join(lines)


def _fields_of(decoded: Iterable[object]) -> tuple[Field, ...]:
    return tuple((bytes(header[0]), bytes(header[1])) for header in decoded)  # type: ignore[index]


@dataclass
class ReplayResult:
    """
    Outcome of replaying an archive selection.

    :param decoded: Fields decoded for each block that was reached.
    :param reached: Block indexes that were actually decoded.
    :param targets: For each target, the decoded fields (``None`` if the target
        was not reached) and whether they match the expectation.
    :param error: The decoding error that stopped the replay, if any.
    :param broken_targets: Targets whose fields do not match.
    """

    decoded: dict[int, tuple[Field, ...]] = field(default_factory=dict)
    reached: tuple[int, ...] = ()
    targets: dict[int, tuple[tuple[Field, ...] | None, bool]] = field(default_factory=dict)
    error: Exception | None = None
    broken_targets: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        """
        Whether every target was reconstructed exactly.
        """
        return self.error is None and not self.broken_targets


def replay(
    blocks: Sequence[bytes],
    plan: ArchivePlan,
    targets: Iterable[Target],
    checkpoints: Iterable[Checkpoint] = (),
    initial_table_size: int = HeaderTable.DEFAULT_SIZE,
    max_allowed_table_size: int | None = None,
) -> ReplayResult:
    """
    Replay ``plan`` from a blank decoder and check every target.

    Archived blocks are decoded in connection order.  Before a block whose
    position carries an archived checkpoint, that checkpoint state is restored,
    which is how earlier blocks are skipped.  Decoding errors are captured in
    the result instead of propagating, so a rejected selection can be reported
    in full.

    :param blocks: All raw blocks of the connection, in order.
    :param plan: The selection to replay.
    :param targets: Targets to check.
    :param checkpoints: Available checkpoints, including their states.
    :param initial_table_size: Table size of the blank decoder, which must be
        the connection's initial setting.
    :param max_allowed_table_size: Largest table size the peer was allowed to
        request.  Defaults to ``initial_table_size``.
    """
    states = {checkpoint.position: checkpoint.state for checkpoint in checkpoints if checkpoint.state is not None}
    wanted = {target.block: target.fields for target in targets}
    archived = set(plan.block_indices)
    checkpoint_positions = set(plan.checkpoint_positions)

    decoder = Decoder()
    decoder.header_table_size = initial_table_size
    decoder.max_allowed_table_size = initial_table_size if max_allowed_table_size is None else max_allowed_table_size

    result = ReplayResult()
    reached: list[int] = []
    try:
        for index in sorted(archived):
            if index >= len(blocks):
                msg = "Archived block index is outside the connection"
                raise IndexError(msg)
            if index in checkpoint_positions:
                state = states.get(index)
                if state is None:
                    msg = f"Checkpoint {index} has no state and cannot start a replay"
                    raise ValueError(msg)
                decoder.header_table.restore(state)
            decoded = decoder.decode(blocks[index], raw=True)
            fields = _fields_of(decoded)
            result.decoded[index] = fields
            reached.append(index)
    except Exception as error:  # noqa: BLE001 - reported to the caller
        result.error = error
    result.reached = tuple(reached)

    broken: list[int] = []
    for index, expected in wanted.items():
        actual = result.decoded.get(index)
        matches = actual == expected
        result.targets[index] = (actual, matches)
        if not matches:
            broken.append(index)
    result.broken_targets = tuple(broken)

    return result


def brute_force_minimum(
    block_sizes: Sequence[int],
    checkpoints: Iterable[Checkpoint],
    targets: Iterable[Target],
) -> int | None:
    """
    Exhaustive minimum for small instances, used to validate :class:`ArchivePlanner`.
    """
    positions = sorted({target.block for target in targets})
    checkpoint_list = list(checkpoints)
    prefix = [0]
    for size in block_sizes:
        prefix.append(prefix[-1] + size)

    best: int | None = None
    for count in range(len(checkpoint_list) + 1):
        for chosen in itertools.combinations(checkpoint_list, count):
            chosen_positions = sorted(checkpoint.position for checkpoint in chosen)
            needed: set[int] = set()
            for target in positions:
                source = 0
                for position in chosen_positions:
                    if position <= target:
                        source = position
                needed.update(range(source, target + 1))
            cost = sum(block_sizes[index] for index in needed) + sum(checkpoint.stored_size for checkpoint in chosen)
            if best is None or cost < best:
                best = cost
    return best
