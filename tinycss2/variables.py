"""Resolve CSS custom properties and the resources they select.

tinycss2 is deliberately a low-level parser: it parses component values but
does not know which declarations win in the cascade, nor how ``var()``
references are substituted.  This module adds that optional next step for
tools that collect assets (for example, a download manifest for a page).

It provides:

* :func:`cascade_custom_properties` to pick the winning definition of each
  custom property from declarations given in source order.
* :class:`Resolver` to substitute ``var()`` references, including nested
  fallbacks, while detecting self and mutual reference cycles.
* :func:`resolve_resource` to select the winning declaration for a property
  and return the resource URL it actually uses.

Every resolved token keeps the base URL of the stylesheet it came from.  A
``url()`` substituted from a custom property defined in another stylesheet is
therefore resolved against *that* stylesheet's base URL, not against the base
URL of the declaration that used the variable.

"""

from copy import copy
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .ast import Declaration as ASTDeclaration
from .ast import Node
from .parser import parse_component_value_list, parse_one_declaration
from .serializer import serialize

#: Keywords that are not valid custom property values.
CSS_WIDE_KEYWORDS = frozenset(
    {'initial', 'inherit', 'unset', 'revert', 'revert-layer'})

#: Functions whose direct string arguments are resource URLs.
_STRING_URL_FUNCTIONS = frozenset({'image-set', '-webkit-image-set', 'src'})

_BLOCK_TYPES = frozenset({'() block', '[] block', '{} block'})
_IGNORED_TYPES = frozenset({'whitespace', 'comment'})


@dataclass(frozen=True)
class Declaration:
    """A declaration with the base URL of the stylesheet that contains it.

    A :class:`~tinycss2.ast.Declaration` returned by tinycss2's parser has no
    notion of a base URL.  Wrap it in this class (or pass it with a
    ``base_url`` argument) when resolving relative URLs.

    :param name: The property name, for example ``background-image``.
    :param value: A CSS string, a single node, or an iterable of nodes.
    :param base_url: The URL of the stylesheet containing the declaration.
    :param important: Whether the declaration has ``!important``.
    :param priority: An explicit cascade priority.  Higher values win;
        ``!important`` still wins over normal declarations.  When omitted,
        source order is used.
    """

    name: str
    value: object
    base_url: str | None = None
    important: bool = False
    priority: int | float | None = None


@dataclass(frozen=True)
class FallbackUsage:
    """A fallback value that was used while resolving a ``var()``.

    :param variable: The custom property name whose primary value was not
        usable.
    :param value: The original fallback component values.
    :param resolved: The fallback component values after nested substitution,
        or :obj:`None` if the fallback itself is invalid.
    :param base_url: The base URL of the stylesheet containing the fallback.
    """

    variable: str
    value: tuple
    resolved: tuple | None
    base_url: str | None = None

    @property
    def text(self):
        """The original fallback as CSS text."""
        return serialize(_trim(self.value))

    @property
    def resolved_text(self):
        """The fallback after nested substitution, or :obj:`None`."""
        if self.resolved is None:
            return None
        return serialize(_trim(self.resolved))


@dataclass(frozen=True)
class Resolution:
    """The result of resolving a value or a matched declaration.

    :param value: The resolved component values, or :obj:`None` when the
        value is invalid at computed-value time.
    :param declaration: The winning declaration, when one was selected.
    :param base_url: The base URL of the declaration's stylesheet.
    :param used_variables: Custom property names consulted during resolution,
        in first-use order.  This includes a variable whose fallback was used.
    :param used_fallbacks: Fallback values that were used.
    :param urls: Resource URLs present in the resolved value, absolute when a
        base URL was available.  Fallbacks that were not selected never
        contribute URLs.
    :param invalid: Whether a missing reference, a cycle, or an invalid
        ``var()`` made the value invalid.
    :param reason: A human-readable explanation when ``invalid`` is true, or
        when no declaration matched.
    """

    value: tuple | None
    declaration: Declaration | None
    base_url: str | None
    used_variables: tuple = ()
    used_fallbacks: tuple = ()
    urls: tuple = ()
    invalid: bool = False
    reason: str | None = None

    @property
    def text(self):
        """The resolved value as CSS text, or :obj:`None` when invalid."""
        if self.value is None:
            return None
        return serialize(self.value)

    @property
    def url(self):
        """The first resource URL in the resolved value, or :obj:`None`."""
        return self.urls[0] if self.urls else None


