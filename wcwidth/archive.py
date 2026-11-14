"""
Line-oriented log archiving with a single-line terminal model.

A log collector often receives progress output that reuses one screen line, for example
``"下载 99%"`` immediately followed by a carriage return and ``"完成"``.  Archiving the raw
stream keeps the stale tail of the old line.  This module replays the explicitly supported
control effects -- carriage return, backspace, erase-in-line, and ordinary text -- onto a cell
grid, so the archived lines match what a terminal would finally display.

Every cell also remembers the absolute position in the input stream from which its visible
text was written, which makes it possible to tell an overwritten cell apart from a stale one.

The parser is streaming-safe: text may be :meth:`LineArchive.feed` in one whole line or one
decoded byte at a time, and the resulting archive is identical.  Incomplete escape sequences
and the final grapheme cluster of a chunk are buffered until the rest of the stream arrives,
so a combining mark or a ZWJ emoji split across chunks paints the same cell as the whole line.
"""
from __future__ import annotations

# std imports
import re
from typing import Iterator, List, NamedTuple, Optional, Tuple

# local
from ._width import width
from .grapheme import iter_graphemes

# Erase in Line (EL): CSI n K.  Missing or 0 erases from the cursor to end of line,
# 1 erases from line start to the cursor (inclusive), 2 erases the whole line.
_ERASE_LINE_PATTERN = re.compile(r'\x1b\[(\d*)K')

# Erase Character (ECH): CSI n X erases n cells starting at the cursor without moving it.
_ERASE_CHARS_PATTERN = re.compile(r'\x1b\[(\d+)X')

# Line feeds: LF, VT, FF, and the CRLF pair.  Vertical positioning is out of scope, each of
# these simply finalizes the current line and starts a fresh one.
_LINE_FEED_PATTERN = re.compile(r'\r\n|[\n\v\f]')

# A zero-width escape sequence consumed without changing the cell grid.
_ZERO_WIDTH_SEQUENCE_PATTERN = re.compile(
    r'\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]|'
    r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|'
    r'\x1b_[^\x1b\x07]*(?:\x07|\x1b\\)|'
    r'\x1bP[^\x1b\x07]*(?:\x07|\x1b\\)|'
    r'\x1b\^[^\x1b\x07]*(?:\x07|\x1b\\)|'
    r'\x1b[()](?s:.)|'
    r'\x1b[\x20-\x2f]+[\x30-\x7e]|'
    # Fe two-byte escapes, excluding the CSI/OSC/DCS/PM/APC introducers P, [, ], ^, _.
    r'\x1b[\x40-\x4fQ-Z\\]'
)

# Suffix of a chunk that may be the beginning of an unfinished escape sequence.
_INCOMPLETE_ESCAPE_PATTERN = re.compile(
    r'\x1b(?:'
    r'\[[\x20-\x3f]*|'
    r'[\]P\^_][^\x07\x1b]*(?:\x1b\\?)?|'
    r'[\x20-\x2f]+'
    r')?$'
)


def _is_control(char: str) -> bool:
    """Whether *char* is a C0/C1 control code (including DEL) handled outside text runs."""
    code = ord(char)
    return code < 0x20 or code == 0x7f or 0x80 <= code < 0xa0


class _Cell(NamedTuple):
    """One terminal cell: the visible grapheme, its display width, and its source position."""

    text: str
    width: int
    source: int


