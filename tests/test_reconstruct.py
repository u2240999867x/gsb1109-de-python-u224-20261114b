"""Tests for terminal-snippet reconstruction (wcwidth.reconstruct)."""

# std imports
import pytest

# local
from wcwidth import (
    ColumnSpan,
    Reconstruction,
    Snippet,
    find_originals,
    reconcile,
    snippet_ranges,
)


def _bounds(ranges):
    return [(rng.start_grapheme, rng.end_grapheme) for rng in ranges]


def test_single_prefix_snippet_matches_candidate_prefix():
    ranges = snippet_ranges(Snippet('hello', 10, 'right'), 'hello world')
    assert _bounds(ranges) == [(0, 5)]
    assert ranges[0].text == 'hello'
    assert ranges[0].columns == (ColumnSpan(0, 0, 5),)


def test_two_snippets_narrow_to_compatible_candidate():
    candidates = ['hello world', 'goodbye world', 'hello again']
    snippets = [Snippet('hello wor', 10, 'right'), Snippet('hello…', 8, 'right')]
    result = find_originals(candidates, snippets)
    assert [recon.candidate for recon in result] == ['hello world']
    assert _bounds(result[0].ranges) == [(0, 5), (0, 9)]


def test_inserted_ellipsis_requires_hidden_text():
    # Hidden original text exists: the stripped anchor is compatible.
    assert _bounds(snippet_ranges(Snippet('hello…', 8, 'right'), 'hello world')) == [(0, 5)]
    # Record ends exactly at the anchor; an inserted ellipsis would have nothing to hide,
    # only the literal reading survives and it needs a real ellipsis in the record.
    assert snippet_ranges(Snippet('hello…', 8, 'right'), 'hello') == []


def test_literal_ellipsis_in_record_is_anchor_text():
    # Window width 2 overflows the width-3 record, so the inserted reading (anchor 'a')
    # and the literal reading (the record's own ellipsis, anchor 'a…') both apply.
    ranges = snippet_ranges(Snippet('a…', 2, 'right'), 'a…b')
    assert _bounds(ranges) == [(0, 1), (0, 2)]
    assert {rng.text for rng in ranges} == {'a', 'a…'}


def test_non_overflowing_window_rules_out_inserted_ellipsis():
    # The width-3 record fits the width-5 window, so the only viable reading is literal.
    ranges = snippet_ranges(Snippet('a…', 5, 'right'), 'a…b')
    assert _bounds(ranges) == [(0, 2)]


def test_left_clip_matches_suffix():
    ranges = snippet_ranges(Snippet('…world', 8, 'left'), 'hello world')
    assert _bounds(ranges) == [(6, 11)]
    assert ranges[0].text == 'world'


def test_left_inserted_ellipsis_requires_hidden_prefix():
    # Width-3 record fits the width-4 window, so no truncation (and no inserted ellipsis).
    assert snippet_ranges(Snippet('…abc', 4, 'left'), 'abc') == []
    # A two-column hidden prefix overflows the width-4 window, making the clip valid.
    assert _bounds(snippet_ranges(Snippet('…abc', 4, 'left'), 'xyabc')) == [(2, 5)]


def test_repeated_phrase_keeps_every_position():
    recon = reconcile('xabc yabc z', [Snippet('abc', 4, 'right')])
    assert _bounds(recon.ranges) == [(1, 4), (6, 9)]
    assert all(rng.text == 'abc' for rng in recon.ranges)


def test_overlapping_occurrences_are_kept_distinct():
    recon = reconcile('xaaa z', [Snippet('aa', 3, 'right')])
    # 'aa' occurs twice with overlapping, but distinct, grapheme bounds.
    assert _bounds(recon.ranges) == [(1, 3), (2, 4)]
    assert all(rng.text == 'aa' for rng in recon.ranges)


def test_custom_ascii_ellipsis_token():
    # '...' (three narrow dots) is the inserted token, distinct from a single U+2026.
    # The width-6 record overflows the width-5 window, leaving hidden text after 'ab'.
    ranges = snippet_ranges(
        Snippet('ab...', 5, 'right', ellipsis='...'), 'abcdef')
    assert _bounds(ranges) == [(0, 2)]


def test_cjk_wide_characters_measured_in_cells():
    record = '中文abc'
    ranges = snippet_ranges(Snippet('中', 4, 'right'), record)
    # '中' occupies two columns.
    assert ranges[0].columns == (ColumnSpan(0, 0, 2),)


def test_ambiguous_width_policy_changes_columns():
    record = 'a·b'
    narrow = snippet_ranges(Snippet('a·', 3, 'right', ambiguous_width=1), record)
    wide = snippet_ranges(Snippet('a·', 3, 'right', ambiguous_width=2), record)
    assert narrow[0].columns == (ColumnSpan(0, 0, 2),)
    assert wide[0].columns == (ColumnSpan(0, 0, 3),)


