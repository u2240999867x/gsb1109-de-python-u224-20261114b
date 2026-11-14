"""Tests for post-concatenation field boundary audits."""

# 3rd party
import pytest

# local
from wcwidth import audit_field_concatenation
from wcwidth.field_audit import FieldAuditResult, FieldMerge

ZWJ = '‍'
RI_U = '\U0001F1FA'  # regional indicator symbol letter U
RI_S = '\U0001F1F8'  # regional indicator symbol letter S
FLAG_US = RI_U + RI_S
MAN = '\U0001F468'
WOMAN = '\U0001F469'


def test_audit_returns_result_type():
    """The audit entry point returns a FieldAuditResult."""
    result = audit_field_concatenation(['hello', 'world'])
    assert isinstance(result, FieldAuditResult)
    assert not result.has_merges
    assert result.merges == ()
    assert result.expected_width == result.actual_width == 10
    assert result.width_delta == 0


def test_field_ranges_recover_original_fragments():
    """Field indices still resolve to the original field text via the ranges."""
    fields = ['ab', RI_U, 'cd']
    result = audit_field_concatenation(fields)
    assert result.field_ranges == ((0, 2), (2, 3), (3, 5))
    for field_index, field in enumerate(fields):
        start, end = result.field_ranges[field_index]
        assert result.joined[start:end] == field
    assert result.fragment_for(0) is None


def test_split_flag_is_reported_with_real_width():
    """A flag made from codepoints of two fields is one merge, not assigned to the left field."""
    left = 'A' + RI_U
    right = RI_S + 'B'
    result = audit_field_concatenation([left, right])

    assert result.has_merges
    assert len(result.merges) == 1
    merge = result.merges[0]
    assert isinstance(merge, FieldMerge)

    # the merged cluster is the whole flag, spanning both fields
    assert merge.text == FLAG_US
    assert (merge.start, merge.end) == (1, 3)
    assert merge.field_indices == (0, 1)
    assert merge.width == 2

    # each side keeps its own piece; the flag is never credited wholesale to the left column
    assert merge.fragments[0] == RI_U
    assert merge.fragments[1] == RI_S
    assert merge.fragments[0] != FLAG_US
    assert result.fragment_for(0) == RI_U
    assert result.fragment_for(1) == RI_S

    # standalone widths (3 + 3) no longer match the real joined column width (4)
    assert result.expected_width == 6
    assert result.actual_width == 4
    assert result.width_delta == -2


def test_flag_inside_one_field_is_not_a_merge():
    """A flag fully contained in a field does not cross a boundary."""
    result = audit_field_concatenation(['X' + FLAG_US, 'Y'])
    assert not result.has_merges
    assert result.expected_width == result.actual_width == 4


def test_zwj_join_across_boundary():
    """A ZWJ at the left seam glues the right field's pictograph into one cluster."""
    left = MAN + ZWJ
    right = WOMAN
    result = audit_field_concatenation([left, right])

    assert len(result.merges) == 1
    merge = result.merges[0]
    assert merge.text == MAN + ZWJ + WOMAN
    assert merge.field_indices == (0, 1)
    assert merge.fragments[0] == MAN + ZWJ
    assert merge.fragments[1] == WOMAN
    assert merge.width == 2
    assert result.expected_width == 4
    assert result.actual_width == 2
    assert result.width_delta == -2


def test_zwj_without_pictograph_keeps_clusters_but_shifts_width():
    """ZWJ before an ordinary character keeps the UAX #29 boundary but still skews column width."""
    result = audit_field_concatenation(['a' + ZWJ, 'b'])

    # 'b' stays its own grapheme cluster, so no cluster spans the field boundary
    assert not result.has_merges

    # the width engine consumes the cell after ZWJ, so the audit reports the real
    # joined width (1) rather than the summed per-field widths (2)
    assert result.expected_width == 2
    assert result.actual_width == 1
    assert result.width_delta == -1


def test_middle_field_can_join_on_both_sides():
    """The middle field contributes fragments to merges on both seams, both recoverable."""
    fields = [RI_U, RI_S + RI_U, RI_S]
    result = audit_field_concatenation(fields)

    assert len(result.merges) == 2
    first_merge, second_merge = result.merges
    assert first_merge.field_indices == (0, 1)
    assert second_merge.field_indices == (1, 2)

    # the middle field's full original text is reassembled from both merges, in order
    assert result.fragment_for(1) == fields[1]
    assert result.fragment_for(0) == RI_U
    assert result.fragment_for(2) == RI_S


def test_empty_fields():
    """Empty fields produce empty ranges and no merges."""
    result = audit_field_concatenation(['', 'abc', ''])
    assert not result.has_merges
    assert result.field_ranges == ((0, 0), (0, 3), (3, 3))
    assert result.expected_width == result.actual_width == 3


def test_no_fields():
    """Auditing zero fields yields an empty result."""
    result = audit_field_concatenation([])
    assert result.joined == ''
    assert result.merges == ()
    assert result.field_ranges == ()
    assert result.expected_width == result.actual_width == 0


def test_control_character_raises():
    """Non-printable control characters make width undefined, so the audit raises."""
    with pytest.raises(ValueError, match='control character'):
        audit_field_concatenation(['a\x01', 'b'])


def test_ambiguous_width_forwarded():
    """The ambiguous_width option reaches the real column width measurement."""
    narrow = audit_field_concatenation(['α', 'α'])
    wide = audit_field_concatenation(['α', 'α'], ambiguous_width=2)
    assert narrow.actual_width == 2
    assert wide.actual_width == 4
    assert wide.expected_width == 4
