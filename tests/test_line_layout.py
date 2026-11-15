"""Tests for incremental re-layout of cached lines across width rule changes."""

# std imports
import random

# 3rd party
import pytest

# local
from wcwidth.line_layout import WidthRule, WidthRules, CachedLine, LineLayoutCache


# A codepoint pool spanning every role the layout engine distinguishes: printable base
# characters, wide CJK/emoji, combining marks and joiners, C1 controls, tab and CR.
_CODEPOINT_POOL = (
    0x41, 0x42, 0x43, 0x100, 0x200, 0x301, 0x302, 0x80, 0x81,
    0x4E2D, 0x1F600, 0x09, 0x0D, 0x200D, 0xFE0F,
)


def _rules(zero=(), wide=(), ambiguous=()):
    """Build WidthRules from codepoint sequences for compact test setup."""
    return WidthRules(WidthRule(
        zero=tuple((cp, cp) for cp in zero),
        wide=tuple((cp, cp) for cp in wide),
        ambiguous=tuple((cp, cp) for cp in ambiguous),
    ))


def _text_from(pool):
    return ''.join(chr(cp) for cp in pool)


def _assert_matches_recompute(cache, cached, text, rules, ambiguous_width=1):
    """The rebuilt cache must equal a from-scratch recomputation, column by column."""
    assert cache.verify_line(cached, text, rules, ambiguous_width)
    expected = cache.column_index(text, rules, ambiguous_width)
    assert cache.reverse_index(cached, text) == expected


def test_build_records_only_clusters_offsets_and_summary():
    """A cache entry keeps just cluster spans, start columns and a text summary."""
    # given
    cache = LineLayoutCache()
    rules = _rules(wide=(0x100,))

    # exercise
    cached = cache.build('aĀ\rb', rules)

    # verify: the carriage return is reflected in offsets but not stored as a field.
    assert cached.clusters == ((0, 1), (1, 2), (2, 3), (3, 4))
    assert cached.offsets == (0, 1, 3, 0)
    assert cached.summary == 'aĀ\rb'
    assert CachedLine._fields == ('clusters', 'offsets', 'summary')


def test_wide_cluster_pushes_later_columns_until_anchor_realignment():
    """A one-cell to two-cell cluster shifts following columns, then an anchor re-aligns."""
    # given
    cache = LineLayoutCache()
    old_rules = _rules()
    new_rules = _rules(wide=(0x100,))
    text = 'aĀb\rcd'
    cached = cache.build(text, old_rules)

    # exercise
    rebuilt = cache.rebuild(cached, text, old_rules, new_rules)

    # verify: 'Ā' widens, pushing 'b' and the CR offset; post-CR segment re-aligns at 0.
    assert rebuilt.offsets == (0, 1, 3, 4, 0, 1)
    _assert_matches_recompute(cache, rebuilt, text, new_rules)


def test_wide_to_narrow_pulls_later_columns_back():
    """Shrinking a cluster from two cells to one pulls every later column left."""
    # given
    cache = LineLayoutCache()
    old_rules = _rules(wide=(0x100,))
    new_rules = _rules()
    text = 'aĀb'
    cached = cache.build(text, old_rules)
    assert cached.offsets == (0, 1, 3)

    # exercise
    rebuilt = cache.rebuild(cached, text, old_rules, new_rules)

    # verify
    assert rebuilt.offsets == (0, 1, 2)
    _assert_matches_recompute(cache, rebuilt, text, new_rules)


def test_boundary_only_change_rebuilds_reverse_index_at_same_total_width():
    """A cluster boundary that moves without a width change still updates the reverse lookup."""
    # given, C1 control U+0080: standalone under old rules, zero-width and merged under new.
    cache = LineLayoutCache()
    old_rules = _rules()
    new_rules = _rules(zero=(0x80,))
    text = 'ab\x80cd'
    cached = cache.build(text, old_rules)
    assert cached.clusters == ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5))

    # exercise
    rebuilt = cache.rebuild(cached, text, old_rules, new_rules)

    # verify: the control merged into the previous cluster but total width is unchanged.
    assert rebuilt.clusters == ((0, 1), (1, 3), (3, 4), (4, 5))
    assert cache.reverse_index(rebuilt, text) == (0, 1, 1, 2, 3)
    _assert_matches_recompute(cache, rebuilt, text, new_rules)


