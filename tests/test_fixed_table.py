"""Tests for fixed-width table splitting (wcwidth.fixed_table)."""

# 3rd party
import pytest

# local
from wcwidth import (CharSpan,
                     FieldSplit,
                     SpanKind,
                     iter_char_spans,
                     parse_fixed_width_table,
                     reassemble_fixed_width,
                     split_fixed_width)
from wcwidth.fixed_table import _is_space_cluster, _validate_boundaries


def _span_tuples(field: FieldSplit) -> list[tuple[str, str, tuple[int, int], tuple[int, int]]]:
    return [
        (span.text, span.kind.value, (span.col_start, span.col_end),
         (span.src_start, span.src_end))
        for span in field.spans
    ]


def test_iter_char_spans_grid_and_source_ranges():
    row = 'AB中DE'
    spans = list(iter_char_spans(row))
    assert [span.text for span in spans] == ['A', 'B', '中', 'D', 'E']
    assert [(span.col_start, span.col_end) for span in spans] == [
        (0, 1), (1, 2), (2, 4), (4, 5), (5, 6)]
    assert all(row[span.src_start:span.src_end] == span.text for span in spans)
    assert [span.kind for span in spans] == [
        SpanKind.FULL, SpanKind.FULL, SpanKind.FULL, SpanKind.FULL, SpanKind.FULL]


def test_iter_char_spans_combining_accent_stays_with_base():
    row = 'café'  # 'e' + U+0301 combining acute
    spans = list(iter_char_spans(row))
    assert [span.text for span in spans] == ['c', 'a', 'f', 'é']
    final = spans[-1]
    assert final.src_start == 3 and final.src_end == 5
    assert (final.col_start, final.col_end) == (3, 4)
    assert row[final.src_start:final.src_end] == 'é'


def test_iter_char_spans_variation_selector_travels_with_base():
    row = 'A❤️B'  # U+2764 + U+FE0F, wide presentation
    spans = list(iter_char_spans(row))
    assert [span.text for span in spans] == ['A', '❤️', 'B']
    heart = spans[1]
    assert heart.text == '❤️'
    assert heart.src_end - heart.src_start == 2
    assert (heart.col_start, heart.col_end) == (1, 3)


def test_iter_char_spans_mark_after_wide_char_merges():
    row = '中\u0301'  # wide ideograph plus combining accent
    spans = list(iter_char_spans(row))
    assert len(spans) == 1
    assert spans[0].text == '中\u0301'
    assert spans[0].src_start == 0 and spans[0].src_end == 2
    assert (spans[0].col_start, spans[0].col_end) == (0, 2)


def test_iter_char_spans_leading_zero_width_marks_buffered():
    spans = list(iter_char_spans('\u0301A'))
    assert len(spans) == 1
    assert spans[0].text == '\u0301A'
    assert spans[0].src_start == 0 and spans[0].src_end == 2


def test_iter_char_spans_only_zero_width_marks():
    spans = list(iter_char_spans('\u0301\u0302'))
    assert len(spans) == 1
    assert spans[0].text == '\u0301\u0302'
    assert spans[0].col_start == 0 and spans[0].col_end == 0


def test_split_zero_width_only_row_is_owned_by_first_field():
    row = '\u0301\u0302'  # two combining marks, no visible base
    fields = split_fixed_width(row, (0, 3, 6))
    assert fields[0].text == row
    assert fields[0].spans[0].kind is SpanKind.FULL
    assert fields[1].spans == () and fields[2].spans == ()
    assert reassemble_fixed_width(fields) == row

    zwsp_row = '\u200b'  # zero-width space
    fields = split_fixed_width(zwsp_row, (0, 2, 4))
    assert fields[0].text == zwsp_row
    assert reassemble_fixed_width(fields) == zwsp_row


def test_iter_char_spans_real_spaces():
    spans = list(iter_char_spans('A B　C'))  # U+3000 ideographic space, width 2
    assert [span.kind for span in spans] == [
        SpanKind.FULL, SpanKind.SPACE, SpanKind.FULL, SpanKind.SPACE, SpanKind.FULL]
    assert (spans[1].col_start, spans[1].col_end) == (1, 2)
    assert (spans[3].col_start, spans[3].col_end) == (3, 5)


