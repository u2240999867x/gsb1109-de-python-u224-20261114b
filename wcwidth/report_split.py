"""
Recover column splits of fixed-width report rows whose fields were redacted.

A batch of fixed-width reports was archived after customer names of differing
lengths were covered with one repeated symbol.  A side table only records the
range of columns each redacted value could have occupied before masking.  The
remaining fields have known alignments, and some columns are known integer
fields.

This module enumerates every split still consistent with an observed row and
the side table, and reports the parts that are forced ("交账"): even when a
row has two interpretations, a column is certain whenever every interpretation
assigns it the same width.

Several masked (missing) segments are supported: each narrows the boundary
positions the dynamic programming may reach, so the missing segments constrain
one another through the fixed fields and the total row width.

Field values are assumed to contain no embedded spaces; spaces only occur as
edge padding dictated by the field alignment.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterator

__all__ = (
    'Alignment',
    'FieldSpec',
    'MaskedField',
    'ReportSpec',
    'RowSplit',
    'ReportSplitError',
    'InfeasibleRowError',
    'possible_splits',
    'certain_split',
    'split_report_row',
)

_DIGITS = frozenset('0123456789')


class Alignment(Enum):
    """Field alignment inside its fixed-width column."""

    LEFT = 'left'
    RIGHT = 'right'


@dataclass(frozen=True)
class FieldSpec:
    """
    Description of one report field (column).

    :param name: Field identifier, used as the key of result mappings.
    :param align: Alignment of the value inside its column.
    :param is_integer: Whether the value is a known integer field; an
        unmasked integer column only accepts an all-digit body.  A masked
        integer column is still validated by its :class:`MaskedField` range.
    """

    name: str
    align: Alignment = Alignment.LEFT
    is_integer: bool = False


@dataclass(frozen=True)
class MaskedField:
    """
    A field whose value was covered by the repeated masking symbol.

    The side table states how many columns the value could have occupied
    before masking, inclusive from ``min_width`` through ``max_width``.  The
    observed run of masking symbols in the row must have a length inside this
    range.

    :param name: Field identifier, matching a :class:`FieldSpec`.
    :param min_width: Minimum possible value width in columns.
    :param max_width: Maximum possible value width in columns.
    """

    name: str
    min_width: int
    max_width: int


@dataclass(frozen=True)
class ReportSpec:
    """
    Description shared by every row of one archived report.

    :param fields: Field order and alignment metadata, one entry per column.
    :param mask_symbol: The single symbol repeated over each redacted value.
    """

    fields: tuple[FieldSpec, ...]
    mask_symbol: str = '*'


@dataclass(frozen=True)
class RowSplit:
    """
    One feasible segmentation of a row into field columns.

    :param widths: Column width in cells, per field name.
    :param values: Substring actually observed in each column.
    :param starts: Zero-based column start offset, per field name.
    """

    widths: dict[str, int]
    values: dict[str, str]
    starts: dict[str, int]


class ReportSplitError(ValueError):
    """Base class for errors while splitting a report row."""


class InfeasibleRowError(ReportSplitError):
    """Raised when a row admits no split consistent with the specification."""


def _check_spec(spec: ReportSpec, masked: tuple[MaskedField, ...]) -> None:
    if not spec.fields:
        raise ReportSplitError('ReportSpec.fields must not be empty')
    if len(spec.mask_symbol) != 1:
        raise ReportSplitError('mask_symbol must be a single character')
    if spec.mask_symbol == ' ':
        raise ReportSplitError('mask_symbol cannot be a space, which is reserved for padding')
    names = [item.name for item in spec.fields]
    if len(names) != len(set(names)):
        raise ReportSplitError('duplicate field names in ReportSpec.fields')
    known = set(names)
    seen: set[str] = set()
    for item in masked:
        if item.name not in known:
            raise ReportSplitError(f'masked field {item.name!r} is not in spec.fields')
        if item.name in seen:
            raise ReportSplitError(f'duplicate masked field entry {item.name!r}')
        if item.min_width < 1 or item.max_width < item.min_width:
            raise ReportSplitError(f'invalid width range for masked field {item.name!r}')
        seen.add(item.name)


def _is_integer_body(body: str) -> bool:
    return bool(body) and all(char in _DIGITS for char in body)


def _span_is_valid(text: str, spec: FieldSpec, mask: str,
                   side: MaskedField | None) -> bool:
    """Check one candidate column slice against alignment, type and range."""
    if not text:
        return False
    if spec.align is Alignment.LEFT:
        body = text.rstrip(' ')
        padding = text[len(body):]
    else:
        body = text.lstrip(' ')
        padding = text[:len(text) - len(body)]
    if not body or ' ' in body or mask in padding:
        return False
    if side is None:
        if mask in body:
            return False
        return not spec.is_integer or _is_integer_body(body)
    if any(char != mask for char in body):
        return False
    return side.min_width <= len(body) <= side.max_width


def _predecessors(row: str, spec: ReportSpec,
                  sides: dict[str, MaskedField], length: int
                  ) -> list[dict[int, list[int]]]:
    """
    Build predecessor tables with a forward dynamic program.

    For every field and every reachable end offset, record each start offset
    from which a valid column slice reaches that end.  Masked fields only
    accept all-mask bodies inside their side-table width range, so the missing
    segments jointly restrict the reachable boundaries.
    """
    tables: list[dict[int, list[int]]] = []
    starts = (0,)
    for field in spec.fields:
        table: dict[int, list[int]] = {}
        side = sides.get(field.name)
        for begin in starts:
            for end in range(begin + 1, length + 1):
                if _span_is_valid(row[begin:end], field, spec.mask_symbol, side):
                    table.setdefault(end, []).append(begin)
        if not table:
            return []
        tables.append(table)
        starts = tuple(table)
    return tables


def _reconstruct(spec: ReportSpec, row: str,
                 tables: list[dict[int, list[int]]]
                 ) -> Iterator[RowSplit]:
    """Walk predecessor tables backwards to emit every feasible split."""
    names = tuple(field.name for field in spec.fields)
    ends = [0] * len(spec.fields)

    def walk(index: int, end: int) -> Iterator[RowSplit]:
        if index < 0:
            offset = 0
            widths: dict[str, int] = {}
            values: dict[str, str] = {}
            starts: dict[str, int] = {}
            for name, boundary in zip(names, ends):
                widths[name] = boundary - offset
                values[name] = row[offset:boundary]
                starts[name] = offset
                offset = boundary
            yield RowSplit(widths=widths, values=values, starts=starts)
            return
        for begin in tables[index][end]:
            ends[index] = end
            yield from walk(index - 1, begin)

    yield from walk(len(spec.fields) - 1, len(row))


def possible_splits(row: str, spec: ReportSpec,
                    masked: tuple[MaskedField, ...] | list[MaskedField]
                    ) -> tuple[RowSplit, ...]:
    """
    Return every feasible split of *row* under *spec* and the side table.

    :param row: One archived fixed-width line, including padding spaces.
    :param spec: Shared field order, alignment and type metadata.
    :param masked: Side-table width ranges for each redacted field; multiple
        masked fields constrain one another through the fixed fields and the
        total row width.
    :returns: All feasible :class:`RowSplit` instances, deterministically
        ordered by column boundaries.
    :raises InfeasibleRowError: When no split is consistent with the row.

    Example, a left-aligned masked name followed by a right-aligned integer::

        >>> spec = ReportSpec((
        ...     FieldSpec('name'),
        ...     FieldSpec('amount', Alignment.RIGHT, is_integer=True),
        ... ))
        >>> row = '****   42'
        >>> splits = possible_splits(row, spec, (MaskedField('name', 3, 6),))
        >>> len(splits)
        4
        >>> {split.widths['amount'] for split in splits} == {2, 3, 4, 5}
        True
    """
    masked_tuple = tuple(masked)
    _check_spec(spec, masked_tuple)
    if not row or '\n' in row:
        raise ReportSplitError('row must be a single non-empty line')
    sides = {item.name: item for item in masked_tuple}
    tables = _predecessors(row, spec, sides, len(row))
    if not tables or len(row) not in tables[-1]:
        raise InfeasibleRowError('row has no feasible split')
    return tuple(_reconstruct(spec, row, tables))


def certain_split(splits: tuple[RowSplit, ...]) -> dict[str, int | None]:
    """
    Collapse feasible splits into the width each field is forced to take.

    Even when the whole row has two interpretations, a field is determined
    whenever every interpretation gives it the same width.

    :param splits: Feasible splits, as returned by :func:`possible_splits`.
    :returns: Field name to column width when forced, or ``None`` when the
        feasible splits disagree on that field's width.

    Example::

        >>> spec = ReportSpec((
        ...     FieldSpec('name'),
        ...     FieldSpec('amount', Alignment.RIGHT, is_integer=True),
        ... ))
        >>> splits, certain = split_report_row(
        ...     '****   42', spec, (MaskedField('name', 3, 6),))
        >>> certain['name'] is None
        True
        >>> certain['amount'] is None
        True
    """
    if not splits:
        raise ReportSplitError('cannot derive a certain split from zero candidates')
    result: dict[str, int | None] = {}
    for name in splits[0].widths:
        widths = {split.widths[name] for split in splits}
        result[name] = widths.pop() if len(widths) == 1 else None
    return result


def split_report_row(row: str, spec: ReportSpec,
                     masked: tuple[MaskedField, ...] | list[MaskedField]
                     ) -> tuple[tuple[RowSplit, ...], dict[str, int | None]]:
    """
    Enumerate feasible splits and the widths forced across all of them.

    This is the one-call entry point combining :func:`possible_splits` and
    :func:`certain_split`.

    :param row: One archived fixed-width line, including padding spaces.
    :param spec: Shared field order, alignment and type metadata.
    :param masked: Side-table width ranges for each redacted field.
    :returns: A ``(splits, certain)`` pair; see the two wrapped functions.
    :raises InfeasibleRowError: When no split is consistent with the row.
    """
    splits = possible_splits(row, spec, masked)
    return splits, certain_split(splits)
