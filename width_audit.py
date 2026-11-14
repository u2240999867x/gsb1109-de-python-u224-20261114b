"""定宽清单宽度约定甄别与入库/复核分流。

背景:两间工厂的定宽清单没有标注终端型号,且部分标点(East Asian
Ambiguous,如 "… — · § †")在不同宽度约定下占 1 格或 2 格,导致按显示列
切字段时边界错位。本模块用一组带已知字段边界的标定行,甄别每个来源仍可能
成立的宽度约定,再用存活约定切分业务行:各约定切出的业务字段完全一致则可
入库,否则(尤其货号不同)留待复核。

宽度约定直接基于仓库内 vendored 的 wcwidth:无终端型号时,唯一有分歧的维度
是 East Asian Ambiguous 宽度,即 ambiguous_width=1(窄)与 =2(宽)两种候选
约定;日后补全终端型号,可再叠加 wcwidth 的 term_program 覆写维度。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from wcwidth import wcwidth


# 约定名 -> East Asian Ambiguous 宽度;无终端型号时仅剩窄/宽两种。
CONVENTIONS: dict[str, int] = {
    "ambiguous_narrow(aw=1)": 1,
    "ambiguous_wide(aw=2)": 2,
}


def _char_width(char: str, ambiguous_width: int) -> int:
    """单字符显示宽度;控制符/不可打印按 0 格处理(业务行通常不含)。"""
    width = wcwidth(char, ambiguous_width=ambiguous_width)
    if width is None or width < 0:
        return 0
    return width


def column_index(text: str, ambiguous_width: int) -> list[int]:
    """返回与 text 等长的数组:元素为该字符起始处的显示列(0 基)。"""
    starts: list[int] = []
    column = 0
    for char in text:
        starts.append(column)
        column += _char_width(char, ambiguous_width)
    return starts


def _char_at_column(starts: list[int], target: int) -> int:
    """显示列 target 对应的字符下标;落在字符内部时回退到该字符起点。"""
    best = 0
    for index, start in enumerate(starts):
        if start <= target:
            best = index
        else:
            break
    return best


def segment(text: str, boundaries: list[int], ambiguous_width: int) -> list[str]:
    """按显示列边界把一行切成字段,并去除两端填充(含全角空格 U+3000)。"""
    starts = column_index(text, ambiguous_width)
    cuts = [_char_at_column(starts, column) for column in boundaries] + [len(text)]
    fields = []
    for left, right in zip(cuts, cuts[1:]):
        fields.append(text[left:right].strip(" 　\t"))
    return fields


@dataclass
class ConventionVerdict:
    name: str
    viable: bool
    # 首个导致该约定被排除的标定行与边界列;存活时为 None。
    eliminated_by: dict | None = None


@dataclass
class RowDecision:
    row_id: str
    status: str  # "ingest" 可入库 / "review" 待复核
    fields_by_convention: dict = field(default_factory=dict)
    differing_fields: list[str] = field(default_factory=list)


@dataclass
class SourceReport:
    source: str
    viable_conventions: list[str]
    eliminated: list[ConventionVerdict]
    rows: list[RowDecision]


def _calibration_failure(cal_row: dict, ambiguous_width: int, field_names: list[str]):
    """返回首个失配字段的 (字段名, 结束边界列);全部命中已知边界则返回 None。"""
    text = cal_row["text"]
    starts = column_index(text, ambiguous_width)
    boundaries = cal_row["boundaries"]
    total_width = (
        starts[-1] + _char_width(text[-1], ambiguous_width) if text else 0
    )
    end_columns = boundaries[1:] + [total_width]
    expected_values = [cal_row["fields"][name] for name in field_names]
    cuts = [_char_at_column(starts, column) for column in boundaries] + [len(text)]
    for field_name, end_column, left, right, expected in zip(
        field_names, end_columns, cuts, cuts[1:], expected_values
    ):
        actual = text[left:right].strip(" 　\t")
        if actual != expected:
            return field_name, end_column
    return None


def analyze_source(
    source: str,
    payload: dict,
    conventions: dict[str, int] | None = None,
) -> SourceReport:
    conventions = conventions or CONVENTIONS
    boundaries = payload["boundaries"]
    field_names = payload["field_names"]
    calibration = payload["sources"][source]["calibration"]
    production = payload["sources"][source]["rows"]

    verdicts: list[ConventionVerdict] = []
    for name, ambiguous_width in conventions.items():
        eliminated_by = None
        for cal_row in calibration:
            failure = _calibration_failure(cal_row, ambiguous_width, field_names)
            if failure is not None:
                bad_field, bad_column = failure
                eliminated_by = {
                    "row": cal_row["id"],
                    "column": bad_column,
                    "field": bad_field,
                }
                break
        verdicts.append(
            ConventionVerdict(
                name=name,
                viable=eliminated_by is None,
                eliminated_by=eliminated_by,
            )
        )

    viable = [verdict.name for verdict in verdicts if verdict.viable]
    eliminated = [verdict for verdict in verdicts if not verdict.viable]

    decisions: list[RowDecision] = []
    for row in production:
        segmented = {
            name: segment(row["text"], boundaries, conventions[name])
            for name in viable
        }
        unique = {tuple(values) for values in segmented.values()}
        differing: list[str] = []
        if len(unique) > 1:
            base = next(iter(segmented.values()))
            for index, field_name in enumerate(field_names):
                if any(values[index] != base[index] for values in segmented.values()):
                    differing.append(field_name)
        decisions.append(
            RowDecision(
                row_id=row["id"],
                status="ingest" if len(unique) == 1 else "review",
                fields_by_convention={
                    name: dict(zip(field_names, values))
                    for name, values in segmented.items()
                },
                differing_fields=differing,
            )
        )

    return SourceReport(
        source=source,
        viable_conventions=viable,
        eliminated=eliminated,
        rows=decisions,
    )


def analyze(payload: dict, conventions: dict[str, int] | None = None) -> dict:
    reports = {
        source: analyze_source(source, payload, conventions)
        for source in payload["sources"]
    }
    return {
        source: {
            "viable_conventions": report.viable_conventions,
            "eliminated": [
                {"convention": verdict.name, "at": verdict.eliminated_by}
                for verdict in report.eliminated
            ],
            "rows": [
                {
                    "id": decision.row_id,
                    "status": decision.status,
                    "differing_fields": decision.differing_fields,
                    "fields_by_convention": decision.fields_by_convention,
                }
                for decision in report.rows
            ],
        }
        for source, report in reports.items()
    }


def _demo_payload() -> dict:
    """构造贴合场景的合成数据(字段显示宽度均为 6,边界列 [0, 6, 12])。

    歧义点 '·' 在 aw=1 占 1 格、aw=2 占 2 格,每个字段块按显示列等宽拼接:
    - 工厂A 标定行的货号块 "A·001" 在 aw=2 下恰 6 格,aw=1 下仅 5 格而错位,
      据此在 A-CAL-1 的第 6 列排除窄约定,仅 aw=2 存活;
    - 工厂B 标定行纯 ASCII(两约定等宽),两种约定都存活;其业务行 B-ROW-1
      两约定切分一致(入库),B-ROW-2 的货号被切成不同值(留待复核)。
    """
    boundaries = [0, 6, 12]
    field_names = ["货号", "名称", "规格"]
    return {
        "boundaries": boundaries,
        "field_names": field_names,
        "sources": {
            "工厂A": {
                "calibration": [
                    {
                        "id": "A-CAL-1",
                        "text": "A·001" + "WIDGET" + "SPEC01",
                        "boundaries": boundaries,
                        "fields": {"货号": "A·001", "名称": "WIDGET", "规格": "SPEC01"},
                    }
                ],
                "rows": [
                    {"id": "A-ROW-1", "text": "A10020" + "WIDGET" + "SPEC02"},
                ],
            },
            "工厂B": {
                "calibration": [
                    {
                        "id": "B-CAL-1",
                        "text": "B20020" + "WIDGET" + "SPEC01",
                        "boundaries": boundaries,
                        "fields": {"货号": "B20020", "名称": "WIDGET", "规格": "SPEC01"},
                    }
                ],
                "rows": [
                    {"id": "B-ROW-1", "text": "BR0001" + "WIDGET" + "SPEC01"},
                    {"id": "B-ROW-2", "text": "B·04XX" + "WIDGET" + "SPEC02"},
                ],
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "payload",
        nargs="?",
        type=Path,
        help="真实清单 JSON;缺省运行内置合成演示。",
    )
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text()) if args.payload else _demo_payload()
    print(json.dumps(analyze(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
