"""End-to-end test of the regulation page scenario."""

import pathlib

from counter_labels import Labeler, Stylesheet, diff_labelings, parse_html

HERE = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = HERE / 'examples' / 'regulation_page'


def load():
    document = parse_html(
        (EXAMPLE / 'regulation.html').read_text(encoding='utf-8'))
    stylesheet = Stylesheet(
        (EXAMPLE / 'regulation.css').read_text(encoding='utf-8'))
    return document, stylesheet, Labeler(document, stylesheet).run()


def label_map(result):
    return {(label.node_id, label.pseudo): label for label in result.labels}


def test_generated_clause_labels_are_absent_from_dom_text():
    document, _, result = load()
    labels = label_map(result)
    assert labels[('art-3', 'before')].text.startswith('第 1.3 条')
    assert labels[('art-3', 'before')].in_dom_text is False
    article = document.find_id('art-3')
    assert '第 1.3 条' not in article.text_content()
    assert labels[('art-3', 'before')].evidence_sources() == {'style'}


def test_outer_counter_continues_in_next_chapter():
    _, _, result = load()
    labels = label_map(result)
    assert labels[('art-1', 'before')].text.startswith('第 1.1 条')
    assert labels[('art-4', 'before')].text.startswith('第 2.5 条')
    assert labels[('art-5', 'before')].text.startswith('第 2.6 条')


def test_reversed_lists_explicit_and_default_starts():
    _, _, result = load()
    list_markers = {}
    for label in result.labels:
        if label.tag != 'li':
            continue
        ol_path = label.node_path.rsplit('/li', 1)[0]
        list_markers.setdefault(ol_path, []).append(label.text)
    explicit = next(values for path, values in list_markers.items()
                    if path.endswith('ol[1]'))
    default = next(values for path, values in list_markers.items()
                   if path.endswith('ol[2]'))
    assert explicit == ['(c) ', '(b) ', '(a) ']
    assert default == ['(d) ', '(c) ', '(b) ', '(a) ']


def test_before_and_after_pseudos_observe_note_counter_timing():
    _, _, result = load()
    labels = label_map(result)
    # ::before is read before the child .note increments, ::after after them.
    assert 'note=0' in labels[('art-2', 'before')].text
    assert 'note=2' in labels[('art-2', 'after')].text
    assert 'note=2' in labels[('art-3', 'before')].text


def test_anchors_back_link_into_tree():
    document, _, result = load()
    for label in result.labels:
        node_path = label.anchor.split('::')[0]
        assert document.find_path(node_path) is not None


def test_editing_one_subclause_marks_downstream_style_only_changes():
    document, stylesheet, before = load()
    html = (EXAMPLE / 'regulation.html').read_text(encoding='utf-8')
    edited = parse_html(html.replace(
        '        <li>无初值子款四</li>\n', ''))
    after = Labeler(edited, stylesheet).run()
    diff = diff_labelings(before, after)

    changed_identities = [
        change.identity_key for change in diff.changed]
    assert len(changed_identities) == 3
    assert all(change.causes == ['counter-value']
               for change in diff.changed)
    assert all(change.style_only for change in diff.changed)
    assert len(diff.removed) == 1
    assert diff.removed[0].identity_key.endswith('无初值子款四::before')

    unaffected = label_map(before)[('art-3', 'before')]
    assert unaffected.anchor in diff.unchanged
