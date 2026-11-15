"""Map parser positions back to the original byte stream.

tinycss2 works on decoded Unicode text. Before the tokenizer even runs, two
transformations have already happened and every position reported by the
parser is relative to their result:

* The byte stream is decoded with :func:`webencodings.decode`, using
  ``errors='replace'``: undecodable byte sequences become ``\\uFFFD`` and
  multi-byte encodings collapse several bytes into one character.
* The tokenizer normalizes line endings (``\\r\\n``, ``\\r`` and ``\\f``
  become ``\\n``) and turns NUL characters into ``\\uFFFD``.

Both transformations are many-to-one: a single character position in the
parsed text may correspond to a range of several bytes, and the original
boundaries cannot be reconstructed from the text alone. This module keeps the
bookkeeping required to slice the *original* bytes conservatively, while
reporting which transformation made the range ambiguous.

"""

import codecs
import contextvars

from .parser import parse_blocks_contents
from .tokenizer import parse_component_value_list

_REPLACE_HANDLER = 'tinycss2_byte_map_replace'
_replacement_events = contextvars.ContextVar(
    'tinycss2_replacement_events', default=None)


def _record_replacement(exc):
    _replacement_events.get().append((exc.start, exc.end))
    return ('�', exc.end)


codecs.register_error(_REPLACE_HANDLER, _record_replacement)


class ByteRange:
    """A half-open range of byte offsets in the original CSS byte string.

    .. attribute:: start

        Offset of the first byte that may belong to the mapped text.

    .. attribute:: end

        Offset one past the last byte that may belong to the mapped text.

    Ranges are conservative on purpose: when a decoded character spans several
    bytes, or a normalized line ending covers ``\\r\\n``, every involved byte
    is included so that slicing the original data never truncates the source
    of a declaration.

    """

    __slots__ = ['end', 'start']

    def __init__(self, start, end):
        self.start = start
        self.end = end

    def __repr__(self):
        return f'<ByteRange {self.start}:{self.end}>'

    def __eq__(self, other):
        return (
            isinstance(other, ByteRange) and
            self.start == other.start and self.end == other.end)

    def __iter__(self):
        return iter((self.start, self.end))


class MappingNote:
    """Explains why a mapped byte range is many-to-one.

    .. attribute:: kind

        One of ``'decode-replacement'`` (undecodable bytes replaced by
        ``\\uFFFD`` while decoding), ``'nul-replacement'`` (a NUL character
        turned into ``\\uFFFD`` by the tokenizer) or
        ``'newline-normalization'`` (``\\r\\n``, ``\\r`` or ``\\f`` folded
        into ``\\n``).

    .. attribute:: detail

        A short machine-readable string, such as ``'CRLF'``, ``'CR'``,
        ``'FF'``, ``'NUL'`` or the name of the codec that rejected the bytes.

    .. attribute:: byte_range

        The bytes collapsed into the single parsed character.

    """

    __slots__ = ['byte_range', 'detail', 'kind']

    def __init__(self, kind, detail, byte_range):
        self.kind = kind
        self.detail = detail
        self.byte_range = byte_range

    def __repr__(self):
        return f'<MappingNote {self.kind} {self.detail} {self.byte_range}>'

    def __eq__(self, other):
        return (
            isinstance(other, MappingNote) and
            self.kind == other.kind and self.detail == other.detail and
            self.byte_range == other.byte_range)

    def __hash__(self):
        return hash((self.kind, self.detail, self.byte_range.start,
                     self.byte_range.end))


class RejectedDeclarationRange:
    """Byte locations of a rejected declaration and the recovery point.

    .. attribute:: error

        The :class:`~tinycss2.ast.ParseError` emitted by the parser.

    .. attribute:: error_text_range

        The ``(start, end)`` character offsets of the rejected declaration in
        the normalized decoded text. The terminating ``;`` is excluded.

    .. attribute:: error_byte_range

        The :class:`ByteRange` covering the rejected declaration in the
        original bytes.

    .. attribute:: next_declaration

        The first :class:`~tinycss2.ast.Declaration` parsed after the error,
        or :obj:`None` if the declaration list ends before a valid one is
        found. At-rules and qualified rules are not declarations and do not
        count as recovery points.

    .. attribute:: next_text_range

        Same as :attr:`error_text_range` for :attr:`next_declaration`,
        or :obj:`None`.

    .. attribute:: next_byte_range

        Same as :attr:`error_byte_range` for :attr:`next_declaration`,
        or :obj:`None`.

    .. attribute:: notes

        Tuple of :class:`MappingNote` objects intersecting either range,
        documenting every many-to-one step involved.

    """

    __slots__ = [
        'error', 'error_byte_range', 'error_text_range', 'next_byte_range',
        'next_declaration', 'next_text_range', 'notes']

    def __init__(self, error, error_text_range, error_byte_range,
                 next_declaration, next_text_range, next_byte_range, notes):
        self.error = error
        self.error_text_range = error_text_range
        self.error_byte_range = error_byte_range
        self.next_declaration = next_declaration
        self.next_text_range = next_text_range
        self.next_byte_range = next_byte_range
        self.notes = tuple(notes)


