r"""`iso8583.audit` reconciles ISO8583 traffic into a two-level audit report
that answers both "how many messages were received" and "how many business
transactions took place".

Level 1 - packet records. One record per received message. Each record
carries the raw MTI, the message category (request, notification, response),
the repeat (retransmission) flag, and the tracking fields. Packet records
are never merged or dropped, so their count always reflects how many
messages were received.

Level 2 - business records. Packets are folded into business transactions.
A business is identified by its message family (message class and function
pair group taken from the MTI) together with the full value of the tracking
fields (by default field 11 STAN and field 37 RRN). Responses are matched
to the earliest open business with the same key. A response whose request
was never archived is kept as an unmatched business that retains the
sequence number of its source packet.

The repeat flag comes from the MTI origin digit (odd origin = repeat) and,
optionally, from an institution-agreed duplicate field/value pair.

Because the message family is part of the business key, an original request
(e.g. ``0200``) and a repeated notification (e.g. ``0421``) that happen to
share the same tracking number remain two distinct businesses. Matching
never truncates tracking values to their trailing digits.

.. code-block:: python

    >>> import iso8583
    >>> from iso8583 import audit
    >>> from iso8583.specs import default_ascii as spec
    >>> req, _ = iso8583.encode({"t": "0200", "11": "000123", "37": "REF000000001"}, spec)
    >>> resp, _ = iso8583.encode({"t": "0210", "11": "000123", "37": "REF000000001", "39": "00"}, spec)
    >>> report = audit.reconcile([req, resp], spec)
    >>> report.summary()["packets"]
    2
    >>> report.summary()["business_status"]
    {'matched': 1, 'open': 0, 'unmatched_response': 0}
"""

import enum
from dataclasses import dataclass, field
from typing import Any, Container, Dict, Iterable, List, Mapping, Optional, Tuple, Union

import iso8583
from iso8583.decoder import DecodedDict, SpecDict

__all__ = [
    "MessageCategory",
    "PacketRecord",
    "BusinessRecord",
    "AuditReport",
    "reconcile",
]


class MessageCategory(enum.Enum):
    r"""Coarse role of a message derived from the MTI function digit."""

    REQUEST = "request"
    NOTIFICATION = "notification"
    RESPONSE = "response"
    OTHER = "other"


# MTI function digit (3rd digit) -> message category
_FUNCTION_CATEGORY: Dict[str, MessageCategory] = {
    "0": MessageCategory.REQUEST,
    "1": MessageCategory.RESPONSE,
    "2": MessageCategory.NOTIFICATION,
    "3": MessageCategory.RESPONSE,
    "4": MessageCategory.NOTIFICATION,
    "5": MessageCategory.RESPONSE,
}

# MTI function digit -> pair group. A request/notification and its
# response share a pair group so they can be matched to each other.
_FUNCTION_PAIR_GROUP: Dict[str, int] = {
    "0": 0,
    "1": 0,
    "2": 1,
    "3": 1,
    "4": 2,
    "5": 2,
}

# MTI origin digits (4th digit) that mark a retransmission
_REPEAT_ORIGINS = frozenset("13579")

DEFAULT_TRACKING_FIELDS: Tuple[str, ...] = ("11", "37")


@dataclass(frozen=True)
class PacketRecord:
    r"""Level 1 record: one received message.

    Attributes
    ----------
    seq : int
        Position of the message in the reconciled input. Serves as the
        provenance handle for level 2 records.
    mti : str
        Raw Message Type Identifier.
    category : MessageCategory
        Request, notification, response, or other.
    is_repeat : bool
        True if the message is a retransmission (odd MTI origin digit or
        institution-agreed duplicate flag).
    family : tuple
        Message family used for matching: (message class, pair group).
    tracking : dict
        Tracking field values present in the message, e.g. STAN and RRN.
    business_id : int
        ID of the level 2 business record this packet belongs to.
    """

    seq: int
    mti: str
    category: MessageCategory
    is_repeat: bool
    family: Tuple[str, int]
    tracking: Dict[str, str]
    business_id: int


