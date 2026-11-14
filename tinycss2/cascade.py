"""
CSS cascade: sort matched declarations and find the winning value.

This implements the cascade sorting order of CSS Cascading and Inheritance
Level 5, including cascade layers (named and anonymous), unlayered rules,
style origins and the reversed order of ``!important`` declarations.

tinycss2 itself does not know selectors or elements, so the caller provides
the specificity of every matched rule (for example computed with a selector
library). Everything else — origins, layers, importance and source order — is
handled here.
"""

from dataclasses import dataclass
from enum import Enum

from .ast import AtRule, Declaration, QualifiedRule
from .parser import parse_blocks_contents
from .serializer import serialize


class CascadeOrigin(Enum):
    """Origins a declaration can come from.

    Enum values follow the priority order of *normal* declarations: user
    agent style sheets lose to user style sheets, which lose to author style
    sheets. Important declarations use the reverse origin order.
    """

    USER_AGENT = 0
    USER = 1
    AUTHOR = 2


# Important origins sort in the opposite direction: author first (lowest
# priority), then user, then user agent (highest priority).
_IMPORTANT_ORIGIN_ORDER = {
    CascadeOrigin.AUTHOR: 0,
    CascadeOrigin.USER: 1,
    CascadeOrigin.USER_AGENT: 2,
}

# At-rules whose contents are not style declarations and must not be walked
# as conditional group rules.
_SKIPPED_AT_RULES = frozenset({
    'counter-style',
    'font-face',
    'font-feature-values',
    'keyframes',
    'property',
    'viewport',
})


class AnonymousLayer:
    """A cascade layer created by an ``@layer`` block without a name.

    Anonymous layers are ordered like named layers but can only be referred to
    by identity: two anonymous layers are never the same layer.
    """

    def __init__(self, index):
        self.index = index

    def __repr__(self):
        return f'AnonymousLayer(#{self.index})'


@dataclass(frozen=True, slots=True)
class CascadeEntry:
    """One matched declaration participating in the cascade.

    .. attribute:: value

        The serialized declaration value, with surrounding whitespace removed.

    .. attribute:: origin

        The :class:`CascadeOrigin` the declaration comes from.

    .. attribute:: specificity

        The selector specificity as a ``(a, b, c)`` tuple, ``(0, 0, 0)`` for
        declarations that do not come from a style rule (e.g. inline styles).

    .. attribute:: order

        The position of the declaration in source order, globally increasing
        across every style sheet added to a :class:`Cascade`.

    .. attribute:: layer

        The name of the cascade layer containing the declaration, an
        :class:`AnonymousLayer` instance for unnamed ``@layer`` blocks, or
        :obj:`None` for unlayered rules.

    .. attribute:: important

        Whether the declaration carries an ``!important`` marker.

    .. attribute:: property

        The ASCII-lower-cased property name, or :obj:`None` if unknown.

    .. attribute:: selector

        The serialized selector of the enclosing style rule, if any.

    .. attribute:: source_line

    .. attribute:: source_column

        The position of the declaration in the CSS source, when parsed with
        tinycss2.

    .. attribute:: declaration

        The original :class:`~tinycss2.ast.Declaration` node, if any.
    """

    value: str
    origin: CascadeOrigin
    specificity: tuple
    order: int
    layer: object = None
    important: bool = False
    property: str | None = None
    selector: str | None = None
    source_line: int | None = None
    source_column: int | None = None
    declaration: Declaration | None = None


def _layer_names(prelude):
    """Split an ``@layer`` prelude into dotted layer names on commas."""
    names = []
    current = []
    for token in prelude:
        if token.type == 'literal' and token.value == ',':
            if current:
                names.append('.'.join(current))
                current = []
        elif token.type == 'ident':
            current.append(token.value)
    if current:
        names.append('.'.join(current))
    return names