def _decode_valid_segment(payload, segment_start, segment_end,
                          bom_length, codec_name, starts, ends,
                          is_replacement, decoded_parts):
    """Decode a byte range known to contain no invalid byte sequence."""
    decoder = codecs.getincrementaldecoder(codec_name)(errors='replace')
    group_start = None
    for offset in range(segment_start, segment_end):
        chunk = decoder.decode(bytes((payload[offset],)))
        if not chunk:
            if group_start is None:
                group_start = offset
            continue
        start = group_start if group_start is not None else offset
        group_start = None
        end = offset + 1
        for char in chunk:
            decoded_parts.append(char)
            starts.append(start + bom_length)
            ends.append(end + bom_length)
            is_replacement.append(False)
    chunk = decoder.decode(b'', final=True)
    for char in chunk:
        start = group_start if group_start is not None else segment_end
        decoded_parts.append(char)
        starts.append(start + bom_length)
        ends.append(segment_end + bom_length)
        is_replacement.append(False)


def _decode_with_byte_map(payload, bom_length, encoding):
    """Decode bytes and attribute every output character to source bytes.

    A full decode with a recording error handler pinpoints the exact byte
    spans replaced with ``\\uFFFD``. The remaining, guaranteed-valid segments
    are then decoded byte by byte so that multi-byte sequences group their
    leader bytes with the byte that completes them. Keeping invalid spans
    separate prevents a replacement from stealing bytes belonging to the
    following valid character.

    """
    token = _replacement_events.set([])
    try:
        decoded, _ = encoding.codec_info.decode(
            payload, errors=_REPLACE_HANDLER)
        events = list(_replacement_events.get())
    finally:
        _replacement_events.reset(token)

    codec_name = encoding.codec_info.name
    starts = []
    ends = []
    is_replacement = []
    decoded_parts = []
    cursor = 0
    for event_start, event_end in events:
        _decode_valid_segment(
            payload, cursor, event_start, bom_length, codec_name,
            starts, ends, is_replacement, decoded_parts)
        decoded_parts.append('�')
        starts.append(event_start + bom_length)
        ends.append(event_end + bom_length)
        is_replacement.append(True)
        cursor = event_end
    _decode_valid_segment(
        payload, cursor, len(payload), bom_length, codec_name,
        starts, ends, is_replacement, decoded_parts)

    reconstructed = ''.join(decoded_parts)
    if len(starts) != len(decoded) or reconstructed != decoded:  # pragma: no cover
        raise RuntimeError(
            'Byte map decoder diverged from the reference decoder')
    return reconstructed, starts, ends, is_replacement


