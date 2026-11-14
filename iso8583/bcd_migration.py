r"""Migrate historical BCD fields between two incompatible padding dialects.

A partner changed two *independent* rules of an odd-length Binary-Coded
Decimal (BCD) field at the same time:

1. **Padding rule** - where the nibble that rounds the value up to a whole
   byte is placed. The legacy specification right-pads (``"123"`` becomes
   ``"\x12\x30"``); the new specification left-pads (``"123"`` becomes
   ``"\x01\x23"``).
2. **Length rule** - what the field length prefix counts. The legacy
   specification counts every nibble on the wire, including the pad nibble.
   The new specification counts only the valid (business) nibbles.

These two rules are modelled separately by :class:`BcdSide`. The conversion
entry :func:`convert_field` keeps the decoded business number as an exact
string, so its width is always preserved (``"01230"`` never collapses to
``"123"``), and re-encodes it independently for every side.

A nibble is half a byte, so an odd number of valid nibbles always needs one
pad nibble to fill the last byte. The length prefix itself is encoded as BCD.

Example
-------
The legacy side right-pads and counts the pad nibble; the new side left-pads
and counts only valid nibbles::

    >>> old = BcdSide("legacy", pad_side="right", length_counts_pad=True)
    >>> new = BcdSide("partner", pad_side="left", length_counts_pad=False)
    >>> conversion = convert_field("01230", old, new)
    >>> conversion.width
    5
    >>> conversion.source.wire.hex(" ")
    '06 01 23 00'
    >>> conversion.target.wire.hex(" ")
    '05 00 12 30'

The business value survives the change and a single ``"0"`` keeps its width::

    >>> single_zero = convert_field("0", old, new)
    >>> single_zero.source.wire.hex(" ")
    '02 00'
    >>> single_zero.target.wire.hex(" ")
    '01 00'

A legacy packet whose last nibble could be either a business zero or a pad
cannot be decided without extra specification. :func:`decode_side` refuses to
guess by default; once the missing rule is known, pass ``assume_pad``::

    >>> decode_side(bytes.fromhex("012300"), 6, old, assume_pad=True).value
    '01230'
    >>> decode_side(bytes.fromhex("001230"), 5, new).value
    '01230'
    >>> decode_side(bytes.fromhex("00"), 1, new).value
    '0'
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple, Union

__all__ = [
    "BcdSide",
    "BcdFieldEncoding",
    "BcdFieldConversion",
    "BcdMigrationError",
    "BcdAmbiguityError",
    "convert_field",
    "encode_side",
    "decode_side",
]

_HEX_DIGITS: FrozenSet[str] = frozenset("0123456789ABCDEF")
_PAD_SIDES: FrozenSet[str] = frozenset(("left", "right", "none"))

# Specification decisions that are required to tell a pad nibble from a real
# business zero when the legacy packet does not carry enough information.
_MISSING_AMBIGUITY_SPECS: Tuple[str, ...] = (
    "whether the length prefix counts the pad nibble or only valid nibbles",
    "the fixed valid width (max_len) of the business field",
    "a pad digit outside the business domain (for example 'F' instead of '0') "
    "or an explicit marker separating pad nibbles from data nibbles",
)


class BcdMigrationError(ValueError):
    r"""Subclass of ValueError for an invalid BCD migration request or packet.

    Attributes
    ----------
    msg : str
        The unformatted error message.
    """

    def __init__(self, msg: str):
        ValueError.__init__(self, msg)
        self.msg = msg


class BcdAmbiguityError(BcdMigrationError):
    r"""A pad nibble cannot be distinguished from a business zero.

    Raised when a legacy, pad-counting packet ends (or starts) with the pad
    digit. The same bytes are valid under two readings, so extra specification
    is required before a decision is possible.

    Attributes
    ----------
    msg : str
        The unformatted error message.
    side : BcdSide
        The dialect under which the packet is ambiguous.
    hex_data : str
        The raw field data as upper-case hex characters, without length prefix.
    reported_length : int
        The length read from the field length prefix, in nibbles.
    missing_specs : tuple of str
        The specification items that must be supplied to resolve the packet.
    """

    def __init__(
        self,
        side: BcdSide,
        hex_data: str,
        reported_length: int,
        missing_specs: Tuple[str, ...],
    ):
        self.side = side
        self.hex_data = hex_data
        self.reported_length = reported_length
        self.missing_specs = tuple(missing_specs)

        msg = (
            f"Field {hex_data!r} on side {side.name!r} is ambiguous: the "
            f"{side.pad_side} nibble {side.pad!r} can be either a pad or a "
            f"business digit. Decide it by providing one of: "
            + "; ".join(missing_specs)
        )
        BcdMigrationError.__init__(self, msg)


@dataclass(frozen=True)
class BcdSide:
    r"""Describe one BCD dialect using two independent rules.

    Attributes
    ----------
    name : str
        Human readable dialect name, used in error messages.
    pad_side : str
        Where the nibble that rounds odd-length data up to a whole byte is
        placed. One of ``"left"``, ``"right"`` or ``"none"``. ``"none"`` means
        the field is always even and rejects odd-length data.
    length_counts_pad : bool
        How the field length prefix is interpreted. ``True`` means the length
        counts every nibble on the wire, including the pad nibble (legacy).
        ``False`` means the length counts only valid business nibbles (new).
    pad : str, optional
        Single upper-case hex digit used as the pad nibble. Defaults to ``"0"``.
    """

    name: str
    pad_side: str
    length_counts_pad: bool
    pad: str = "0"

    def __post_init__(self) -> None:
        if self.pad_side not in _PAD_SIDES:
            raise BcdMigrationError(
                f"Invalid pad_side {self.pad_side!r}, expected one of "
                f"{sorted(_PAD_SIDES)}"
            )
        if len(self.pad) != 1 or self.pad not in _HEX_DIGITS:
            raise BcdMigrationError(
                f"Invalid pad {self.pad!r}, expected a single upper-case hex digit"
            )


@dataclass(frozen=True)
class BcdFieldEncoding:
    r"""The on-wire representation of one value under one :class:`BcdSide`.

    Attributes
    ----------
    side : BcdSide
        The dialect used to produce this encoding.
    value : str
        The business digits. Their width is the canonical width of the number.
    hex_data : str
        Upper-case hex characters carried in the field, including any pad.
    valid_nibbles : int
        Number of business nibbles in ``value``.
    padded_nibbles : int
        Number of nibbles on the wire, always even because of whole-byte data.
    pad_added : bool
        Whether a pad nibble had to be added.
    length : int
        The integer placed in the field length prefix, per the side rule.
    length_bytes : bytes
        The BCD encoded ``length`` prefix.
    wire : bytes
        The complete field: ``length_bytes`` followed by the BCD data bytes.
    """

    side: BcdSide
    value: str
    hex_data: str
    valid_nibbles: int
    padded_nibbles: int
    pad_added: bool
    length: int
    length_bytes: bytes
    wire: bytes

    @property
    def data_bytes(self) -> bytes:
        """The BCD field data without the length prefix."""
        return bytes.fromhex(self.hex_data)


@dataclass(frozen=True)
class BcdFieldConversion:
    r"""Result of migrating one business value between two :class:`BcdSide` dialects.

    The same ``value`` is encoded on both sides, so the business width is
    preserved while the padding and length rules differ independently.

    Attributes
    ----------
    value : str
        The business digits shared by both encodings.
    width : int
        Number of business digits (nibbles) in ``value``.
    source : BcdFieldEncoding
        Encoding of ``value`` under the source (legacy) dialect.
    target : BcdFieldEncoding
        Encoding of ``value`` under the target (new) dialect.
    """

    value: str
    width: int
    source: BcdFieldEncoding
    target: BcdFieldEncoding

    @property
    def source_wire(self) -> bytes:
        """Complete source field, length prefix plus BCD data."""
        return self.source.wire

    @property
    def target_wire(self) -> bytes:
        """Complete target field, length prefix plus BCD data."""
        return self.target.wire


def convert_field(value: str, source: BcdSide, target: BcdSide) -> BcdFieldConversion:
    r"""Migrate one BCD business value from a source dialect to a target one.

    The value is treated as the canonical business number and is never padded
    or trimmed. Both dialects encode the same digits independently, which keeps
    the business width identical on the way out and on the way back.

    Parameters
    ----------
    value : str
        Business digits as a hex character string. Must be non-empty and may
        contain leading zeros; their width is preserved.
    source : BcdSide
        The dialect the historical samples were produced with.
    target : BcdSide
        The dialect the partner now expects.

    Returns
    -------
    BcdFieldConversion
        The shared value with a separate :class:`BcdFieldEncoding` for each side.

    Raises
    ------
    BcdMigrationError
        ``value`` is empty or contains non-hex data, or a side has no padding
        rule and ``value`` has an odd number of nibbles.
    """
    value = _validate_value(value)
    return BcdFieldConversion(
        value=value,
        width=len(value),
        source=encode_side(value, source),
        target=encode_side(value, target),
    )


def encode_side(value: str, side: BcdSide) -> BcdFieldEncoding:
    r"""Encode a business value under a single :class:`BcdSide`.

    Parameters
    ----------
    value : str
        Business digits as a hex character string.
    side : BcdSide
        The dialect describing the padding and length rules to apply.

    Returns
    -------
    BcdFieldEncoding
        The padded hex data, the length prefix and the complete field bytes.

    Raises
    ------
    BcdMigrationError
        ``value`` is empty, contains non-hex data, or is odd while ``side``
        does not define a pad.
    """
    value = _validate_value(value)
    valid_nibbles = len(value)
    hex_data, pad_added = _apply_pad(value, side)
    padded_nibbles = len(hex_data)

    if side.length_counts_pad:
        length = padded_nibbles
    else:
        length = valid_nibbles

    length_bytes = _bcd_length(length)
    wire = length_bytes + bytes.fromhex(hex_data)

    return BcdFieldEncoding(
        side=side,
        value=value,
        hex_data=hex_data,
        valid_nibbles=valid_nibbles,
        padded_nibbles=padded_nibbles,
        pad_added=pad_added,
        length=length,
        length_bytes=length_bytes,
        wire=wire,
    )


def decode_side(
    data: Union[bytes, bytearray],
    reported_length: int,
    side: BcdSide,
    assume_pad: Optional[bool] = None,
) -> BcdFieldEncoding:
    r"""Interpret a raw BCD field under a single :class:`BcdSide`.

    Use this when migrating from an actual historical packet rather than an
    already decoded value.

    Parameters
    ----------
    data : bytes or bytearray
        The BCD field data without its length prefix.
    reported_length : int
        The length read from the field length prefix, in nibbles.
    side : BcdSide
        The dialect describing the padding and length rules.
    assume_pad : bool, optional
        Only used for a legacy, pad-counting packet whose pad-position nibble
        equals the pad digit. ``True`` treats that nibble as a pad, ``False``
        treats it as a business digit. When left as ``None`` the packet is
        refused with :class:`BcdAmbiguityError` instead of guessing.

    Returns
    -------
    BcdFieldEncoding
        A canonical encoding whose ``value`` is the recovered business number.

    Raises
    ------
    BcdMigrationError
        The packet is malformed for ``side``, for example a length mismatch.
    BcdAmbiguityError
        A pad nibble cannot be told apart from a business zero and
        ``assume_pad`` was not supplied. The exception lists the missing
        specification items in ``missing_specs``.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise BcdMigrationError(
            f"BCD field data must be bytes or bytearray, not {data.__class__.__name__}"
        )
    if reported_length < 0:
        raise BcdMigrationError("Field length must not be negative")

    hex_data = bytes(data).hex().upper()
    wire_nibbles = len(hex_data)

    if wire_nibbles == 0:
        raise BcdMigrationError("BCD field data must not be empty")

    if side.length_counts_pad:
        valid_nibbles = _valid_nibbles_counting_pad(
            hex_data, reported_length, side, assume_pad
        )
    else:
        valid_nibbles = reported_length
        expected_wire_nibbles = _round_up_to_byte(valid_nibbles)
        if expected_wire_nibbles != wire_nibbles:
            raise BcdMigrationError(
                f"Field data is {wire_nibbles} nibbles, expecting "
                f"{expected_wire_nibbles} for {valid_nibbles} valid nibbles"
            )

    value = _strip_pad(hex_data, valid_nibbles, side)

    # Re-encode the recovered value so the result is canonical and any
    # inconsistency in the supplied packet is surfaced as an error.
    encoding = encode_side(value, side)
    if encoding.length != reported_length or encoding.hex_data != hex_data:
        raise BcdMigrationError(
            f"Field data {hex_data!r} with length {reported_length} is not "
            f"consistent with side {side.name!r}"
        )
    return encoding


