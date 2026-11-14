"""tinycss2-backed CSS model: parsed rules, matching and declarations.

The stylesheet is parsed with :func:`tinycss2.parse_stylesheet`.  The raw
tinycss2 AST is kept (it is the "tinycss2 parsing result" artifact) and a
small selector engine matches the subset of selectors typically used to lay
out legal documents (type, class, id, universal, attribute, ``>`` /
descendant combinators, ``:first-child``, ``:not()`` and the
``::before`` / ``::after`` pseudo-elements).
"""

import tinycss2
from tinycss2.ast import AtRule, Declaration, QualifiedRule

from .htmltree import Element

PSEUDO_ELEMENTS = {'before', 'after'}


class CounterOperation:
    """One entry of ``counter-reset`` / ``counter-set`` / ``counter-increment``."""

    __slots__ = ('name', 'value', 'reversed')

    def __init__(self, name, value=None, reversed=False):
        self.name = name
        self.value = value
        self.reversed = reversed

    def __eq__(self, other):
        if not isinstance(other, CounterOperation):
            return NotImplemented
        return (self.name == other.name and self.value == other.value
                and self.reversed == other.reversed)

    def __hash__(self):
        return hash((self.name, self.value, self.reversed))

    def __repr__(self):
        suffix = ' reversed' if self.reversed else ''
        return 'CounterOperation(%r, %r%s)' % (
            self.name, self.value, suffix)


class CounterRef:
    """A ``counter()`` / ``counters()`` reference inside ``content``."""

    __slots__ = ('name', 'style', 'separator', 'nested')

    def __init__(self, name, style='decimal', separator=None, nested=False):
        self.name = name
        self.style = style
        self.separator = separator
        self.nested = nested

    def __repr__(self):
        kind = 'counters' if self.nested else 'counter'
        return '%s(%r, %r)' % (kind, self.name, self.style)


class AttrRef:
    """An ``attr()`` reference inside ``content``."""

    __slots__ = ('name',)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return 'attr(%r)' % self.name


class MatchedDeclaration:
    """A declaration that matched a node, with full provenance."""

    __slots__ = ('rule', 'selector_text', 'specificity', 'pseudo',
                 'declaration', 'order')

    def __init__(self, rule, selector_text, specificity, pseudo,
                 declaration, order):
        self.rule = rule
        self.selector_text = selector_text
        self.specificity = specificity
        self.pseudo = pseudo
        self.declaration = declaration
        self.order = order

    @property
    def name(self):
        return self.declaration.lower_name

    @property
    def important(self):
        return self.declaration.important

    @property
    def source_line(self):
        return self.declaration.source_line

    def __repr__(self):
        return 'MatchedDeclaration(%s on %s%s, line %d)' % (
            self.name, self.selector_text,
            '::' + self.pseudo if self.pseudo else '', self.source_line)


class StyleRule:
    """A qualified rule flattened out of nested at-rules such as ``@media``."""

    __slots__ = ('selector_text', 'selectors', 'declarations', 'source_line',
                 'source_column', 'media_chain', 'raw_rule', 'order')

    def __init__(self, raw_rule, media_chain, order):
        self.raw_rule = raw_rule
        self.media_chain = media_chain
        self.order = order
        self.source_line = raw_rule.source_line
        self.source_column = raw_rule.source_column
        self.selector_text = tinycss2.serialize(raw_rule.prelude).strip()
        self.selectors = list(split_selector_groups(raw_rule.prelude))
        self.declarations = [
            node for node in tinycss2.parse_blocks_contents(
                raw_rule.content, skip_comments=True, skip_whitespace=True)
            if isinstance(node, Declaration)
        ]

    def __repr__(self):
        return 'StyleRule(%r, %d declarations)' % (
            self.selector_text, len(self.declarations))


def split_selector_groups(prelude):
    """Split a rule prelude on top-level commas."""
    group = []
    depth = 0
    for token in prelude:
        if token.type == 'literal' and token.value == ',' and depth == 0:
            text = tinycss2.serialize(group).strip()
            if text:
                yield text
            group = []
            continue
        if token.type.endswith(' block') or token.type == 'function':
            depth += 1
        group.append(token)
    text = tinycss2.serialize(group).strip()
    if text:
        yield text