@dataclass
class BusinessRecord:
    r"""Level 2 record: one business transaction.

    Attributes
    ----------
    business_id : int
        Sequential ID of the business record.
    family : tuple
        Message family: (message class, pair group).
    tracking : dict
        Tracking field values identifying the business.
    requests, notifications, responses, others : list of int
        Sequence numbers of the source packets per category.
    repeats : list of int
        Sequence numbers of retransmitted packets.
    """

    business_id: int
    family: Tuple[str, int]
    tracking: Dict[str, str]
    requests: List[int] = field(default_factory=list)
    notifications: List[int] = field(default_factory=list)
    responses: List[int] = field(default_factory=list)
    others: List[int] = field(default_factory=list)
    repeats: List[int] = field(default_factory=list)

    @property
    def status(self) -> str:
        r"""Business status: ``matched``, ``open``, or ``unmatched_response``."""
        has_origin = bool(self.requests or self.notifications or self.others)
        has_response = bool(self.responses)
        if has_origin and has_response:
            return "matched"
        if has_response:
            return "unmatched_response"
        return "open"

    @property
    def packet_seqs(self) -> List[int]:
        r"""Sorted sequence numbers of all source packets of this business."""
        return sorted(
            set(
                self.requests
                + self.notifications
                + self.responses
                + self.others
                + self.repeats
            )
        )


@dataclass
class AuditReport:
    r"""Two-level audit report.

    Attributes
    ----------
    packets : list of PacketRecord
        Level 1 records, one per received message.
    businesses : list of BusinessRecord
        Level 2 records, one per business transaction.
    """

    packets: List[PacketRecord]
    businesses: List[BusinessRecord]

    def unmatched(self) -> List[BusinessRecord]:
        r"""Business records that are not fully matched.

        Includes open businesses (request/notification without response)
        and unmatched responses (response whose request was never archived).
        Each returned record retains the sequence numbers of its source
        packets for provenance.
        """
        return [b for b in self.businesses if b.status != "matched"]

    def summary(self) -> Dict[str, Any]:
        r"""Report counts: how many messages vs. how many businesses."""
        categories: Dict[str, int] = {}
        for packet in self.packets:
            key = packet.category.value
            categories[key] = categories.get(key, 0) + 1

        statuses: Dict[str, int] = {"matched": 0, "open": 0, "unmatched_response": 0}
        for business in self.businesses:
            statuses[business.status] += 1

        return {
            "packets": len(self.packets),
            "packet_categories": categories,
            "repeats": sum(1 for p in self.packets if p.is_repeat),
            "businesses": len(self.businesses),
            "business_status": statuses,
        }


