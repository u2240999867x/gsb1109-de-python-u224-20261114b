"""Tests for SGR event table extraction."""

from __future__ import annotations

# 3rd party
import pytest

# local
from wcwidth import SgrEvent, SgrEventParser, iter_sgr_events, sgr_events
from wcwidth.sgr_events import byte_offset_at


def test_plain_text_single_event():
    """Text without any SGR sequence yields one unpainted event."""
    assert sgr_events('hello') == [SgrEvent(start=0, end=5, start_byte=0, end_byte=5, text='hello')]


def test_foreground_mapping_and_offsets():
    """Mapped foreground spans a text interval with exact byte offsets."""
    text = '\x1b[31mred\x1b[39m plain'
    assert sgr_events(text, foreground={31: 'alert'}) == [
        SgrEvent(start=0, end=3, start_byte=5, end_byte=8, text='red', foreground='alert'),
        SgrEvent(start=3, end=9, start_byte=13, end_byte=19, text=' plain'),
    ]


def test_background_mapping():
    """Mapped background is reported independently of foreground."""
    text = '\x1b[44mblue\x1b[49mnone'
    assert sgr_events(text, background={44: 'info'}) == [
        SgrEvent(start=0, end=4, start_byte=5, end_byte=9, text='blue', background='info'),
        SgrEvent(start=4, end=8, start_byte=14, end_byte=18, text='none'),
    ]


def test_intensity_mapping():
    """Bold and dim map separately, and both together use the combined key."""
    text = '\x1b[1mbold\x1b[2mdim\x1b[22mplain'
    events = sgr_events(text, intensity={1: 'high', 2: 'low', (1, 2): 'both'})
    assert [(event.text, event.intensity) for event in events] == [
        ('bold', 'high'),
        ('dim', 'both'),
        ('plain', None),
    ]


def test_unmapped_style_keeps_text():
    """A color with no mapping never drops the text it paints."""
    text = '\x1b[35mmagenta\x1b[0m tail'
    events = sgr_events(text, foreground={31: 'red'})
    assert [(event.text, event.foreground) for event in events] == [
        ('magenta', None),
        (' tail', None),
    ]
    assert events[0].byte_slice(text.encode()) == b'magenta'


def test_default_foreground_does_not_clear_background():
    """SGR 39 restores only the foreground; the background remains active."""
    text = '\x1b[31;44mAB\x1b[39mCD\x1b[49mEF'
    events = sgr_events(text, foreground={31: 'red'}, background={44: 'blue'})
    assert [(event.text, event.foreground, event.background) for event in events] == [
        ('AB', 'red', 'blue'),
        ('CD', None, 'blue'),
        ('EF', None, None),
    ]


def test_default_background_does_not_clear_foreground():
    """SGR 49 restores only the background."""
    text = '\x1b[31;44mAB\x1b[49mCD\x1b[39mEF'
    events = sgr_events(text, foreground={31: 'red'}, background={44: 'blue'})
    assert [(event.text, event.foreground, event.background) for event in events] == [
        ('AB', 'red', 'blue'),
        ('CD', 'red', None),
        ('EF', None, None),
    ]


def test_custom_default_color_mapping():
    """The caller may map the default color with None or the SGR code."""
    text = '\x1b[31mred\x1b[39mdefault'
    by_none = sgr_events(text, foreground={31: 'red', None: 'default'})
    by_code = sgr_events(text, foreground={31: 'red', 39: 'default'})
    assert [event.foreground for event in by_none] == ['red', 'default']
    assert [event.foreground for event in by_code] == ['red', 'default']


def test_extended_color_mapping():
    """256-color and truecolor tuples are mapping keys."""
    text = '\x1b[38;5;196mX\x1b[38;2;1;2;3mY'
    events = sgr_events(text, foreground={(38, 5, 196): 'indexed', (38, 2, 1, 2, 3): 'rgb'})
    assert [event.foreground for event in events] == ['indexed', 'rgb']


def test_colon_extended_color_mapping():
    """ITU T.416 colon colors map by their parsed tuple."""
    text = '\x1b[38:2::255:0:0mX'
    events = sgr_events(text, foreground={(38, 2, 0, 255, 0, 0): 'rgb'})
    assert [event.foreground for event in events] == ['rgb']


def test_wide_characters_use_screen_columns():
    """Event columns count display cells, not codepoints."""
    text = '中\x1b[32mok\x1b[0m!'
    assert sgr_events(text, foreground={32: 'ok'}) == [
        SgrEvent(start=0, end=2, start_byte=0, end_byte=3, text='中'),
        SgrEvent(start=2, end=4, start_byte=8, end_byte=10, text='ok', foreground='ok'),
        SgrEvent(start=4, end=5, start_byte=14, end_byte=15, text='!'),
    ]