@dataclass(frozen=True)
class _Sourced:
    node: Node
    base_url: str | None


@dataclass
class _Result:
    value: tuple | None
    variables: list = field(default_factory=list)
    fallbacks: list = field(default_factory=list)
    urls: list = field(default_factory=list)
    reason: str | None = None


def _tokens(value):
    """Return *value* as a tuple of component values."""
    if isinstance(value, str):
        return tuple(parse_component_value_list(value))
    if isinstance(value, Node):
        return (value,)
    return tuple(value)


def _is_significant(token):
    return token.type not in _IGNORED_TYPES


def _to_declaration(item, base_url=None):
    """Normalize a declaration-like object."""
    if isinstance(item, Declaration):
        if item.base_url is None and base_url is not None:
            return Declaration(
                item.name, item.value, base_url, item.important, item.priority)
        return item
    if isinstance(item, ASTDeclaration):
        return Declaration(
            item.name, item.value, base_url, item.important)
    if isinstance(item, dict):
        if 'name' not in item or 'value' not in item:
            raise ValueError(
                'Declaration mappings need "name" and "value" keys')
        return Declaration(
            item['name'], item['value'],
            item.get('base_url', base_url), bool(item.get('important', False)),
            item.get('priority'))
    if isinstance(item, str):
        parsed = parse_one_declaration(item)
        if parsed.type != 'declaration':
            raise ValueError(f'Invalid declaration: {item!r}')
        return Declaration(
            parsed.name, parsed.value, base_url, parsed.important)
    if isinstance(item, (tuple, list)):
        if len(item) == 2:
            name, value = item
            base_url = base_url
            important = False
            priority = None
        elif len(item) == 3:
            name, value, base_url = item
            important = False
            priority = None
        elif len(item) == 4:
            name, value, base_url, important = item
            priority = None
        elif len(item) == 5:
            name, value, base_url, important, priority = item
        else:
            raise ValueError(
                'Declaration sequences must have 2, 3, 4, or 5 items')
        return Declaration(
            name, value, base_url, bool(important), priority)
    raise TypeError(f'Unsupported declaration: {item!r}')


def _declaration_key(declaration, index):
    """Return the cascade key of a declaration."""
    return (
        bool(declaration.important),
        declaration.priority if declaration.priority is not None else index,
        index)


def cascade_custom_properties(custom_properties, base_url=None):
    """Select the winning definition of each custom property.

    Declarations are given in ascending cascade order: later declarations
    override earlier ones.  ``!important`` declarations override normal ones,
    and an explicit :attr:`Declaration.priority` overrides source order.
    Custom property names are case-sensitive.

    :param custom_properties: A mapping from custom property names to values
        or :class:`Declaration` objects, a single declaration, or an iterable
        of declarations.  Mapping values may be strings, nodes, iterables of
        nodes, :class:`Declaration` objects, or
        :class:`~tinycss2.ast.Declaration` objects.
    :param base_url: The default base URL for declarations that do not carry
        one.
    :returns: A mapping from winning custom property names to
        :class:`Declaration` objects.
    """
    if not custom_properties:
        return {}

    if isinstance(custom_properties, dict):
        items = []
        for name, value in custom_properties.items():
            if isinstance(value, (Declaration, ASTDeclaration, dict)):
                declaration = _to_declaration(value, base_url)
                declaration = Declaration(
                    name, declaration.value, declaration.base_url,
                    declaration.important, declaration.priority)
            else:
                declaration = Declaration(name, value, base_url)
            items.append(declaration)
    elif isinstance(custom_properties, (Declaration, ASTDeclaration, str)):
        items = [_to_declaration(custom_properties, base_url)]
    else:
        items = [
            _to_declaration(item, base_url) for item in custom_properties]

    winners = {}
    winner_keys = {}
    for index, declaration in enumerate(items):
        if not declaration.name.startswith('--'):
            raise ValueError(
                'Custom property names must start with --: '
                f'{declaration.name!r}')
        key = _declaration_key(declaration, index)
        current = winner_keys.get(declaration.name)
        if current is None or key >= current:
            winners[declaration.name] = declaration
            winner_keys[declaration.name] = key
    return winners


