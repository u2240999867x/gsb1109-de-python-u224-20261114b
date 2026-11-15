"""Incremental stylesheet collection.

Parsing CSS from bytes requires two steps: decoding the bytes according to the
stylesheet encoding rules, then tokenizing the resulting Unicode string.
:class:`StylesheetCollector` caches the second step and keeps enough
information about the first step to decide, when a parent stylesheet changes,
which children must be decoded again from their original bytes.

Encoding is deliberately part of the cache key.  The same byte string can be
decoded differently when it is linked from two parents that provide different
:ref:`environment encodings
<environment encoding>`, and both results are kept side by side.  A child
with its own BOM, protocol encoding or ``@charset`` rule has the same decoded
text in both contexts and can share the cached parse.
"""

import hashlib
from dataclasses import dataclass, field

from .ast import (  # isort:skip
    AtKeywordToken, AtRule, CurlyBracketsBlock, Declaration, DimensionToken,
    FunctionBlock, HashToken, IdentToken, ParenthesesBlock, QualifiedRule,
    SquareBracketsBlock, StringToken, URLToken)
from .bytes import (
    BOM, CHARSET, ENVIRONMENT, PROTOCOL, EncodingDecision,
    decode_stylesheet_bytes_detailed, determine_encoding)
from .parser import parse_stylesheet
from .serializer import serialize

# Reasons reported by :class:`ParseRecord`.
FIRST_PARSE = 'first-parse'
SOURCE_CHANGED = 'source-changed'
PROTOCOL_CHANGED = 'protocol-changed'
ENCODING_CHANGED = 'encoding-changed'
INHERITED_ENCODING_CHANGED = 'inherited-encoding-changed'
OWN_ENCODING_HINT = 'own-encoding-hint'
UNCHANGED = 'unchanged'

# Encoding sources that come from the resource itself.
_OWN_ENCODING_SOURCES = (BOM, PROTOCOL, CHARSET)


@dataclass(frozen=True)
class SourceBytes:
    """The original bytes of a stylesheet and its protocol hints.

    .. attribute:: css_bytes

        The original byte string.

    .. attribute:: protocol_encoding

        The encoding label supplied by the protocol, if any.

    .. attribute:: digest

        A SHA-256 digest of :attr:`css_bytes`.

    """
    css_bytes: bytes
    protocol_encoding: str | None = None

    @property
    def digest(self):
        return hashlib.sha256(self.css_bytes).hexdigest()


@dataclass(frozen=True)
class ParseKey:
    """The identity of a cached parse.

    Two attempts produce the same tree exactly when these values match.

    .. attribute:: digest

        Digest of the original bytes.

    .. attribute:: encoding_name

        Canonical name of the encoding actually used.

    .. attribute:: skip_comments

        Whether comments were skipped while parsing.

    .. attribute:: skip_whitespace

        Whether top-level whitespace was skipped while parsing.

    """
    digest: str
    encoding_name: str
    skip_comments: bool
    skip_whitespace: bool


@dataclass(frozen=True)
class ParseEvent:
    """One attempt to collect a stylesheet.

    Events are appended to :attr:`StylesheetCollector.parse_events` for every
    collection, including cached ones, so that every parse can be audited.

    .. attribute:: resource_id

        Identifier of the collected resource.

    .. attribute:: parent_id

        Identifier of the referring resource, or :obj:`None` for a root.

    .. attribute:: key

        The :class:`ParseKey` that was looked up.

    .. attribute:: decision

        The :class:`~tinycss2.bytes.EncodingDecision` used for this attempt.

    .. attribute:: reused

        :obj:`True` if a cached tree was reused.

    .. attribute:: reason

        Why the record was reused or parsed again, see :data:`FIRST_PARSE`,
        :data:`SOURCE_CHANGED`, :data:`PROTOCOL_CHANGED`,
        :data:`ENCODING_CHANGED`, :data:`INHERITED_ENCODING_CHANGED`,
        :data:`OWN_ENCODING_HINT` and :data:`UNCHANGED`.

    """
    resource_id: str
    parent_id: str | None
    key: ParseKey
    decision: EncodingDecision
    reused: bool
    reason: str


