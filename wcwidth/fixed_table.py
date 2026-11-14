"""
Split fixed-width terminal tables into fields by display column boundaries.

Terminal inventory tables align fields on a fixed cell grid: narrow characters occupy one cell and
wide characters (such as CJK ideographs) occupy two.  A wide character may therefore straddle a
field boundary, and combining marks or variation selectors must travel with their base character.

The functions in this module segment a row into :class:`CharSpan` records that keep each grapheme
cluster together, classify it, and carry its original source range, then group those spans into
:class:`FieldSplit` fields.

Example::

    >>> row = 'AB中DE'  # columns: A=0, B=1, 中=2-3, D=4, E=5
    >>> fields = split_fixed_width(row, (0, 3, 6))
    >>> fields[0].text
    'AB中'
    >>> fields[1].text
    'DE'
    >>> # the wide ideograph straddles the boundary at column 3, it is recorded in both fields
    >>> fields[0].spans[-1].kind
    <SpanKind.FRAGMENT: 'fragment'>
    >>> fields[1].spans[0].kind
    <SpanKind.FRAGMENT: 'fragment'>
    >>> # but it keeps a single source range and is restored exactly once on reassembly
    >>> fields[0].spans[-1].src_start == fields[1].spans[0].src_start
    True
    >>> reassemble_fixed_width(fields) == row
    True
"""

from __future__ import annotations

# std imports
import enum
import unicodedata
from typing import Iterator, NamedTuple, Sequence

__lazy_modules__ = [
    "wcwidth._width",
    "wcwidth.grapheme",
]

# local
from ._width import width
from .grapheme import iter_graphemes


class SpanKind(enum.Enum):
    """
    How a grapheme cluster occupies a field.

    - ``FULL``: the whole cluster lies inside the field, no boundary runs through its cells.
    - ``FRAGMENT``: a wide cluster straddles a field boundary; the same source range is reported as
      a fragment by every field the cluster overlaps, and the cluster is owned (emitted in
      :attr:`FieldSplit.text`) by the field containing its first cell.
    - ``SPACE``: the cluster is whitespace wholly inside the field, a real space rather than a
      fragment; a wide space straddling a boundary is still reported as ``FRAGMENT``.
    """

    FULL = 'full'
    FRAGMENT = 'fragment'
    SPACE = 'space'


class CharSpan(NamedTuple):
    """
    One grapheme cluster located on the display-cell grid.

    :param text: The grapheme cluster text, including any combining marks or variation selectors
        that belong to the base character.
    :param col_start: First display cell occupied by the cluster.
    :param col_end: One past the last display cell occupied by the cluster; equal to *col_start*
        for zero-width clusters.
    :param src_start: Index of the first code unit of the cluster in the source line.
    :param src_end: One past the last code unit of the cluster in the source line.
    :param kind: Classification relative to the field the span is reported for.
    """

    text: str
    col_start: int
    col_end: int
    src_start: int
    src_end: int
    kind: SpanKind


class FieldSplit(NamedTuple):
    """
    One field extracted from a fixed-width row.

    :param start: First display cell of the field (inclusive).
    :param end: One past the last display cell of the field.
    :param text: Field content formed from the spans owned by the field, so that concatenating the
        text of every field restores the source row without duplicating straddling clusters.
    :param src_start: First source index covered by an owned span.
    :param src_end: One past the last source index covered by an owned span.
    :param spans: Every cluster overlapping the field, including boundary fragments owned by a
        neighbouring field.
    """

    start: int
    end: int
    text: str
    src_start: int
    src_end: int
    spans: tuple[CharSpan, ...]


def _is_space_cluster(cluster: str) -> bool:
    """Return True when a grapheme cluster consists of a space separator plus its marks."""
    saw_base = False
    for char in cluster:
        if unicodedata.category(char) == 'Mn':
            continue
        if unicodedata.category(char) != 'Zs':
            return False
        saw_base = True
    return saw_base