class Stylesheet:
    """Parsed stylesheet: raw tinycss2 AST plus flattened style rules."""

    def __init__(self, css_text):
        self.source = css_text
        self.ast = tinycss2.parse_stylesheet(
            css_text, skip_comments=True, skip_whitespace=True)
        self.errors = [node for node in self.ast if node.type == 'error']
        self.rules = []
        self._collect_rules(self.ast, [])

    def _collect_rules(self, nodes, media_chain):
        order = len(self.rules)
        for node in nodes:
            if isinstance(node, QualifiedRule):
                self.rules.append(StyleRule(node, list(media_chain), order))
                order += 1
            elif isinstance(node, AtRule) and node.content is not None:
                children = tinycss2.parse_blocks_contents(
                    node.content, skip_comments=True, skip_whitespace=True)
                chain = media_chain + [
                    tinycss2.serialize(node.prelude).strip() or
                    node.at_keyword]
                order = self._collect_rules(children, chain)
        return order

    def matched_declarations(self, element, pseudo=None):
        """All declarations matching ``element`` (optionally a pseudo-element)."""
        matches = []
        for rule in self.rules:
            for selector_text in rule.selectors:
                parsed = parse_selector(selector_text)
                if parsed is None:
                    continue
                selector_pseudo = parsed[-1][1]['pseudo']
                if selector_pseudo != pseudo:
                    continue
                specificity, matched = _match_chain(parsed, element)
                if not matched:
                    continue
                for declaration in rule.declarations:
                    matches.append(MatchedDeclaration(
                        rule, selector_text, specificity, pseudo,
                        declaration, rule.order))
        return matches

    def effective_style(self, element, pseudo=None):
        """Cascade declarations into a ``{property: MatchedDeclaration}`` map."""
        matched = self.matched_declarations(element, pseudo)
        ordered = sorted(
            matched,
            key=lambda item: (
                item.important, item.specificity, item.order,
                item.declaration.source_line, item.declaration.source_column))
        return {item.name: item for item in ordered}


# ---------------------------------------------------------------------------
# Selector parsing and matching
# ---------------------------------------------------------------------------

def parse_selector(text):
    """Parse a complex selector into ``[(combinator, conditions), ...]``.

    Supported grammar (enough for regulation stylesheets): ``tag``, ``*``,
    ``#id``, ``.class``, ``[attr...]`` compound sequences joined by a space
    or ``>``, plus ``:first-child``, ``:not(...)`` and the
    ``::before`` / ``::after`` pseudo-elements.  Returns ``None`` on
    unsupported syntax.
    """
    tokens = tinycss2.parse_component_value_list(text, skip_comments=True)
    chain = []
    current = _empty_compound()
    combinator = None
    pending_combinator = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == 'whitespace':
            if (_compound_started(current) or chain) and \
                    pending_combinator is None:
                pending_combinator = ' '
            index += 1
            continue
        if token.type == 'literal' and token.value == '>':
            pending_combinator = '>'
            index += 1
            continue
        if pending_combinator is not None and _compound_started(current):
            chain.append((combinator, current))
            combinator = pending_combinator
            pending_combinator = None
            current = _empty_compound()
        next_index = _consume_compound_token(tokens, index, current)
        if next_index is None:
            return None
        index = next_index
    chain.append((combinator, current))
    return chain


def _empty_compound():
    return {'classes': [], 'ids': [], 'attrs': [], 'tag': None,
            'universal': False, 'pseudo': None, 'first_child': False,
            'not': []}


def _compound_started(conditions):
    return any((conditions['tag'], conditions['classes'], conditions['ids'],
                conditions['attrs'], conditions['pseudo'], conditions['not'],
                conditions['first_child'])) or conditions['universal']