@dataclass
class ParseRecord:
    """The result of collecting one resource in one encoding context.

    .. attribute:: resource_id

        Identifier of the collected resource.

    .. attribute:: parent_id

        Identifier of the referring resource, or :obj:`None` for a root.

    .. attribute:: digest

        Digest of the original bytes used for this parse.

    .. attribute:: protocol_encoding

        The protocol encoding label that was applied, if any.

    .. attribute:: decision

        The :class:`~tinycss2.bytes.EncodingDecision` that was used.  This
        records the encoding *and* why it was selected.

    .. attribute:: rules

        The parsed top-level rules.

    .. attribute:: key

        The :class:`ParseKey` of this parse.

    .. attribute:: reused

        :obj:`True` if a cached tree was reused.

    .. attribute:: reason

        Why this record was reused or parsed, see :class:`ParseEvent`.

    """
    resource_id: str
    parent_id: str | None
    digest: str
    protocol_encoding: str | None
    decision: EncodingDecision
    rules: list
    key: ParseKey
    reused: bool
    reason: str

    @property
    def encoding(self):
        """The encoding actually used."""
        return self.decision.encoding

    @property
    def encoding_source(self):
        """Where the encoding came from, see :mod:`tinycss2.bytes`."""
        return self.decision.source

    @property
    def encoding_label(self):
        """The original label for protocol and ``@charset`` encodings."""
        return self.decision.label

    @property
    def environment_encoding(self):
        """The environment encoding supplied by the parent, if any."""
        return self.decision.environment_encoding

    @property
    def identifiers(self):
        """Identifiers in the parsed rules, in document order."""
        return extract_identifiers(self.rules)

    @property
    def resource_references(self):
        """Resource references in the parsed rules, in document order."""
        return extract_resource_references(self.rules)

    def serialize(self):
        """Serialize the parsed rules to CSS syntax."""
        return serialize(self.rules)


@dataclass
class CollectionResult:
    """The result of a recursive :meth:`StylesheetCollector.collect_all`.

    .. attribute:: root_id

        Identifier of the root resource.

    .. attribute:: records

        All records visited, in depth-first order.  The same resource can
        appear more than once when it is reachable from several parents or
        decoded in several encoding contexts.

    """
    root_id: str
    records: list = field(default_factory=list)

    @property
    def reparsed(self):
        """Records parsed again during this collection."""
        return [record for record in self.records if not record.reused]

    @property
    def reused(self):
        """Records that reused a cached tree during this collection."""
        return [record for record in self.records if record.reused]

    @property
    def reread_from_source(self):
        """Records decoded again from their original bytes.

        These are records with unchanged bytes whose encoding changed, plus
        records whose bytes were updated.  Records reused from the cache are
        excluded.

        """
        return [
            record for record in self.reparsed
            if record.reason in (
                SOURCE_CHANGED, PROTOCOL_CHANGED, ENCODING_CHANGED,
                INHERITED_ENCODING_CHANGED)]

    def records_for(self, resource_id):
        """Return all records for ``resource_id`` in this collection."""
        return [
            record for record in self.records
            if record.resource_id == resource_id]

    def record_for(self, resource_id):
        """Return the first record for ``resource_id``, or :obj:`None`."""
        records = self.records_for(resource_id)
        return records[0] if records else None


def _child_nodes(node):
    """Yield the direct child nodes of ``node``."""
    if isinstance(node, (AtRule, QualifiedRule)):
        yield from node.prelude
        if node.content is not None:
            yield from node.content
    elif isinstance(node, Declaration):
        yield from node.value
    elif isinstance(node, FunctionBlock):
        yield from node.arguments
    elif isinstance(node, (CurlyBracketsBlock, ParenthesesBlock,
                           SquareBracketsBlock)):
        yield from node.content


def _iter_nodes(nodes):
    """Yield ``nodes`` and all their descendants in document order."""
    for node in nodes:
        yield node
        yield from _iter_nodes(_child_nodes(node))


def extract_identifiers(nodes):
    """Return the CSS identifiers in ``nodes``, in document order.

    This includes ident tokens, at-keywords (without ``@``), hash tokens that
    are valid identifiers, function names, dimension units and declaration
    names.

    :type nodes: :term:`iterable`
    :param nodes: An iterable of :class:`~tinycss2.ast.Node` objects.
    :returns: A list of Unicode strings.

    """
    identifiers = []
    for node in _iter_nodes(nodes):
        if isinstance(node, (IdentToken, AtKeywordToken)):
            identifiers.append(node.value)
        elif isinstance(node, HashToken) and node.is_identifier:
            identifiers.append(node.value)
        elif isinstance(node, (FunctionBlock, Declaration)):
            identifiers.append(node.name)
        elif isinstance(node, DimensionToken):
            identifiers.append(node.unit)
    return identifiers


