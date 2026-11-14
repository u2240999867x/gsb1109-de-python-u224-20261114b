"""
plan_archive.py
~~~~~~~~~~~~~~~~

Given a recorded HPACK connection, choose the cheapest set of raw header blocks
and dynamic-table checkpoints that can be archived in order to reproduce a set
of target blocks in a blank environment.

Input (JSON, path as the only argument)::

    {
      "initial_table_size": 4096,           # optional, defaults to 4096
      "max_allowed_table_size": 4096,       # optional, defaults to initial size
      "blocks": ["<hex wire>", ...],        # raw header blocks, in order
      "checkpoints": [                      # optional; defaults to none
        {"position": 3, "size": 120},       # state immediately before block 3
        {"position": 7, "size": 140}        # size optional: derived from state
      ],
      "targets": [
        {"block": 4, "fields": [["x-a", "1"]]},
        {"block": 9}                        # fields optional: taken from the recording
      ]
    }

Output (JSON)::

    {
      "plan": {"blocks": [0, 3, 4], "checkpoints": [3], "total_size": 311},
      "replay": {"ok": true, "targets": {...}},
      "explanation": "targets: ...\n..."
    }

A checkpoint at ``position p`` is the dynamic table *before* block ``p`` is
decoded.  Omitting a checkpoint means the only way to start is at position 0.
The planner archives every block in each replay interval, not only the blocks
that mention an indexed field: an insertion that is never referenced can still
evict entries and shift indices, so the plan is verified by an actual decode
rather than by collecting referenced fields.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from hpack import ArchivePlanner, Checkpoint, Decoder, Target, replay
from hpack.table import HeaderTable


def record(
    blocks: list[bytes],
    initial_table_size: int,
    max_allowed_table_size: int,
) -> tuple[list[Any], dict[int, tuple[tuple[bytes, bytes], ...]]]:
    """
    Decode the whole connection once, capturing the table before every block
    and the fields each block produced.
    """
    decoder = Decoder()
    decoder.header_table_size = initial_table_size
    decoder.max_allowed_table_size = max_allowed_table_size

    states = []
    fields = {}
    for index, block in enumerate(blocks):
        states.append(decoder.header_table.snapshot())
        decoded = decoder.decode(block, raw=True)
        fields[index] = tuple((bytes(name), bytes(value)) for name, value in decoded)
    states.append(decoder.header_table.snapshot())
    return states, fields


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("input", help="path to the recording JSON")
    args = parser.parse_args(argv)

    with open(args.input, encoding="utf-8") as handle:
        document = json.load(handle)

    initial_table_size = int(document.get("initial_table_size", HeaderTable.DEFAULT_SIZE))
    max_allowed_table_size = int(document.get("max_allowed_table_size", initial_table_size))
    blocks = [bytes.fromhex(wire) for wire in document["blocks"]]
    states, recorded_fields = record(blocks, initial_table_size, max_allowed_table_size)

    checkpoints = []
    for item in document.get("checkpoints", []):
        position = int(item["position"])
        state = states[position]
        checkpoints.append(Checkpoint(position, int(item.get("size", state.stored_size)), state))

    targets = []
    for item in document["targets"]:
        index = int(item["block"])
        if "fields" in item:
            fields = tuple((name.encode("utf-8"), value.encode("utf-8")) for name, value in item["fields"])
        else:
            fields = recorded_fields[index]
        targets.append(Target(index, fields))

    planner = ArchivePlanner([len(block) for block in blocks], checkpoints, targets)
    plan = planner.plan()
    result = replay(
        blocks,
        plan,
        targets,
        checkpoints,
        initial_table_size=initial_table_size,
        max_allowed_table_size=max_allowed_table_size,
    )

    output = {
        "plan": {
            "blocks": list(plan.block_indices),
            "checkpoints": list(plan.checkpoint_positions),
            "total_size": plan.total_size,
        },
        "replay": {
            "ok": result.ok,
            "error": None if result.error is None else type(result.error).__name__,
            "targets": {
                str(index): {
                    "fields": None if actual is None else [[name.decode(), value.decode()] for name, value in actual],
                    "match": matches,
                }
                for index, (actual, matches) in result.targets.items()
            },
        },
        "explanation": planner.explain(plan),
    }
    json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
