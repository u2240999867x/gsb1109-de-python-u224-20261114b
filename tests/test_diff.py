"""Tests for before/after label diffs and evidence classification."""

from counter_labels import Labeler, Stylesheet, diff_labelings, parse_html

HTML = """
<html><body>
  <ol class="items" id="list">
    <li id="i1">甲</li>
    <li id="i2">乙</li>
    <li id="i3">丙</li>
  </ol>
</body></html>
"""

CSS = """
ol.items { counter-reset: item; }
ol.items > li { counter-increment: item; }
li::before { content: "第 " counter(item) " 款"; }
"""


def label(html=HTML, css=CSS):
    document = parse_html(html)
    stylesheet = Stylesheet(css)
    return Labeler(document, stylesheet).run()


def by_identity(changes):
    return {change.identity_key: change for change in changes}


def find(changes, suffix):
    return next(change for change in changes
                if change.identity_key.endswith(suffix))


def test_removing_subclause_renumbers_later_labels():
    before = label()
    edited_html = HTML.replace('    <li id="i2">乙</li>\n', '')
    after = label(edited_html)
    diff = diff_labelings(before, after)

    assert any(change.identity_key.endswith('#i2::before')
               for change in diff.removed)
    changed = by_identity(diff.changed)
    surviving = find(diff.changed, '#i3::before')
    assert surviving.old_text == '第 3 款'
    assert surviving.new_text == '第 2 款'
    assert 'counter-value' in surviving.causes
    assert any(anchor.endswith('li[1]::before') for anchor in diff.unchanged)


def test_changed_labels_are_style_evidence_only():
    before = label()
    edited_html = HTML.replace('    <li id="i2">乙</li>\n', '')
    after = label(edited_html)
    diff = diff_labelings(before, after)
    style_only = {change.identity_key for change in diff.style_evidence_only()}
    assert any(key.endswith('#i3::before') for key in style_only)
    for change in diff.changed:
        assert change.style_only is True


def test_css_only_edit_is_detected_as_style_evidence_change():
    before = label()
    after = label(css=CSS.replace(
        'counter-reset: item;', 'counter-reset: item 9;'))
    diff = diff_labelings(before, after)
    changed = by_identity(diff.changed)
    first = find(diff.changed, '#i1::before')
    assert first.old_text == '第 1 款'
    assert first.new_text == '第 10 款'
    assert 'style-evidence' in first.causes
    assert first.style_only is True


def test_no_changes_when_inputs_identical():
    diff = diff_labelings(label(), label())
    assert diff.changed == []
    assert diff.added == []
    assert diff.removed == []
    assert len(diff.unchanged) == 3


def test_added_element_generates_added_change():
    before = label()
    edited_html = HTML.replace(
        '  </ol>', '    <li id="i4">丁</li>\n  </ol>')
    after = label(edited_html)
    diff = diff_labelings(before, after)
    added = find(diff.added, '#i4::before')
    assert added.new_text == '第 4 款'


def test_downstream_helper_filters_by_document_order():
    before = label()
    edited_html = HTML.replace('    <li id="i2">乙</li>\n', '')
    after = label(edited_html)
    diff = diff_labelings(before, after)
    first = after.document.find_id('i1')
    downstream = diff.downstream_of(first.path)
    assert all(change.node_doc_id != first.doc_id for change in downstream)
    assert any(change.identity_key.endswith('#i3::before')
               for change in downstream)


def test_removed_and_added_keep_their_anchor_back_links():
    before = label()
    edited_html = HTML.replace('    <li id="i2">乙</li>\n', '')
    after = label(edited_html)
    diff = diff_labelings(before, after)
    removed = find(diff.removed, '#i2::before')
    assert removed.anchor.startswith('/html/body/ol/li')
    assert removed.anchor.endswith('::before')
    assert removed.old_text == '第 2 款'
