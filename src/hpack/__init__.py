"""
HTTP/2 header encoding for Python.
"""
from __future__ import annotations

from .checkpoint import dumps_checkpoint, load_checkpoint, loads_checkpoint, save_checkpoint
from .exceptions import (
    HPACKDecodingError,
    HPACKError,
    InvalidTableIndex,
    InvalidTableIndexError,
    InvalidTableSizeError,
    InvalidTableStateError,
    OversizedHeaderListError,
)
from .hpack import Decoder, Encoder
from .struct import HeaderTuple, NeverIndexedHeaderTuple

__all__ = [
    "Decoder",
    "Encoder",
    "HPACKDecodingError",
    "HPACKError",
    "HeaderTuple",
    "InvalidTableIndex",
    "InvalidTableIndexError",
    "InvalidTableSizeError",
    "InvalidTableStateError",
    "NeverIndexedHeaderTuple",
    "OversizedHeaderListError",
    "dumps_checkpoint",
    "load_checkpoint",
    "loads_checkpoint",
    "save_checkpoint",
]

__version__ = "4.3.0+dev"
