"""Reconstruct compatible original-record ranges from truncated terminal snippets.

Two inspection points may capture the same terminal record through windows of different
widths, and a capture may insert an ellipsis at the clipped edge.  Given the captured
fragments, their window widths, clip directions and the set of candidate original records,
this module locates every range of each candidate that is compatible with all of the
evidence.

Visible widths are measured with :func:`wcwidth.wcswidth`, or :func:`wcwidth.wcstwidth`
when a per-terminal wide-character policy (``term_program``) is supplied.  Each inspection
point may use its own policy, so all range bookkeeping is done in grapheme-index space;
display columns are reported alongside as policy-specific annotations.

An inserted ellipsis never counts as original text, while an ellipsis character that is
genuinely part of the record is kept as literal anchor text.  When a repeated phrase
matches at several positions, every compatible position is retained.
"""

from __future__ import annotations

# std imports
from typing import NamedTuple, Optional, Iterable, Sequence

# local
from .grapheme import iter_graphemes
from ._wcswidth import wcswidth, wcstwidth

# Accepted aliases for the clipped edge.  A "right" clip keeps a prefix of the record and
# inserts the ellipsis on the right; a "left" clip keeps a suffix and inserts on the left.
_SIDE_ALIASES = {
    'right': 'right', 'r': 'right', 'prefix': 'right', 'head': 'right',
    'left': 'left', 'l': 'left', 'suffix': 'left', 'tail': 'left',
}


class Snippet(NamedTuple):
    """One captured fragment of a terminal record.

    :param text: The captured fragment, including any inserted edge ellipsis.
    :param width: The collection window width, in display cells.
    :param side: Which edge was clipped: ``'right'`` (prefix capture, ellipsis on the
        right) or ``'left'`` (suffix capture, ellipsis on the left).  Aliases
        ``'r'``/``'prefix'`` and ``'l'``/``'suffix'`` are accepted.
    :param ellipsis: The ellipsis token the collector inserts at a clipped edge.
    :param ambiguous_width: Width applied to East Asian Ambiguous characters (1 or 2).
    :param term_program: Terminal wide-character policy.  ``False`` (default) uses the
        standard tables; otherwise a canonical terminal name as accepted by
        :func:`wcwidth.wcstwidth`.
    """

    text: str
    width: int
    side: str = 'right'
    ellipsis: str = '…'
    ambiguous_width: int = 1
    term_program: bool | str = False

    def normalized_side(self) -> str:
        """Return the validated clip side, ``'left'`` or ``'right'``."""
        try:
            return _SIDE_ALIASES[self.side.lower()]
        except (AttributeError, KeyError) as err:
            raise ValueError(
                f"side must be one of {sorted(set(_SIDE_ALIASES))}, got {self.side!r}"
            ) from err


class ColumnSpan(NamedTuple):
    """Display-column span of a range as measured under one snippet's policy."""

    snippet: int
    start_column: int
    end_column: int


class TextRange(NamedTuple):
    """A compatible range inside a candidate record.

    Grapheme indices are policy-independent and identify the range exactly; ``text`` is the
    corresponding substring.  ``columns`` reports the display-column span under the policy
    of every snippet whose anchor contributed to this range.
    """

    start_grapheme: int
    end_grapheme: int
    text: str
    columns: tuple[ColumnSpan, ...]


class Reconstruction(NamedTuple):
    """All compatible ranges found within one candidate original record."""

    candidate: str
    ranges: tuple[TextRange, ...]


def _measure(text: str, ambiguous_width: int, term_program: bool | str) -> int:
    """Measure visible width under the given ambiguous-width and terminal policy."""
    if term_program:
        return wcstwidth(text, ambiguous_width=ambiguous_width,
                         term_program=term_program)
    return wcswidth(text, ambiguous_width=ambiguous_width)


def _graphemes(text: str) -> list[str]:
    return list(iter_graphemes(text))


def _prefix_columns(
    graphemes: Sequence[str],
    ambiguous_width: int,
    term_program: bool | str,
) -> Optional[list[int]]:
    """Return prefix column offsets, or ``None`` if a grapheme has no printable width."""
    columns = [0]
    for grapheme in graphemes:
        grapheme_width = _measure(grapheme, ambiguous_width, term_program)
        if grapheme_width < 0:
            return None
        columns.append(columns[-1] + grapheme_width)
    return columns


