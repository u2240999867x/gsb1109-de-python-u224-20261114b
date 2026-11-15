r"""
Convert SGR-styled terminal text into an event table of styled text intervals.

.. versionadded:: 0.9.5

An *event* is a contiguous run of visible text that maps to the same foreground value, background
value, and intensity. Each event reports its screen-column range and the byte range of the original
input, so a caller can place it on a terminal grid and recover the exact input bytes:

    >>> events = sgr_events('\x1b[31mred\x1b[39m plain', foreground={31: 'alert'})
    >>> [(event.start, event.end, event.text, event.foreground) for event in events]
    [(0, 3, 'red', 'alert'), (3, 9, ' plain', None)]

Colors are never interpreted by name here. The caller supplies mappings from SGR color parameters to
any value, for example ``{31: 'red', (38, 5, 196): 'critical'}``. A one-element color tuple such as
``(31,)`` may be mapped by either the tuple or the bare integer ``31``. Extended colors use their
parsed tuple, ``(38, 5, 196)`` or ``(38, 2, 255, 0, 0)``.

Intensity is mapped from SGR 1 (bold) and SGR 2 (dim). Keys are ``1``, ``2``, and ``(1, 2)`` for a
state where both are active. The strings ``'bold'``, ``'dim'``, ``'bold+dim'``, and ``'normal'`` are
also accepted.

The default foreground (SGR 39) and default background (SGR 49) are separate state transitions:
resetting one never clears the other. The caller may map the default color with the key ``None``
(or its SGR code, 39/49). Text styled with a color that has no mapping is still emitted; its mapped
value is simply ``None``.

:class:`SgrEventParser` is incremental. Input may be delivered in arbitrary chunks, including in
the middle of an escape sequence or a multi-byte UTF-8 character. A partial SGR sequence is held
until it is complete, so its parameters never take effect early.
"""

from __future__ import annotations

# std imports
import re

import typing

# local
from ._wcswidth import wcswidth, wcstwidth
from .escape_sequences import TEXT_SIZING_PATTERN
from .grapheme import iter_graphemes
from .sgr_state import _SGR_STATE_DEFAULT, _SGRState, _parse_sgr_params, _sgr_state_apply
from .text_sizing import TextSizing

__all__ = (
    'SgrEvent',
    'SgrEventParser',
    'iter_sgr_events',
    'sgr_events',
    'SgrSpan',
    'SgrParser',
    'iter_sgr_spans',
    'parse_sgr_events',
    'sgr_event_table',
)

# C1 controls are encoded as two UTF-8 bytes and are zero-width.
_C1_CONTROL_PATTERN = re.compile('[\x80-\x9f]')
_SGR_SEQUENCE_PATTERN = re.compile(r'\x1b\[([\d;:]*)m')
_CURSOR_PARAMS_PATTERN = re.compile(rb'[\d;:]*')

ESC = 0x1B
BEL = 0x07
ST_BACKSLASH = 0x5C

# UTF-8 sequence length from the first byte, indexed by the whole byte, or 0 for an invalid lead.
_UTF8_LENGTHS = bytes([1] * 0x80 + [0] * 0x40 + [2] * 0x20 + [3] * 0x10 + [4] * 8 + [0] * 8)


