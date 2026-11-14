"""
Reconstruct logical records from a line-by-line screen transcript.

A capture side table supplies the column width and wrap markers that the physical screen
transcript alone cannot prove.  Trailing spaces are preserved exactly; ambiguous seams are
reported rather than guessed.
"""

from __future__ import annotations

# std imports
import enum
from dataclasses import dataclass

from typing import Literal, Optional, Sequence, Union

# local
from ._width import width as wcwidth_width


class WrapMarker(enum.Enum):
    """
    Wrap state of a physical line boundary in a screen transcript.

    :data:`SOFT` continues the logical record on the next physical line, :data:`HARD` ends it, and
    :data:`UNKNOWN` means the capture side table does not record a marker.
    """

    SOFT = 'soft'
    HARD = 'hard'
    UNKNOWN = 'unknown'

    @property
    def ambiguous(self) -> bool:
        """Whether this marker fails to prove either a soft or hard boundary."""
        return self is WrapMarker.UNKNOWN

    @classmethod
    def parse(cls, value: Union['WrapMarker', str, int, bool, None]) -> 'WrapMarker':
        """
        Coerce a side-table value to a :class:`WrapMarker`.

        ``True``, ``1`` and the strings ``'soft'``, ``'s'``, ``'wrap'``, ``'wrapped'``,
        ``'continuation'`` and ``'continued'`` mean :data:`SOFT`.  ``False``, ``0`` and ``'hard'``,
        ``'h'``, ``'newline'``, ``'break'`` and ``'ended'`` mean :data:`HARD`.  ``None``,
        ``'unknown'``, ``'u'``, ``'?'`` and the empty string mean :data:`UNKNOWN`.

        :param value: Side-table marker value.
        :raises ValueError: If the value is not a recognized marker.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, enum.Enum):
            value = value.value
        if value is None:
            return cls.UNKNOWN
        if isinstance(value, bool):
            return cls.SOFT if value else cls.HARD
        if isinstance(value, int):
            if value == 1:
                return cls.SOFT
            if value == 0:
                return cls.HARD
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ('soft', 's', 'wrap', 'wrapped', 'continuation', 'continued'):
                return cls.SOFT
            if normalized in ('hard', 'h', 'newline', 'break', 'ended'):
                return cls.HARD
            if normalized in ('', 'unknown', 'u', '?', 'missing'):
                return cls.UNKNOWN
        raise ValueError(f'invalid wrap marker {value!r}')


@dataclass(frozen=True)
class UnwrapSeam:
    """
    The boundary after one physical line of a screen transcript.

    :param index: Zero-based index of the physical line whose end is this seam.
    :param marker: The recorded :class:`WrapMarker`.
    :param trailing: Trailing horizontal whitespace of the physical line before the seam.
    :param proven: Whether the original transcript itself carries a hard line terminator here.
    """

    index: int
    marker: WrapMarker
    trailing: str = ''
    proven: bool = False

    @property
    def ambiguous(self) -> bool:
        """Whether the seam is unproven because its marker is missing."""
        return self.marker.ambiguous

    @property
    def padding(self) -> bool:
        """
        Whether trailing whitespace at this seam may be layout padding rather than content.

        Only a soft seam can leave a cell unused because the next grapheme cluster did not fit.
        The exact characters remain available in :attr:`trailing`; they are never silently
        discarded from the reconstructed record text.
        """
        return self.marker is WrapMarker.SOFT and bool(self.trailing)

    def __str__(self) -> str:
        """Return a short human-readable description."""
        kind = 'ambiguous' if self.ambiguous else self.marker.value
        return f'seam {self.index}: {kind}'


@dataclass(frozen=True, eq=False)
class LogicalRecord:
    """
    A reconstructed logical record and the physical lines that prove it.

    :param text: Exact reconstructed text, including every character of its physical lines.
    :param start: Zero-based index of the first physical line.
    :param end: Zero-based index of the last physical line.
    :param seams: Boundaries at either end of and inside this record.
    :param trailing: Trailing horizontal whitespace of the final physical line.
    :param segments: Exact physical line text joined to form :attr:`text`.
    """

    text: str
    start: int
    end: int
    seams: tuple[UnwrapSeam, ...] = ()
    trailing: str = ''
    segments: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        """Whether this record borders a seam with a missing marker."""
        return any(seam.ambiguous for seam in self.seams)

    @property
    def padding(self) -> bool:
        """Whether a soft seam inside this record leaves possible layout padding."""
        return any(seam.padding for seam in self.seams)

    def __str__(self) -> str:
        """Return the reconstructed text."""
        return self.text

    def __eq__(self, other):
        if isinstance(other, str):
            return self.text == other
        if isinstance(other, LogicalRecord):
            return ((self.text, self.start, self.end, self.seams, self.trailing, self.segments) ==
                    (other.text, other.start, other.end, other.seams, other.trailing,
                     other.segments))
        return NotImplemented

    __hash__ = None


class UnwrapResult(list):
    """
    Reconstructed logical record texts with their physical-line proof metadata.

    This is a :class:`list` of :attr:`LogicalRecord.text` strings, so it compares and iterates like
    the result of :func:`wrap`.  The richer :attr:`records`, :attr:`seams`, :attr:`lines`,
    :attr:`widths` and :attr:`markers` attributes preserve what was proven and what remains
    ambiguous.  Use :meth:`resolve` to supply a missing side-table marker without reflowing records
    outside the affected segment.
    """

    def __init__(self, records: tuple[LogicalRecord, ...], seams: tuple[UnwrapSeam, ...],
                 lines: tuple[str, ...], widths: tuple[Optional[int], ...],
                 markers: tuple[WrapMarker, ...], proven: tuple[bool, ...]) -> None:
        super().__init__(record.text for record in records)
        self.records = records
        self.seams = seams
        self.lines = lines
        self.widths = widths
        self.markers = markers
        self.proven = proven

    def texts(self) -> list[str]:
        """Return reconstructed record texts as a list of strings."""
        return list(self)

    def __eq__(self, other):
        if isinstance(other, tuple):
            return list(self) == list(other)
        return super().__eq__(other)

    __hash__ = None

    @property
    def ambiguous_seams(self) -> tuple[UnwrapSeam, ...]:
        """Return all seams whose side-table marker is missing."""
        return tuple(seam for seam in self.seams if seam.ambiguous)

    @property
    def complete(self) -> bool:
        """Whether every seam has an explicit soft or hard marker."""
        return not self.ambiguous_seams

    def resolve(self, index: int,
                marker: Union[WrapMarker, str, int, bool, None]) -> 'UnwrapResult':
        """
        Return a new result with the marker at one seam supplied.

        Only the record or pair of records touching *index* are rebuilt.  A soft marker joins the
        record ending at *index* with the following one; a hard marker splits a record at
        *index*.  Records away from that segment are reused unchanged.

        :param index: Zero-based index of the physical line whose end is the seam.
        :param marker: New side-table marker value, accepted by :meth:`WrapMarker.parse`.
        :returns: A new :class:`UnwrapResult`.
        :raises IndexError: If *index* is not a seam index.
        :raises ValueError: If a soft marker contradicts an explicit hard line terminator.
        """
        new_marker = WrapMarker.parse(marker)
        if not 0 <= index < len(self.markers):
            raise IndexError(f'seam index {index} out of range')
        if new_marker is WrapMarker.SOFT and self.proven[index]:
            raise ValueError(
                f'line {index} ends with an explicit hard newline and cannot be made soft')
        if new_marker is self.markers[index]:
            return self

        old_marker = self.markers[index]
        new_markers = list(self.markers)
        new_markers[index] = new_marker

        if old_marker is WrapMarker.SOFT:
            left = next(record for record in self.records
                        if record.start <= index < record.end)
            right = left
        else:
            left = next(record for record in self.records if record.end == index)
            right = next(record for record in self.records if record.start == index + 1)

        rebuilt = _build_records(self.lines, tuple(new_markers), self.proven,
                                 left.start, right.end)
        before = tuple(record for record in self.records if record.end < left.start)
        after = tuple(record for record in self.records if record.start > right.end)
        seams = tuple(
            UnwrapSeam(seam_index, seam_marker, _trailing_whitespace(self.lines[seam_index]),
                       self.proven[seam_index])
            for seam_index, seam_marker in enumerate(new_markers))
        return UnwrapResult(records=before + rebuilt + after, seams=seams, lines=self.lines,
                            widths=self.widths, markers=tuple(new_markers),
                            proven=self.proven)

    def resolve_many(
            self, markers: dict[int, Union[WrapMarker, str, int, bool, None]]
    ) -> 'UnwrapResult':
        """
        Return a new result with several seam markers supplied.

        This is equivalent to calling :meth:`resolve` repeatedly, but reconstructs each affected
        segment once.

        :param markers: Mapping of zero-based physical-line index to marker value.
        :returns: A new :class:`UnwrapResult`.
        :raises IndexError: If any key is not a seam index.
        """
        result = self
        for index in sorted(markers):
            result = result.resolve(index, markers[index])
        return result


def _coerce_lines(lines: Union[str, Sequence[str]]) -> tuple[tuple[str, ...], tuple[bool, ...]]:
    r"""
    Normalize transcript lines and report explicit hard line terminators.

    A string input is split at ``\n``, ``\r`` and ``\r\n``; these are physical-line separators,
    not proof of a logical break, so they are not reported as explicit hard boundaries.  A
    sequence element ending with one of these terminators keeps the terminator as clear hard
    evidence and the terminator is removed from the line text.  Other trailing spaces are never
    removed.
    """
    if isinstance(lines, str):
        raw_lines: list[str] = []
        start = 0
        index = 0
        while index < len(lines):
            char = lines[index]
            if char == '\r':
                raw_lines.append(lines[start:index])
                index += 2 if index + 1 < len(lines) and lines[index + 1] == '\n' else 1
                start = index
            elif char == '\n':
                raw_lines.append(lines[start:index])
                index += 1
                start = index
            else:
                index += 1
        if start < len(lines):
            raw_lines.append(lines[start:])
        string_terminated = False
    else:
        raw_lines = list(lines)
        string_terminated = True

    physical: list[str] = []
    explicit_hard: list[bool] = []
    for raw in raw_lines:
        if not isinstance(raw, str):
            raise TypeError(f'physical line must be str, not {type(raw).__name__}')
        if string_terminated and raw.endswith('\r\n'):
            physical.append(raw[:-2])
            explicit_hard.append(True)
        elif string_terminated and (raw.endswith('\n') or raw.endswith('\r')):
            physical.append(raw[:-1])
            explicit_hard.append(True)
        else:
            physical.append(raw)
            explicit_hard.append(False)
    return tuple(physical), tuple(explicit_hard)


def _coerce_widths(width: Union[int, Sequence[Optional[int]], None],
                   count: int) -> tuple[Optional[int], ...]:
    """Normalize a scalar or per-line width side table to one entry per physical line."""
    if width is None:
        return (None,) * count
    if isinstance(width, bool):
        raise ValueError(f'invalid capture width {width!r}')
    if isinstance(width, int):
        values: Sequence[Optional[int]] = [width] * count
    else:
        values = width
    if len(values) > count:
        raise ValueError(f'width table has {len(values)} entries for {count} physical lines')
    result: list[Optional[int]] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError(f'invalid capture width {value!r}')
        if value is not None and (not isinstance(value, int) or value < 1):
            raise ValueError(f'invalid capture width {value!r}')
        result.append(value)
    result.extend([None] * (count - len(result)))
    return tuple(result)


def _coerce_markers(markers: Union[Sequence[Union[WrapMarker, str, int, bool, None]],
                                   dict[int, Union[WrapMarker, str, int, bool, None]],
                                   None],
                    count: int, explicit_hard: tuple[bool, ...]) -> tuple[WrapMarker, ...]:
    """
    Normalize a marker side table to one entry per seam between physical lines.

    A table may contain one entry per physical line, in which case the final entry describes the
    end of the transcript and is ignored.  Shorter tables leave the remaining seams unknown.
    """
    seam_count = max(0, count - 1)
    if markers is None:
        values: list[Union[WrapMarker, str, int, bool, None]] = [None] * seam_count
    elif isinstance(markers, dict):
        values = [markers.get(index) for index in range(seam_count)]
    else:
        values = list(markers)
        if len(values) == count:
            values = values[:seam_count]
        if len(values) > seam_count:
            raise ValueError(
                f'marker table has {len(values)} entries for {seam_count} line boundaries')

    result = [WrapMarker.parse(value) for value in values]
    result.extend([WrapMarker.UNKNOWN] * (seam_count - len(result)))

    for index, hard in enumerate(explicit_hard[:seam_count]):
        if hard:
            if result[index] is WrapMarker.SOFT:
                raise ValueError(
                    f'line {index} ends with a hard newline but its marker is soft')
            result[index] = WrapMarker.HARD
    return tuple(result)


def _trailing_whitespace(text: str) -> str:
    """Return the trailing run of horizontal whitespace, excluding line terminators."""
    index = len(text)
    while index > 0 and text[index - 1].isspace() and text[index - 1] not in '\r\n':
        index -= 1
    return text[index:]


def _build_records(lines: tuple[str, ...], markers: tuple[WrapMarker, ...],
                   proven: tuple[bool, ...], start_line: int = 0,
                   end_line: Optional[int] = None) -> tuple[LogicalRecord, ...]:
    """Build logical records for the inclusive physical-line range, given resolved markers."""
    if end_line is None:
        end_line = len(lines) - 1
    if not lines or start_line > end_line:
        return ()

    def seam_at(index: int) -> UnwrapSeam:
        return UnwrapSeam(index, markers[index], _trailing_whitespace(lines[index]),
                          proven[index])

    records: list[LogicalRecord] = []
    record_start = start_line
    record_seams: list[UnwrapSeam] = []
    if start_line > 0:
        record_seams.append(seam_at(start_line - 1))
    for index in range(start_line, end_line):
        seam = seam_at(index)
        record_seams.append(seam)
        if markers[index] is WrapMarker.SOFT:
            continue
        records.append(LogicalRecord(text=''.join(lines[record_start:index + 1]),
                                     start=record_start, end=index,
                                     seams=tuple(record_seams),
                                     trailing=_trailing_whitespace(lines[index]),
                                     segments=lines[record_start:index + 1]))
        record_start = index + 1
        record_seams = [seam]

    if end_line < len(markers):
        record_seams.append(seam_at(end_line))

    records.append(LogicalRecord(text=''.join(lines[record_start:end_line + 1]),
                                 start=record_start, end=end_line,
                                 seams=tuple(record_seams),
                                 trailing=_trailing_whitespace(lines[end_line]),
                                 segments=lines[record_start:end_line + 1]))
    return tuple(records)


def unwrap(lines: Union[str, Sequence[str]],
           width: Union[int, Sequence[Optional[int]], None] = None,
           markers: Union[Sequence[Union[WrapMarker, str, int, bool, None]],
                          dict[int, Union[WrapMarker, str, int, bool, None]],
                          None] = None,
           *,
           validate_width: bool = True,
           control_codes: Literal['parse', 'strict', 'ignore'] = 'parse',
           tabsize: int = 8,
           ambiguous_width: int = 1,
           term_program: bool | str = False) -> UnwrapResult:
    r"""
    Reconstruct logical records from a line-by-line screen transcript.

    The transcript supplies the physical lines; *markers* is the side table that records whether
    each line was soft-wrapped.  A line that ends at the captured column does **not** prove a soft
    wrap: a hard newline can follow a full line just as it can follow a short one, and a soft wrap
    can leave layout space when the next grapheme cluster does not fit.  Reconstruction therefore
    joins lines only where the side table explicitly says :data:`WrapMarker.SOFT`, or where an
    explicit hard line terminator proves a :data:`WrapMarker.HARD` seam.  A missing marker leaves
    both adjacent records marked :attr:`LogicalRecord.ambiguous`; call
    :meth:`UnwrapResult.resolve` to supply it later.

    Trailing spaces are part of the physical line and are retained verbatim in the reconstructed
    text, including at hard boundaries and at the end of the transcript.  When a soft seam has
    trailing whitespace, :attr:`LogicalRecord.padding` and :attr:`UnwrapSeam.padding` flag that it
    may instead be unused layout space, and :attr:`UnwrapSeam.trailing` returns the exact
    characters.  Nothing is stripped or padded behind the caller's back.

    A string input is split at ``\n`` and ``\r\n``; these separators describe physical lines
    only.  A sequence entry ending with ``\n``, ``\r`` or ``\r\n`` carries a clear hard newline; the
    terminator is removed and that seam is hard even when the side table has no marker.  A
    side-table marker stating soft for such a line is contradictory and raises :exc:`ValueError`.

    :param lines: Physical lines of the screen transcript.  A sequence entry may include its
        original line terminator; trailing spaces are preserved.
    :param width: Capture width, either one integer for every line or a per-line sequence.  A
        ``None`` entry skips width validation for that line.
    :param markers: Side-table wrap markers, one per boundary between physical lines.  Values may
        be :class:`WrapMarker`, strings (``'soft'``, ``'hard'``, ``'unknown'``), integers
        (``1`` is soft, ``0`` is hard), booleans (``True`` is soft, ``False`` is hard), or
        ``None`` for a missing marker.  A mapping uses zero-based physical-line indices as keys.  A
        table may instead contain one entry per physical line; its final entry is ignored.  Missing
        entries are ambiguous seams.
    :param validate_width: If True (default), require every measured line to fit its capture
        width.  This is a consistency check only; width is never used to infer a marker.
    :param control_codes: How to measure terminal sequences, see :func:`~.width`.
    :param tabsize: Tab stop width for width validation.
    :param ambiguous_width: Width to use for East Asian Ambiguous (A) characters.
    :param term_program: Terminal software identifier for table correction.
    :returns: :class:`UnwrapResult`, a :class:`list` of record strings carrying proof metadata.
    :raises ValueError: On an invalid marker, a contradictory hard newline, a line wider than its
        capture width, or an oversized side table.

    Example::

        >>> from wcwidth import WrapMarker, unwrap
        >>> transcript = ['Diagnostic  ', 'complete  ', 'Next record']
        >>> result = unwrap(transcript, width=12, markers=[
        ...     WrapMarker.SOFT, WrapMarker.HARD])
        >>> result.texts()
        ['Diagnostic  complete  ', 'Next record']

    A full line does not imply a soft wrap, and a short line does not imply a hard one::

        >>> unwrap(['abcdefgh', 'ij'], width=8, markers=[False]).texts()
        ['abcdefgh', 'ij']
        >>> unwrap(['abc', 'def'], width=8, markers=[True]).texts()
        ['abcdef']

    A missing marker is reported rather than guessed::

        >>> result = unwrap(['abc', 'def'], width=8)
        >>> result.texts()
        ['abc', 'def']
        >>> [str(seam) for seam in result.ambiguous_seams]
        ['seam 0: ambiguous']
        >>> result.resolve(0, WrapMarker.SOFT).texts()
        ['abcdef']

    .. versionadded:: 0.9.5
    """
    physical, explicit_hard = _coerce_lines(lines)
    widths = _coerce_widths(width, len(physical))
    marker_values = _coerce_markers(markers, len(physical), explicit_hard)

    if validate_width:
        for index, (line, capture_width) in enumerate(zip(physical, widths)):
            if capture_width is None:
                continue
            measured = wcwidth_width(line, control_codes=control_codes, tabsize=tabsize,
                                     ambiguous_width=ambiguous_width,
                                     term_program=term_program)
            if measured > capture_width:
                raise ValueError(
                    f'physical line {index} is {measured} cells wide, '
                    f'exceeding capture width {capture_width}')

    proven = explicit_hard[:len(marker_values)]
    records = _build_records(physical, marker_values, proven)
    seams = tuple(UnwrapSeam(index, marker, _trailing_whitespace(physical[index]), proven[index])
                  for index, marker in enumerate(marker_values))
    return UnwrapResult(records=records, seams=seams, lines=physical, widths=widths,
                        markers=marker_values, proven=proven)
