"""
Records the encoding layout of HPACK header blocks.

This module provides analysis tooling that maps a "normal" (well-formed)
HPACK header block back onto its source bytes. For every field line it
records which source bytes are occupied by the field representation, by
each length integer, and by each (possibly Huffman-compressed) string.

Input may arrive as an ordered sequence of byte fragments whose boundaries
are allowed to fall in the middle of multi-byte integers or strings. While
the header block has not been marked as ended, trailing undecodable bytes
are kept as pending; only once the end of the block is signalled is a
leftover remainder judged to be a truncated block.
"""
from __future__ import annotations

from dataclasses import dataclass

from .exceptions import HPACKDecodingError, HPACKError
from .hpack import VARINT_MAX_LENGTH, _PREFIX_BIT_MAX_NUMBERS

#: Field representation kinds recorded in :class:`FieldLayout`.
INDEXED = "indexed"
LITERAL_INCREMENTAL = "literal_with_incremental_indexing"
LITERAL_WITHOUT_INDEXING = "literal_without_indexing"
LITERAL_NEVER_INDEXED = "literal_never_indexed"
TABLE_SIZE_UPDATE = "table_size_update"


@dataclass(frozen=True)
class FieldLayout:
    """
    The source-byte layout of a single decoded field line.

    All spans are ``(start, end)`` offsets into the concatenated header
    block, with ``end`` exclusive. Spans that do not apply to a given
    representation kind are ``None``.

    ``span`` covers the whole field representation. ``index`` covers the
    prefix integer (the leading byte plus any continuation bytes): the
    table index for indexed fields and literals with indexed names, or the
    new table size for size updates. ``name_length``/``name`` and
    ``value_length``/``value`` cover the length integers and the raw
    (possibly Huffman-compressed) string bytes of literal names and values.
    """

    kind: str
    span: tuple[int, int]
    index: tuple[int, int] | None = None
    name_length: tuple[int, int] | None = None
    name: tuple[int, int] | None = None
    value_length: tuple[int, int] | None = None
    value: tuple[int, int] | None = None
    name_huffman: bool = False
    value_huffman: bool = False


def _try_decode_integer(data: bytes | bytearray, offset: int, prefix_bits: int) -> tuple[int, int] | None:
    """
    Decodes an HPACK integer starting at ``offset``. Returns a tuple of the
    decoded value and the number of bytes consumed, or ``None`` if the
    available bytes run out before the integer is complete.
    """
    max_number = _PREFIX_BIT_MAX_NUMBERS[prefix_bits]
    mask = 0xFF >> (8 - prefix_bits)

    if offset >= len(data):
        return None

    number = data[offset] & mask
    index = offset + 1
    if number != max_number:
        return number, 1

    shift = 0
    while True:
        if index >= len(data):
            return None
        next_byte = data[index]
        index += 1

        if next_byte >= 128:
            number += (next_byte - 128) << shift
        else:
            number += next_byte << shift
            return number, index - offset
        shift += 7

        if index - offset > VARINT_MAX_LENGTH:  # sanity check, mirrors decode_integer
            msg = f"Variable integer representation is too long: {bytes(data[offset:index])!r}"
            raise HPACKDecodingError(msg)


