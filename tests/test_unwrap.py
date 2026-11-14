"""Tests for reconstructing logical records from physical screen transcripts."""

# std imports
import pytest

# local
import wcwidth
from wcwidth import (LogicalRecord,
                     UnwrapResult,
                     UnwrapSeam,
                     WrapMarker,
                     unwrap)


def test_unwrap_is_exported():
    """unwrap() and its metadata types are part of the public API."""
    assert wcwidth.unwrap is unwrap
    assert isinstance(wcwidth.WrapMarker.SOFT, WrapMarker)
    assert UnwrapSeam(0, WrapMarker.SOFT).index == 0
    assert UnwrapResult((LogicalRecord('x', 0, 0),), (), ('x',), (None,), (), ()).texts() == ['x']
    assert 'unwrap' in wcwidth.__all__
    assert 'WrapMarker' in wcwidth.__all__
    assert 'UnwrapSeam' in wcwidth.__all__
    assert 'LogicalRecord' in wcwidth.__all__
    assert 'UnwrapResult' in wcwidth.__all__


@pytest.mark.parametrize('value,expected', [
    (WrapMarker.SOFT, WrapMarker.SOFT),
    ('soft', WrapMarker.SOFT),
    ('SOFT', WrapMarker.SOFT),
    (' wrap ', WrapMarker.SOFT),
    ('wrapped', WrapMarker.SOFT),
    ('continuation', WrapMarker.SOFT),
    ('continued', WrapMarker.SOFT),
    ('s', WrapMarker.SOFT),
    (True, WrapMarker.SOFT),
    (1, WrapMarker.SOFT),
    (WrapMarker.HARD, WrapMarker.HARD),
    ('hard', WrapMarker.HARD),
    ('newline', WrapMarker.HARD),
    ('break', WrapMarker.HARD),
    ('ended', WrapMarker.HARD),
    ('h', WrapMarker.HARD),
    (False, WrapMarker.HARD),
    (0, WrapMarker.HARD),
    (WrapMarker.UNKNOWN, WrapMarker.UNKNOWN),
    (None, WrapMarker.UNKNOWN),
    ('unknown', WrapMarker.UNKNOWN),
    ('u', WrapMarker.UNKNOWN),
    ('?', WrapMarker.UNKNOWN),
    ('missing', WrapMarker.UNKNOWN),
    ('', WrapMarker.UNKNOWN),
])
def test_wrap_marker_parse(value, expected):
    """Side-table spellings are coerced to the canonical marker."""
    assert WrapMarker.parse(value) is expected


@pytest.mark.parametrize('value', ['maybe', 'yes', 2, -1, 3.5, object()])
def test_wrap_marker_parse_invalid(value):
    """Unrecognized markers are rejected instead of guessed."""
    with pytest.raises(ValueError):
        WrapMarker.parse(value)


def test_wrap_marker_ambiguous():
    """Only an unknown marker is ambiguous."""
    assert WrapMarker.UNKNOWN.ambiguous
    assert not WrapMarker.SOFT.ambiguous
    assert not WrapMarker.HARD.ambiguous


def test_unwrap_soft_marker_joins_lines():
    """An explicit soft marker joins physical lines into one logical record."""
    result = unwrap(['alpha ', 'beta'], width=10, markers=[WrapMarker.SOFT])
    assert result.texts() == ['alpha beta']
    assert len(result) == 1
    assert result.complete


def test_unwrap_hard_marker_keeps_records_apart():
    """An explicit hard marker ends the logical record."""
    result = unwrap(['alpha', 'beta'], width=10, markers=[WrapMarker.HARD])
    assert result.texts() == ['alpha', 'beta']
    assert len(result) == 2


def test_unwrap_full_line_does_not_imply_soft_wrap():
    """A line exactly filling the capture width is still hard when so marked."""
    result = unwrap(['abcdefgh', 'ij'], width=8, markers=[WrapMarker.HARD])
    assert result.texts() == ['abcdefgh', 'ij']


def test_unwrap_full_line_without_marker_is_ambiguous():
    """A full line with no side-table marker is not assumed to be soft-wrapped."""
    result = unwrap(['abcdefgh', 'ij'], width=8)
    assert result.texts() == ['abcdefgh', 'ij']
    assert [seam.index for seam in result.ambiguous_seams] == [0]


def test_unwrap_short_line_does_not_imply_hard_break():
    """A short line is still soft-wrapped when the side table says so."""
    result = unwrap(['abc', 'def'], width=20, markers=[WrapMarker.SOFT])
    assert result.texts() == ['abcdef']


