"""Cleaning and reconciliation of CSS declarations.

This module builds on the low-level parser to provide declaration keys and
scope-aware handling of duplicate declarations. It is meant for consumers that
need to group declarations by their logical property name while still being
able to point back at the original spelling used in the source.

Ordinary property names are ASCII case-insensitive. Custom property names
(``--*``) are case-sensitive. Duplicate declarations are resolved per scope,
where a scope is a list of declarations such as the contents of one rule.
Later declarations win, except that an ``!important`` declaration may not be
overridden by a non-important one within the same scope.

"""

from .ast import Declaration
from .parser import parse_blocks_contents
from .serializer import serialize, serialize_identifier


def declaration_key(declaration):
    """Return the logical key used to group a declaration.

    Ordinary properties are folded to ASCII lower case, custom properties keep
    their case-sensitive name.

    :type declaration: :class:`~tinycss2.ast.Declaration`
    :returns: A :obj:`str` logical property name.

    """
    if declaration.name.startswith('--'):
        return declaration.name
    return declaration.lower_name


def declaration_raw_name(declaration):
    """Return the property name spelling found in the source.

    The returned string keeps escape sequences as written. When the
    declaration has no source spelling (for example because it was built by
    hand), the canonical serialization of its name is returned.

    :type declaration: :class:`~tinycss2.ast.Declaration`
    :returns: A :obj:`str` CSS identifier.

    """
    raw_name = getattr(declaration, 'raw_name', None)
    if raw_name is None:
        return serialize_identifier(declaration.name)
    return raw_name


def clean_declaration(declaration):
    """Return a declaration whose name is the canonical logical spelling.

    The value and the ``!important`` flag are preserved. The original spelling
    of the name is kept in :attr:`~tinycss2.ast.Declaration.raw_name`, so the
    cleaned declaration can still point back at the source form. This makes it
    easy to re-export cleaned declarations and tokenize them again without
    losing the ability to compare logical keys.

    :type declaration: :class:`~tinycss2.ast.Declaration`
    :returns: A new :class:`~tinycss2.ast.Declaration`.

    """
    name = declaration_key(declaration)
    return Declaration(
        declaration.source_line, declaration.source_column, name, name,
        list(declaration.value), declaration.important,
        raw_name=declaration_raw_name(declaration))


def _priority(declaration):
    return 1 if declaration.important else 0


def resolve_declarations(declarations, keep='last'):
    """Resolve duplicate declarations within a single scope.

    Declarations are matched by :func:`declaration_key`. By default the last
    declaration wins. An ``!important`` declaration can only be replaced by
    another ``!important`` declaration, as in the cascade.

    The returned list keeps the position of the winning declaration in the
    original list, so that declaration order remains available for
    reconciliation.

    :type declarations: :term:`iterable`
    :param declarations: An iterable of
        :class:`~tinycss2.ast.Declaration` objects.
    :param keep:
        Either ``'last'`` (default) or ``'first'``. ``'first'`` keeps the first
        declaration for each logical key instead.
    :returns: A list of :class:`~tinycss2.ast.Declaration`.

    """
    if keep not in ('last', 'first'):
        raise ValueError(f'Invalid keep value: {keep!r}')
    declarations = list(declarations)
    winners = {}
    for index, declaration in enumerate(declarations):
        key = declaration_key(declaration)
        if key not in winners:
            winners[key] = index
            continue
        if keep == 'first':
            continue
        previous = declarations[winners[key]]
        if _priority(declaration) >= _priority(previous):
            winners[key] = index
    ordered = sorted(winners.items(), key=lambda item: item[1])
    return [declarations[index] for _, index in ordered]


def clean_declarations(input, skip_comments=False, skip_whitespace=True):
    """Parse and clean a declaration list.

    This is a convenience wrapper around
    :func:`~tinycss2.parse_blocks_contents` and
    :func:`resolve_declarations`. At-rules and nested rules are ignored, only
    valid declarations are returned. The result keeps declaration order and
    each declaration exposes both :attr:`~tinycss2.ast.Declaration.name`
    (logical, canonical) and :attr:`~tinycss2.ast.Declaration.raw_name`
    (original spelling).

    :type input: :obj:`str` or :term:`iterable`
    :param input: A string or an iterable of :term:`component values`.
    :type skip_comments: :obj:`bool`
    :param skip_comments: Ignore CSS comments at the top-level of the list.
    :type skip_whitespace: :obj:`bool`
    :param skip_whitespace: Ignore whitespace at the top-level of the list.
    :returns: A list of cleaned
        :class:`~tinycss2.ast.Declaration` objects.

    """
    nodes = parse_blocks_contents(
        input, skip_comments=skip_comments, skip_whitespace=skip_whitespace)
    declarations = [
        clean_declaration(node) for node in nodes if node.type == 'declaration']
    return resolve_declarations(declarations)