class SgrEvent(typing.NamedTuple):
    """
    One contiguous run of visible text with a mapped SGR style.

    :param start: First screen column occupied by the run (inclusive, 0-indexed).
    :param end: Screen column just past the run (exclusive).
    :param start_byte: First UTF-8 byte offset in the input (inclusive).
    :param end_byte: UTF-8 byte offset just past the run (exclusive).
    :param text: The visible text of the run, excluding escape sequences and control characters.
    :param foreground: Caller-mapped foreground value, or ``None`` when unmapped or default.
    :param background: Caller-mapped background value, or ``None`` when unmapped or default.
    :param intensity: Caller-mapped intensity value, or ``None`` when unmapped or absent.
    """

    start: int
    end: int
    start_byte: int
    end_byte: int
    text: str
    foreground: typing.Any = None
    background: typing.Any = None
    intensity: typing.Any = None

    @property
    def start_column(self) -> int:
        """Alias for :attr:`start`, the inclusive screen column."""
        return self.start

    @property
    def end_column(self) -> int:
        """Alias for :attr:`end`, the exclusive screen column."""
        return self.end

    @property
    def column_start(self) -> int:
        """Alias for :attr:`start`, the inclusive screen column."""
        return self.start

    @property
    def column_end(self) -> int:
        """Alias for :attr:`end`, the exclusive screen column."""
        return self.end

    @property
    def byte_start(self) -> int:
        """Alias for :attr:`start_byte`."""
        return self.start_byte

    @property
    def byte_end(self) -> int:
        """Alias for :attr:`end_byte`."""
        return self.end_byte

    @property
    def fg(self) -> typing.Any:
        """Alias for :attr:`foreground`."""
        return self.foreground

    @property
    def bg(self) -> typing.Any:
        """Alias for :attr:`background`."""
        return self.background

    def byte_slice(self, data: bytes) -> bytes:
        """
        Return this event's original input bytes.

        :param data: The same input bytes that were parsed.
        :returns: ``data[start_byte:end_byte]``.
        """
        return data[self.start_byte : self.end_byte]

    def byte_offset_at(self, column: int) -> int | None:
        """
        Return the absolute input byte offset of the character at *column*.

        The column is absolute, as reported by :attr:`start` and :attr:`end`. Wide characters map
        both of their columns to the byte offset where the character begins. ``column == end``
        returns :attr:`end_byte`.

        The event's own :attr:`text` is measured with the default width rules. Use
        :func:`byte_offset_at` over the whole event list when the text contains East Asian
        Ambiguous characters measured with ``ambiguous_width=2``, or ignored escape sequences
        inside the span.

        :param column: Screen column to look up.
        :returns: Byte offset, or ``None`` when *column* is outside this event.
        """
        if not self.start <= column <= self.end:
            return None
        if column == self.end:
            return self.end_byte
        offset = self.start_byte
        current = self.start
        for cluster in iter_graphemes(self.text):
            width = wcswidth(cluster, ambiguous_width=1)
            if width < 0:
                width = 0
            if current <= column < current + width:
                return offset
            current += width
            offset += len(cluster.encode('utf-8'))
        return self.end_byte

    def as_dict(self) -> dict[str, typing.Any]:
        """Return the event as a plain dictionary."""
        return {
            'start': self.start,
            'end': self.end,
            'start_byte': self.start_byte,
            'end_byte': self.end_byte,
            'text': self.text,
            'foreground': self.foreground,
            'background': self.background,
            'intensity': self.intensity,
        }


class _TextRun:
    """Accumulator for a single event that is still open."""

    __slots__ = ('start_col', 'start_byte', 'end_byte', 'style', 'chars')

    def __init__(self, start_col: int, start_byte: int, style: tuple[typing.Any, ...]) -> None:
        self.start_col = start_col
        self.start_byte = start_byte
        self.end_byte = start_byte
        self.style = style
        # (character, absolute start byte, absolute end byte)
        self.chars: list[tuple[str, int, int]] = []

    def __bool__(self) -> bool:
        return bool(self.chars)

    def append(self, char: str, start_byte: int, end_byte: int) -> None:
        """Append one decoded character with its original byte span."""
        self.chars.append((char, start_byte, end_byte))
        self.end_byte = end_byte