def test_per_terminal_wide_char_policy():
    # U+18E00 is width 2 in the standard tables but width 1 under kitty's overrides.
    wide = chr(0x18E00)
    record = 'ab' + wide + 'cd'
    standard = snippet_ranges(Snippet('ab', 4, 'right'), record)[0]
    kitty = snippet_ranges(
        Snippet('ab', 4, 'right', term_program='kitty'), record)[0]
    # Grapheme positions are policy independent.
    assert (kitty.start_grapheme, kitty.end_grapheme) == (0, 2)
    assert (standard.start_grapheme, standard.end_grapheme) == (0, 2)
    # The columns of a later anchor differ across policies.
    later_standard = snippet_ranges(Snippet('cd', 4, 'right'), record)[0]
    later_kitty = snippet_ranges(
        Snippet('cd', 4, 'right', term_program='kitty'), record)[0]
    assert later_standard.columns[0].start_column == 4
    assert later_kitty.columns[0].start_column == 3


def test_grapheme_clusters_count_as_one_index():
    # 'e' + combining acute is one grapheme; an emoji ZWJ sequence is one grapheme.
    record = 'café ' + '\U0001F468‍\U0001F469'
    anchor = 'café'
    ranges = snippet_ranges(Snippet(anchor, 8, 'right'), record)
    # Four graphemes: c, a, f, e+acute; the combining mark adds no index of its own.
    assert _bounds(ranges) == [(0, 4)]
    assert ranges[0].text == anchor
    emoji = '\U0001F468‍\U0001F469'
    emoji_ranges = snippet_ranges(Snippet(emoji, 4, 'right'), record)
    assert _bounds(emoji_ranges) == [(5, 6)]


def test_joint_reconciliation_keeps_each_snippet_bounds():
    # The two snippets nest but keep their distinct bounds; identical bounds would merge.
    recon = reconcile('hello world', [
        Snippet('hello', 8, 'right'),
        Snippet('hello wo', 10, 'right'),
    ])
    assert _bounds(recon.ranges) == [(0, 5), (0, 8)]
    by_bounds = {(rng.start_grapheme, rng.end_grapheme): rng for rng in recon.ranges}
    assert by_bounds[(0, 5)].columns == (ColumnSpan(0, 0, 5),)
    assert by_bounds[(0, 8)].columns == (ColumnSpan(1, 0, 8),)


def test_identical_bounds_merge_column_spans_from_both_snippets():
    recon = reconcile('hello world', [
        Snippet('hello', 8, 'right'),
        Snippet('hello', 9, 'right'),
    ])
    assert _bounds(recon.ranges) == [(0, 5)]
    assert {span.snippet for span in recon.ranges[0].columns} == {0, 1}


def test_candidate_contradicting_any_snippet_is_dropped():
    # The second anchor occurs in no candidate, so every candidate is rejected.
    snippets = [Snippet('hello', 8, 'right'), Snippet('zzz', 6, 'right')]
    assert find_originals(['hello world', 'xyz hello'], snippets) == []


def test_find_originals_preserves_order_and_dedupes():
    candidates = ['abc def', 'ghi', 'abc def']
    result = find_originals(candidates, [Snippet('abc', 5, 'right')])
    assert [recon.candidate for recon in result] == ['abc def']


def test_invalid_window_width_raises():
    with pytest.raises(ValueError):
        snippet_ranges(Snippet('a', 0, 'right'), 'abc')
    with pytest.raises(ValueError):
        snippet_ranges(Snippet('a', -1, 'right'), 'abc')


def test_fragment_wider_than_window_raises():
    with pytest.raises(ValueError):
        snippet_ranges(Snippet('abcdef', 3, 'right'), 'abcdef')


def test_invalid_ambiguous_width_raises():
    with pytest.raises(ValueError):
        snippet_ranges(Snippet('a', 3, 'right', ambiguous_width=3), 'abc')


def test_invalid_side_raises():
    with pytest.raises(ValueError):
        snippet_ranges(Snippet('a', 3, 'middle'), 'abc')


def test_side_aliases_normalize():
    assert Snippet('a', 3, 'r').normalized_side() == 'right'
    assert Snippet('a', 3, 'prefix').normalized_side() == 'right'
    assert Snippet('a', 3, 'l').normalized_side() == 'left'
    assert Snippet('a', 3, 'suffix').normalized_side() == 'left'


def test_ellipsis_only_fragment():
    # A lone inserted ellipsis with no anchor locates no range but stays compatible.
    ranges = snippet_ranges(Snippet('…', 2, 'right'), 'anything')
    assert ranges == []
    recon = reconcile('anything', [Snippet('…', 2, 'right')])
    assert isinstance(recon, Reconstruction)
    assert recon.ranges == ()
    # A literal ellipsis-only fragment still matches a record that contains it.
    assert _bounds(snippet_ranges(Snippet('…', 2, 'right'), '…')) == [(0, 1)]


def test_inserted_ellipsis_requires_window_overflow():
    # Record width 4 fits inside a width-5 window, so no truncation (and no inserted
    # ellipsis) is possible; the literal reading needs a real ellipsis that is absent.
    assert snippet_ranges(Snippet('ab…', 5, 'right'), 'abcd') == []
    # The same record overflows a width-3 window, so the inserted reading is valid.
    assert _bounds(snippet_ranges(Snippet('ab…', 3, 'right'), 'abcd')) == [(0, 2)]


def test_no_matching_occurrence_returns_empty():
    assert snippet_ranges(Snippet('zzz', 5, 'right'), 'abcdef') == []
    assert reconcile('abcdef', [Snippet('zzz', 5, 'right')]) is None
