"""
Incremental re-layout of cached terminal lines across Unicode width rule changes.

A historical line cache only retains, per line, the pieces needed to reconstruct its
layout without keeping the full measurement state:

- ``clusters``: grapheme cluster spans, ``(start, end)`` string indices per cluster;
- ``offsets``: the display column each cluster starts at;
- ``summary``: a short digest of the original text, enough to detect a stale entry.

Fixed positioning points (anchors) are not cached: they are re-derived from the original
text at rebuild time.  A carriage return, for example, re-aligns the cursor at absolute
column 0 no matter how far preceding clusters drifted.

When the Unicode width rules are upgraded, most lines keep identical layout.  Instead of
re-measuring every line, the two rule sets are diffed and only the affected clusters are
re-laid.  A cluster whose width grows from one cell to two pushes every later column to the
right until an anchor is reached, where the accumulated drift is discarded and layout
re-synchronizes.  A rule change that only moves a cluster boundary while the total width is
unchanged still invalidates the string-index-to-column reverse lookup, which is rebuilt.

The incremental result is validated column by column against a from-scratch recomputation:
:meth:`LineLayoutCache.verify_line` asserts the two agree for every string index of a line.
"""

from __future__ import annotations

# std imports
from dataclasses import dataclass

from typing import NamedTuple

__lazy_modules__ = [
    "wcwidth.bisearch",
    "wcwidth.grapheme",
]
# local
from ._constants import _clamp_ambiguous_width
from .bisearch import bisearch
from .grapheme import iter_graphemes

__all__ = ('WidthRule', 'WidthRules', 'CachedLine', 'LineLayoutCache')

# Keep summaries bounded; the full length is kept alongside the truncated text.
_SUMMARY_LIMIT = 32


def _make_summary(text: str) -> str:
    """Return a bounded digest of the original text."""
    if len(text) <= _SUMMARY_LIMIT:
        return text
    return f'{text[:_SUMMARY_LIMIT - 3]}...(+{len(text) - _SUMMARY_LIMIT + 3} chars)'


class WidthRule(NamedTuple):
    """
    One codepoint-width classification table, as a tuple of ``(lo, hi)`` ranges.

    :param zero: Codepoints measured as zero cells (combining marks, joiners, ...).
    :param wide: Codepoints measured as two cells (East Asian Wide/FullWidth, ...).
    :param ambiguous: East Asian Ambiguous codepoints, two cells only when
        ``ambiguous_width=2``.
    """

    zero: tuple[tuple[int, int], ...]
    wide: tuple[tuple[int, int], ...]
    ambiguous: tuple[tuple[int, int], ...]