class SgrEventParser:
    r"""
    Incrementally turn SGR-styled text into :class:`SgrEvent` runs.

    :param foreground: Mapping of SGR foreground parameters to caller values.
    :param background: Mapping of SGR background parameters to caller values.
    :param intensity: Mapping of SGR intensity parameters (1, 2, or ``(1, 2)``) to caller values.
    :param ambiguous_width: Width of East Asian Ambiguous characters, 1 or 2.
    :param tabsize: Tab stop interval, or 0 to make tabs zero-width.
    :param term_program: Terminal identifier for width corrections, as accepted by
        :func:`wcwidth.wcstwidth`.

    Chunks passed to :meth:`feed` may split an escape sequence or a UTF-8 character in any place.
    No partial sequence changes the active style: such bytes are buffered until the sequence is
    complete. Text before an incomplete sequence is emitted normally under the style in effect at
    that point.

    Every escape sequence and control character ends the current event, so an event's byte range
    covers only its own text. A later run that maps to the same style is reported as a new event.

    Example::

        >>> parser = SgrEventParser(foreground={31: 'alert'})
        >>> parser.feed('a\x1b[3')
        [SgrEvent(start=0, end=1, start_byte=0, end_byte=1, text='a', ...)]
        >>> parser.feed('1mb')
        []
        >>> parser.finish()
        [SgrEvent(start=1, end=2, start_byte=6, end_byte=7, text='b', foreground='alert', ...)]
    """

    def __init__(
        self,
        foreground: typing.Mapping[typing.Any, typing.Any] | None = None,
        background: typing.Mapping[typing.Any, typing.Any] | None = None,
        intensity: typing.Mapping[typing.Any, typing.Any] | None = None,
        *,
        ambiguous_width: int = 1,
        tabsize: int = 8,
        term_program: bool | str = False,
        fg: typing.Mapping[typing.Any, typing.Any] | None = None,
        bg: typing.Mapping[typing.Any, typing.Any] | None = None,
        foreground_map: typing.Mapping[typing.Any, typing.Any] | None = None,
        background_map: typing.Mapping[typing.Any, typing.Any] | None = None,
        intensity_map: typing.Mapping[typing.Any, typing.Any] | None = None,
    ) -> None:
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        self.foreground = _first_mapping(foreground, fg, foreground_map)
        self.background = _first_mapping(background, bg, background_map)
        self.intensity = _first_mapping(intensity, intensity_map)
        self.ambiguous_width = ambiguous_width
        self.tabsize = tabsize
        self.term_program = term_program

        self._state: _SGRState = _SGR_STATE_DEFAULT
        self._column = 0
        self._buffer = bytearray()
        # Absolute UTF-8 byte offset of self._buffer[0] in the full input.
        self._offset = 0
        self._run: _TextRun | None = None
        self._closed = False

    @property
    def column(self) -> int:
        """The current screen column, after all text and sequences fed so far."""
        if self._run is None:
            return self._column
        text = ''.join(char for char, _s, _e in self._run.chars)
        return self._run.start_col + self._measure(text)

    @property
    def state(self) -> _SGRState:
        """The active SGR state, after all complete sequences fed so far."""
        return self._state

    def feed(self, data: str | bytes | bytearray | memoryview) -> list[SgrEvent]:
        r"""
        Feed the next chunk of text, returning every event that is now complete.

        :param data: Text or UTF-8 bytes. Bytes are not re-encoded, so their offsets are reported
            exactly as supplied.
        :returns: Newly completed :class:`SgrEvent` values, in input order.
        :raises RuntimeError: If called after :meth:`finish`.
        """
        if self._closed:
            raise RuntimeError('feed() called after finish()')
        if isinstance(data, str):
            data = data.encode('utf-8')
        else:
            data = bytes(data)

        self._buffer += data
        events: list[SgrEvent] = []
        pos = 0
        size = len(self._buffer)

        while pos < size:
            byte = self._buffer[pos]

            if byte == ESC:
                end = _scan_escape(self._buffer, pos)
                if end is None:
                    # The text before an incomplete sequence is already determined: its style and
                    # columns cannot change. Emit it now, and hold the partial bytes so they never
                    # take effect early.
                    self._flush(events)
                    break
                self._handle_sequence(self._buffer[pos:end], events)
                pos = end
                continue

            if byte < 0x20 or byte == 0x7F:
                self._handle_control(byte, events)
                pos += 1
                continue

            # Ordinary text up to the next C0 control or ESC. ESC and C0 bytes cannot occur
            # inside a valid UTF-8 multi-byte character, so scanning bytes is safe.
            stop = pos + 1
            while stop < size and 0x20 <= self._buffer[stop] != ESC and self._buffer[stop] != 0x7F:
                stop += 1
            consumed = self._decode_text(bytes(self._buffer[pos:stop]), self._offset + pos, events)
            if consumed == 0:
                break  # incomplete UTF-8 character at the end of the buffer
            pos += consumed

        if pos:
            del self._buffer[:pos]
            self._offset += pos
        return events

    def finish(self) -> list[SgrEvent]:
        r"""
        Finish parsing and return any remaining events.

        An incomplete escape sequence at the end of the input is discarded and never changes the
        active style. Left-over plain text (for example an incomplete UTF-8 character) is decoded
        with replacement characters so text is not silently lost.

        :returns: Remaining :class:`SgrEvent` values.
        """
        if self._closed:
            return []
        self._closed = True
        events: list[SgrEvent] = []
        if self._buffer and self._buffer[0] != ESC:
            self._decode_text(bytes(self._buffer), self._offset, events, final=True)
        self._buffer = bytearray()
        self._flush(events)
        return events

    def _decode_text(
        self,
        data: bytes,
        base_offset: int,
        events: list[SgrEvent],
        *,
        final: bool = False,
    ) -> int:
        """
        Decode and consume the longest valid UTF-8 prefix of *data*.

        :returns: Number of bytes consumed. Zero means an incomplete character begins the data and
            more bytes are needed; ``final=True`` consumes such trailing bytes as replacements.
        """
        pos = 0
        size = len(data)
        while pos < size:
            lead = data[pos]
            length = 1 if lead < 0x80 else _UTF8_LENGTHS[lead]

            if length == 0:
                # An invalid lead byte cannot start a character; it is replaced in place.
                self._append_char('�', base_offset + pos, base_offset + pos + 1, events)
                pos += 1
                continue

            if pos + length > size:
                if not final:
                    break
                self._append_char('�', base_offset + pos, base_offset + pos + 1, events)
                pos += 1
                continue

            try:
                char = data[pos : pos + length].decode('utf-8')
            except UnicodeDecodeError:
                # A truncated or otherwise invalid sequence consumes one byte.
                self._append_char('�', base_offset + pos, base_offset + pos + 1, events)
                pos += 1
                continue

            if _C1_CONTROL_PATTERN.fullmatch(char):
                self._flush(events)
            else:
                self._append_char(char, base_offset + pos, base_offset + pos + length, events)
            pos += length
        return pos

    def _append_char(
        self, char: str, start_byte: int, end_byte: int, events: list[SgrEvent]
    ) -> None:
        if self._run is None:
            self._run = _TextRun(self._column, start_byte, self._style_snapshot())
        self._run.append(char, start_byte, end_byte)

    def _style_snapshot(self) -> tuple[typing.Any, ...]:
        """Return the mapped style of the currently active SGR state."""
        return (
            _map_color(self._state.foreground, self.foreground, default_code=39),
            _map_color(self._state.background, self.background, default_code=49),
            _map_intensity(self._state, self.intensity),
        )

    def _measure(self, text: str) -> int:
        if not text:
            return 0
        if self.term_program:
            width = wcstwidth(
                text, ambiguous_width=self.ambiguous_width, term_program=self.term_program
            )
        else:
            width = wcswidth(text, ambiguous_width=self.ambiguous_width)
        return width if width > 0 else 0

    def _handle_control(self, byte: int, events: list[SgrEvent]) -> None:
        self._flush(events)
        if byte == 0x09:  # HT
            if self.tabsize > 0:
                self._column += self.tabsize - (self._column % self.tabsize)
        elif byte == 0x08:  # BS
            if self._column:
                self._column -= 1
        elif byte in (0x0A, 0x0B, 0x0C, 0x0D):  # LF, VT, FF, CR start a new line
            self._column = 0
        # Every other C0 byte is zero-width.

    def _handle_sequence(self, sequence: bytes, events: list[SgrEvent]) -> None:
        # Every escape sequence ends the current text run. Keeping a run open across one would
        # make its [start_byte, end_byte) cover the sequence bytes, so a byte reverse lookup would
        # no longer return just the text of the event.
        self._flush(events)

        if (sgr_match := _SGR_SEQUENCE_PATTERN.fullmatch(sequence.decode('latin-1'))) is not None:
            params = tuple(_parse_sgr_params(sgr_match.group()))
            self._state = _sgr_state_apply(self._state, params)
            return

        if sequence.startswith(b'\x1b[') and sequence.endswith((b'C', b'D', b'G')):
            self._handle_cursor(sequence)
            return

        if sequence.startswith(b'\x1b]66;'):
            text = sequence.decode('utf-8', 'replace')
            if (match := TEXT_SIZING_PATTERN.fullmatch(text)) is not None:
                self._column += TextSizing.from_match(match).display_width(self.ambiguous_width)
            return

        # Other zero-width sequences (OSC 8 hyperlinks, titles, character sets, ...) do not change
        # the mapped style or column.

    def _handle_cursor(self, sequence: bytes) -> None:
        final = sequence[-1:]
        params = sequence[2:-1]
        if not _CURSOR_PARAMS_PATTERN.fullmatch(params):
            return
        try:
            amount = int(params) if params else 1
        except ValueError:
            return
        if final == b'C':
            self._column += amount
        elif final == b'D':
            self._column = max(0, self._column - amount)
        else:  # b'G', horizontal position absolute (1-indexed)
            self._column = max(0, amount - 1)

    def _flush(self, events: list[SgrEvent]) -> None:
        if self._run is None or not self._run.chars:
            self._run = None
            return
        run = self._run
        self._run = None
        text = ''.join(char for char, _start, _end in run.chars)
        width = self._measure(text)
        end_col = run.start_col + width
        self._column = end_col
        events.append(
            SgrEvent(
                start=run.start_col,
                end=end_col,
                start_byte=run.start_byte,
                end_byte=run.end_byte,
                text=text,
                foreground=run.style[0],
                background=run.style[1],
                intensity=run.style[2],
            )
        )


