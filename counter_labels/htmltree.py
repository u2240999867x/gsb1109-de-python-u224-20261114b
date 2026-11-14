"""Offline HTML node tree.

The tree is built with the standard library :mod:`html.parser` (no browser,
no network) and keeps only what the labeling engine needs:

* parent / child links and document-order identifiers,
* stable CSS-like paths for back-linking,
* source line/column and the ``id`` attribute,
* the text actually present in the document (generated markers are *not*
  part of it, which is precisely why they must be reconstructed).
"""

from html.parser import HTMLParser

VOID_ELEMENTS = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
    'meta', 'param', 'source', 'track', 'wbr',
})


class Node:
    """Base class for offline tree nodes."""

    def __init__(self, parent=None):
        self.parent = parent
        self.doc_id = -1
        self.source_line = None
        self.source_column = None

    @property
    def path(self):
        raise NotImplementedError


class TextNode(Node):
    """A text node. Generated CSS content never appears here."""

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = data

    @property
    def path(self):
        index = self.parent.children.index(self)
        return self.parent.path + '/text()[%d]' % (index + 1)

    def __repr__(self):
        snippet = self.data if len(self.data) <= 20 else self.data[:17] + '...'
        return 'TextNode(%r)' % snippet


class Element(Node):
    """An element node with attributes, children and bookkeeping data."""

    def __init__(self, tag, attrs=None, parent=None):
        super().__init__(parent)
        self.tag = tag.lower()
        self.attrs = dict(attrs or {})
        self.children = []

    @property
    def id(self):
        return self.attrs.get('id')

    @property
    def classes(self):
        return self.attrs.get('class', '').split()

    def get_attribute(self, name):
        return self.attrs.get(name)

    @property
    def path(self):
        if self.parent is None or not isinstance(self.parent, Element):
            return '/' + self.tag
        same_tag = [
            sibling for sibling in self.parent.children
            if isinstance(sibling, Element) and sibling.tag == self.tag
        ]
        if len(same_tag) == 1:
            return self.parent.path + '/' + self.tag
        position = same_tag.index(self) + 1
        return self.parent.path + '/%s[%d]' % (self.tag, position)

    def iter(self, include_text=False):
        """Yield descendant nodes in document order, then ``self`` first."""
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.iter(include_text)
            elif include_text:
                yield child

    def text_content(self):
        chunks = []
        for child in self.children:
            if isinstance(child, TextNode):
                chunks.append(child.data)
            else:
                chunks.append(child.text_content())
        return ''.join(chunks)

    def __repr__(self):
        return 'Element(%r, id=%r, doc_id=%d)' % (
            self.tag, self.id, self.doc_id)


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = None
        self._stack = []
        self._counter = 0

    def _register(self, node):
        node.doc_id = self._counter
        self._counter += 1
        line, column = self.getpos()
        node.source_line = line
        node.source_column = column + 1
        return node

    def handle_starttag(self, tag, attrs):
        parent = self._stack[-1] if self._stack else None
        element = self._register(Element(tag, attrs, parent))
        if parent is None:
            self.root = element
        else:
            parent.children.append(element)
        if tag not in VOID_ELEMENTS:
            self._stack.append(element)

    def handle_startendtag(self, tag, attrs):
        parent = self._stack[-1] if self._stack else None
        element = self._register(Element(tag, attrs, parent))
        if parent is None:
            self.root = element
        else:
            parent.children.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag.lower():
                del self._stack[index:]
                return

    def handle_data(self, data):
        if not self._stack or not data:
            return
        parent = self._stack[-1]
        node = TextNode(data, parent)
        node.doc_id = self._counter
        self._counter += 1
        parent.children.append(node)


class Document:
    """Parsed offline document: node lookup plus traversal helpers."""

    def __init__(self, root, node_count):
        self.root = root
        self.node_count = node_count

    def elements(self):
        yield from self.root.iter()

    def find_id(self, element_id):
        for element in self.elements():
            if element.id == element_id:
                return element
        return None

    def find_path(self, path):
        if path == self.root.path:
            return self.root
        for element in self.elements():
            if element.path == path:
                return element
        return None

    def to_source(self):
        chunks = []
        self._serialize(self.root, chunks)
        return ''.join(chunks)

    def _serialize(self, element, chunks):
        attrs = ''.join(
            ' %s="%s"' % (name, value)
            for name, value in sorted(element.attrs.items()))
        chunks.append('<%s%s>' % (element.tag, attrs))
        for child in element.children:
            if isinstance(child, TextNode):
                chunks.append(child.data)
            else:
                self._serialize(child, chunks)
        if element.tag not in VOID_ELEMENTS:
            chunks.append('</%s>' % element.tag)


def parse_html(markup):
    """Parse an HTML string into an offline :class:`Document`.

    Markup fragments are wrapped in ``<html><body>`` so every document has
    the same root shape and stable paths.
    """
    stripped = markup.lstrip()
    if not stripped.lower().startswith('<!doctype') and \
            not stripped.lower().startswith('<html'):
        markup = '<html><body>%s</body></html>' % markup
    builder = _TreeBuilder()
    builder.feed(markup)
    builder.close()
    if builder.root is None:
        raise ValueError('No element found in HTML input')
    return Document(builder.root, builder._counter)
