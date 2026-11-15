"""命令行入口: 由历史流水构建节前尖峰对照表。

用法:
    python -m preholiday_panel --config config.json --transactions tx.csv \
        --out table.csv [--adjust adjust.json --rematch-out rematch.csv \
        --adjusted-out adjusted.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import warnings
from datetime import date

from .panel import (
    PanelConfig,
    build_panel,
    diff_panels,
    write_changes_csv,
)


def _parse_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def load_config(path):
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return PanelConfig(
        calendar=raw["calendar"],
        range_start=_parse_date(raw["range_start"]),
        range_end=_parse_date(raw["range_end"]),
        business_days_before=raw.get("business_days_before", 1),
        exclude_before=raw.get("exclude_before", 0),
        exclude_after=raw.get("exclude_after", 0),
        extra_working_days=frozenset(
            map(_parse_date, raw.get("extra_working_days", []))
        ),
        extra_holidays=frozenset(map(_parse_date, raw.get("extra_holidays", []))),
    )


def load_transactions(path):
    """读取历史流水 CSV (store_id,date,amount), 按 (门店, 日期) 汇总。"""
    amounts = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"store_id", "date", "amount"} - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"流水文件缺少列: {sorted(missing)}")
        for row in reader:
            key = (row["store_id"], _parse_date(row["date"]))
            amounts[key] = amounts.get(key, 0.0) + float(row["amount"])
    store_ids = sorted({store for store, _ in amounts})
    return store_ids, amounts


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="preholiday_panel",
        description="由历史流水构建节前客流尖峰对照表 (可复算)",
    )
    parser.add_argument("--config", required=True, help="构建参数 JSON")
    parser.add_argument("--transactions", required=True, help="历史流水 CSV")
    parser.add_argument("--out", required=True, help="对照表输出 CSV")
    parser.add_argument(
        "--adjust",
        help="临时调休 JSON: {\"extra_working_days\": [...], "
        "\"extra_holidays\": [...]}",
    )
    parser.add_argument("--adjusted-out", help="调休后的对照表输出 CSV")
    parser.add_argument("--rematch-out", help="调休前后需重配组的清单 CSV")
    args = parser.parse_args(argv)

    warnings.filterwarnings("ignore", category=UserWarning)

    config = load_config(args.config)
    store_ids, amounts = load_transactions(args.transactions)
    result = build_panel(store_ids, amounts, config)
    result.to_csv(args.out)
    matched = sum(1 for r in result.rows if r["status"] == "matched")
    print(
        f"对照表已写入 {args.out}: 共 {len(result.rows)} 个观察日, "
        f"{matched} 个已配对对照日"
    )

    if args.adjust:
        with open(args.adjust, encoding="utf-8") as fh:
            adjust = json.load(fh)
        adjusted_config = config.with_adjustments(
            extra_working_days=map(
                _parse_date, adjust.get("extra_working_days", [])
            ),
            extra_holidays=map(_parse_date, adjust.get("extra_holidays", [])),
        )
        adjusted = build_panel(store_ids, amounts, adjusted_config)
        if args.adjusted_out:
            adjusted.to_csv(args.adjusted_out)
            print(f"调休后对照表已写入 {args.adjusted_out}")
        changes = diff_panels(result, adjusted)
        if args.rematch_out:
            write_changes_csv(changes, args.rematch_out)
        print(f"临时调休影响: {len(changes)} 个组需要重配")
        for change in changes:
            print(
                f"  {change['store_id']} {change['holiday']} "
                f"({change['holiday_start']}): {change['change_type']} "
                f"观察日 {change['before_observation']} -> "
                f"{change['after_observation']}, 对照日 "
                f"{change['before_control']} -> {change['after_control']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