def _first_mapping(
    *mappings: typing.Mapping[typing.Any, typing.Any] | None,
) -> typing.Mapping[typing.Any, typing.Any]:
    for mapping in mappings:
        if mapping is not None:
            return mapping
    return {}


def _map_color(
    code: tuple[int, ...] | None,
    mapping: typing.Mapping[typing.Any, typing.Any],
    default_code: int,
) -> typing.Any:
    """Map a parsed color tuple, accepting bare integers and the default-code alias."""
    if code is None:
        if None in mapping:
            return mapping[None]
        return mapping.get(default_code)

    candidates: list[typing.Any] = [code]
    if len(code) == 1:
        candidates.append(code[0])
    elif len(code) >= 2 and code[1] == 2:
        # ITU T.416 form carries a colour-space element the legacy RGB form omits.
        if len(code) == 6:
            candidates.append((code[0], code[1], code[3], code[4], code[5]))
            candidates.append((code[3], code[4], code[5]))
        elif len(code) == 5:
            candidates.append((code[2], code[3], code[4]))
    elif len(code) == 3 and code[1] == 5:
        candidates.append(code[2])

    for candidate in candidates:
        if candidate in mapping:
            return mapping[candidate]
    return None


def _map_intensity(state: _SGRState, mapping: typing.Mapping[typing.Any, typing.Any]) -> typing.Any:
    """Map active bold/dim attributes, accepting the combined ``(1, 2)`` key."""
    active = tuple(code for code, enabled in ((1, state.bold), (2, state.dim)) if enabled)
    if not active:
        candidates: list[typing.Any] = [None, 0, 'normal']
    elif active == (1,):
        candidates = [1, 'bold', (1,)]
    elif active == (2,):
        candidates = [2, 'dim', (2,)]
    else:
        candidates = [(1, 2), 'bold+dim', 'bold dim', 'dim+bold']
    for candidate in candidates:
        if candidate in mapping:
            return mapping[candidate]
    if len(active) == 2:
        # A caller mapping only the single intensities still gets a sensible combined value.
        mapped = tuple(mapping[code] for code in active if code in mapping)
        if len(mapped) == 2:
            return mapped
    return None


