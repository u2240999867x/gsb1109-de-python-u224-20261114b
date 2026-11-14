"""
A sanitising report export for HPACK header collection records.

Collection reports frequently contain synthetic requests whose header fields
repeat across records.  The same field name may be sent normally in one record
and with the ``never indexed`` representation in another.  That distinction
carries security meaning: a normal field value may be reported, but the value
of a ``never indexed`` field must not be retained or serialised at all.

This module provides :func:`clean_records` (and the single-record
:func:`clean_record`) as a cleaning export.  Restricted occurrences are
reduced, while the record is being read, to:

* the field name,
* its position in the record and its occurrence order for that name,
* only the aggregate quantities explicitly allowed by the caller through
  :class:`ReportPolicy`.

The raw value of a restricted field is never stored on any of the returned
objects.  It therefore cannot be read back from the detail records, the
statistics record, ``repr()`` output, or from :class:`SanitizationError`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .exceptions import HPACKError
from .struct import HeaderTuple

__all__ = [
    "EMPTY",
    "EMPTY_VALUE",
    "REDACTED",
    "REDACTED_VALUE",
    "Aggregate",
    "CleanedField",
    "CleanedRecord",
    "CleanedReport",
    "DuplicateFieldName",
    "ErrorSummary",
    "FieldStatistics",
    "ReportPolicy",
    "SanitizationError",
    "ValueState",
    "clean_record",
    "clean_records",
    "sanitize_headers",
    "sanitize_records",
]


class ValueState(StrEnum):
    """
    Sentinel states used when a field value is not reported verbatim.

    ``EMPTY`` and ``REDACTED`` are deliberately distinct values.  An ordinary
    empty field is represented by ``EMPTY``; a never-indexed field is always
    represented by ``REDACTED``, even when its real value happened to be
    empty.
    """

    EMPTY = "empty"
    REDACTED = "redacted"


EMPTY = ValueState.EMPTY
"""Marker for an ordinary header field whose value is the empty string."""

REDACTED = ValueState.REDACTED
"""Marker for a never-indexed header field whose value is withheld."""

EMPTY_VALUE = EMPTY
"""Alias for :data:`EMPTY`."""

REDACTED_VALUE = REDACTED
"""Alias for :data:`REDACTED`."""


class Aggregate(StrEnum):
    """
    Quantities a caller may explicitly allow for restricted fields.

    No aggregate is allowed by default.  In particular, the value itself is
    not an aggregate and can never be enabled.
    """

    COUNT = "count"
    """Number of restricted occurrences of the field name."""

    RECORD_COUNT = "record_count"
    """Number of distinct records containing a restricted occurrence."""

    EMPTY_COUNT = "empty_count"
    """Number of restricted occurrences whose original value was empty."""

    VALUE_LENGTH_TOTAL = "value_length_total"
    """Sum of the character lengths of the restricted values."""

    VALUE_LENGTH_MIN = "value_length_min"
    """Smallest character length among the restricted values."""

    VALUE_LENGTH_MAX = "value_length_max"
    """Largest character length among the restricted values."""


@dataclass(frozen=True, slots=True)
class ReportPolicy:
    """
    Caller-controlled policy for a cleaned report.

    :param allowed_aggregates: Restricted-field aggregate quantities the
        caller permits.  Strings or :class:`Aggregate` members are accepted.
        The default is to disclose no restricted aggregate at all.
    :param collect_errors: When true (the default), malformed records are
        represented in :attr:`CleanedReport.errors` and processing continues.
        When false, the first malformed input raises
        :class:`SanitizationError`.
    """

    allowed_aggregates: frozenset[Aggregate] = frozenset()
    collect_errors: bool = True

    def __post_init__(self) -> None:
        value = self.allowed_aggregates
        if isinstance(value, (str, Aggregate)):
            value = (value,)
        try:
            aggregates = frozenset(Aggregate(item) for item in value)
        except (TypeError, ValueError):
            msg = "allowed_aggregates must be an iterable of Aggregate values"
            raise TypeError(msg) from None
        if not isinstance(self.collect_errors, bool):
            msg = "collect_errors must be a bool"
            raise TypeError(msg)
        object.__setattr__(self, "allowed_aggregates", aggregates)

    @property
    def restricted_aggregates(self) -> frozenset[Aggregate]:
        """Alias for :attr:`allowed_aggregates`."""
        return self.allowed_aggregates


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    """A structural error record that never contains a header value."""

    code: str
    message: str
    record_id: str | None = None
    field_name: str | None = None
    position: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe summary without any header value."""
        return {
            "code": self.code,
            "message": self.message,
            "record_id": self.record_id,
            "field_name": self.field_name,
            "position": self.position,
        }


