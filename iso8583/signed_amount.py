r"""Parse legacy fixed-length amount fields that carry the sign in the
last nibble.

Some legacy and private ISO 8583 amount fields are binary-coded decimal
(BCD) fixed-length fields where the last nibble is not a digit but a sign
code, for example ``00 00 01 2C`` representing a positive amount of 12 and
``00 00 00 0D`` representing a declared negative zero.

Sign nibble convention
----------------------
The recognized sign nibbles follow the common mainframe overpunch table
used by these legacy fields:

============================  ==========
Sign nibble                   Meaning
============================  ==========
``C``, ``E`` or ``F``         positive (``F`` also means unsigned)
``D`` or ``B``                negative (``D`` is the standard code)
anything else (``A``, 0-9...) invalid sign code
============================  ==========

Only the even-nibble, no-pad BCD layout keeps the sign nibble verbatim.
A field measured in nibbles with ``left_pad`` or ``right_pad`` configured
would treat a nibble as padding and lose it. Use
:func:`signed_amount_field_spec` to build a matching field specification.

Verbatim preservation
---------------------
The parser never rebuilds the field from the numeric value. Both the
original bytes (``raw``) and the full upper-case nibble string
(``nibbles``) are retained and are the authoritative source when the
value is written back to the original specification. Formatting the
numeric value cannot recover the original bit string, because the same
numeric value may legitimately appear in fields of different business
widths or with different sign declarations such as negative zero.

Examples
--------
>>> from iso8583.signed_amount import parse_signed_amount
>>> amount = parse_signed_amount(b"\x00\x00\x01\x2c")
>>> amount.status.value
'positive'
>>> amount.value
12
>>> amount.nibbles
'0000012C'
>>> amount.to_decimal()
Decimal('0.12')
>>> parse_signed_amount(b"\x00\x00\x00\x0d").status.value
'negative_zero'
>>> parse_signed_amount(b"\x00\x00\x01\x2a").value is None
True
>>> parse_signed_amount(b"").status.value
'empty'
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Union

__all__ = [
    "AmountStatus",
    "SignedAmount",
    "POSITIVE_SIGN_NIBBLES",
    "NEGATIVE_SIGN_NIBBLES",
    "parse_signed_amount",
    "parse_signed_amount_nibbles",
    "build_signed_amount",
    "signed_amount_field_spec",
]

#: Sign nibbles marking a positive amount. ``F`` doubles as "unsigned".
POSITIVE_SIGN_NIBBLES = frozenset(("C", "E", "F"))

#: Sign nibbles marking a negative amount.
NEGATIVE_SIGN_NIBBLES = frozenset(("D", "B"))

_HEX_NIBBLES = frozenset("0123456789ABCDEF")
_BCD_DIGITS = frozenset("0123456789")


class AmountStatus(str, Enum):
    r"""Classification of a trailing-sign amount field."""

    #: Field is absent or zero bytes long.
    EMPTY = "empty"
    #: Zero magnitude with a positive sign nibble.
    ZERO = "zero"
    #: Zero magnitude with a negative sign nibble, a distinct raw declaration.
    NEGATIVE_ZERO = "negative_zero"
    #: Non-zero magnitude with a positive sign nibble.
    POSITIVE = "positive"
    #: Non-zero magnitude with a negative sign nibble.
    NEGATIVE = "negative"
    #: Last nibble is not a recognized positive or negative sign code.
    INVALID_SIGN = "invalid_sign"


@dataclass(frozen=True)
class SignedAmount:
    r"""Parsed view of a trailing-sign amount field.

    Attributes
    ----------
    raw : bytes
        Original field bytes exactly as received.
    nibbles : str
        Upper-case hex nibble string, including the sign nibble. This and
        ``raw`` are the authoritative bit string for re-encoding.
    width : int
        Business width of the field in nibbles.
    sign_nibble : str
        Last nibble of the field, upper case. Empty for an empty field.
    digits : str
        All nibbles except the sign nibble.
    status : AmountStatus
        Classification of the field, distinguishing negative zero, normal
        zero, an empty field and an invalid sign code.
    value : int or None
        Signed numeric magnitude. ``None`` for an empty field or an
        invalid sign code. Negative zero reports ``0``.
    is_negative : bool or None
        ``True`` for a negative sign, ``False`` for a positive sign and
        ``None`` for an empty field or an invalid sign code.
    """

    raw: bytes
    nibbles: str
    width: int
    sign_nibble: str
    digits: str
    status: AmountStatus
    value: Optional[int]
    is_negative: Optional[bool]

    def to_nibbles(self) -> str:
        r"""Return the verbatim nibble string to re-encode.

        Returns
        -------
        str
            Upper-case nibble string, identical to the decoded field data.
        """
        return self.nibbles

    def to_bytes(self) -> bytes:
        r"""Return the verbatim field bytes.

        Returns
        -------
        bytes
            Original field bytes, never reconstructed from the value.
        """
        return self.raw

    def to_decimal(self, decimal_places: int = 2) -> Optional[Decimal]:
        r"""Return an analysis-ready fixed-point numeric view.

        Parameters
        ----------
        decimal_places : int, optional
            Implied number of decimal places, 2 by default.

        Returns
        -------
        Decimal or None
            The signed amount scaled by ``10**-decimal_places``, or
            ``None`` for an empty field or an invalid sign code.
        """
        if self.value is None:
            return None
        if decimal_places < 0:
            raise ValueError("decimal_places must be non-negative")
        return Decimal(self.value).scaleb(-decimal_places)


def parse_signed_amount(
    data: Union[bytes, bytearray], width: Optional[int] = None
) -> SignedAmount:
    r"""Parse a trailing-sign BCD amount field from raw field bytes.

    Parameters
    ----------
    data : bytes or bytearray
        Raw fixed-length field bytes. An empty instance represents an
        absent field.
    width : int, optional
        Expected field width in nibbles. Defaults to the width implied by
        ``len(data)``.

    Returns
    -------
    SignedAmount
        Parsed amount retaining the original bytes and nibble string.

    Raises
    ------
    TypeError
        ``data`` is not bytes or bytearray.
    ValueError
        The width is odd or mismatched, or a digit position holds a
        non-decimal nibble.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            f"Signed amount data must be bytes or bytearray, not "
            f"{data.__class__.__name__}"
        )

    raw = bytes(data)
    implied_width = len(raw) * 2

    if width is None:
        width = implied_width
    elif width != implied_width:
        raise ValueError(
            f"Field data is {implied_width} nibbles wide, expecting {width}"
        )

    if len(raw) == 0:
        if width != 0:
            raise ValueError(f"Field data is 0 nibbles wide, expecting {width}")
        return SignedAmount(b"", "", 0, "", "", AmountStatus.EMPTY, None, None)

    return _parse_nibbles(raw.hex().upper(), width, raw)


