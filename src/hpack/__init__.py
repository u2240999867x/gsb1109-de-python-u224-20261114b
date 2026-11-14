"""
HTTP/2 header encoding for Python.
"""
from __future__ import annotations

from .exceptions import HPACKDecodingError, HPACKError, InvalidTableIndex, InvalidTableIndexError, InvalidTableSizeError, OversizedHeaderListError
from .history import (
    REQUEST,
    RESPONSE,
    BlockDecoder,
    BlockRef,
    BlockSequenceError,
    CompressionContext,
    DecodedBlock,
    DecodedField,
    FieldRow,
    HeaderBlockRef,
    Representation,
)
from .hpack import Decoder, Encoder
from .struct import HeaderTuple, NeverIndexedHeaderTuple

__all__ = [
    "REQUEST",
    "RESPONSE",
    "BlockDecoder",
    "BlockRef",
    "BlockSequenceError",
    "CompressionContext",
    "DecodedBlock",
    "DecodedField",
    "Decoder",
    "Encoder",
    "FieldRow",
    "HPACKDecodingError",
    "HPACKError",
    "HeaderBlockRef",
    "HeaderTuple",
    "InvalidTableIndex",
    "InvalidTableIndexError",
    "InvalidTableSizeError",
    "NeverIndexedHeaderTuple",
    "OversizedHeaderListError",
    "Representation",
]

__version__ = "4.3.0+dev"