def test_anchor_confines_drift_to_its_segment():
    """Clusters after an anchor keep cached columns when their own segment is unaffected."""
    # given
    cache = LineLayoutCache()
    old_rules = _rules()
    new_rules = _rules(wide=(0x100,))
    text = 'Ā\rxyz'
    cached = cache.build(text, old_rules)

    # exercise
    rebuilt = cache.rebuild(cached, text, old_rules, new_rules)

    # verify: drift changes the first segment only; the post-CR segment stays at 0,1,2.
    assert rebuilt.offsets == (0, 2, 0, 1, 2)
    _assert_matches_recompute(cache, rebuilt, text, new_rules)


def test_multiple_dirty_segments_separated_by_anchors():
    """Width changes in two segments are each re-laid and re-synchronized at the anchor."""
    # given
    cache = LineLayoutCache()
    old_rules = _rules()
    new_rules = _rules(wide=(0x100, 0x200))
    text = 'Āx\rĀy'
    cached = cache.build(text, old_rules)

    # exercise
    rebuilt = cache.rebuild(cached, text, old_rules, new_rules)

    # verify
    assert rebuilt.offsets == (0, 2, 3, 0, 2)
    _assert_matches_recompute(cache, rebuilt, text, new_rules)


def test_unchanged_rules_preserve_cached_offsets():
    """When no rule differs, every cached offset survives the rebuild."""
    # given
    cache = LineLayoutCache()
    rules = WidthRules.from_current()
    text = 'a中b\rc\td'
    cached = cache.build(text, rules)

    # exercise
    rebuilt = cache.rebuild(cached, text, rules, rules)

    # verify
    assert rebuilt == cached
    _assert_matches_recompute(cache, rebuilt, text, rules)


def test_tabs_advance_to_tab_stops():
    """Tabs expand to the next tab stop and participate in drift correctly."""
    # given
    cache = LineLayoutCache(tabsize=4)
    old_rules = _rules()
    new_rules = _rules(wide=(0x100,))
    text = 'a\tbĀ'
    cached = cache.build(text, old_rules)
    assert cached.offsets == (0, 1, 4, 5)

    # exercise
    rebuilt = cache.rebuild(cached, text, old_rules, new_rules)

    # verify
    assert rebuilt.offsets == (0, 1, 4, 5)
    _assert_matches_recompute(cache, rebuilt, text, new_rules)


def test_ambiguous_width_two_is_honored_in_diff_and_layout():
    """Ambiguous codepoints count as two cells when ambiguous_width=2."""
    # given
    cache = LineLayoutCache()
    old_rules = _rules(ambiguous=(0x100,))
    new_rules = _rules(wide=(0x100,))
    text = 'aĀb'

    # exercise: under amb=2 both rule sets measure 'Ā' as width 2, so nothing changes.
    cached = cache.build(text, old_rules, ambiguous_width=2)
    rebuilt = cache.rebuild(cached, text, old_rules, new_rules, ambiguous_width=2)

    # verify
    assert old_rules.diff(new_rules, ambiguous_width=2) == frozenset()
    assert rebuilt.offsets == (0, 1, 3)
    _assert_matches_recompute(cache, rebuilt, text, new_rules, ambiguous_width=2)


def test_combining_mark_cluster_is_measured_as_a_unit():
    """A base plus combining mark stays one cluster at the base's start column."""
    # given, 'e' followed by a combining acute accent (U+0301), plus a trailing base.
    cache = LineLayoutCache()
    rules = WidthRules.from_current()
    text = 'e' + chr(0x301) + 'x'  # e + combining acute + x

    # exercise
    cached = cache.build(text, rules)

    # verify
    assert cached.clusters == ((0, 2), (2, 3))
    assert cache.column_index(text, rules) == (0, 0, 1)
    _assert_matches_recompute(cache, cached, text, rules)


