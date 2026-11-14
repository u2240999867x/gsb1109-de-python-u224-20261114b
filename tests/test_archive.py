"""
Tests for archive planning and blank-environment replay.
"""
import dataclasses
import random

import pytest

from hpack import (
    ArchivePlanner,
    Checkpoint,
    Decoder,
    Encoder,
    TableState,
    Target,
    brute_force_minimum,
    replay,
)


BLOCK = [b'\x82', b'\x84', b'\x86', b'\x88']
SIZES = [3, 4, 5, 6]


def planner(checkpoints, targets):
    return ArchivePlanner(SIZES, checkpoints, targets)


class TestCostPlanning:
    def test_plan_counts_archived_material(self):
        checkpoints = [Checkpoint(1, 2), Checkpoint(3, 2)]
        plan = planner(checkpoints, [Target(1), Target(3)]).plan()

        assert plan.blocks == len(plan.block_indices) == 2
        assert plan.checkpoints == len(plan.checkpoint_positions) == 2

    def test_shared_prefix_is_counted_once(self):
        plan = planner([], [Target(1), Target(3)]).plan()

        assert plan.block_indices == (0, 1, 2, 3)
        assert plan.checkpoint_positions == ()
        assert plan.total_size == sum(SIZES)
        assert plan.sources[-1].first_block == 0

    def test_checkpoint_replaces_prefix(self):
        checkpoints = [Checkpoint(2, 4)]
        plan = planner(checkpoints, [Target(3)]).plan()

        assert plan.block_indices == (2, 3)
        assert plan.checkpoint_positions == (2,)
        assert plan.total_size == 4 + SIZES[2] + SIZES[3]

    def test_checkpoint_is_skipped_when_not_worth_it(self):
        checkpoints = [Checkpoint(1, 100)]
        plan = planner(checkpoints, [Target(3)]).plan()

        assert plan.block_indices == (0, 1, 2, 3)
        assert plan.checkpoint_positions == ()

    def test_later_targets_use_different_checkpoints(self):
        checkpoints = [Checkpoint(0, 1), Checkpoint(3, 1)]
        plan = planner(checkpoints, [Target(1), Target(3)]).plan()

        # Target 1 uses blocks 0..1, target 3 starts at checkpoint 3; the
        # unread block 2 is not archived.
        assert plan.total_size == SIZES[0] + SIZES[1] + 1 + SIZES[3]
        assert plan.checkpoint_positions == (3,)
        assert plan.block_indices == (0, 1, 3)

    def test_target_bytes_are_always_kept(self):
        checkpoints = [Checkpoint(2, 1)]
        plan = planner(checkpoints, [Target(2)]).plan()

        assert plan.block_indices == (2,)
        assert plan.checkpoint_positions == (2,)

    def test_no_targets_is_empty(self):
        plan = planner([Checkpoint(1, 5)], []).plan()

        assert plan.total_size == 0
        assert plan.block_indices == ()
        assert plan.checkpoint_positions == ()

    def test_explain_mentions_partial_and_total(self):
        checkpoints = [Checkpoint(2, 4)]
        archive = planner(checkpoints, [Target(3)])
        text = archive.explain(archive.plan())

        assert "target 3" in text
        assert "checkpoint 2" in text
        assert "minimal" in text

    def test_invalid_inputs(self):
        with pytest.raises(ValueError):
            ArchivePlanner([-1], [], [])
        with pytest.raises(ValueError):
            ArchivePlanner([1], [Checkpoint(0, 1), Checkpoint(0, 2)], [])
        with pytest.raises(ValueError):
            ArchivePlanner([1], [Checkpoint(2, 1)], [])
        with pytest.raises(ValueError):
            ArchivePlanner([1], [], [Target(1)])
        with pytest.raises(ValueError):
            ArchivePlanner([1], [], [Target(0), Target(0)])
        with pytest.raises(ValueError):
            Checkpoint(-1, 1)
        with pytest.raises(ValueError):
            Checkpoint(0, -1)
        with pytest.raises(ValueError):
            Target(-1)


class TestPlannerIsExact:
    @pytest.mark.parametrize("seed", range(20))
    def test_matches_brute_force(self, seed):
        rng = random.Random(seed)
        sizes = [rng.randint(1, 9) for _ in range(rng.randint(1, 7))]
        checkpoints = [
            Checkpoint(position, rng.randint(1, 15))
            for position in range(len(sizes) + 1)
            if rng.random() < 0.5
        ]
        targets = [Target(index) for index in range(len(sizes)) if rng.random() < 0.5]
        if not targets:
            # Still exercise the empty target path.
            targets = [Target(0)]

        plan = ArchivePlanner(sizes, checkpoints, targets).plan()
        minimum = brute_force_minimum(sizes, checkpoints, targets)

        assert plan.total_size == minimum
        assert set(target.block for target in targets) <= set(plan.block_indices)
        assert plan.total_size == (
            sum(sizes[index] for index in plan.block_indices)
            + sum(item.stored_size for item in checkpoints if item.position in plan.checkpoint_positions)
        )


class TestTableState:
    def test_round_trip(self):
        encoder = Encoder()
        encoder.encode([("a", "1"), ("b", "2")], huffman=False)

        state = encoder.header_table.snapshot()
        assert isinstance(state, TableState)

        decoder = Decoder()
        decoder.header_table.restore(state)

        assert decoder.header_table.maxsize == encoder.header_table.maxsize
        assert list(decoder.header_table.dynamic_entries) == list(encoder.header_table.dynamic_entries)

    def test_snapshots_compare_by_value(self):
        encoder = Encoder()
        encoder.encode([("a", "1")], huffman=False)

        state = encoder.header_table.snapshot()
        assert state == encoder.header_table.snapshot()
        assert state.stored_size > 0
        assert hash(state) == hash(encoder.header_table.snapshot())
        assert repr(state).startswith("TableState(")
        assert state.__eq__(object()) is NotImplemented

        restored = Decoder()
        restored.header_table.restore(state)
        assert restored.header_table.resized is False


