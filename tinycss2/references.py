"""

Extract ``@import`` and resource references from locally captured CSS.

The input is a manifest of captured HTTP responses (a HAR document or a list
of simple response dictionaries). Stylesheets are parsed through the usual
tinycss2 entry points, but each distinct stylesheet body is only parsed once:
the references found in a body are base-independent, so they are re-resolved
against the final URL of every response that serves that body.

"""

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from .parser import parse_blocks_contents, parse_stylesheet
from .serializer import serialize

__all__ = [
    'ResourceReference', 'extract_references', 'load_manifest',
    'resolve_manifest_url',
]


@dataclass(frozen=True, slots=True)
class ResourceReference:
    """A single ``@import`` or resource relationship.

    .. attribute:: kind

        ``'import'`` for an ``@import`` rule, ``'resource'`` for any other
        ``url()`` reference (images, fonts, …).

    .. attribute:: source

        The final response URL of the stylesheet containing the reference.

    .. attribute:: raw_url

        The URL as written in the CSS source, including quotes or the
        ``url(...)`` markers, without any normalization.

    .. attribute:: target

        The final response URL of the referenced resource, after resolving the
        relative reference against :attr:`source` and following the redirect
        chains recorded in the manifest.

    .. attribute:: condition

        For ``@import`` rules, the media query, ``supports()`` and ``layer()``
        conditions following the URL, as a single string. :obj:`None` for
        unconditional imports and for resource references.

    .. attribute:: cycle

        :obj:`True` when an ``@import`` points back to one of its ancestor
        stylesheets in the current import chain. Such a relationship is still
        reported, but not followed again. Always :obj:`False` for resources.

    """

    kind: str
    source: str
    raw_url: str
    target: str
    condition: str | None = None
    cycle: bool = False


def load_manifest(path):
    """Load a response manifest from a JSON file.

    :type path: :obj:`str` or :class:`pathlib.Path`
    :param path: Path to a HAR document or a JSON list of responses.
    :returns: The parsed manifest, suitable for :func:`extract_references`.

    """
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _response_text(entry):
    text = entry.get('text')
    if text is None:
        text = entry.get('content')
    if text is None:
        text = entry.get('body')
    if isinstance(text, bytes):
        return text.decode('utf-8')
    if isinstance(text, str) and entry.get('encoding') == 'base64':
        return base64.b64decode(text).decode('utf-8')
    return text or ''


def _normalize_har_entry(entry):
    response = entry.get('response', {})
    content = response.get('content', {})
    text = content.get('text', '')
    if content.get('encoding') == 'base64' and text:
        text = base64.b64decode(text).decode('utf-8')
    return {
        'url': entry.get('request', {}).get('url'),
        'status': response.get('status'),
        'redirect_url': response.get('redirectURL') or None,
        'mime_type': content.get('mimeType'),
        'text': text,
    }


def _normalize_manifest(manifest):
    if isinstance(manifest, dict) and 'log' in manifest:
        entries = [
            _normalize_har_entry(entry)
            for entry in manifest['log'].get('entries', [])]
    elif isinstance(manifest, dict):
        entries = []
        for url, value in manifest.items():
            if isinstance(value, dict):
                entry = dict(value)
                entry.setdefault('url', url)
                entries.append(entry)
            else:
                entries.append({'url': url, 'text': value})
    else:
        entries = []
        for entry in manifest:
            if 'request' in entry or 'response' in entry:
                entries.append(_normalize_har_entry(entry))
            else:
                entries.append({
                    'url': entry.get('url'),
                    'status': entry.get('status'),
                    'redirect_url': (
                        entry.get('redirect_url') or
                        entry.get('redirectURL')),
                    'mime_type': (
                        entry.get('mime_type') or entry.get('mimeType')),
                    'text': _response_text(entry),
                })
    return {entry['url']: entry for entry in entries if entry.get('url')}


def _is_stylesheet(url, entry):
    mime_type = (entry.get('mime_type') or '').lower()
    if 'text/css' in mime_type:
        return True
    path = url.split('?', 1)[0].split('#', 1)[0]
    return path.endswith('.css')


def resolve_manifest_url(manifest, url):
    """Return the final response URL for *url* according to *manifest*.

    Redirect chains (3xx responses with a ``redirect_url``) are followed as
    long as their targets are part of the manifest. The last known address is
    returned when a redirect leaves the captured responses.
    """
    entries = _normalize_manifest(manifest)
    seen = set()
    while url in entries and url not in seen:
        seen.add(url)
        redirect = entries[url].get('redirect_url')
        if not redirect:
            break
        url = redirect
    return url


def _first_significant(tokens):
    for token in tokens:
        if token.type not in ('whitespace', 'comment'):
            return token
    return None


def _string_argument(function):
    token = _first_significant(function.arguments)
    if token is not None and token.type == 'string':
        return token
    return None


