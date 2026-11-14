"""
Post-concatenation audit of independently validated fields.

Each field may measure and segment cleanly on its own, but joining fields can change the grapheme
cluster boundaries at the seam: a trailing regional indicator can pair across the seam into an emoji
flag (UAX #29 GB12/GB13), or a trailing ZWJ can glue the next field's leading pictograph into one
cluster (GB9/GB11).  The per-field column widths then no longer add up to the width of the joined
line.

This module re-segments the concatenated string with :func:`wcwidth.iter_graphemes` and compares the
result against the field boundaries supplied by the caller.  Every joined cluster that spans more
than one field is reported with its real (re-measured) column width, while the original field
fragments stay reachable through their field index.
"""

from __future__ import annotations

# std imports
from typing import NamedTuple, Optional, Sequence

# local
from ._wcswidth import wcswidth
from .grapheme import iter_graphemes

__all__ = ('FieldMerge', 'FieldAuditResult', 'audit_field_concatenation')


class FieldMerge(NamedTuple):
    """
    A grapheme cluster that re-clustered across a field boundary.

    :param start: Start index of the merged cluster in the concatenated string.
    :param end: End index (exclusive) of the merged cluster.
    :param text: The merged cluster text as segmented after concatenation.
    :param field_indices: Indices, in field order, of every field contributing codepoints.
    :param fragments: Substring each contributing field lost into the cluster, keyed by field index.
    :param width: Real terminal column width of the merged cluster.
    """

    start: int
    end: int
    text: str
    field_indices: tuple[int, ...]
    fragments: dict[int, str]
    width: int


class FieldAuditResult(NamedTuple):
    """
    Outcome of auditing concatenated fields against their original boundaries.

    :param joined: The concatenated string that was segmented and measured.
    :param merges: Clusters that span two or more fields, in string order.
    :param expected_width: Sum of the independently measured per-field column widths.
    :param actual_width: Column width of the joined string, re-measured as a whole.
    :param field_ranges: ``(start, end)`` codepoint span of each field inside ``joined``.
    """

    joined: str
    merges: tuple[FieldMerge, ...]
    expected_width: int
    actual_width: int
    field_ranges: tuple[tuple[int, int], ...]

    @property
    def has_merges(self) -> bool:
        """Return whether any cluster merged codepoints from different fields."""
        return bool(self.merges)

    @property
    def width_delta(self) -> int:
        """Return ``actual_width - expected_width``; zero means the columns still line up."""
        return self.actual_width - self.expected_width

    def fragment_for(self, field_index: int) -> Optional[str]:
        """
        Return the original fragments a field lost into merged clusters, joined in string order.

        A field can contribute to more than one merge (for example the middle field of three
        regional-indicator fields).  Fields whose codepoints never crossed a boundary return
        ``None``.

        :param field_index: Index of the field as passed to the audit.
        """
        fragments = [
            merge.fragments[field_index]
            for merge in self.merges
            if field_index in merge.fragments
        ]
        return ''.join(fragments) if fragments else None


def audit_field_concatenation(
    fields: Sequence[str],
    *,
    ambiguous_width: int = 1,
) -> FieldAuditResult:
    r"""
    Re-check field boundaries after the fields are concatenated into one line.

    Each field is assumed to have already passed validation on its own.  This function joins them,
    re-segments the result into grapheme clusters, and reports every cluster that absorbed
    codepoints from more than one field.  Typical triggers are emoji flags split across two fields
    (regional indicator pairs) and ZWJ sequences cut at the seam.

    :param fields: Field strings in left-to-right order.
    :param ambiguous_width: Width for East Asian Ambiguous characters, forwarded to
        :func:`wcwidth.wcswidth` when measuring real column widths.
    :returns: A :class:`FieldAuditResult` with the merged clusters, their real widths, and the
        codepoint ranges of the original fields so field indices still resolve to their fragments.
    :raises ValueError: If re-measuring any field or the joined string hits a C0/C1 control
        character (:func:`wcwidth.wcswidth` returns ``-1``).

    Example::

        >>> result = audit_field_concatenation(['A\U0001F1FA', '\U0001F1F8B'])
        >>> result.has_merges
        True
        >>> merge = result.merges[0]
        >>> merge.text == '\U0001F1FA\U0001F1F8'
        True
        >>> merge.field_indices
        (0, 1)
        >>> result.expected_width, result.actual_width
        (6, 4)

    .. versionadded:: 0.3.6
    """
    field_ranges: list[tuple[int, int]] = []
    expected_width = 0
    offset = 0
    for field in fields:
        width = wcswidth(field, ambiguous_width=ambiguous_width)
        if width < 0:
            raise ValueError(f'Field at index {len(field_ranges)} contains a control character')
        expected_width += width
        field_ranges.append((offset, offset + len(field)))
        offset += len(field)

    joined = ''.join(fields)
    actual_width = wcswidth(joined, ambiguous_width=ambiguous_width)
    if actual_width < 0:
        raise ValueError('Concatenated fields contain a control character')

    merges: list[FieldMerge] = []
    cluster_start = 0
    for cluster in iter_graphemes(joined):
        cluster_end = cluster_start + len(cluster)
        contributing = [
            field_index
            for field_index, (field_start, field_end) in enumerate(field_ranges)
            if cluster_start < field_end and cluster_end > field_start
        ]
        if len(contributing) > 1:
            fragments: dict[int, str] = {}
            for field_index in contributing:
                field_start, field_end = field_ranges[field_index]
                overlap_start = max(cluster_start, field_start)
                overlap_end = min(cluster_end, field_end)
                if overlap_end > overlap_start:
                    fragments[field_index] = joined[overlap_start:overlap_end]
            merges.append(FieldMerge(
                start=cluster_start,
                end=cluster_end,
                text=cluster,
                field_indices=tuple(contributing),
                fragments=fragments,
                width=wcswidth(cluster, ambiguous_width=ambiguous_width),
            ))
        cluster_start = cluster_end

    return FieldAuditResult(
        joined=joined,
        merges=tuple(merges),
        expected_width=expected_width,
        actual_width=actual_width,
        field_ranges=tuple(field_ranges),
    )
