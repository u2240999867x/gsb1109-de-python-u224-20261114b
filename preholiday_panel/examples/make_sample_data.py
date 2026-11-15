"""生成示例数据: 2023 年中秋 (9/29) 与国庆 (9/30-10/6) 挤在同一周的场景。

运行:
    python examples/make_sample_data.py

生成:
    examples/transactions.csv  两家门店 2023-08-01..2023-10-31 的日流水
    examples/config.json       对照表构建参数
    examples/adjust.json       临时调休示例 (2023-09-28 临时休息)
"""
import csv
import json
import random
import warnings
from datetime import date, timedelta
from pathlib import Path

from workalendar.registry import registry

HERE = Path(__file__).resolve().parent

RANGE_START = date(2023, 8, 1)
RANGE_END = date(2023, 10, 31)
STORES = ("STORE_A", "STORE_B")


def main():
    warnings.filterwarnings("ignore", category=UserWarning)
    calendar = registry.get("CN")()

    rows = []
    day = RANGE_START
    while day <= RANGE_END:
        if calendar.is_working_day(day):
            for store in STORES:
                rng = random.Random(f"{store}|{day.isoformat()}")
                for _ in range(rng.randint(1, 3)):
                    rows.append((store, day.isoformat(), rng.randint(50, 500)))
        day += timedelta(days=1)

    with open(HERE / "transactions.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["store_id", "date", "amount"])
        writer.writerows(rows)

    config = {
        "calendar": "CN",
        "range_start": RANGE_START.isoformat(),
        "range_end": RANGE_END.isoformat(),
        "business_days_before": 1,
        "exclude_before": 0,
        "exclude_after": 0,
    }
    with open(HERE / "config.json", "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)

    adjust = {"extra_holidays": ["2023-09-28"]}
    with open(HERE / "adjust.json", "w", encoding="utf-8") as fh:
        json.dump(adjust, fh, ensure_ascii=False, indent=2)

    print(f"已生成 {len(rows)} 条流水及配置, 目录: {HERE}")


if __name__ == "__main__":
    main()
