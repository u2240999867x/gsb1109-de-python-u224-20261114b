#!/usr/bin/env python3
"""日终对账工具：解 ISO 8583 脱敏流水，按机构/币种/借贷方向汇总，
对照清算方控制数（笔数 + 整数金额），从导入端候选样本清单中找出
能同时抹平所有差额的最小改动集。

规则：
- 每个候选只能按其标注（补=add / 减=remove）使用一次；
- 同组存在多组同样最小的解（如两笔同额流水各自都能抹平差额）时，
  全部列出，带来源编号；
- 金额能平但笔数平不了的分组，保留差额留账，不强行凑数。

输入：
  --log        脱敏流水：CSV（表头 id,hex）或每行一条 hex 报文（id=行号）
  --control    控制数 CSV：institution,currency,direction,count,amount
  --candidates 候选清单 CSV：id,institution,currency,direction,amount,action
               direction ∈ {借,贷,debit,credit,D,C}，action ∈ {补,减,add,remove}
  --spec       报文规范：default_ascii（默认）或 default
  --amount-scale  金额缩放（默认 1，即报文 4 域已是整数金额）
  --selftest   用内置合成数据演示三种情形
"""

import argparse
import csv
import itertools
import sys
from collections import defaultdict

import iso8583
import iso8583.specs

DEBIT = "借"
CREDIT = "贷"

# 处理码（3 域）前两位 20-29 为贷记类（退货/存款等），其余为借记类
CREDIT_PC_PREFIXES = {str(n) for n in range(20, 30)}

DIRECTION_ALIASES = {
    "借": DEBIT, "debit": DEBIT, "d": DEBIT,
    "贷": CREDIT, "credit": CREDIT, "c": CREDIT,
}

ACTION_ALIASES = {"补": "add", "add": "add", "减": "remove", "remove": "remove"}


def norm_direction(value):
    key = value.strip().lower()
    if key in DIRECTION_ALIASES:
        return DIRECTION_ALIASES[key]
    raise ValueError(f"无法识别的借贷方向: {value!r}")


def norm_action(value):
    key = value.strip().lower()
    if key in ACTION_ALIASES:
        return ACTION_ALIASES[key]
    raise ValueError(f"无法识别的候选动作: {value!r}")


def tx_direction(mti, processing_code):
    """由 MTI 与处理码推借贷方向；冲正类报文（x4xx）方向取反。"""
    direction = CREDIT if processing_code[:2] in CREDIT_PC_PREFIXES else DEBIT
    if len(mti) >= 2 and mti[1] == "4":
        direction = DEBIT if direction == CREDIT else CREDIT
    return direction


def read_log(path, spec, amount_scale):
    """解出流水，返回 [(来源编号, 机构, 币种, 方向, 整数金额)]。"""
    records = []
    with open(path, newline="", encoding="utf-8") as fh:
        sample = fh.read(256)
        fh.seek(0)
        if "," in sample.splitlines()[0]:
            rows = ((r["id"].strip(), r["hex"].strip()) for r in csv.DictReader(fh))
        else:
            rows = ((str(i), line.strip()) for i, line in enumerate(fh, 1) if line.strip())
        for source_id, hex_str in rows:
            decoded, _ = iso8583.decode(bytes.fromhex(hex_str), spec)
            amount = int(decoded["4"]) // amount_scale
            records.append(
                (
                    source_id,
                    decoded["32"].strip(),
                    decoded["49"].strip(),
                    tx_direction(decoded["t"], decoded["3"]),
                    amount,
                )
            )
    return records


def read_control(path):
    totals = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (
                row["institution"].strip(),
                row["currency"].strip(),
                norm_direction(row["direction"]),
            )
            totals[key] = (int(row["count"]), int(row["amount"]))
    return totals


def read_candidates(path):
    candidates = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            candidates.append(
                {
                    "id": row["id"].strip(),
                    "key": (
                        row["institution"].strip(),
                        row["currency"].strip(),
                        norm_direction(row["direction"]),
                    ),
                    "amount": int(row["amount"]),
                    "action": norm_action(row["action"]),
                }
            )
    return candidates


def aggregate(records):
    totals = defaultdict(lambda: [0, 0])
    for _, inst, ccy, direction, amount in records:
        bucket = totals[(inst, ccy, direction)]
        bucket[0] += 1
        bucket[1] += amount
    return totals


def signed_effect(candidate):
    sign = 1 if candidate["action"] == "add" else -1
    return sign, sign * candidate["amount"]


def find_minimal_solutions(candidates, dcount, damount, match_count=True):
    """按组合大小递增枚举，返回能抹平差额的所有最小候选子集。

    match_count=False 时只要求金额平（用于金额平、笔数留账的情形）。
    """
    for size in range(0, len(candidates) + 1):
        solutions = []
        for combo in itertools.combinations(candidates, size):
            count_delta = sum(signed_effect(c)[0] for c in combo)
            amount_delta = sum(signed_effect(c)[1] for c in combo)
            if amount_delta != damount:
                continue
            if match_count and count_delta != dcount:
                continue
            solutions.append(combo)
        if solutions:
            return solutions
    return []