def _consume_compound_token(tokens, index, current):
    token = tokens[index]
    if token.type == 'ident':
        current['tag'] = token.lower_value
        return index + 1
    if token.type == 'literal' and token.value == '*':
        current['universal'] = True
        return index + 1
    if token.type == 'hash' and token.is_identifier:
        current['ids'].append(token.value)
        return index + 1
    if token.type == '[] block':
        attr = _parse_attr_block(token.content)
        if attr is None:
            return None
        current['attrs'].append(attr)
        return index + 1
    if token.type == 'function':
        if token.lower_name == 'not':
            inner = tinycss2.serialize(token.arguments).strip()
            inner_parsed = parse_selector(inner)
            if not inner_parsed:
                return None
            current['not'].append(inner_parsed)
            return index + 1
        return None
    if token.type == 'literal' and token.value == '.':
        next_token = tokens[index + 1] if index + 1 < len(tokens) else None
        if next_token is None or next_token.type != 'ident':
            return None
        current['classes'].append(next_token.value)
        return index + 2
    if token.type == 'literal' and token.value == ':':
        return _consume_pseudo(tokens, index, current)
    return None


def _consume_pseudo(tokens, index, current):
    next_token = tokens[index + 1] if index + 1 < len(tokens) else None
    if next_token is None:
        return None
    if next_token.type == 'function' and next_token.lower_name == 'not':
        inner = tinycss2.serialize(next_token.arguments).strip()
        inner_parsed = parse_selector(inner)
        if not inner_parsed:
            return None
        current['not'].append(inner_parsed)
        return index + 2
    if next_token.type == 'literal' and next_token.value == ':':
        name_token = tokens[index + 2] if index + 2 < len(tokens) else None
        if name_token is None or name_token.type != 'ident':
            return None
        if name_token.lower_value not in PSEUDO_ELEMENTS:
            return None
        current['pseudo'] = name_token.lower_value
        return index + 3
    if next_token.type == 'ident':
        if next_token.lower_value in PSEUDO_ELEMENTS:
            current['pseudo'] = next_token.lower_value
            return index + 2
        if next_token.lower_value == 'first-child':
            current['first_child'] = True
            return index + 2
    return None


def _parse_attr_block(content):
    tokens = [token for token in content if token.type != 'whitespace']
    if not tokens or tokens[0].type != 'ident':
        return None
    name = tokens[0].lower_value
    if len(tokens) == 1:
        return (name, None, None)
    if len(tokens) >= 3 and tokens[1].type == 'literal':
        operator = tokens[1].value
        value_token = tokens[2]
        if value_token.type == 'ident':
            value = value_token.value
        elif value_token.type == 'string':
            value = value_token.value
        else:
            return None
        return (name, operator, value)
    return None


def _match_chain(parsed, element):
    specificity = [0, 0, 0]
    node = element
    index = len(parsed) - 1
    while index >= 0:
        combinator, conditions = parsed[index]
        if not _match_compound(conditions, node, specificity):
            return (0, 0, 0), False
        index -= 1
        if index < 0:
            break
        if combinator == '>':
            node = node.parent
            if not isinstance(node, Element):
                return (0, 0, 0), False
        else:
            node = _nearest_matching_ancestor(node, parsed[index][1])
            if node is None:
                return (0, 0, 0), False
    return tuple(specificity), True


def _nearest_matching_ancestor(node, conditions):
    ancestor = node.parent
    while isinstance(ancestor, Element):
        if _match_compound(conditions, ancestor, [0, 0, 0]):
            return ancestor
        ancestor = ancestor.parent
    return None


def _match_compound(conditions, node, specificity):
    if not isinstance(node, Element):
        return False
    if conditions['tag'] is not None:
        if node.tag != conditions['tag']:
            return False
        specificity[2] += 1
    for class_name in conditions['classes']:
        if class_name not in node.classes:
            return False
        specificity[1] += 1
    for element_id in conditions['ids']:
        if node.id != element_id:
            return False
        specificity[0] += 1
    for name, operator, value in conditions['attrs']:
        if not _match_attr(node, name, operator, value):
            return False
        specificity[1] += 1
    if conditions['first_child']:
        if not _is_first_element_child(node):
            return False
        specificity[1] += 1
    if conditions['pseudo'] is not None:
        specificity[1] += 1
    for inner in conditions['not']:
        _, matched = _match_chain(inner, node)
        if matched:
            return False
        specificity[1] += 1
    return True


