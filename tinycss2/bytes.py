from dataclasses import dataclass

from webencodings import UTF8, Encoding, decode, lookup

from .parser import parse_stylesheet

#: The parsed bytes started with a byte order mark.
BOM = 'bom'
#: The encoding came from the protocol (e.g. an HTTP ``charset`` parameter).
PROTOCOL = 'protocol'
#: The encoding came from the leading ``@charset`` rule.
CHARSET = 'charset'
#: The encoding was inherited from the stylesheet that linked this one.
ENVIRONMENT = 'environment'
#: No hint was available and UTF-8 was used.
DEFAULT = 'default'

ENCODING_SOURCES = (BOM, PROTOCOL, CHARSET, ENVIRONMENT, DEFAULT)


@dataclass(frozen=True)
class EncodingDecision:
    """The encoding used to decode a byte string, and why it was used.

    .. attribute:: encoding

        The :class:`webencodings.Encoding` object that was used.

    .. attribute:: source

        Where the encoding came from. One of :data:`BOM`, :data:`PROTOCOL`,
        :data:`CHARSET`, :data:`ENVIRONMENT` or :data:`DEFAULT`.

    .. attribute:: label

        The original encoding label, for the :data:`PROTOCOL` and
        :data:`CHARSET` sources. :obj:`None` otherwise.

    .. attribute:: environment_encoding

        The environment encoding supplied by the caller, if any. It is kept
        even when a BOM, a protocol encoding or an ``@charset`` rule took
        precedence, so that the context of a parse can be recorded.

    """
    encoding: Encoding
    source: str
    label: str | None = None
    environment_encoding: Encoding | None = None


@dataclass(frozen=True)
class DecodedStylesheet:
    """A decoded stylesheet together with its full encoding decision.

    The object is iterable as ``(css_unicode, encoding)`` for compatibility
    with :func:`decode_stylesheet_bytes`.

    .. attribute:: css

        The decoded Unicode string.

    .. attribute:: encoding

        The :class:`webencodings.Encoding` object that was used.

    .. attribute:: encoding_source

        Where the encoding came from, see :class:`EncodingDecision`.

    .. attribute:: encoding_label

        The original label for protocol and ``@charset`` encodings.

    .. attribute:: decision

        The :class:`EncodingDecision` object.

    """
    css: str
    encoding: Encoding
    encoding_source: str
    encoding_label: str | None
    decision: EncodingDecision

    def __iter__(self):
        # Keep old-style ``css, encoding = ...`` unpacking working.
        return iter((self.css, self.encoding))


def _detect_bom_encoding(css_bytes):
    if css_bytes.startswith(b'\xEF\xBB\xBF'):
        return UTF8
    if css_bytes.startswith(b'\xFF\xFE'):
        return lookup('utf-16le')
    if css_bytes.startswith(b'\xFE\xFF'):
        return lookup('utf-16be')
    return None


def _detect_charset_encoding(css_bytes):
    if not css_bytes.startswith(b'@charset "'):
        return None
    # 10 is len(b'@charset "')
    # 100 is arbitrary so that no encoding label is more than 100-10 bytes.
    end_quote = css_bytes.find(b'"', 10, 100)
    if end_quote == -1 or not css_bytes.startswith(b'";', end_quote):
        return None
    label = css_bytes[10:end_quote].decode('latin1')
    encoding = lookup(label)
    if encoding is None:
        return None
    if encoding.name in ('utf-16be', 'utf-16le'):
        # Non-ASCII-compatible encodings can not be declared by @charset.
        return UTF8, label
    return encoding, label


def _coerce_environment_encoding(environment_encoding):
    if not environment_encoding:
        return None
    if hasattr(environment_encoding, 'codec_info'):
        return environment_encoding
    encoding = lookup(environment_encoding)
    if encoding is None:
        raise LookupError(
            f'Unknown encoding label: {environment_encoding!r}')
    return encoding


def determine_encoding(css_bytes, protocol_encoding=None,
                       environment_encoding=None):
    """Return the :class:`EncodingDecision` used to decode ``css_bytes``.

    The precedence is the same as in the CSS syntax specification:
    BOM, then protocol encoding, then ``@charset``, then environment
    encoding, then UTF-8.

    """
    environment_encoding = _coerce_environment_encoding(environment_encoding)

    bom_encoding = _detect_bom_encoding(css_bytes)
    if bom_encoding is not None:
        return EncodingDecision(
            bom_encoding, BOM, environment_encoding=environment_encoding)

    if protocol_encoding:
        if hasattr(protocol_encoding, 'codec_info'):
            encoding = protocol_encoding
            label = protocol_encoding.name
        else:
            encoding = lookup(protocol_encoding)
            label = protocol_encoding
        if encoding is not None:
            return EncodingDecision(
                encoding, PROTOCOL, label,
                environment_encoding=environment_encoding)

    charset = _detect_charset_encoding(css_bytes)
    if charset is not None:
        encoding, label = charset
        return EncodingDecision(
            encoding, CHARSET, label,
            environment_encoding=environment_encoding)

    if environment_encoding is not None:
        return EncodingDecision(
            environment_encoding, ENVIRONMENT,
            environment_encoding=environment_encoding)

    return EncodingDecision(
        UTF8, DEFAULT, environment_encoding=environment_encoding)