class WidthRules:
    """
    An immutable set of Unicode width rules against which a line is laid out.

    Two rule sets are compared with :meth:`diff`; the returned codepoints are exactly those
    whose effective cell width changed, so clusters not containing any of them keep their
    measured width.
    """

    def __init__(self, rule: WidthRule):
        self._rule = rule

    @classmethod
    def from_current(cls) -> 'WidthRules':
        """Build rule set from the width tables shipped with this library version."""
        # local
        from ._constants import (_ZERO_WIDTH_TABLE,
                                 _WIDE_EASTASIAN_TABLE,
                                 _AMBIGUOUS_TABLE)
        return cls(WidthRule(
            zero=tuple(_ZERO_WIDTH_TABLE),
            wide=tuple(_WIDE_EASTASIAN_TABLE),
            ambiguous=tuple(_AMBIGUOUS_TABLE),
        ))

    @property
    def rule(self) -> WidthRule:
        """The underlying classification table."""
        return self._rule

    def codepoint_width(self, ucs: int, ambiguous_width: int = 1) -> int:
        """
        Return the cell width of one codepoint under these rules.

        The lookup order mirrors :func:`wcwidth.wcwidth`: zero-width first, then wide, then
        the ambiguous table (only in CJK context).
        """
        if self._rule.zero and bisearch(ucs, self._rule.zero):
            return 0
        if self._rule.wide and bisearch(ucs, self._rule.wide):
            return 2
        if ambiguous_width == 2 and self._rule.ambiguous and bisearch(
                ucs, self._rule.ambiguous):
            return 2
        return 1

    def diff(self, other: 'WidthRules', ambiguous_width: int = 1) -> frozenset[int]:
        """
        Return the codepoints whose effective width differs from *other*.

        :param other: The rule set to compare against, e.g. the upgraded rules.
        :param ambiguous_width: 1 or 2, selects whether the ambiguous table contributes.
        :returns: Immutable set of codepoint ordinals with a changed cell width.

        The tables are partitioned at every range boundary, so each resulting interval has a
        constant classification under both rule sets; only differing intervals are expanded.
        """
        ambiguous_width = _clamp_ambiguous_width(ambiguous_width)
        tables = (self._rule.zero, self._rule.wide,
                  other._rule.zero, other._rule.wide)
        if ambiguous_width == 2:
            tables += (self._rule.ambiguous, other._rule.ambiguous)

        breakpoints = {0}
        for table in tables:
            for lo, hi in table:
                breakpoints.add(lo)
                breakpoints.add(hi + 1)

        changed: set[int] = set()
        bounds = sorted(breakpoints)
        for lo, hi_exclusive in zip(bounds, bounds[1:]):
            sample = lo
            if (self.codepoint_width(sample, ambiguous_width)
                    != other.codepoint_width(sample, ambiguous_width)):
                changed.update(range(lo, hi_exclusive))
        return frozenset(changed)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WidthRules) and other._rule == self._rule

    def __hash__(self) -> int:
        return hash(self._rule)

    def __repr__(self) -> str:
        return (f'WidthRules(zero={len(self._rule.zero)} ranges, '
                f'wide={len(self._rule.wide)} ranges, '
                f'ambiguous={len(self._rule.ambiguous)} ranges)')


class CachedLine(NamedTuple):
    """
    The historical cache entry for one laid-out line.

    Only the minimum needed to reconstruct layout is retained; fixed positioning points are
    re-derived from the original text at rebuild time rather than cached.

    :param clusters: ``(start, end)`` string index span of each grapheme cluster.
    :param offsets: Display column each cluster starts at, parallel to *clusters*.
    :param summary: Bounded digest of the original text.
    """

    clusters: tuple[tuple[int, int], ...]
    offsets: tuple[int, ...]
    summary: str


@dataclass(frozen=True)
class _ScanResult:
    """Output of one forward layout scan over a line."""

    clusters: tuple[tuple[int, int], ...]
    offsets: tuple[int, ...]
    anchors: dict[int, int]
    reverse_index: tuple[int, ...]