def test_combining_cluster_is_one_cell():
    """A combining cluster measures as one cell and spans all of its bytes."""
    text = 'éx'
    assert sgr_events(text) == [SgrEvent(start=0, end=2, start_byte=0, end_byte=4, text='éx')]


def test_tab_advances_column():
    """Tabs occupy screen columns and are not part of an event."""
    text = 'a\tb'
    events = sgr_events(text)
    assert [(event.text, event.start, event.end) for event in events] == [('a', 0, 1), ('b', 8, 9)]
    assert sgr_events(text, tabsize=0)[1].start == 1


def test_newline_resets_column():
    """A line feed returns the following text to column zero."""
    events = sgr_events('ab\r\ncd')
    assert [(event.text, event.start, event.end) for event in events] == [
        ('ab', 0, 2),
        ('cd', 0, 2),
    ]


def test_byte_offset_at_round_trip():
    """Every event byte range round-trips through the original bytes."""
    raw = '\x1b[31mred\x1b[39m plain'.encode()
    events = sgr_events(raw, foreground={31: 'red'})
    for event in events:
        assert event.byte_slice(raw).decode() == event.text
        assert raw[event.start_byte : event.end_byte].decode() == event.text


def test_byte_offset_at_column():
    """A screen column reverses to the byte offset where its character starts."""
    raw = '\x1b[31mred\x1b[0m'.encode()
    events = sgr_events(raw)
    assert byte_offset_at(events, raw, 0) == 5
    assert byte_offset_at(events, raw, 1) == 6
    assert byte_offset_at(events, raw, 3) is None


def test_event_byte_offset_at():
    """SgrEvent.byte_offset_at maps its own columns, with wide cells sharing the start byte."""
    raw = '中x'.encode()
    event = sgr_events(raw)[0]
    assert event.byte_offset_at(0) == 0
    assert event.byte_offset_at(1) == 0
    assert event.byte_offset_at(2) == 3
    assert event.byte_offset_at(3) == 4
    assert event.byte_offset_at(4) is None


def test_incremental_split_sequence_does_not_apply_early():
    """A style split across chunks takes effect only when the final byte arrives."""
    parser = SgrEventParser(foreground={31: 'red'})
    assert parser.feed('a\x1b[31') == [SgrEvent(start=0, end=1, start_byte=0, end_byte=1, text='a')]
    assert parser.feed('m') == []
    assert parser.feed('b') == []
    assert parser.finish() == [
        SgrEvent(start=1, end=2, start_byte=6, end_byte=7, text='b', foreground='red')
    ]


def test_incremental_split_sequence_preserves_prior_event():
    """Text before an incomplete sequence is emitted under the old style."""
    parser = SgrEventParser(foreground={31: 'red'})
    assert parser.feed('plain\x1b[3') == [
        SgrEvent(start=0, end=5, start_byte=0, end_byte=5, text='plain')
    ]
    assert parser.feed('1mred') == []
    assert parser.finish() == [
        SgrEvent(start=5, end=8, start_byte=10, end_byte=13, text='red', foreground='red')
    ]


@pytest.mark.parametrize('cut', range(1, 6))
def test_incremental_every_cut_point(cut):
    """Feeding a sequence in two pieces at any point matches one-shot parsing."""
    text = '\x1b[31mred\x1b[39m plain'
    expected = sgr_events(text, foreground={31: 'red'})
    parser = SgrEventParser(foreground={31: 'red'})
    events = parser.feed(text[:cut]) + parser.feed(text[cut:]) + parser.finish()
    assert events == expected


def test_incremental_split_utf8_character():
    """A multi-byte character split across chunks stays in one event."""
    raw = '中'.encode()
    parser = SgrEventParser()
    assert parser.feed(raw[:1]) == []
    assert parser.feed(raw[1:]) == []
    assert parser.finish() == [SgrEvent(start=0, end=2, start_byte=0, end_byte=3, text='中')]


def test_incremental_split_combining_cluster():
    """A combining mark that arrives later joins the preceding base character."""
    parser = SgrEventParser()
    assert parser.feed('e') == []
    assert parser.feed('́') == []
    assert parser.finish() == [SgrEvent(start=0, end=1, start_byte=0, end_byte=3, text='é')]


