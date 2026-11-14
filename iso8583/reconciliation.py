r"""Reconcile ISO8583 financial transactions with their reversals.

ISO8583 terminals commonly restart their System Trace Audit Number (DE 11)
every business day.  A reversal must therefore never be matched to the first
message with the same trace number.  This module keeps the original reference,
acquirer, terminal, and local date together so that a trace number is only
unique within that composite key.

The reconciliation rules implemented here are intentionally conservative:

* DE 90 (Original Data Elements) is authoritative when it is complete.
* Without a complete DE 90, an original is looked up by the composite key
  ``(STAN, acquirer, terminal, original date)``.  A trace number by itself is
  never enough to create a match, and an undated trace number is never used
  to match across business days.
* If the message carries no original trace number, the amount and a
  configurable time window may be used.  A single candidate is accepted;
  two or more candidates remain pending as ambiguous, even across midnight.
* Reversals may be ingested before their original.  The reversal becomes
  pending and is resolved when the original arrives or when a later resend
  supplies the missing DE 90.
* A resend with the same reversal identity is a duplicate.  Matched amounts
  are applied at most once, so resends and late references cannot reduce the
  balance twice.

The public API is :class:`ReconciliationLedger`, :func:`reconcile`,
:func:`reconcile_file`, :func:`parse_original_data_elements`, and
:func:`load_messages`.  Inputs are the decoded ``dict`` objects returned by
:func:`iso8583.decode`, or raw ISO8583 messages via
:meth:`ReconciliationLedger.ingest_raw`.
"""

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
)

__all__ = [
    "IngestResult",
    "IngestStatus",
    "Match",
    "MatchKind",
    "OriginalDataElements",
    "RecordKind",
    "ReconciliationError",
    "ReconciliationLedger",
    "ReconciliationReport",
    "ReversalStatus",
    "TransactionRecord",
    "load_messages",
    "parse_original_data_elements",
    "reconcile",
    "reconcile_file",
]

_FIELD_MTI = "t"
_FIELD_AMOUNT = "4"
_FIELD_TRANSMISSION_DATETIME = "7"
_FIELD_STAN = "11"
_FIELD_LOCAL_TIME = "12"
_FIELD_LOCAL_DATE = "13"
_FIELD_ACQUIRER = "32"
_FIELD_TERMINAL = "41"
_FIELD_CURRENCY = "49"
_FIELD_ORIGINAL = "90"

_DE90_LEN = 42
_DIGITS_RE = re.compile(r"^[0-9]+$")


class ReconciliationError(ValueError):
    """Raised when an input message cannot be interpreted for reconciliation."""


class RecordKind(str, Enum):
    """Whether a message is an original transaction or a reversal."""

    ORIGINAL = "original"
    REVERSAL = "reversal"


class ReversalStatus(str, Enum):
    """State of a reversal record after matching."""

    MATCHED = "matched"
    PENDING = "pending"
    AMBIGUOUS = "ambiguous"
    DUPLICATE = "duplicate"
    ALREADY_REVERSED = "already_reversed"


class MatchKind(str, Enum):
    """How a reversal was tied to its original transaction."""

    EXACT = "exact"
    COMPOSITE = "composite"
    AMOUNT_TIME = "amount_time"


class IngestStatus(str, Enum):
    """Outcome of a single :meth:`ReconciliationLedger.ingest` call."""

    ORIGINAL = "original"
    ORIGINAL_DUPLICATE = "original_duplicate"
    REVERSAL_MATCHED = "reversal_matched"
    REVERSAL_PENDING = "reversal_pending"
    REVERSAL_AMBIGUOUS = "reversal_ambiguous"
    REVERSAL_DUPLICATE = "reversal_duplicate"
    REVERSAL_RESOLVED = "reversal_resolved"
    REVERSAL_ALREADY_APPLIED = "reversal_already_applied"


