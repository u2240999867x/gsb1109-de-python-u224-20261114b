"""对照表构建主逻辑。

术语约定:
- 观察日: 节日(假期段)之前第 ``business_days_before`` 个营业日,
  按门店日历 (workalendar + 门店临时调休) 推算;
- 对照日: 与观察日同星期、落在给定日期范围内、是营业日、
  不落在任何节日的排除窗内、也不是任何观察日、且当天有历史流水;
  同店同一天最多被一个观察日使用;
- 匹配目标 (字典序): 先让有对照的观察日尽量多, 再让总日期距离尽量短;
- 存在多种最佳配法时, 标出每个观察日可互换的对照日期;
- 临时调休 (extra_working_days / extra_holidays) 改变日历后,
  用 ``diff_panels`` 对比前后两张表, 列出需要重配的组。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from workalendar.exceptions import CalendarError
from workalendar.registry import registry

from .matching import interchangeable_options, optimal_assignment

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

ROW_FIELDS = (
    "store_id",
    "group_id",
    "holiday",
    "holiday_start",
    "holiday_end",
    "observation_date",
    "observation_weekday",
    "status",
    "control_date",
    "control_weekday",
    "distance_days",
    "interchangeable",
    "alt_controls",
    "observation_amount",
    "control_amount",
)

CHANGE_FIELDS = (
    "store_id",
    "holiday",
    "holiday_start",
    "change_type",
    "before_group",
    "after_group",
    "before_observation",
    "after_observation",
    "before_control",
    "after_control",
    "before_status",
    "after_status",
)


@dataclass(frozen=True)
class PanelConfig:
    """对照表构建参数。"""

    calendar: str                      # workalendar 注册代码, 如 "CN"
    range_start: date                  # 对照日可选范围 (含)
    range_end: date                    # 对照日可选范围 (含)
    business_days_before: int = 1      # 观察日 = 节前第 N 个营业日
    exclude_before: int = 0            # 排除窗: 假期开始前额外排除的日历天数
    exclude_after: int = 0             # 排除窗: 假期结束后额外排除的日历天数
    extra_working_days: frozenset = frozenset()  # 临时调休: 改为上班
    extra_holidays: frozenset = frozenset()      # 临时调休: 改为休息

    def with_adjustments(self, extra_working_days=(), extra_holidays=()):
        """叠加临时调休, 返回新配置。"""
        return replace(
            self,
            extra_working_days=frozenset(
                self.extra_working_days | set(extra_working_days)
            ),
            extra_holidays=frozenset(
                self.extra_holidays | set(extra_holidays)
            ),
        )


class StoreCalendar:
    """门店日历 = workalendar 日历 + 门店临时调休。"""

    def __init__(self, code, extra_working_days=(), extra_holidays=()):
        calendar_cls = registry.get(code)
        if calendar_cls is None:
            raise ValueError(f"未知的日历代码: {code!r}")
        for day in list(extra_working_days) + list(extra_holidays):
            if not isinstance(day, date):
                raise TypeError(f"调休日期必须是 date, 收到: {day!r}")
        self.calendar = calendar_cls()
        builtin = tuple(getattr(self.calendar, "extra_working_days", ()))
        self.extra_working_days = tuple(sorted(set(builtin) | set(extra_working_days)))
        self.extra_holidays = tuple(sorted(set(extra_holidays)))

    def is_working_day(self, day):
        return self.calendar.is_working_day(
            day, self.extra_working_days, self.extra_holidays
        )

    def sub_working_days(self, day, delta):
        return self.calendar.sub_working_days(
            day, delta, self.extra_working_days, self.extra_holidays
        )


@dataclass(frozen=True)
class HolidayEvent:
    """一段连续同名的假期。"""

    name: str
    start: date
    end: date


def holiday_events(calendar, year):
    """把一年的节假日按"连续同名"聚成假期段。"""
    events = []
    for day, name in sorted(calendar.holidays(year)):
        if events and events[-1].name == name and (day - events[-1].end).days == 1:
            events[-1] = HolidayEvent(name, events[-1].start, day)
        else:
            events.append(HolidayEvent(name, day, day))
    return events


def _collect_events(store_calendar, config):
    """收集日期范围 (前后各延一年, 供排除窗使用) 内的全部假期段。"""
    events = []
    for year in range(config.range_start.year - 1, config.range_end.year + 2):
        try:
            events.extend(holiday_events(store_calendar.calendar, year))
        except CalendarError:
            continue  # 该日历不支持此年份 (如中国日历仅覆盖部分年份)
    return events


def _in_windows(day, windows):
    return any(start <= day <= end for start, end in windows)


def _candidate_dates(store_calendar, obs_date, windows, obs_dates, store_days, config):
    """单个观察日的候选对照日: {日期: 距离天数}。"""
    candidates = {}
    day = config.range_start
    while day <= config.range_end:
        if (
            day.weekday() == obs_date.weekday()
            and day not in obs_dates
            and day in store_days
            and not _in_windows(day, windows)
            and store_calendar.is_working_day(day)
        ):
            candidates[day] = abs((day - obs_date).days)
        day += timedelta(days=1)
    return candidates


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class PanelResult:
    """一张可复算的对照表。"""

    config: PanelConfig
    rows: list = field(default_factory=list)

    def to_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=ROW_FIELDS)
            writer.writeheader()
            for row in self.rows:
                writer.writerow({k: _format_cell(v) for k, v in row.items()})


def _format_cell(value):
    if isinstance(value, date):
        return value.isoformat()
    return value


def build_panel(store_ids, amounts, config):
    """构建对照表。

    参数:
        store_ids: 门店 id 可迭代对象;
        amounts: {(store_id, date): 当日流水合计}, 用于判断某天是否可用
                 以及回填观察日/对照日流水;
        config: PanelConfig。
    """
    store_calendar = StoreCalendar(
        config.calendar, config.extra_working_days, config.extra_holidays
    )
    events = _collect_events(store_calendar, config)
    windows = [
        (
            event.start - timedelta(days=config.exclude_before),
            event.end + timedelta(days=config.exclude_after),
        )
        for event in events
    ]

    # 每个假期段的观察日; 观察日本身也不允许充当对照日
    obs_by_event = {}
    obs_dates = set()
    for event in events:
        try:
            obs = store_calendar.sub_working_days(
                event.start, config.business_days_before
            )
        except CalendarError:
            continue
        obs_dates.add(obs)
        if config.range_start <= obs <= config.range_end:
            obs_by_event[event] = obs

    rows = []
    for store in sorted(store_ids):
        store_days = {d for (sid, d) in amounts if sid == store}
        # key = (观察日, 假期开始, 节日名), 可排序保证结果确定
        candidates = {}
        key_event = {}
        for event, obs in sorted(
            obs_by_event.items(), key=lambda kv: (kv[1], kv[0].start, kv[0].name)
        ):
            key = (obs, event.start, event.name)
            key_event[key] = (event, obs)
            candidates[key] = _candidate_dates(
                store_calendar, obs, windows, obs_dates, store_days, config
            )

        # 共享候选日期的观察日划入同组, 组内统一求解
        union = _UnionFind()
        seen_by_date = {}
        for key, cands in candidates.items():
            for day in cands:
                if day in seen_by_date:
                    union.union(key, seen_by_date[day])
                else:
                    seen_by_date[day] = key
        groups = {}
        for key in candidates:
            groups.setdefault(union.find(key), []).append(key)

        ordered_groups = sorted(
            groups.values(), key=lambda keys: min(k[0] for k in keys)
        )
        for group_no, keys in enumerate(ordered_groups, start=1):
            group_id = f"{store}#G{group_no}"
            sub = {key: candidates[key] for key in keys}
            matching, n_matched, total_cost = optimal_assignment(sub)
            options = interchangeable_options(sub, n_matched, total_cost)
            for key in keys:
                event, obs = key_event[key]
                control = matching.get(key)
                alts = [d for d in options[key] if d != control]
                rows.append(
                    {
                        "store_id": store,
                        "group_id": group_id,
                        "holiday": event.name,
                        "holiday_start": event.start,
                        "holiday_end": event.end,
                        "observation_date": obs,
                        "observation_weekday": WEEKDAY_CN[obs.weekday()],
                        "status": "matched" if control else "unmatched",
                        "control_date": control or "",
                        "control_weekday": (
                            WEEKDAY_CN[control.weekday()] if control else ""
                        ),
                        "distance_days": (
                            abs((control - obs).days) if control else ""
                        ),
                        "interchangeable": (
                            "yes" if len(options[key]) > 1 else "no"
                        ),
                        "alt_controls": ";".join(d.isoformat() for d in alts),
                        "observation_amount": amounts.get((store, obs), ""),
                        "control_amount": (
                            amounts.get((store, control), "") if control else ""
                        ),
                    }
                )
    rows.sort(
        key=lambda r: (
            r["store_id"],
            r["observation_date"],
            r["holiday_start"],
            r["holiday"],
        )
    )
    return PanelResult(config=config, rows=rows)


def diff_panels(before, after):
    """对比临时调休前后的两张对照表, 列出需要重配的组。

    返回变更记录列表, change_type 取值:
    observation_shifted / control_changed / newly_matched /
    newly_unmatched / group_added / group_removed。
    """
    def key_of(row):
        return (row["store_id"], row["holiday"], row["holiday_start"])

    before_map = {key_of(r): r for r in before.rows}
    after_map = {key_of(r): r for r in after.rows}
    changes = []
    for key in sorted(set(before_map) | set(after_map)):
        old, new = before_map.get(key), after_map.get(key)
        if old is None:
            changes.append(_change_row(new, new, "group_added"))
        elif new is None:
            changes.append(_change_row(old, old, "group_removed"))
        else:
            types = []
            if old["observation_date"] != new["observation_date"]:
                types.append("observation_shifted")
            if old["status"] != new["status"]:
                types.append(
                    "newly_matched" if new["status"] == "matched"
                    else "newly_unmatched"
                )
            if old["control_date"] != new["control_date"]:
                types.append("control_changed")
            if types:
                changes.append(_change_row(old, new, ";".join(types)))
    return changes


def _change_row(old, new, change_type):
    return {
        "store_id": new["store_id"],
        "holiday": new["holiday"],
        "holiday_start": new["holiday_start"],
        "change_type": change_type,
        "before_group": old["group_id"],
        "after_group": new["group_id"],
        "before_observation": old["observation_date"],
        "after_observation": new["observation_date"],
        "before_control": old["control_date"],
        "after_control": new["control_date"],
        "before_status": old["status"],
        "after_status": new["status"],
    }


def write_changes_csv(changes, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CHANGE_FIELDS)
        writer.writeheader()
        for change in changes:
            writer.writerow({k: _format_cell(v) for k, v in change.items()})