def test_incremental_byte_offsets_are_absolute():
    """Byte offsets remain absolute across many chunks."""
    parser = SgrEventParser(foreground={31: 'red'})
    supplied = ['ab', '\x1b[', '31', 'mc', 'd', '\x1b[', '0m', 'ef']
    events: list[SgrEvent] = []
    for chunk in supplied:
        events.extend(parser.feed(chunk))
    events.extend(parser.finish())
    assert [(event.text, event.start_byte, event.end_byte) for event in events] == [
        ('ab', 0, 2),
        ('cd', 7, 9),
        ('ef', 13, 15),
    ]


def test_incomplete_sequence_at_finish_never_applies():
    """An unterminated SGR sequence at the end of input is discarded."""
    parser = SgrEventParser(foreground={31: 'red'})
    assert parser.feed('ok') == []
    assert parser.feed('\x1b[31') == [SgrEvent(start=0, end=2, start_byte=0, end_byte=2, text='ok')]
    assert parser.finish() == []


def test_style_snapshot_frozen_at_run_start():
    """Sequences after a run never change the style reported for it."""
    text = '\x1b[31mred\x1b[32m'
    assert sgr_events(text, foreground={31: 'red', 32: 'green'}) == [
        SgrEvent(start=0, end=3, start_byte=5, end_byte=8, text='red', foreground='red')
    ]


def test_non_sgr_sequences_are_not_painted_or_lost():
    """Non-SGR sequences are omitted from events without dropping surrounding text."""
    text = 'a\x1b]0;title\x07b\x1b]8;;http://x\x07c\x1b]8;;\x07d'
    events = sgr_events(text)
    assert [event.text for event in events] == ['a', 'b', 'c', 'd']
    assert [event.start for event in events] == [0, 1, 2, 3]
    raw = text.encode()
    assert b''.join(event.byte_slice(raw) for event in events) == b'abcd'


def test_cursor_movement_moves_columns():
    """Cursor forward and horizontal position shift subsequent text."""
    assert [(event.text, event.start) for event in sgr_events('a\x1b[3Cb')] == [('a', 0), ('b', 4)]
    assert [(event.text, event.start) for event in sgr_events('a\x1b[10Gb')] == [('a', 0), ('b', 9)]


def test_c1_control_is_zero_width_and_splits():
    """C1 controls separate runs without occupying a column."""
    text = 'a\u0085b'
    events = sgr_events(text)
    assert [(event.text, event.start, event.end, event.start_byte) for event in events] == [
        ('a', 0, 1, 0),
        ('b', 1, 2, 3),
    ]


def test_repeated_style_reports_same_mapping():
    """Re-applying the same style yields separate intervals with equal mapping."""
    text = 'ab\x1b[31mcd\x1b[31mef'
    events = sgr_events(text, foreground={31: 'red'})
    assert [(event.text, event.foreground) for event in events] == [
        ('ab', None),
        ('cd', 'red'),
        ('ef', 'red'),
    ]


def test_intensity_string_keys():
    """Intensity also accepts the descriptive string keys."""
    text = '\x1b[1mA\x1b[2mB\x1b[22mC'
    events = sgr_events(text, intensity={'bold': 'strong', 'dim': 'faint', 'bold+dim': 'mixed'})
    assert [event.intensity for event in events] == ['strong', 'mixed', None]


def test_iter_sgr_events_is_lazy():
    """The iterator yields the same events as the list function."""
    assert list(iter_sgr_events('\x1b[31mred')) == sgr_events('\x1b[31mred')


def test_event_aliases_and_dict():
    """Column, byte, and color aliases agree with the canonical fields."""
    event = sgr_events('\x1b[31mred', foreground={31: 'red'})[0]
    assert (event.start_column, event.end_column) == (event.start, event.end)
    assert (event.column_start, event.column_end) == (event.start, event.end)
    assert (event.byte_start, event.byte_end) == (event.start_byte, event.end_byte)
    assert (event.fg, event.bg) == ('red', None)
    assert event.as_dict() == {
        'start': 0,
        'end': 3,
        'start_byte': 5,
        'end_byte': 8,
        'text': 'red',
        'foreground': 'red',
        'background': None,
        'intensity': None,
    }


def test_feed_after_finish_raises():
    """The parser is single-use and cannot accept bytes after finish()."""
    parser = SgrEventParser()
    parser.feed('a')
    parser.finish()
    with pytest.raises(RuntimeError):
        parser.feed('b')
    assert parser.finish() == []


def test_ambiguous_width_argument():
    """Width of East Asian Ambiguous characters follows the caller's choice."""
    assert sgr_events('±x')[0].end == 2
    event = sgr_events('±x', ambiguous_width=2)[0]
    assert event.end == 3