def test_unwrap_unused_cells_are_not_width_proof():
    """Space left because the next cluster did not fit is layout, not a break."""
    result = unwrap(['abc   ', 'def'], width=12, markers=[WrapMarker.SOFT])
    assert result.texts() == ['abc   def']
    assert result.records[0].padding
    assert result.seams[0].trailing == '   '
    assert result.seams[0].padding


def test_unwrap_preserves_exact_text_including_padding():
    """Exact record text retains layout trailing spaces."""
    result = unwrap(['abc   ', 'def'], width=12, markers=[WrapMarker.SOFT])
    assert result.texts() == ['abc   def']
    assert result.records[0].segments == ('abc   ', 'def')
    assert result.records[0].padding
    assert result.records[0].seams[0].trailing == '   '


def test_unwrap_trailing_spaces_at_hard_break_are_kept():
    """Trailing spaces before a hard break are part of the record text."""
    result = unwrap(['value   ', 'next'], width=12, markers=[WrapMarker.HARD])
    assert result.texts() == ['value   ', 'next']
    assert result.records[0].text == 'value   '
    assert result.records[0].trailing == '   '


def test_unwrap_trailing_spaces_at_end_of_transcript_are_kept():
    """Trailing spaces on the final physical line are content."""
    result = unwrap(['value   '], width=12)
    assert result.texts() == ['value   ']
    assert result.records[0].text == 'value   '


def test_unwrap_missing_marker_yields_ambiguous_seam():
    """A missing marker leaves the seam unresolved and adjacent records marked."""
    result = unwrap(['abc', 'def'], width=10)
    assert result.texts() == ['abc', 'def']
    assert len(result.ambiguous_seams) == 1
    seam = result.ambiguous_seams[0]
    assert seam.index == 0
    assert seam.marker is WrapMarker.UNKNOWN
    assert seam.ambiguous
    assert not result.complete
    assert all(record.ambiguous for record in result.records)


def test_unwrap_partial_marker_table_fills_unknown():
    """A short side table leaves later seams ambiguous."""
    result = unwrap(['a', 'b', 'c'], width=10, markers=[WrapMarker.SOFT])
    assert result.texts() == ['ab', 'c']
    assert [seam.index for seam in result.ambiguous_seams] == [1]


def test_unwrap_mapping_markers():
    """Sparse markers may be supplied as a mapping of seam index."""
    result = unwrap(['a', 'b', 'c'], width=10, markers={0: 'soft', 1: 'hard'})
    assert result.texts() == ['ab', 'c']
    assert result.complete


def test_unwrap_marker_per_line_table():
    """A table with one entry per physical line ignores its final entry."""
    result = unwrap(['a', 'b', 'c'], width=10,
                    markers=[WrapMarker.SOFT, WrapMarker.HARD, WrapMarker.HARD])
    assert result.texts() == ['ab', 'c']
    assert result.complete


def test_unwrap_marker_table_too_long():
    """A table cannot describe more boundaries than the transcript has."""
    with pytest.raises(ValueError):
        unwrap(['a', 'b'], width=10, markers=['soft', 'hard', 'hard'])


def test_unwrap_string_input_splits_newlines():
    """String input is split at newlines into physical lines."""
    result = unwrap('alpha\nbeta\ngamma', width=10,
                    markers=[WrapMarker.SOFT, WrapMarker.HARD])
    assert result.texts() == ['alphabeta', 'gamma']
    assert result.lines == ('alpha', 'beta', 'gamma')


def test_unwrap_string_input_trailing_newline():
    """A trailing newline does not invent an empty physical line."""
    result = unwrap('alpha\nbeta\n', width=10,
                    markers=[WrapMarker.SOFT, WrapMarker.HARD])
    assert result.lines == ('alpha', 'beta')
    assert result.texts() == ['alphabeta']


def test_unwrap_string_input_crlf():
    """CR LF is one physical-line separator, not two."""
    result = unwrap('alpha\r\nbeta', width=10, markers=[WrapMarker.SOFT])
    assert result.lines == ('alpha', 'beta')
    assert result.texts() == ['alphabeta']


def test_unwrap_string_input_other_separators_are_content():
    """Vertical tab and Unicode separators are not physical-line separators here."""
    result = unwrap('a\x0bb c', width=10)
    assert result.lines == ('a\x0bb c',)


@pytest.mark.parametrize('line', ['alpha\n', 'alpha\r', 'alpha\r\n'])
def test_unwrap_sequence_hard_terminator_proves_hard_seam(line):
    """An explicit newline on a sequence entry proves a hard seam without a marker."""
    result = unwrap([line, 'beta'], width=10)
    assert result.texts() == ['alpha', 'beta']
    assert result.seams[0].marker is WrapMarker.HARD
    assert result.complete


