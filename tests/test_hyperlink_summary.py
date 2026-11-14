"""Tests for terminal capture summaries and OSC 8 hyperlink region attribution."""

# 3rd party
import pytest

# local
from wcwidth import (HyperlinkRegion, HyperlinkSummary, count_clickable_keys,
                     count_visible_keys, iter_hyperlink_regions, strip_sequences)

BEL = '\x07'
ST = '\x1b\\'
SGR_RED = '\x1b[31m'
SGR_BLUE = '\x1b[34m'
SGR_RESET = '\x1b[0m'


def osc_open(url, params='', term=BEL):
    return f'\x1b]8;{params};{url}{term}'


def osc_close(term=BEL):
    return f'\x1b]8;;{term}'


def test_region_is_empty():
    assert HyperlinkRegion('http://example.com', 0, 2).is_empty is False
    assert HyperlinkRegion('', 3, 3).is_empty is True


def test_basic_region_matches_visible_text_offsets():
    capture = osc_open('http://example.com') + 'Hello' + osc_close() + ' world'
    regions = list(iter_hyperlink_regions(capture))
    assert regions == [HyperlinkRegion('http://example.com', 0, 5)]
    visible = strip_sequences(capture)
    assert visible[regions[0].start:regions[0].end] == 'Hello'


def test_st_terminator():
    capture = osc_open('http://example.com', term=ST) + 'Hi' + osc_close(term=ST)
    assert list(iter_hyperlink_regions(capture)) == [
        HyperlinkRegion('http://example.com', 0, 2)]


def test_color_only_change_identical_keys():
    red = SGR_RED + '详情' + SGR_RESET
    blue = SGR_BLUE + '详情' + SGR_RESET
    red_summary = HyperlinkSummary.from_capture(red)
    blue_summary = HyperlinkSummary.from_capture(blue)
    assert red_summary.visible_key == blue_summary.visible_key == '详情'
    assert red_summary.clickable_key == blue_summary.clickable_key


def test_same_visible_text_different_links_distinct_clickable_key():
    first = osc_open('http://a') + '详情' + osc_close()
    second = osc_open('http://b') + '详情' + osc_close()
    first_summary = HyperlinkSummary.from_capture(first)
    second_summary = HyperlinkSummary.from_capture(second)
    assert first_summary.visible_key == second_summary.visible_key == '详情'
    assert first_summary.clickable_key != second_summary.clickable_key
    assert first_summary.clickable_key[1] == (HyperlinkRegion('http://a', 0, 2),)
    assert second_summary.clickable_key[1] == (HyperlinkRegion('http://b', 0, 2),)


def test_sgr_inside_link_does_not_shift_range():
    capture = osc_open('http://example.com') + SGR_RED + 'Hi' + SGR_RESET + osc_close()
    regions = list(iter_hyperlink_regions(capture))
    assert regions == [HyperlinkRegion('http://example.com', 0, 2)]


def test_empty_url_link_is_attributed():
    # An empty URL with parameters is an open of an empty-target link, not a close.
    capture = osc_open('', params='id=1') + 'text' + osc_close()
    assert list(iter_hyperlink_regions(capture)) == [HyperlinkRegion('', 0, 4)]


def test_zero_length_region_is_reported():
    capture = 'before' + osc_open('http://example.com') + osc_close() + 'after'
    regions = list(iter_hyperlink_regions(capture))
    assert regions == [HyperlinkRegion('http://example.com', 6, 6)]
    assert regions[0].is_empty


def test_bare_close_without_open_emits_nothing():
    assert list(iter_hyperlink_regions('plain' + osc_close() + 'text')) == []


def test_close_and_reopen_same_address_is_two_regions():
    capture = (osc_open('http://example.com') + 'ab' + osc_close()
               + 'cd' + osc_open('http://example.com') + 'ef' + osc_close())
    assert list(iter_hyperlink_regions(capture)) == [
        HyperlinkRegion('http://example.com', 0, 2),
        HyperlinkRegion('http://example.com', 4, 6),
    ]


def test_reopen_without_close_ends_previous_region():
    capture = (osc_open('http://a') + 'ab'
               + osc_open('http://b') + 'cd' + osc_close())
    assert list(iter_hyperlink_regions(capture)) == [
        HyperlinkRegion('http://a', 0, 2),
        HyperlinkRegion('http://b', 2, 4),
    ]


def test_reopen_identical_address_without_close_is_two_regions():
    capture = (osc_open('http://example.com') + 'ab'
               + osc_open('http://example.com') + 'cd' + osc_close())
    assert list(iter_hyperlink_regions(capture)) == [
        HyperlinkRegion('http://example.com', 0, 2),
        HyperlinkRegion('http://example.com', 2, 4),
    ]


