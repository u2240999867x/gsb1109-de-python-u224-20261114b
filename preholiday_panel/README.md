# 节前客流尖峰对照表 (preholiday_panel)

用历史流水为每个"节前最后一个营业日"(观察日) 匹配同星期的对照日,
输出一张可复算的对照表。基于本仓库的 workalendar 门店日历。

## 规则

- **观察日**: 按门店日历推算的"假期段之前第 N 个营业日"
  (`business_days_before`, 默认 1, 即节前最后一个营业日)。
  假期段 = 日历中连续同名的节假日。
- **对照日** 必须同时满足:
  - 与观察日同星期;
  - 落在 `[range_start, range_end]` 内;
  - 是门店营业日 (含法定节假日调休与门店临时调休);
  - 不落在任何节日的排除窗 (假期段前后各延 `exclude_before` /
    `exclude_after` 个日历日) 内, 也不是任何观察日;
  - 当天有历史流水;
  - 同店同一天最多被一个观察日使用。
- **匹配目标 (字典序)**: 先让有对照的观察日尽量多, 再让总日期距离
  (|对照日 - 观察日| 的日历天数之和) 尽量短。共享候选日期的观察日
  划入同组统一求解 (最小费用最大流)。
- **可互换**: 若存在多种最佳配法, `interchangeable=yes` 且
  `alt_controls` 列出该观察日全部可互换的对照日期。
- **临时调休**: 用 `--adjust` 提供 `extra_working_days` /
  `extra_holidays` 后重算, `--rematch-out` 输出需要重配的组
  (观察日位移 / 对照日变更 / 新增失配等)。

## 用法

```bash
# 生成示例数据 (2023 年中秋+国庆同周场景)
python -m preholiday_panel.examples.make_sample_data  # 或直接进入 examples 目录运行

# 构建对照表
python -m preholiday_panel \
  --config preholiday_panel/examples/config.json \
  --transactions preholiday_panel/examples/transactions.csv \
  --out table.csv

# 模拟临时调休并重配
python -m preholiday_panel \
  --config preholiday_panel/examples/config.json \
  --transactions preholiday_panel/examples/transactions.csv \
  --out table.csv \
  --adjust preholiday_panel/examples/adjust.json \
  --adjusted-out adjusted.csv --rematch-out rematch.csv
```

## 输入格式

- `config.json`: `calendar` (workalendar 注册代码, 如 `CN`)、
  `range_start` / `range_end`、`business_days_before`、
  `exclude_before` / `exclude_after`、可选 `extra_working_days` /
  `extra_holidays`。
- 流水 CSV: `store_id,date,amount` (同日多行自动汇总)。

## 输出

`table.csv` 每行一个 (门店 × 节日) 观察日: 组别、假期段、观察日、
对照日、距离、是否可互换、可互换对照日、观察日/对照日流水。
相同输入必得相同结果, 可复算。

## 测试

```bash
.venv/bin/python -m pytest preholiday_panel/tests -q
```