def _parse_var_arguments(function):
    """Return ``(name, fallback)`` for a ``var()`` function, or ``None``.

    ``fallback`` is :obj:`None` when there is no comma, and a tuple (possibly
    empty) when a comma is present.
    """
    arguments = function.arguments
    index = 0
    while index < len(arguments) and not _is_significant(arguments[index]):
        index += 1
    if index == len(arguments):
        return None
    name_token = arguments[index]
    if (name_token.type != 'ident' or
            not name_token.value.startswith('--') or
            len(name_token.value) == 2):
        return None
    name = name_token.value
    index += 1
    while index < len(arguments) and not _is_significant(arguments[index]):
        index += 1
    if index == len(arguments):
        return name, None
    if arguments[index] != ',':
        return None
    return name, tuple(arguments[index + 1:])


def _iter_var_references(nodes):
    """Yield custom property names referenced anywhere in *nodes*.

    References in fallback values are included: CSS cycle detection uses the
    full dependency graph, whether or not a fallback ends up being used.
    """
    for node in nodes:
        if node.type == 'function':
            if node.lower_name == 'var':
                parsed = _parse_var_arguments(node)
                if parsed is not None:
                    name, fallback = parsed
                    yield name
                    if fallback is not None:
                        yield from _iter_var_references(fallback)
            else:
                yield from _iter_var_references(node.arguments)
        elif node.type in _BLOCK_TYPES:
            yield from _iter_var_references(node.content)


def _find_cycles(custom_properties):
    """Return the set of custom property names involved in a cycle."""
    graph = {
        name: set(_iter_var_references(_tokens(declaration.value)))
        for name, declaration in custom_properties.items()}
    for name in graph:
        graph[name].intersection_update(graph)

    cyclic = set()
    state = {}
    for root in graph:
        if root in state:
            continue
        state[root] = 1
        path = [root]
        stack = [(root, iter(graph[root]))]
        while stack:
            node, children = stack[-1]
            try:
                child = next(children)
            except StopIteration:
                state[node] = 2
                stack.pop()
                path.pop()
                continue
            child_state = state.get(child)
            if child_state is None:
                state[child] = 1
                path.append(child)
                stack.append((child, iter(graph[child])))
            elif child_state == 1:
                cyclic.update(path[path.index(child):])
    return cyclic


def _is_css_wide_keyword(nodes):
    significant = [node for node in nodes if _is_significant(node)]
    return (
        len(significant) == 1 and
        significant[0].type == 'ident' and
        significant[0].lower_value in CSS_WIDE_KEYWORDS)


def _deduplicate(items):
    result = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _deduplicate_fallbacks(fallbacks):
    result = []
    seen = set()
    for fallback in fallbacks:
        key = (fallback.variable, fallback.base_url, fallback.text)
        if key not in seen:
            seen.add(key)
            result.append(fallback)
    return result


def _trim(nodes):
    """Drop leading and trailing whitespace and comments."""
    start = 0
    end = len(nodes)
    while start < end and not _is_significant(nodes[start]):
        start += 1
    while end > start and not _is_significant(nodes[end - 1]):
        end -= 1
    return tuple(nodes[start:end])


def _resolution(result, declaration, base_url):
    if result.value is None:
        return Resolution(
            None, declaration, base_url,
            tuple(_deduplicate(result.variables)),
            tuple(_deduplicate_fallbacks(result.fallbacks)),
            (), True, result.reason)
    nodes = _trim(tuple(sourced.node for sourced in result.value))
    return Resolution(
        nodes, declaration, base_url,
        tuple(_deduplicate(result.variables)),
        tuple(_deduplicate_fallbacks(result.fallbacks)),
        tuple(_deduplicate(result.urls)), False, None)


def _urls_in(node, base_url):
    """Return the absolute resource URLs represented by *node*."""
    urls = []
    for value in _iter_urls(node):
        url = urljoin(base_url, value) if base_url else value
        if url not in urls:
            urls.append(url)
    return urls


def _iter_urls(node):
    """Yield the resource URL values directly represented by *node*."""
    if node.type == 'url':
        yield node.value
    elif node.type == 'function':
        if node.lower_name == 'url':
            significant = [
                token for token in node.arguments if _is_significant(token)]
            if len(significant) == 1 and significant[0].type == 'string':
                yield significant[0].value
            return
        if node.lower_name in _STRING_URL_FUNCTIONS:
            for token in node.arguments:
                if token.type == 'string':
                    yield token.value
        for token in node.arguments:
            yield from _iter_urls(token)
    elif node.type in _BLOCK_TYPES:
        for token in node.content:
            yield from _iter_urls(token)