def _join_layers(parent, child):
    """Combine the layer names of nested ``@layer`` blocks."""
    if parent is None:
        return child
    if child is None:
        return parent
    if isinstance(parent, AnonymousLayer):
        # A named layer nested in an anonymous layer cannot be referenced by
        # its bare name, so keep the anonymous identity as the bucket.
        return parent
    if isinstance(child, AnonymousLayer):
        return child
    return f'{parent}.{child}'


class Cascade:
    """Collect matched declarations and resolve the winning value.

    Layers are ordered by their first appearance, which may be an
    ``@layer a, b;`` statement seen long before any rule actually declares a
    value in them. Declarations are kept in insertion order and only sorted
    when :meth:`entries` or :meth:`winner` is called, so moving unrelated
    rules around inside one layer never changes which layer wins.
    """

    def __init__(self):
        self._entries = []
        self._layer_index = {}
        self._anonymous_count = 0
        self._counter = 0

    @property
    def layers(self):
        """Layer names in cascade order, first declared layer first."""
        return tuple(
            sorted(self._layer_index, key=lambda key: self._layer_index[key]))

    def declare_layers(self, names):
        """Register layer names in order, as ``@layer a, b;`` does.

        Names already registered keep their existing position: layer order is
        fixed by the first mention of each name.
        """
        for name in names:
            self._register_layer(name)
        return self.layers

    def _register_layer(self, layer):
        if layer is not None and layer not in self._layer_index:
            self._layer_index[layer] = len(self._layer_index)

    def _next_order(self):
        order = self._counter
        self._counter += 1
        return order

    def add_entry(self, entry):
        """Add a pre-built :class:`CascadeEntry`."""
        self._register_layer(entry.layer)
        self._counter = max(self._counter, entry.order + 1)
        self._entries.append(entry)
        return entry

    def add(self, value, *, origin, specificity=(0, 0, 0), layer=None,
            important=False, property=None, selector=None, order=None,
            source_line=None, source_column=None, declaration=None):
        """Add one matched declaration and return its :class:`CascadeEntry`."""
        self._register_layer(layer)
        if order is None:
            order = self._next_order()
        else:
            self._counter = max(self._counter, order + 1)
        entry = CascadeEntry(
            value=value, origin=origin, specificity=tuple(specificity),
            order=order, layer=layer, important=important, property=property,
            selector=selector, source_line=source_line,
            source_column=source_column, declaration=declaration)
        self._entries.append(entry)
        return entry

    def add_declaration(self, declaration, origin, specificity, *,
                        layer=None, selector=None, order=None):
        """Add a :class:`~tinycss2.ast.Declaration` node."""
        return self.add(
            serialize(declaration.value).strip(), origin=origin,
            specificity=specificity, layer=layer,
            important=declaration.important,
            property=declaration.lower_name, selector=selector, order=order,
            source_line=declaration.source_line,
            source_column=declaration.source_column, declaration=declaration)

    def add_qualified_rule(self, rule, origin, specificity, *,
                           layer=None, property=None, selector=None):
        """Add every declaration of a matched :class:`~tinycss2.ast.QualifiedRule`.

        Nested qualified rules are ignored here; :meth:`add_rules` walks them.
        """
        if selector is None:
            selector = serialize(rule.prelude).strip()
        entries = []
        for node in parse_blocks_contents(
                rule.content, skip_comments=True, skip_whitespace=True):
            if not isinstance(node, Declaration):
                continue
            if property is not None and node.lower_name != property:
                continue
            entries.append(self.add_declaration(
                node, origin, specificity, layer=layer, selector=selector))
        return entries

    def add_rules(self, rules, origin, specificity, *, layer=None,
                  property=None):
        """Walk parsed rules and add declarations from matched style rules.

        :type rules: iterable of :class:`~tinycss2.ast.QualifiedRule` and
            :class:`~tinycss2.ast.AtRule` nodes, as returned by
            :func:`~tinycss2.parse_stylesheet`.
        :param origin: :class:`CascadeOrigin` of these rules.
        :param specificity: either a ``(a, b, c)`` tuple applied to every
            rule, or a callable taking ``(rule, selector_text)`` and returning
            a tuple or :obj:`None`. Returning :obj:`None` means the selector
            does not match and the rule is skipped.
        :param layer: layer currently in scope when walking a bare rule list.
        :param property: when given, only declarations of this ASCII-lower
            property name are collected.
        """
        entries = []
        for rule in rules:
            if isinstance(rule, QualifiedRule):
                selector = serialize(rule.prelude).strip()
                if callable(specificity):
                    weight = specificity(rule, selector)
                else:
                    weight = specificity
                if weight is None:
                    continue
                entries.extend(self.add_qualified_rule(
                    rule, origin, weight, layer=layer, property=property,
                    selector=selector))
                nested = [
                    node for node in parse_blocks_contents(
                        rule.content, skip_comments=True,
                        skip_whitespace=True)
                    if isinstance(node, QualifiedRule)]
                entries.extend(self.add_rules(
                    nested, origin, specificity, layer=layer,
                    property=property))
            elif isinstance(rule, AtRule):
                entries.extend(self._add_at_rule(
                    rule, origin, specificity, layer, property))
        return entries

    def _add_at_rule(self, rule, origin, specificity, layer, property):
        keyword = rule.lower_at_keyword
        if keyword == 'layer':
            names = _layer_names(rule.prelude)
            if rule.content is None:
                self.declare_layers(names)
                return []
            if names:
                child_layer = names[0]
            else:
                child_layer = AnonymousLayer(self._anonymous_count)
                self._anonymous_count += 1
            child_layer = _join_layers(layer, child_layer)
            return self._walk_content(
                rule.content, origin, specificity, child_layer, property)
        if rule.content is None or keyword in _SKIPPED_AT_RULES:
            return []
        return self._walk_content(
            rule.content, origin, specificity, layer, property)

    def _walk_content(self, content, origin, specificity, layer, property):
        nodes = parse_blocks_contents(
            content, skip_comments=True, skip_whitespace=True)
        return self.add_rules(
            nodes, origin, specificity, layer=layer, property=property)

    def _sort_key(self, entry):
        layer_count = len(self._layer_index)
        if entry.layer is None:
            # Unlayered normal rules beat every layered normal rule, while
            # unlayered important rules lose to every layered important rule.
            normal_rank = layer_count
            important_rank = -1
        else:
            index = self._layer_index[entry.layer]
            normal_rank = index
            important_rank = layer_count - 1 - index
        if entry.important:
            return (
                1, _IMPORTANT_ORIGIN_ORDER[entry.origin], important_rank,
                entry.specificity, entry.order)
        return (
            0, entry.origin.value, normal_rank,
            entry.specificity, entry.order)

    def entries(self, property=None):
        """Return all entries sorted by increasing cascade priority."""
        entries = self._entries
        if property is not None:
            entries = [
                entry for entry in entries if entry.property == property]
        return sorted(entries, key=self._sort_key)

    def winner(self, property=None):
        """Return the winning :class:`CascadeEntry`, or :obj:`None`."""
        entries = self.entries(property)
        if not entries:
            return None
        return entries[-1]

    def winning_value(self, property=None):
        """Return the serialized winning value, or :obj:`None`."""
        winner = self.winner(property)
        return None if winner is None else winner.value


def cascade(entries, *, layer_order=()):
    """Resolve the winner from a ready-made list of :class:`CascadeEntry`.

    :param entries: declarations already collected by the caller, each with a
        unique source ``order``.
    :param layer_order: layer names in their declared order, as established by
        ``@layer`` statements before the entries' rules were seen.
    """
    cascade_set = Cascade()
    cascade_set.declare_layers(layer_order)
    for entry in entries:
        cascade_set.add_entry(entry)
    return cascade_set.winner()