def test_unclosed_link_runs_to_end():
    capture = 'hi ' + osc_open('http://example.com') + 'there'
    assert list(iter_hyperlink_regions(capture)) == [
        HyperlinkRegion('http://example.com', 3, 8)]


def test_link_spanning_newline_keeps_continuous_range():
    capture = osc_open('http://example.com') + 'line1\nline2' + osc_close()
    regions = list(iter_hyperlink_regions(capture))
    assert regions == [HyperlinkRegion('http://example.com', 0, 11)]
    visible = strip_sequences(capture)
    assert visible == 'line1\nline2'
    assert visible[regions[0].start:regions[0].end] == 'line1\nline2'


def test_multiple_links_in_one_line():
    capture = (osc_open('http://a') + 'aa' + osc_close()
               + ' mid '
               + osc_open('http://b') + 'bb' + osc_close())
    assert list(iter_hyperlink_regions(capture)) == [
        HyperlinkRegion('http://a', 0, 2),
        HyperlinkRegion('http://b', 7, 9),
    ]


def test_visible_key_ignores_all_escape_sequences():
    capture = SGR_RED + osc_open('http://a') + 'x' + osc_close() + SGR_RESET
    summary = HyperlinkSummary.from_capture(capture)
    assert summary.visible_key == 'x'
    assert strip_sequences(capture) == summary.visible_key


def test_keys_are_hashable():
    summary = HyperlinkSummary.from_capture(
        osc_open('http://a') + 'x' + osc_close())
    assert hash(summary.visible_key) == hash('x')
    hash(summary.clickable_key)


def test_count_visible_keys_merges_color_and_link_variants():
    lines = [
        SGR_RED + '详情' + SGR_RESET,
        SGR_BLUE + '详情' + SGR_RESET,
        osc_open('http://a') + '详情' + osc_close(),
        osc_open('http://b') + '详情' + osc_close(),
        'other',
    ]
    counts = count_visible_keys(lines)
    assert counts == {'详情': 4, 'other': 1}
    assert len(counts) == 2


def test_count_clickable_keys_separates_targets():
    lines = [
        osc_open('http://a') + '详情' + osc_close(),
        SGR_BLUE + osc_open('http://a') + '详情' + osc_close() + SGR_RESET,
        osc_open('http://b') + '详情' + osc_close(),
        'plain',
    ]
    counts = count_clickable_keys(lines)
    # Same target and range, even with different color, share a key.
    assert len(counts) == 3
    assert sum(counts.values()) == 4
    by_region = {key[1]: count for key, count in counts.items()}
    assert by_region[(HyperlinkRegion('http://a', 0, 2),)] == 2
    assert by_region[(HyperlinkRegion('http://b', 0, 2),)] == 1
    assert by_region[()] == 1


def test_two_key_counts_explain_extra_clickable_regions():
    lines = [
        osc_open('http://a') + '详情' + osc_close(),
        osc_open('http://b') + '详情' + osc_close(),
        osc_open('http://c') + '详情' + osc_close(),
    ]
    visible_counts = count_visible_keys(lines)
    clickable_counts = count_clickable_keys(lines)
    # One distinct visible line ...
    assert len(visible_counts) == 1
    # ... but three distinct clickable regions behind it.
    assert len(clickable_counts) == 3
    extra_urls = {key[1][0].url for key in clickable_counts}
    assert extra_urls == {'http://a', 'http://b', 'http://c'}


def test_count_accepts_generator():
    lines = (osc_open('http://a') + 'x' + osc_close() for _ in range(2))
    assert sum(count_clickable_keys(lines).values()) == 2


def test_empty_input():
    assert list(iter_hyperlink_regions('')) == []
    summary = HyperlinkSummary.from_capture('')
    assert summary.visible_key == ''
    assert summary.clickable_key == ('', ())
    assert count_visible_keys([]) == {}
    assert count_clickable_keys([]) == {}


@pytest.mark.parametrize('text', [
    'plain text',
    SGR_RED + 'red' + SGR_RESET,
    '\x1b[31m',
])
def test_text_without_links_has_no_regions(text):
    assert list(iter_hyperlink_regions(text)) == []


def test_lone_escape_stays_aligned_with_visible_text():
    # An unrecognized ESC (here ESC followed by newline) is retained by
    # strip_sequences(), so it still occupies a visible offset.
    capture = 'a\x1b\nb' + osc_open('http://example.com') + 'cd' + osc_close()
    visible = strip_sequences(capture)
    assert visible == 'a\x1b\nbcd'
    regions = list(iter_hyperlink_regions(capture))
    assert regions == [HyperlinkRegion('http://example.com', 4, 6)]
    assert visible[regions[0].start:regions[0].end] == 'cd'