class LineArchive:
    r"""
    Accumulate a terminal stream into archived visible lines.

    The archive models a single unbounded line at a time: ordinary text paints cells at the
    cursor, carriage return (``\r``) moves the cursor to column 0, backspace (``\b``) moves it
    one cell left, and erase-in-line sequences (``CSI n K``) blank cells.  Line feed, vertical
    tab, form feed, and CRLF finalize the current line into the archive and start a new one.

    A wide character occupies two cells.  When a narrow character later overwrites either half,
    the other half is blanked as well, so an orphaned continuation cell can never survive.

    :param columns: Maximum number of columns per line, default 80.  Text reaching the margin
        wraps onto a new archived line, as a terminal would.
    :param tabsize: Tab stop width, default 8.  Set to 0 to treat tabs as zero-width.
    :param fillchar: Single-width character written into blanked or split cells, default space.
    :param ambiguous_width: Width of East Asian Ambiguous characters, default 1.
    :param term_program: Terminal identifier for width table overrides, default ``False``.

    :raises ValueError: If *columns* is not positive, or *fillchar* is not a single-width cell.
    """

    # pylint: disable=too-many-arguments,too-many-instance-attributes

    def __init__(
        self,
        columns: int = 80,
        *,
        tabsize: int = 8,
        fillchar: str = ' ',
        ambiguous_width: int = 1,
        term_program: bool | str = False,
    ) -> None:
        if columns <= 0:
            raise ValueError(f"columns must be positive, got {columns!r}")
        if len(fillchar) != 1 or width_of(fillchar, ambiguous_width, term_program) != 1:
            raise ValueError(f"fillchar must have display width 1, got {fillchar!r}")
        self.columns = columns
        self.tabsize = tabsize
        self.fillchar = fillchar
        self.ambiguous_width = ambiguous_width
        self.term_program = term_program
        self._lines: List[str] = []
        self._line_sources: List[Tuple[Optional[int], ...]] = []
        self._cells: dict[int, _Cell] = {}
        self._line_width = 0
        self._col = 0
        # Stream position at which the buffered tail (_pending) begins.
        self._position = 0
        # Chunk edge: an unfinished escape sequence or the last grapheme of a chunk.
        self._pending = ''

    @property
    def cursor(self) -> int:
        """Current cursor column on the active line."""
        return self._col

    @property
    def lines(self) -> Tuple[str, ...]:
        """Archived lines followed by the visible active line, without trailing blanks."""
        self._flush_pending()
        return tuple(self._lines) + (self.render(),)

    @property
    def line_sources(self) -> Tuple[Tuple[Optional[int], ...], ...]:
        """Per-cell source positions for every entry of :attr:`lines`, aligned column by column."""
        self._flush_pending()
        return tuple(self._line_sources) + (self.cell_sources(),)

    def render(self) -> str:
        """Return the visible text of the active line with trailing blank cells trimmed."""
        self._flush_pending()
        end = self._trimmed_width()
        return self._render(end)

    def cell_sources(self) -> Tuple[Optional[int], ...]:
        """
        Source position last written into each cell of the active line.

        Entries are absolute offsets of the input stream.  A cell that has never been written,
        or that was later erased, reports ``None``.  Trailing blank cells are omitted, matching
        :meth:`render`.
        """
        self._flush_pending()
        return self._sources(self._trimmed_width())

    def cell_source(self, col: int) -> Optional[int]:
        """Source position last written into cell *col*, or ``None`` when blank."""
        self._flush_pending()
        cell = self._cells.get(col)
        return None if cell is None else cell.source

    def write(self, text: str) -> "LineArchive":
        """Paint ordinary *text* (no control effects) at the cursor and return self."""
        self._flush_pending()
        offset = 0
        for grapheme in iter_graphemes(text):
            self._write_grapheme(grapheme, self._position + offset)
            offset += len(grapheme)
        self._position += offset
        return self

    def backspace(self) -> "LineArchive":
        """Move the cursor one cell left (never past column 0) and return self."""
        self._flush_pending()
        self._backspace()
        self._position += 1
        return self

    def carriage_return(self) -> "LineArchive":
        """Move the cursor to column 0 without erasing anything and return self."""
        self._flush_pending()
        self._col = 0
        self._position += 1
        return self

    def erase_line(self, mode: int = 0) -> "LineArchive":
        """
        Erase part of the active line and return self.

        Mode 0 (default) erases from the cursor to end of line, mode 1 from line start to the
        cursor (inclusive), mode 2 erases the whole line.  The cursor does not move.

        :raises ValueError: If *mode* is not one of 0, 1, or 2.
        """
        self._flush_pending()
        self._erase_line(mode)
        self._position += 1
        return self

    def erase_chars(self, count: int = 1) -> "LineArchive":
        """Erase *count* cells starting at the cursor without moving it, return self.

        :raises ValueError: If *count* is negative.
        """
        self._flush_pending()
        self._erase_chars(count)
        self._position += 1
        return self

    def newline(self) -> "LineArchive":
        """Archive the active line and begin a fresh empty line, return self."""
        self._flush_pending()
        self._finalize_line()
        self._position += 1
        return self

    def feed(self, data: str) -> "LineArchive":
        r"""
        Apply a chunk of the terminal stream and return self.

        Carriage return, backspace, erase-in-line (``CSI n K``), erase-character (``CSI n X``),
        line feeds, and ordinary text are interpreted.  Other control characters and zero-width
        escape sequences do not affect the grid.  Chunks may split escape sequences, grapheme
        clusters, or CRLF pairs; the unfinished tail is buffered and completed by the next call.
        Call :meth:`finish` once at end of stream to flush the last buffered grapheme.
        """
        # pylint: disable=too-complex,too-many-branches,too-many-statements
        if not data:
            return self
        # Invariant: _position is the stream offset at which _pending begins, so data[k] here
        # lives at absolute stream offset base + k.
        data = self._pending + data
        self._pending = ''
        base = self._position
        idx = 0
        total = len(data)
        while idx < total:
            char = data[idx]

            line_feed_match = _LINE_FEED_PATTERN.match(data, idx)
            if line_feed_match is not None:
                self._finalize_line()
                self._col = 0
                idx = line_feed_match.end()
                continue

            if char == '\r':
                self._col = 0
                idx += 1
                continue

            if char == '\x08':
                self._backspace()
                idx += 1
                continue

            if char == '\t':
                tab_source = base + idx
                idx += 1
                if self.tabsize > 0:
                    next_tab = self._col + (self.tabsize - (self._col % self.tabsize))
                    while self._col < next_tab:
                        self._paint(self.fillchar, 1, self._col, tab_source)
                        self._col += 1
                        if self._col >= self.columns:
                            self._wrap_line()
                continue

            if char == '\x1b':
                idx = self._consume_escape(data, idx)
                continue

            if _is_control(char):
                # Unsupported C0/C1 control: zero-width, no effect on the grid.
                idx += 1
                continue

            run_end = idx
            while run_end < total and not _is_control(data[run_end]):
                run_end += 1
            graphemes = list(iter_graphemes(data[idx:run_end]))
            held = ''
            if run_end == total and graphemes:
                # The final cluster may gain combining marks or a ZWJ continuation in the next
                # chunk, so hold it back until the boundary is confirmed.
                held = graphemes[-1]
                graphemes = graphemes[:-1]
            run_offset = 0
            for grapheme in graphemes:
                self._write_grapheme(grapheme, base + idx + run_offset)
                run_offset += len(grapheme)
            idx += run_offset
            if held:
                self._pending = data[idx:]
                break

        # Everything except the buffered tail has been consumed from the stream.
        self._position = base + total - len(self._pending)
        return self

    def finish(self) -> Tuple[str, ...]:
        """Flush any buffered tail and return the complete tuple of archived lines."""
        self._flush_pending()
        return self.lines

    def _flush_pending(self) -> None:
        """Paint the grapheme or escape tail held back at the edge of the previous chunk."""
        if not self._pending:
            return
        pending = self._pending
        self._pending = ''
        if pending[0] == '\x1b':
            # The stream ended in the middle of an escape sequence: it never completed, so it
            # never took effect and must not leak its parameter bytes into the visible grid.
            return
        self.write(pending)

    def _consume_escape(self, data: str, idx: int) -> int:
        """Interpret an escape sequence at *idx*, returning the index just past it."""
        erase_match = _ERASE_LINE_PATTERN.match(data, idx)
        if erase_match is not None:
            mode_text = erase_match.group(1)
            self._erase_line(0 if mode_text == '' else int(mode_text))
            return erase_match.end()

        erase_chars_match = _ERASE_CHARS_PATTERN.match(data, idx)
        if erase_chars_match is not None:
            self._erase_chars(int(erase_chars_match.group(1)))
            return erase_chars_match.end()

        zero_width_match = _ZERO_WIDTH_SEQUENCE_PATTERN.match(data, idx)
        if zero_width_match is not None:
            return zero_width_match.end()

        if _INCOMPLETE_ESCAPE_PATTERN.match(data, idx) is not None:
            self._pending = data[idx:]
            return len(data)

        # Lone or unrecognized ESC: consume one byte without touching the grid.
        return idx + 1

    def _write_grapheme(self, grapheme: str, source: int) -> None:
        """Paint one grapheme cluster at the cursor, wrapping at the right margin."""
        grapheme_w = width_of(grapheme, self.ambiguous_width, self.term_program)
        if grapheme_w == 0:
            if _is_control(grapheme[0]):
                # A dangling control (such as a lone ESC at end of stream) paints nothing.
                return
            # A combining mark joins the cell under the cursor without moving it; the cell keeps
            # its original source position, where the base character began.
            cell = self._cells.get(self._col)
            if cell is not None:
                self._cells[self._col] = _Cell(cell.text + grapheme, cell.width, cell.source)
            return
        while self._col + grapheme_w > self.columns:
            if self._col >= self.columns:
                self._wrap_line()
            else:
                # A wide glyph does not fit in the final column: blank it and wrap first.
                self._paint(self.fillchar, 1, self._col, source)
                self._col += 1
                self._wrap_line()
        self._paint(grapheme, grapheme_w, self._col, source)
        self._col += grapheme_w

    def _backspace(self) -> None:
        """Move the cursor one cell left, clamped at column 0."""
        if self._col > 0:
            self._col -= 1

    def _erase_line(self, mode: int) -> None:
        """Blank cells selected by EL *mode* without moving the cursor."""
        if mode == 0:
            targets = range(self._col, self._line_width)
        elif mode == 1:
            targets = range(0, min(self._col + 1, self._line_width))
        elif mode == 2:
            targets = range(0, self._line_width)
        else:
            raise ValueError(f"erase line mode must be 0, 1, or 2, got {mode!r}")
        for col in list(targets):
            self._cells.pop(col, None)

    def _erase_chars(self, count: int) -> None:
        """Blank *count* cells starting at the cursor without moving it."""
        if count < 0:
            raise ValueError(f"erase count must be non-negative, got {count!r}")
        for col in range(self._col, min(self._col + count, self._line_width)):
            self._cells.pop(col, None)

    def _paint(self, text: str, cell_width: int, col: int, source: int) -> None:
        """Write *text* of *cell_width* cells at *col*, clearing any wide-char neighbors."""
        for offset in range(cell_width):
            target = col + offset
            left = self._cells.get(target - 1)
            if left is not None and left.width == 2:
                # The left half of a wide glyph is being overwritten: blank its continuation.
                self._cells[target - 1] = _Cell(self.fillchar, 1, source)
            current = self._cells.get(target)
            if current is not None and current.width == 2:
                # The right half of a wide glyph is being overwritten: blank its lead cell.
                self._cells[target + 1] = _Cell(self.fillchar, 1, source)
            self._cells.pop(target, None)
        self._cells[col] = _Cell(text, cell_width, source)
        end_col = col + cell_width
        if end_col > self._line_width:
            self._line_width = end_col

    def _wrap_line(self) -> None:
        """Move the cells at or beyond the margin onto a new active line."""
        self._lines.append(self._render(self.columns))
        self._line_sources.append(self._sources(self.columns))
        overflow = {
            col - self.columns: cell
            for col, cell in self._cells.items()
            if col >= self.columns
        }
        self._cells = overflow
        self._line_width = (max(overflow) + 1) if overflow else 0
        self._col -= self.columns

    def _finalize_line(self) -> None:
        """Archive the active line, trimming its trailing blank cells."""
        end = self._trimmed_width()
        self._lines.append(self._render(end))
        self._line_sources.append(self._sources(end))
        self._cells = {}
        self._line_width = 0
        self._col = 0

    def _sources(self, end: int) -> Tuple[Optional[int], ...]:
        """Source position of each column in ``[0, end)``, with ``None`` for blank cells."""
        return tuple(
            None if col not in self._cells else self._cells[col].source
            for col in range(end)
        )

    def _trimmed_width(self) -> int:
        """Rightmost column that still holds a cell, ignoring trailing blanks."""
        end = self._line_width
        while end > 0 and (end - 1) not in self._cells:
            end -= 1
        return end

    def _render(self, end: int) -> str:
        """Build the visible text of columns ``[0, end)`` from the cell grid."""
        parts: List[str] = []
        col = 0
        while col < end:
            cell = self._cells.get(col)
            if cell is None:
                parts.append(self.fillchar)
                col += 1
            else:
                parts.append(cell.text)
                col += cell.width
        return ''.join(parts)

    def __iter__(self) -> Iterator[str]:
        return iter(self.lines)

    def __repr__(self) -> str:
        return f"LineArchive(lines={self._lines!r}, active={self.render()!r})"