#
# Private interface
#


def _validate_value(value: str) -> str:
    if not isinstance(value, str):
        raise BcdMigrationError(
            f"BCD value must be str, not {value.__class__.__name__}"
        )
    value = value.upper()
    if len(value) == 0:
        raise BcdMigrationError("BCD value must not be empty")
    if any(digit not in _HEX_DIGITS for digit in value):
        raise BcdMigrationError(f"BCD value {value!r} contains non-hex data")
    return value


def _apply_pad(value: str, side: BcdSide) -> Tuple[str, bool]:
    if len(value) % 2 == 0:
        return value, False

    if side.pad_side == "left":
        return side.pad + value, True
    if side.pad_side == "right":
        return value + side.pad, True

    raise BcdMigrationError(
        f"Odd-length BCD value {value!r} cannot be encoded because side "
        f"{side.name!r} defines no pad"
    )


def _bcd_length(length: int) -> bytes:
    text = str(length)
    if len(text) % 2:
        text = "0" + text
    return bytes.fromhex(text)


def _round_up_to_byte(nibbles: int) -> int:
    return ((nibbles + 1) // 2) * 2


def _valid_nibbles_counting_pad(
    hex_data: str,
    reported_length: int,
    side: BcdSide,
    assume_pad: Optional[bool],
) -> int:
    wire_nibbles = len(hex_data)

    # A length that counts the pad always equals the even on-wire nibble count.
    if reported_length != wire_nibbles:
        raise BcdMigrationError(
            f"Field data is {wire_nibbles} nibbles, but length prefix reports "
            f"{reported_length} on a pad-counting side"
        )

    if side.pad_side == "none":
        return reported_length

    pad_nibble = hex_data[0] if side.pad_side == "left" else hex_data[-1]
    if pad_nibble != side.pad:
        # The nibble where a pad would sit is not the pad digit, so no pad is
        # present and every on-wire nibble is business data.
        return reported_length

    if assume_pad is None:
        raise BcdAmbiguityError(
            side, hex_data, reported_length, _MISSING_AMBIGUITY_SPECS
        )
    if assume_pad:
        return reported_length - 1
    return reported_length


def _strip_pad(hex_data: str, valid_nibbles: int, side: BcdSide) -> str:
    wire_nibbles = len(hex_data)

    if valid_nibbles == wire_nibbles:
        return hex_data
    if valid_nibbles != wire_nibbles - 1:
        raise BcdMigrationError(
            f"Field data is {wire_nibbles} nibbles, expecting {valid_nibbles} "
            f"valid nibbles and at most one pad nibble"
        )

    if side.pad_side == "left":
        if hex_data[0] != side.pad:
            raise BcdMigrationError(
                f"Expected left pad {side.pad!r}, found {hex_data[0]!r}"
            )
        return hex_data[1:]
    if side.pad_side == "right":
        if hex_data[-1] != side.pad:
            raise BcdMigrationError(
                f"Expected right pad {side.pad!r}, found {hex_data[-1]!r}"
            )
        return hex_data[:-1]

    raise BcdMigrationError(
        f"Side {side.name!r} defines no pad but the field carries a pad nibble"
    )