class LineLayoutCache:
    """
    Build historical line caches and re-lay them incrementally across rule upgrades.

    :param tabsize: Tab stop width; tabs advance to the next multiple of this value.

    Lines are plain text without terminal escape sequences.  A carriage return (``\\r``) is
    recorded as an anchor at absolute column 0; everything else is segmented into UAX #29
    grapheme clusters.

    Example::

        >>> cache = LineLayoutCache()
        >>> rules = WidthRules.from_current()
        >>> line = cache.build('a中b', rules)
        >>> line.clusters, line.offsets
        (((0, 1), (1, 2), (2, 3)), (0, 1, 3))
        >>> index = cache.column_index('a中b', rules)
        >>> index
        (0, 1, 3)
    """

    def __init__(self, tabsize: int = 8):
        if tabsize <= 0:
            raise ValueError(f'tabsize must be positive, got {tabsize}')
        self._tabsize = tabsize

    def _cluster_width(self, cluster: str, rules: WidthRules,
                       ambiguous_width: int) -> int:
        """
        Return the cell width of one grapheme cluster under *rules*.

        Marks and joiners measure zero, so the cluster occupies the widest cell its
        codepoints claim.
        """
        width = 0
        for char in cluster:
            width = max(width, rules.codepoint_width(ord(char), ambiguous_width))
        return width

    def _segment_clusters(self, text: str, rules: WidthRules,
                          ambiguous_width: int) -> list[tuple[int, int]]:
        """
        Segment *text* into cluster spans under the width rules.

        UAX #29 boundaries are the starting point.  A zero-width control codepoint under
        *rules* extends the cluster it follows, so a rule upgrade that turns such a codepoint
        zero-width can change cluster boundaries without changing the line's total width.
        Carriage returns and tabs always stay their own cluster because they move the cursor.
        """
        spans: list[tuple[int, int]] = []
        for cluster in iter_graphemes(text):
            start = spans[-1][1] if spans else 0
            end = start + len(cluster)
            ucs = ord(cluster[0])
            if cluster == '\r' or cluster == '\t':
                # Carriage returns and tabs carry explicit cursor movement, so they always
                # remain standalone positioning points regardless of width classification.
                spans.append((start, end))
                continue
            if spans and rules.codepoint_width(ucs, ambiguous_width) == 0:
                # Rule-derived boundary: a codepoint UAX #29 keeps standalone (a control) but
                # the width rules measure zero joins the previous cluster.  Combining marks and
                # joiners already arrive merged from iter_graphemes, so this only fires for the
                # control case, which is what makes a boundary move without changing total width.
                prev_start, _prev_end = spans[-1]
                spans[-1] = (prev_start, end)
            else:
                spans.append((start, end))
        return spans

    def _advance_col(self, col: int, cluster: str, rules: WidthRules,
                     ambiguous_width: int) -> int:
        """Return the cursor column after laying *cluster* at column *col*."""
        if cluster == '\r':
            return 0
        if cluster == '\t':
            return col + self._tabsize - (col % self._tabsize)
        return col + self._cluster_width(cluster, rules, ambiguous_width)

    def _scan(self, text: str, rules: WidthRules, ambiguous_width: int) -> _ScanResult:
        """Forward layout scan: the single source of truth for cluster spans and columns."""
        clusters: list[tuple[int, int]] = []
        offsets: list[int] = []
        anchors: dict[int, int] = {}
        reverse_index = [0] * len(text)

        col = 0
        for start, end in self._segment_clusters(text, rules, ambiguous_width):
            cluster = text[start:end]
            clusters.append((start, end))
            offsets.append(col)
            for string_idx in range(start, end):
                reverse_index[string_idx] = col

            if cluster == '\r':
                # Fixed positioning point: the cursor is absolutely re-placed at column 0.
                anchors[start] = 0
            col = self._advance_col(col, cluster, rules, ambiguous_width)

        return _ScanResult(
            clusters=tuple(clusters),
            offsets=tuple(offsets),
            anchors=anchors,
            reverse_index=tuple(reverse_index),
        )

    def build(self, text: str, rules: WidthRules,
              ambiguous_width: int = 1) -> CachedLine:
        """
        Lay out *text* from scratch and return its historical cache entry.

        :param text: The plain-text line to cache.
        :param rules: Width rules in effect when the line was first laid out.
        :param ambiguous_width: Width for East Asian Ambiguous codepoints (1 or 2).
        """
        ambiguous_width = _clamp_ambiguous_width(ambiguous_width)
        scan = self._scan(text, rules, ambiguous_width)
        return CachedLine(
            clusters=scan.clusters,
            offsets=scan.offsets,
            summary=_make_summary(text),
        )

    def column_index(self, text: str, rules: WidthRules,
                     ambiguous_width: int = 1) -> tuple[int, ...]:
        """
        Full from-scratch recomputation of the string-index to display-column reverse lookup.

        Every string index maps to the column its cluster starts at; zero-width marks share
        their base column.  This is the reference the incremental rebuild must match.
        """
        ambiguous_width = _clamp_ambiguous_width(ambiguous_width)
        return self._scan(text, rules, ambiguous_width).reverse_index

    def rebuild(self, cached: CachedLine, text: str, old_rules: WidthRules,
                new_rules: WidthRules, ambiguous_width: int = 1) -> CachedLine:
        """
        Re-lay a cached line incrementally after a width-rule upgrade.

        Only clusters affected by the actual rule difference are re-laid: clusters containing
        a changed codepoint, and clusters whose segmentation boundary moved.  Columns drift
        from the first affected cluster onward until an anchor snaps the cursor back to its
        absolute column.  The reverse lookup is always rebuilt, so a boundary-only change
        with identical total width is still reflected.

        :param cached: The historical entry produced under *old_rules*.
        :param text: The original line text; cache entries only keep a summary of it.
        :param old_rules: Rules the cache was built with.
        :param new_rules: Upgraded rules to lay the line out under.
        :param ambiguous_width: Width for East Asian Ambiguous codepoints (1 or 2).
        """
        ambiguous_width = _clamp_ambiguous_width(ambiguous_width)
        if _make_summary(text) != cached.summary:
            raise ValueError('cached summary does not match the given text')

        scan = self._scan(text, new_rules, ambiguous_width)
        new_clusters = scan.clusters
        # Anchor positions (carriage returns, ...) are structural and do not depend on the
        # width tables, so deriving them under the new rules yields the same string indices.
        anchors = scan.anchors

        changed_codepoints = old_rules.diff(new_rules, ambiguous_width)
        old_cluster_set = set(cached.clusters)
        cached_offset_by_span = {
            span: offset for span, offset in zip(cached.clusters, cached.offsets)
        }

        def is_affected(span: tuple[int, int]) -> bool:
            start, end = span
            if span not in old_cluster_set:
                return True
            return any(ord(text[i]) in changed_codepoints
                       for i in range(start, end))

        affected_flags = [is_affected(span) for span in new_clusters]

        # Anchors split the line into independently laid-out segments.  The anchor cluster
        # closes the segment it terminates, because drift before it changes its recorded
        # offset; the next segment starts right after the anchor at an absolute column, so a
        # clean segment there keeps its cached offsets even when earlier segments were dirty.
        # The forward walk needs no anchor lookup: _advance_col applies the same absolute
        # positioning (a carriage return returns column 0).
        segments: list[tuple[int, int, bool]] = []
        segment_start = 0
        segment_dirty = False
        for cluster_idx, span in enumerate(new_clusters):
            if affected_flags[cluster_idx]:
                segment_dirty = True
            if span[0] in anchors:
                segments.append((segment_start, cluster_idx + 1, segment_dirty))
                segment_start = cluster_idx + 1
                segment_dirty = False
        segments.append((segment_start, len(new_clusters), segment_dirty))

        new_offsets: list[int] = []
        col = 0
        for segment_start, segment_end, segment_dirty in segments:
            if segment_start >= segment_end:
                continue
            if not segment_dirty:
                for seg_idx in range(segment_start, segment_end):
                    span = new_clusters[seg_idx]
                    new_offsets.append(cached_offset_by_span[span])
                    col = self._advance_col(
                        col, text[span[0]:span[1]], new_rules, ambiguous_width)
            else:
                for seg_idx in range(segment_start, segment_end):
                    start, end = new_clusters[seg_idx]
                    new_offsets.append(col)
                    col = self._advance_col(
                        col, text[start:end], new_rules, ambiguous_width)

        return CachedLine(
            clusters=new_clusters,
            offsets=tuple(new_offsets),
            summary=cached.summary,
        )

    def reverse_index(self, cached: CachedLine, text: str) -> tuple[int, ...]:
        """
        Build the string-index to display-column reverse lookup from a cache entry.

        Marks and joiners inside a cluster share the cluster's start column.
        """
        reverse_index = [0] * len(text)
        for (start, end), col in zip(cached.clusters, cached.offsets):
            for string_idx in range(start, end):
                reverse_index[string_idx] = col
        return tuple(reverse_index)

    def verify_line(self, cached: CachedLine, text: str, rules: WidthRules,
                    ambiguous_width: int = 1) -> bool:
        """
        Return True when *cached* matches a from-scratch recomputation, index by index.

        Both the cluster start columns and the full string-index reverse lookup are compared
        column by column, which is the acceptance check for an incremental rebuild.
        """
        ambiguous_width = _clamp_ambiguous_width(ambiguous_width)
        scan = self._scan(text, rules, ambiguous_width)
        if cached.clusters != scan.clusters:
            return False
        if cached.offsets != scan.offsets:
            return False
        return self.reverse_index(cached, text) == scan.reverse_index
