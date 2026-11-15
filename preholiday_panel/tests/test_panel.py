import csv
import json
import warnings
from datetime import date, timedelta

import pytest

from preholiday_panel.cli import main as cli_main
from preholiday_panel.matching import (
    interchangeable_options,
    optimal_assignment,
)
from preholiday_panel.panel import (
    PanelConfig,
    StoreCalendar,
    build_panel,
    diff_panels,
    holiday_events,
)

warnings.filterwarnings("ignore", category=UserWarning)

D = date


def working_day_amounts(store_ids, start, end, calendar_code="CN", amount=100.0):
    """为范围内每个营业日生成 (门店, 日期) 流水。"""
    cal = StoreCalendar(calendar_code)
    amounts = {}
    day = start
    while day <= end:
        if cal.is_working_day(day):
            for store in store_ids:
                amounts[(store, day)] = amount
        day += timedelta(days=1)
    return amounts


# ---------------------------------------------------------------- 匹配求解器

def test_assignment_prefers_more_matches_over_distance():
    candidates = {
        "o1": {D(2023, 9, 21): 7, D(2023, 9, 14): 14},
        "o2": {D(2023, 9, 21): 7},
    }
    matching, n_matched, total_cost = optimal_assignment(candidates)
    assert n_matched == 2
    assert matching["o2"] == D(2023, 9, 21)
    assert matching["o1"] == D(2023, 9, 14)
    assert total_cost == 21


def test_assignment_min_distance_tiebreak():
    candidates = {"o1": {D(2023, 9, 21): 7, D(2023, 9, 14): 14}}
    matching, n_matched, total_cost = optimal_assignment(candidates)
    assert matching == {"o1": D(2023, 9, 21)}
    assert (n_matched, total_cost) == (1, 7)


def test_same_day_used_at_most_once():
    day = D(2023, 9, 21)
    candidates = {"o1": {day: 7}, "o2": {day: 7}}
    matching, n_matched, _ = optimal_assignment(candidates)
    assert n_matched == 1
    assert list(matching.values()) == [day]


def test_interchangeable_options_marks_ties():
    d1, d2 = D(2023, 9, 21), D(2023, 9, 14)
    candidates = {"o1": {d1: 7, d2: 7}, "o2": {d1: 7, d2: 7}}
    _, n_matched, total_cost = optimal_assignment(candidates)
    options = interchangeable_options(candidates, n_matched, total_cost)
    assert options["o1"] == [d2, d1]
    assert options["o2"] == [d2, d1]


def test_interchangeable_options_respects_cost():
    d1, d2 = D(2023, 9, 21), D(2023, 9, 14)
    candidates = {"o1": {d1: 1, d2: 5}}
    _, n_matched, total_cost = optimal_assignment(candidates)
    options = interchangeable_options(candidates, n_matched, total_cost)
    assert options["o1"] == [d1]


# ---------------------------------------------------------------- 观察日推算

def test_observation_day_is_last_business_day_before_holiday():
    cal = StoreCalendar("CN")
    events = {e.name: e for e in holiday_events(cal.calendar, 2023)}
    mid_autumn = events["Mid-Autumn Festival"]
    national = events["National Day"]
    assert (mid_autumn.start, mid_autumn.end) == (D(2023, 9, 29), D(2023, 9, 29))
    assert (national.start, national.end) == (D(2023, 9, 30), D(2023, 10, 6))
    # 9/29 是假期, 两个节日的观察日都是 9/28 (周四)
    assert cal.sub_working_days(mid_autumn.start, 1) == D(2023, 9, 28)
    assert cal.sub_working_days(national.start, 1) == D(2023, 9, 28)


def test_temporary_schedule_change_shifts_observation():
    cal = StoreCalendar("CN", extra_holidays={D(2023, 9, 28)})
    assert cal.sub_working_days(D(2023, 9, 29), 1) == D(2023, 9, 27)


# ---------------------------------------------------------------- 对照表构建

def make_config(**overrides):
    params = dict(
        calendar="CN",
        range_start=D(2023, 8, 1),
        range_end=D(2023, 10, 31),
    )
    params.update(overrides)
    return PanelConfig(**params)


def rows_by_holiday(result, store="S1"):
    return {r["holiday"]: r for r in result.rows if r["store_id"] == store}


def test_panel_crowded_week_both_holidays_matched():
    amounts = working_day_amounts(["S1"], D(2023, 8, 1), D(2023, 10, 31))
    result = build_panel(["S1"], amounts, make_config())
    rows = rows_by_holiday(result)
    mid, nat = rows["Mid-Autumn Festival"], rows["National Day"]
    # 两个节日挤在同一周: 观察日同为 2023-09-28, 划入同组
    assert mid["observation_date"] == D(2023, 9, 28)
    assert nat["observation_date"] == D(2023, 9, 28)
    assert mid["group_id"] == nat["group_id"]
    assert mid["status"] == nat["status"] == "matched"
    # 同店同一天最多使用一次
    assert mid["control_date"] != nat["control_date"]
    # 最优: 9/21 (距 7) + 9/14 或 10/12 (距 14), 总距离 21
    distances = sorted([mid["distance_days"], nat["distance_days"]])
    assert distances == [7, 14]


def test_panel_interchangeable_controls_marked():
    amounts = working_day_amounts(["S1"], D(2023, 8, 1), D(2023, 10, 31))
    result = build_panel(["S1"], amounts, make_config())
    rows = rows_by_holiday(result)
    expected = {D(2023, 9, 14), D(2023, 9, 21), D(2023, 10, 12)}
    for row in rows.values():
        assert row["interchangeable"] == "yes"
        pool = {row["control_date"]} | {
            D.fromisoformat(d) for d in row["alt_controls"].split(";")
        }
        assert pool == expected