def width_of(text: str, ambiguous_width: int, term_program: bool | str) -> int:
    """Display width of a grapheme cluster under the archive's width options."""
    return width(
        text,
        control_codes='ignore',
        ambiguous_width=ambiguous_width,
        term_program=term_program,
    )


def archive_text(
    data: str,
    *,
    columns: int = 80,
    tabsize: int = 8,
    fillchar: str = ' ',
    ambiguous_width: int = 1,
    term_program: bool | str = False,
) -> Tuple[str, ...]:
    r"""
    Archive a terminal stream into the lines finally visible on screen.

    Carriage return, backspace, erase-in-line, and line feeds are replayed on a cell grid so
    overwritten progress text is resolved exactly as a terminal would show it, including stale
    characters that no later write or erase covered.  The result is independent of how *data* is
    chunked: feeding the whole string at once, one character at a time, or one decoded byte at a
    time through :meth:`LineArchive.feed` produces the same tuple.

    :param data: The raw terminal stream.
    :param columns: Columns per line before wrapping, default 80.
    :param tabsize: Tab stop width, default 8.
    :param fillchar: Character used for blanked or split cells, default space.
    :param ambiguous_width: Width of East Asian Ambiguous characters, default 1.
    :param term_program: Terminal identifier for width table overrides, default ``False``.
    :returns: The archived visible lines, including the unfinished final line.

    Example::

        >>> archive_text('下载 99%\r完成\n')
        ('完成 99%', '')
        >>> archive_text('下载 99%\r\x1b[K完成\n')
        ('完成', '')
    """
    archive = LineArchive(
        columns,
        tabsize=tabsize,
        fillchar=fillchar,
        ambiguous_width=ambiguous_width,
        term_program=term_program,
    )
    archive.feed(data)
    return archive.finish()
