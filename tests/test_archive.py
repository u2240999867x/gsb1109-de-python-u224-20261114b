"""
Tests for the line-oriented log archive (wcwidth.archive).

The archive replays the explicitly supported control effects -- carriage return, backspace,
erase-in-line, and ordinary text -- onto a cell grid, recording the source position last written
into every cell.  These tests cover the visible final lines, per-cell source positions, wide
character half-overwrite (no orphaned continuation cell), and byte-vs-line chunking equivalence.
"""
# std imports
import codecs

# 3rd party
import pytest

# local
from wcwidth.archive import LineArchive, archive_text


def _feed_bytewise(text: str, **kwargs) -> LineArchive:
    """Feed *text* through an incremental UTF-8 decoder, one decoded byte at a time."""
    archive = LineArchive(**kwargs)
    decoder = codecs.getincrementaldecoder('utf-8')()
    for byte in text.encode('utf-8'):
        piece = decoder.decode(bytes([byte]))
        if piece:
            archive.feed(piece)
    tail = decoder.decode(b'', final=True)
    if tail:
        archive.feed(tail)
    archive.finish()
    return archive


def _feed_charwise(text: str, **kwargs) -> LineArchive:
    """Feed *text* one Python character at a time."""
    archive = LineArchive(**kwargs)
    for char in text:
        archive.feed(char)
    archive.finish()
    return archive


@pytest.mark.parametrize('text,expected', [
    # A carriage return reuses the line; the shorter repaint leaves the stale tail visible.
    ('下载 99%\r完成\n', ('完成 99%', '')),
    # Erase-in-line after the carriage return clears the stale tail before repainting.
    ('下载 99%\r\x1b[K完成\n', ('完成', '')),
    # A plain line is archived as-is, followed by the fresh empty active line.
    ('hello world\n', ('hello world', '')),
    # Multiple progress reports on one line resolve to the final visible state.
    ('10%\r50%\r100%\n', ('100%', '')),
    ('abc\rXY', ('XYc',)),
    ('abc\rXY\nnext', ('XYc', 'next')),
    # CRLF, vertical tab and form feed all finalize a line.
    ('a\r\nb', ('a', 'b')),
    ('a\vb', ('a', 'b')),
    ('a\fb', ('a', 'b')),
])
def test_archive_final_visible_lines(text, expected):
    assert archive_text(text) == expected
    assert LineArchive().feed(text).finish() == expected


def test_progress_download_scenario_with_and_without_erase():
    """The motivating log-collector scenario: CR alone leaves old cells, EL clears them."""
    without_erase = LineArchive()
    without_erase.feed('下载 99%').carriage_return().feed('完成')
    assert without_erase.render() == '完成 99%'

    with_erase = LineArchive()
    with_erase.feed('下载 99%').carriage_return().erase_line(0).feed('完成')
    assert with_erase.render() == '完成'


def test_lines_includes_active_line_without_trailing_blanks():
    archive = LineArchive(10)
    archive.feed('abc\rX')
    assert archive.lines == ('Xbc',)
    archive.newline()
    assert archive.lines == ('Xbc', '')


def test_cell_sources_record_last_write_position():
    archive = LineArchive(10)
    archive.feed('abc')
    assert archive.cell_sources() == (0, 1, 2)
    archive.carriage_return().feed('XY')
    # Stream offsets: a=0, b=1, c=2, CR=3, X=4, Y=5; column 2 keeps its original source.
    assert archive.cell_sources() == (4, 5, 2)
    assert archive.cell_source(0) == 4
    assert archive.cell_source(2) == 2


def test_erased_cells_have_no_source():
    archive = LineArchive(10)
    archive.feed('abcdef').carriage_return().erase_line(0)
    assert archive.render() == ''
    assert archive.cell_sources() == ()
    assert archive.cell_source(3) is None

    archive.feed('xy')
    # 'abcdef' occupies 0-5, CR is offset 6, EL is offset 7, so 'xy' lands at 8 and 9.
    assert archive.cell_sources() == (8, 9)