def test_control_candidates_respect_rules():
    amounts = working_day_amounts(["S1"], D(2023, 8, 1), D(2023, 10, 31))
    result = build_panel(["S1"], amounts, make_config())
    rows = rows_by_holiday(result)
    obs = D(2023, 9, 28)
    for row in rows.values():
        control = row["control_date"]
        assert control.weekday() == obs.weekday()          # 同星期
        assert make_config().range_start <= control <= make_config().range_end
        assert control != obs                              # 不是观察日
        assert not (D(2023, 9, 29) <= control <= D(2023, 10, 6))  # 不在排除窗
        assert row["control_amount"] == 100.0              # 当天有流水


def test_exclusion_window_blocks_nearby_candidates():
    amounts = working_day_amounts(["S1"], D(2023, 8, 1), D(2023, 10, 31))
    base = rows_by_holiday(build_panel(["S1"], amounts, make_config()))
    # 排除窗扩到节前 8 天: 中秋窗 [9/21, 9/29], 9/21 落入窗内被排除
    wide = rows_by_holiday(
        build_panel(["S1"], amounts, make_config(exclude_before=8))
    )
    assert base["Mid-Autumn Festival"]["control_date"] in (
        D(2023, 9, 14), D(2023, 9, 21),
    )
    controls = {row["control_date"] for row in wide.values()}
    assert D(2023, 9, 21) not in controls
    assert controls == {D(2023, 9, 14), D(2023, 10, 12)}


def test_control_requires_transactions():
    amounts = working_day_amounts(["S1"], D(2023, 8, 1), D(2023, 10, 31))
    # 删掉 9 月全部流水 -> 最近的可用周四变成 10/12 (距 14) 与 10/19 (距 21)
    amounts = {k: v for k, v in amounts.items() if k[1].month != 9}
    result = build_panel(["S1"], amounts, make_config())
    rows = rows_by_holiday(result)
    controls = {row["control_date"] for row in rows.values()}
    assert controls == {D(2023, 10, 12), D(2023, 10, 19)}
    distances = sorted(row["distance_days"] for row in rows.values())
    assert distances == [14, 21]


def test_unmatched_when_no_candidate():
    # 范围太短, 没有任何同星期候选
    amounts = working_day_amounts(["S1"], D(2023, 9, 25), D(2023, 10, 1))
    config = make_config(range_start=D(2023, 9, 25), range_end=D(2023, 10, 1))
    result = build_panel(["S1"], amounts, config)
    rows = rows_by_holiday(result)
    assert rows["Mid-Autumn Festival"]["status"] == "unmatched"
    assert rows["Mid-Autumn Festival"]["control_date"] == ""


# ---------------------------------------------------------------- 临时调休

def test_adjustment_marks_groups_for_rematch():
    amounts = working_day_amounts(["S1"], D(2023, 8, 1), D(2023, 10, 31))
    config = make_config()
    before = build_panel(["S1"], amounts, config)
    # 临时调休: 2023-09-28 (原观察日) 改为休息
    after = build_panel(
        ["S1"], amounts, config.with_adjustments(extra_holidays={D(2023, 9, 28)})
    )
    changes = diff_panels(before, after)
    assert len(changes) == 2  # 中秋、国庆两组都要重配
    for change in changes:
        assert "observation_shifted" in change["change_type"]
        assert "control_changed" in change["change_type"]
        assert change["before_observation"] == D(2023, 9, 28)
        assert change["after_observation"] == D(2023, 9, 27)
        assert change["after_control"].weekday() == 2  # 周三


def test_no_change_when_adjustment_irrelevant():
    amounts = working_day_amounts(["S1"], D(2023, 8, 1), D(2023, 10, 31))
    config = make_config()
    before = build_panel(["S1"], amounts, config)
    after = build_panel(
        ["S1"], amounts, config.with_adjustments(extra_holidays={D(2023, 8, 15)})
    )
    assert diff_panels(before, after) == []


# ---------------------------------------------------------------- CLI 端到端

def test_cli_end_to_end(tmp_path):
    tx_path = tmp_path / "tx.csv"
    amounts = working_day_amounts(["S1"], D(2023, 8, 1), D(2023, 10, 31))
    with open(tx_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["store_id", "date", "amount"])
        for (store, day), amount in sorted(amounts.items(), key=str):
            writer.writerow([store, day.isoformat(), amount])

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "calendar": "CN",
        "range_start": "2023-08-01",
        "range_end": "2023-10-31",
    }), encoding="utf-8")
    adjust_path = tmp_path / "adjust.json"
    adjust_path.write_text(
        json.dumps({"extra_holidays": ["2023-09-28"]}), encoding="utf-8"
    )

    out = tmp_path / "table.csv"
    rematch = tmp_path / "rematch.csv"
    rc = cli_main([
        "--config", str(config_path),
        "--transactions", str(tx_path),
        "--out", str(out),
        "--adjust", str(adjust_path),
        "--rematch-out", str(rematch),
    ])
    assert rc == 0

    with open(out, encoding="utf-8-sig") as fh:
        rows = {r["holiday"]: r for r in csv.DictReader(fh)}
    assert rows["Mid-Autumn Festival"]["observation_date"] == "2023-09-28"
    assert rows["Mid-Autumn Festival"]["status"] == "matched"

    with open(rematch, encoding="utf-8-sig") as fh:
        changes = list(csv.DictReader(fh))
    assert len(changes) == 2
    assert all("observation_shifted" in c["change_type"] for c in changes)