def parse_signed_amount_nibbles(
    nibbles: str, width: Optional[int] = None
) -> SignedAmount:
    r"""Parse a trailing-sign BCD amount from its nibble string.

    This is the counterpart of :func:`parse_signed_amount` for the
    upper-case hex string produced by :func:`iso8583.decode` for a binary
    nibble field.

    Parameters
    ----------
    nibbles : str
        Hex nibble string including the trailing sign nibble. An empty
        string represents an absent field.
    width : int, optional
        Expected field width in nibbles. Defaults to ``len(nibbles)``.

    Returns
    -------
    SignedAmount
        Parsed amount retaining the nibble string and its bytes.

    Raises
    ------
    TypeError
        ``nibbles`` is not a string.
    ValueError
        The width is odd or mismatched, the string is not valid hex, or a
        digit position holds a non-decimal nibble.
    """
    if not isinstance(nibbles, str):
        raise TypeError(
            f"Signed amount nibbles must be a string, not {nibbles.__class__.__name__}"
        )

    normalized = nibbles.upper()
    implied_width = len(normalized)

    if width is None:
        width = implied_width
    elif width != implied_width:
        raise ValueError(
            f"Field data is {implied_width} nibbles wide, expecting {width}"
        )

    if normalized == "":
        return SignedAmount(b"", "", 0, "", "", AmountStatus.EMPTY, None, None)

    if any(nibble not in _HEX_NIBBLES for nibble in normalized):
        raise ValueError("Signed amount contains non-hex nibble data")
    if implied_width & 1:
        raise ValueError("Trailing-sign amount field must have an even nibble width")

    return _parse_nibbles(normalized, width, bytes.fromhex(normalized))