class Resolver:
    """Resolve ``var()`` references against a set of custom properties.

    :param custom_properties: Custom property declarations, in source order.
        See :func:`cascade_custom_properties`.
    :param base_url: The default base URL for custom properties that do not
        carry one.

    Instances cache the resolved value of each custom property, so a resolver
    can be reused for many declarations that share the same custom properties.
    """

    def __init__(self, custom_properties=(), base_url=None):
        self.custom_properties = cascade_custom_properties(
            custom_properties, base_url)
        self.cyclic = _find_cycles(self.custom_properties)
        self._cache = {}
        self._resolving = set()

    def resolve_value(self, value, base_url=None):
        """Resolve ``var()`` references in a component value or CSS string.

        :param value: A CSS string, a single node, or an iterable of nodes.
        :param base_url: The base URL of the stylesheet containing *value*.
        :returns: A :class:`Resolution`.
        """
        nodes = _tokens(value)
        result = self._substitute(
            [_Sourced(node, base_url) for node in nodes])
        return _resolution(result, None, base_url)

    def resolve_declaration(self, declaration, base_url=None):
        """Resolve one declaration.

        :param declaration: A :class:`Declaration`, an
            :class:`~tinycss2.ast.Declaration`, a declaration string, or a
            declaration-like mapping.
        :param base_url: The default base URL when the declaration has none.
        :returns: A :class:`Resolution` whose ``declaration`` field is the
            normalized winner.
        """
        declaration = _to_declaration(declaration, base_url)
        actual_base_url = (
            declaration.base_url
            if declaration.base_url is not None else base_url)
        result = self._substitute([
            _Sourced(node, actual_base_url)
            for node in _tokens(declaration.value)])
        normalized = Declaration(
            declaration.name, declaration.value, actual_base_url,
            declaration.important, declaration.priority)
        return _resolution(result, normalized, actual_base_url)

    def resolve_property(self, declarations, name=None, base_url=None):
        """Resolve the winning declaration for one property.

        Declarations must be given in ascending cascade order: later
        declarations win, ``!important`` declarations win over normal ones,
        and an explicit :attr:`Declaration.priority` wins over source order.
        If *name* is omitted, all declarations must target the same property
        (custom property declarations are ignored).

        :param declarations: An iterable of declarations, in source order.
        :param name: The property to select, for example
            ``background-image``.
        :param base_url: The default base URL for declarations without one.
        :returns: A :class:`Resolution`.
        """
        if isinstance(declarations, (Declaration, ASTDeclaration, str, dict)):
            declarations = [declarations]
        items = [
            _to_declaration(item, base_url) for item in declarations
            if not _is_custom_property_item(item)]
        if name is not None:
            lowered = name.lower()
            items = [item for item in items if item.name.lower() == lowered]
        elif len({item.name.lower() for item in items}) > 1:
            raise ValueError(
                'name is required when declarations target multiple properties')

        if not items:
            return Resolution(
                None, None, base_url, invalid=False,
                reason='no matching declaration')

        winner = None
        winner_key = None
        for index, item in enumerate(items):
            key = _declaration_key(item, index)
            if winner_key is None or key >= winner_key:
                winner, winner_key = item, key
        return self.resolve_declaration(winner, base_url)

    def _substitute(self, sourced_nodes):
        output = []
        variables = []
        fallbacks = []
        urls = []
        for sourced in sourced_nodes:
            node = sourced.node
            base_url = sourced.base_url
            if node.type == 'function' and node.lower_name == 'var':
                parsed = _parse_var_arguments(node)
                if parsed is None:
                    return _Result(
                        None, variables, fallbacks, urls, 'invalid var() syntax')
                name, fallback = parsed
                variables.append(name)
                resolved = self._resolve_custom_property(name)
                variables.extend(resolved.variables)
                fallbacks.extend(resolved.fallbacks)
                if resolved.value is not None:
                    output.extend(resolved.value)
                    urls.extend(resolved.urls)
                    continue
                if fallback is None:
                    reason = resolved.reason or f'custom property {name} is invalid'
                    return _Result(None, variables, fallbacks, urls, reason)

                fallback_result = self._substitute([
                    _Sourced(token, base_url) for token in fallback])
                fallbacks.append(FallbackUsage(
                    name, fallback,
                    None if fallback_result.value is None else tuple(
                        item.node for item in fallback_result.value),
                    base_url))
                variables.extend(fallback_result.variables)
                fallbacks.extend(fallback_result.fallbacks)
                urls.extend(fallback_result.urls)
                if fallback_result.value is None:
                    reason = fallback_result.reason or 'invalid fallback value'
                    return _Result(None, variables, fallbacks, urls, reason)
                output.extend(fallback_result.value)
            elif node.type == 'function':
                result = self._substitute([
                    _Sourced(token, base_url) for token in node.arguments])
                variables.extend(result.variables)
                fallbacks.extend(result.fallbacks)
                urls.extend(result.urls)
                if result.value is None:
                    return _Result(None, variables, fallbacks, urls, result.reason)
                copied = copy(node)
                copied.arguments = [item.node for item in result.value]
                output.append(_Sourced(copied, base_url))
                urls.extend(_urls_in(copied, base_url))
            elif node.type in _BLOCK_TYPES:
                result = self._substitute([
                    _Sourced(token, base_url) for token in node.content])
                variables.extend(result.variables)
                fallbacks.extend(result.fallbacks)
                urls.extend(result.urls)
                if result.value is None:
                    return _Result(None, variables, fallbacks, urls, result.reason)
                copied = copy(node)
                copied.content = [item.node for item in result.value]
                output.append(_Sourced(copied, base_url))
                urls.extend(_urls_in(copied, base_url))
            else:
                output.append(sourced)
                urls.extend(_urls_in(node, base_url))
        return _Result(tuple(output), variables, fallbacks, urls)

    def _resolve_custom_property(self, name):
        if name in self._cache:
            return self._cache[name]
        if name in self.cyclic:
            result = _Result(
                None, reason=f'custom property {name} is invalid (reference cycle)')
            self._cache[name] = result
            return result
        declaration = self.custom_properties.get(name)
        if declaration is None:
            result = _Result(None, reason=f'missing custom property {name}')
            self._cache[name] = result
            return result
        if name in self._resolving:
            result = _Result(
                None, reason=f'custom property {name} is invalid (reference cycle)')
            self._cache[name] = result
            return result

        nodes = _tokens(declaration.value)
        if _is_css_wide_keyword(nodes):
            result = _Result(
                None, reason=f'custom property {name} has a CSS-wide keyword')
            self._cache[name] = result
            return result

        self._resolving.add(name)
        try:
            result = self._substitute([
                _Sourced(node, declaration.base_url) for node in nodes])
        finally:
            self._resolving.remove(name)
        if result.value is None:
            reason = result.reason or f'custom property {name} is invalid'
            result = _Result(
                None, result.variables, result.fallbacks, result.urls, reason)
        self._cache[name] = result
        return result