def _try_parse_field(data: bytes | bytearray, offset: int) -> tuple[FieldLayout, int] | None:
    """
    Parses a single field line starting at ``offset``. Returns the layout
    and the number of bytes consumed, or ``None`` if the available bytes do
    not yet cover a complete field line.
    """
    if offset >= len(data):
        return None
    leading = data[offset]

    if leading & 0x80:  # Indexed header field, 7-bit prefix index.
        indexed_field = _try_decode_integer(data, offset, 7)
        if indexed_field is None:
            return None
        _, index_len = indexed_field
        end = offset + index_len
        return FieldLayout(kind=INDEXED, span=(offset, end), index=(offset, end)), index_len

    if leading & 0x20 and not leading & 0x40:  # Dynamic table size update, 5-bit prefix.
        size = _try_decode_integer(data, offset, 5)
        if size is None:
            return None
        _, size_len = size
        end = offset + size_len
        return FieldLayout(kind=TABLE_SIZE_UPDATE, span=(offset, end), index=(offset, end)), size_len

    if leading & 0x40:
        kind = LITERAL_INCREMENTAL
        prefix_bits = 6
    else:
        kind = LITERAL_NEVER_INDEXED if leading & 0x10 else LITERAL_WITHOUT_INDEXING
        prefix_bits = 4

    indexed = _try_decode_integer(data, offset, prefix_bits)
    if indexed is None:
        return None
    name_index, index_len = indexed
    cursor = offset + index_len
    index_span: tuple[int, int] | None = (offset, cursor)

    if name_index:
        name_length_span = None
        name_span = None
        name_huffman = False
    else:
        name_length = _try_decode_integer(data, cursor, 7)
        if name_length is None:
            return None
        name_len, name_length_len = name_length
        name_huffman = bool(data[cursor] & 0x80)
        name_length_span = (cursor, cursor + name_length_len)
        cursor += name_length_len
        if cursor + name_len > len(data):
            return None
        name_span = (cursor, cursor + name_len)
        cursor += name_len

    value_length = _try_decode_integer(data, cursor, 7)
    if value_length is None:
        return None
    value_len, value_length_len = value_length
    value_huffman = bool(data[cursor] & 0x80)
    value_length_span = (cursor, cursor + value_length_len)
    cursor += value_length_len
    if cursor + value_len > len(data):
        return None
    value_span = (cursor, cursor + value_len)
    cursor += value_len

    layout = FieldLayout(
        kind=kind,
        span=(offset, cursor),
        index=index_span,
        name_length=name_length_span,
        name=name_span,
        value_length=value_length_span,
        value=value_span,
        name_huffman=name_huffman,
        value_huffman=value_huffman,
    )
    return layout, cursor - offset


def parse_header_block_layout(data: bytes | bytearray) -> list[FieldLayout]:
    """
    Parses a complete HPACK header block and returns the layout of every
    field line, in source order.

    :param data: A bytestring containing a complete HPACK header block.
    :raises HPACKDecodingError: If the block ends mid-field.
    """
    fields = []
    offset = 0
    while offset < len(data):
        parsed = _try_parse_field(data, offset)
        if parsed is None:
            msg = "Truncated header block"
            raise HPACKDecodingError(msg)
        layout, consumed = parsed
        fields.append(layout)
        offset += consumed
    return fields


class LayoutRecorder:
    """
    Incrementally records the encoding layout of an HPACK header block that
    arrives as an ordered sequence of byte fragments.

    Fragment boundaries may fall anywhere, including in the middle of
    integer continuation bytes or strings. Bytes that do not yet form a
    complete field line are held as pending until more fragments arrive.
    Call :meth:`finish` when the external end-of-header-block marker is
    received: only then is a pending remainder reported as a truncated
    block.
    """

    def __init__(self) -> None:
        self._fragments: list[bytes] = []
        self._buffer = bytearray()
        self._fields: list[FieldLayout] = []
        self._offset = 0
        self._ended = False

    def feed(self, fragment: bytes | bytearray) -> list[FieldLayout]:
        """
        Appends a fragment and parses as many complete field lines as the
        buffered bytes allow. Returns the layouts parsed from this feed, in
        source order.
        """
        if self._ended:
            msg = "Cannot feed fragments after the header block has ended"
            raise HPACKError(msg)

        fragment = bytes(fragment)
        self._fragments.append(fragment)
        self._buffer += fragment

        parsed_fields = []
        while True:
            parsed = _try_parse_field(self._buffer, self._offset)
            if parsed is None:
                break
            layout, consumed = parsed
            self._fields.append(layout)
            self._offset += consumed
            parsed_fields.append(layout)
        return parsed_fields

    @property
    def fields(self) -> list[FieldLayout]:
        """
        The layouts of all complete field lines parsed so far, in source
        order.
        """
        return list(self._fields)

    @property
    def pending(self) -> bytes:
        """
        Buffered bytes that do not yet form a complete field line.
        """
        return bytes(self._buffer[self._offset:])

    @property
    def ended(self) -> bool:
        """
        Whether the end-of-header-block marker has been received.
        """
        return self._ended

    def finish(self) -> list[FieldLayout]:
        """
        Marks the end of the header block and returns the layouts of all
        field lines, in source order.

        :raises HPACKDecodingError: If undecodable bytes remain pending,
                                    meaning the block is truncated.
        """
        self._ended = True
        if self._offset != len(self._buffer):
            msg = "Truncated header block"
            raise HPACKDecodingError(msg)
        return list(self._fields)

    def reassembled(self) -> list[FieldLayout]:
        """
        Concatenates every fed fragment and re-reads the whole header block
        from scratch. The field order and source byte ranges are identical
        to those recorded incrementally via :meth:`feed`.
        """
        return parse_header_block_layout(b"".join(self._fragments))
