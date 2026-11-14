"""
HTTP/2 header encoding for Python.
"""
from __future__ import annotations

from .exceptions import HPACKDecodingError, HPACKError, InvalidTableIndex, InvalidTableIndexError, InvalidTableSizeError, OversizedHeaderListError
from .hpack import Decoder, Encoder
from .report import (
    EMPTY,
    EMPTY_VALUE,
    REDACTED,
    REDACTED_VALUE,
    Aggregate,
    CleanedField,
    CleanedRecord,
    CleanedReport,
    DuplicateFieldName,
    ErrorSummary,
    FieldStatistics,
    ReportPolicy,
    SanitizationError,
    ValueState,
    clean_record,
    clean_records,
    sanitize_headers,
    sanitize_records,
)
from .struct import HeaderTuple, NeverIndexedHeaderTuple

__all__ = [
    "EMPTY",
    "EMPTY_VALUE",
    "REDACTED",
    "REDACTED_VALUE",
    "Aggregate",
    "CleanedField",
    "CleanedRecord",
    "CleanedReport",
    "Decoder",
    "DuplicateFieldName",
    "Encoder",
    "ErrorSummary",
    "FieldStatistics",
    "HPACKDecodingError",
    "HPACKError",
    "HeaderTuple",
    "InvalidTableIndex",
    "InvalidTableIndexError",
    "InvalidTableSizeError",
    "NeverIndexedHeaderTuple",
    "OversizedHeaderListError",
    "ReportPolicy",
    "SanitizationError",
    "ValueState",
    "clean_record",
    "clean_records",
    "sanitize_headers",
    "sanitize_records",
]

__version__ = "4.3.0+dev"