def test_unwrap_seam_proven_hard_terminator():
    """A seam backed by an explicit newline is marked proven."""
    result = unwrap(['alpha\n', 'beta'], width=10)
    assert result.seams[0].proven
    assert not unwrap(['alpha', 'beta'], width=10, markers=[WrapMarker.HARD]).seams[0].proven


def test_unwrap_resolve_cannot_soften_proven_hard_terminator():
    """An explicit newline cannot be overridden by a late soft marker."""
    result = unwrap(['alpha\n', 'beta'], width=10)
    with pytest.raises(ValueError):
        result.resolve(0, WrapMarker.SOFT)


def test_unwrap_sequence_hard_terminator_conflicts_with_soft_marker():
    """A hard newline and a soft side-table marker are contradictory."""
    with pytest.raises(ValueError):
        unwrap(['alpha\n', 'beta'], width=10, markers=[WrapMarker.SOFT])


def test_unwrap_sequence_hard_terminator_with_unknown_marker_upgrades():
    """An explicit newline resolves a missing marker to hard."""
    result = unwrap(['alpha\n', 'beta'], width=10)
    assert result.seams[0].marker is WrapMarker.HARD


def test_unwrap_empty_input():
    """Empty transcripts yield no records and no seams."""
    for empty in ('', [], ()):
        result = unwrap(empty, width=10)
        assert result.texts() == []
        assert result.seams == ()
        assert result.complete


def test_unwrap_single_line_has_no_seams():
    """A one-line transcript has no boundary to resolve."""
    result = unwrap(['hello'], width=10)
    assert result.texts() == ['hello']
    assert result.seams == ()
    assert result.complete


def test_unwrap_rejects_non_string_line():
    """Physical lines must be strings."""
    with pytest.raises(TypeError):
        unwrap(['ok', 3], width=10)


def test_unwrap_width_validation_accepts_fitting_line():
    """A line that fits the capture width is accepted."""
    assert unwrap(['abcde'], width=5).texts() == ['abcde']


def test_unwrap_width_validation_rejects_wider_line():
    """A physical line wider than its capture width is inconsistent."""
    with pytest.raises(ValueError):
        unwrap(['abcdef'], width=5)


def test_unwrap_width_validation_measures_display_cells():
    """Width validation measures cells, not codepoints."""
    assert unwrap(['中文'], width=4).texts() == ['中文']
    with pytest.raises(ValueError):
        unwrap(['中文'], width=3)


def test_unwrap_width_validation_respects_ambiguous_width():
    """Ambiguous characters use the requested width."""
    assert unwrap(['¡¡'], width=2, ambiguous_width=1).texts() == ['¡¡']
    with pytest.raises(ValueError):
        unwrap(['¡¡'], width=2, ambiguous_width=2)


def test_unwrap_width_validation_ignores_sequences():
    """Escape sequences do not contribute to the measured width."""
    assert unwrap(['\x1b[31mab\x1b[0m'], width=2).texts() == ['\x1b[31mab\x1b[0m']


@pytest.mark.parametrize('width', [0, -1, True])
def test_unwrap_invalid_scalar_width(width):
    """Capture widths must be positive integers."""
    with pytest.raises(ValueError):
        unwrap(['a'], width=width)


def test_unwrap_per_line_widths():
    """Capture width may vary per physical line."""
    assert unwrap(['abcde', 'e'], width=[5, 1]).texts() == ['abcde', 'e']
    with pytest.raises(ValueError):
        unwrap(['abcde', 'e'], width=[3, 1])


def test_unwrap_per_line_width_none_skips_validation():
    """A None per-line width skips validation for that line."""
    assert unwrap(['abcdef'], width=[None]).texts() == ['abcdef']


def test_unwrap_width_table_too_long():
    """A width table cannot describe more lines than the transcript has."""
    with pytest.raises(ValueError):
        unwrap(['a'], width=[1, 1])


def test_unwrap_validate_width_disabled():
    """Width validation may be disabled for transcripts of unknown capture width."""
    assert unwrap(['abcdef'], width=2, validate_width=False).texts() == ['abcdef']


def test_unwrap_no_width_skips_validation():
    """With no width side table, no width consistency check is performed."""
    assert unwrap(['abcdef'], width=None).texts() == ['abcdef']


def test_unwrap_result_sequence_protocol():
    """The result behaves as a read-only sequence of records."""
    result = unwrap(['a', 'b', 'c'], width=10,
                    markers=[WrapMarker.HARD, WrapMarker.SOFT])
    assert len(result) == 2
    assert result[0] == 'a'
    assert result[1] == 'bc'
    assert [str(record) for record in result] == ['a', 'bc']
    assert result.texts() == ['a', 'bc']
    assert result == ['a', 'bc']