def _scan_escape(data: bytes | bytearray, start: int) -> int | None:
    """
    Return the end offset of the escape sequence beginning at *start*.

    ``None`` means the sequence is incomplete and more bytes are required before it may be applied.
    The accepted forms follow :data:`wcwidth.escape_sequences.ZERO_WIDTH_PATTERN`.
    """
    # pylint: disable=too-many-return-statements,too-many-branches,too-complex
    size = len(data)
    if start + 1 >= size:
        return None
    kind = data[start + 1]

    if kind == ord('['):  # CSI
        pos = start + 2
        while pos < size and 0x30 <= data[pos] <= 0x3F:
            pos += 1
        while pos < size and 0x20 <= data[pos] <= 0x2F:
            pos += 1
        if pos >= size:
            return None
        if 0x40 <= data[pos] <= 0x7E:
            return pos + 1
        return start + 2  # malformed CSI: ESC '[' is zero-width

    if kind in (ord(']'), ord('P'), ord('_'), ord('^')):  # OSC, DCS, APC, PM
        pos = start + 2
        while pos < size:
            if data[pos] == BEL:
                return pos + 1
            if data[pos] == ESC:
                if pos + 1 >= size:
                    return None
                if data[pos + 1] == ST_BACKSLASH:
                    return pos + 2
                return pos
            pos += 1
        return None

    if kind in (ord('('), ord(')')):  # character set designation
        return None if start + 2 >= size else start + 3

    if 0x20 <= kind <= 0x2F:  # nF sequence
        pos = start + 1
        while pos < size and 0x20 <= data[pos] <= 0x2F:
            pos += 1
        if pos >= size:
            return None
        if 0x30 <= data[pos] <= 0x7E:
            return pos + 1
        return pos

    if 0x30 <= kind <= 0x7E:  # Fe, Fp, or Fs sequence
        return start + 2

    return start + 1  # lone ESC