def clean_scopes(nodes, skip_comments=False, skip_whitespace=True):
    """Clean declarations in each scope of a parsed tree.

    A scope is a qualified rule or an at-rule with a declaration block.
    Declarations are resolved independently within each scope, so the same
    property may appear once per rule after cleaning. Nested rules, including
    those inside conditional at-rules such as ``@media``, are visited
    recursively. Scopes are returned in document order.

    :type nodes: :term:`iterable`
    :param nodes: Parsed nodes, typically the result of
        :func:`~tinycss2.parse_stylesheet` or
        :func:`~tinycss2.parse_rule_list`.
    :returns: A list of :obj:`dict` objects, each with ``rule`` (the source
        node) and ``declarations`` (the cleaned declarations of that scope).

    """
    scopes = []

    def visit(nodes):
        for node in nodes:
            if node.type == 'qualified-rule':
                content = node.content
            elif node.type == 'at-rule':
                content = node.content
            else:
                continue
            if content is None:
                continue
            parsed = parse_blocks_contents(
                content, skip_comments=skip_comments,
                skip_whitespace=skip_whitespace)
            cleaned = [
                clean_declaration(declaration) for declaration in parsed
                if declaration.type == 'declaration']
            if cleaned:
                scopes.append({
                    'rule': node,
                    'declarations': resolve_declarations(cleaned),
                })
            visit(parsed)

    visit(nodes)
    return scopes


def clean_stylesheet(input, skip_comments=True, skip_whitespace=True):
    """Parse a stylesheet and clean each rule’s declarations.

    This is a convenience wrapper around
    :func:`~tinycss2.parse_stylesheet` and :func:`clean_scopes`.

    :type input: :obj:`str` or :term:`iterable`
    :param input: A string or an iterable of :term:`component values`.
    :returns: A list of :obj:`dict` objects as returned by
        :func:`clean_scopes`.

    """
    from .parser import parse_stylesheet

    nodes = parse_stylesheet(
        input, skip_comments=skip_comments, skip_whitespace=skip_whitespace)
    return clean_scopes(
        nodes, skip_comments=skip_comments, skip_whitespace=skip_whitespace)


def declaration_records(declarations):
    """Return a reconciliation-friendly view of declarations.

    Each record is a dict with the logical ``key``, the original ``raw_name``,
    the canonical ``name``, the declaration ``important`` flag and the
    serialized declaration ``text``. Records keep the declaration order.

    :type declarations: :term:`iterable`
    :param declarations: An iterable of
        :class:`~tinycss2.ast.Declaration` objects.
    :returns: A list of :obj:`dict` objects.

    """
    return [
        {
            'key': declaration_key(declaration),
            'name': declaration.name,
            'raw_name': declaration_raw_name(declaration),
            'important': declaration.important,
            'value': serialize(declaration.value),
            'text': declaration.serialize(),
        }
        for declaration in declarations
    ]


def reconciliation_errors(records, reparsed):
    """Compare two record lists obtained from :func:`declaration_records`.

    Logical keys, order and values must match. Original spellings
    (``raw_name``) and full declaration text are not compared, because
    re-exporting cleaned declarations canonicalizes the name on purpose.

    :returns: A list of human-readable error strings, empty when the two
        lists reconcile.

    """
    errors = []
    if len(records) != len(reparsed):
        errors.append(
            f'Declaration count changed from {len(records)} to {len(reparsed)}')
    for index, (before, after) in enumerate(zip(records, reparsed)):
        for field in ('key', 'name', 'important', 'value'):
            if before[field] != after[field]:
                errors.append(
                    f'Declaration {index} {field} changed from '
                    f'{before[field]!r} to {after[field]!r}')
    return errors


def reconcile_declarations(source, reexport=None):
    """Parse, clean, export and re-tokenize declarations for reconciliation.

    ``source`` is parsed with :func:`clean_declarations`. The cleaned
    declarations are serialized together and parsed again, and both sides are
    returned so callers can compare logical keys, order and values.

    :type source: :obj:`str` or :term:`iterable`
    :param source: The declarations to clean.
    :param reexport:
        Optional callable used to serialize the cleaned declarations. It
        receives the list of cleaned declarations and must return CSS text.
        Defaults to :func:`tinycss2.serialize`.
    :returns: A :obj:`dict` with ``declarations`` (cleaned),
        ``records`` (from :func:`declaration_records`), ``serialized`` (the
        re-exported text), ``reparsed`` (the records obtained by parsing
        ``serialized`` again) and ``errors`` (from
        :func:`reconciliation_errors`).

    """
    if reexport is None:
        reexport = serialize
    declarations = clean_declarations(source)
    serialized = reexport(declarations)
    reparsed = [
        declaration
        for declaration in parse_blocks_contents(
            serialized, skip_comments=True, skip_whitespace=True)
        if declaration.type == 'declaration']
    records = declaration_records(declarations)
    reparsed_records = declaration_records(reparsed)
    return {
        'declarations': declarations,
        'records': records,
        'serialized': serialized,
        'reparsed': reparsed_records,
        'errors': reconciliation_errors(records, reparsed_records),
    }