@pytest.mark.parametrize('mode,setup,expected_text,expected_sources', [
    # Backspaces park the cursor at column 3; erase from the cursor to end of line.
    (0, 'abcdef\x08\x08\x08', 'abc', (0, 1, 2)),
    # Cursor at column 2: erase from line start through the cursor.
    (1, 'abcdef\x08\x08\x08\x08', '   def', (None, None, None, 3, 4, 5)),
    # Whole line erased.
    (2, 'abcdef', '', ()),
])
def test_erase_line_modes(mode, setup, expected_text, expected_sources):
    archive = LineArchive(10)
    archive.feed(setup)
    archive.erase_line(mode)
    assert archive.render() == expected_text
    assert archive.cell_sources() == expected_sources


def test_erase_chars_blank_without_moving_cursor():
    archive = LineArchive(10)
    archive.feed('abcdef').carriage_return().erase_chars(3)
    assert archive.render() == '   def'
    assert archive.cursor == 0
    archive.feed('XY')
    # XY refills two of the three erased cells; one blank cell remains before 'def'.
    assert archive.render() == 'XY def'


def test_backspace_overwrites_previous_cell():
    archive = LineArchive(10)
    archive.feed('abc').backspace().backspace().feed('XY')
    assert archive.render() == 'aXY'


def test_backspace_never_past_column_zero():
    archive = LineArchive(10)
    archive.backspace().backspace().feed('z')
    assert archive.render() == 'z'
    assert archive.cursor == 1


@pytest.mark.parametrize('text,expected', [
    # Overwriting the lead half of a wide char blanks its continuation cell as well.
    ('a中b\rXX', 'XX b'),
    # Reaching the right half by backspace likewise removes the lead half, not the neighbor.
    ('a中\x08\x08X', 'aX '),
    # Backspace onto the lead half through an explicit space also leaves no orphan.
    ('中\x08 X', '  X'),
    # Repainting with another wide character is a clean full replacement.
    ('中\r国', '国'),
])
def test_wide_character_half_overwrite_leaves_no_orphan(text, expected):
    archive = LineArchive(10)
    archive.feed(text).finish()
    assert archive.render() == expected
    # No two cells may claim overlapping columns: a split wide char leaves only paired
    # single-width cells, never an isolated continuation half.
    occupied = set()
    for col, cell in archive._cells.items():
        for span in range(col, col + cell.width):
            assert span not in occupied
            occupied.add(span)


def test_half_overwrite_blank_is_attributed_to_overwriting_write():
    archive = LineArchive(10)
    archive.feed('中').backspace().feed('X')
    # Both the painted X and the blanked continuation cell trace to the X write.
    assert archive.cell_sources() == (2, 2)


def test_wrapping_at_margin():
    archive = LineArchive(4)
    archive.feed('abcdefgh')
    assert archive.lines == ('abcd', 'efgh')


def test_wide_character_wraps_instead_of_splitting_at_margin():
    archive = LineArchive(3)
    archive.feed('ab中')
    assert archive.lines == ('ab ', '中')


def test_tab_expansion_and_zero_width_mode():
    assert archive_text('a\tb', columns=20) == ('a       b',)
    archive = LineArchive(20, tabsize=0)
    archive.feed('a\tb')
    assert archive.render() == 'ab'
    assert archive.cursor == 2


def test_combining_mark_joins_base_cell():
    archive = LineArchive(10)
    archive.feed('é').finish()
    assert archive.render() == 'é'
    assert archive.cell_sources() == (0,)


