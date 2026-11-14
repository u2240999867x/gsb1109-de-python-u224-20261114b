"""Tests for tinycss2-backed rule parsing, matching and property parsing."""

import pytest

from counter_labels.cssmodel import (
    CounterOperation, Stylesheet, parse_content, parse_counter_property,
    parse_selector)
from counter_labels.htmltree import parse_html
import tinycss2


CSS = """
body { counter-reset: chapter note; }
.chapter { counter-increment: chapter; counter-reset: clause; }
.chapter .clause { counter-increment: clause; }
.clause.renumbered { counter-set: clause 5; }
.clause::before { content: "第 " counter(chapter) "." counter(clause) " 条"; }
ol.reversed-with-start { counter-reset: reversed(item 3); }
ol.reversed-default { counter-reset: reversed(item); }
ol.sub > li { counter-increment: item -1; }
ol.sub > li::before { content: "(" counter(item, lower-alpha) ") "; }
.clause::after { content: " [tail " counters(chapter, ".") "]"; }
.note { counter-increment: note; }
@media print { .clause::before { content: "P"; } }
"""


def document():
    return parse_html(
        '<html><body><section class="chapter">'
        '<article class="clause" id="a1"><p>x</p>'
        '<ol class="sub reversed-with-start"><li>甲</li><li>乙</li></ol>'
        '<span class="note">n1</span></article>'
        '<article class="clause renumbered" id="a2">y</article>'
        '</section></body></html>')


def test_tinycss2_ast_is_exposed():
    sheet = Stylesheet(CSS)
    expected = tinycss2.parse_stylesheet(CSS, skip_comments=True,
                                         skip_whitespace=True)
    assert [node.serialize() for node in sheet.ast] == \
        [node.serialize() for node in expected]
    assert all(hasattr(node, 'source_line') for node in sheet.ast)
    assert sheet.errors == []


def test_selector_parsing():
    chain = parse_selector('ol.sub > li::before')
    assert chain[0][1]['tag'] == 'ol'
    assert chain[0][1]['classes'] == ['sub']
    assert chain[1][0] == '>'
    assert chain[1][1]['tag'] == 'li'
    assert chain[1][1]['pseudo'] == 'before'
    assert parse_selector('section + p') is None
    assert parse_selector('.a:not(.b)')[0][1]['not']


def test_matched_declarations_and_specificity():
    sheet = Stylesheet(CSS)
    doc = document()
    clause = doc.find_id('a1')
    matched = sheet.matched_declarations(clause)
    properties = {item.name for item in matched}
    assert {'counter-increment'}.issubset(properties)
    before = sheet.matched_declarations(clause, 'before')
    assert any(item.name == 'content' for item in before)
    after = sheet.matched_declarations(clause, 'after')
    assert any(item.name == 'content' for item in after)
    li = doc.find_path('/html/body/section/article[1]/ol/li[1]')
    li_matches = sheet.matched_declarations(li)
    increment = next(item for item in li_matches
                     if item.name == 'counter-increment')
    assert increment.selector_text == 'ol.sub > li'
    assert increment.specificity == (0, 1, 2)


def test_cascade_keeps_highest_priority():
    sheet = Stylesheet(CSS)
    doc = document()
    clause = doc.find_id('a2')
    style = sheet.effective_style(clause)
    assert style['counter-set'].declaration.serialize() == \
        'counter-set: clause 5'
    assert style['counter-increment'].selector_text == \
        '.chapter .clause'


def test_nested_media_rules_are_flattened():
    sheet = Stylesheet(CSS)
    media_rule = next(rule for rule in sheet.rules if rule.media_chain)
    assert media_rule.media_chain == ['print']
    doc = document()
    clause = doc.find_id('a1')
    print_content = next(
        item for item in sheet.matched_declarations(clause, 'before')
        if item.selector_text == '.clause::before'
        and item.rule.media_chain == ['print'])
    assert print_content.rule.media_chain == ['print']
    string_tokens = [token for token in print_content.declaration.value
                     if token.type == 'string']
    assert string_tokens[0].value == 'P'


def test_parse_counter_property():
    reset = parse_counter_property(
        tinycss2.parse_one_declaration('counter-reset: chapter note'))
    assert [op.name for op in reset] == ['chapter', 'note']
    assert all(op.value is None for op in reset)

    explicit = parse_counter_property(
        tinycss2.parse_one_declaration('counter-reset: reversed(item 3)'))
    assert explicit == [CounterOperation('item', 3, True)]

    default_reversed = parse_counter_property(
        tinycss2.parse_one_declaration('counter-reset: reversed(item)'))
    assert default_reversed == [CounterOperation('item', None, True)]

    none_ops = parse_counter_property(
        tinycss2.parse_one_declaration('counter-reset: none'))
    assert none_ops == []

    increment = parse_counter_property(
        tinycss2.parse_one_declaration('counter-increment: item -1'))
    assert increment == [CounterOperation('item', -1, False)]

    invalid = parse_counter_property(
        tinycss2.parse_one_declaration('counter-reset: 12'))
    assert invalid is None


def test_parse_content_items():
    items = parse_content(tinycss2.parse_one_declaration(
        'content: "第 " counter(chapter) "." counter(clause) " 条"'))
    kinds = [kind for kind, _ in items]
    assert kinds == ['text', 'counter', 'text', 'counter', 'text']
    chapter_ref = items[1][1]
    assert chapter_ref.name == 'chapter'
    assert chapter_ref.style == 'decimal'

    nested = parse_content(tinycss2.parse_one_declaration(
        'content: counters(chapter, ".", upper-roman)'))
    ref = nested[0][1]
    assert ref.nested is True
    assert ref.separator == '.'
    assert ref.style == 'upper-roman'

    styled = parse_content(tinycss2.parse_one_declaration(
        'content: "(" counter(item, lower-alpha) ")"'))
    assert styled[1][1].style == 'lower-alpha'

    assert parse_content(
        tinycss2.parse_one_declaration('content: none')) is None


def test_important_and_invalid_declarations():
    sheet = Stylesheet(
        '.a { color: red; counter-increment: x; }\n'
        '.b { counter-increment: y !important; }')
    doc = parse_html('<html><body><p class="a b">z</p></body></html>')
    paragraph = doc.find_path('/html/body/p')
    style = sheet.effective_style(paragraph)
    assert style['counter-increment'].important is True
    assert style['counter-increment'].declaration.serialize() == \
        'counter-increment: y !important'


def test_attribute_and_not_selectors():
    sheet = Stylesheet(
        'ol[data-kind="rev"] > li { counter-increment: item; }\n'
        'li:not(.skip)::before { content: counter(item); }')
    doc = parse_html(
        '<html><body><ol data-kind="rev">'
        '<li class="skip">a</li><li>b</li></ol></body></html>')
    skipped = doc.find_path('/html/body/ol/li[1]')
    regular = doc.find_path('/html/body/ol/li[2]')
    assert sheet.matched_declarations(skipped, 'before') == []
    assert sheet.matched_declarations(regular, 'before')
    assert sheet.effective_style(regular)['counter-increment']