class SanitizationError(HPACKError):
    """
    A malformed input was encountered while building a cleaned report.

    The exception message and summary contain structural details only.  Header
    values are never included, even when the malformed field was never-indexed.
    """

    def __init__(self, error: ErrorSummary) -> None:
        self._error = error
        super().__init__(error.message)

    @property
    def error(self) -> ErrorSummary:
        """The safe, structured error summary."""
        return self._error

    def summary(self) -> dict[str, Any]:
        """Return a JSON-safe summary without any header value."""
        return self._error.to_dict()

    def to_dict(self) -> dict[str, Any]:
        """Alias for :meth:`summary`."""
        return self.summary()


@dataclass(frozen=True, slots=True)
class CleanedField:
    """
    One cleaned occurrence of a header field.

    For an ordinary field :attr:`value` is either the reported string or
    :data:`EMPTY`.  For a restricted field :attr:`value` is always
    :data:`REDACTED`; constructing a restricted instance with any other value
    raises :class:`ValueError`, as does constructing an ordinary instance with
    :data:`REDACTED`.
    """

    name: str
    record_id: str
    position: int
    ordinal: int
    restricted: bool
    value: str | ValueState

    def __post_init__(self) -> None:
        if self.restricted and self.value is not REDACTED:
            msg = "restricted header fields must use REDACTED"
            raise ValueError(msg)
        if not self.restricted and self.value is REDACTED:
            msg = "ordinary header fields cannot use REDACTED"
            raise ValueError(msg)

    @property
    def value_state(self) -> str:
        """``"empty"``, ``"redacted"``, or ``"value"``."""
        if isinstance(self.value, ValueState):
            return self.value.value
        return "value"

    @property
    def is_empty(self) -> bool:
        """Whether this occurrence is an ordinary empty value."""
        return self.value is EMPTY

    @property
    def is_redacted(self) -> bool:
        """Whether this occurrence's value is withheld."""
        return self.value is REDACTED

    @property
    def empty(self) -> bool:
        """Alias for :attr:`is_empty`."""
        return self.is_empty

    @property
    def redacted(self) -> bool:
        """Alias for :attr:`is_redacted`."""
        return self.is_redacted

    def summary_dict(self) -> dict[str, Any]:
        """Return occurrence metadata without any value."""
        return {
            "record_id": self.record_id,
            "position": self.position,
            "ordinal": self.ordinal,
            "restricted": self.restricted,
            "value_state": self.value_state,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation with restricted values withheld."""
        if isinstance(self.value, ValueState):
            value: Any = {"kind": self.value.value}
        else:
            value = self.value
        return {
            "name": self.name,
            "record_id": self.record_id,
            "position": self.position,
            "ordinal": self.ordinal,
            "restricted": self.restricted,
            "value_state": self.value_state,
            "value": value,
        }

    def __repr__(self) -> str:
        if self.restricted:
            value = "<redacted>"
        elif self.value is EMPTY:
            value = "<empty>"
        else:
            value = repr(self.value)
        return (
            f"CleanedField(name={self.name!r}, record_id={self.record_id!r}, "
            f"position={self.position}, ordinal={self.ordinal}, "
            f"restricted={self.restricted}, value={value})"
        )


@dataclass(frozen=True, slots=True)
class CleanedRecord:
    """A record's cleaned field details."""

    record_id: str
    fields: tuple[CleanedField, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation of this record."""
        return {
            "record_id": self.record_id,
            "fields": [field.to_dict() for field in self.fields],
        }


@dataclass(frozen=True, slots=True)
class DuplicateFieldName:
    """
    All occurrences of a field name that appears more than once.

    Ordinary occurrences and restricted occurrences use the same occurrence
    model and therefore appear together in this list.  Ordinary empty values
    are shown as ``EMPTY``; restricted values are shown as ``REDACTED``.
    """

    name: str
    occurrences: tuple[CleanedField, ...]

    @property
    def restricted_occurrences(self) -> tuple[CleanedField, ...]:
        """The restricted members of this duplicate group."""
        return tuple(occurrence for occurrence in self.occurrences if occurrence.restricted)

    @property
    def empty_occurrences(self) -> tuple[CleanedField, ...]:
        """The ordinary empty members of this duplicate group."""
        return tuple(occurrence for occurrence in self.occurrences if occurrence.is_empty)

    def summary_dict(self) -> dict[str, Any]:
        """Return the group's value-free occurrence metadata."""
        return {
            "name": self.name,
            "occurrences": [occurrence.summary_dict() for occurrence in self.occurrences],
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation of this duplicate group."""
        return {
            "name": self.name,
            "occurrences": [occurrence.to_dict() for occurrence in self.occurrences],
        }


@dataclass(frozen=True, slots=True)
class FieldStatistics:
    """
    Aggregate statistics for one field name.

    ``restricted`` contains only the aggregates explicitly allowed by the
    report policy.  It never contains a value or a value-derived string.
    """

    name: str
    occurrences: int
    ordinary_occurrences: int
    empty_occurrences: int
    duplicate: bool
    restricted: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "restricted", dict(self.restricted))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation of these statistics."""
        return {
            "name": self.name,
            "occurrences": self.occurrences,
            "ordinary_occurrences": self.ordinary_occurrences,
            "empty_occurrences": self.empty_occurrences,
            "duplicate": self.duplicate,
            "restricted": dict(self.restricted),
        }


@dataclass(frozen=True, slots=True)
class CleanedReport:
    """The result of cleaning one or more collection records."""

    records: tuple[CleanedRecord, ...]
    duplicates: tuple[DuplicateFieldName, ...]
    statistics: tuple[FieldStatistics, ...]
    errors: tuple[ErrorSummary, ...]
    policy: ReportPolicy

    @property
    def fields(self) -> tuple[CleanedField, ...]:
        """Every cleaned field occurrence, in report order."""
        return tuple(field for record in self.records for field in record.fields)

    @property
    def restricted_fields(self) -> tuple[CleanedField, ...]:
        """Every restricted field occurrence, in report order."""
        return tuple(field for field in self.fields if field.restricted)

    @property
    def duplicate_field_names(self) -> tuple[DuplicateFieldName, ...]:
        """Alias for :attr:`duplicates`."""
        return self.duplicates

    @property
    def field_statistics(self) -> tuple[FieldStatistics, ...]:
        """Alias for :attr:`statistics`."""
        return self.statistics

    def to_dict(self) -> dict[str, Any]:
        """
        Return the full report as JSON-safe data.

        Ordinary values appear in the detail records.  Restricted values only
        ever appear as :data:`REDACTED`.
        """
        return {
            "policy": self._policy_dict(),
            "record_count": len(self.records),
            "records": [record.to_dict() for record in self.records],
            "duplicates": [duplicate.to_dict() for duplicate in self.duplicates],
            "statistics": [statistics.to_dict() for statistics in self.statistics],
            "errors": [error.to_dict() for error in self.errors],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """Serialise :meth:`to_dict` using :func:`json.dumps`."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def statistics_record(self) -> dict[str, Any]:
        """
        Return a statistics-only record.

        This deliberately omits the detail records, so neither ordinary nor
        restricted values are present.  Error summaries are also value-free.
        """
        return {
            "policy": self._policy_dict(),
            "record_count": len(self.records),
            "field_names": [statistics.name for statistics in self.statistics],
            "statistics": [statistics.to_dict() for statistics in self.statistics],
            "duplicates": [duplicate.summary_dict() for duplicate in self.duplicates],
            "errors": [error.to_dict() for error in self.errors],
        }

    def statistics_json(self, *, indent: int | None = None) -> str:
        """Serialise :meth:`statistics_record` using :func:`json.dumps`."""
        return json.dumps(self.statistics_record(), ensure_ascii=False, indent=indent)

    def statistics_by_name(self) -> dict[str, FieldStatistics]:
        """Return the per-field statistics keyed by field name."""
        return {statistics.name: statistics for statistics in self.statistics}

    def _policy_dict(self) -> dict[str, list[str]]:
        return {
            "allowed_aggregates": sorted(aggregate.value for aggregate in self.policy.allowed_aggregates),
        }

    def __repr__(self) -> str:
        return (
            f"CleanedReport(record_count={len(self.records)}, "
            f"duplicate_names={len(self.duplicates)}, "
            f"statistics={len(self.statistics)}, errors={len(self.errors)})"
        )


@dataclass(slots=True)
class _NameAccumulator:
    occurrences: int = 0
    ordinary_occurrences: int = 0
    empty_occurrences: int = 0
    restricted_count: int = 0
    restricted_empty_count: int = 0
    restricted_total_length: int = 0
    restricted_min_length: int | None = None
    restricted_max_length: int | None = None
    restricted_records: set[str] = field(default_factory=set)


def _error(
    code: str,
    message: str,
    *,
    record_id: str | None = None,
    field_name: str | None = None,
    position: int | None = None,
) -> ErrorSummary:
    return ErrorSummary(
        code=code,
        message=message,
        record_id=record_id,
        field_name=field_name,
        position=position,
    )


def _record_error(
    errors: list[ErrorSummary],
    error: ErrorSummary,
    policy: ReportPolicy,
) -> None:
    if not policy.collect_errors:
        raise SanitizationError(error) from None
    errors.append(error)


def _as_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _record_id(value: object, fallback: str) -> tuple[str, ErrorSummary | None]:
    if isinstance(value, str):
        return value, None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8"), None
        except UnicodeDecodeError:
            return fallback, _error(
                "invalid_record_id",
                "record id bytes are not valid UTF-8",
            )
    return fallback, _error("invalid_record_id", "record id must be bytes or str")


def _invalid_header(errors: list[ErrorSummary], policy: ReportPolicy, error: ErrorSummary) -> None:
    _record_error(errors, error, policy)


def _pair_from_sequence(
    header: tuple[Any, ...] | list[Any],
    *,
    record_id: str,
    position: int,
    errors: list[ErrorSummary],
    policy: ReportPolicy,
) -> tuple[tuple[Any, Any], bool] | None:
    if len(header) == 2:
        return (header[0], header[1]), False
    if len(header) != 3:
        _invalid_header(
            errors,
            policy,
            _error(
                "invalid_header",
                "header field must have two or three elements",
                record_id=record_id,
                position=position,
            ),
        )
        return None
    if not isinstance(header[2], bool):
        _invalid_header(
            errors,
            policy,
            _error(
                "invalid_sensitive_flag",
                "header sensitive flag must be a bool",
                record_id=record_id,
                position=position,
            ),
        )
        return None
    return (header[0], header[1]), header[2]


def _header_pair(
    header: object,
    *,
    record_id: str,
    position: int,
    errors: list[ErrorSummary],
    policy: ReportPolicy,
) -> tuple[tuple[Any, Any], bool] | None:
    if isinstance(header, HeaderTuple):
        if len(header) != 2:
            _invalid_header(
                errors,
                policy,
                _error(
                    "invalid_header",
                    "header field must be a (name, value) tuple",
                    record_id=record_id,
                    position=position,
                ),
            )
            return None
        return (header[0], header[1]), not header.indexable
    if isinstance(header, (tuple, list)):
        return _pair_from_sequence(
            header,
            record_id=record_id,
            position=position,
            errors=errors,
            policy=policy,
        )
    _invalid_header(
        errors,
        policy,
        _error(
            "invalid_header",
            "header field must be a tuple or list",
            record_id=record_id,
            position=position,
        ),
    )
    return None


def _header_text(
    name_value: tuple[Any, Any],
    *,
    record_id: str,
    position: int,
    errors: list[ErrorSummary],
    policy: ReportPolicy,
) -> tuple[str, str] | None:
    name = _as_text(name_value[0])
    if name is None:
        _invalid_header(
            errors,
            policy,
            _error(
                "invalid_header_name",
                "header field name must be UTF-8 bytes or str",
                record_id=record_id,
                position=position,
            ),
        )
        return None

    value = _as_text(name_value[1])
    if value is None:
        _invalid_header(
            errors,
            policy,
            _error(
                "invalid_header_value",
                "header field value must be UTF-8 bytes or str",
                record_id=record_id,
                field_name=name,
                position=position,
            ),
        )
        return None
    return name, value


def _parse_header(
    header: object,
    *,
    record_id: str,
    position: int,
    errors: list[ErrorSummary],
    policy: ReportPolicy,
) -> tuple[str, str, bool] | None:
    pair = _header_pair(
        header,
        record_id=record_id,
        position=position,
        errors=errors,
        policy=policy,
    )
    if pair is None:
        return None
    name_value, restricted = pair
    text = _header_text(
        name_value,
        record_id=record_id,
        position=position,
        errors=errors,
        policy=policy,
    )
    if text is None:
        return None
    name, value = text
    return name, value, restricted


def _iteration_error(error: Exception, subject: str) -> ErrorSummary:
    return _error(
        f"{subject}_iteration_error",
        f"{subject} iterator raised {type(error).__name__}",
    )


def _is_record_pair(item: object) -> bool:
    return isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], (str, bytes)) and not isinstance(item[1], (str, bytes))


def _iter_mapping(
    records: Mapping[Any, Any],
    *,
    errors: list[ErrorSummary],
    policy: ReportPolicy,
) -> Iterator[tuple[str, Any]]:
    try:
        for index, (key, headers) in enumerate(records.items(), 1):
            record_id, id_error = _record_id(key, f"record-{index}")
            if id_error is not None:
                _record_error(errors, id_error, policy)
            yield record_id, headers
    except SanitizationError:
        raise
    except Exception as error:  # noqa: BLE001
        _record_error(errors, _iteration_error(error, "records"), policy)


def _iter_sequence(
    records: Iterable[Any],
    *,
    errors: list[ErrorSummary],
    policy: ReportPolicy,
) -> Iterator[tuple[str, Any]]:
    try:
        for index, item in enumerate(records, 1):
            fallback = f"record-{index}"
            if _is_record_pair(item):
                record_id, id_error = _record_id(item[0], fallback)
                if id_error is not None:
                    _record_error(errors, id_error, policy)
                yield record_id, item[1]
            else:
                yield fallback, item
    except SanitizationError:
        raise
    except Exception as error:  # noqa: BLE001
        _record_error(errors, _iteration_error(error, "records"), policy)


def _iter_records(
    records: Mapping[Any, Any] | Iterable[Any],
    *,
    errors: list[ErrorSummary],
    policy: ReportPolicy,
) -> Iterator[tuple[str, Any]]:
    if isinstance(records, Mapping):
        yield from _iter_mapping(records, errors=errors, policy=policy)
        return

    if isinstance(records, (str, bytes)) or not isinstance(records, Iterable):
        _record_error(
            errors,
            _error(
                "invalid_records",
                "records must be a mapping or an iterable of records",
            ),
            policy,
        )
        return

    yield from _iter_sequence(records, errors=errors, policy=policy)


def _accumulate_field(
    accumulator: _NameAccumulator,
    *,
    record_id: str,
    value: str,
    restricted: bool,
) -> str | ValueState:
    if restricted:
        accumulator.restricted_count += 1
        accumulator.restricted_records.add(record_id)
        length = len(value)
        if not value:
            accumulator.restricted_empty_count += 1
        accumulator.restricted_total_length += length
        if accumulator.restricted_min_length is None or length < accumulator.restricted_min_length:
            accumulator.restricted_min_length = length
        if accumulator.restricted_max_length is None or length > accumulator.restricted_max_length:
            accumulator.restricted_max_length = length
        return REDACTED

    accumulator.ordinary_occurrences += 1
    if not value:
        accumulator.empty_occurrences += 1
        return EMPTY
    return value


def _apply_restricted_aggregates(
    accumulator: _NameAccumulator,
    policy: ReportPolicy,
) -> dict[str, int]:
    allowed = policy.allowed_aggregates
    result: dict[str, int] = {}
    if Aggregate.COUNT in allowed:
        result[Aggregate.COUNT.value] = accumulator.restricted_count
    if Aggregate.RECORD_COUNT in allowed:
        result[Aggregate.RECORD_COUNT.value] = len(accumulator.restricted_records)
    if Aggregate.EMPTY_COUNT in allowed:
        result[Aggregate.EMPTY_COUNT.value] = accumulator.restricted_empty_count
    if Aggregate.VALUE_LENGTH_TOTAL in allowed:
        result[Aggregate.VALUE_LENGTH_TOTAL.value] = accumulator.restricted_total_length
    if Aggregate.VALUE_LENGTH_MIN in allowed:
        minimum = accumulator.restricted_min_length
        result[Aggregate.VALUE_LENGTH_MIN.value] = minimum if minimum is not None else 0
    if Aggregate.VALUE_LENGTH_MAX in allowed:
        maximum = accumulator.restricted_max_length
        result[Aggregate.VALUE_LENGTH_MAX.value] = maximum if maximum is not None else 0
    return result


@dataclass(slots=True)
class _ReportBuilder:
    policy: ReportPolicy
    errors: list[ErrorSummary] = field(default_factory=list)
    records: list[CleanedRecord] = field(default_factory=list)
    occurrences: dict[str, list[CleanedField]] = field(default_factory=dict)
    accumulators: dict[str, _NameAccumulator] = field(default_factory=dict)
    name_order: list[str] = field(default_factory=list)

    def add_record(self, record_id: str, headers: object) -> None:
        if isinstance(headers, (str, bytes)) or not isinstance(headers, Iterable):
            _record_error(
                self.errors,
                _error(
                    "invalid_record_headers",
                    "record headers must be an iterable of header fields",
                    record_id=record_id,
                ),
                self.policy,
            )
            return

        fields: list[CleanedField] = []
        try:
            for position, header in enumerate(headers, 1):
                cleaned = self._add_header(record_id, position, header)
                if cleaned is not None:
                    fields.append(cleaned)
        except SanitizationError:
            raise
        except Exception as error:  # noqa: BLE001
            _record_error(
                self.errors,
                _error(
                    "headers_iteration_error",
                    f"headers iterator raised {type(error).__name__}",
                    record_id=record_id,
                ),
                self.policy,
            )

        self.records.append(CleanedRecord(record_id=record_id, fields=tuple(fields)))

    def _add_header(
        self,
        record_id: str,
        position: int,
        header: object,
    ) -> CleanedField | None:
        parsed = _parse_header(
            header,
            record_id=record_id,
            position=position,
            errors=self.errors,
            policy=self.policy,
        )
        if parsed is None:
            return None
        name, value, restricted = parsed

        if name not in self.accumulators:
            self.accumulators[name] = _NameAccumulator()
            self.occurrences[name] = []
            self.name_order.append(name)
        accumulator = self.accumulators[name]
        accumulator.occurrences += 1
        reported_value = _accumulate_field(
            accumulator,
            record_id=record_id,
            value=value,
            restricted=restricted,
        )
        cleaned = CleanedField(
            name=name,
            record_id=record_id,
            position=position,
            ordinal=len(self.occurrences[name]) + 1,
            restricted=restricted,
            value=reported_value,
        )
        self.occurrences[name].append(cleaned)
        return cleaned

    def build(self) -> CleanedReport:
        duplicates = tuple(
            DuplicateFieldName(name=name, occurrences=tuple(self.occurrences[name])) for name in self.name_order if len(self.occurrences[name]) > 1
        )
        statistics = tuple(
            FieldStatistics(
                name=name,
                occurrences=self.accumulators[name].occurrences,
                ordinary_occurrences=self.accumulators[name].ordinary_occurrences,
                empty_occurrences=self.accumulators[name].empty_occurrences,
                duplicate=len(self.occurrences[name]) > 1,
                restricted=_apply_restricted_aggregates(
                    self.accumulators[name],
                    self.policy,
                ),
            )
            for name in self.name_order
        )
        return CleanedReport(
            records=tuple(self.records),
            duplicates=duplicates,
            statistics=statistics,
            errors=tuple(self.errors),
            policy=self.policy,
        )


def clean_records(
    records: Mapping[Any, Iterable[Any]] | Iterable[Any],
    *,
    policy: ReportPolicy | None = None,
) -> CleanedReport:
    """
    Clean one or more header collection records.

    ``records`` may be a mapping of record ids to iterables of header fields,
    or an iterable of ``(record_id, headers)`` pairs.  For convenience, an
    iterable of bare header iterables is also accepted and receives generated
    ``record-N`` ids.

    Header fields may be :class:`~hpack.HeaderTuple`,
    :class:`~hpack.NeverIndexedHeaderTuple`, two-tuples, or three-tuples whose
    final element is the sensitive flag.

    Restricted values are not stored.  Their length and emptiness statistics
    are accumulated while reading and then discarded.
    """
    report_policy = policy or ReportPolicy()
    builder = _ReportBuilder(policy=report_policy)
    for record_id, headers in _iter_records(
        records,
        errors=builder.errors,
        policy=report_policy,
    ):
        builder.add_record(record_id, headers)
    return builder.build()


def clean_record(
    headers: Iterable[Any],
    *,
    record_id: str = "record-1",
    policy: ReportPolicy | None = None,
) -> CleanedReport:
    """Clean a single record and return a one-record report."""
    return clean_records({record_id: headers}, policy=policy)


def sanitize_records(
    records: Mapping[Any, Iterable[Any]] | Iterable[Any],
    *,
    policy: ReportPolicy | None = None,
) -> CleanedReport:
    """Alias for :func:`clean_records`."""
    return clean_records(records, policy=policy)


def sanitize_headers(
    headers: Iterable[Any],
    *,
    record_id: str = "record-1",
    policy: ReportPolicy | None = None,
) -> CleanedReport:
    """Alias for :func:`clean_record`."""
    return clean_record(headers, record_id=record_id, policy=policy)