def build_signed_amount(digits: str, sign_nibble: str, width: int) -> bytes:
    r"""Build raw field bytes for a trailing-sign amount field.

    Parameters
    ----------
    digits : str
        Decimal digit nibbles without the sign nibble.
    sign_nibble : str
        Trailing sign nibble, for example ``"C"`` or ``"D"``.
    width : int
        Total field width in nibbles including the sign nibble.

    Returns
    -------
    bytes
        Raw field bytes for the given nibble string.

    Raises
    ------
    ValueError
        The width is odd or mismatched, the digits are not decimal, or the
        sign nibble is not a recognized sign code.
    """
    if not isinstance(digits, str) or not isinstance(sign_nibble, str):
        raise TypeError("Digits and sign nibble must be strings")

    sign = sign_nibble.upper()
    if width <= 0 or width & 1:
        raise ValueError("Trailing-sign amount field must have an even nibble width")
    if width != len(digits) + 1:
        raise ValueError(
            f"Field data is {len(digits) + 1} nibbles wide, expecting {width}"
        )
    if any(digit not in _BCD_DIGITS for digit in digits):
        raise ValueError("Signed amount contains non-decimal digit nibbles")
    if sign not in POSITIVE_SIGN_NIBBLES and sign not in NEGATIVE_SIGN_NIBBLES:
        raise ValueError(f"Unrecognized sign nibble {sign_nibble!r}")

    return bytes.fromhex(f"{digits}{sign}")


def signed_amount_field_spec(width: int, desc: str) -> Dict[str, Any]:
    r"""Build a fixed-length BCD field specification for a signed amount.

    The field is measured in nibbles without any pad character, so the
    trailing sign nibble is preserved verbatim instead of being removed as
    padding. The width must be even because a nibble field always occupies
    whole bytes.

    Parameters
    ----------
    width : int
        Fixed field width in nibbles, including the trailing sign nibble.
    desc : str
        Field description used by :func:`iso8583.pp`.

    Returns
    -------
    dict
        Field specification suitable for embedding into an ISO 8583 spec.
    """
    if width <= 0 or width & 1:
        raise ValueError("Trailing-sign amount field must have an even nibble width")

    return {
        "data_enc": "b",
        "len_enc": "ascii",
        "len_type": 0,
        "max_len": width,
        "len_count": "nibbles",
        "desc": desc,
    }


def _parse_nibbles(nibbles: str, width: int, raw: bytes) -> SignedAmount:
    if width & 1:
        raise ValueError("Trailing-sign amount field must have an even nibble width")

    sign_nibble = nibbles[-1]
    digits = nibbles[:-1]

    if any(digit not in _BCD_DIGITS for digit in digits):
        raise ValueError("Signed amount contains non-decimal digit nibbles")

    magnitude = int(digits)

    if sign_nibble in POSITIVE_SIGN_NIBBLES:
        status = AmountStatus.ZERO if magnitude == 0 else AmountStatus.POSITIVE
        return SignedAmount(
            raw, nibbles, width, sign_nibble, digits, status, magnitude, False
        )

    if sign_nibble in NEGATIVE_SIGN_NIBBLES:
        status = (
            AmountStatus.NEGATIVE_ZERO
            if magnitude == 0
            else AmountStatus.NEGATIVE
        )
        return SignedAmount(
            raw, nibbles, width, sign_nibble, digits, status, -magnitude, True
        )

    return SignedAmount(
        raw, nibbles, width, sign_nibble, digits, AmountStatus.INVALID_SIGN, None, None
    )
