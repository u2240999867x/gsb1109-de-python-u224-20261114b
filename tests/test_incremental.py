import pytest
from webencodings import lookup

from tinycss2 import (
    parse_stylesheet_bytes, parse_stylesheet_bytes_detailed, serialize)
from tinycss2.bytes import (
    BOM, CHARSET, DEFAULT, ENVIRONMENT, PROTOCOL, determine_encoding)
from tinycss2.incremental import (
    FIRST_PARSE, INHERITED_ENCODING_CHANGED, OWN_ENCODING_HINT,
    PROTOCOL_CHANGED, SOURCE_CHANGED, UNCHANGED, StylesheetCollector,
    extract_identifiers, extract_resource_references)


def test_encoding_decision_records_source():
    css_bytes = b'@charset "ISO-8859-5"; .x {}'
    decision = determine_encoding(css_bytes)
    assert decision.encoding.name == 'iso-8859-5'
    assert decision.source == CHARSET
    assert decision.label == 'ISO-8859-5'


def test_parse_stylesheet_bytes_detailed_matches_plain_parse():
    css_bytes = b'@charset "ISO-8859-5"; .\xe9 {}'
    rules, decision = parse_stylesheet_bytes_detailed(css_bytes)
    plain_rules, encoding = parse_stylesheet_bytes(css_bytes)
    assert decision.encoding == encoding
    assert decision.source == CHARSET
    assert serialize(rules) == serialize(plain_rules)


def test_bom_source_is_reported():
    collector = StylesheetCollector()
    collector.add('root', b'\xff\xfe.\x00a\x00')
    record = collector.collect('root')
    assert record.encoding.name == 'utf-16le'
    assert record.encoding_source == BOM


def test_default_source_is_reported():
    collector = StylesheetCollector()
    collector.add('root', b'.a {}')
    record = collector.collect('root')
    assert record.encoding_source == DEFAULT


def test_environment_encoding_is_reported_for_child():
    collector = StylesheetCollector()
    collector.add('child', b'.\xe9 {}')
    record = collector.collect('child', lookup('iso-8859-2'))
    assert record.encoding.name == 'iso-8859-2'
    assert record.encoding_source == ENVIRONMENT
    assert record.environment_encoding.name == 'iso-8859-2'


def test_parent_encoding_is_inherited_by_tree_walk():
    collector = StylesheetCollector()
    collector.add('root', b'@charset "iso-8859-2";')
    collector.add('child', b'.\xe9 {}')
    collector.link('root', 'child')
    result = collector.collect_all('root')
    assert result.record_for('root').encoding.name == 'iso-8859-2'
    child = result.record_for('child')
    assert child.encoding.name == 'iso-8859-2'
    assert child.encoding_source == ENVIRONMENT
    assert child.rules[0].prelude[1].value == '\xe9'


def test_unchanged_parent_does_not_reread_child():
    collector = StylesheetCollector()
    collector.add('root', b'@charset "iso-8859-2";')
    collector.add('child', b'.\xe9 {}')
    collector.link('root', 'child')
    collector.collect_all('root')
    assert collector.children_to_reread('root') == []
    second = collector.collect_all('root')
    child = second.record_for('child')
    assert child.reused
    assert child.reason == UNCHANGED


def test_parent_encoding_change_rereads_child_from_bytes():
    collector = StylesheetCollector()
    collector.add('root', b'@charset "iso-8859-2";')
    collector.add('child', b'.\xe9 {}')
    collector.link('root', 'child')
    collector.collect_all('root')
    collector.update('root', b'@charset "iso-8859-5";')
    assert collector.children_to_reread('root') == ['child']
    result = collector.collect_all('root')
    child = result.record_for('child')
    assert not child.reused
    assert child.reason == INHERITED_ENCODING_CHANGED
    assert child.encoding.name == 'iso-8859-5'
    assert child.rules[0].prelude[1].value == 'щ'
    assert child in result.reread_from_source


def test_child_with_bom_is_not_reread_on_parent_change():
    collector = StylesheetCollector()
    collector.add('root', b'@charset "iso-8859-2";')
    collector.add('child', b'\xef\xbb\xbf.\xc3\xa9 {}')
    collector.link('root', 'child')
    collector.collect_all('root')
    before = collector.collect('child', lookup('iso-8859-2'))
    collector.update('root', b'@charset "iso-8859-5";')
    assert collector.children_to_reread('root') == []
    after = collector.collect('child', lookup('iso-8859-5'))
    assert after.reused
    assert after.key == before.key
    assert after.encoding_source == BOM
    assert after.reason == OWN_ENCODING_HINT
    assert after.rules[0].prelude[1].value == '\xe9'


def test_child_with_charset_is_not_reread_on_parent_change():
    collector = StylesheetCollector()
    collector.add('root', b'@charset "iso-8859-2";')
    collector.add('child', b'@charset "utf-8"; .x {}')
    collector.link('root', 'child')
    collector.collect_all('root')
    collector.update('root', b'@charset "iso-8859-5";')
    assert collector.children_to_reread('root') == []
    record = collector.collect('child', lookup('iso-8859-5'))
    assert record.reused
    assert record.encoding_source == CHARSET


def test_same_bytes_in_two_encoding_contexts_coexist():
    collector = StylesheetCollector()
    collector.add('child', b'.\xe9 {}')
    first = collector.collect('child', lookup('iso-8859-2'))
    second = collector.collect('child', lookup('iso-8859-5'))
    assert first.key != second.key
    assert first.encoding.name == 'iso-8859-2'
    assert second.encoding.name == 'iso-8859-5'
    assert first.rules[0].prelude[1].value == '\xe9'
    assert second.rules[0].prelude[1].value == 'щ'
    records = collector.records_for('child')
    assert len(records) == 2
    assert {record.encoding.name for record in records} == {
        'iso-8859-2', 'iso-8859-5'}


