"""Tests for rendered labels, evidence provenance and artifact export."""

import json

from counter_labels import Labeler, Stylesheet, parse_html
from counter_labels.labeling import serialize_tree

HTML = """
<html><body>
  <section class="chapter" id="c1">
    <article class="clause" id="a1"><p>正文不含编号</p></article>
    <article class="clause" id="a2"><p>同样不含</p></article>
  </section>
</body></html>
"""

CSS = """
body { counter-reset: chapter; }
.chapter { counter-increment: chapter; counter-reset: clause; }
.clause { counter-increment: clause; }
.clause::before { content: "第 " counter(chapter) "." counter(clause) " 条"; }
"""


def build():
    document = parse_html(HTML)
    stylesheet = Stylesheet(CSS)
    return Labeler(document, stylesheet).run()


def test_labels_render_generated_markers():
    result = build()
    labels = {label.node_id: label for label in result.labels}
    assert labels['a1'].text == '第 1.1 条'
    assert labels['a2'].text == '第 1.2 条'
    assert labels['a1'].pseudo == 'before'
    assert labels['a1'].tag == 'article'


def test_generated_marker_is_not_in_dom_text():
    result = build()
    label = next(label for label in result.labels
                 if label.node_id == 'a1')
    article = result.document.find_id('a1')
    assert '第 1.1 条' not in article.text_content()
    assert label.in_dom_text is False
    assert label.evidence_sources() == {'style'}


def test_label_carries_counter_values_and_evidence_chain():
    result = build()
    label = next(label for label in result.labels
                 if label.node_id == 'a2')
    assert label.counters == {'chapter': 1, 'clause': 2}
    properties = {item.name for item in label.evidence}
    assert 'content' in properties
    assert 'counter-reset' in properties
    assert 'counter-increment' in properties
    content_evidence = next(item for item in label.evidence
                            if item.name == 'content')
    assert content_evidence.selector_text == '.clause::before'
    assert content_evidence.source_line == CSS[:CSS.index(
        '.clause::before')].count('\n') + 1


def test_anchor_back_links_to_tree_node():
    result = build()
    label = result.labels[0]
    anchor_path = label.anchor.split('::')[0]
    assert result.document.find_path(anchor_path) is not None
    assert label.anchor.endswith('::before')


def test_artifact_bundle_is_json_serializable():
    result = build()
    bundle = result.to_dict()
    assert 'node_tree' in bundle
    assert 'tinycss2_ast' in bundle
    assert 'matched_declarations' in bundle
    assert 'labels' in bundle
    encoded = json.dumps(bundle, ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded['labels'][0]['evidence_sources'] == ['style']
    assert decoded['node_tree']['tag'] == 'html'


def test_serialized_tree_keeps_paths_and_text_nodes():
    result = build()
    tree = serialize_tree(result.document.root)

    def first_element(node):
        return next(child for child in node['children']
                    if child.get('tag'))

    section = first_element(first_element(tree))
    assert section['tag'] == 'section'
    clause = first_element(section)
    assert clause['path'].endswith('/article[1]')
    paragraph = first_element(clause)
    text_leaf = next(child for child in paragraph['children']
                     if child.get('node') == '#text')
    assert text_leaf['data'] == '正文不含编号'


def test_matched_declarations_dump_groups_by_anchor():
    result = build()
    dump = result.to_dict()['matched_declarations']
    before_key = next(
        key for key in dump if key.endswith('article[1]::before'))
    assert any(item['property'] == 'content' for item in dump[before_key])
    element_key = '/html/body/section/article[1]'
    assert any(item['property'] == 'counter-increment'
               for item in dump[element_key])


def test_read_points_record_state_for_every_pseudo():
    result = build()
    anchors = [point['anchor'] for point in result.read_points]
    assert '/html/body/section/article[1]::before' in anchors
    assert '/html/body/section/article[1]::after' in anchors
    before = next(point for point in result.read_points
                  if point['anchor'].endswith('article[2]::before'))
    assert before['counters']['clause'] == [2]