class SourceMap:
    """Bidirectional bookkeeping between normalized text and original bytes.

    Instances are usually built with :meth:`from_bytes`. They expose the
    :attr:`normalized_text` that the tokenizer consumes and translate
    character ranges in that text back to :class:`ByteRange` objects in the
    original input, annotated with :class:`MappingNote` objects.

    """

    __slots__ = [
        '_char_byte_end', '_char_byte_start', '_line_starts',
        '_newline_notes', '_norm_to_dec', '_nul_norm', '_replacement_norm',
        'bom_length', 'encoding', 'normalized_text']

    def __init__(self, css_bytes, encoding, bom_length=0):
        payload = css_bytes[bom_length:]
        decoded, starts, ends, is_replacement = _decode_with_byte_map(
            payload, bom_length, encoding)

        norm_chars = []
        norm_to_dec = []
        replacement_norm = set()
        nul_norm = set()
        newline_notes = {}
        decoded_index = 0
        normalized_index = 0
        while decoded_index < len(decoded):
            if decoded.startswith('\r\n', decoded_index):
                norm_chars.append('\n')
                norm_to_dec.append(decoded_index)
                newline_notes[normalized_index] = 'CRLF'
                decoded_index += 2
            elif decoded[decoded_index] == '\r':
                norm_chars.append('\n')
                norm_to_dec.append(decoded_index)
                newline_notes[normalized_index] = 'CR'
                decoded_index += 1
            elif decoded[decoded_index] == '\f':
                norm_chars.append('\n')
                norm_to_dec.append(decoded_index)
                newline_notes[normalized_index] = 'FF'
                decoded_index += 1
            else:
                char = decoded[decoded_index]
                norm_chars.append(char)
                norm_to_dec.append(decoded_index)
                if is_replacement[decoded_index]:
                    replacement_norm.add(normalized_index)
                if char == '\0':
                    nul_norm.add(normalized_index)
                decoded_index += 1
            normalized_index += 1

        self.encoding = encoding
        self.bom_length = bom_length
        self.normalized_text = ''.join(norm_chars)
        self._norm_to_dec = norm_to_dec
        self._char_byte_start = starts
        self._char_byte_end = ends
        self._replacement_norm = replacement_norm
        self._nul_norm = nul_norm
        self._newline_notes = newline_notes
        self._line_starts = self._build_line_starts()

    @classmethod
    def from_bytes(cls, css_bytes, protocol_encoding=None,
                   environment_encoding=None):
        """Build a map using the same encoding rules as the parser.

        The BOM, ``@charset`` rule, protocol encoding and environment
        encoding are honored exactly as in
        :func:`tinycss2.decode_stylesheet_bytes`.

        """
        from .bytes import _detect_bom, _lookup_encoding

        fallback = _lookup_encoding(
            css_bytes, protocol_encoding, environment_encoding)
        bom_encoding, bom_length, _ = _detect_bom(css_bytes)
        encoding = bom_encoding or fallback
        return cls(css_bytes, encoding, bom_length)

    def _build_line_starts(self):
        line_starts = [0]
        for index, char in enumerate(self.normalized_text):
            if char == '\n':
                line_starts.append(index + 1)
        return line_starts

    def line_column_to_index(self, line, column):
        """Return the character index for a 1-based line/column position."""
        return self._line_starts[line - 1] + column - 1

    def _note_at(self, normalized_index):
        decoded_index = self._norm_to_dec[normalized_index]
        byte_range = ByteRange(
            self._char_byte_start[decoded_index],
            self._char_byte_end[decoded_index])
        if normalized_index in self._replacement_norm:
            return MappingNote(
                'decode-replacement', self.encoding.name, byte_range)
        if normalized_index in self._nul_norm:
            return MappingNote('nul-replacement', 'NUL', byte_range)
        kind = self._newline_notes.get(normalized_index)
        if kind is not None:
            return MappingNote('newline-normalization', kind, byte_range)
        return None

    def locate_range(self, start, end):
        """Map a half-open character range in the normalized text.

        Returns a ``(byte_range, notes)`` tuple. The byte range covers every
        source byte of the mapped characters, including bytes hidden by
        replacements and line ending normalization. Notes are deduplicated
        and ordered by their position in the text.

        """
        if start < 0 or end <= start or end > len(self.normalized_text):
            raise ValueError(
                f'Invalid range {start}:{end} for text of length '
                f'{len(self.normalized_text)}')
        first_decoded = self._norm_to_dec[start]
        last_decoded = self._norm_to_dec[end - 1]
        byte_range = ByteRange(
            self._char_byte_start[first_decoded],
            self._char_byte_end[last_decoded])
        notes = []
        seen = set()
        for index in range(start, end):
            note = self._note_at(index)
            if note is not None and note not in seen:
                notes.append(note)
                seen.add(note)
        return byte_range, tuple(notes)


def _token_index(source_map, token):
    return source_map.line_column_to_index(
        token.source_line, token.source_column)


def _token_end(source_map, tokens, index):
    """Return the text index just past token ``index`` in ``tokens``."""
    if index + 1 < len(tokens):
        return _token_index(source_map, tokens[index + 1])
    return len(source_map.normalized_text)