def _is_custom_property_item(item):
    if isinstance(item, Declaration):
        return item.name.startswith('--')
    if isinstance(item, ASTDeclaration):
        return item.name.startswith('--')
    if isinstance(item, dict):
        return str(item.get('name', '')).startswith('--')
    if isinstance(item, str):
        parsed = parse_one_declaration(item)
        return (
            parsed.type == 'declaration' and parsed.name.startswith('--'))
    return False


def resolve_variables(value, custom_properties=(), base_url=None):
    """Resolve ``var()`` references in *value*.

    This is a convenience wrapper around :class:`Resolver` for a single value.

    :param value: A CSS string, a single node, or an iterable of nodes.
    :param custom_properties: Custom property declarations in source order.
    :param base_url: The base URL of the stylesheet containing *value* and the
        default base URL for custom properties without one.
    :returns: A :class:`Resolution`.
    """
    return Resolver(custom_properties, base_url).resolve_value(value, base_url)


def resolve_declaration(declaration, custom_properties=(), base_url=None):
    """Resolve one declaration against *custom_properties*.

    :param declaration: A declaration or declaration-like object.
    :param custom_properties: Custom property declarations in source order.
    :param base_url: The default base URL for the declaration and for custom
        properties without one.
    :returns: A :class:`Resolution`.
    """
    return Resolver(custom_properties, base_url).resolve_declaration(
        declaration, base_url)


def resolve_resource(
        declarations, custom_properties=(), name=None, base_url=None):
    """Select the winning declaration and return its used resource URL.

    This is the high-level helper for download manifests: declarations are
    the ones already matched to a component, in ascending cascade order.  The
    returned :attr:`Resolution.urls` contains only URLs present in the
    winning, resolved value.  URLs in custom property fallbacks that were not
    selected are not included.

    :param declarations: Declarations in source order.
    :param custom_properties: Custom property declarations in source order.
    :param name: The property to select.  Optional when all declarations
        target the same property.
    :param base_url: The default base URL for declarations without one.
    :returns: A :class:`Resolution`.
    """
    return Resolver(custom_properties, base_url).resolve_property(
        declarations, name, base_url)