@dataclass(frozen=True)
class OriginalDataElements:
    r"""Parsed ISO8583 DE 90 (Original Data Elements).

    The fixed 42-character layout is::

        original MTI             4
        original STAN            6
        original transmission   10  (MMDDhhmmss)
        acquiring institution   11
        forwarding institution  11

    A short or partially masked value is retained so a later resend can fill
    in the missing pieces.  :attr:`is_complete` tells the ledger whether the
    value is strong enough to be authoritative.
    """

    raw: str = ""
    message_type: str = ""
    stan: str = ""
    transmission_datetime: str = ""
    acquiring_institution_id: str = ""
    forwarding_institution_id: str = ""
    separated: bool = False

    def __bool__(self) -> bool:
        return bool(self.raw)

    @classmethod
    def parse(cls, value: Any) -> Optional["OriginalDataElements"]:
        """Parse a DE 90 value.

        Empty values return ``None``.  Values shorter than 42 characters are
        accepted as partial references; they are useful for a pending
        reversal but do not authorize an exact match.  Separated values such
        as ``0200:123456:0101120000:12345678901:12345678901`` are also
        accepted.
        """

        raw = _clean(value)
        if not raw:
            return None

        parts = _split_de90(raw)
        if parts is not None:
            return cls(
                raw=raw,
                message_type=parts[0],
                stan=parts[1],
                transmission_datetime=parts[2],
                acquiring_institution_id=parts[3],
                forwarding_institution_id=parts[4],
                separated=True,
            )

        if len(raw) > _DE90_LEN:
            raise ReconciliationError(
                "Original Data Elements must not exceed 42 characters"
            )

        padded = raw.ljust(_DE90_LEN)

        return cls(
            raw=raw,
            message_type=padded[0:4].strip(),
            stan=padded[4:10].strip(),
            transmission_datetime=padded[10:20].strip(),
            acquiring_institution_id=padded[20:31].strip(),
            forwarding_institution_id=padded[31:42].strip(),
        )

    @property
    def is_complete(self) -> bool:
        """Whether DE 90 identifies an original strongly enough.

        The forwarding institution and trailing padding are optional in many
        samples, so a value is complete as soon as the four identifying
        components are present: original MTI, original STAN, a full
        ``MMDDhhmmss`` timestamp, and an acquiring institution.
        """

        return bool(
            self.message_type
            and self.stan
            and len(self.transmission_datetime) >= 10
            and (self.acquiring_institution_id or self.forwarding_institution_id)
        )

    @property
    def month_day(self) -> Optional[str]:
        """The ``MMDD`` prefix of the original transmission timestamp."""

        if len(self.transmission_datetime) >= 4:
            return self.transmission_datetime[:4]
        return None

    @property
    def time_of_day(self) -> Optional[str]:
        """The ``hhmmss`` suffix of the original transmission timestamp."""

        if len(self.transmission_datetime) >= 10:
            return self.transmission_datetime[4:10]
        return None

    def datetime(self, reference: date) -> Optional[datetime]:
        """Resolve the original timestamp against a nearby reference date."""

        resolved = self.date(reference)
        time_of_day = self.time_of_day
        if resolved is None or time_of_day is None:
            return None
        parsed_time = _parse_time(time_of_day)
        if parsed_time is None:
            return None
        return datetime.combine(resolved, parsed_time)

    def date(self, reference: date) -> Optional[date]:
        """Resolve the ``MMDD`` part against a nearby reference date."""

        month_day = self.month_day
        if month_day is None:
            return None
        return _resolve_date(month_day, reference)

    def format(self) -> str:
        """Return the canonical 42-character DE 90 value."""

        if not self.is_complete:
            raise ReconciliationError("Original Data Elements are incomplete")
        return (
            self.message_type[:4].ljust(4)
            + self.stan[:6].ljust(6)
            + self.transmission_datetime[:10].ljust(10)
            + self.acquiring_institution_id[:11].ljust(11)
            + self.forwarding_institution_id[:11].ljust(11)
        )


def parse_original_data_elements(value: Any) -> Optional[OriginalDataElements]:
    """Public wrapper around :meth:`OriginalDataElements.parse`."""

    return OriginalDataElements.parse(value)


