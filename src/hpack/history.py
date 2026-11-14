"""
HPACK decoding for labeled batches of HTTP/2 header blocks.

This module builds on :class:`hpack.Decoder` to decode a collection of
desensitized HPACK header blocks whose inputs carry connection generation,
direction, stream and block-order labels.  In addition to the decoded header
fields it retains, for every table index reference, the historical block on
which that reference is based.

HPACK compression state (the dynamic table) is shared by every stream within
one direction of one connection.  The opposite direction, and every new
connection generation after a reconnect, each own an independent dictionary.
Those rules are modeled here by keying every :class:`CompressionContext` on a
``(connection_generation, direction)`` pair.  Because contexts never share
state, interleaving the input order of blocks belonging to two connections
cannot change the fields decoded for any stream.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal

from .exceptions import (
    HPACKDecodingError,
    InvalidTableIndex,
    InvalidTableSizeError,
    OversizedHeaderListError,
)
from .hpack import DEFAULT_MAX_HEADER_LIST_SIZE, decode_integer
from .huffman_table import decode_huffman
from .table import HeaderTable, table_entry_size

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator  # pragma: no cover

log = logging.getLogger(__name__)

#: Direction label for blocks traveling from client to server.
REQUEST = "request"

#: Direction label for blocks traveling from server to client.
RESPONSE = "response"

TableKind = Literal["static", "dynamic"]


class Representation(Enum):
    """
    The HPACK representation used to encode a header field.

    See RFC 7541 sections 6.1 through 6.3.
    """

    #: Indexed header field (RFC 7541 section 6.1).
    INDEXED = "indexed"
    #: Literal header field with incremental indexing (section 6.2.1).
    INCREMENTAL = "incremental"
    #: Literal header field without indexing (section 6.2.2).
    WITHOUT_INDEXING = "without_indexing"
    #: Literal header field never indexed (section 6.2.3).
    NEVER_INDEXED = "never_indexed"


@dataclass(frozen=True, slots=True)
class BlockRef:
    """
    Immutable label identifying one header block inside the input batch.

    :param connection_generation: Connection generation.  A reconnect starts a
        new generation and gets a fresh compression context per direction.
    :param direction: Direction label, typically :data:`REQUEST` or
        :data:`RESPONSE`.  Opposite directions never share a dictionary.
    :param stream_id: HTTP/2 stream identifier the block belongs to.
    :param sequence: Block order within the (generation, direction) context.
        Sequences must be supplied in strictly increasing order within a
        context, independent of global arrival order.
    """

    connection_generation: int
    direction: str
    stream_id: int
    sequence: int


@dataclass(frozen=True, slots=True)
class HeaderBlockRef:
    """
    Provenance record for a single table index reference.

    A reference snapshots the table entry at the moment it was used, so the
    record stays valid even after the entry is evicted from the dynamic table.

    :param table_index: One-based HPACK table index that was referenced.
    :param table_kind: Whether the index resolved into the static table or the
        dynamic table.
    :param name: Field name stored in the referenced table entry.
    :param value: Field value stored in the referenced table entry.
    :param block: Historical block that introduced the entry, or ``None`` for
        static-table entries, which exist independently of any input block.
    :param field_index: Ordinal of the introducing field inside ``block``, or
        ``None`` for static-table entries.
    """

    table_index: int
    table_kind: TableKind
    name: bytes
    value: bytes
    block: BlockRef | None
    field_index: int | None


@dataclass(frozen=True, slots=True)
class DecodedField:
    """
    One header field decoded from a header block.

    :param name: Decoded field name as raw bytes.
    :param value: Decoded field value as raw bytes.
    :param representation: HPACK representation used by the field.
    :param name_ref: Provenance of the indexed field name, or ``None`` when the
        name was sent as a literal.
    :param value_ref: Provenance of the indexed field value, or ``None`` when
        the value was sent as a literal.  For an indexed representation this is
        the same entry as :attr:`name_ref`.
    """

    name: bytes
    value: bytes
    representation: Representation
    name_ref: HeaderBlockRef | None
    value_ref: HeaderBlockRef | None


@dataclass(frozen=True, slots=True)
class DecodedBlock:
    """
    The result of decoding one labeled header block.

    :param ref: Label identifying the block inside the batch.
    :param raw: The original encoded header block bytes.
    :param fields: Fields decoded from the block, in wire order.
    :param table_size_updates: Dynamic table size updates requested at the
        start of the block, in wire order.
    """

    ref: BlockRef
    raw: bytes
    fields: tuple[DecodedField, ...]
    table_size_updates: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FieldRow:
    """
    One row of the long, flattened table of decoded request fields.

    Rows are ordered deterministically by
    ``(connection_generation, direction, block sequence, field ordinal)`` and
    therefore do not depend on the arrival order of interleaved connections.
    """

    connection_generation: int
    direction: str
    stream_id: int
    block_sequence: int
    field_index: int
    name: bytes
    value: bytes
    representation: Representation
    name_ref: HeaderBlockRef | None
    value_ref: HeaderBlockRef | None


@dataclass(frozen=True, slots=True)
class _EntryOrigin:
    """
    Origin metadata attached to one dynamic table entry.
    """

    block: BlockRef
    field_index: int


class _ProvenanceHeaderTable(HeaderTable):
    """
    A :class:`HeaderTable` that keeps an origin record next to every dynamic
    entry.  The ``origins`` deque always mirrors ``dynamic_entries``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.origins: deque[_EntryOrigin] = deque()

    def add(self, name: bytes, value: bytes, origin: _EntryOrigin) -> None:  # type: ignore[override]
        size = table_entry_size(name, value)
        if size > self._maxsize:
            self.dynamic_entries.clear()
            self.origins.clear()
            self._current_size = 0
        else:
            self.dynamic_entries.appendleft((name, value))
            self.origins.appendleft(origin)
            self._current_size += size
            self._shrink()

    def set_maxsize(self, newmax: int) -> None:
        """
        Resize the table, keeping the origins deque in sync with evictions.
        """
        newmax = int(newmax)
        log.debug("Resizing header table to %d from %d", newmax, self._maxsize)
        oldmax = self._maxsize
        self._maxsize = newmax
        self.resized = newmax != oldmax
        if newmax <= 0:
            self.dynamic_entries.clear()
            self.origins.clear()
            self._current_size = 0
        elif oldmax > newmax:
            self._shrink()

    def _shrink(self) -> None:
        cursize = self._current_size
        while cursize > self._maxsize:
            name, value = self.dynamic_entries.pop()
            self.origins.pop()
            cursize -= table_entry_size(name, value)
            log.debug("Evicting %s: %s from the header table", name, value)
        self._current_size = cursize

    def origin_at_dynamic_index(self, dynamic_index: int) -> _EntryOrigin:
        return self.origins[dynamic_index]