def test_is_space_cluster():
    assert _is_space_cluster(' ')
    assert _is_space_cluster('　')
    assert _is_space_cluster(' \u0301')
    assert not _is_space_cluster('A')
    assert not _is_space_cluster('中')
    assert not _is_space_cluster('\u0301')


def test_split_wide_char_straddles_boundary():
    row = 'AB中DE'  # 中 occupies columns 2-3, boundary at 3
    fields = split_fixed_width(row, (0, 3, 6))

    first, second, third = fields
    assert first.text == 'AB中'
    assert second.text == 'DE'
    assert third.text == ''

    # The ideograph is a fragment in both fields with the identical source range.
    left_frag = first.spans[-1]
    right_frag = second.spans[0]
    assert left_frag.kind is SpanKind.FRAGMENT
    assert right_frag.kind is SpanKind.FRAGMENT
    assert left_frag.text == right_frag.text == '中'
    assert (left_frag.src_start, left_frag.src_end) == (2, 3)
    assert (left_frag.src_start, left_frag.src_end) == (
        right_frag.src_start, right_frag.src_end)

    # No replacement glyphs are invented, field text is always a source slice.
    assert '?' not in first.text + second.text
    assert first.text == row[first.src_start:first.src_end]
    assert second.text == row[second.src_start:second.src_end]


def test_split_fragment_emitted_once_in_field_text():
    row = '中AB'  # 中 on columns 0-1, boundary at 1
    fields = split_fixed_width(row, (0, 1, 4))
    assert fields[0].text == '中'
    assert fields[1].text == 'AB'
    assert ''.join(field.text for field in fields) == row
    # The fragment reference in the second field must not duplicate the ideograph.
    assert fields[1].spans[0].kind is SpanKind.FRAGMENT
    assert fields[1].spans[0].text == '中'


def test_split_straddling_emoji_zwj_cluster():
    row = 'x👨‍👩y'  # ZWJ family pair, one grapheme of width 2 on columns 1-2
    fields = split_fixed_width(row, (0, 2, 5))
    emoji = '👨‍👩'
    assert fields[0].spans[-1].kind is SpanKind.FRAGMENT
    assert fields[1].spans[0].kind is SpanKind.FRAGMENT
    assert fields[0].spans[-1].text == emoji
    assert fields[1].spans[0].text == emoji
    assert fields[0].spans[-1].src_start == fields[1].spans[0].src_start
    assert reassemble_fixed_width(fields) == row


def test_split_distinct_space_and_fragment_kinds():
    row = 'AB 中'  # A=0 B=1 space=2 中=3-4
    fields = split_fixed_width(row, (0, 3, 5))
    kinds = [span.kind for span in fields[0].spans]
    assert kinds == [SpanKind.FULL, SpanKind.FULL, SpanKind.SPACE]
    assert all(span.kind is not SpanKind.FRAGMENT for span in fields[0].spans)
    assert fields[1].spans[0].kind is SpanKind.FULL

    # A wide space straddling a boundary is a fragment, not a field space.
    wide_space_row = 'AB　CD'  # U+3000 on columns 2-3, boundary at 3
    fields = split_fixed_width(wide_space_row, (0, 3, 6))
    assert fields[0].spans[-1].kind is SpanKind.FRAGMENT
    assert fields[1].spans[0].kind is SpanKind.FRAGMENT


def test_split_boundaries_inside_narrow_fields():
    fields = split_fixed_width('AB CD', (0, 3, 4))
    assert _span_tuples(fields[0]) == [
        ('A', 'full', (0, 1), (0, 1)),
        ('B', 'full', (1, 2), (1, 2)),
        (' ', 'space', (2, 3), (2, 3)),
    ]
    assert fields[1].text == 'C'
    assert fields[2].text == 'D'


def test_split_short_row_leaves_empty_fields():
    fields = split_fixed_width('AB', (0, 3, 6))
    assert [field.text for field in fields] == ['AB', '', '']
    assert fields[1].spans == ()
    assert fields[1].src_start == fields[1].src_end == 2


def test_split_empty_line():
    fields = split_fixed_width('', (0, 3, 6))
    assert [field.text for field in fields] == ['', '', '']
    assert all(field.spans == () for field in fields)
    assert reassemble_fixed_width(fields) == ''