def reconcile(
    messages: Iterable[Union[bytes, bytearray, DecodedDict]],
    spec: Optional[SpecDict] = None,
    tracking_fields: Tuple[str, ...] = DEFAULT_TRACKING_FIELDS,
    duplicate_field: Optional[str] = None,
    duplicate_values: Container[str] = frozenset({"1"}),
) -> AuditReport:
    r"""Reconcile ISO8583 messages into a two-level audit report.

    Parameters
    ----------
    messages : iterable of bytes, bytearray, or dict
        Received messages in their original order. ``bytes``/``bytearray``
        items are decoded with ``spec``; dict items are used as decoded
        data directly.
    spec : dict, optional
        ISO8583 specification required to decode raw messages.
        See :mod:`iso8583.specs` module for examples.
    tracking_fields : tuple of str, optional
        Fields that identify a business together with the message family.
        Full field values are compared; values are never truncated to
        trailing digits. Defaults to ``("11", "37")`` (STAN, RRN).
    duplicate_field : str, optional
        Institution-agreed field carrying a duplicate (retransmission)
        flag, e.g. a private field. When the field value is in
        ``duplicate_values`` the message is treated as a repeat in
        addition to the MTI origin digit check.
    duplicate_values : container of str, optional
        Values of ``duplicate_field`` that mark a duplicate.
        Defaults to ``{"1"}``.

    Returns
    -------
    AuditReport
        Two-level report with packet records and business records.

    Raises
    ------
    ValueError
        Message MTI is missing or is not 4 digits.
    TypeError
        Message is neither bytes, bytearray, nor dict.
    """
    packets: List[PacketRecord] = []
    businesses: List[BusinessRecord] = []
    # business key -> IDs of businesses still waiting for a response
    open_by_key: Dict[Tuple[Tuple[str, int], Tuple[Optional[str], ...]], List[int]] = {}
    # business key -> ID of the first business with this key
    first_by_key: Dict[Tuple[Tuple[str, int], Tuple[Optional[str], ...]], int] = {}

    for seq, message in enumerate(messages):
        doc = _ensure_decoded(message, spec)
        category, family, is_repeat = _classify(doc, duplicate_field, duplicate_values)
        key = (family, tuple(doc.get(f) for f in tracking_fields))

        if category is MessageCategory.RESPONSE:
            queue = open_by_key.get(key)
            if queue:
                business = businesses[queue.pop(0)]
            elif key in first_by_key:
                business = businesses[first_by_key[key]]
            else:
                business = _new_business(businesses, family, doc, tracking_fields)
                first_by_key[key] = business.business_id
            business.responses.append(seq)
        else:
            if is_repeat and key in first_by_key:
                business = businesses[first_by_key[key]]
            else:
                business = _new_business(businesses, family, doc, tracking_fields)
                first_by_key.setdefault(key, business.business_id)
                open_by_key.setdefault(key, []).append(business.business_id)
            if category is MessageCategory.REQUEST:
                business.requests.append(seq)
            elif category is MessageCategory.NOTIFICATION:
                business.notifications.append(seq)
            else:
                business.others.append(seq)

        if is_repeat:
            business.repeats.append(seq)

        packets.append(
            PacketRecord(
                seq=seq,
                mti=doc["t"],
                category=category,
                is_repeat=is_repeat,
                family=family,
                tracking={f: doc[f] for f in tracking_fields if f in doc},
                business_id=business.business_id,
            )
        )

    return AuditReport(packets=packets, businesses=businesses)


def _new_business(
    businesses: List[BusinessRecord],
    family: Tuple[str, int],
    doc: Mapping[str, str],
    tracking_fields: Tuple[str, ...],
) -> BusinessRecord:
    business = BusinessRecord(
        business_id=len(businesses),
        family=family,
        tracking={f: doc[f] for f in tracking_fields if f in doc},
    )
    businesses.append(business)
    return business


def _ensure_decoded(
    message: Union[bytes, bytearray, DecodedDict], spec: Optional[SpecDict]
) -> DecodedDict:
    if isinstance(message, dict):
        return message
    if isinstance(message, (bytes, bytearray)):
        if spec is None:
            raise TypeError("spec is required to reconcile raw ISO8583 data")
        decoded, _ = iso8583.decode(message, spec)
        return decoded
    raise TypeError(
        f"Message must be bytes, bytearray, or dict, not {type(message).__name__}"
    )


def _classify(
    doc: Mapping[str, str],
    duplicate_field: Optional[str],
    duplicate_values: Container[str],
) -> Tuple[MessageCategory, Tuple[str, int], bool]:
    mti = doc.get("t", "")
    if len(mti) != 4 or not mti.isdigit():
        raise ValueError(f"Message MTI must be 4 digits, not {mti!r}")

    function = mti[2]
    category = _FUNCTION_CATEGORY.get(function, MessageCategory.OTHER)
    family = (mti[1], _FUNCTION_PAIR_GROUP.get(function, 3))

    is_repeat = mti[3] in _REPEAT_ORIGINS
    if duplicate_field is not None:
        value = doc.get(duplicate_field)
        if value is not None and value in duplicate_values:
            is_repeat = True

    return category, family, is_repeat