class CompressionContext:
    """
    The HPACK compression context for one direction of one connection
    generation.

    Each context owns an independent dynamic table.  Blocks must be decoded in
    strictly increasing sequence order; stream identifiers do not affect the
    shared dictionary.

    :param connection_generation: Connection generation of this context.
    :param direction: Direction label of this context.
    :param max_allowed_table_size: Maximum dynamic table size the encoder is
        allowed to request.
    :param max_header_list_size: Maximum decompressed size accepted for a
        single header block.
    """

    def __init__(
        self,
        connection_generation: int,
        direction: str,
        max_allowed_table_size: int = HeaderTable.DEFAULT_SIZE,
        max_header_list_size: int = DEFAULT_MAX_HEADER_LIST_SIZE,
    ) -> None:
        self.connection_generation = connection_generation
        self.direction = direction
        self.max_allowed_table_size = max_allowed_table_size
        self.max_header_list_size = max_header_list_size
        self._table = _ProvenanceHeaderTable()
        self._last_sequence: int | None = None
        self._blocks: dict[int, DecodedBlock] = {}

    @property
    def header_table(self) -> _ProvenanceHeaderTable:
        """The dynamic table backing this context."""
        return self._table

    def blocks(self) -> tuple[DecodedBlock, ...]:
        """All decoded blocks, ordered by block sequence."""
        return tuple(self._blocks[sequence] for sequence in sorted(self._blocks))

    def decode_block(
        self,
        data: bytes | bytearray | memoryview,
        stream_id: int,
        sequence: int,
    ) -> DecodedBlock:
        """
        Decode one labeled header block inside this context.

        :param data: The complete HPACK-encoded header block.
        :param stream_id: HTTP/2 stream identifier of the block.
        :param sequence: Block order within this context.
        :raises BlockSequenceError: If the sequence is not strictly increasing.
        """
        if self._last_sequence is not None and sequence <= self._last_sequence:
            msg = (
                f"Block sequence {sequence} for generation "
                f"{self.connection_generation!r} direction {self.direction!r} "
                f"must be greater than previous sequence {self._last_sequence}"
            )
            raise BlockSequenceError(msg)

        ref = BlockRef(
            connection_generation=self.connection_generation,
            direction=self.direction,
            stream_id=stream_id,
            sequence=sequence,
        )
        decoded = self._decode(bytes(data), ref)
        self._blocks[sequence] = decoded
        self._last_sequence = sequence
        return decoded

    def _lookup(self, index: int) -> tuple[bytes, bytes, TableKind, _EntryOrigin | None]:
        if index <= 0:
            msg = f"Invalid table index {index}"
            raise InvalidTableIndex(msg)
        if index <= HeaderTable.STATIC_TABLE_LENGTH:
            name, value = self._table.get_by_index(index)
            return name, value, "static", None
        name, value = self._table.get_by_index(index)
        origin = self._table.origin_at_dynamic_index(index - HeaderTable.STATIC_TABLE_LENGTH - 1)
        return name, value, "dynamic", origin

    def _make_ref(self, index: int) -> HeaderBlockRef:
        name, value, kind, origin = self._lookup(index)
        if origin is None:
            return HeaderBlockRef(
                table_index=index,
                table_kind=kind,
                name=name,
                value=value,
                block=None,
                field_index=None,
            )
        return HeaderBlockRef(
            table_index=index,
            table_kind=kind,
            name=name,
            value=value,
            block=origin.block,
            field_index=origin.field_index,
        )

    def _decode(self, data: bytes, block_ref: BlockRef) -> DecodedBlock:
        log.debug("Decoding labeled block %s: %s", block_ref, data)

        fields: list[DecodedField] = []
        size_updates: list[int] = []
        inflated_size = 0
        position = 0
        data_length = len(data)

        while position < data_length:
            first_byte = data[position]

            if first_byte & 0x80:
                field, consumed = self._decode_indexed(data, position)
            elif first_byte & 0x40:
                field, consumed = self._decode_literal(
                    data,
                    position,
                    _EntryOrigin(block=block_ref, field_index=len(fields)),
                    Representation.INCREMENTAL,
                )
            elif first_byte & 0x20:
                if fields:
                    msg = "Table size update not at the start of the block"
                    raise HPACKDecodingError(msg)
                new_size, consumed = decode_integer(data[position:], 5)
                if new_size > self.max_allowed_table_size:
                    msg = "Encoder exceeded max allowable table size"
                    raise InvalidTableSizeError(msg)
                self._table.set_maxsize(new_size)
                size_updates.append(new_size)
                field = None
            else:
                representation = (
                    Representation.NEVER_INDEXED
                    if first_byte & 0x10
                    else Representation.WITHOUT_INDEXING
                )
                field, consumed = self._decode_literal(
                    data,
                    position,
                    _EntryOrigin(block=block_ref, field_index=len(fields)),
                    representation,
                )

            if field is not None:
                fields.append(field)
                inflated_size += table_entry_size(field.name, field.value)
                if inflated_size > self.max_header_list_size:
                    msg = f"A header list larger than {self.max_header_list_size} has been received"
                    raise OversizedHeaderListError(msg)

            position += consumed

        return DecodedBlock(
            ref=block_ref,
            raw=data,
            fields=tuple(fields),
            table_size_updates=tuple(size_updates),
        )

    def _decode_indexed(self, data: bytes, position: int) -> tuple[DecodedField, int]:
        index, consumed = decode_integer(data[position:], 7)
        ref = self._make_ref(index)
        field = DecodedField(
            name=ref.name,
            value=ref.value,
            representation=Representation.INDEXED,
            name_ref=ref,
            value_ref=ref,
        )
        return field, consumed

    def _decode_literal(
        self,
        data: bytes,
        position: int,
        origin: _EntryOrigin,
        representation: Representation,
    ) -> tuple[DecodedField, int]:
        should_index = representation is Representation.INCREMENTAL
        name_prefix = 6 if should_index else 4
        first_byte = data[position]
        indexed_name = first_byte & ((1 << name_prefix) - 1)

        if indexed_name:
            index, consumed = decode_integer(data[position:], name_prefix)
            name_ref = self._make_ref(index)
            name = name_ref.name
            cursor = position + consumed
        else:
            name_length, name_len_consumed = decode_integer(data[position + 1:], 7)
            name_start = position + 1 + name_len_consumed
            name_end = name_start + name_length
            if name_end > len(data):
                msg = "Truncated header block"
                raise HPACKDecodingError(msg)
            name = data[name_start:name_end]
            if data[position + 1] & 0x80:
                name = decode_huffman(name)
            name_ref = None
            cursor = name_end

        value_length, value_len_consumed = decode_integer(data[cursor:], 7)
        value_start = cursor + value_len_consumed
        value_end = value_start + value_length
        if value_end > len(data):
            msg = "Truncated header block"
            raise HPACKDecodingError(msg)
        value = data[value_start:value_end]
        if data[cursor] & 0x80:
            value = decode_huffman(value)
        consumed = value_end - position

        field = DecodedField(
            name=name,
            value=value,
            representation=representation,
            name_ref=name_ref,
            value_ref=None,
        )

        if should_index:
            self._table.add(name, value, origin)

        return field, consumed


