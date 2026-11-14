"""
HTTP/2 header encoding for Python.
"""
from __future__ import annotations

from .archive import ArchivePlan, ArchivePlanner, Checkpoint, Target, TargetSource, brute_force_minimum, replay
from .exceptions import HPACKDecodingError, HPACKError, InvalidTableIndex, InvalidTableIndexError, InvalidTableSizeError, OversizedHeaderListError
from .hpack import Decoder, Encoder
from .struct import HeaderTuple, NeverIndexedHeaderTuple
from .table import TableState

__all__ = [
    "ArchivePlan",
    "ArchivePlanner",
    "Checkpoint",
    "Decoder",
    "Encoder",
    "HPACKDecodingError",
    "HPACKError",
    "HeaderTuple",
    "InvalidTableIndex",
    "InvalidTableIndexError",
    "InvalidTableSizeError",
    "NeverIndexedHeaderTuple",
    "OversizedHeaderListError",
    "TableState",
    "Target",
    "TargetSource",
    "brute_force_minimum",
    "replay",
]

__version__ = "4.3.0+dev"
