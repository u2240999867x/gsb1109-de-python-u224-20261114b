"""Assemble back-linkable clause labels from the tree, styles and counters."""

from dataclasses import dataclass, field

from .counters import (
    COUNTER_PROPERTIES, CounterEngine, format_counter)
from .cssmodel import parse_content
from .htmltree import Element


@dataclass
class Label:
    """One generated marker (e.g. the text rendered by ``::before``)."""

    text: str
    node_path: str
    node_id: object
    tag: str
    pseudo: str
    source_line: int
    source_column: int
    counters: dict
    evidence: list = field(default_factory=list)
    in_dom_text: bool = False
    node_doc_id: int = -1
    node_signature: str = ''

    @property
    def anchor(self):
        """Stable back-link target: node path plus pseudo-element."""
        return '%s::%s' % (self.node_path, self.pseudo)

    @property
    def identity_key(self):
        """Position-independent node identity used when diffing edits."""
        return '%s::%s' % (self.node_signature, self.pseudo)

    def evidence_sources(self):
        """``{'style'}`` when the marker exists only in CSS-generated content."""
        return {'style'} if not self.in_dom_text else {'style', 'text'}

    def to_dict(self):
        return {
            'text': self.text,
            'anchor': self.anchor,
            'node_id': self.node_id,
            'tag': self.tag,
            'pseudo': self.pseudo,
            'source': {'line': self.source_line,
                       'column': self.source_column},
            'node_doc_id': self.node_doc_id,
            'identity_key': self.identity_key,
            'counters': dict(self.counters),
            'in_dom_text': self.in_dom_text,
            'evidence_sources': sorted(self.evidence_sources()),
            'evidence': [
                {
                    'property': item.name,
                    'css_text': item.declaration.serialize(),
                    'selector': item.selector_text,
                    'pseudo': item.pseudo,
                    'css_line': item.source_line,
                    'specificity': list(item.specificity),
                    'important': item.important,
                }
                for item in self.evidence
            ],
        }


@dataclass
class LabelingResult:
    """Full offline artifact bundle for one document + stylesheet pair."""

    document: object
    stylesheet: object
    labels: list
    read_points: list

    def label_map(self):
        return {label.anchor: label for label in self.labels}

    def to_dict(self):
        return {
            'node_tree': serialize_tree(self.document.root),
            'tinycss2_ast': [
                node.serialize() for node in self.stylesheet.ast],
            'matched_declarations': self._matched_declarations_dump(),
            'labels': [label.to_dict() for label in self.labels],
        }

    def _matched_declarations_dump(self):
        dump = {}
        for element in self.document.elements():
            for pseudo in (None, 'before', 'after'):
                matched = self.stylesheet.matched_declarations(
                    element, pseudo)
                if not matched:
                    continue
                key = element.path + ('::' + pseudo if pseudo else '')
                dump[key] = [
                    {
                        'property': item.name,
                        'css_text': item.declaration.serialize(),
                        'selector': item.selector_text,
                        'css_line': item.source_line,
                        'specificity': list(item.specificity),
                        'important': item.important,
                    }
                    for item in matched
                ]
        return dump


def serialize_tree(element, depth=0):
    """Offline node tree as nested, JSON-ready dictionaries."""
    node = {
        'tag': element.tag,
        'doc_id': element.doc_id,
        'path': element.path,
        'source': {'line': element.source_line,
                   'column': element.source_column},
        'attributes': dict(element.attrs),
        'children': [],
    }
    for child in element.children:
        if isinstance(child, Element):
            node['children'].append(serialize_tree(child, depth + 1))
        else:
            node['children'].append({
                'node': '#text',
                'doc_id': child.doc_id,
                'data': child.data,
            })
    return node


class Labeler:
    """Runs the counter engine and renders every generated marker."""

    QUOTES = {
        'open-quote': '“',
        'close-quote': '”',
        'no-open-quote': '',
        'no-close-quote': '',
    }

    def __init__(self, document, stylesheet):
        self.document = document
        self.stylesheet = stylesheet
        self.engine = CounterEngine(document, stylesheet)
        self.labels = []
        self.read_points = []

    def run(self):
        self.engine.run(self._read)
        return LabelingResult(
            self.document, self.stylesheet, self.labels, self.read_points)

    def _read(self, element, pseudo, state):
        style = self.stylesheet.effective_style(element, pseudo)
        self.read_points.append({
            'anchor': '%s::%s' % (element.path, pseudo),
            'counters': {
                name: [instance.value for instance in instances]
                for name, instances in sorted(state.items())},
        })
        content_match = style.get('content')
        if content_match is None:
            return
        items = parse_content(content_match.declaration)
        if not items:
            return
        text = self._render(items, element, state)
        counters = {
            ref.name: self.engine.counter_value(state, ref.name)
            for kind, payload in items if kind == 'counter'
            for ref in [payload]}
        label = Label(
            text=text,
            node_path=element.path,
            node_id=element.id,
            tag=element.tag,
            pseudo=pseudo,
            source_line=element.source_line,
            source_column=element.source_column,
            counters=counters,
            evidence=self._evidence(element, content_match, items, state),
            in_dom_text=text in element.text_content(),
            node_doc_id=element.doc_id,
            node_signature=self._node_signature(element))
        self.labels.append(label)

    @staticmethod
    def _node_signature(element):
        """Position-independent identity.

        Uses the element id when present, otherwise the tag plus the node's
        own direct text.  Ancestor chain entries use ids or bare tags so the
        signature stays stable when siblings elsewhere are inserted or
        removed.
        """
        parts = []
        node = element
        while isinstance(node, Element):
            if node is element:
                if node.id:
                    parts.append('#' + node.id)
                else:
                    direct_text = ''.join(
                        child.data for child in node.children
                        if not isinstance(child, Element))
                    parts.append('%s:%s' % (
                        node.tag, ''.join(direct_text.split())[:32]))
            elif node.id:
                parts.append('#' + node.id)
            else:
                parts.append(node.tag)
            node = node.parent
        return '/'.join(reversed(parts))

    def _render(self, items, element, state):
        chunks = []
        for kind, payload in items:
            if kind == 'text':
                chunks.append(payload)
            elif kind == 'attr':
                chunks.append(element.attrs.get(payload.name, ''))
            elif kind == 'quote':
                chunks.append(self.QUOTES[payload])
            else:
                if payload.nested:
                    values = self.engine.counter_values(
                        state, payload.name)
                    chunks.append(payload.separator.join(
                        format_counter(value, payload.style)
                        for value in values))
                else:
                    value = self.engine.counter_value(state, payload.name)
                    chunks.append(format_counter(value, payload.style))
        return ''.join(chunks)

    def _evidence(self, element, content_match, items, state):
        evidence = [content_match]
        counter_names = {
            payload.name for kind, payload in items
            if kind == 'counter'}
        owner_ids = set()
        for name in counter_names:
            for instance in state.get(name, []):
                owner_ids.add(instance.owner_id)
        for owner_id in owner_ids:
            owner = self._find_by_doc_id(owner_id)
            if owner is None:
                continue
            for pseudo in (None, 'before', 'after'):
                for matched in self.stylesheet.matched_declarations(
                        owner, pseudo):
                    if matched.name in COUNTER_PROPERTIES and \
                            matched not in evidence:
                        evidence.append(matched)
        return evidence

    def _find_by_doc_id(self, doc_id):
        for element in self.document.elements():
            if element.doc_id == doc_id:
                return element
        return None
