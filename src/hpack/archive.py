"""
Lossless migration helpers for archived HPACK header blocks.

The historical archive pipeline decoded header blocks with
``Decoder.decode(..., raw=False)``, which raises
:class:`HPACKDecodingError <hpack.HPACKDecodingError>` as soon as any
single field is not valid UTF-8, causing the whole batch to be dropped.

These helpers decode in raw mode instead, so every field survives. Each
field keeps its original bytes as the lossless source of truth and is
annotated with a text view, a decode status, and the byte positions of
any UTF-8 errors. Field order and duplicate fields are preserved exactly
as decoded, so re-running a partition yields the same sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .struct import HeaderTuple, NeverIndexedHeaderTuple

if TYPE_CHECKING:
    from collections.abc import Iterable  # pragma: no cover

    from .hpack import Decoder  # pragma: no cover

#: Decode status for a field that is valid UTF-8.
UTF8_OK = "ok"

#: Decode status for a field that contains invalid UTF-8 byte sequences.
UTF8_INVALID = "invalid"


def _utf8_error_positions(data: bytes) -> tuple[int, ...]:
    """
    Returns the byte offsets at which ``data`` fails to decode as UTF-8.

    Each position is the first byte of an invalid sequence, matching the
    sequences that the text view replaces with U+FFFD.
    """
    positions: list[int] = []
    offset = 0
    while offset < len(data):
        try:
            data[offset:].decode("utf-8")
        except UnicodeDecodeError as err:
            positions.append(offset + err.start)
            offset += max(err.end, err.start + 1)
        else:
            break
    return tuple(positions)


@dataclass(frozen=True)
class ArchivedField:
    """
    A single archived header field.

    ``name`` and ``value`` are the ordered raw bytes exactly as decoded
    from the header block and are the lossless source of truth. The text
    views, decode statuses, and error positions are derived from them and
    are safe to use for display and analysis, but must not be used as
    identity: a value that legitimately contains U+FFFD and a value with
    invalid bytes share a text view, while their raw bytes (and therefore
    their :attr:`business_key`) differ.
    """

    name: bytes
    value: bytes
    indexable: bool = True

    @property
    def name_text(self) -> str:
        """
        Text view of the field name. Invalid UTF-8 sequences are shown as
        U+FFFD replacement characters.
        """
        return self.name.decode("utf-8", errors="replace")

    @property
    def value_text(self) -> str:
        """
        Text view of the field value. Invalid UTF-8 sequences are shown as
        U+FFFD replacement characters.
        """
        return self.value.decode("utf-8", errors="replace")

    @property
    def name_error_positions(self) -> tuple[int, ...]:
        """
        Byte offsets of invalid UTF-8 sequences within the field name.
        """
        return _utf8_error_positions(self.name)

    @property
    def value_error_positions(self) -> tuple[int, ...]:
        """
        Byte offsets of invalid UTF-8 sequences within the field value.
        """
        return _utf8_error_positions(self.value)

    @property
    def name_status(self) -> str:
        """
        Decode status of the field name: ``UTF8_OK`` or ``UTF8_INVALID``.
        """
        return UTF8_INVALID if self.name_error_positions else UTF8_OK

    @property
    def value_status(self) -> str:
        """
        Decode status of the field value: ``UTF8_OK`` or ``UTF8_INVALID``.
        """
        return UTF8_INVALID if self.value_error_positions else UTF8_OK

    @property
    def business_key(self) -> tuple[bytes, bytes]:
        """
        Identity of this field for deduplication and join keys.

        Derived from the raw bytes, never from the text views, so a value
        that legitimately contains U+FFFD never collides with a value that
        merely renders as U+FFFD because of invalid bytes.
        """
        return (self.name, self.value)

    def to_header_tuple(self) -> HeaderTuple:
        """
        Restores this field as a raw-bytes :class:`HeaderTuple
        <hpack.HeaderTuple>`, preserving the original indexing semantics.
        Lossless for valid and invalid UTF-8 sequences alike.
        """
        if not self.indexable:
            return NeverIndexedHeaderTuple(self.name, self.value)
        return HeaderTuple(self.name, self.value)


def decode_for_archive(decoder: Decoder, data: bytes) -> list[ArchivedField]:
    """
    Decodes one HPACK header block into an ordered list of
    :class:`ArchivedField`.

    Decoding happens in raw mode, so fields that are not valid UTF-8 no
    longer abort the batch. Field order and duplicate fields are preserved
    exactly as they appear in the block.

    :param decoder: The :class:`Decoder <hpack.Decoder>` holding the
                    compression state for this connection or partition.
    :param data: A bytestring representing a complete HPACK-encoded header
                 block.
    """
    return [ArchivedField(name=bytes(header[0]), value=bytes(header[1]), indexable=header.indexable) for header in decoder.decode(data, raw=True)]


def restore_header_block(fields: Iterable[ArchivedField]) -> list[HeaderTuple]:
    """
    Restores archived fields to an ordered list of raw-bytes header tuples.

    Valid and invalid UTF-8 sequences round-trip losslessly, and duplicate
    fields keep their original order and count, so the result can be
    re-encoded or compared against a fresh decode of the same partition.
    """
    return [field.to_header_tuple() for field in fields]