def byte_offset_at(events: typing.Sequence[SgrEvent], data: bytes, column: int) -> int | None:
    r"""
    Reverse a screen column to the input byte offset it came from.

    :param events: Events produced from *data*.
    :param data: The original input bytes.
    :param column: Absolute screen column to look up.
    :returns: Absolute byte offset, or ``None`` when no event covers *column*.

    Example::

        >>> text = '\x1b[31mred\x1b[0m'
        >>> events = sgr_events(text)
        >>> byte_offset_at(events, text.encode(), 1)
        6
    """
    for event in events:
        if event.start <= column < event.end:
            return event.byte_offset_at(column)
    return None


def iter_sgr_events(
    text: str | bytes | bytearray,
    *,
    foreground: typing.Mapping[typing.Any, typing.Any] | None = None,
    background: typing.Mapping[typing.Any, typing.Any] | None = None,
    intensity: typing.Mapping[typing.Any, typing.Any] | None = None,
    ambiguous_width: int = 1,
    tabsize: int = 8,
    term_program: bool | str = False,
    fg: typing.Mapping[typing.Any, typing.Any] | None = None,
    bg: typing.Mapping[typing.Any, typing.Any] | None = None,
    foreground_map: typing.Mapping[typing.Any, typing.Any] | None = None,
    background_map: typing.Mapping[typing.Any, typing.Any] | None = None,
    intensity_map: typing.Mapping[typing.Any, typing.Any] | None = None,
) -> typing.Iterator[SgrEvent]:
    r"""
    Iterate over styled text intervals of *text*.

    This is a convenience wrapper around :class:`SgrEventParser` for input that is already complete.

    :param text: Text or UTF-8 bytes to parse.
    :param foreground: Mapping of SGR foreground parameters to caller values.
    :param background: Mapping of SGR background parameters to caller values.
    :param intensity: Mapping of SGR intensity parameters to caller values.
    :param ambiguous_width: Width of East Asian Ambiguous characters, 1 or 2.
    :param tabsize: Tab stop interval, or 0 to make tabs zero-width.
    :param term_program: Terminal identifier for width corrections.
    :yields: :class:`SgrEvent` values in input order.

    Example::

        >>> [(event.start, event.end, event.text) for event in
        ...  iter_sgr_events('中\x1b[32mok\x1b[0m!', foreground={32: 'ok'})]
        [(0, 2, '中'), (2, 4, 'ok'), (4, 5, '!')]
    """
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    parser = SgrEventParser(
        foreground=foreground,
        background=background,
        intensity=intensity,
        ambiguous_width=ambiguous_width,
        tabsize=tabsize,
        term_program=term_program,
        fg=fg,
        bg=bg,
        foreground_map=foreground_map,
        background_map=background_map,
        intensity_map=intensity_map,
    )
    yield from parser.feed(text)
    yield from parser.finish()


def sgr_events(
    text: str | bytes | bytearray,
    *,
    foreground: typing.Mapping[typing.Any, typing.Any] | None = None,
    background: typing.Mapping[typing.Any, typing.Any] | None = None,
    intensity: typing.Mapping[typing.Any, typing.Any] | None = None,
    ambiguous_width: int = 1,
    tabsize: int = 8,
    term_program: bool | str = False,
    fg: typing.Mapping[typing.Any, typing.Any] | None = None,
    bg: typing.Mapping[typing.Any, typing.Any] | None = None,
    foreground_map: typing.Mapping[typing.Any, typing.Any] | None = None,
    background_map: typing.Mapping[typing.Any, typing.Any] | None = None,
    intensity_map: typing.Mapping[typing.Any, typing.Any] | None = None,
) -> list[SgrEvent]:
    """
    Return the styled text intervals of *text* as a list.

    Parameters are the same as :func:`iter_sgr_events`.

    Example::

        >>> events = sgr_events('\x1b[1;31mALERT\x1b[0m ok', foreground={31: 'red'},
        ...                     intensity={1: 'high'})
        >>> [(event.text, event.foreground, event.intensity) for event in events]
        [('ALERT', 'red', 'high'), (' ok', None, None)]
    """
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    return list(
        iter_sgr_events(
            text,
            foreground=foreground,
            background=background,
            intensity=intensity,
            ambiguous_width=ambiguous_width,
            tabsize=tabsize,
            term_program=term_program,
            fg=fg,
            bg=bg,
            foreground_map=foreground_map,
            background_map=background_map,
            intensity_map=intensity_map,
        )
    )


# Descriptive aliases, so callers may use the terminology of their own event table.
SgrSpan = SgrEvent
SgrParser = SgrEventParser
iter_sgr_spans = iter_sgr_events
parse_sgr_events = sgr_events
sgr_event_table = sgr_events