def reconcile(actual, control, candidates):
    by_group = defaultdict(list)
    for cand in candidates:
        by_group[cand["key"]].append(cand)

    groups = sorted(set(actual) | set(control) | set(by_group))
    report = []
    for key in groups:
        act_count, act_amount = actual.get(key, (0, 0))
        ctl_count, ctl_amount = control.get(key, (0, 0))
        dcount = ctl_count - act_count
        damount = ctl_amount - act_amount
        entry = {
            "key": key,
            "actual": (act_count, act_amount),
            "control": (ctl_count, ctl_amount),
            "diff": (dcount, damount),
            "solutions": [],
            "residual": None,
        }
        if dcount == 0 and damount == 0:
            report.append(entry)
            continue
        group_candidates = by_group.get(key, [])
        solutions = find_minimal_solutions(group_candidates, dcount, damount)
        if solutions:
            entry["solutions"] = solutions
        else:
            # 笔数金额无法同时平：尝试只平金额，笔数差额留账
            amount_only = find_minimal_solutions(
                group_candidates, dcount, damount, match_count=False
            )
            if amount_only:
                best = amount_only[0]
                applied = sum(signed_effect(c)[0] for c in best)
                entry["solutions"] = amount_only
                entry["residual"] = (dcount - applied, 0)
            else:
                entry["residual"] = (dcount, damount)
        report.append(entry)
    return report


def print_report(report):
    print("== 分组对账结果 ==")
    outstanding = []
    for entry in report:
        inst, ccy, direction = entry["key"]
        ac, aa = entry["actual"]
        cc, ca = entry["control"]
        dc, da = entry["diff"]
        head = f"[{inst} | {ccy} | {direction}] 控制 {cc}笔/{ca}  实际 {ac}笔/{aa}"
        if dc == 0 and da == 0:
            print(f"{head}  → 平")
            continue
        print(f"{head}  → 差 {dc:+d}笔/{da:+d}")
        shown = [s for s in entry["solutions"] if s or entry["residual"] is None]
        for i, solution in enumerate(shown, 1):
            parts = [
                f"{'补' if c['action'] == 'add' else '减'} {c['id']}({c['amount']})"
                for c in solution
            ]
            label = f"  方案{i}" if len(shown) > 1 else "  方案"
            print(f"{label}: " + "，".join(parts) if parts else f"{label}: （无需调整）")
        if entry["residual"] is not None:
            rc, ra = entry["residual"]
            outstanding.append((entry["key"], rc, ra))
            print(f"  留账: {rc:+d}笔/{ra:+d}（候选无法同时抹平，差额挂账）")
    if outstanding:
        print("\n== 未平分组（留账） ==")
        for (inst, ccy, direction), rc, ra in outstanding:
            print(f"[{inst} | {ccy} | {direction}] 剩余差额 {rc:+d}笔/{ra:+d}")


def selftest():
    spec = iso8583.specs.default_ascii

    def make_msg(mti, pc, amount, inst, ccy, pan="123456******7890"):
        decoded = {
            "t": mti,
            "2": pan,
            "3": pc,
            "4": f"{amount:012d}",
            "7": "1114000000",
            "11": "000001",
            "32": inst,
            "49": ccy,
        }
        raw, _ = iso8583.encode(decoded, spec)
        return raw.hex()

    log_lines = [
        make_msg("0200", "000000", 100, "INST1", "156"),
        make_msg("0200", "000000", 200, "INST1", "156"),
        make_msg("0200", "000000", 300, "INST1", "156"),
        make_msg("0200", "000000", 400, "INST1", "156"),
        make_msg("0200", "200000", 500, "INST1", "156"),  # 贷记，疑重复
        make_msg("0200", "000000", 700, "INST2", "840"),
    ]
    control = {
        ("INST1", "156", DEBIT): (5, 1800),   # 差 +1笔/+800，两候选各自可平
        ("INST1", "156", CREDIT): (0, 0),     # 差 -1笔/-500，疑重复记账
        ("INST2", "840", DEBIT): (2, 700),    # 金额平、笔数差 +1 → 留账
    }
    candidates = [
        {"id": "CAND-01", "key": ("INST1", "156", DEBIT), "amount": 800, "action": "add"},
        {"id": "CAND-02", "key": ("INST1", "156", DEBIT), "amount": 800, "action": "add"},
        {"id": "CAND-03", "key": ("INST1", "156", CREDIT), "amount": 500, "action": "remove"},
    ]

    records = []
    for i, hex_str in enumerate(log_lines, 1):
        decoded, _ = iso8583.decode(bytes.fromhex(hex_str), spec)
        records.append(
            (
                str(i),
                decoded["32"],
                decoded["49"],
                tx_direction(decoded["t"], decoded["3"]),
                int(decoded["4"]),
            )
        )
    report = reconcile(aggregate(records), control, candidates)
    print_report(report)


def main(argv=None):
    parser = argparse.ArgumentParser(description="ISO 8583 日终对账：最小改动解释控制数")
    parser.add_argument("--log", help="脱敏流水文件（CSV id,hex 或每行一条 hex）")
    parser.add_argument("--control", help="控制数 CSV")
    parser.add_argument("--candidates", help="候选样本清单 CSV")
    parser.add_argument(
        "--spec",
        choices=["default_ascii", "default"],
        default="default_ascii",
        help="报文规范（默认 default_ascii）",
    )
    parser.add_argument(
        "--amount-scale",
        type=int,
        default=1,
        help="4 域金额除数，换算为整数金额（默认 1）",
    )
    parser.add_argument("--selftest", action="store_true", help="运行内置合成示例")
    args = parser.parse_args(argv)

    if args.selftest:
        selftest()
        return 0

    if not (args.log and args.control and args.candidates):
        parser.error("需要同时提供 --log、--control、--candidates（或用 --selftest 演示）")

    spec = getattr(iso8583.specs, args.spec)
    records = read_log(args.log, spec, args.amount_scale)
    control = read_control(args.control)
    candidates = read_candidates(args.candidates)
    report = reconcile(aggregate(records), control, candidates)
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