class BlockSequenceError(HPACKDecodingError):
    """
    A header block was presented outside the strictly increasing sequence
    order of its compression context, or the same block sequence was
    presented twice.
    """


@dataclass(slots=True)
class BlockDecoder:
    """
    Decodes a labeled batch of HPACK header blocks while keeping one
    independent compression context per ``(connection_generation, direction)``
    pair.

    Streams share the dictionary of their direction; opposite directions and
    reconnect generations are isolated.  Blocks may therefore be fed in any
    arrival order across connections without affecting the decoded fields.

    :param max_allowed_table_size: Maximum dynamic table size an encoder is
        allowed to request in any context.
    :param max_header_list_size: Maximum decompressed size accepted for a
        single header block.
    """

    max_allowed_table_size: int = HeaderTable.DEFAULT_SIZE
    max_header_list_size: int = DEFAULT_MAX_HEADER_LIST_SIZE
    _contexts: dict[tuple[int, str], CompressionContext] = field(default_factory=dict, init=False)

    def context(
        self,
        connection_generation: int,
        direction: str,
    ) -> CompressionContext:
        """
        Return the compression context for a generation/direction pair,
        creating it on first use.
        """
        key = (connection_generation, direction)
        context = self._contexts.get(key)
        if context is None:
            context = CompressionContext(
                connection_generation=connection_generation,
                direction=direction,
                max_allowed_table_size=self.max_allowed_table_size,
                max_header_list_size=self.max_header_list_size,
            )
            self._contexts[key] = context
        return context

    @property
    def contexts(self) -> dict[tuple[int, str], CompressionContext]:
        """Mapping of ``(generation, direction)`` to compression contexts."""
        return self._contexts

    def decode_block(
        self,
        data: bytes | bytearray | memoryview,
        *,
        connection_generation: int,
        direction: str,
        stream_id: int,
        sequence: int,
    ) -> DecodedBlock:
        """
        Decode one labeled header block and retain it in the long table.
        """
        context = self.context(connection_generation, direction)
        return context.decode_block(data, stream_id=stream_id, sequence=sequence)

    def decode_all(
        self,
        blocks: Iterable[tuple[int, str, int, int, bytes | bytearray | memoryview]],
    ) -> tuple[DecodedBlock, ...]:
        """
        Decode a batch of ``(generation, direction, stream_id, sequence,
        data)`` tuples and return the decoded blocks.

        The tuples may arrive in any order: each block is routed to its own
        compression context, so interleaving two connections does not change
        the decoded fields.
        """
        return tuple(
            self.decode_block(
                data,
                connection_generation=generation,
                direction=direction,
                stream_id=stream_id,
                sequence=sequence,
            )
            for generation, direction, stream_id, sequence, data in blocks
        )

    @property
    def blocks(self) -> tuple[DecodedBlock, ...]:
        """
        All decoded blocks, deterministically ordered by
        ``(generation, direction, sequence)`` regardless of arrival order.
        """
        ordered: list[DecodedBlock] = []
        for key in sorted(self._contexts):
            ordered.extend(self._contexts[key].blocks())
        return tuple(ordered)

    @property
    def fields(self) -> tuple[FieldRow, ...]:
        """
        The flattened long table of decoded fields.

        Rows are ordered by ``(generation, direction, block sequence, field
        ordinal)``, so the table is identical when interleaved connections are
        swapped.
        """
        rows: list[FieldRow] = []
        for block in self.blocks:
            for field_index, decoded_field in enumerate(block.fields):
                rows.append(
                    FieldRow(
                        connection_generation=block.ref.connection_generation,
                        direction=block.ref.direction,
                        stream_id=block.ref.stream_id,
                        block_sequence=block.ref.sequence,
                        field_index=field_index,
                        name=decoded_field.name,
                        value=decoded_field.value,
                        representation=decoded_field.representation,
                        name_ref=decoded_field.name_ref,
                        value_ref=decoded_field.value_ref,
                    ),
                )
        return tuple(rows)

    def __iter__(self) -> Iterator[DecodedBlock]:
        return iter(self.blocks)

    def __len__(self) -> int:
        return len(self.blocks)