class TestEvictionTrap:
    """
    A block that is not itself a target can still be mandatory: it inserts an
    entry, evicts one, and shifts every dynamic index.  Dropping it makes the
    replay either fail or, worse, resolve a different name.
    """

    SIZE = 72

    def build(self, blocks):
        encoder = Encoder()
        encoder.header_table_size = self.SIZE
        states = {}
        encoded = []
        for index, headers in enumerate(blocks):
            states[index] = encoder.header_table.snapshot()
            encoded.append(encoder.encode(headers, huffman=False))
        states[len(blocks)] = encoder.header_table.snapshot()
        return encoded, states

    def test_missing_evicting_insert_breaks_replay(self):
        blocks, states = self.build([
            [("a", "1"), ("b", "2")],
            [("c", "3")],
            [("b", "2")],
        ])

        checkpoints = [
            Checkpoint(position, states[position].stored_size, states[position])
            for position in states
        ]
        target = Target(2, ((b"b", b"2"),))

        # The target is cheap: block 2 alone is one indexed field.  But replaying
        # it from a blank table cannot resolve the index at all.
        direct = ArchivePlanner([len(block) for block in blocks], checkpoints, [target]).plan()
        assert direct.block_indices == (0, 1, 2)
        assert direct.total_size == sum(len(block) for block in blocks)

        assert replay(blocks, direct, [target], checkpoints, initial_table_size=self.SIZE).ok is True

        skip_middle = dataclasses.replace(direct, block_indices=(0, 2))
        result = replay(blocks, skip_middle, [target], checkpoints, initial_table_size=self.SIZE)
        assert result.ok is False
        assert result.broken_targets == (2,)

    def test_dropping_evicting_insert_can_change_the_name(self):
        blocks, states = self.build([
            [("a", "1"), ("b", "2")],
            [("c", "3")],
            [("b", "7")],
        ])

        checkpoints = [
            Checkpoint(position, states[position].stored_size, states[position])
            for position in states
        ]

        # With block 1 replayed, dynamic index 63 is b'c', so the target decodes
        # to b'7' with name b'b' (partial match shifts to the older b entry).
        complete = replay(
            blocks,
            ArchivePlanner([len(block) for block in blocks], checkpoints, [Target(2)]).plan(),
            [Target(2)],
            checkpoints,
            initial_table_size=self.SIZE,
        )
        assert complete.decoded[2] == ((b"b", b"7"),)

        # Without block 1, index 63 is b'b', so the same raw byte silently
        # decodes as b'a' instead.
        partial = dataclasses.replace(
            ArchivePlanner([len(block) for block in blocks], checkpoints, [Target(2)]).plan(),
            block_indices=(0, 2),
        )
        result = replay(blocks, partial, [Target(2, ((b"b", b"7"),))], checkpoints, initial_table_size=self.SIZE)
        assert result.decoded[2] == ((b"a", b"7"),)
        assert result.broken_targets == (2,)


class TestReplayAgainstRealConnection:
    def test_replays_targets_from_a_checkpoint(self):
        encoder = Encoder()
        encoder.header_table_size = 256

        headers = [
            [("x-a", "1")],
            [("x-b", "2")],
            [("x-c", "3")],
            [("x-a", "1")],
            [("x-d", "4")],
        ]

        blocks = []
        states = {}
        for index, case in enumerate(headers):
            states[index] = encoder.header_table.snapshot()
            blocks.append(encoder.encode(case, huffman=False))
        states[len(headers)] = encoder.header_table.snapshot()

        checkpoints = [
            Checkpoint(position, states[position].stored_size, states[position])
            for position in states
        ]
        targets = [Target(3, ((b"x-a", b"1"),)), Target(4, ((b"x-d", b"4"),))]

        plan = ArchivePlanner([len(block) for block in blocks], checkpoints, targets).plan()
        result = replay(blocks, plan, targets, checkpoints, initial_table_size=256)

        assert result.ok is True
        assert set(result.reached) == set(plan.block_indices)

    def test_report_contains_broken_target_details(self):
        blocks = [b'\x82']
        plan = ArchivePlanner([1], [], [Target(0, ((b":method", b"GET"),))]).plan()
        result = replay(blocks, plan, [Target(0, ((b":method", b"POST"),))])

        assert result.ok is False
        assert result.broken_targets == (0,)
        assert result.targets[0] == (((b":method", b"GET"),), False)

    def test_missing_checkpoint_state_is_reported(self):
        blocks = [b'\x82']
        plan = ArchivePlanner([1], [], [Target(0)]).plan()

        result = replay(
            blocks,
            dataclasses.replace(plan, checkpoint_positions=(0,)),
            [Target(0)],
        )

        assert result.ok is False
        assert isinstance(result.error, ValueError)
        assert "no state" in str(result.error)

    def test_out_of_range_block_is_reported(self):
        blocks = [b'\x82']
        plan = ArchivePlanner([1], [], [Target(0)]).plan()

        result = replay(blocks, dataclasses.replace(plan, block_indices=(0, 1)), [Target(0)])
        assert result.ok is False
        assert isinstance(result.error, IndexError)
