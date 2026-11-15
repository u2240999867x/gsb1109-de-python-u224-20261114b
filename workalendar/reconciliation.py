"""
Reconciliation windows between two calendars.

This module computes "reconciliation windows" when a sender and a receiver
work with two different working-day calendars.

Two contracts are supported:

``same_day``
    A handover can happen on a date that is a working day for **both** the
    sender and the receiver.  Dispatch and receipt happen on that same date.

``next_working_day``
    A parcel can be dispatched on any working day of the sender.  The receiver
    signs it on the receiver's **next** working day, strictly after the
    dispatch date.  This is not a same-day handover, even when both calendars
    are open on the dispatch date.

The second contract is deliberately directional: changing which calendar is
the sender changes the result.  Both dates are expressed in the local
Gregorian calendar; the calendars only decide whether a date is a working
day.

Receipt lookup is independent of the query interval: only the dispatch side
is filtered by ``start``/``end``.  A receipt may therefore lie after ``end``.
This makes adjacent sub-queries compose to the same result as one query over
the whole interval, even when a boundary falls inside a holiday.  Window
grouping is presentation only and is also recomputed from the same pairs.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Iterable, List, Sequence, Tuple, Union

from .core import CoreCalendar, cleaned_date, daterange
from .exceptions import CalendarError, UnsupportedContract

SAME_DAY = "same_day"
NEXT_WORKING_DAY = "next_working_day"
BOTH = "both"

CONTRACTS = (SAME_DAY, NEXT_WORKING_DAY)

__all__ = [
    "BOTH",
    "CONTRACTS",
    "NEXT_WORKING_DAY",
    "SAME_DAY",
    "Contract",
    "ReconciliationCalendar",
    "ReconciliationPlan",
    "ReconciliationWindow",
    "get_dispatch_receipts",
    "get_reconciliation_windows",
    "get_same_day_handovers",
    "normalize_contract",
]

# Hard bound for the receiver's next-working-day search.  One year is far
# beyond any real holiday closure, and it turns a misconfigured calendar that
# never reopens into a fast, explicit error instead of an infinite loop.
MAX_RECEIPT_SEARCH_DAYS = 366

_CONTRACT_ALIASES = {
    "same_day": SAME_DAY,
    "same_day_handover": SAME_DAY,
    "same_day_handovers": SAME_DAY,
    "same_day_transfer": SAME_DAY,
    "both_working": SAME_DAY,
    "both_working_days": SAME_DAY,
    "direct": SAME_DAY,
    "handover": SAME_DAY,
    "next_working_day": NEXT_WORKING_DAY,
    "next_working_day_receipt": NEXT_WORKING_DAY,
    "next_day": NEXT_WORKING_DAY,
    "dispatch": NEXT_WORKING_DAY,
    "dispatch_receipt": NEXT_WORKING_DAY,
    "transit": NEXT_WORKING_DAY,
    "shipping": NEXT_WORKING_DAY,
    "both": BOTH,
    "all": BOTH,
}


class Contract(str, Enum):
    """The two reconciliation contracts, usable as plain strings too."""

    SAME_DAY = SAME_DAY
    NEXT_WORKING_DAY = NEXT_WORKING_DAY
    BOTH = BOTH


ContractLike = Union[str, Contract, None]


def normalize_contract(contract: ContractLike = None) -> str:
    """Return a canonical contract name.

    ``None`` (or ``"both"``) means *all* contracts.  A few explicit aliases
    are accepted so callers do not have to depend on the internal spelling.
    """
    if contract is None:
        return BOTH
    if isinstance(contract, Enum):
        contract = contract.value
    if not isinstance(contract, str):
        raise UnsupportedContract(
            f"Unsupported reconciliation contract: {contract!r}")
    key = contract.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return _CONTRACT_ALIASES[key]
    except KeyError:
        raise UnsupportedContract(
            f"Unsupported reconciliation contract: {contract!r}. "
            f"Choose one of: {', '.join(CONTRACTS)} or {BOTH}.")


def _clean_range(start, end) -> Tuple[date, date]:
    """Normalise a date interval, including ``datetime`` inputs."""
    start = cleaned_date(start)
    end = cleaned_date(end)
    if start > end:
        start, end = end, start
    return start, end


@dataclass(frozen=True)
class ReconciliationWindow:
    """A contiguous block of reconciliation opportunities.

    ``dispatch_start`` and ``dispatch_end`` are inclusive.  For the same-day
    contract, ``receipt_start``/``receipt_end`` mirror the dispatch range.  For
    the next-working-day contract, all dispatches in the range share one
    receipt date, so both receipt bounds are that date (and are equal).

    The sender and receiver calendar names are attached to every window so a
    caller never has to remember which pair produced it.
    """

    contract: str
    dispatch_start: date
    dispatch_end: date
    receipt_start: date
    receipt_end: date
    sender_calendar: str
    receiver_calendar: str

    def __post_init__(self):
        contract = normalize_contract(self.contract)
        if contract == BOTH:
            raise UnsupportedContract(
                "A window must use one concrete contract, not 'both'")
        object.__setattr__(self, "contract", contract)
        if self.dispatch_start > self.dispatch_end:
            raise ValueError("dispatch_start must not be after dispatch_end")
        if self.receipt_start > self.receipt_end:
            raise ValueError("receipt_start must not be after receipt_end")
        if self.receipt_start < self.dispatch_start:
            raise ValueError("receipt cannot precede dispatch")
        if contract == SAME_DAY:
            if (self.dispatch_start, self.dispatch_end) != (
                    self.receipt_start, self.receipt_end):
                raise ValueError(
                    "same-day windows must have identical dispatch/receipt "
                    "ranges")
        elif self.dispatch_end > self.receipt_start:
            raise ValueError(
                "next-working-day receipts must be after the dispatch range")

    @property
    def dispatch_dates(self) -> Tuple[date, ...]:
        return tuple(daterange(self.dispatch_start, self.dispatch_end))

    @property
    def receipt_dates(self) -> Tuple[date, ...]:
        return tuple(daterange(self.receipt_start, self.receipt_end))

    @property
    def send_dates(self) -> Tuple[date, ...]:
        return self.dispatch_dates

    @property
    def receive_dates(self) -> Tuple[date, ...]:
        return self.receipt_dates

    @property
    def dispatch_date(self) -> date:
        """The only dispatch date, for single-dispatch windows."""
        if self.dispatch_start != self.dispatch_end:
            raise ValueError("window contains more than one dispatch date")
        return self.dispatch_start

    @property
    def receipt_date(self) -> date:
        """The only receipt date, for single-receipt windows."""
        if self.receipt_start != self.receipt_end:
            raise ValueError("window contains more than one receipt date")
        return self.receipt_start

    @property
    def send_start(self) -> date:
        return self.dispatch_start

    @property
    def send_end(self) -> date:
        return self.dispatch_end

    @property
    def receive_start(self) -> date:
        return self.receipt_start

    @property
    def receive_end(self) -> date:
        return self.receipt_end

    @property
    def is_same_day(self) -> bool:
        return self.contract == SAME_DAY

    @property
    def is_next_working_day(self) -> bool:
        return self.contract == NEXT_WORKING_DAY

    @property
    def start(self) -> date:
        return self.dispatch_start

    @property
    def end(self) -> date:
        return self.receipt_end

    @property
    def calendars(self) -> dict:
        return self.calendar_names

    @property
    def sending_calendar(self) -> str:
        return self.sender_calendar

    @property
    def receiving_calendar(self) -> str:
        return self.receiver_calendar

    @property
    def calendar_names(self) -> dict:
        return {
            "sender": self.sender_calendar,
            "receiver": self.receiver_calendar,
        }

    def as_dict(self) -> dict:
        """Serialisable representation, convenient for reports."""
        return {
            "contract": self.contract,
            "dispatch_start": self.dispatch_start.isoformat(),
            "dispatch_end": self.dispatch_end.isoformat(),
            "receipt_start": self.receipt_start.isoformat(),
            "receipt_end": self.receipt_end.isoformat(),
            "sender_calendar": self.sender_calendar,
            "receiver_calendar": self.receiver_calendar,
        }


@dataclass(frozen=True)
class ReconciliationPlan:
    """Both contracts for one interval, with the calendars named."""

    start: date
    end: date
    sender_calendar: str
    receiver_calendar: str
    same_day: Tuple[ReconciliationWindow, ...]
    next_working_day: Tuple[ReconciliationWindow, ...]

    @property
    def calendar_names(self) -> dict:
        return {
            "sender": self.sender_calendar,
            "receiver": self.receiver_calendar,
        }

    @property
    def windows(self) -> Tuple[ReconciliationWindow, ...]:
        return self.same_day + self.next_working_day

    @property
    def dispatch_receipt_windows(self) -> Tuple[ReconciliationWindow, ...]:
        return self.next_working_day

    @property
    def calendars(self) -> dict:
        return self.calendar_names

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "sender_calendar": self.sender_calendar,
            "receiver_calendar": self.receiver_calendar,
            "same_day": [w.as_dict() for w in self.same_day],
            "next_working_day": [w.as_dict() for w in self.next_working_day],
        }

    def by_contract(self, contract: ContractLike = None) -> Tuple[
            ReconciliationWindow, ...]:
        contract = normalize_contract(contract)
        if contract == SAME_DAY:
            return self.same_day
        if contract == NEXT_WORKING_DAY:
            return self.next_working_day
        return self.windows


class ReconciliationCalendar:
    """Pair a sender calendar with a receiver calendar.

    The pair keeps the two calendars independent: each side is only ever
    queried through its own ``is_working_day`` / ``add_working_days`` methods.
    No date is assumed to be a working day because the other side is open.
    """

    def __init__(self, sender: CoreCalendar, receiver: CoreCalendar,
                 sender_name: str = None, receiver_name: str = None,
                 max_search_days: int = MAX_RECEIPT_SEARCH_DAYS):
        sender = self._as_calendar(sender, "sender")
        receiver = self._as_calendar(receiver, "receiver")
        if not isinstance(max_search_days, int) or max_search_days < 1:
            raise ValueError("max_search_days must be a positive integer")
        self.sender = sender
        self.receiver = receiver
        self.sender_calendar = sender_name or sender.name
        self.receiver_calendar = receiver_name or receiver.name
        self.max_search_days = max_search_days

    @staticmethod
    def _as_calendar(value, role: str) -> CoreCalendar:
        if isinstance(value, CoreCalendar):
            return value
        if isinstance(value, type) and issubclass(value, CoreCalendar):
            return value()
        raise TypeError(f"{role} must be a CoreCalendar instance or class")

    # -- primitive queries -------------------------------------------------
    def is_common_working_day(self, day) -> bool:
        """True when the day is a working day for both sides."""
        day = cleaned_date(day)
        return (self.sender.is_working_day(day)
                and self.receiver.is_working_day(day))

    def get_common_working_days(self, start, end) -> List[date]:
        """Dates in ``[start, end]`` that are working days on both sides."""
        start, end = _clean_range(start, end)
        return [day for day in daterange(start, end)
                if self.is_common_working_day(day)]

    def get_sender_working_days(self, start, end) -> List[date]:
        """Dates in ``[start, end]`` that are working days for the sender."""
        start, end = _clean_range(start, end)
        return [day for day in daterange(start, end)
                if self.sender.is_working_day(day)]

    def get_next_working_day(self, day) -> date:
        """First receiver working day strictly after ``day``.

        ``add_working_days(day, 1)`` is used instead of
        ``find_following_working_day`` because the latter ignores holidays.
        Strictness is intentional: the next-working-day contract never falls
        back to a same-day handover when both calendars happen to be open.

        The search is bounded by ``max_search_days`` so a misconfigured
        receiver that never reopens raises :class:`CalendarError` instead of
        looping forever.
        """
        day = cleaned_date(day)
        candidate = day
        for _ in range(self.max_search_days):
            candidate += timedelta(days=1)
            if self.receiver.is_working_day(candidate):
                return candidate
        raise CalendarError(
            f"No {self.receiver_calendar} working day found within "
            f"{self.max_search_days} days after {day.isoformat()}")

    def get_dispatch_receipts(self, start, end) -> List[Tuple[date, date]]:
        """One ``(dispatch, receipt)`` pair per sender working day.

        The dispatch date is filtered by the query interval.  The receipt is
        always the receiver's next working day according to the receiver
        calendar, so it may fall after ``end``; this keeps the mapping stable
        when a query boundary cuts through a receiver holiday.
        """
        start, end = _clean_range(start, end)
        return [
            (day, self.get_next_working_day(day))
            for day in self.get_sender_working_days(start, end)
        ]

    # -- same-day contract -------------------------------------------------
    def get_same_day_handovers(self, start, end) -> List[date]:
        """Dates on which both sides can hand over on the same day."""
        return self.get_common_working_days(start, end)

    def get_same_day_windows(self, start, end) -> List[ReconciliationWindow]:
        """Contiguous runs of common working days."""
        days = self.get_same_day_handovers(start, end)
        return list(self._group_same_day(days))

    def _group_same_day(self, days: Sequence[date],
                        ) -> Iterable[ReconciliationWindow]:
        if not days:
            return
        run_start = previous = days[0]
        for day in days[1:]:
            if day != previous + timedelta(days=1):
                yield self._same_day_window(run_start, previous)
                run_start = day
            previous = day
        yield self._same_day_window(run_start, previous)

    def _same_day_window(self, start: date, end: date,
                         ) -> ReconciliationWindow:
        return ReconciliationWindow(
            contract=SAME_DAY,
            dispatch_start=start,
            dispatch_end=end,
            receipt_start=start,
            receipt_end=end,
            sender_calendar=self.sender_calendar,
            receiver_calendar=self.receiver_calendar,
        )

    # -- next-working-day contract ----------------------------------------
    def get_dispatch_windows(self, start, end) -> List[ReconciliationWindow]:
        """Contiguous dispatch runs that share one receiver receipt date.

        A long receiver holiday produces the characteristic shape of this
        contract: several sender working days, and therefore several dispatch
        dates, all collapse into one receipt date.  Consecutive dispatch dates
        with different receipts form separate windows.
        """
        pairs = self.get_dispatch_receipts(start, end)
        return list(self._group_dispatch(pairs))

    def get_receipt_groups(self, start, end) -> dict:
        """Map each receipt date to all dispatch dates in the query range."""
        groups = {}
        for dispatch, receipt in self.get_dispatch_receipts(start, end):
            groups.setdefault(receipt, []).append(dispatch)
        return groups

    def get_dispatch_receipt_map(self, start, end) -> dict:
        """Map each dispatch date to its receipt date."""
        return dict(self.get_dispatch_receipts(start, end))

    def _group_dispatch(self, pairs: Sequence[Tuple[date, date]],
                        ) -> Iterable[ReconciliationWindow]:
        if not pairs:
            return
        run_start = previous_day = pairs[0][0]
        receipt = pairs[0][1]
        for day, next_receipt in pairs[1:]:
            if (day != previous_day + timedelta(days=1)
                    or next_receipt != receipt):
                yield self._dispatch_window(run_start, previous_day, receipt)
                run_start, receipt = day, next_receipt
            previous_day = day
        yield self._dispatch_window(run_start, previous_day, receipt)

    def _dispatch_window(self, start: date, end: date, receipt: date,
                         ) -> ReconciliationWindow:
        return ReconciliationWindow(
            contract=NEXT_WORKING_DAY,
            dispatch_start=start,
            dispatch_end=end,
            receipt_start=receipt,
            receipt_end=receipt,
            sender_calendar=self.sender_calendar,
            receiver_calendar=self.receiver_calendar,
        )

    # -- combined entry points ---------------------------------------------
    def get_windows(self, start, end, contract: ContractLike = None,
                    ) -> List[ReconciliationWindow]:
        """Windows for one contract, or for both contracts when omitted."""
        contract = normalize_contract(contract)
        start, end = _clean_range(start, end)
        if contract == SAME_DAY:
            return self.get_same_day_windows(start, end)
        if contract == NEXT_WORKING_DAY:
            return self.get_dispatch_windows(start, end)
        return sorted(
            self.get_same_day_windows(start, end)
            + self.get_dispatch_windows(start, end),
            key=lambda window: (
                window.dispatch_start,
                0 if window.is_same_day else 1,
            ),
        )

    # Common aliases, so the public API can be used without guessing names.
    split = get_windows
    windows = get_windows
    reconciliation_windows = get_windows
    get_same_day_handover_dates = get_same_day_handovers
    get_dispatch_receipt_windows = get_dispatch_receipts
    get_next_working_day_windows = get_dispatch_windows
    same_day_windows = get_same_day_windows
    dispatch_windows = get_dispatch_windows
    receipt_groups = get_receipt_groups

    def plan(self, start, end) -> ReconciliationPlan:
        """Both contracts for the interval, with the calendars named."""
        start, end = _clean_range(start, end)
        return ReconciliationPlan(
            start=start,
            end=end,
            sender_calendar=self.sender_calendar,
            receiver_calendar=self.receiver_calendar,
            same_day=tuple(self.get_same_day_windows(start, end)),
            next_working_day=tuple(self.get_dispatch_windows(start, end)),
        )


# Module-level shortcuts -----------------------------------------------------

def get_reconciliation_windows(sender: CoreCalendar, receiver: CoreCalendar,
                               start, end, contract: ContractLike = None,
                               ) -> List[ReconciliationWindow]:
    """Shortcut for ``ReconciliationCalendar(...).get_windows(...)``."""
    return ReconciliationCalendar(sender, receiver).get_windows(
        start, end, contract=contract)


def get_same_day_handovers(sender: CoreCalendar, receiver: CoreCalendar,
                           start, end) -> List[date]:
    """Shortcut returning same-day handover dates for the pair."""
    return ReconciliationCalendar(sender,
                                  receiver).get_same_day_handovers(start, end)


def get_dispatch_receipts(sender: CoreCalendar, receiver: CoreCalendar,
                          start, end) -> List[Tuple[date, date]]:
    """Shortcut returning ``(dispatch, receipt)`` pairs for the pair."""
    return ReconciliationCalendar(sender,
                                  receiver).get_dispatch_receipts(start, end)