def test_two_parents_inherit_different_encodings_side_by_side():
    collector = StylesheetCollector()
    collector.add('parent2', b'@charset "iso-8859-5";')
    collector.add('parent1', b'@charset "iso-8859-2";')
    collector.add('shared', b'.\xe9 {}')
    collector.link('parent1', 'shared')
    collector.link('parent2', 'shared')
    result = collector.collect_all('parent1')
    collector.collect_all('parent2')
    records = [
        record for record in collector._records.values()
        if record.resource_id == 'shared']
    assert {record.encoding.name for record in records} == {
        'iso-8859-2', 'iso-8859-5'}
    assert {record.parent_id for record in records} == {'parent1', 'parent2'}
    assert result.record_for('shared').encoding.name == 'iso-8859-2'


def test_reread_plan_follows_the_affected_subtree():
    collector = StylesheetCollector()
    collector.add('root', b'@charset "iso-8859-2";')
    collector.add('child', b'.x {}')
    collector.add('grandchild', b'.\xe9 {}')
    collector.add('stable', b'\xef\xbb\xbf.a {}')
    collector.link('root', 'child')
    collector.link('child', 'grandchild')
    collector.link('root', 'stable')
    collector.collect_all('root')
    assert collector.reread_plan('root') == []
    collector.update('root', b'@charset "iso-8859-5";')
    assert collector.reread_plan('root') == [
        ('root', None), ('child', 'root'), ('grandchild', 'child')]
    result = collector.collect_all('root')
    assert result.reparsed


def test_reread_plan_keeps_child_with_own_hint_out():
    collector = StylesheetCollector()
    collector.add('root', b'@charset "iso-8859-2";')
    collector.add('child', b'@charset "utf-8"; .x {}')
    collector.link('root', 'child')
    collector.collect_all('root')
    collector.update('root', b'@charset "iso-8859-5";')
    assert collector.reread_plan('root') == [('root', None)]


def test_incremental_output_matches_full_parse():
    root_bytes = '@charset "iso-8859-2"; .a { background: url(./x.png) }'
    child_bytes = '@import "./y.css"; .\xe9 { content: "\xe9" }'
    grandchild_bytes = '.\xe9 { color: red }'

    collector = StylesheetCollector()
    collector.add('root', root_bytes.encode('latin1'))
    collector.add('child', child_bytes.encode('latin1'))
    collector.add('grandchild', grandchild_bytes.encode('latin1'))
    collector.link('root', 'child')
    collector.link('child', 'grandchild')

    result = collector.collect_all('root')
    expected_sources = {
        'root': (root_bytes, 'iso-8859-2'),
        'child': (child_bytes, 'iso-8859-2'),
        'grandchild': (grandchild_bytes, 'iso-8859-2'),
    }
    for resource_id, (source, _) in expected_sources.items():
        record = result.record_for(resource_id)
        rules, encoding = parse_stylesheet_bytes(
            source.encode('latin1'),
            environment_encoding=record.environment_encoding)
        assert record.encoding.name == encoding.name
        assert record.serialize() == serialize(rules)
        assert record.identifiers == extract_identifiers(rules)
        assert record.resource_references == extract_resource_references(rules)


def test_identifiers_and_resources_are_collected():
    collector = StylesheetCollector()
    collector.add(
        'root',
        b'@import "base.css"; #id.cls { background: url(a.png); '
        b'width: 2px; font: url("b.woff") }')
    record = collector.collect('root')
    assert record.resource_references == ['base.css', 'a.png', 'b.woff']
    identifiers = record.identifiers
    assert 'id' in identifiers
    assert 'cls' in identifiers
    assert 'background' in identifiers
    assert 'url' in identifiers
    assert 'px' in identifiers


def test_source_change_forces_reread():
    collector = StylesheetCollector()
    collector.add('root', b'.a {}')
    collector.collect('root')
    collector.update('root', b'.b {}')
    record = collector.collect('root')
    assert not record.reused
    assert record.reason == SOURCE_CHANGED
    assert record.identifiers == ['b']


def test_protocol_change_forces_reread():
    collector = StylesheetCollector()
    collector.add('root', b'.\xe9 {}')
    collector.collect('root')
    collector.update('root', b'.\xe9 {}', protocol_encoding='iso-8859-5')
    record = collector.collect('root')
    assert not record.reused
    assert record.reason == PROTOCOL_CHANGED
    assert record.encoding_source == PROTOCOL


def test_first_parse_is_reported():
    collector = StylesheetCollector()
    collector.add('root', b'')
    record = collector.collect('root')
    assert record.reason == FIRST_PARSE
    assert not record.reused


def test_link_validation():
    collector = StylesheetCollector()
    with pytest.raises(ValueError):
        collector.link('a', 'a')
    collector.link('a', 'b')
    assert collector.children_of('a') == ('b',)
    assert collector.link('a', 'b') is False
    assert collector.unlink('a', 'b') is True
    assert collector.children_of('a') == ()


def test_tree_reuses_cache_across_two_roots():
    collector = StylesheetCollector()
    collector.add('root1', b'@charset "iso-8859-2";')
    collector.add('root2', b'@charset "iso-8859-2";')
    collector.add('shared', b'.\xe9 {}')
    collector.link('root1', 'shared')
    collector.link('root2', 'shared')
    first = collector.collect_all('root1')
    second = collector.collect_all('root2')
    assert first.record_for('shared').key == second.record_for('shared').key
    assert second.record_for('shared').reused
    assert first.record_for('shared').reason == FIRST_PARSE