def iter_char_spans(
    line: str,
    *,
    ambiguous_width: int = 1,
    term_program: bool | str = False,
) -> Iterator[CharSpan]:
    r"""
    Yield a :class:`CharSpan` for every grapheme cluster in *line*.

    Grapheme clustering (UAX #29) keeps combining accents and variation selectors attached to their
    base character.  Zero-width clusters are merged into the preceding span so marks never become
    detached at a field boundary.  The :attr:`CharSpan.kind` is :attr:`SpanKind.SPACE` for
    whitespace and :attr:`SpanKind.FULL` otherwise; :func:`split_fixed_width` reclassifies spans as
    :attr:`SpanKind.FRAGMENT` when a boundary runs through them.

    :param line: A single table row without line separators.
    :param ambiguous_width: Width to use for East Asian Ambiguous characters, ``1`` (default) or
        ``2``.
    :param term_program: Terminal software identifier for table correction, see
        :func:`wcwidth.width`.

    Example::

        >>> spans = list(iter_char_spans('A 中'))
        >>> spans[0].text, spans[0].col_start, spans[0].col_end, spans[0].kind
        ('A', 0, 1, <SpanKind.FULL: 'full'>)
        >>> spans[1].text, spans[1].kind
        (' ', <SpanKind.SPACE: 'space'>)
        >>> spans[2].text, spans[2].col_start, spans[2].col_end
        ('中', 2, 4)
        >>> span = next(iter(iter_char_spans('e\u0301')))
        >>> span.text, span.src_start, span.src_end
        ('e\u0301', 0, 2)
    """
    if not isinstance(line, str):
        raise TypeError(f"line must be str, got {type(line).__name__}")
    if '\n' in line or '\r' in line:
        raise ValueError("line must be a single row, without newline or carriage return")
    if any(unicodedata.category(char) == 'Cc' for char in line):
        raise ValueError(
            "line must contain printable text only; control characters such as "
            "TAB have no fixed meaning on a display-cell grid")

    col = 0
    src_idx = 0
    pending_text = ''
    pending_start = 0
    current: CharSpan | None = None

    def _extend(span: CharSpan, extra: str, new_src_end: int) -> CharSpan:
        """Attach a zero-width mark, keeping the base span's cells and kind."""
        return span._replace(text=span.text + extra, src_end=new_src_end)

    for cluster in iter_graphemes(line):
        cluster_width = width(
            cluster,
            control_codes='ignore',
            ambiguous_width=ambiguous_width,
            term_program=term_program,
        )
        src_end = src_idx + len(cluster)

        if cluster_width == 0:
            # Zero-width marks belong to the preceding base; buffer leading marks until the first
            # visible cluster exists.
            if current is not None:
                current = _extend(current, cluster, src_end)
            else:
                if not pending_text:
                    pending_start = src_idx
                pending_text += cluster
            src_idx = src_end
            continue

        if current is not None:
            yield current
        kind = SpanKind.SPACE if _is_space_cluster(cluster) else SpanKind.FULL
        text = pending_text + cluster
        span_start = pending_start if pending_text else src_idx
        current = CharSpan(text, col, col + cluster_width, span_start, src_end, kind)
        pending_text = ''
        col += cluster_width
        src_idx = src_end

    if current is not None:
        yield current
    elif pending_text:
        # Only zero-width marks on the row: report them at column 0 rather than dropping them.
        yield CharSpan(pending_text, 0, 0, pending_start, len(line), SpanKind.FULL)


def _validate_boundaries(columns: Sequence[int]) -> tuple[int, ...]:
    """Validate field boundary columns, which must start at 0 and strictly increase."""
    boundaries = list(columns)
    if not boundaries:
        raise ValueError("columns must contain at least one field boundary")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in boundaries):
        raise TypeError("column boundaries must be integers")
    if boundaries[0] != 0:
        raise ValueError(
            f"first column boundary must be 0 (field start), got {boundaries[0]}")
    if any(later <= earlier for earlier, later in zip(boundaries, boundaries[1:])):
        raise ValueError(f"column boundaries must be strictly increasing: {boundaries}")
    return tuple(boundaries)


