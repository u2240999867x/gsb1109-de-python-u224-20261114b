"""按设备档案审计嵌套 @media，判定每项资源必用、可能用或可排除。

规则语义：
- 逗号分隔的媒体查询是备选分支（任一成立即可）。
- 内外层 @media 的条件必须同时成立（AND）。
- 范围区分开闭（> / >= 等），与设备视口宽度区间求交。
- 未知媒体特性保留不确定性（不会仅因它排除或必用），
  但若同一分支里已知宽度条件与设备毫无交集，该分支仍可确定排除。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

import tinycss2
from tinycss2.ast import (
    AtRule,
    DimensionToken,
    IdentToken,
    LiteralToken,
    NumberToken,
    ParenthesesBlock,
)

ALWAYS = 'always'      # 对这批设备恒真 → 必用
NEVER = 'never'        # 对这批设备恒假 → 可排除
SOMETIMES = 'sometimes'  # 部分设备成立 → 可能用
UNKNOWN = 'unknown'    # 含未知条件，无法确定 → 可能用（保留不确定性）

VERDICT_LABELS = {
    ALWAYS: '必用',
    NEVER: '可排除',
    SOMETIMES: '可能用',
    UNKNOWN: '可能用（含未知条件，保留不确定性）',
}

KNOWN_MEDIA_TYPES = {'screen', 'print', 'speech'}
NEVER_MATCH_TYPES = {
    'tty', 'tv', 'projection', 'handheld', 'braille', 'embossed', 'aural',
}


@dataclass(frozen=True)
class Interval:
    """可能开闭的数值区间，None 表示该侧无界。"""

    lower: float | None = None
    upper: float | None = None
    lower_open: bool = False
    upper_open: bool = False

    def is_empty(self):
        if self.lower is not None and self.upper is not None:
            if self.lower > self.upper:
                return True
            if self.lower == self.upper and (self.lower_open or self.upper_open):
                return True
        return False

    def intersection(self, other):
        def max_bound(a, b):
            if a[0] is None:
                return b
            if b[0] is None:
                return a
            if a[0] != b[0]:
                return a if a[0] > b[0] else b
            return (a[0], a[1] or b[1])

        def min_bound(a, b):
            if a[0] is None:
                return b
            if b[0] is None:
                return a
            if a[0] != b[0]:
                return a if a[0] < b[0] else b
            return (a[0], a[1] or b[1])

        lo = max_bound((self.lower, self.lower_open),
                       (other.lower, other.lower_open))
        hi = min_bound((self.upper, self.upper_open),
                       (other.upper, other.upper_open))
        return Interval(lo[0], hi[0], lo[1], hi[1])

    def covers(self, other):
        """self 是否完全覆盖 other。"""
        if self.lower is not None:
            if other.lower is None or other.lower < self.lower:
                return False
            if (other.lower == self.lower and self.lower_open
                    and not other.lower_open):
                return False
        if self.upper is not None:
            if other.upper is None or other.upper > self.upper:
                return False
            if (other.upper == self.upper and self.upper_open
                    and not other.upper_open):
                return False
        return True


@dataclass
class DeviceProfile:
    """这批设备的档案：视口宽度范围、配色模式、媒体类型。"""

    min_width: float | None = None
    max_width: float | None = None
    color_schemes: tuple = ('light', 'dark')
    media_types: tuple = ('screen',)

    def width_interval(self):
        return Interval(self.min_width, self.max_width)

    def width_desc(self):
        if self.min_width is None and self.max_width is None:
            return '不限'
        lo = '-∞' if self.min_width is None else f'{self.min_width:g}'
        hi = '+∞' if self.max_width is None else f'{self.max_width:g}'
        return f'{lo}–{hi}px'

    def describe(self):
        return (
            f'视口宽度 {self.width_desc()}；'
            f'配色模式 {", ".join(self.color_schemes)}；'
            f'媒体类型 {", ".join(self.media_types)}'
        )


@dataclass
class Leaf:
    """媒体条件叶子：text 为条件出处原文。"""

    text: str
    kind: str  # 'width' | 'color-scheme' | 'media-type' | 'unknown'
    interval: Interval | None = None
    scheme: str | None = None
    media_type: str | None = None


# ---------------------------------------------------------------- 解析

def _significant(tokens):
    return [t for t in tokens if t.type not in ('whitespace', 'comment')]


def _split_top(tokens, separator=','):
    parts, current = [], []
    for token in tokens:
        if token.type == 'literal' and token.value == separator:
            parts.append(current)
            current = []
        else:
            current.append(token)
    parts.append(current)
    return parts


def _parse_length(tokens):
    """解析单个长度值，仅支持 px（及 0）；其它单位返回 None。"""
    tokens = _significant(tokens)
    if len(tokens) != 1:
        return None
    token = tokens[0]
    if isinstance(token, DimensionToken) and token.lower_unit == 'px':
        return token.value
    if isinstance(token, NumberToken) and token.value == 0:
        return 0.0
    return None


def _unknown_leaf(text):
    return ('leaf', Leaf(text=text, kind='unknown'))


def _flip(op):
    return {'<': '>', '<=': '>=', '>': '<', '>=': '<=', '=': '='}[op]


def _apply_bound(interval, op, value):
    """把 "width <op> value" 形式的约束并入区间。"""
    lower, upper = interval.lower, interval.upper
    lower_open, upper_open = interval.lower_open, interval.upper_open
    if op in ('>', '>='):
        candidate = Interval(value, None, lower_open=(op == '>'))
        return interval.intersection(candidate)
    if op in ('<', '<='):
        candidate = Interval(None, value, upper_open=(op == '<'))
        return interval.intersection(candidate)
    return interval.intersection(Interval(value, value))


def _parse_range(tokens, text):
    """解析 (400px <= width <= 800px)、(width > 600px) 等范围式。"""
    items = []
    pending_op = ''
    for token in tokens:
        if isinstance(token, LiteralToken) and token.value in '<>=':
            pending_op += token.value
            continue
        if pending_op:
            items.append(('op', pending_op))
            pending_op = ''
        if isinstance(token, (DimensionToken, NumberToken)):
            length = _parse_length([token])
            if length is None:
                return _unknown_leaf(text)
            items.append(('len', length))
        elif isinstance(token, IdentToken):
            items.append(('name', token.lower_value))
        else:
            return _unknown_leaf(text)
    if pending_op:
        items.append(('op', pending_op))

    kinds = [kind for kind, _ in items]
    constraints = []
    if kinds == ['name', 'op', 'len']:
        (_, name), (_, op), (_, value) = items
        constraints.append((op, value))
    elif kinds == ['len', 'op', 'name']:
        (_, value), (_, op), (_, name) = items
        constraints.append((_flip(op), value))
    elif kinds == ['len', 'op', 'name', 'op', 'len']:
        (_, v1), (_, op1), (_, name), (_, op2), (_, v2) = items
        constraints.append((_flip(op1), v1))
        constraints.append((op2, v2))
    else:
        return _unknown_leaf(text)
    if name != 'width':
        return _unknown_leaf(text)
    interval = Interval()
    for op, value in constraints:
        interval = _apply_bound(interval, op, value)
    return ('leaf', Leaf(text=text, kind='width', interval=interval))


def _parse_feature(block):
    """把括号块解析成条件叶子。"""
    text = f'({tinycss2.serialize(block.content).strip()})'
    tokens = _significant(block.content)
    if not tokens:
        return _unknown_leaf(text)

    # 经典式：(min-width: 600px)、(prefers-color-scheme: dark)
    if (len(tokens) >= 2 and isinstance(tokens[0], IdentToken)
            and isinstance(tokens[1], LiteralToken)
            and tokens[1].value == ':'):
        name = tokens[0].lower_value
        value = tokens[2:]
        if name in ('width', 'min-width', 'max-width'):
            length = _parse_length(value)
            if length is None:
                return _unknown_leaf(text)
            if name == 'width':
                interval = Interval(length, length)
            elif name == 'min-width':
                interval = Interval(lower=length)
            else:
                interval = Interval(upper=length)
            return ('leaf', Leaf(text=text, kind='width', interval=interval))
        if name == 'prefers-color-scheme':
            value = _significant(value)
            if len(value) == 1 and isinstance(value[0], IdentToken):
                return ('leaf', Leaf(
                    text=text, kind='color-scheme',
                    scheme=value[0].lower_value))
            return _unknown_leaf(text)
        return _unknown_leaf(text)

    # 范围式：(width > 600px)、(400px <= width <= 800px)
    if any(isinstance(t, LiteralToken) and t.value in '<>='
           for t in tokens):
        return _parse_range(tokens, text)

    # 裸特性：(width)、(hover)
    if len(tokens) == 1 and isinstance(tokens[0], IdentToken):
        if tokens[0].lower_value == 'width':
            return ('leaf', Leaf(
                text=text, kind='width',
                interval=Interval(lower=0, lower_open=True)))
        return _unknown_leaf(text)

    return _unknown_leaf(text)


def _parse_query(tokens):
    """解析逗号之间的单个媒体查询。"""
    tokens = _significant(tokens)
    if not tokens:
        return ('true',)
    negate = False
    if (isinstance(tokens[0], IdentToken)
            and tokens[0].lower_value == 'only'):
        tokens = tokens[1:]
    if (tokens and isinstance(tokens[0], IdentToken)
            and tokens[0].lower_value == 'not'):
        negate = True
        tokens = tokens[1:]

    conjuncts = []
    if (tokens and isinstance(tokens[0], IdentToken)
            and tokens[0].lower_value not in ('and', 'or')):
        conjuncts.append(('leaf', Leaf(
            text=tokens[0].value, kind='media-type',
            media_type=tokens[0].lower_value)))
        tokens = tokens[1:]

    terms, connectives = [], []
    for token in tokens:
        if isinstance(token, ParenthesesBlock):
            terms.append(_parse_feature(token))
        elif isinstance(token, IdentToken) and token.lower_value in ('and', 'or'):
            connectives.append(token.lower_value)
        else:
            terms.append(_unknown_leaf(tinycss2.serialize([token]).strip()))

    # and 优先于 or：先按 or 分组，每组内部取 AND
    if terms:
        groups, group = [], [terms[0]]
        for connective, term in zip(connectives, terms[1:]):
            if connective == 'or':
                groups.append(group)
                group = [term]
            else:
                group.append(term)
        groups.append(group)
        parts = [g[0] if len(g) == 1 else ('and', g) for g in groups]
        condition = parts[0] if len(parts) == 1 else ('or', parts)
        conjuncts.append(condition)

    if not conjuncts:
        expr = ('true',)
    elif len(conjuncts) == 1:
        expr = conjuncts[0]
    else:
        expr = ('and', conjuncts)
    return ('not', expr) if negate else expr


def parse_media_query_list(prelude):
    """解析整条 @media 前言：逗号分隔的备选分支取 OR。"""
    queries = [_parse_query(part) for part in _split_top(prelude)]
    if len(queries) == 1:
        return queries[0]
    return ('or', queries)


# ---------------------------------------------------------------- 求值

def _eval_leaf(leaf, profile):
    if leaf.kind == 'width':
        device = profile.width_interval()
        if device.intersection(leaf.interval).is_empty():
            return NEVER, [
                f'{leaf.text}：与设备视口宽度 {profile.width_desc()} 无交集']
        if leaf.interval.covers(device):
            return ALWAYS, [
                f'{leaf.text}：覆盖设备全部视口宽度 {profile.width_desc()}']
        return SOMETIMES, [
            f'{leaf.text}：仅覆盖设备部分视口宽度 {profile.width_desc()}']
    if leaf.kind == 'color-scheme':
        schemes = set(profile.color_schemes)
        if leaf.scheme not in ('light', 'dark'):
            return UNKNOWN, [f'{leaf.text}：未知配色模式取值，保留不确定性']
        if leaf.scheme not in schemes:
            return NEVER, [
                f'{leaf.text}：设备配色模式为 '
                f'{"、".join(sorted(schemes))}，不相符']
        if schemes == {leaf.scheme}:
            return ALWAYS, [
                f'{leaf.text}：设备配色模式恒为 {leaf.scheme}']
        return SOMETIMES, [
            f'{leaf.text}：仅部分设备处于 {leaf.scheme} 模式']
    if leaf.kind == 'media-type':
        media_type = leaf.media_type
        if media_type == 'all':
            return ALWAYS, [f'{leaf.text}：匹配所有媒体类型']
        if media_type in NEVER_MATCH_TYPES:
            return NEVER, [
                f'{leaf.text}：媒体类型 {media_type} 已废弃，永不匹配']
        if media_type in KNOWN_MEDIA_TYPES:
            if media_type not in profile.media_types:
                return NEVER, [
                    f'{leaf.text}：设备媒体类型为 '
                    f'{"、".join(profile.media_types)}，不含 {media_type}']
            if len(profile.media_types) == 1:
                return ALWAYS, [
                    f'{leaf.text}：设备媒体类型恒为 {media_type}']
            return SOMETIMES, [
                f'{leaf.text}：仅部分设备属于 {media_type} 类型']
        return UNKNOWN, [
            f'{leaf.text}：未知媒体类型 {media_type}，保留不确定性']
    return UNKNOWN, [f'{leaf.text}：未知媒体特性，保留不确定性']


def _eval_and(evals):
    if any(status == NEVER for status, _ in evals):
        return NEVER, [
            reason for status, reasons in evals if status == NEVER
            for reason in reasons]
    if all(status == ALWAYS for status, _ in evals):
        return ALWAYS, [
            reason for _, reasons in evals for reason in reasons]
    status = (UNKNOWN if any(s == UNKNOWN for s, _ in evals)
              else SOMETIMES)
    return status, [
        reason for status, reasons in evals if status != ALWAYS
        for reason in reasons]


def _eval_or(evals):
    if any(status == ALWAYS for status, _ in evals):
        return ALWAYS, [
            reason for status, reasons in evals if status == ALWAYS
            for reason in reasons]
    if all(status == NEVER for status, _ in evals):
        return NEVER, [
            reason for _, reasons in evals for reason in reasons]
    status = (UNKNOWN if any(s == UNKNOWN for s, _ in evals)
              else SOMETIMES)
    return status, [
        reason for status, reasons in evals if status != NEVER
        for reason in reasons]


def _eval_not(evaluation):
    status, reasons = evaluation
    inverted = {ALWAYS: NEVER, NEVER: ALWAYS}.get(status, status)
    return inverted, [f'（取反）{reason}' for reason in reasons]


def eval_expr(expr, profile):
    tag = expr[0]
    if tag == 'true':
        return ALWAYS, []
    if tag == 'leaf':
        return _eval_leaf(expr[1], profile)
    if tag == 'and':
        return _eval_and([eval_expr(e, profile) for e in expr[1]])
    if tag == 'or':
        return _eval_or([eval_expr(e, profile) for e in expr[1]])
    if tag == 'not':
        return _eval_not(eval_expr(expr[1], profile))
    raise ValueError(f'未知表达式节点：{tag}')


# ---------------------------------------------------------------- 审计

@dataclass
class Resource:
    """一项资源的判定结果。"""

    name: str
    line: int
    depth: int
    status: str
    reasons: list = field(default_factory=list)

    @property
    def verdict(self):
        return VERDICT_LABELS[self.status]


def _walk_media(rule, chain, depth, profile, resources):
    prelude_text = tinycss2.serialize(rule.prelude).strip()
    expr = parse_media_query_list(rule.prelude)
    chain = chain + [(prelude_text, expr)]

    level_evals = []
    for level, (text, level_expr) in enumerate(chain, 1):
        status, reasons = eval_expr(level_expr, profile)
        level_evals.append(
            (status, [f'第{level}层 @media {text}｜{r}' for r in reasons]))
    status, reasons = _eval_and(level_evals)
    resources.append(Resource(
        name=f'@media {prelude_text}', line=rule.source_line, depth=depth,
        status=status, reasons=reasons))

    inner = tinycss2.parse_rule_list(
        rule.content or [], skip_whitespace=True, skip_comments=True)
    for child in inner:
        if isinstance(child, AtRule) and child.lower_at_keyword == 'media':
            _walk_media(child, chain, depth + 1, profile, resources)


def audit(css, profile):
    """审计样式表，返回每项资源的判定（含嵌套层级与条件出处）。"""
    rules = tinycss2.parse_stylesheet(
        css, skip_whitespace=True, skip_comments=True)
    resources = []
    has_unconditional = False
    for rule in rules:
        if isinstance(rule, AtRule) and rule.lower_at_keyword == 'media':
            _walk_media(rule, [], 1, profile, resources)
        elif rule.type == 'qualified-rule':
            has_unconditional = True
    if has_unconditional:
        resources.append(Resource(
            name='（无条件规则）', line=0, depth=0, status=ALWAYS,
            reasons=['不依赖任何媒体条件，对这批设备始终适用']))
    return resources


# ---------------------------------------------------------------- 报告

def format_report(resources, profile):
    lines = [f'设备档案：{profile.describe()}', '']
    if not resources:
        lines.append('未找到任何 @media 资源。')
    for index, resource in enumerate(resources, 1):
        indent = '  ' * (resource.depth - 1)
        where = f'第{resource.line}行' if resource.line else '顶层'
        lines.append(
            f'{indent}[{index}] {where} {resource.name}')
        lines.append(f'{indent}    判定：{resource.verdict}')
        if resource.reasons:
            lines.append(f'{indent}    依据：')
            for reason in resource.reasons:
                lines.append(f'{indent}      - {reason}')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='按设备档案审计嵌套 @media 资源的适用性')
    parser.add_argument('css_file', help='待审计的 CSS 文件')
    parser.add_argument('--min-width', type=float, default=None,
                        help='这批设备的最小视口宽度（px）')
    parser.add_argument('--max-width', type=float, default=None,
                        help='这批设备的最大视口宽度（px）')
    parser.add_argument('--color-scheme', default='light,dark',
                        help='设备支持的配色模式，逗号分隔（默认 light,dark）')
    parser.add_argument('--media-type', default='screen',
                        help='设备媒体类型，逗号分隔（默认 screen）')
    parser.add_argument('--json', action='store_true',
                        help='以 JSON 输出判定结果')
    args = parser.parse_args(argv)

    if (args.min_width is not None and args.max_width is not None
            and args.min_width > args.max_width):
        parser.error('--min-width 不能大于 --max-width')

    profile = DeviceProfile(
        min_width=args.min_width,
        max_width=args.max_width,
        color_schemes=tuple(
            s.strip() for s in args.color_scheme.split(',') if s.strip()),
        media_types=tuple(
            s.strip() for s in args.media_type.split(',') if s.strip()),
    )
    with open(args.css_file, encoding='utf-8') as f:
        resources = audit(f.read(), profile)

    if args.json:
        print(json.dumps({
            'profile': {
                'min_width': profile.min_width,
                'max_width': profile.max_width,
                'color_schemes': list(profile.color_schemes),
                'media_types': list(profile.media_types),
            },
            'resources': [{
                'name': r.name, 'line': r.line, 'depth': r.depth,
                'verdict': r.verdict, 'status': r.status,
                'reasons': r.reasons,
            } for r in resources],
        }, ensure_ascii=False, indent=2))
    else:
        print(format_report(resources, profile))
    return 0


if __name__ == '__main__':
    sys.exit(main())
