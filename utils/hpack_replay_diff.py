"""
Offline HPACK replay and byte-level differential accounting.

Two fixture directories produced by two encoding strategies are replayed over
the same ordered header sequences (tests/test_fixtures/raw-data), starting from
the same initial header table: the RFC 7541 static table plus an empty dynamic
table with a maximum size of 4096 octets.

Every wire block is parsed into its HPACK representations and each octet is
attributed to one of:

* table size update (RFC 7541 section 6.3)
* indexed header field (section 6.2.2)
* literal header field, with incremental indexing / without indexing /
  never-indexed (section 6.2.3). Literal octets are split further into
  representation overhead (prefix bits, name index, length prefixes),
  Huffman-coded string payload and plain string payload, separately for the
  field name and the field value.

The two sides are then reconciled block by block and field by field so that a
byte saved on one representation is kept together with the cost it may cause on
a later reference (for example an eviction turning a one-octet indexed
reference into a long literal). Decoded ordered fields and never-indexed
(sensitive) claims are compared as well, so a content change can never be
mistaken for a traffic saving.

Usage::

    python utils/hpack_replay_diff.py            # print the ledger
    python utils/hpack_replay_diff.py --out DIR  # also write markdown + JSON
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
from binascii import unhexlify

from hpack.hpack import decode_integer
from hpack.huffman_table import decode_huffman
from hpack.struct import HeaderTuple, NeverIndexedHeaderTuple
from hpack.table import HeaderTable, table_entry_size

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "tests", "test_fixtures")

STRATEGY_A = "nghttp2"
STRATEGY_B = "nghttp2-change-table-size"

REP_TABLE_SIZE_UPDATE = "table-size-update"
REP_INDEXED = "indexed"
REP_LITERAL_INCREMENTAL = "literal-incremental"
REP_LITERAL_NO_INDEX = "literal-no-index"
REP_LITERAL_NEVER = "literal-never"

LITERAL_REPS = (
    REP_LITERAL_INCREMENTAL,
    REP_LITERAL_NO_INDEX,
    REP_LITERAL_NEVER,
)

CATEGORIES = (
    "table_size_update",
    "indexed",
    "literal_overhead",
    "huffman_name",
    "huffman_value",
    "plain_name",
    "plain_value",
)


@dataclasses.dataclass
class Record:
    """One parsed HPACK representation within a header block."""

    kind: str
    start: int
    end: int
    name: bytes = b""
    value: bytes = b""
    sensitive: bool = False
    name_indexed: bool | None = None
    name_huffman: bool | None = None
    value_huffman: bool | None = None
    name_wire_len: int = 0
    value_wire_len: int = 0
    table_size: int | None = None
    evicted: list[tuple[bytes, bytes]] = dataclasses.field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return self.end - self.start

    def category_bytes(self) -> dict[str, int]:
        out = {key: 0 for key in CATEGORIES}
        if self.kind == REP_TABLE_SIZE_UPDATE:
            out["table_size_update"] = self.total_bytes
            return out
        if self.kind == REP_INDEXED:
            out["indexed"] = self.total_bytes
            return out

        payload = 0
        if self.name_huffman is not None:
            out["huffman_name" if self.name_huffman else "plain_name"] = self.name_wire_len
            payload += self.name_wire_len
        out["huffman_value" if self.value_huffman else "plain_value"] = self.value_wire_len
        payload += self.value_wire_len
        out["literal_overhead"] = self.total_bytes - payload
        return out


def replay_block(wire: bytes, table: HeaderTable) -> list[Record]:
    """Parse one HPACK block, mutating ``table`` exactly like a decoder would."""
    records: list[Record] = []
    pos = 0
    data = memoryview(wire)
    while pos < len(wire):
        start = pos
        first = wire[pos]

        if first & 0x80:
            index, consumed = decode_integer(data[pos:], 7)
            name, value = table.get_by_index(index)
            pos += consumed
            records.append(
                Record(REP_INDEXED, start, pos, name, value),
            )
            continue

        if (first & 0xE0) == 0x20:
            new_size, consumed = decode_integer(data[pos:], 5)
            before = list(table.dynamic_entries)
            table.maxsize = new_size
            evicted = _evicted_since(before, table.dynamic_entries)
            pos += consumed
            records.append(
                Record(
                    REP_TABLE_SIZE_UPDATE,
                    start,
                    pos,
                    table_size=new_size,
                    evicted=evicted,
                ),
            )
            continue

        if first & 0x40:
            kind = REP_LITERAL_INCREMENTAL
            sensitive = False
            prefix_bits = 6
            should_index = True
        elif first & 0x10:
            kind = REP_LITERAL_NEVER
            sensitive = True
            prefix_bits = 4
            should_index = False
        else:
            kind = REP_LITERAL_NO_INDEX
            sensitive = False
            prefix_bits = 4
            should_index = False

        indexed_name = first & ((1 << prefix_bits) - 1)
        name_indexed = bool(indexed_name)
        name_huffman: bool | None = None
        name_wire_len = 0
        if indexed_name:
            index, consumed = decode_integer(data[pos:], prefix_bits)
            name, _ = table.get_by_index(index)
            pos += consumed
        else:
            pos += 1  # literal name flag byte
            length, consumed = decode_integer(data[pos:], 7)
            name_huffman = bool(wire[pos] & 0x80)
            pos += consumed
            name_payload = bytes(wire[pos:pos + length])
            name_wire_len = length
            name = decode_huffman(name_payload) if name_huffman else name_payload
            pos += length

        value_length, consumed = decode_integer(data[pos:], 7)
        value_huffman = bool(wire[pos] & 0x80)
        pos += consumed
        value_payload = bytes(wire[pos:pos + value_length])
        value_wire_len = value_length
        value = decode_huffman(value_payload) if value_huffman else value_payload
        pos += value_length

        before = list(table.dynamic_entries)
        if should_index:
            table.add(name, value)
        evicted = _evicted_since(before, table.dynamic_entries)

        record = Record(
            kind,
            start,
            pos,
            name,
            value,
            sensitive=sensitive,
            name_indexed=name_indexed,
            name_huffman=name_huffman,
            value_huffman=value_huffman,
            name_wire_len=name_wire_len,
            value_wire_len=value_wire_len,
            evicted=evicted,
        )
        records.append(record)

    return records


def _evicted_since(before: list, after) -> list[tuple[bytes, bytes]]:
    after_set = set(after)
    return [entry for entry in before if entry not in after_set]


def record_categories(record: Record) -> dict[str, int]:
    return record.category_bytes()


def replay_story(path: str) -> dict:
    """Replay every block of a story file and return a detailed trace."""
    story = json.load(open(path, encoding="utf-8"))
    table = HeaderTable()
    blocks = []
    for case in story["cases"]:
        wire = unhexlify(case["wire"])
        expected = [
            (name.encode("utf-8"), value.encode("utf-8"))
            for header in case["headers"]
            for name, value in header.items()
        ]
        records = replay_block(wire, table)
        decoded = [
            (NeverIndexedHeaderTuple(r.name, r.value) if r.sensitive else HeaderTuple(r.name, r.value))
            for r in records
            if r.kind != REP_TABLE_SIZE_UPDATE
        ]
        assert [tuple(h) for h in decoded] == expected, (
            f"replay mismatch in {path} case {case.get('seqno')}"
        )
        assert sum(r.total_bytes for r in records) == len(wire)
        blocks.append(
            {
                "seqno": case.get("seqno"),
                "wire_hex": case["wire"],
                "wire_bytes": len(wire),
                "header_table_size_signal": case.get("header_table_size"),
                "expected_headers": expected,
                "records": records,
                "decoded": decoded,
                "dynamic_table_size_after": table._current_size,
                "dynamic_table_maxsize_after": table.maxsize,
                "dynamic_entries_after": list(table.dynamic_entries),
            }
        )
    return {"path": path, "blocks": blocks}


def block_totals(records: list[Record]) -> dict[str, int]:
    totals = {key: 0 for key in CATEGORIES}
    for record in records:
        for key, value in record_categories(record).items():
            totals[key] += value
    return totals


def field_records(records: list[Record]) -> list[Record]:
    return [r for r in records if r.kind != REP_TABLE_SIZE_UPDATE]


def compare_stories(story_name: str) -> dict:
    path_a = os.path.join(FIXTURES, STRATEGY_A, story_name)
    path_b = os.path.join(FIXTURES, STRATEGY_B, story_name)
    if not os.path.exists(path_b):
        return {"story": story_name, "skipped": "missing in strategy B"}

    trace_a = replay_story(path_a)
    trace_b = replay_story(path_b)
    assert len(trace_a["blocks"]) == len(trace_b["blocks"])

    block_rows = []
    transitions: dict[str, int] = {}
    transition_costs: dict[str, dict[str, int]] = {}
    sensitive_mismatches = []
    later_cost_examples = []

    for block_a, block_b in zip(trace_a["blocks"], trace_b["blocks"]):
        assert block_a["expected_headers"] == block_b["expected_headers"]
        fields_a = field_records(block_a["records"])
        fields_b = field_records(block_b["records"])
        assert len(fields_a) == len(fields_b)

        for index, (ra, rb) in enumerate(zip(fields_a, fields_b)):
            assert (ra.name, ra.value) == (rb.name, rb.value)
            if ra.sensitive != rb.sensitive:
                sensitive_mismatches.append(
                    {
                        "seqno": block_a["seqno"],
                        "field_index": index,
                        "name": ra.name.decode("utf-8", "replace"),
                        "value": ra.value.decode("utf-8", "replace"),
                        "a_sensitive": ra.sensitive,
                        "b_sensitive": rb.sensitive,
                    },
                )
            transition = f"{ra.kind} -> {rb.kind}"
            transitions[transition] = transitions.get(transition, 0) + 1
            bucket = transition_costs.setdefault(
                transition,
                {"count": 0, "a_bytes": 0, "b_bytes": 0, "delta": 0},
            )
            bucket["count"] += 1
            bucket["a_bytes"] += ra.total_bytes
            bucket["b_bytes"] += rb.total_bytes
            bucket["delta"] += rb.total_bytes - ra.total_bytes
            if ra.kind == REP_INDEXED and rb.kind in LITERAL_REPS:
                later_cost_examples.append(
                    {
                        "story": story_name,
                        "seqno": block_a["seqno"],
                        "field_index": index,
                        "name": ra.name.decode("utf-8", "replace"),
                        "value": ra.value.decode("utf-8", "replace"),
                        "a_bytes": ra.total_bytes,
                        "b_bytes": rb.total_bytes,
                        "delta_bytes": rb.total_bytes - ra.total_bytes,
                        "b_representation": rb.kind,
                        "b_value_huffman": rb.value_huffman,
                    },
                )

        totals_a = block_totals(block_a["records"])
        totals_b = block_totals(block_b["records"])
        block_rows.append(
            {
                "seqno": block_a["seqno"],
                "a_bytes": block_a["wire_bytes"],
                "b_bytes": block_b["wire_bytes"],
                "delta": block_b["wire_bytes"] - block_a["wire_bytes"],
                "a_categories": totals_a,
                "b_categories": totals_b,
                "category_delta": {
                    key: totals_b[key] - totals_a[key] for key in CATEGORIES
                },
                "a_size_updates": [
                    {
                        "new_size": r.table_size,
                        "bytes": r.total_bytes,
                        "evicted": [
                            [n.decode("utf-8", "replace"), v.decode("utf-8", "replace")]
                            for n, v in r.evicted
                        ],
                    }
                    for r in block_a["records"]
                    if r.kind == REP_TABLE_SIZE_UPDATE
                ],
                "b_size_updates": [
                    {
                        "new_size": r.table_size,
                        "bytes": r.total_bytes,
                        "evicted": [
                            [n.decode("utf-8", "replace"), v.decode("utf-8", "replace")]
                            for n, v in r.evicted
                        ],
                    }
                    for r in block_b["records"]
                    if r.kind == REP_TABLE_SIZE_UPDATE
                ],
            },
        )

    return {
        "story": story_name,
        "blocks": block_rows,
        "transitions": transitions,
        "transition_costs": transition_costs,
        "sensitive_mismatches": sensitive_mismatches,
        "later_cost_examples": later_cost_examples,
        "trace_a": trace_a,
        "trace_b": trace_b,
    }


def aggregate(comparisons: list[dict]) -> dict:
    totals = {
        side: {key: 0 for key in ("bytes", *CATEGORIES)}
        for side in ("a", "b")
    }
    rep_counts = {"a": {}, "b": {}}
    transitions: dict[str, int] = {}
    transition_costs: dict[str, dict[str, int]] = {}
    size_updates = {"a": [], "b": []}
    evictions = {
        "a": {"update_driven": 0, "add_driven": 0},
        "b": {"update_driven": 0, "add_driven": 0},
    }
    story_rows = []

    for comp in comparisons:
        if comp.get("skipped"):
            continue
        story_delta = 0
        for block in comp["blocks"]:
            totals["a"]["bytes"] += block["a_bytes"]
            totals["b"]["bytes"] += block["b_bytes"]
            story_delta += block["delta"]
            for key in CATEGORIES:
                totals["a"][key] += block["a_categories"][key]
                totals["b"][key] += block["b_categories"][key]
            for update in block["a_size_updates"]:
                size_updates["a"].append({"story": comp["story"], **update})
            for update in block["b_size_updates"]:
                size_updates["b"].append({"story": comp["story"], **update})
        for side, prefix in (("a", "trace_a"), ("b", "trace_b")):
            for blk in comp[prefix]["blocks"]:
                for record in blk["records"]:
                    rep_counts[side][record.kind] = (
                        rep_counts[side].get(record.kind, 0) + 1
                    )
                    cause = "update_driven" if record.kind == REP_TABLE_SIZE_UPDATE else "add_driven"
                    evictions[side][cause] += len(record.evicted)
        for transition, count in comp["transitions"].items():
            transitions[transition] = transitions.get(transition, 0) + count
        for transition, bucket in comp["transition_costs"].items():
            merged = transition_costs.setdefault(
                transition,
                {"count": 0, "a_bytes": 0, "b_bytes": 0, "delta": 0},
            )
            for key in merged:
                merged[key] += bucket[key]
        story_rows.append(
            {
                "story": comp["story"],
                "a_bytes": sum(b["a_bytes"] for b in comp["blocks"]),
                "b_bytes": sum(b["b_bytes"] for b in comp["blocks"]),
                "delta": story_delta,
                "blocks": len(comp["blocks"]),
            },
        )

    return {
        "totals": totals,
        "rep_counts": rep_counts,
        "transitions": dict(sorted(transitions.items())),
        "transition_costs": dict(sorted(transition_costs.items())),
        "size_updates": size_updates,
        "evictions": evictions,
        "story_rows": story_rows,
    }


def render_markdown(comparisons: list[dict], summary: dict, cross_checks: dict) -> str:
    lines = []
    lines.append("# HPACK 双策略离线重放差异账\n")
    lines.append(
        f"- 策略 A：`tests/test_fixtures/{STRATEGY_A}`（nghttp2，动态表上限固定 4096，"
        "全程不发表大小更新）",
    )
    lines.append(
        f"- 策略 B：`tests/test_fixtures/{STRATEGY_B}`（同一 nghttp2 编码器、同一初始表，"
        "按 case 发送动态表大小更新并触发驱逐）",
    )
    lines.append(
        "- 输入序列：`tests/test_fixtures/raw-data` 中 31 个 story（00–30，31 在 B 侧缺失），"
        "两侧字段序列逐字段相同",
    )
    lines.append("- 初始状态：RFC 7541 静态表 + 空动态表，maxsize=4096\n")

    lines.append("## 1. 一致性核对\n")
    lines.append(f"- 重放块数：{cross_checks['blocks_replayed']}（A/B 各 {cross_checks['blocks_replayed']}）")
    lines.append(
        f"- 字节守恒：每个块解析表示的字节之和等于 wire 长度，"
        f"违例 {cross_checks['byte_conservation_violations']} 处",
    )
    lines.append(
        f"- 有序字段：两侧解回字段与 raw-data 声明完全一致，违例 "
        f"{cross_checks['field_mismatches']} 处",
    )
    lines.append(
        f"- 敏感声明（never-indexed）：A/B 两侧字段级声明不一致 "
        f"{cross_checks['sensitive_mismatches']} 处",
    )
    lines.append(
        f"- 与库内 `hpack.Decoder` 交叉解码：{cross_checks['library_decoder_mismatches']} 处不一致",
    )
    lines.append(
        "- 勾稽关系：字段级实际成本差额 + 表大小更新差额 = wire 总差额，"
        f"违例 {cross_checks['reconciliation_violations']} 处\n",
    )

    totals = summary["totals"]
    lines.append("## 2. 字节总账（31 个 story 累计）\n")
    lines.append("| 类别 | A 字节 | B 字节 | B−A |")
    lines.append("|---|---:|---:|---:|")
    lines.append(
        f"| **wire 总量** | {totals['a']['bytes']} | {totals['b']['bytes']} "
        f"| {totals['b']['bytes'] - totals['a']['bytes']} |",
    )
    labels = {
        "table_size_update": "表大小更新",
        "indexed": "索引表示（整字段引用）",
        "literal_overhead": "字面量表示开销（前缀/长度/名索引）",
        "huffman_name": "Huffman 字符串（字段名）",
        "huffman_value": "Huffman 字符串（字段值）",
        "plain_name": "明文字符串（字段名）",
        "plain_value": "明文字符串（字段值）",
    }
    for key in CATEGORIES:
        lines.append(
            f"| {labels[key]} | {totals['a'][key]} | {totals['b'][key]} "
            f"| {totals['b'][key] - totals['a'][key]} |",
        )
    lines.append("")

    lines.append("### 表示方式出现次数\n")
    lines.append("| 表示 | A 次数 | B 次数 |")
    lines.append("|---|---:|---:|")
    kinds = (
        REP_TABLE_SIZE_UPDATE,
        REP_INDEXED,
        REP_LITERAL_INCREMENTAL,
        REP_LITERAL_NO_INDEX,
        REP_LITERAL_NEVER,
    )
    kind_labels = {
        REP_TABLE_SIZE_UPDATE: "表大小更新",
        REP_INDEXED: "索引字段",
        REP_LITERAL_INCREMENTAL: "字面量（增量索引）",
        REP_LITERAL_NO_INDEX: "字面量（不索引）",
        REP_LITERAL_NEVER: "字面量（never-indexed）",
    }
    for kind in kinds:
        lines.append(
            f"| {kind_labels[kind]} | {summary['rep_counts']['a'].get(kind, 0)} "
            f"| {summary['rep_counts']['b'].get(kind, 0)} |",
        )
    lines.append("")

    lines.append("## 3. 同字段表示迁移矩阵（A → B）\n")
    lines.append("| 迁移 | 次数字段数 | A 字节 | B 字节 | B−A |")
    lines.append("|---|---:|---:|---:|---:|")
    field_delta_sum = 0
    for transition, bucket in summary["transition_costs"].items():
        field_delta_sum += bucket["delta"]
        lines.append(
            f"| `{transition}` | {bucket['count']} | {bucket['a_bytes']} "
            f"| {bucket['b_bytes']} | {bucket['delta']} |",
        )
    lines.append("")
    lines.append(
        f"- 字段级实际成本差额合计（不含表大小更新）：B−A = **{field_delta_sum}** 字节",
    )
    lines.append(
        f"- 表大小更新线上成本：B−A = **{totals['b']['table_size_update'] - totals['a']['table_size_update']}** 字节",
    )
    lines.append(
        f"- 两项勾稽：{field_delta_sum} + "
        f"{totals['b']['table_size_update'] - totals['a']['table_size_update']} = "
        f"**{field_delta_sum + totals['b']['table_size_update'] - totals['a']['table_size_update']}**，"
        f"应等于 wire 总差额 {totals['b']['bytes'] - totals['a']['bytes']}\n",
    )

    lines.append("## 4. 分 story 差额\n")
    lines.append("| story | 块数 | A 字节 | B 字节 | B−A |")
    lines.append("|---|---:|---:|---:|---:|")
    for row in summary["story_rows"]:
        lines.append(
            f"| {row['story']} | {row['blocks']} | {row['a_bytes']} "
            f"| {row['b_bytes']} | {row['delta']} |",
        )
    lines.append("")

    lines.append("## 5. 表大小更新与驱逐明细\n")
    lines.append("### 驱逐总量（后续引用变成长字面量的根因）\n")
    lines.append("| 策略 | 表大小更新触发的驱逐 | 插入新条目触发的驱逐 |")
    lines.append("|---|---:|---:|")
    lines.append(
        f"| A | {summary['evictions']['a']['update_driven']} "
        f"| {summary['evictions']['a']['add_driven']} |",
    )
    lines.append(
        f"| B | {summary['evictions']['b']['update_driven']} "
        f"| {summary['evictions']['b']['add_driven']} |\n",
    )
    for side in ("a", "b"):
        updates = summary["size_updates"][side]
        lines.append(f"### 策略 {side.upper()}（{len(updates)} 次更新，线上字节 "
                     f"{sum(u['bytes'] for u in updates)}）\n")
        if updates:
            lines.append("| story | 新上限 | 线上字节 | 本次驱逐条目数 |")
            lines.append("|---|---:|---:|---:|")
            for update in updates:
                lines.append(
                    f"| {update['story']} | {update['new_size']} "
                    f"| {update['bytes']} | {len(update['evicted'])} |",
                )
        lines.append("")

    examples = [
        example
        for comp in comparisons
        if not comp.get("skipped")
        for example in comp["later_cost_examples"]
    ]
    lines.append("## 6. 后续引用成本：索引被驱逐为长字面量的字段\n")
    lines.append(
        f"共 {len(examples)} 个字段在 A 侧为索引表示、在 B 侧退化为字面量。"
        "下表列出字节差额最大的 10 个（保留的是该次引用的实际线上成本）：\n",
    )
    lines.append("| story | seqno | 字段 | A 字节 | B 字节 | B−A | B 侧表示 | B 侧值 Huffman |")
    lines.append("|---|---:|---|---:|---:|---:|---|---|")
    for example in sorted(examples, key=lambda item: -item["delta_bytes"])[:10]:
        field = f"{example['name']}: {example['value'][:48]}"
        lines.append(
            f"| {example['story']} | {example['seqno']} | `{field}` "
            f"| {example['a_bytes']} | {example['b_bytes']} "
            f"| {example['delta_bytes']} | {example['b_representation']} "
            f"| {example['b_value_huffman']} |",
        )
    lines.append("")

    return "\n".join(lines)


def cross_check_against_library(comparisons: list[dict]) -> int:
    from hpack import Decoder

    mismatches = 0
    for comp in comparisons:
        if comp.get("skipped"):
            continue
        for side, prefix in (("a", "trace_a"), ("b", "trace_b")):
            decoder = Decoder()
            for block in comp[prefix]["blocks"]:
                signal = block["header_table_size_signal"]
                if signal is not None:
                    decoder.header_table_size = signal
                decoded = decoder.decode(unhexlify(block["wire_hex"]), raw=True)
                if [tuple(h) for h in decoded] != block["expected_headers"]:
                    mismatches += 1
    return mismatches


def serialize(comparisons: list[dict], summary: dict, cross_checks: dict) -> dict:
    def block_dump(block):
        return {
            "seqno": block["seqno"],
            "a_bytes": block["a_bytes"],
            "b_bytes": block["b_bytes"],
            "delta": block["delta"],
            "category_delta": block["category_delta"],
            "a_size_updates": block["a_size_updates"],
            "b_size_updates": block["b_size_updates"],
        }

    return {
        "strategies": {"a": STRATEGY_A, "b": STRATEGY_B},
        "cross_checks": cross_checks,
        "summary": {
            "totals": summary["totals"],
            "rep_counts": summary["rep_counts"],
            "transitions": summary["transitions"],
            "transition_costs": summary["transition_costs"],
            "evictions": summary["evictions"],
            "story_rows": summary["story_rows"],
        },
        "stories": [
            {
                "story": comp["story"],
                "skipped": comp.get("skipped", False),
                "blocks": [block_dump(block) for block in comp.get("blocks", [])],
                "sensitive_mismatches": comp.get("sensitive_mismatches", []),
                "later_cost_examples": comp.get("later_cost_examples", []),
            }
            for comp in comparisons
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="optional output directory for the ledger files")
    args = parser.parse_args()

    story_names = sorted(
        name
        for name in os.listdir(os.path.join(FIXTURES, STRATEGY_A))
        if name.endswith(".json")
    )
    comparisons = [compare_stories(name) for name in story_names]
    compared = [comp for comp in comparisons if not comp.get("skipped")]
    summary = aggregate(compared)

    blocks_replayed = sum(len(comp["blocks"]) for comp in compared)
    sensitive_mismatches = sum(
        len(comp["sensitive_mismatches"]) for comp in compared
    )
    conservation_violations = 0
    for comp in compared:
        for block in comp["blocks"]:
            if sum(block["a_categories"].values()) != block["a_bytes"]:
                conservation_violations += 1
            if sum(block["b_categories"].values()) != block["b_bytes"]:
                conservation_violations += 1
    field_delta = sum(
        bucket["delta"] for bucket in summary["transition_costs"].values()
    )
    size_update_delta = (
        summary["totals"]["b"]["table_size_update"]
        - summary["totals"]["a"]["table_size_update"]
    )
    reconciliation_violations = (
        field_delta + size_update_delta
        != summary["totals"]["b"]["bytes"] - summary["totals"]["a"]["bytes"]
    )
    cross_checks = {
        "blocks_replayed": blocks_replayed,
        "byte_conservation_violations": conservation_violations,
        "field_mismatches": 0,
        "sensitive_mismatches": sensitive_mismatches,
        "library_decoder_mismatches": cross_check_against_library(compared),
        "reconciliation_violations": int(reconciliation_violations),
        "field_level_delta_bytes": field_delta,
        "table_size_update_delta_bytes": size_update_delta,
    }

    markdown = render_markdown(comparisons, summary, cross_checks)
    print(markdown)

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        md_path = os.path.join(args.out, "hpack_replay_ledger.md")
        json_path = os.path.join(args.out, "hpack_replay_ledger.json")
        with open(md_path, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(serialize(comparisons, summary, cross_checks), handle, ensure_ascii=False, indent=2)
        print(f"\nledger written to {md_path} and {json_path}")


if __name__ == "__main__":
    main()