def _strip_inserted_ellipsis(
    fragment_graphemes: Sequence[str],
    ellipsis_graphemes: Sequence[str],
    side: str,
) -> tuple[list[str], bool]:
    """Remove a single inserted edge ellipsis, returning ``(anchor, inserted)``.

    Only one ellipsis token is treated as inserted; any further ellipsis occurrences stay
    in the anchor and must match the original record literally.
    """
    token_len = len(ellipsis_graphemes)
    if token_len == 0 or len(fragment_graphemes) < token_len:
        return list(fragment_graphemes), False
    if side == 'right':
        edge = fragment_graphemes[-token_len:]
        if list(edge) == list(ellipsis_graphemes):
            return list(fragment_graphemes[:-token_len]), True
    else:
        edge = fragment_graphemes[:token_len]
        if list(edge) == list(ellipsis_graphemes):
            return list(fragment_graphemes[token_len:]), True
    return list(fragment_graphemes), False


def _prepare_snippet(
    snippet: Snippet,
) -> tuple[str, list[tuple[list[str], bool]]]:
    """Validate a snippet and enumerate its anchor readings.

    An edge ellipsis is ambiguous: it may be the collector-inserted token or a literal
    ellipsis belonging to the record.  Both readings are returned, each as
    ``(anchor graphemes, inserted)``; the caller unions their compatible ranges.
    """
    if not isinstance(snippet.width, int) or snippet.width <= 0:
        raise ValueError(
            f"window width must be a positive integer, got {snippet.width!r}")
    if snippet.ambiguous_width not in (1, 2):
        raise ValueError(
            f"ambiguous_width must be 1 or 2, got {snippet.ambiguous_width!r}")
    if not snippet.ellipsis:
        raise ValueError("ellipsis token must not be empty")
    side = snippet.normalized_side()
    fragment_graphemes = _graphemes(snippet.text)
    if not fragment_graphemes:
        raise ValueError("snippet text must not be empty")
    fragment_width = 0
    for grapheme in fragment_graphemes:
        grapheme_width = _measure(
            grapheme, snippet.ambiguous_width, snippet.term_program)
        if grapheme_width < 0:
            raise ValueError(
                f"snippet {snippet.text!r} contains an unprintable control character")
        fragment_width += grapheme_width
    if fragment_width > snippet.width:
        raise ValueError(
            f"snippet {snippet.text!r} needs {fragment_width} cells but window width is "
            f"{snippet.width}")

    ellipsis_graphemes = _graphemes(snippet.ellipsis)
    readings: list[tuple[list[str], bool]] = [
        # Literal reading: the whole fragment is original text, nothing inserted.
        (list(fragment_graphemes), False),
    ]
    stripped, inserted = _strip_inserted_ellipsis(
        fragment_graphemes, ellipsis_graphemes, side)
    if inserted:
        readings.append((stripped, True))
    return side, readings


def _anchor_intervals(
    candidate_graphemes: Sequence[str],
    candidate_columns: Sequence[int],
    anchor: Sequence[str],
    side: str,
    inserted: bool,
    window_width: int,
) -> Optional[list[tuple[int, int, int, int]]]:
    """Locate every compatible anchor occurrence, as grapheme and column intervals.

    Matching is occurrence based: every substring occurrence of the anchor is a candidate
    position, so a phrase that repeats in the record yields one interval per occurrence.
    An inserted ellipsis means the record overflowed the window (its width exceeds
    *window_width*) and hidden original text remains on the clipped side of the
    occurrence.  Returns ``None`` when no occurrence is compatible.
    """
    total_width = candidate_columns[-1]
    overflow = total_width > window_width
    if not anchor:
        if inserted and not overflow:
            return None
        return []

    anchor_len = len(anchor)
    limit = len(candidate_graphemes) - anchor_len
    intervals: list[tuple[int, int, int, int]] = []
    for start in range(limit + 1):
        if candidate_graphemes[start:start + anchor_len] != anchor:
            continue
        end = start + anchor_len
        start_column = candidate_columns[start]
        end_column = candidate_columns[end]
        if inserted:
            if not overflow:
                continue
            if side == 'right' and end_column >= total_width:
                continue
            if side == 'left' and start_column <= 0:
                continue
        intervals.append((start, end, start_column, end_column))
    return intervals or None


