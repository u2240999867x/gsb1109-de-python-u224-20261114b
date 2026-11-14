"""Compare two labelings to see which downstream labels moved.

A sub-clause edit (adding or removing an element, or changing a counter
declaration) typically renumbers every later marker even though the
document text of those later nodes did not change.  Positional paths shift
when elements are inserted or removed, so labels are aligned with an LCS
over position-independent identity keys (element id, or tag + text), then
classified as unchanged / changed / added / removed.
"""

from dataclasses import dataclass, field


@dataclass
class LabelChange:
    anchor: str
    kind: str
    old_text: str = None
    new_text: str = None
    old_counters: dict = field(default_factory=dict)
    new_counters: dict = field(default_factory=dict)
    causes: list = field(default_factory=list)
    style_only: bool = True
    node_doc_id: int = -1
    identity_key: str = ''

    def to_dict(self):
        return {
            'anchor': self.anchor,
            'identity_key': self.identity_key,
            'kind': self.kind,
            'old_text': self.old_text,
            'new_text': self.new_text,
            'old_counters': self.old_counters,
            'new_counters': self.new_counters,
            'causes': self.causes,
            'style_only': self.style_only,
        }


@dataclass
class LabelingDiff:
    unchanged: list
    changed: list
    added: list
    removed: list
    _after: object = None

    def downstream_of(self, edited_anchor):
        """Changed labels whose nodes follow ``edited_anchor`` in the tree."""
        edited_path = edited_anchor.split('::')[0]
        edited = self._after.document.find_path(edited_path)
        if edited is None:
            return []
        edited_id = edited.doc_id
        return [change for change in self.changed
                if change.node_doc_id > edited_id]

    def style_evidence_only(self):
        """Changed labels whose marker text never appears in DOM text."""
        return [change for change in self.changed if change.style_only]

    def to_dict(self):
        return {
            'unchanged': self.unchanged,
            'changed': [change.to_dict() for change in self.changed],
            'added': [change.to_dict() for change in self.added],
            'removed': [change.to_dict() for change in self.removed],
        }


def _evidence_keys(label):
    if label is None:
        return frozenset()
    return frozenset(
        (item.name, item.declaration.serialize(), item.selector_text,
         item.source_line)
        for item in label.evidence)


def _align(before_labels, after_labels):
    """LCS alignment on identity keys; returns paired plus only-on-side lists."""
    before_keys = [label.identity_key for label in before_labels]
    after_keys = [label.identity_key for label in after_labels]
    rows = len(before_labels)
    cols = len(after_labels)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for row in range(rows - 1, -1, -1):
        for col in range(cols - 1, -1, -1):
            if before_keys[row] == after_keys[col]:
                table[row][col] = table[row + 1][col + 1] + 1
            else:
                table[row][col] = max(
                    table[row + 1][col], table[row][col + 1])
    paired = []
    removed = []
    added = []
    row = col = 0
    while row < rows and col < cols:
        if before_keys[row] == after_keys[col]:
            paired.append((before_labels[row], after_labels[col]))
            row += 1
            col += 1
        elif table[row + 1][col] >= table[row][col + 1]:
            removed.append(before_labels[row])
            row += 1
        else:
            added.append(after_labels[col])
            col += 1
    removed.extend(before_labels[row:])
    added.extend(after_labels[col:])
    return paired, removed, added


def diff_labelings(before, after):
    """Diff two :class:`~counter_labels.labeling.LabelingResult` objects."""
    paired, removed_labels, added_labels = _align(
        before.labels, after.labels)
    unchanged = []
    changed = []

    for old, new in paired:
        if old.text == new.text and old.counters == new.counters and \
                _evidence_keys(old) == _evidence_keys(new):
            unchanged.append(new.anchor)
            continue
        changed.append(LabelChange(
            anchor=new.anchor,
            kind='changed',
            old_text=old.text,
            new_text=new.text,
            old_counters=dict(old.counters),
            new_counters=dict(new.counters),
            causes=_causes(old, new),
            style_only=not new.in_dom_text,
            node_doc_id=new.node_doc_id,
            identity_key=new.identity_key))

    added = [
        LabelChange(
            anchor=label.anchor, kind='added', new_text=label.text,
            new_counters=dict(label.counters),
            style_only=not label.in_dom_text,
            node_doc_id=label.node_doc_id,
            identity_key=label.identity_key)
        for label in added_labels]
    removed = [
        LabelChange(
            anchor=label.anchor, kind='removed', old_text=label.text,
            old_counters=dict(label.counters),
            style_only=not label.in_dom_text,
            node_doc_id=label.node_doc_id,
            identity_key=label.identity_key)
        for label in removed_labels]
    return LabelingDiff(unchanged, changed, added, removed, after)


def _causes(old, new):
    causes = []
    if old.counters != new.counters:
        causes.append('counter-value')
    if _evidence_keys(old) != _evidence_keys(new):
        causes.append('style-evidence')
    if old.in_dom_text != new.in_dom_text or (
            old.in_dom_text and new.in_dom_text and old.text != new.text
            and old.counters == new.counters):
        causes.append('dom-text')
    if not causes:
        causes.append('rendered-text')
    return causes