def split_fixed_width(
    line: str,
    columns: Sequence[int],
    *,
    ambiguous_width: int = 1,
    term_program: bool | str = False,
) -> tuple[FieldSplit, ...]:
    r"""
    Split one fixed-width row into fields along display-cell boundaries.

    *columns* gives the 0-based start cell of every field and must begin with ``0``; the final
    field extends to the end of the row.  Wide clusters that straddle a boundary are reported as
    :attr:`SpanKind.FRAGMENT` spans in every field their cells overlap, but the cluster text is
    owned by the field containing its first cell, so joining the field text restores the row
    exactly once.  Whitespace that fits inside a field is reported as :attr:`SpanKind.SPACE`;
    only a boundary running through a wide space turns it into a fragment.

    :param line: A single table row.
    :param columns: Field start columns, e.g. ``(0, 4, 8)`` defines fields at cells ``0-3`` and
        ``4-7`` plus a trailing field.
    :param ambiguous_width: Width to use for East Asian Ambiguous characters, ``1`` (default) or
        ``2``.
    :param term_program: Terminal software identifier for table correction, see
        :func:`wcwidth.width`.
    :returns: One :class:`FieldSplit` per field, including empty fields past the end of the row.

    Example::

        >>> fields = split_fixed_width('AB CD', (0, 3, 4))
        >>> fields[0].text, fields[1].text, fields[2].text
        ('AB ', 'C', 'D')
        >>> [span.kind for span in fields[0].spans]
        [<SpanKind.FULL: 'full'>, <SpanKind.FULL: 'full'>, <SpanKind.SPACE: 'space'>]
    """
    boundaries = _validate_boundaries(columns)
    spans = list(iter_char_spans(
        line, ambiguous_width=ambiguous_width, term_program=term_program))
    row_width = spans[-1].col_end if spans else 0
    ends = boundaries[1:] + (max(boundaries[-1], row_width),)

    results: list[FieldSplit] = []
    for field_index, field_start in enumerate(boundaries):
        field_end = ends[field_index]
        overlapping = [
            span for span in spans
            if (field_start <= span.col_start < field_end
                if span.col_end == span.col_start
                else span.col_start < field_end and span.col_end > field_start)
        ]

        field_spans: list[CharSpan] = []
        owned_text: list[str] = []
        owned_src_start = -1
        owned_src_end = -1
        for span in overlapping:
            if not (span.col_start >= field_start and span.col_end <= field_end):
                # A boundary runs through the cluster's cells; this takes precedence so a wide
                # space straddling a boundary is still reported as a fragment.
                kind = SpanKind.FRAGMENT
            elif span.kind is SpanKind.SPACE:
                kind = SpanKind.SPACE
            else:
                kind = SpanKind.FULL
            field_spans.append(span._replace(kind=kind))

            if field_start <= span.col_start < field_end:
                owned_text.append(span.text)
                if owned_src_start < 0:
                    owned_src_start = span.src_start
                owned_src_end = span.src_end

        if owned_src_start < 0:
            # No cluster starts here: either an empty trailing field or a field whose first cell
            # is the second half of a neighbour's wide cluster.
            if overlapping:
                owned_src_start = owned_src_end = overlapping[0].src_start
            else:
                owned_src_start = owned_src_end = len(line)

        results.append(FieldSplit(
            start=field_start,
            end=field_end,
            text=''.join(owned_text),
            src_start=owned_src_start,
            src_end=owned_src_end,
            spans=tuple(field_spans),
        ))
    return tuple(results)


def parse_fixed_width_table(
    lines: Sequence[str],
    columns: Sequence[int],
    *,
    ambiguous_width: int = 1,
    term_program: bool | str = False,
) -> tuple[tuple[FieldSplit, ...], ...]:
    """
    Split every row of a fixed-width table using the same field boundaries.

    See :func:`split_fixed_width` for boundary and fragment semantics.

    Example::

        >>> table = parse_fixed_width_table(['AB中DE', 'FG  HI'], (0, 3, 6))
        >>> [row[0].text for row in table]
        ['AB中', 'FG ']
    """
    return tuple(
        split_fixed_width(
            line,
            columns,
            ambiguous_width=ambiguous_width,
            term_program=term_program,
        )
        for line in lines
    )


def reassemble_fixed_width(fields: Sequence[FieldSplit]) -> str:
    """
    Rejoin split fields into the original row.

    Each cluster is restored exactly once: spans owned by a field are emitted in field and span
    order, and boundary fragments shared between neighbouring fields are deduplicated by their
    source range.

    :param fields: Field results from :func:`split_fixed_width`.
    :returns: The reconstructed source row.

    Example::

        >>> row = 'AB中DE'
        >>> reassemble_fixed_width(split_fixed_width(row, (0, 3, 6))) == row
        True
    """
    parts: list[str] = []
    seen_ranges: set[tuple[int, int]] = set()
    for field in fields:
        for span in field.spans:
            if not field.start <= span.col_start < field.end:
                continue
            source_range = (span.src_start, span.src_end)
            if source_range in seen_ranges:
                continue
            seen_ranges.add(source_range)
            parts.append(span.text)
    return ''.join(parts)
