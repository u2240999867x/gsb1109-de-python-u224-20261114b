"""
HTTP/2 header encoding for Python.
"""
from __future__ import annotations

from .exceptions import HPACKDecodingError, HPACKError, InvalidTableIndex, InvalidTableIndexError, InvalidTableSizeError, OversizedHeaderListError
from .hpack import Decoder, Encoder
from .layout import FieldLayout, LayoutRecorder, parse_header_block_layout
from .struct import HeaderTuple, NeverIndexedHeaderTuple

__all__ = [
    "Decoder",
    "Encoder",
    "FieldLayout",
    "HPACKDecodingError",
    "HPACKError",
    "HeaderTuple",
    "InvalidTableIndex",
    "InvalidTableIndexError",
    "InvalidTableSizeError",
    "LayoutRecorder",
    "NeverIndexedHeaderTuple",
    "OversizedHeaderListError",
    "parse_header_block_layout",
]

__version__ = "4.3.0+dev"
