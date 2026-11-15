"""
Forensic, best-effort decoding of damaged HPACK header blocks.

The regular :class:`~hpack.Decoder` is strict: a single representation that
references a dynamic table entry which is not available raises
:class:`~hpack.InvalidTableIndexError` and discards the rest of the header
block. When an archived header block has a gap of this kind, callers may still
want to salvage every later field whose value can be proven from the visible
wire bytes.

This module never raises for a bad table reference. Instead it records the gap
as an event and keeps scanning, classifying every recovered field by where its
bytes actually came from:

* ``static`` fields are indexed representations that resolve entirely in the
  immutable static table (RFC 7541 Appendix A);
* ``literal`` fields carry complete name/value bytes in the header block, or
  carry a literal value with a static-table name;
* ``dynamic`` fields are only provable through an entry of the dynamic table.

Every recovered value carries :class:`RecoveryEvidence` describing the exact
span of input bytes that supports it. New incremental insertions after a gap
may make some dynamic indexes readable again, but that does not restore the
old table: a complete new start is only reported once an explicit dynamic
table clear is observed.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import TYPE_CHECKING

from .exceptions import HPACKDecodingError, InvalidTableIndexError, InvalidTableSizeError
from .huffman_table import decode_huffman
from .hpack import decode_integer
from .struct import HeaderTuple, NeverIndexedHeaderTuple
from .table import HeaderTable, table_entry_size

if TYPE_CHECKING:
    from .hpack import Decoder  # pragma: no cover

# Provenance tags for individual dynamic table entries.
PROVENANCE_PRE_GAP = "pre_gap"
PROVENANCE_POST_GAP_INSERT = "post_gap_insert"
PROVENANCE_POST_CLEAR_INSERT = "post_clear_insert"

# Kinds of recovery events.
EVENT_INVALID_INDEX = "invalid_index"
EVENT_UNRESOLVABLE_NAME = "unresolvable_name_index"
EVENT_TRUNCATED = "truncated_block"
EVENT_MALFORMED = "malformed_representation"
EVENT_TABLE_CLEAR = "table_clear"

REP_INDEXED = "indexed"
REP_LITERAL_INCREMENTAL = "literal_incremental"
REP_LITERAL_WITHOUT_INDEXING = "literal_without_indexing"
REP_LITERAL_NEVER_INDEXED = "literal_never_indexed"

SOURCE_STATIC = "static"
SOURCE_LITERAL = "literal"
SOURCE_DYNAMIC = "dynamic"

class _NameUnresolvableError(Exception):
    """Internal control signal: a literal name index could not be resolved."""


class _TruncatedBlockError(HPACKDecodingError):
    """Internal signal: a representation runs past the end of the block."""


class _LiteralName:
    """Result of parsing the name portion of a literal representation."""

    __slots__ = (
        "name",
        "name_source",
        "static_index",
        "dynamic_index",
        "provenance",
        "name_span",
        "name_huffman",
    )

    def __init__(
        self,
        name: bytes,
        name_source: str,
        static_index: int | None,
        dynamic_index: int | None,
        provenance: str | None,
        name_span: tuple[int, int] | None,
        name_huffman: bool | None,
    ) -> None:  # noqa: PLR0913
        self.name = name
        self.name_source = name_source
        self.static_index = static_index
        self.dynamic_index = dynamic_index
        self.provenance = provenance
        self.name_span = name_span
        self.name_huffman = name_huffman


class RecoveryEvidence:
    """
    The visible input supporting a single recovered header field.

    :param representation: The HPACK representation used on the wire, one of
        ``indexed``, ``literal_incremental``, ``literal_without_indexing`` and
        ``literal_never_indexed``.
    :param start: Offset of the first byte of the representation in the header
        block.
    :param end: Offset immediately past the last byte of the representation.
    :param name_source: Where the field name was proven from: ``static``,
        ``literal`` or ``dynamic``.
    :param value_source: Where the field value was proven from: ``static``,
        ``literal`` or ``dynamic``.
    :param static_index: Static table index used, if any.
    :param dynamic_index: Dynamic table index used, if any.
    :param name_huffman: Whether the literal name bytes were Huffman encoded.
    :param value_huffman: Whether the literal value bytes were Huffman encoded.
    :param name_span: Byte span of the literal name, if present on the wire.
    :param value_span: Byte span of the literal value, if present on the wire.
    :param table_entry_provenance: For dynamic references, whether the resolved
        entry predates the gap (``pre_gap``), was inserted after the gap
        (``post_gap_insert``) or after an explicit clear
        (``post_clear_insert``).
    """

    __slots__ = (
        "representation",
        "start",
        "end",
        "name_source",
        "value_source",
        "static_index",
        "dynamic_index",
        "name_huffman",
        "value_huffman",
        "name_span",
        "value_span",
        "table_entry_provenance",
    )

    def __init__(
        self,
        representation: str,
        start: int,
        end: int,
        name_source: str,
        value_source: str,
        static_index: int | None = None,
        dynamic_index: int | None = None,
        name_huffman: bool | None = None,
        value_huffman: bool | None = None,
        name_span: tuple[int, int] | None = None,
        value_span: tuple[int, int] | None = None,
        table_entry_provenance: str | None = None,
    ) -> None:  # noqa: PLR0913
        self.representation = representation
        self.start = start
        self.end = end
        self.name_source = name_source
        self.value_source = value_source
        self.static_index = static_index
        self.dynamic_index = dynamic_index
        self.name_huffman = name_huffman
        self.value_huffman = value_huffman
        self.name_span = name_span
        self.value_span = value_span
        self.table_entry_provenance = table_entry_provenance

    def __repr__(self) -> str:
        return (
            "RecoveryEvidence(representation={!r}, start={}, end={}, "
            "name_source={!r}, value_source={!r}, static_index={!r}, "
            "dynamic_index={!r}, table_entry_provenance={!r})".format(
                self.representation,
                self.start,
                self.end,
                self.name_source,
                self.value_source,
                self.static_index,
                self.dynamic_index,
                self.table_entry_provenance,
            )
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RecoveryEvidence):
            return NotImplemented
        return all(getattr(self, slot) == getattr(other, slot) for slot in self.__slots__)


class RecoveredHeader:
    """
    A header field salvaged from a damaged header block.

    Tuple subclasses cannot carry per-instance attributes, so the proven
    header is held by composition. The object otherwise behaves like a
    two-tuple of raw byte strings: it supports indexing, iteration, hashing
    and equality against plain tuples and :class:`~hpack.HeaderTuple`.
    """

    __slots__ = ("name", "value", "evidence")

    indexable = True

    def __init__(self, name: bytes, value: bytes, evidence: RecoveryEvidence) -> None:
        self.name = name
        self.value = value
        self.evidence = evidence

    def as_tuple(self) -> HeaderTuple:
        """Returns the salvaged field as a plain :class:`~hpack.HeaderTuple`."""
        return HeaderTuple(self.name, self.value)

    def __iter__(self) -> Iterator[bytes]:
        yield self.name
        yield self.value

    def __len__(self) -> int:
        return 2

    def __getitem__(self, item: int) -> bytes:
        return (self.name, self.value)[item]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RecoveredHeader):
            return (self.name, self.value) == (other.name, other.value)
        if isinstance(other, tuple):
            return (self.name, self.value) == other
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:
        return hash((self.name, self.value))

    def __repr__(self) -> str:
        return f"RecoveredHeader(({self.name!r}, {self.value!r}), evidence={self.evidence!r})"


class RecoveredNeverIndexedHeader(RecoveredHeader):
    """
    A salvaged header field that was marked as never indexed.
    """

    __slots__ = ()

    indexable = False

    def as_tuple(self) -> NeverIndexedHeaderTuple:
        """Returns the salvaged field as a :class:`~hpack.NeverIndexedHeaderTuple`."""
        return NeverIndexedHeaderTuple(self.name, self.value)


class RecoveryEvent:
    """
    A point at which strict decoding broke down, or an observed table reset.

    :param kind: One of the ``EVENT_*`` constants.
    :param message: Human readable description of what failed.
    :param start: Offset of the first byte belonging to the event.
    :param end: Offset immediately past the last identifiable byte of the
        event. For a terminal failure whose boundary cannot be located this is
        the last safely consumed offset.
    """

    __slots__ = ("kind", "message", "start", "end")

    def __init__(self, kind: str, message: str, start: int, end: int) -> None:
        self.kind = kind
        self.message = message
        self.start = start
        self.end = end

    def __repr__(self) -> str:
        return (
            f"RecoveryEvent(kind={self.kind!r}, start={self.start}, "
            f"end={self.end}, message={self.message!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RecoveryEvent):
            return NotImplemented
        return (self.kind, self.message, self.start, self.end) == (
            other.kind,
            other.message,
            other.start,
            other.end,
        )


class RecoveryResult:
    """
    The outcome of a best-effort decode.

    Fields are kept in wire order in :attr:`headers`; the three filtered
    views partition that list exhaustively according to how each value was
    proven.
    """

    __slots__ = (
        "headers",
        "gap_events",
        "clear_events",
        "fresh_start_offset",
    )

    def __init__(
        self,
        headers: list[RecoveredHeader],
        gap_events: list[RecoveryEvent],
        clear_events: list[RecoveryEvent],
        fresh_start_offset: int | None,
    ) -> None:
        self.headers = headers
        self.gap_events = gap_events
        self.clear_events = clear_events
        self.fresh_start_offset = fresh_start_offset

    @property
    def gap_detected(self) -> bool:
        """Whether at least one unresolvable reference was encountered."""
        return bool(self.gap_events)

    @property
    def fresh_start(self) -> bool:
        """
        Whether an explicit dynamic table clear was observed after the gap.

        Only after this point can insertions be treated as a brand new
        compression context rather than as repairs of the old table.
        """
        return self.fresh_start_offset is not None

    @property
    def static_headers(self) -> list[RecoveredHeader]:
        """Indexed fields resolved entirely in the static table."""
        return [
            header
            for header in self.headers
            if header.evidence.representation == REP_INDEXED
            and header.evidence.name_source == SOURCE_STATIC
        ]

    @property
    def literal_headers(self) -> list[RecoveredHeader]:
        """Fields whose value is fully present as literal wire bytes."""
        return [
            header
            for header in self.headers
            if header.evidence.representation != REP_INDEXED
            and header.evidence.name_source != SOURCE_DYNAMIC
        ]

    @property
    def dynamic_reference_headers(self) -> list[RecoveredHeader]:
        """Fields whose proof depends on a dynamic table entry."""
        return [
            header
            for header in self.headers
            if header.evidence.name_source == SOURCE_DYNAMIC
            or header.evidence.value_source == SOURCE_DYNAMIC
        ]


def _decode_string(data: bytes, offset: int) -> tuple[bytes, int, bool, tuple[int, int]]:
    """
    Decodes one HPACK string (7-bit length prefix) at ``offset``.

    Returns the decoded value, the number of bytes consumed, whether Huffman
    encoding was used and the span of the encoded string bytes in the input.
    Raises :class:`HPACKDecodingError` if the block is truncated or the
    Huffman code is invalid.
    """
    if offset >= len(data):
        msg = "Truncated header block"
        raise _TruncatedBlockError(msg)
    length, consumed = decode_integer(data[offset:], 7)
    huffman_encoded = bool(data[offset] & 0x80)
    string_start = offset + consumed
    string_end = string_start + length
    if string_end > len(data):
        msg = "Truncated header block"
        raise _TruncatedBlockError(msg)
    raw = data[string_start:string_end]
    if huffman_encoded:
        value = decode_huffman(raw)
    else:
        value = raw
    return value, string_end - offset, huffman_encoded, (string_start, string_end)


# Return type of a single representation scan: either a salvaged header or
# ``None`` when the representation produced no recoverable field.
_ScanOutcome = RecoveredHeader | None


class _RecoveryScanner:
    """
    Holds the mutable state of one best-effort decoding pass.
    """

    def __init__(self, data: bytes, decoder: Decoder) -> None:
        self.data = data
        self.data_len = len(data)
        self.decoder = decoder
        self.table: HeaderTable = decoder.header_table
        self.position = 0
        self.gap_active = False
        self.fresh_start_offset: int | None = None
        self.recovered: list[RecoveredHeader] = []
        self.gap_events: list[RecoveryEvent] = []
        self.clear_events: list[RecoveryEvent] = []

        # Parallel deque labelling every dynamic entry with its provenance. It
        # is kept aligned with table.dynamic_entries, which only ever grows on
        # the left and shrinks (or clears) on the right.
        self.entry_provenance: deque[str] = deque(
            PROVENANCE_PRE_GAP for _ in range(len(self.table.dynamic_entries))
        )

    def run(self) -> RecoveryResult:
        while self.position < self.data_len:
            element_start = self.position
            first_byte = self.data[self.position]
            try:
                if first_byte & 0x80:
                    outcome = self._scan_indexed(element_start)
                elif first_byte & 0x20 and not (first_byte & 0x40):
                    outcome = self._scan_size_update(element_start)
                else:
                    outcome = self._scan_literal(element_start, first_byte)
            except InvalidTableSizeError as err:
                self._record_terminal(EVENT_MALFORMED, err, element_start)
                break
            except _TruncatedBlockError as err:
                self._record_terminal(EVENT_TRUNCATED, err, element_start)
                break
            except HPACKDecodingError as err:
                kind = EVENT_TRUNCATED if self.position >= self.data_len else EVENT_MALFORMED
                self._record_terminal(kind, err, element_start)
                break

            if outcome is not None:
                self.recovered.append(outcome)

        return RecoveryResult(
            headers=self.recovered,
            gap_events=self.gap_events,
            clear_events=self.clear_events,
            fresh_start_offset=self.fresh_start_offset,
        )

    def _record_terminal(self, kind: str, err: Exception, element_start: int) -> None:
        self.gap_active = True
        self.gap_events.append(
            RecoveryEvent(kind, str(err), element_start, self.position),
        )

    def _sync_provenance(self) -> None:
        current = len(self.table.dynamic_entries)
        while len(self.entry_provenance) > current:
            self.entry_provenance.pop()
        if current == 0:
            self.entry_provenance.clear()

    def _insertion_provenance(self) -> str:
        if self.fresh_start_offset is not None:
            return PROVENANCE_POST_CLEAR_INSERT
        if self.gap_active:
            return PROVENANCE_POST_GAP_INSERT
        return PROVENANCE_PRE_GAP

    def _resolve(self, index: int) -> tuple[bytes, bytes, str, str | None]:
        name, value = self.table.get_by_index(index)
        if index <= HeaderTable.STATIC_TABLE_LENGTH:
            return name, value, SOURCE_STATIC, None
        entry_position = index - HeaderTable.STATIC_TABLE_LENGTH - 1
        provenance = self.entry_provenance[entry_position]
        return name, value, SOURCE_DYNAMIC, provenance

    def _record_gap(self, kind: str, err: Exception, element_start: int) -> None:
        self.gap_active = True
        self.gap_events.append(
            RecoveryEvent(kind, str(err), element_start, self.position),
        )

    def _note_clear(self, message: str, element_start: int) -> None:
        self.fresh_start_offset = self.position
        self.clear_events.append(
            RecoveryEvent(EVENT_TABLE_CLEAR, message, element_start, self.position),
        )

    def _scan_indexed(self, element_start: int) -> _ScanOutcome:
        # Indexed header field (RFC 7541 section 6.1).
        index, consumed = decode_integer(self.data[self.position :], 7)
        self.position += consumed
        try:
            name, value, source, provenance = self._resolve(index)
        except InvalidTableIndexError as err:
            self._record_gap(EVENT_INVALID_INDEX, err, element_start)
            return None

        evidence = RecoveryEvidence(
            REP_INDEXED,
            element_start,
            self.position,
            source,
            source,
            static_index=index if source == SOURCE_STATIC else None,
            dynamic_index=index if source == SOURCE_DYNAMIC else None,
            table_entry_provenance=provenance,
        )
        return RecoveredHeader(name, value, evidence)

    def _scan_size_update(self, element_start: int) -> _ScanOutcome:
        # Dynamic table size update (RFC 7541 section 6.3).
        new_size, consumed = decode_integer(self.data[self.position :], 5)
        self.position += consumed
        if new_size > self.decoder.max_allowed_table_size:
            msg = "Encoder exceeded max allowable table size"
            raise InvalidTableSizeError(msg)
        had_entries = bool(self.table.dynamic_entries)
        self.table.maxsize = new_size
        self._sync_provenance()
        # A size of zero empties the table unconditionally; a shrink that
        # evicts every local entry is equally deterministic. Either one is a
        # trustworthy reset even when local state had already diverged.
        cleared = new_size == 0 or (had_entries and not self.table.dynamic_entries)
        if self.gap_active and self.fresh_start_offset is None and cleared:
            self._note_clear(f"Dynamic table cleared by size update to {new_size}", element_start)
        return None

    def _scan_literal(self, element_start: int, first_byte: int) -> _ScanOutcome:
        # Literal header field (RFC 7541 section 6.2). The three variants
        # share their wire layout after the first byte.
        should_index = bool(first_byte & 0x40)
        never_indexed = bool(first_byte & 0x10) and not should_index
        if should_index:
            name_prefix = 6
            representation = REP_LITERAL_INCREMENTAL
        else:
            name_prefix = 4
            representation = (
                REP_LITERAL_NEVER_INDEXED if never_indexed else REP_LITERAL_WITHOUT_INDEXING
            )

        try:
            parsed_name = self._scan_literal_name(element_start, first_byte, should_index, name_prefix)
        except _NameUnresolvableError:
            return None

        value, value_consumed, value_huffman, value_span = _decode_string(
            self.data,
            self.position,
        )
        self.position += value_consumed

        evidence = RecoveryEvidence(
            representation,
            element_start,
            self.position,
            parsed_name.name_source,
            SOURCE_LITERAL,
            static_index=parsed_name.static_index,
            dynamic_index=parsed_name.dynamic_index,
            name_huffman=parsed_name.name_huffman,
            value_huffman=value_huffman,
            name_span=parsed_name.name_span,
            value_span=value_span,
            table_entry_provenance=parsed_name.provenance,
        )
        if never_indexed:
            header: RecoveredHeader = RecoveredNeverIndexedHeader(parsed_name.name, value, evidence)
        else:
            header = RecoveredHeader(parsed_name.name, value, evidence)

        if should_index:
            oversized = table_entry_size(parsed_name.name, value) > self.table.maxsize
            self.table.add(parsed_name.name, value)
            self.entry_provenance.appendleft(self._insertion_provenance())
            self._sync_provenance()
            # An entry larger than the maximum size clears the table, which is
            # an explicit, wire-observable reset even when it was already empty.
            if oversized and self.gap_active and self.fresh_start_offset is None:
                self._note_clear(
                    "Dynamic table cleared by oversized incremental entry",
                    element_start,
                )
        return header

    def _scan_literal_name(
        self,
        element_start: int,
        first_byte: int,
        should_index: bool,
        name_prefix: int,
    ) -> _LiteralName:
        indexed_name = first_byte & (0x3F if should_index else 0x0F)
        static_index: int | None = None
        dynamic_index: int | None = None
        provenance: str | None = None
        name_span: tuple[int, int] | None = None
        name_huffman: bool | None = None

        if indexed_name:
            name_index, name_consumed = decode_integer(self.data[self.position :], name_prefix)
            self.position += name_consumed
            try:
                name, _ignored_value, name_source, provenance = self._resolve(name_index)
            except InvalidTableIndexError as err:
                # The name cannot be recovered, but the literal value that
                # follows still has a deterministic boundary, so the scan can
                # resynchronize afterwards.
                _value, value_consumed, _huffman, _span = _decode_string(self.data, self.position)
                self.position += value_consumed
                self._record_gap(EVENT_UNRESOLVABLE_NAME, err, element_start)
                raise _NameUnresolvableError from err
            if name_source == SOURCE_STATIC:
                static_index = name_index
            else:
                dynamic_index = name_index
        else:
            self.position += 1
            name, name_consumed, name_huffman, name_span = _decode_string(
                self.data,
                self.position,
            )
            self.position += name_consumed
            name_source = SOURCE_LITERAL

        return _LiteralName(
            name=name,
            name_source=name_source,
            static_index=static_index,
            dynamic_index=dynamic_index,
            provenance=provenance,
            name_span=name_span,
            name_huffman=name_huffman,
        )


def decode_with_recovery(data: bytes, decoder: Decoder | None = None) -> RecoveryResult:
    """
    Decodes a potentially damaged HPACK header block without raising.

    Unresolvable dynamic table references are recorded as gap events and
    scanning continues at the next representation whenever its boundary is
    still determinable. Truncated or otherwise malformed representations end
    the scan, but everything proven before them is returned.

    The dynamic table of ``decoder`` is mutated exactly as the visible
    representations dictate, so the decoder stays usable for later header
    blocks. Insertions made after a gap are tracked separately and never
    presented as proof that the pre-gap table was restored.

    :param data: The HPACK-encoded header block.
    :param decoder: An existing :class:`~hpack.Decoder` whose table state
        should be used. A fresh decoder is created when omitted.
    :returns: A :class:`RecoveryResult` containing the salvaged headers,
        evidence and gap/clear events.
    """
    if decoder is None:
        from .hpack import Decoder

        decoder = Decoder()

    return _RecoveryScanner(bytes(data), decoder).run()