@pytest.mark.parametrize('text', [
    '下载 99%\r\x1b[K完成\n',
    'a中\x08 X\r\x1b[2Kok\n',
    'abc\b\bXY\rZZ\x1b[1K\n',
    'é\bZ',
    '👩‍💻\rX',
    'a' * 30 + '\r' + 'b' * 5 + '\nline two',
    '\x1b]8;;http://example.com\x07hi\x1b]8;;\x07\rYY\n',
    'abcdef\x1b[3Xgh\rZZ\n',
    '中\r国\x08字',
])
def test_bytewise_charwise_and_whole_input_archive_identically(text):
    whole = LineArchive(12)
    whole.feed(text).finish()
    charwise = _feed_charwise(text, columns=12)
    bytewise = _feed_bytewise(text, columns=12)

    assert charwise.lines == whole.lines
    assert bytewise.lines == whole.lines
    assert charwise.cell_sources() == whole.cell_sources()
    assert bytewise.cell_sources() == whole.cell_sources()
    assert charwise.line_sources == whole.line_sources
    assert bytewise.line_sources == whole.line_sources
    assert archive_text(text, columns=12) == whole.lines


def test_split_escape_sequence_across_chunks():
    archive = LineArchive(10)
    for piece in ('a\r\x1b', '[', 'K', 'bc'):
        archive.feed(piece)
    archive.finish()
    assert archive.render() == 'bc'


def test_split_osc_across_chunks_is_zero_width():
    archive = LineArchive(10)
    archive.feed('x\x1b]8;;http://x')
    archive.feed('\x07yz')
    archive.finish()
    assert archive.render() == 'xyz'


def test_unsupported_controls_and_sequences_are_zero_width():
    archive = LineArchive(10)
    archive.feed('a\x00\x07b\x1b[31mc\x1b[0m')
    assert archive.render() == 'abc'


def test_explicit_primitives_advance_source_positions():
    archive = LineArchive(10)
    archive.write('ab').backspace().carriage_return().erase_line(2).write('z')
    assert archive.render() == 'z'
    assert archive.cell_sources() == (5,)


def test_invalid_constructor_arguments():
    with pytest.raises(ValueError, match='columns must be positive'):
        LineArchive(0)
    with pytest.raises(ValueError, match='fillchar'):
        LineArchive(fillchar='中')
    with pytest.raises(ValueError, match='erase line mode'):
        LineArchive().erase_line(9)
    with pytest.raises(ValueError, match='erase count'):
        LineArchive().erase_chars(-1)


def test_iteration_and_repr():
    archive = LineArchive(10)
    archive.feed('one\ntwo')
    assert list(archive) == ['one', 'two']
    assert repr(archive) == "LineArchive(lines=['one'], active='two')"


def test_line_sources_align_with_archived_lines():
    archive = LineArchive(10)
    archive.feed('ab\rXY\nz')
    archive.finish()
    assert archive.lines == ('XY', 'z')
    # Stream offsets: a=0, b=1, CR=2, X=3, Y=4, LF=5, z=6; both cells were repainted.
    assert archive.line_sources == ((3, 4), (6,))


_FUZZ_SAMPLES = [
    '下载 99%\r\x1b[K完成\n',
    'a中\x08 X\r\x1b[2Kok\n',
    'abc\b\bXY\rZZ\x1b[1K\n',
    'é\bZ',
    '👩‍💻\rX',
    'a' * 30 + '\r' + 'b' * 5 + '\nline two',
    '\x1b]8;;http://example.com\x07hi\x1b]8;;\x07\rYY\n',
    'abcdef\x1b[3Xgh\rZZ\n',
    '中\r国\x08字',
]


@pytest.mark.parametrize('text', _FUZZ_SAMPLES)
def test_every_split_point_archives_identically(text):
    """Splitting the input at every possible index must not change the archive."""
    whole = LineArchive(10)
    whole.feed(text).finish()
    for split in range(1, len(text)):
        split_archive = LineArchive(10)
        split_archive.feed(text[:split])
        split_archive.feed(text[split:])
        split_archive.finish()
        assert split_archive.lines == whole.lines
        assert split_archive.line_sources == whole.line_sources


def test_dangling_escape_at_end_of_stream_paints_nothing():
    for tail in ('\x1b', '\x1b[', '\x1b[1', '\x1b]8;;x'):
        whole = LineArchive(10)
        whole.feed('ab' + tail).finish()
        charwise = _feed_charwise('ab' + tail, columns=10)
        assert whole.lines == ('ab',)
        assert charwise.lines == ('ab',)