def _merge_ranges(
    per_snippet: Sequence[Sequence[tuple[int, int, int, int]]],
    candidate_graphemes: Sequence[str],
) -> tuple[TextRange, ...]:
    """Collect per-snippet intervals into distinct, deduplicated ranges.

    Intervals sharing the same ``(start, end)`` grapheme bounds merge into one range whose
    ``columns`` carries every contributing snippet's column span.  Intervals with different
    bounds are kept as separate ranges even when they overlap or nest, so that every
    compatible position (including repeated phrases) is retained.
    """
    grouped: dict[tuple[int, int], list[ColumnSpan]] = {}
    order: list[tuple[int, int]] = []
    for snippet_index, intervals in enumerate(per_snippet):
        for start, end, col_start, col_end in intervals:
            key = (start, end)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(ColumnSpan(snippet_index, col_start, col_end))

    ranges = []
    for start, end in sorted(order):
        spans = tuple(sorted(set(grouped[(start, end)])))
        ranges.append(TextRange(
            start_grapheme=start,
            end_grapheme=end,
            text=''.join(candidate_graphemes[start:end]),
            columns=spans,
        ))
    return tuple(ranges)


def _snippet_intervals(
    candidate_graphemes: Sequence[str],
    candidate_columns: Sequence[int],
    side: str,
    readings: Sequence[tuple[list[str], bool]],
    window_width: int,
) -> Optional[list[tuple[int, int, int, int]]]:
    """Union of compatible intervals across every anchor reading of one snippet.

    ``None`` means every reading is incompatible.  An empty list means the snippet is
    compatible (for example an ellipsis-only fragment whose inserted reading has no anchor
    text) but locates no range.
    """
    union: list[tuple[int, int, int, int]] = []
    compatible = False
    for anchor, inserted in readings:
        intervals = _anchor_intervals(
            candidate_graphemes, candidate_columns, anchor, side, inserted,
            window_width)
        if intervals is None:
            continue
        compatible = True
        union.extend(intervals)
    if not compatible:
        return None
    # Deduplicate identical intervals while preserving order.
    seen: set[tuple[int, int, int, int]] = set()
    unique: list[tuple[int, int, int, int]] = []
    for interval in union:
        if interval not in seen:
            seen.add(interval)
            unique.append(interval)
    return unique


def snippet_ranges(snippet: Snippet, candidate: str) -> list[TextRange]:
    """Enumerate every range of *candidate* compatible with a single snippet."""
    side, readings = _prepare_snippet(snippet)
    candidate_graphemes = _graphemes(candidate)
    candidate_columns = _prefix_columns(
        candidate_graphemes, snippet.ambiguous_width, snippet.term_program)
    if candidate_columns is None:
        return []
    intervals = _snippet_intervals(
        candidate_graphemes, candidate_columns, side, readings, snippet.width)
    if not intervals:
        return []
    ranges = _merge_ranges([intervals], candidate_graphemes)
    return [
        TextRange(
            start_grapheme=text_range.start_grapheme,
            end_grapheme=text_range.end_grapheme,
            text=text_range.text,
            columns=tuple(
                ColumnSpan(0, span.start_column, span.end_column)
                for span in text_range.columns),
        )
        for text_range in ranges
    ]


def reconcile(candidate: str, snippets: Sequence[Snippet]) -> Optional[Reconstruction]:
    """Reconcile all snippets against one candidate record.

    Returns a :class:`Reconstruction` with the union of compatible ranges, or ``None`` when
    the candidate is incompatible with any snippet.
    """
    candidate_graphemes = _graphemes(candidate)
    per_snippet: list[list[tuple[int, int, int, int]]] = []

    for snippet in snippets:
        side, readings = _prepare_snippet(snippet)
        candidate_columns = _prefix_columns(
            candidate_graphemes, snippet.ambiguous_width, snippet.term_program)
        if candidate_columns is None:
            return None
        intervals = _snippet_intervals(
            candidate_graphemes, candidate_columns, side, readings,
            snippet.width)
        if intervals is None:
            return None
        per_snippet.append(intervals)

    ranges = _merge_ranges(per_snippet, candidate_graphemes)
    return Reconstruction(candidate=candidate, ranges=ranges)


def find_originals(
    candidates: Iterable[str],
    snippets: Sequence[Snippet],
) -> list[Reconstruction]:
    """Find compatible original-record ranges for every candidate record.

    Each candidate is tested against all snippets.  A candidate that contradicts any
    snippet is dropped; surviving candidates carry the union of ranges compatible with the
    evidence, ordered by grapheme position.  Candidate order is preserved (duplicates
    removed) and every matching position of a repeated phrase is retained.
    """
    snippets = tuple(snippets)
    results: list[Reconstruction] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        reconstruction = reconcile(candidate, snippets)
        if reconstruction is not None:
            results.append(reconstruction)
    return results