def test_unwrap_record_line_range():
    """Records report the physical lines they were built from."""
    result = unwrap(['a', 'b', 'c', 'd'], width=10,
                    markers=[WrapMarker.SOFT, WrapMarker.HARD, WrapMarker.SOFT])
    assert [(record.start, record.end) for record in result.records] == [(0, 1), (2, 3)]


def test_unwrap_resolve_soft_joins_records():
    """Supplying a soft marker joins only the records touching that seam."""
    before = unwrap(['a', 'b', 'c', 'd'], width=10,
                    markers=[WrapMarker.HARD, WrapMarker.UNKNOWN, WrapMarker.HARD])
    joined = before.resolve(1, WrapMarker.SOFT)
    assert before.texts() == ['a', 'b', 'c', 'd']
    assert joined.texts() == ['a', 'bc', 'd']
    assert joined.records[0] == before.records[0]
    assert joined.records[-1] == before.records[-1]
    assert joined.complete


def test_unwrap_resolve_hard_splits_record():
    """Supplying a hard marker splits only the record containing that seam."""
    before = unwrap(['a', 'b', 'c', 'd'], width=10,
                    markers=[WrapMarker.HARD, WrapMarker.SOFT, WrapMarker.UNKNOWN])
    split = before.resolve(1, WrapMarker.HARD)
    assert before.texts() == ['a', 'bc', 'd']
    assert split.texts() == ['a', 'b', 'c', 'd']
    assert split.records[0] == before.records[0]


def test_unwrap_resolve_preserves_records_outside_segment():
    """Records wholly outside the affected segment are reused unchanged."""
    before = unwrap(['a', 'b', 'c', 'd'], width=10,
                    markers=[WrapMarker.HARD, WrapMarker.SOFT, WrapMarker.HARD])
    after = before.resolve(1, WrapMarker.HARD)
    assert after.records[0] is before.records[0]
    assert after.records[-1] is before.records[-1]


def test_unwrap_resolve_many():
    """Several seam markers may be supplied in one call."""
    before = unwrap(['a', 'b', 'c', 'd'], width=10)
    after = before.resolve_many({0: WrapMarker.SOFT, 1: WrapMarker.HARD, 2: WrapMarker.SOFT})
    assert after.texts() == ['ab', 'cd']
    assert after.complete


def test_unwrap_resolve_invalid_index():
    """Only real seam indices may be resolved."""
    result = unwrap(['a', 'b'], width=10)
    for index in (-1, 1):
        with pytest.raises(IndexError):
            result.resolve(index, WrapMarker.SOFT)


def test_unwrap_resolve_same_marker_returns_same_result():
    """Resolving a seam to its current marker changes nothing."""
    result = unwrap(['a', 'b'], width=10, markers=[WrapMarker.SOFT])
    assert result.resolve(0, WrapMarker.SOFT) is result


def test_unwrap_resolve_invalid_marker():
    """An invalid marker is rejected by resolve()."""
    result = unwrap(['a', 'b'], width=10)
    with pytest.raises(ValueError):
        result.resolve(0, 'sideways')


def test_unwrap_round_trips_wrap():
    """Markers derived from wrap() reconstruct the original records."""
    records = ['Diagnostic complete', 'Next record']
    wrapped = [wcwidth.wrap(record, width=12, drop_whitespace=False) for record in records]
    physical = [line for lines in wrapped for line in lines]
    markers = [WrapMarker.SOFT] * (len(wrapped[0]) - 1)
    markers += [WrapMarker.HARD]
    markers += [WrapMarker.SOFT] * (len(wrapped[1]) - 1)
    result = unwrap(physical, width=12, markers=markers)
    assert result.texts() == records
    assert result.complete


def test_unwrap_wide_characters():
    """Reconstruction preserves wide characters and join boundaries."""
    result = unwrap(['中文', '字'], width=4,
                    markers=[WrapMarker.SOFT])
    assert result.texts() == ['中文字']


def test_unwrap_grapheme_cluster_untouched():
    """Reconstruction does not split grapheme clusters or sequences."""
    cluster = 'é'
    result = unwrap([cluster, 'x'], width=2, markers=[WrapMarker.SOFT])
    assert result.texts() == [cluster + 'x']


def test_unwrap_seam_str():
    """Seams have a concise string representation."""
    result = unwrap(['a', 'b'], width=10, markers=[WrapMarker.SOFT])
    assert str(result.seams[0]) == 'seam 0: soft'
    assert str(unwrap(['a', 'b'], width=10).seams[0]) == 'seam 0: ambiguous'