def test_diff_matches_brute_force_classification():
    """The range-partition diff reports exactly the codepoints a per-codepoint scan finds."""
    # given
    old_rules = _rules(zero=(0x100,), wide=(0x200, 0x201, 0x202),
                       ambiguous=(0x300, 0x301))
    new_rules = _rules(zero=(0x101,), wide=(0x201, 0x202, 0x203),
                       ambiguous=(0x300,))

    # exercise / verify, for both ambiguous contexts
    for ambiguous_width in (1, 2):
        expected = frozenset(
            cp for cp in range(0x50, 0x320)
            if old_rules.codepoint_width(cp, ambiguous_width)
            != new_rules.codepoint_width(cp, ambiguous_width))
        assert old_rules.diff(new_rules, ambiguous_width) == expected


def test_diff_includes_two_to_zero_transitions():
    """A codepoint leaving the wide table for zero-width is part of the rule difference."""
    # given
    old_rules = _rules(wide=(0x200,))
    new_rules = _rules(zero=(0x200,))

    # verify
    assert 0x200 in old_rules.diff(new_rules)


def test_rebuild_rejects_text_not_matching_summary():
    """A cache entry only summarizes text, so mismatched text is refused rather than corrupted."""
    # given
    cache = LineLayoutCache()
    rules = _rules()
    cached = cache.build('the original line text', rules)

    # exercise / verify
    with pytest.raises(ValueError, match='summary does not match'):
        cache.rebuild(cached, 'a completely different line', rules, rules)


def test_verify_line_detects_tampered_cache():
    """verify_line returns False when offsets no longer match a from-scratch recomputation."""
    # given
    cache = LineLayoutCache()
    rules = _rules()
    cached = cache.build('abc', rules)
    tampered = CachedLine(
        clusters=cached.clusters,
        offsets=(0, 1, 9),
        summary=cached.summary,
    )

    # verify
    assert not cache.verify_line(tampered, 'abc', rules)


def test_invalid_tabsize_is_rejected():
    """Tab size must be positive because modulo tab expansion relies on it."""
    with pytest.raises(ValueError, match='tabsize must be positive'):
        LineLayoutCache(tabsize=0)


def test_from_current_rules_round_trip_real_tables():
    """Rules built from shipped tables lay out real CJK and emoji text correctly."""
    # given
    cache = LineLayoutCache()
    rules = WidthRules.from_current()

    # exercise / verify
    for text in ('hello', 'コンニチハ', 'a中b\rc\td', 'café', '👨‍👩‍👧', ''):
        cached = cache.build(text, rules)
        _assert_matches_recompute(cache, cached, text, rules)


@pytest.mark.parametrize('seed', range(25))
def test_rebuild_matches_full_recompute_randomized(seed):
    """Property test: incremental rebuild equals full recompute for random rules and lines."""
    # given
    rng = random.Random(seed)
    cache = LineLayoutCache(tabsize=rng.choice((1, 4, 8)))

    def random_rules():
        shuffled = list(_CODEPOINT_POOL)
        rng.shuffle(shuffled)
        cut1 = rng.randrange(0, len(shuffled) + 1)
        cut2 = rng.randrange(cut1, len(shuffled) + 1)
        return _rules(zero=shuffled[:cut1], wide=shuffled[cut1:cut2],
                      ambiguous=shuffled[cut2:])

    old_rules = random_rules()
    new_rules = random_rules()
    text = _text_from(rng.sample(
        _CODEPOINT_POOL, rng.randrange(0, len(_CODEPOINT_POOL))))
    ambiguous_width = rng.choice((1, 2))

    # exercise
    cached = cache.build(text, old_rules, ambiguous_width)
    rebuilt = cache.rebuild(cached, text, old_rules, new_rules, ambiguous_width)

    # verify: every new index equals the from-scratch recomputation, column by column.
    _assert_matches_recompute(cache, rebuilt, text, new_rules, ambiguous_width)