def decode_stylesheet_bytes_detailed(css_bytes, protocol_encoding=None,
                                     environment_encoding=None):
    """Decode a stylesheet and report the encoding decision.

    This behaves exactly like :func:`decode_stylesheet_bytes`, but returns a
    :class:`DecodedStylesheet` that also records *why* the encoding was
    chosen.

    """
    decision = determine_encoding(
        css_bytes, protocol_encoding, environment_encoding)
    css_unicode, encoding = decode(css_bytes, decision.encoding)
    return DecodedStylesheet(
        css_unicode, encoding, decision.source, decision.label, decision)


def decode_stylesheet_bytes(css_bytes, protocol_encoding=None,
                            environment_encoding=None):
    """Determine the character encoding of a CSS stylesheet and decode it.

    This is based on the presence of a :abbr:`BOM (Byte Order Mark)`,
    a ``@charset`` rule, and encoding meta-information.

    :type css_bytes: :obj:`bytes`
    :param css_bytes: A CSS byte string.
    :type protocol_encoding: :obj:`str`
    :param protocol_encoding:
        The encoding label, if any, defined by HTTP or equivalent protocol.
        (e.g. via the ``charset`` parameter of the ``Content-Type`` header.)
    :type environment_encoding: :class:`webencodings.Encoding`
    :param environment_encoding:
        The `environment encoding
        <https://www.w3.org/TR/css-syntax/#environment-encoding>`_, if any.
    :returns:
        A 2-tuple of a decoded Unicode string and the
        :class:`webencodings.Encoding` object that was used.

    """
    decoded = decode_stylesheet_bytes_detailed(
        css_bytes, protocol_encoding, environment_encoding)
    return decoded.css, decoded.encoding


def parse_stylesheet_bytes_detailed(css_bytes, protocol_encoding=None,
                                    environment_encoding=None,
                                    skip_comments=False,
                                    skip_whitespace=False):
    """Parse a stylesheet from bytes and report the encoding decision.

    This behaves exactly like :func:`parse_stylesheet_bytes`, but returns a
    ``(rules, decision)`` tuple where ``decision`` is the
    :class:`EncodingDecision` that records which encoding was used and why.

    """
    decision = determine_encoding(
        css_bytes, protocol_encoding, environment_encoding)
    decoded = decode_stylesheet_bytes_detailed(
        css_bytes, protocol_encoding, environment_encoding)
    stylesheet = parse_stylesheet(
        decoded.css, skip_comments, skip_whitespace)
    return stylesheet, decision


def parse_stylesheet_bytes(css_bytes, protocol_encoding=None,
                           environment_encoding=None,
                           skip_comments=False, skip_whitespace=False):
    """Parse :diagram:`stylesheet` from bytes,
    determining the character encoding as web browsers do.

    This is used when reading a file or fetching a URL.
    The character encoding is determined from the initial bytes
    (a :abbr:`BOM (Byte Order Mark)` or a ``@charset`` rule)
    as well as the parameters. The ultimate fallback is UTF-8.

    :type css_bytes: :obj:`bytes`
    :param css_bytes: A CSS byte string.
    :type protocol_encoding: :obj:`str`
    :param protocol_encoding:
        The encoding label, if any, defined by HTTP or equivalent protocol.
        (e.g. via the ``charset`` parameter of the ``Content-Type`` header.)
    :type environment_encoding: :class:`webencodings.Encoding`
    :param environment_encoding:
        The `environment encoding`_, if any.
    :type skip_comments: :obj:`bool`
    :param skip_comments:
        Ignore CSS comments at the top-level of the stylesheet.
        If the input is a string, ignore all comments.
    :type skip_whitespace: :obj:`bool`
    :param skip_whitespace:
        Ignore whitespace at the top-level of the stylesheet.
        Whitespace is still preserved
        in the :attr:`~tinycss2.ast.QualifiedRule.prelude`
        and the :attr:`~tinycss2.ast.QualifiedRule.content` of rules.
    :returns:
        A ``(rules, encoding)`` tuple.

        * ``rules`` is a list of
          :class:`~tinycss2.ast.QualifiedRule`,
          :class:`~tinycss2.ast.AtRule`,
          :class:`~tinycss2.ast.Comment` (if ``skip_comments`` is false),
          :class:`~tinycss2.ast.WhitespaceToken`
          (if ``skip_whitespace`` is false),
          and :class:`~tinycss2.ast.ParseError` objects.
        * ``encoding`` is the :class:`webencodings.Encoding` object
          that was used.
          If ``rules`` contains an ``@import`` rule, this is
          the `environment encoding`_ for the imported stylesheet.

    .. _environment encoding:
            https://www.w3.org/TR/css-syntax/#environment-encoding

    .. code-block:: python

        response = urlopen('http://example.net/foo.css')
        rules, encoding = parse_stylesheet_bytes(
            css_bytes=response.read(),
            # Python 3.x
            protocol_encoding=response.info().get_content_type().get_param('charset'),
            # Python 2.x
            protocol_encoding=response.info().gettype().getparam('charset'),
        )
        for rule in rules:
            ...

    """
    decoded = decode_stylesheet_bytes_detailed(
        css_bytes, protocol_encoding, environment_encoding)
    stylesheet = parse_stylesheet(
        decoded.css, skip_comments, skip_whitespace)
    return stylesheet, decoded.encoding