@dataclass
class TransactionRecord:
    """A normalized original or reversal message held by the ledger."""

    id: int
    kind: RecordKind
    mti: str
    stan: str
    acquirer: str
    terminal: str
    amount: Decimal
    local_date: Optional[date]
    local_time: str
    local_datetime: Optional[datetime]
    transmission_datetime: Optional[datetime]
    original: Optional[OriginalDataElements]
    raw: Dict[str, str]
    reference_date: date
    currency: str = ""
    status: Optional[ReversalStatus] = None
    matched_original_id: Optional[int] = None
    matched_reversal_id: Optional[int] = None
    match_kind: Optional[MatchKind] = None
    applied_amount: Decimal = Decimal("0")
    candidate_ids: Tuple[int, ...] = ()
    revision: int = 0

    @property
    def is_reversal(self) -> bool:
        return self.kind is RecordKind.REVERSAL

    @property
    def is_pending(self) -> bool:
        return self.status in (ReversalStatus.PENDING, ReversalStatus.AMBIGUOUS)

    @property
    def reference(self) -> Optional[OriginalDataElements]:
        """The DE 90 reference, if the message carries one."""

        return self.original

    def summary(self) -> Dict[str, Any]:
        """Return a JSON-friendly view of this record."""

        return {
            "id": self.id,
            "kind": self.kind.value,
            "mti": self.mti,
            "stan": self.stan,
            "acquirer": self.acquirer,
            "terminal": self.terminal,
            "amount": str(self.amount),
            "local_date": self.local_date.isoformat() if self.local_date else None,
            "local_time": self.local_time,
            "currency": self.currency,
            "original": self.original.raw if self.original else None,
            "status": self.status.value if self.status else None,
            "matched_original_id": self.matched_original_id,
            "matched_reversal_id": self.matched_reversal_id,
            "match_kind": self.match_kind.value if self.match_kind else None,
            "applied_amount": str(self.applied_amount),
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass(frozen=True)
class Match:
    """A reversal successfully tied to an original transaction."""

    reversal_id: int
    original_id: int
    kind: MatchKind
    amount: Decimal

    def summary(self) -> Dict[str, Any]:
        return {
            "reversal_id": self.reversal_id,
            "original_id": self.original_id,
            "kind": self.kind.value,
            "amount": str(self.amount),
        }


@dataclass(frozen=True)
class IngestResult:
    """The disposition of one ingested message."""

    record: TransactionRecord
    status: IngestStatus
    match: Optional[Match] = None
    candidates: Tuple[TransactionRecord, ...] = ()
    resolved: Tuple[Match, ...] = ()
    updated: bool = False
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.match is not None

    @property
    def pending(self) -> bool:
        return self.status in (
            IngestStatus.REVERSAL_PENDING,
            IngestStatus.REVERSAL_AMBIGUOUS,
        )

    def summary(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "record": self.record.summary(),
            "status": self.status.value,
            "updated": self.updated,
            "reason": self.reason,
            "candidates": [candidate.id for candidate in self.candidates],
            "resolved": [match.summary() for match in self.resolved],
        }
        if self.match is not None:
            data["match"] = self.match.summary()
        if self.candidates:
            data["candidate_refs"] = [
                {
                    "id": candidate.id,
                    "stan": candidate.stan,
                    "acquirer": candidate.acquirer,
                    "terminal": candidate.terminal,
                    "local_date": (
                        candidate.local_date.isoformat()
                        if candidate.local_date
                        else None
                    ),
                    "amount": str(candidate.amount),
                }
                for candidate in self.candidates
            ]
        return data


@dataclass(frozen=True)
class ReconciliationReport:
    """A snapshot of ledger state after one or more ingests."""

    originals: Tuple[TransactionRecord, ...]
    reversals: Tuple[TransactionRecord, ...]
    matches: Tuple[Match, ...]
    pending: Tuple[TransactionRecord, ...]
    ambiguous: Tuple[TransactionRecord, ...]
    duplicates: Tuple[TransactionRecord, ...]
    unapplied: Tuple[TransactionRecord, ...]
    original_total: Decimal
    reversed_total: Decimal
    balance: Decimal

    @property
    def unmatched_originals(self) -> Tuple[TransactionRecord, ...]:
        return tuple(
            record for record in self.originals if record.matched_reversal_id is None
        )

    def summary(self) -> Dict[str, Any]:
        return {
            "original_count": len(self.originals),
            "reversal_count": len(self.reversals),
            "matched_count": len(self.matches),
            "pending_count": len(self.pending),
            "ambiguous_count": len(self.ambiguous),
            "duplicate_count": len(self.duplicates),
            "unapplied_count": len(self.unapplied),
            "original_total": str(self.original_total),
            "reversed_total": str(self.reversed_total),
            "balance": str(self.balance),
            "pending_ids": [record.id for record in self.pending],
            "ambiguous_ids": [record.id for record in self.ambiguous],
            "unapplied_ids": [record.id for record in self.unapplied],
            "matches": [match.summary() for match in self.matches],
        }


class ReconciliationLedger:
    """Match ISO8583 reversals to originals without trace-number collisions.

    Parameters
    ----------
    time_window : timedelta or int, optional
        Look-back window used only when neither DE 90 nor the composite key
        identifies a single original.  An integer is interpreted as seconds.
        The default is 24 hours, which spans a business day while still
        limiting the amount-only candidate set.
    date_hint : date, optional
        Reference date used to assign a year to ``MMDD`` dates.  Defaults to
        the current local date.  ``received_at`` on :meth:`ingest` takes
        precedence for an individual message.
    amount_scale : int, optional
        Decimal places applied to digit-only amounts.  ISO8583 DE 4 carries
        minor units, so a scheme that uses cents can pass ``2`` to turn
        ``000000010000`` into ``100.00``.  The default of ``0`` preserves the
        numbers exactly as received; values containing ``.`` or ``,`` are
        always read as written.  Matching compares normalized amounts, so the
        scale does not change which records pair up.
    """

    def __init__(
        self,
        time_window: Union[timedelta, int] = timedelta(hours=24),
        date_hint: Optional[date] = None,
        amount_scale: int = 0,
    ) -> None:
        if isinstance(time_window, int):
            if time_window < 0:
                raise ValueError("time_window must not be negative")
            time_window = timedelta(seconds=time_window)
        if time_window < timedelta(0):
            raise ValueError("time_window must not be negative")
        if amount_scale < 0:
            raise ValueError("amount_scale must not be negative")
        self.time_window = time_window
        self.date_hint = date_hint or date.today()
        self.amount_scale = amount_scale

        self._records: List[TransactionRecord] = []
        self._originals: List[TransactionRecord] = []
        self._reversals: List[TransactionRecord] = []
        self._matches: List[Match] = []
        self._duplicates: List[TransactionRecord] = []
        self._original_fingerprints: Set[Tuple[Any, ...]] = set()
        self._reversals_by_key: Dict[Tuple[Any, ...], TransactionRecord] = {}
        self._next_id = 1
        self._original_total = Decimal("0")
        self._reversed_total = Decimal("0")

    @property
    def records(self) -> Tuple[TransactionRecord, ...]:
        return tuple(self._records)

    @property
    def originals(self) -> Tuple[TransactionRecord, ...]:
        return tuple(self._originals)

    @property
    def reversals(self) -> Tuple[TransactionRecord, ...]:
        return tuple(self._reversals)

    @property
    def matches(self) -> Tuple[Match, ...]:
        return tuple(self._matches)

    @property
    def pending(self) -> Tuple[TransactionRecord, ...]:
        return tuple(record for record in self._reversals if record.is_pending)

    @property
    def balance(self) -> Decimal:
        """Original amount less the reversals applied exactly once."""

        return self._original_total - self._reversed_total

    def ingest(
        self,
        decoded: Mapping[str, Any],
        *,
        kind: Optional[Union[RecordKind, str]] = None,
        received_at: Optional[Union[datetime, date]] = None,
    ) -> IngestResult:
        """Ingest one decoded ISO8583 message.

        Parameters
        ----------
        decoded : Mapping[str, Any]
            Decoded ISO8583 fields, as returned by :func:`iso8583.decode`.
        kind : RecordKind or str, optional
            Force the message to be treated as an ``"original"`` or
            ``"reversal"``.  By default MTIs starting with ``04`` are
            reversals and all other messages are originals.
        received_at : datetime or date, optional
            Date used to resolve ``MMDD`` values when a year is not present.
        """

        if not isinstance(decoded, Mapping):
            raise TypeError("decoded ISO8583 message must be a mapping")

        record_kind = _coerce_kind(kind, decoded)
        reference_date = _reference_date(received_at, self.date_hint)
        record = self._build_record(decoded, record_kind, reference_date)

        if record_kind is RecordKind.ORIGINAL:
            return self._ingest_original(record)

        return self._ingest_reversal(record)

    def ingest_raw(
        self,
        raw: Union[bytes, bytearray],
        spec: Mapping[str, Mapping[str, Any]],
        *,
        kind: Optional[Union[RecordKind, str]] = None,
        received_at: Optional[Union[datetime, date]] = None,
    ) -> IngestResult:
        """Decode and ingest a raw ISO8583 message."""

        from iso8583.decoder import decode

        decoded, _ = decode(raw, spec)
        return self.ingest(decoded, kind=kind, received_at=received_at)

    def ingest_many(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        kind: Optional[Union[RecordKind, str]] = None,
        received_at: Optional[Union[datetime, date]] = None,
    ) -> Tuple[IngestResult, ...]:
        """Ingest a sequence of decoded messages in order."""

        return tuple(
            self.ingest(message, kind=kind, received_at=received_at)
            for message in messages
        )

    def report(self) -> ReconciliationReport:
        """Return a snapshot of all records and the current balance."""

        pending = self.pending
        ambiguous = tuple(
            record for record in pending if record.status is ReversalStatus.AMBIGUOUS
        )
        pending_only = tuple(
            record for record in pending if record.status is ReversalStatus.PENDING
        )
        unapplied = tuple(
            record
            for record in self._reversals
            if record.status is ReversalStatus.ALREADY_REVERSED
        )
        return ReconciliationReport(
            originals=tuple(self._originals),
            reversals=tuple(self._reversals),
            matches=tuple(self._matches),
            pending=pending_only,
            ambiguous=ambiguous,
            duplicates=tuple(self._duplicates),
            unapplied=unapplied,
            original_total=self._original_total,
            reversed_total=self._reversed_total,
            balance=self.balance,
        )

    def summary(self) -> Dict[str, Any]:
        """Return the report as a JSON-friendly dictionary."""

        return self.report().summary()

    def export(self) -> Dict[str, Any]:
        """Return all records and matches in JSON-friendly form."""

        report = self.report()
        return {
            "originals": [record.summary() for record in self._originals],
            "reversals": [record.summary() for record in self._reversals],
            "records": [record.summary() for record in self._records],
            "matches": [match.summary() for match in self._matches],
            "report": report.summary(),
        }

    #
    # Ingest paths
    #

    def _ingest_original(self, record: TransactionRecord) -> IngestResult:
        fingerprint = _original_fingerprint(record)
        if fingerprint in self._original_fingerprints:
            self._duplicates.append(record)
            return IngestResult(
                record=record,
                status=IngestStatus.ORIGINAL_DUPLICATE,
                reason="identical original transaction was already ingested",
            )

        self._original_fingerprints.add(fingerprint)
        self._originals.append(record)
        self._records.append(record)
        self._original_total += record.amount

        resolved = self._resolve_pending()

        return IngestResult(
            record=record,
            status=IngestStatus.ORIGINAL,
            resolved=resolved,
            reason="original transaction added",
        )

    def _ingest_reversal(self, record: TransactionRecord) -> IngestResult:
        existing = self._reversals_by_key.get(_reversal_key(record))

        if existing is not None:
            if existing.status is ReversalStatus.MATCHED:
                self._duplicates.append(record)
                return IngestResult(
                    record=existing,
                    status=IngestStatus.REVERSAL_DUPLICATE,
                    match=self._match_for_reversal(existing.id),
                    updated=False,
                    reason="the same reversal was already ingested and applied",
                )

            if existing.status is ReversalStatus.ALREADY_REVERSED:
                self._duplicates.append(record)
                return IngestResult(
                    record=existing,
                    status=IngestStatus.REVERSAL_DUPLICATE,
                    updated=False,
                    reason="the original transaction already has an applied reversal",
                )

            if existing.status not in (
                ReversalStatus.PENDING,
                ReversalStatus.AMBIGUOUS,
            ):
                self._duplicates.append(record)
                return IngestResult(
                    record=existing,
                    status=IngestStatus.REVERSAL_DUPLICATE,
                    updated=False,
                    reason="the same reversal was already ingested",
                )

            reference_changed = record.original is not None and (
                existing.original is None
                or record.original.raw != existing.original.raw
            )

            existing.original = record.original or existing.original
            existing.raw = record.raw
            if record.local_datetime is not None:
                existing.local_datetime = record.local_datetime
            if record.transmission_datetime is not None:
                existing.transmission_datetime = record.transmission_datetime
            if record.local_date is not None:
                existing.local_date = record.local_date
            if record.local_time:
                existing.local_time = record.local_time
            if record.amount != 0:
                existing.amount = record.amount
            existing.revision += 1

            if (
                existing.status is ReversalStatus.AMBIGUOUS
                and not reference_changed
            ):
                candidates = self._candidates_for(existing)
                return IngestResult(
                    record=existing,
                    status=IngestStatus.REVERSAL_AMBIGUOUS,
                    candidates=candidates,
                    updated=True,
                    reason=(
                        "resend has no new original reference; ambiguous case "
                        "remains pending"
                    ),
                )

            status, match, candidates, reason = self._evaluate_reversal(existing)
            return IngestResult(
                record=existing,
                status=(
                    IngestStatus.REVERSAL_RESOLVED if match is not None else status
                ),
                match=match,
                candidates=candidates,
                resolved=(match,) if match is not None else (),
                updated=True,
                reason=(
                    "pending reversal reference updated and resolved"
                    if match is not None
                    else "pending reversal reference updated; " + reason
                ),
            )

        self._records.append(record)
        self._reversals.append(record)
        self._reversals_by_key[_reversal_key(record)] = record

        status, match, candidates, reason = self._evaluate_reversal(record)
        return IngestResult(
            record=record,
            status=status,
            match=match,
            candidates=candidates,
            reason=reason,
        )

    #
    # Matching
    #

    def _resolve_pending(self) -> Tuple[Match, ...]:
        """Resolve only reversals whose mismatch was missing data.

        Ambiguous reversals deliberately stay ambiguous until their own
        reference is completed; another reversal being matched must not
        quietly consume one of their candidates.
        """

        resolved: List[Match] = []
        for reversal in self._reversals:
            if reversal.status is not ReversalStatus.PENDING:
                continue
            status, match, _, _ = self._evaluate_reversal(reversal)
            if match is not None:
                resolved.append(match)
            elif status is IngestStatus.REVERSAL_ALREADY_APPLIED:
                reversal.status = ReversalStatus.ALREADY_REVERSED
        return tuple(resolved)

    def _evaluate_reversal(
        self, reversal: TransactionRecord
    ) -> Tuple[IngestStatus, Optional[Match], Tuple[TransactionRecord, ...], str]:
        reference = _build_reference(reversal)
        candidates, match_kind, reason = self._find_candidates(
            reversal, reference, require_unmatched=True
        )

        if not candidates:
            exhausted, _, exhausted_reason = self._find_candidates(
                reversal, reference, require_unmatched=False
            )
            if len(exhausted) == 1 and exhausted[0].matched_reversal_id is not None:
                reversal.status = ReversalStatus.ALREADY_REVERSED
                reversal.candidate_ids = (exhausted[0].id,)
                return (
                    IngestStatus.REVERSAL_ALREADY_APPLIED,
                    None,
                    exhausted,
                    "the referenced original transaction already has an applied "
                    "reversal",
                )
            if len(exhausted) > 1:
                reversal.status = ReversalStatus.AMBIGUOUS
                reversal.candidate_ids = tuple(candidate.id for candidate in exhausted)
                return (
                    IngestStatus.REVERSAL_AMBIGUOUS,
                    None,
                    exhausted,
                    "more than one original transaction still matches the reference",
                )
            reversal.status = ReversalStatus.PENDING
            reversal.candidate_ids = ()
            return (
                IngestStatus.REVERSAL_PENDING,
                None,
                (),
                reason or exhausted_reason,
            )

        if len(candidates) > 1:
            reversal.status = ReversalStatus.AMBIGUOUS
            reversal.candidate_ids = tuple(candidate.id for candidate in candidates)
            return IngestStatus.REVERSAL_AMBIGUOUS, None, candidates, reason

        original = candidates[0]
        if original.matched_reversal_id is not None:
            reversal.status = ReversalStatus.ALREADY_REVERSED
            reversal.candidate_ids = (original.id,)
            return (
                IngestStatus.REVERSAL_ALREADY_APPLIED,
                None,
                (original,),
                "the original transaction already has an applied reversal",
            )
        applied_amount = _applied_amount(reversal.amount, original.amount)
        match = Match(
            reversal_id=reversal.id,
            original_id=original.id,
            kind=match_kind,
            amount=applied_amount,
        )

        reversal.status = ReversalStatus.MATCHED
        reversal.candidate_ids = (original.id,)
        reversal.matched_original_id = original.id
        reversal.match_kind = match_kind
        reversal.applied_amount = applied_amount

        original.matched_reversal_id = reversal.id
        original.match_kind = match_kind

        self._matches.append(match)
        self._reversed_total += applied_amount

        return (
            IngestStatus.REVERSAL_MATCHED,
            match,
            (original,),
            "reversal matched its original transaction",
        )

    def _find_candidates(
        self,
        reversal: TransactionRecord,
        reference: "_Reference",
        *,
        require_unmatched: bool,
    ) -> Tuple[Tuple[TransactionRecord, ...], MatchKind, str]:
        if reference.complete:
            exact = self._narrow_by_amount(
                reversal,
                self._exact_candidates(
                    reference, require_unmatched=require_unmatched
                ),
            )
            if exact:
                return exact, MatchKind.EXACT, "matched complete Original Data Elements"
            return (
                (),
                MatchKind.EXACT,
                "complete Original Data Elements match no original transaction yet",
            )

        composite = self._narrow_by_amount(
            reversal,
            self._composite_candidates(
                reference, require_unmatched=require_unmatched
            ),
        )
        if composite:
            return composite, MatchKind.COMPOSITE, "matched original composite key"
        if reference.has_stan:
            return (
                (),
                MatchKind.COMPOSITE,
                (
                    "the supplied original trace reference does not identify "
                    "one original transaction yet"
                ),
            )

        amount_time = self._amount_time_candidates(
            reversal, reference, require_unmatched=require_unmatched
        )
        if not amount_time:
            return (
                (),
                MatchKind.AMOUNT_TIME,
                "no original transaction matches the amount and time window yet",
            )
        if len(amount_time) > 1:
            return (
                amount_time,
                MatchKind.AMOUNT_TIME,
                (
                    "amount and time window point at more than one original; "
                    "waiting for a complete original reference"
                ),
            )
        return amount_time, MatchKind.AMOUNT_TIME, "matched by amount and time window"

    @staticmethod
    def _narrow_by_amount(
        reversal: TransactionRecord, candidates: Tuple[TransactionRecord, ...]
    ) -> Tuple[TransactionRecord, ...]:
        if len(candidates) <= 1:
            return candidates
        if reversal.amount == 0:
            return candidates
        narrowed = tuple(
            candidate
            for candidate in candidates
            if _amount_matches(reversal.amount, candidate.amount)
        )
        if not narrowed:
            return candidates
        return narrowed

    def _exact_candidates(
        self, reference: "_Reference", *, require_unmatched: bool
    ) -> Tuple[TransactionRecord, ...]:
        if reference.local_date is None:
            return ()
        candidates = tuple(
            original
            for original in self._originals
            if self._is_candidate(original, require_unmatched)
            and _field_matches(reference.message_type, original.mti)
            and _field_matches(reference.stan, original.stan)
            and _institution_matches(reference.acquirer, original.acquirer)
            and _date_matches(reference.local_date, original.local_date)
        )
        return self._narrow_by_terminal(reference.terminal, candidates)

    def _composite_candidates(
        self, reference: "_Reference", *, require_unmatched: bool
    ) -> Tuple[TransactionRecord, ...]:
        if not reference.stan or not reference.date_from_reference:
            return ()
        candidates = tuple(
            original
            for original in self._originals
            if self._is_candidate(original, require_unmatched)
            and _field_matches(reference.stan, original.stan)
            and _institution_matches(reference.acquirer, original.acquirer)
            and _date_matches(reference.local_date, original.local_date)
        )
        return self._narrow_by_terminal(reference.terminal, candidates)

    @staticmethod
    def _narrow_by_terminal(
        terminal: str, candidates: Tuple[TransactionRecord, ...]
    ) -> Tuple[TransactionRecord, ...]:
        if not terminal or len(candidates) <= 1:
            return candidates
        matching = tuple(
            candidate
            for candidate in candidates
            if candidate.terminal == terminal
        )
        return matching or candidates

    def _amount_time_candidates(
        self,
        reversal: TransactionRecord,
        reference: "_Reference",
        *,
        require_unmatched: bool,
    ) -> Tuple[TransactionRecord, ...]:
        # Amount and time are the last resort, used only when the message
        # carries no original trace number.  Two same-amount originals in the
        # window must stay ambiguous rather than letting a heuristic pick one
        # and debit the wrong transaction.
        candidates: List[TransactionRecord] = []
        for original in self._originals:
            if not self._is_candidate(original, require_unmatched):
                continue
            if not _amount_matches(reversal.amount, original.amount):
                continue
            if not _institution_matches(reversal.acquirer, original.acquirer):
                continue
            if not _field_matches(reversal.terminal, original.terminal):
                continue
            if not self._within_time_window(reversal, original, reference):
                continue
            candidates.append(original)
        return tuple(candidates)

    def _within_time_window(
        self,
        reversal: TransactionRecord,
        original: TransactionRecord,
        reference: "_Reference",
    ) -> bool:
        anchor = reference.when or reversal.local_datetime
        original_when = original.transmission_datetime or original.local_datetime
        if anchor is not None and original_when is not None:
            return abs(anchor - original_when) <= self.time_window
        if reversal.local_date is not None and original.local_date is not None:
            return reversal.local_date == original.local_date
        return False

    @staticmethod
    def _is_candidate(
        original: TransactionRecord, require_unmatched: bool
    ) -> bool:
        if not require_unmatched:
            return True
        return original.matched_reversal_id is None

    def _match_for_reversal(self, reversal_id: int) -> Optional[Match]:
        for match in self._matches:
            if match.reversal_id == reversal_id:
                return match
        return None

    def _candidates_for(
        self, reversal: TransactionRecord
    ) -> Tuple[TransactionRecord, ...]:
        wanted = set(reversal.candidate_ids)
        return tuple(
            original for original in self._originals if original.id in wanted
        )

    #
    # Record construction
    #

    def _build_record(
        self,
        decoded: Mapping[str, Any],
        kind: RecordKind,
        reference_date: date,
    ) -> TransactionRecord:
        mti = _field(decoded, _FIELD_MTI)
        stan = _normalize_stan(_field(decoded, _FIELD_STAN))
        acquirer = _normalize_id(_field(decoded, _FIELD_ACQUIRER))
        terminal = _normalize_id(_field(decoded, _FIELD_TERMINAL))
        currency = _field(decoded, _FIELD_CURRENCY)
        amount = _parse_amount(_field(decoded, _FIELD_AMOUNT), self.amount_scale)
        original = OriginalDataElements.parse(_field(decoded, _FIELD_ORIGINAL))

        transmission = _parse_de7(
            _field(decoded, _FIELD_TRANSMISSION_DATETIME), reference_date
        )
        declared_date = _parse_local_date(
            _field(decoded, _FIELD_LOCAL_DATE),
            transmission.date() if transmission is not None else reference_date,
        )
        local_date = declared_date
        if local_date is None and transmission is not None:
            local_date = transmission.date()
        local_time = _parse_clock(_field(decoded, _FIELD_LOCAL_TIME))
        if not local_time and transmission is not None:
            local_time = transmission.strftime("%H%M%S")
        local_datetime = _combine(local_date, local_time)

        record = TransactionRecord(
            id=self._next_id,
            kind=kind,
            mti=mti,
            stan=stan,
            acquirer=acquirer,
            terminal=terminal,
            amount=amount,
            local_date=local_date,
            local_time=local_time,
            local_datetime=local_datetime,
            transmission_datetime=transmission,
            original=original,
            raw=dict(decoded),
            reference_date=reference_date,
            currency=currency,
        )
        self._next_id += 1
        return record


@dataclass(frozen=True)
class _Reference:
    message_type: str
    stan: str
    acquirer: str
    terminal: str
    local_date: Optional[date]
    when: Optional[datetime]
    complete: bool
    date_from_reference: bool
    has_stan: bool

    @property
    def dated(self) -> bool:
        return self.local_date is not None


def _build_reference(reversal: TransactionRecord) -> _Reference:
    original = reversal.original

    if original is None:
        return _Reference(
            message_type="",
            stan="",
            acquirer=reversal.acquirer,
            terminal=reversal.terminal,
            local_date=reversal.local_date,
            when=reversal.local_datetime or reversal.transmission_datetime,
            complete=False,
            date_from_reference=False,
            has_stan=False,
        )

    reference_date = reversal.reference_date
    original_date = original.date(reference_date)
    original_datetime = original.datetime(reference_date)
    has_stan = bool(original.stan)
    complete = original.is_complete and original_date is not None

    # A partial reference still tells us the original trace number.  Without
    # its own MMDD date it may only be matched as a same-day composite key;
    # the reversal's own business date is the reference date in that case.
    reference_date_value = original_date
    if reference_date_value is None and has_stan:
        reference_date_value = reversal.local_date

    return _Reference(
        message_type=original.message_type,
        stan=original.stan,
        acquirer=(
            original.acquiring_institution_id
            or original.forwarding_institution_id
            or reversal.acquirer
        ),
        terminal=reversal.terminal,
        local_date=reference_date_value,
        when=original_datetime
        or reversal.local_datetime
        or reversal.transmission_datetime,
        complete=complete,
        date_from_reference=original_date is not None,
        has_stan=has_stan,
    )


def _reversal_key(record: TransactionRecord) -> Tuple[Any, ...]:
    """Identity of the reversal itself, independent of an enriched DE 90.

    A terminal-day STAN is unique, so a resend that only adds DE 90 still
    updates the same pending reversal.  If the STAN is missing, amount and
    time are the only stable identity available.
    """

    if record.stan:
        return (
            record.stan,
            record.acquirer,
            record.terminal,
            record.local_date.isoformat() if record.local_date else "",
        )
    return (
        "",
        record.acquirer,
        record.terminal,
        record.local_date.isoformat() if record.local_date else "",
        record.local_time,
        str(record.amount),
    )


def _original_fingerprint(record: TransactionRecord) -> Tuple[Any, ...]:
    """Identity of an original for exact-resend detection.

    A terminal reusing a trace number on another business day is a distinct
    transaction.  Within one day the same trace number must also keep its
    amount and time so two separate originals remain available to an
    amount-time ambiguity check.
    """

    return (
        record.mti,
        record.stan,
        record.acquirer,
        record.terminal,
        record.local_date.isoformat() if record.local_date else "",
        record.local_time,
        str(record.amount),
    )


def _field_matches(expected: str, actual: str) -> bool:
    if not expected:
        return True
    return expected == actual


def _institution_matches(expected: str, actual: str) -> bool:
    if not expected:
        return True
    canonical_expected = _canonical_institution(expected)
    if not canonical_expected:
        return True
    return canonical_expected == _canonical_institution(actual)


def _canonical_institution(value: str) -> str:
    return value.strip().lstrip("0")


def _date_matches(expected: Optional[date], actual: Optional[date]) -> bool:
    if expected is None:
        return True
    return expected == actual


def _amount_matches(reversal_amount: Decimal, original_amount: Decimal) -> bool:
    if reversal_amount == 0:
        return False
    return reversal_amount == original_amount


def _applied_amount(reversal_amount: Decimal, original_amount: Decimal) -> Decimal:
    if original_amount <= Decimal("0"):
        return Decimal("0")
    if reversal_amount <= Decimal("0"):
        return original_amount
    return min(reversal_amount, original_amount)


def _coerce_kind(
    kind: Optional[Union[RecordKind, str]], decoded: Mapping[str, Any]
) -> RecordKind:
    if kind is not None:
        if isinstance(kind, RecordKind):
            return kind
        normalized = str(kind).strip().lower()
        if normalized in ("original", "orig", "auth", "transaction"):
            return RecordKind.ORIGINAL
        if normalized in ("reversal", "reverse", "rev", "undo"):
            return RecordKind.REVERSAL
        raise ValueError("kind must be 'original' or 'reversal'")

    mti = _field(decoded, _FIELD_MTI)
    if mti.startswith("04"):
        return RecordKind.REVERSAL
    return RecordKind.ORIGINAL


def _field(decoded: Mapping[str, Any], key: str) -> str:
    value = decoded.get(key, "")
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("ascii", "replace").strip()
    return str(value).strip()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("ascii", "replace").strip()
    return str(value).strip()


def _normalize_stan(value: str) -> str:
    return value.strip()


def _normalize_id(value: str) -> str:
    return value.strip()


def _split_de90(raw: str) -> Optional[Tuple[str, str, str, str, str]]:
    for separator in ("|", ":", ";"):
        if separator in raw:
            parts = [part.strip() for part in raw.split(separator)]
            if len(parts) == 5:
                return (parts[0], parts[1], parts[2], parts[3], parts[4])
    return None


def _parse_amount(value: str, scale: int) -> Decimal:
    if not value:
        return Decimal("0")
    cleaned = value.replace(",", "").replace(" ", "")
    try:
        if scale and re.fullmatch(r"-?[0-9]+", cleaned):
            amount = Decimal(cleaned).scaleb(-scale)
        else:
            amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ReconciliationError(f"invalid transaction amount: {value!r}") from exc
    if not amount.is_finite():
        raise ReconciliationError(f"invalid transaction amount: {value!r}")
    return amount


def _parse_clock(value: str) -> str:
    if len(value) >= 6 and _DIGITS_RE.match(value[:6]):
        return value[:6]
    return ""


def _parse_time(value: str) -> Optional[time]:
    if len(value) < 6 or not _DIGITS_RE.match(value[:6]):
        return None
    hour = int(value[0:2])
    minute = int(value[2:4])
    second = int(value[4:6])
    if hour > 23 or minute > 59 or second > 59:
        return None
    return time(hour, minute, second)


def _parse_de7(value: str, reference: date) -> Optional[datetime]:
    if not value:
        return None
    if len(value) >= 14 and _DIGITS_RE.match(value[:14]):
        try:
            return datetime(
                int(value[0:4]),
                int(value[4:6]),
                int(value[6:8]),
                int(value[8:10]),
                int(value[10:12]),
                int(value[12:14]),
            )
        except ValueError:
            return None
    if len(value) >= 10 and _DIGITS_RE.match(value[:10]):
        resolved = _resolve_date(value[0:4], reference)
        parsed_time = _parse_time(value[4:10])
        if resolved is None or parsed_time is None:
            return None
        return datetime.combine(resolved, parsed_time)
    return None


def _parse_local_date(value: str, reference: date) -> Optional[date]:
    if not value:
        return None
    if len(value) >= 8 and _DIGITS_RE.match(value[:8]):
        try:
            return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
        except ValueError:
            return None
    if len(value) >= 4 and _DIGITS_RE.match(value[:4]):
        return _resolve_date(value[0:4], reference)
    return None


def _resolve_date(month_day: str, reference: date) -> Optional[date]:
    if len(month_day) < 4 or not _DIGITS_RE.match(month_day[:4]):
        return None
    month = int(month_day[0:2])
    day = int(month_day[2:4])

    candidates: List[date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None

    past = [candidate for candidate in candidates if candidate <= reference]
    if past:
        return max(past)
    return min(candidates)


def _combine(value: Optional[date], clock: str) -> Optional[datetime]:
    if value is None or not clock:
        return None
    parsed_time = _parse_time(clock)
    if parsed_time is None:
        return None
    return datetime.combine(value, parsed_time)


def _reference_date(
    received_at: Optional[Union[datetime, date]], fallback: date
) -> date:
    if received_at is None:
        return fallback
    if isinstance(received_at, datetime):
        return received_at.date()
    return received_at


def reconcile(
    messages: Iterable[Mapping[str, Any]],
    *,
    time_window: Union[timedelta, int] = timedelta(hours=24),
    date_hint: Optional[date] = None,
    kind: Optional[Union[RecordKind, str]] = None,
    received_at: Optional[Union[datetime, date]] = None,
    amount_scale: int = 0,
) -> ReconciliationLedger:
    """Convenience wrapper that ingests messages into a new ledger."""

    ledger = ReconciliationLedger(
        time_window=time_window, date_hint=date_hint, amount_scale=amount_scale
    )
    ledger.ingest_many(messages, kind=kind, received_at=received_at)
    return ledger


def load_messages(path: Union[str, Path]) -> List[Dict[str, str]]:
    """Load decoded ISO8583 messages from JSON, JSONL, or CSV.

    JSON may be a list of objects or an object containing ``messages`` or
    ``records``.  CSV files must have a header row; values are read as
    strings so masked samples survive unchanged.
    """

    source = Path(path)
    suffix = source.suffix.lower()

    if suffix in (".jsonl", ".ndjson"):
        messages: List[Dict[str, str]] = []
        with source.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    messages.append(_stringify_message(json.loads(line)))
        return messages

    if suffix == ".json":
        with source.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            for key in ("messages", "records", "transactions"):
                if key in data and isinstance(data[key], list):
                    return [_stringify_message(item) for item in data[key]]
            return [_stringify_message(data)]
        if isinstance(data, list):
            return [_stringify_message(item) for item in data]
        raise ReconciliationError("JSON sample must contain an object or a list")

    if suffix == ".csv":
        with source.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return [
                {str(key): str(value) for key, value in row.items()} for row in reader
            ]

    raise ReconciliationError(
        "unsupported sample format; use .json, .jsonl, .ndjson, or .csv"
    )


def _stringify_message(message: Mapping[str, Any]) -> Dict[str, str]:
    return {
        str(key): "" if value is None else str(value) for key, value in message.items()
    }


def reconcile_file(
    path: Union[str, Path],
    *,
    time_window: Union[timedelta, int] = timedelta(hours=24),
    date_hint: Optional[date] = None,
    kind: Optional[Union[RecordKind, str]] = None,
    amount_scale: int = 0,
) -> ReconciliationLedger:
    """Load a JSON/JSONL/CSV sample and reconcile it in file order."""

    return reconcile(
        load_messages(path),
        time_window=time_window,
        date_hint=date_hint,
        kind=kind,
        amount_scale=amount_scale,
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Command line entry for reconciling a masked sample file."""

    parser = argparse.ArgumentParser(
        prog="python -m iso8583.reconciliation",
        description=(
            "Reconcile decoded ISO8583 originals and reversals while keeping "
            "trace numbers scoped to acquirer, terminal, and local date."
        ),
    )
    parser.add_argument("path", help="JSON, JSONL, or CSV sample file")
    parser.add_argument(
        "--date-hint",
        type=_parse_iso_date,
        default=None,
        help="reference date (YYYY-MM-DD) for MMDD values",
    )
    parser.add_argument(
        "--time-window",
        type=int,
        default=24 * 60 * 60,
        help="amount/time fallback window in seconds (default: 86400)",
    )
    parser.add_argument(
        "--amount-scale",
        type=int,
        default=0,
        help="decimal places for digit-only DE 4 amounts (default: 0)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full ledger export instead of the report summary",
    )
    args = parser.parse_args(argv)

    ledger = reconcile_file(
        args.path,
        time_window=args.time_window,
        date_hint=args.date_hint,
        amount_scale=args.amount_scale,
    )
    payload = ledger.export() if args.json else ledger.summary()
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


if __name__ == "__main__":
    raise SystemExit(main())