def _url_function_value(node):
    if node.lower_name != 'url':
        return None
    significant = [
        argument for argument in node.arguments
        if argument.type not in ('whitespace', 'comment')]
    if len(significant) == 1 and isinstance(significant[0], StringToken):
        return significant[0].value


def extract_resource_references(nodes):
    """Return the resource references in ``nodes``, in document order.

    This includes unquoted ``url()`` tokens, quoted ``url()`` functions and
    string URLs in ``@import`` rules.

    :type nodes: :term:`iterable`
    :param nodes: An iterable of :class:`~tinycss2.ast.Node` objects.
    :returns: A list of Unicode strings.

    """
    references = []
    for node in _iter_nodes(nodes):
        if isinstance(node, URLToken):
            references.append(node.value)
        elif isinstance(node, FunctionBlock):
            value = _url_function_value(node)
            if value is not None:
                references.append(value)
        elif (isinstance(node, AtRule) and
              node.lower_at_keyword == 'import'):
            for token in node.prelude:
                if isinstance(token, (URLToken, StringToken)):
                    references.append(token.value)
                    break
    return references


class StylesheetCollector:
    """Collect and incrementally re-parse stylesheets from their original bytes.

    Original byte strings are registered with :meth:`add`, and parent/child
    relationships with :meth:`link`.  Calling :meth:`collect_all` then parses
    the root and recursively parses linked children.

    A linked child inherits the encoding of its parent, as CSS ``@import``
    rules do.  If a parent later changes and its encoding changes too, only
    children that do not carry their own encoding hint are decoded again from
    their original bytes.  Parses are cached by content and actual encoding,
    so the same original bytes can have concurrent results in different
    encoding contexts.

    The collector never mutates registered bytes; it only caches parse results
    in memory.  Parse trees are shared between collections that resolve to the
    same :class:`ParseKey` and should be treated as read-only.

    :type skip_comments: :obj:`bool`
    :param skip_comments: Ignore comments while parsing.
    :type skip_whitespace: :obj:`bool`
    :param skip_whitespace: Ignore top-level whitespace while parsing.

    """
    def __init__(self, *, skip_comments=False, skip_whitespace=False):
        self.skip_comments = skip_comments
        self.skip_whitespace = skip_whitespace
        self._sources = {}
        self._children = {}
        self._cache = {}
        self._records = {}
        self._edge_records = {}
        self.parse_events = []

    @property
    def resource_ids(self):
        """Identifiers of all registered resources."""
        return set(self._sources)

    def add(self, resource_id, css_bytes, protocol_encoding=None):
        """Register or replace the original bytes of a resource.

        :type resource_id: :obj:`str`
        :param resource_id: A stable identifier for the resource.
        :type css_bytes: :obj:`bytes`
        :param css_bytes: The original byte string.
        :type protocol_encoding: :obj:`str`
        :param protocol_encoding:
            The encoding label defined by the protocol, if any.
        :returns:
            :obj:`True` if the registered source changed.

        """
        source = SourceBytes(bytes(css_bytes), protocol_encoding)
        changed = self._sources.get(resource_id) != source
        self._sources[resource_id] = source
        return changed

    def update(self, resource_id, css_bytes, protocol_encoding=None):
        """Replace an already registered resource.

        This is the same as :meth:`add`, but raises :exc:`KeyError` when the
        resource has not been registered yet.

        """
        if resource_id not in self._sources:
            raise KeyError(f'Unknown stylesheet resource: {resource_id!r}')
        return self.add(resource_id, css_bytes, protocol_encoding)

    def remove(self, resource_id):
        """Remove a resource and its links.

        Cached parses are kept so that other resources can still reuse them.

        """
        self._sources.pop(resource_id, None)
        self._children.pop(resource_id, None)
        for children in self._children.values():
            children.pop(resource_id, None)
        for edge in list(self._edge_records):
            if resource_id in edge:
                del self._edge_records[edge]

    def link(self, parent_id, child_id):
        """Declare that ``child_id`` is referred to by ``parent_id``.

        The child is parsed recursively by :meth:`collect_all`, inheriting the
        parent's encoding.  The declaration can be made before either resource
        is added to the collector.

        :returns: :obj:`True` if the link was new.

        """
        if parent_id == child_id:
            raise ValueError('A stylesheet can not link to itself')
        children = self._children.setdefault(parent_id, {})
        if child_id in children:
            return False
        children[child_id] = None
        return True

    def unlink(self, parent_id, child_id):
        """Remove a parent/child link.

        :returns: :obj:`True` if the link existed.

        """
        children = self._children.get(parent_id)
        if children is None or child_id not in children:
            return False
        del children[child_id]
        self._edge_records.pop((child_id, parent_id), None)
        return True

    def children_of(self, resource_id):
        """Return the identifiers linked from ``resource_id``.

        Children are returned in the order in which they were linked.

        """
        return tuple(self._children.get(resource_id, ()))

    def collect(self, resource_id, environment_encoding=None):
        """Parse one resource without following its links.

        :type resource_id: :obj:`str`
        :param resource_id: Identifier of the resource to parse.
        :type environment_encoding: :class:`webencodings.Encoding`
        :param environment_encoding:
            The environment encoding, if the resource is parsed as the target
            of a link rather than as a root.
        :returns: A :class:`ParseRecord`.

        """
        return self._collect_one(resource_id, None, environment_encoding)

    def collect_all(self, root_id, environment_encoding=None):
        """Parse a root and all resources reachable through links.

        :type root_id: :obj:`str`
        :param root_id: Identifier of the root resource.
        :type environment_encoding: :class:`webencodings.Encoding`
        :param environment_encoding: The environment encoding of the root.
        :returns: A :class:`CollectionResult`.

        """
        result = CollectionResult(root_id)
        self._visit(
            root_id, None, environment_encoding, (), result)
        return result

    def children_to_reread(self, parent_id, css_bytes=None,
                           protocol_encoding=None, environment_encoding=None):
        """Return children that must be read again from their original bytes.

        Call this after :meth:`update` to find out which direct children will
        need to be decoded again if the parent is recollected.  Only direct
        children are inspected; :meth:`reread_plan` walks the whole subtree.

        :type parent_id: :obj:`str`
        :param parent_id: Identifier of the parent resource.
        :type css_bytes: :obj:`bytes`
        :param css_bytes:
            The proposed parent bytes.  Defaults to the currently registered
            bytes.
        :type protocol_encoding: :obj:`str`
        :param protocol_encoding:
            The proposed protocol encoding.  Defaults to the currently
            registered one.
        :type environment_encoding: :class:`webencodings.Encoding`
        :param environment_encoding:
            The parent's own environment encoding, if the parent is itself a
            child.
        :returns: A list of child identifiers, in link order.

        """
        source = self._sources.get(parent_id)
        if source is None:
            raise KeyError(f'Unknown stylesheet resource: {parent_id!r}')
        if css_bytes is not None or protocol_encoding is not None:
            source = SourceBytes(
                bytes(source.css_bytes if css_bytes is None else css_bytes),
                source.protocol_encoding if protocol_encoding is None else
                protocol_encoding)
        parent_decision = determine_encoding(
            source.css_bytes, source.protocol_encoding, environment_encoding)
        reread = []
        for child_id in self.children_of(parent_id):
            child_source = self._sources.get(child_id)
            if child_source is None:
                continue
            previous = self._edge_records.get((child_id, parent_id))
            child_decision = determine_encoding(
                child_source.css_bytes, child_source.protocol_encoding,
                parent_decision.encoding)
            key = self._key(child_source, child_decision)
            if previous is None or previous.key != key:
                reread.append(child_id)
        return reread

    def reread_plan(self, root_id, css_bytes=None, protocol_encoding=None,
                    environment_encoding=None):
        """Return all resources that must be read again from their bytes.

        This is like :meth:`children_to_reread`, but follows links recursively
        and reports the whole affected subtree.  A parent whose own parse key
        is unchanged is still walked, because a descendant may change through
        its own bytes or protocol encoding even when its ancestors do not.

        :type root_id: :obj:`str`
        :param root_id: Identifier of the changed root resource.
        :returns: A list of ``(resource_id, parent_id)`` tuples in
            depth-first order.  ``parent_id`` is the identifier of the first
            referring parent that triggered the reread.

        """
        source = self._sources.get(root_id)
        if source is None:
            raise KeyError(f'Unknown stylesheet resource: {root_id!r}')
        if css_bytes is not None or protocol_encoding is not None:
            source = SourceBytes(
                bytes(source.css_bytes if css_bytes is None else css_bytes),
                source.protocol_encoding if protocol_encoding is None else
                protocol_encoding)
        plan = []
        self._plan_walk(
            root_id, None, environment_encoding, source, plan, ())
        return plan

    def _plan_walk(self, resource_id, parent_id, environment_encoding,
                   proposed_source, plan, path):
        if proposed_source is None and resource_id not in self._sources:
            return
        source = proposed_source or self._sources[resource_id]
        decision = determine_encoding(
            source.css_bytes, source.protocol_encoding, environment_encoding)
        key = self._key(source, decision)
        previous = self._edge_records.get((resource_id, parent_id))
        if previous is None or previous.key != key:
            plan.append((resource_id, parent_id))
        if resource_id in path:
            return
        for child_id in self.children_of(resource_id):
            self._plan_walk(
                child_id, resource_id, decision.encoding, None, plan,
                path + (resource_id,))

    def records_for(self, resource_id):
        """Return current records for ``resource_id``, one per encoding context.

        Records for obsolete byte strings are not returned, but records for
        the current bytes decoded with different encodings are all returned.

        """
        source = self._sources.get(resource_id)
        if source is None:
            return []
        return [
            record for (record_id, _), record in self._records.items()
            if record_id == resource_id and record.digest == source.digest]

    def clear_cache(self):
        """Forget cached parses and comparison state."""
        self._cache.clear()
        self._records.clear()
        self._edge_records.clear()

    def _key(self, source, decision):
        return ParseKey(
            source.digest, decision.encoding.name,
            self.skip_comments, self.skip_whitespace)

    def _visit(self, resource_id, parent_id, environment_encoding,
               path, result):
        record = self._collect_one(
            resource_id, parent_id, environment_encoding)
        result.records.append(record)
        if resource_id in path:
            # Do not recurse forever on cyclic links.
            return
        for child_id in self.children_of(resource_id):
            self._visit(
                child_id, resource_id, record.encoding,
                path + (resource_id,), result)

    def _collect_one(self, resource_id, parent_id, environment_encoding):
        source = self._sources.get(resource_id)
        if source is None:
            raise KeyError(f'Unknown stylesheet resource: {resource_id!r}')

        decision = determine_encoding(
            source.css_bytes, source.protocol_encoding, environment_encoding)
        key = self._key(source, decision)

        cached = self._cache.get(key)
        if cached is None:
            decoded = decode_stylesheet_bytes_detailed(
                source.css_bytes, source.protocol_encoding,
                environment_encoding)
            rules = parse_stylesheet(
                decoded.css, self.skip_comments, self.skip_whitespace)
            self._cache[key] = rules
            reused = False
        else:
            rules = cached
            reused = True

        previous = self._edge_records.get((resource_id, parent_id))
        reason = self._reason(source, previous, decision, reused)
        record = ParseRecord(
            resource_id, parent_id, source.digest, source.protocol_encoding,
            decision, rules, key, reused, reason)
        self._records[(resource_id, key)] = record
        self._edge_records[(resource_id, parent_id)] = record
        self.parse_events.append(ParseEvent(
            resource_id, parent_id, key, decision, reused, reason))
        return record

    @staticmethod
    def _reason(source, previous, decision, reused):
        if previous is None:
            return FIRST_PARSE
        if previous.digest != source.digest:
            return SOURCE_CHANGED
        if previous.encoding.name != decision.encoding.name:
            if decision.source == ENVIRONMENT:
                return INHERITED_ENCODING_CHANGED
            if decision.source == PROTOCOL:
                return PROTOCOL_CHANGED
            return ENCODING_CHANGED
        previous_environment = previous.environment_encoding
        new_environment = decision.environment_encoding
        environment_changed = (
            (previous_environment is None) != (new_environment is None) or
            (previous_environment is not None and
             new_environment is not None and
             previous_environment.name != new_environment.name))
        if environment_changed and decision.source in _OWN_ENCODING_SOURCES:
            return OWN_ENCODING_HINT
        return UNCHANGED