def _match_attr(node, name, operator, value):
    actual = node.attrs.get(name)
    if operator is None:
        return actual is not None
    if actual is None:
        return False
    if operator == '=':
        return actual == value
    if operator == '~=':
        return value in actual.split()
    if operator == '|=':
        return actual == value or actual.startswith(value + '-')
    if operator == '^=':
        return actual.startswith(value)
    if operator == '$=':
        return actual.endswith(value)
    if operator == '*=':
        return value in actual
    return False


def _is_first_element_child(node):
    parent = node.parent
    if not isinstance(parent, Element):
        return False
    for child in parent.children:
        if isinstance(child, Element):
            return child is node
    return False


# ---------------------------------------------------------------------------
# Counter and content property parsing
# ---------------------------------------------------------------------------

def parse_counter_property(declaration):
    """Parse a counter property into a list of :class:`CounterOperation`.

    Returns ``[]`` for ``none`` and ``None`` for invalid syntax.
    """
    tokens = [token for token in declaration.value
              if token.type != 'whitespace']
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0].type == 'ident' and \
            tokens[0].lower_value == 'none':
        return []
    operations = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        reversed_flag = False
        inner_value = None
        if token.type == 'function' and token.lower_name == 'reversed':
            reversed_flag = True
            inner = [item for item in token.arguments
                     if item.type != 'whitespace']
            inner_value = None
            if len(inner) == 1 and inner[0].type == 'ident':
                name = inner[0].value
            elif len(inner) == 2 and inner[0].type == 'ident' and \
                    inner[1].type == 'number' and inner[1].is_integer:
                name = inner[0].value
                inner_value = inner[1].int_value
            elif not inner:
                name = 'list-item'
            else:
                return None
            index += 1
        elif token.type == 'ident':
            name = token.value
            index += 1
        else:
            return None
        value = inner_value
        if value is None and index < len(tokens) and \
                tokens[index].type == 'number' and \
                tokens[index].is_integer:
            value = tokens[index].int_value
            index += 1
        operations.append(CounterOperation(name, value, reversed_flag))
    return operations


def parse_content(declaration):
    """Parse ``content`` into ``(kind, payload)`` items.

    ``kind`` is ``text``, ``counter``, ``attr`` or ``quote``.  Returns
    ``None`` for ``content: none``.
    """
    tokens = [token for token in declaration.value
              if token.type != 'whitespace']
    if len(tokens) == 1 and tokens[0].type == 'ident' and \
            tokens[0].lower_value == 'none':
        return None
    items = []
    for token in tokens:
        if token.type == 'string':
            items.append(('text', token.value))
        elif token.type == 'function' and token.lower_name in (
                'counter', 'counters'):
            ref = _parse_counter_function(token)
            if ref is None:
                return None
            items.append(('counter', ref))
        elif token.type == 'function' and token.lower_name == 'attr':
            inner = [item for item in token.arguments
                     if item.type != 'whitespace']
            if len(inner) != 1 or inner[0].type != 'ident':
                return None
            items.append(('attr', AttrRef(inner[0].value)))
        elif token.type == 'ident' and token.lower_value in (
                'open-quote', 'close-quote', 'no-open-quote',
                'no-close-quote'):
            items.append(('quote', token.lower_value))
        else:
            items.append(('text', tinycss2.serialize([token])))
    return items


def _parse_counter_function(token):
    args = [item for item in token.arguments
            if not (item.type == 'whitespace' or
                    (item.type == 'literal' and item.value == ','))]
    if not args or args[0].type != 'ident':
        return None
    name = args[0].value
    if token.lower_name == 'counter':
        style = 'decimal'
        if len(args) == 2:
            if args[1].type != 'ident':
                return None
            style = args[1].lower_value
        elif len(args) > 2:
            return None
        return CounterRef(name, style)
    # counters(<counter> , <counter-style>? ) with a mandatory separator.
    rest = [item for item in token.arguments
            if not (item.type == 'whitespace' or
                    (item.type == 'literal' and item.value == ','))]
    if len(rest) < 2 or rest[1].type != 'string':
        return None
    separator = rest[1].value
    style = 'decimal'
    if len(rest) == 3:
        if rest[2].type != 'ident':
            return None
        style = rest[2].lower_value
    elif len(rest) > 3:
        return None
    return CounterRef(name, style, separator=separator, nested=True)