_STRING_RE = re.compile(
    r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", re.DOTALL)
_URL_RE = re.compile(
    r"url\(\s*(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\""
    r"|(?:\\[^\n]|[^)\\\s])*)\s*\)",
    re.IGNORECASE | re.DOTALL)


class _SourceText:
    """A stylesheet body and the mapping from token positions to its text.

    The text is normalized exactly like the tokenizer does internally so that
    :attr:`~tinycss2.ast.Node.source_line` and
    :attr:`~tinycss2.ast.Node.source_column` line up with its offsets.

    """

    __slots__ = ['line_offsets', 'text']

    def __init__(self, text):
        self.text = (
            text.replace('\0', '\uFFFD')
            .replace('\r\n', '\n').replace('\r', '\n')
            .replace('\f', '\n'))
        self.line_offsets = [0]
        for match in re.finditer('\n', self.text):
            self.line_offsets.append(match.end())

    def offset(self, node):
        return (
            self.line_offsets[node.source_line - 1] +
            node.source_column - 1)

    def raw(self, node, pattern):
        start = self.offset(node)
        match = pattern.match(self.text, start)
        if match is None:
            return node.serialize()
        return match.group(0)


def _raw_spelling(source, src):
    if source.type == 'string':
        return src.raw(source, _STRING_RE)
    return src.raw(source, _URL_RE)


def _import_reference(rule, src):
    if rule.content is not None:
        return None
    source = _first_significant(rule.prelude)
    if source is None:
        return None
    if source.type in ('string', 'url') or (
            source.type == 'function' and source.lower_name == 'url'):
        if source.type == 'function' and _string_argument(source) is None:
            return None
        raw_url = _raw_spelling(source, src)
        decoded = source.value if source.type != 'function' else (
            _string_argument(source).value)
    else:
        return None
    source_index = next(
        index for index, token in enumerate(rule.prelude)
        if token is source)
    condition = ' '.join(
        serialize(rule.prelude[source_index + 1:]).split())
    return (raw_url, decoded, condition or None)


def _walk_tokens(tokens, src, references, follow_imports=False):
    for node in tokens:
        node_type = node.type
        if node_type == 'url':
            references.append(
                ('resource', src.raw(node, _URL_RE), node.value,
                 None, False))
        elif node_type == 'function':
            if node.lower_name == 'url':
                string = _string_argument(node)
                if string is not None:
                    references.append(
                        ('resource', src.raw(node, _URL_RE), string.value,
                         None, False))
            _walk_tokens(node.arguments, src, references)
        elif node_type in ('() block', '[] block', '{} block'):
            _walk_tokens(node.content, src, references)
        elif node_type == 'declaration':
            _walk_tokens(node.value, src, references)
        elif node_type == 'qualified-rule':
            _walk_blocks(node.content, src, references)
        elif node_type == 'at-rule':
            if node.lower_at_keyword == 'import':
                import_ref = _import_reference(node, src)
                if import_ref is not None:
                    references.append(
                        ('import', *import_ref, follow_imports))
                continue
            _walk_tokens(node.prelude, src, references)
            if node.content is not None:
                _walk_blocks(node.content, src, references)


def _walk_blocks(tokens, src, references):
    _walk_tokens(
        parse_blocks_contents(tokens, skip_comments=True),
        src, references)


def _extract_direct_references(css_text, cache):
    if css_text not in cache:
        src = _SourceText(css_text)
        direct = []
        for rule in parse_stylesheet(src.text, skip_comments=True):
            if rule.type == 'error':
                continue
            if rule.type == 'at-rule' and rule.lower_at_keyword == 'import':
                import_ref = _import_reference(rule, src)
                if import_ref is not None:
                    direct.append(('import', *import_ref, True))
            elif rule.type == 'at-rule':
                _walk_tokens(rule.prelude, src, direct)
                if rule.content is not None:
                    _walk_blocks(rule.content, src, direct)
            elif rule.type == 'qualified-rule':
                _walk_blocks(rule.content, src, direct)
        cache[css_text] = direct
    return cache[css_text]


def extract_references(manifest, roots=None):
    """Extract every CSS reference relationship from a response manifest.

    :type manifest: :obj:`dict` or :obj:`list`
    :param manifest:
        A HAR document, a mapping of URLs to stylesheet text, or a list of
        response dictionaries. Each response may define ``url``, ``status``,
        ``redirect_url`` (or ``redirectURL``), ``mime_type`` (or
        ``mimeType``) and a body through ``text``, ``content`` or ``body``.
    :type roots: :term:`iterable` of :obj:`str`
    :param roots:
        URLs of the stylesheets to start from. By default every captured
        stylesheet is used as a root, so identical bodies served from
        different directories produce distinct targets.
    :returns: A list of :class:`ResourceReference` objects, in source order.

    Identical stylesheet bodies are parsed once. The relative references they
    contain are then resolved again against the final URL of each response
    serving that body, so the same reference under two base URLs yields two
    relationships with different targets. Relationships in unrelated import
    branches are never deduplicated; only an import that loops back into its
    own ancestor chain is marked as a cycle and stops that branch.

    """
    entries = _normalize_manifest(manifest)

    def final_url(url, seen=frozenset()):
        while url in entries and url not in seen:
            seen = seen | {url}
            redirect = entries[url].get('redirect_url')
            if not redirect:
                return url
            url = redirect
        return url

    sheets = {}
    for url, entry in entries.items():
        final = final_url(url)
        if _is_stylesheet(final, entries.get(final, entry)):
            sheets[final] = entries.get(final, entry)

    if roots is None:
        root_urls = list(sheets)
    else:
        root_urls = [final_url(url) for url in roots]

    parse_cache = {}
    result = []

    def walk(url, chain):
        entry = sheets[url]
        direct = _extract_direct_references(
            _response_text(entry), parse_cache)
        for kind, raw_url, decoded, condition, followable in direct:
            target = final_url(urljoin(url, decoded))
            is_cycle = kind == 'import' and target in chain
            result.append(ResourceReference(
                kind, url, raw_url, target, condition, is_cycle))
            if (followable and not is_cycle and target in sheets):
                walk(target, chain + (target,))

    for root in root_urls:
        if root in sheets:
            walk(root, (root,))
    return result