def _segment_declarations(source_map, tokens, span_end):
    """Split a component value list at top-level ``;`` tokens.

    Semicolons nested in ``()``, ``[]``, ``{}`` blocks and functions stay
    inside their block, so a ``;`` wrapped in parentheses can never terminate
    a declaration or trigger error recovery.

    Returns ``(node, text_start, text_end)`` triples where the end excludes
    the terminating ``;`` when one was present. Nodes come from a single full
    parse of the list, so the returned :class:`~tinycss2.ast.ParseError`
    objects are exactly the ones the parser reports.

    """
    significant = [
        token for token in tokens
        if token.type not in ('whitespace', 'comment')]
    boundaries = []
    current_indices = []
    for index, token in enumerate(significant):
        if token == ';':
            if current_indices:
                start = _token_index(
                    source_map, significant[current_indices[0]])
                end = _token_index(source_map, token)
                boundaries.append((current_indices, start, end))
                current_indices = []
        else:
            current_indices.append(index)
    if current_indices:
        start = _token_index(
            source_map, significant[current_indices[0]])
        last_index = current_indices[-1]
        last_token = significant[last_index]
        if last_token.type == 'error':
            end = span_end
        else:
            end = min(
                _token_index(source_map, last_token) +
                len(last_token.serialize()),
                span_end)
        boundaries.append((current_indices, start, end))

    parsed_nodes = [
        node for node in parse_blocks_contents(
            tokens, skip_whitespace=True, skip_comments=True)
        if node.type not in ('whitespace', 'comment')]
    if len(parsed_nodes) != len(boundaries):  # pragma: no cover
        raise RuntimeError(
            'Declaration segments do not match the parsed nodes')
    return [
        (node, start, end)
        for node, (_, start, end) in zip(parsed_nodes, boundaries)]


def locate_declaration_list_errors(source_map, tokens, span_end=None):
    """Locate rejected declarations and their recovery points in original bytes.

    :type source_map: :class:`SourceMap`
    :param source_map: The map describing the parsed text.
    :type tokens: iterable of component values
    :param tokens:
        The component values of a declaration list, for example the
        :attr:`~tinycss2.ast.QualifiedRule.content` of a style rule.
        Positions are interpreted against the source map's
        :attr:`~SourceMap.normalized_text`.
    :param span_end:
        Character index one past the declaration list in the normalized text.
        Defaults to the end of the text.
    :returns: A list of :class:`RejectedDeclarationRange`.

    The rejected declaration range covers the whole segment consumed by the
    parser up to but excluding the top-level ``;`` where recovery happens, so
    slicing the original bytes never includes the following valid items.

    """
    if span_end is None:
        span_end = len(source_map.normalized_text)
    located = _segment_declarations(source_map, list(tokens), span_end)
    results = []
    for index, (node, start, end) in enumerate(located):
        if node.type != 'error':
            continue
        error_byte_range, error_notes = source_map.locate_range(start, end)
        next_entry = next(
            ((candidate, candidate_start, candidate_end)
             for candidate, candidate_start, candidate_end in located[index + 1:]
             if candidate.type == 'declaration'),
            None)
        notes = list(error_notes)
        if next_entry is None:
            next_declaration = None
            next_text_range = None
            next_byte_range = None
        else:
            next_declaration, next_start, next_end = next_entry
            next_text_range = (next_start, next_end)
            next_byte_range, next_notes = source_map.locate_range(
                next_start, next_end)
            seen = set(notes)
            for note in next_notes:
                if note not in seen:
                    notes.append(note)
                    seen.add(note)
        results.append(RejectedDeclarationRange(
            error=node,
            error_text_range=(start, end),
            error_byte_range=error_byte_range,
            next_declaration=next_declaration,
            next_text_range=next_text_range,
            next_byte_range=next_byte_range,
            notes=notes))
    return results


def locate_stylesheet_declaration_errors(source_map):
    """Find rejected declarations in every ``{}`` block of a whole document.

    Top-level style rules and nested at-rules are both covered: every
    curly-brackets block is treated as a declaration list, and blocks nested
    inside it are walked recursively. Returns a flat list of
    :class:`RejectedDeclarationRange`.

    """
    results = []

    def walk(tokens):
        for index, token in enumerate(tokens):
            if token.type != '{} block':
                continue
            block_end = _token_end(source_map, tokens, index)
            # Exclude the closing '}' when the block actually has one;
            # an unterminated block runs to the end of the text.
            if block_end < len(source_map.normalized_text):
                content_end = block_end - 1
            else:
                content_end = block_end
            results.extend(locate_declaration_list_errors(
                source_map, token.content, content_end))
            walk(token.content)

    walk(parse_component_value_list(source_map.normalized_text))
    return results