def test_split_ambiguous_width_changes_grid():
    row = '¡中x'  # U+00A1 is East Asian Ambiguous
    narrow = split_fixed_width(row, (0, 2, 4, 6))
    wide = split_fixed_width(row, (0, 2, 4, 6), ambiguous_width=2)
    assert narrow[0].text == '¡中'
    assert wide[0].text == '¡'
    assert wide[1].text == '中'
    assert reassemble_fixed_width(wide) == row


ROUNDTRIP_ROWS = [
    '',
    'A',
    'hello world',
    '库存编号  名称  数量',
    'X中Y国Z',
    'aa b中cc d',
    'cafe\u0301',
    'A❤️B',
    'x👨‍👩y',
    '中\u0301文',
    'AB　CD',
    '¡ambiguo',
]

ROUNDTRIP_BOUNDARIES = [
    (0, 1), (0, 2, 4), (0, 3, 5, 8), (0, 1, 2, 3, 4, 5, 6, 7, 8),
]


@pytest.mark.parametrize('row', ROUNDTRIP_ROWS)
@pytest.mark.parametrize('columns', ROUNDTRIP_BOUNDARIES)
def test_reassemble_restores_row_exactly_once(row, columns):
    fields = split_fixed_width(row, columns)
    restored = reassemble_fixed_width(fields)
    assert restored == row
    # Field ownership alone must also restore the row, with no fragment doubled.
    assert ''.join(field.text for field in fields) == row

    emitted: list[tuple[int, int]] = []
    for field in fields:
        for span in field.spans:
            if field.start <= span.col_start < field.end:
                emitted.append((span.src_start, span.src_end))
    assert len(emitted) == len(set(emitted))


@pytest.mark.parametrize('row', ROUNDTRIP_ROWS)
@pytest.mark.parametrize('columns', ROUNDTRIP_BOUNDARIES)
def test_field_text_is_contiguous_source_slice(row, columns):
    fields = split_fixed_width(row, columns)
    for field in fields:
        assert field.text == row[field.src_start:field.src_end]


def test_parse_fixed_width_table_matches_single_rows():
    lines = ['AB中DE', 'FG  HI', '']
    table = parse_fixed_width_table(lines, (0, 3, 6))
    for line, table_row in zip(lines, table):
        assert table_row == split_fixed_width(line, (0, 3, 6))
        assert reassemble_fixed_width(table_row) == line
    assert [row[0].text for row in table] == ['AB中', 'FG ', '']


def test_fragment_keeps_whole_cluster_across_three_fields():
    row = '中A中'  # columns 0-1, 2, 3-4
    fields = split_fixed_width(row, (0, 1, 4, 5))
    assert fields[0].text == '中'
    assert fields[1].text == 'A中'
    assert fields[2].text == ''
    # Second ideograph starts in the middle field and fragments into the last.
    assert fields[1].spans[-1].kind is SpanKind.FRAGMENT
    assert fields[2].spans[0].kind is SpanKind.FRAGMENT
    assert fields[1].spans[-1].src_start == fields[2].spans[0].src_start
    assert reassemble_fixed_width(fields) == row


def test_validate_boundaries_rejects_bad_inputs():
    with pytest.raises(ValueError, match='first column boundary'):
        _validate_boundaries((1, 3))
    with pytest.raises(ValueError, match='strictly increasing'):
        _validate_boundaries((0, 3, 3))
    with pytest.raises(ValueError, match='strictly increasing'):
        _validate_boundaries((0, 5, 4))
    with pytest.raises(ValueError, match='at least one'):
        _validate_boundaries(())
    with pytest.raises(TypeError, match='integers'):
        _validate_boundaries((0, '3'))
    with pytest.raises(TypeError, match='integers'):
        _validate_boundaries((0, True))


def test_iter_char_spans_rejects_control_and_multiline():
    with pytest.raises(ValueError, match='single row'):
        list(iter_char_spans('ab\ncd'))
    with pytest.raises(ValueError, match='single row'):
        list(iter_char_spans('ab\rcd'))
    with pytest.raises(ValueError, match='printable text'):
        list(iter_char_spans('ab\tcd'))
    with pytest.raises(TypeError, match='str'):
        list(iter_char_spans(b'ab'))  # type: ignore[arg-type]


def test_return_types_are_named_tuples():
    fields = split_fixed_width('AB中', (0, 2, 4))
    assert all(isinstance(field, FieldSplit) for field in fields)
    assert all(isinstance(span, CharSpan) for field in fields for span in field.spans)
    assert isinstance(fields, tuple)
