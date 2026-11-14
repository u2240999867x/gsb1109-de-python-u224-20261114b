r"""Executable cross-specification check samples for legacy fixed-length
amount fields carrying the sign in the last nibble.

The same numeric value can appear in fields of different business widths.
These samples decode the same kind of signed amount through two different
field specifications and assert that:

- negative zero, normal zero, an empty field and an invalid sign code stay
  distinct and are never collapsed into a plain number;
- the numeric view is available for analysis but is never used to rebuild
  the field;
- the original nibble string and bytes survive a full
  :func:`iso8583.decode` / :func:`iso8583.encode` round trip, including the
  two samples that both represent the value 12 but use different widths.

Run the module to print the reading results::

    python examples/signed_amount_samples.py

The samples are also consumed by ``tests/test_signed_amount.py``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import iso8583
from iso8583.specs import default
from iso8583.signed_amount import (
    AmountStatus,
    parse_signed_amount,
    parse_signed_amount_nibbles,
    signed_amount_field_spec,
)


@dataclass(frozen=True)
class Sample:
    """One cross-specification signed amount sample.

    Attributes
    ----------
    sample_id : str
        Unique sample identifier.
    description : str
        Business meaning of the raw declaration.
    field : str
        ISO 8583 field number carrying the amount.
    width : int
        Fixed field width in nibbles, including the sign nibble.
    raw : bytes or None
        Raw field bytes. ``None`` means the field is absent from the
        message, i.e. the empty domain.
    nibbles : str
        Expected upper-case nibble string produced by the decoder.
    status : AmountStatus
        Expected parser classification.
    value : int or None
        Expected signed numeric view.
    is_negative : bool or None
        Expected sign flag.
    decimal : Decimal or None
        Expected fixed-point view with two implied decimal places.
    """

    sample_id: str
    description: str
    field: str
    width: int
    raw: Optional[bytes]
    nibbles: str
    status: AmountStatus
    value: Optional[int]
    is_negative: Optional[bool]
    decimal: Optional[Decimal]


# Field 4 is an 8-nibble (4 byte) signed amount, field 5 is a narrower
# 6-nibble (3 byte) signed amount. Both follow the same sign convention.
WIDE_FIELD = "4"
WIDE_WIDTH = 8
NARROW_FIELD = "5"
NARROW_WIDTH = 6

# fmt: off
SAMPLES: List[Sample] = [
    Sample(
        sample_id="positive_12_wide",
        description="positive 12 in the 8-nibble transaction amount field",
        field=WIDE_FIELD,
        width=WIDE_WIDTH,
        raw=b"\x00\x00\x01\x2c",
        nibbles="0000012C",
        status=AmountStatus.POSITIVE,
        value=12,
        is_negative=False,
        decimal=Decimal("0.12"),
    ),
    Sample(
        sample_id="positive_12_narrow",
        description="positive 12 in the 6-nibble settlement amount field",
        field=NARROW_FIELD,
        width=NARROW_WIDTH,
        raw=b"\x00\x01\x2c",
        nibbles="00012C",
        status=AmountStatus.POSITIVE,
        value=12,
        is_negative=False,
        decimal=Decimal("0.12"),
    ),
    Sample(
        sample_id="negative_12_wide",
        description="negative 12, sign nibble D",
        field=WIDE_FIELD,
        width=WIDE_WIDTH,
        raw=b"\x00\x00\x01\x2d",
        nibbles="0000012D",
        status=AmountStatus.NEGATIVE,
        value=-12,
        is_negative=True,
        decimal=Decimal("-0.12"),
    ),
    Sample(
        sample_id="negative_zero",
        description="declared negative zero, distinct from normal zero",
        field=WIDE_FIELD,
        width=WIDE_WIDTH,
        raw=b"\x00\x00\x00\x0d",
        nibbles="0000000D",
        status=AmountStatus.NEGATIVE_ZERO,
        value=0,
        is_negative=True,
        decimal=Decimal("0.00"),
    ),
    Sample(
        sample_id="normal_zero",
        description="normal zero with a positive sign nibble C",
        field=WIDE_FIELD,
        width=WIDE_WIDTH,
        raw=b"\x00\x00\x00\x0c",
        nibbles="0000000C",
        status=AmountStatus.ZERO,
        value=0,
        is_negative=False,
        decimal=Decimal("0.00"),
    ),
    Sample(
        sample_id="unsigned_zero_narrow",
        description="zero carrying the unsigned F sign nibble",
        field=NARROW_FIELD,
        width=NARROW_WIDTH,
        raw=b"\x00\x00\x0f",
        nibbles="00000F",
        status=AmountStatus.ZERO,
        value=0,
        is_negative=False,
        decimal=Decimal("0.00"),
    ),
    Sample(
        sample_id="invalid_sign_a",
        description="illegal sign nibble A, no numeric view is produced",
        field=WIDE_FIELD,
        width=WIDE_WIDTH,
        raw=b"\x00\x00\x01\x2a",
        nibbles="0000012A",
        status=AmountStatus.INVALID_SIGN,
        value=None,
        is_negative=None,
        decimal=None,
    ),
    Sample(
        sample_id="invalid_sign_digit_seven",
        description="digit nibble 7 used as a sign code is illegal",
        field=WIDE_FIELD,
        width=WIDE_WIDTH,
        raw=b"\x00\x00\x01\x27",
        nibbles="00000127",
        status=AmountStatus.INVALID_SIGN,
        value=None,
        is_negative=None,
        decimal=None,
    ),
    Sample(
        sample_id="empty_field",
        description="field absent from the message, the empty domain",
        field=WIDE_FIELD,
        width=WIDE_WIDTH,
        raw=None,
        nibbles="",
        status=AmountStatus.EMPTY,
        value=None,
        is_negative=None,
        decimal=None,
    ),
]
# fmt: on

# A non-decimal nibble in a digit position cannot be classified at all;
# parsing it must raise rather than silently produce a number.
BAD_DIGIT_RAW = b"\x00\x00\x1a\x2c"
BAD_DIGIT_NIBBLES = "00001A2C"


def build_spec(field: str, width: int, desc: str) -> Dict[str, Any]:
    """Build an ASCII message spec with one signed BCD amount field."""
    spec = copy.deepcopy(default)
    spec["t"]["data_enc"] = "ascii"
    spec["p"]["data_enc"] = "ascii"
    spec[field] = signed_amount_field_spec(width, desc)
    return spec


def check_sample(spec: Dict[str, Any], sample: Sample) -> None:
    """Decode one sample through ``spec`` and assert every view.

    The encoded message is rebuilt from the decoded nibble string and must
    come back byte identical, proving the original bit string survives
    without being reconstructed from a formatted amount.
    """
    if sample.raw is None:
        message, _ = iso8583.encode({"t": "0200"}, spec)
        decoded, encoded = iso8583.decode(bytes(message), spec)
        assert sample.field not in decoded
        assert sample.field not in encoded

        amount = parse_signed_amount(b"")
        assert amount.status == AmountStatus.EMPTY
        assert amount.value is None
        assert amount.is_negative is None
        assert amount.to_decimal() is None
        assert amount.nibbles == ""
        assert amount.raw == b""
        return

    message, _ = iso8583.encode(
        {"t": "0200", sample.field: sample.nibbles}, spec
    )
    decoded, encoded = iso8583.decode(bytes(message), spec)

    # The decoder keeps both the nibble string and the original bytes.
    assert decoded[sample.field] == sample.nibbles
    assert encoded[sample.field]["data"] == sample.raw
    assert encoded[sample.field]["len"] == b""

    # Both parsing entry points must agree, and the width must be enforced.
    amount = parse_signed_amount(sample.raw, sample.width)
    amount_from_nibbles = parse_signed_amount_nibbles(
        decoded[sample.field], sample.width
    )
    assert amount == amount_from_nibbles

    assert amount.nibbles == sample.nibbles
    assert amount.width == sample.width
    assert amount.sign_nibble == sample.nibbles[-1]
    assert amount.status == sample.status
    assert amount.value == sample.value
    assert amount.is_negative == sample.is_negative
    assert amount.to_decimal() == sample.decimal

    # The verbatim views, not the numeric value, drive re-encoding.
    assert amount.to_bytes() == sample.raw
    assert amount.to_nibbles() == sample.nibbles

    rebuilt_message, _ = iso8583.encode(
        {"t": "0200", sample.field: amount.to_nibbles()}, spec
    )
    assert bytes(rebuilt_message) == bytes(message)


def check_value_12_widths() -> None:
    """The two value-12 samples share a number, not a bit string."""
    wide = parse_signed_amount(b"\x00\x00\x01\x2c", WIDE_WIDTH)
    narrow = parse_signed_amount(b"\x00\x01\x2c", NARROW_WIDTH)

    assert wide.value == narrow.value == 12
    assert wide.to_decimal() == narrow.to_decimal() == Decimal("0.12")
    assert wide.width != narrow.width
    assert wide.nibbles != narrow.nibbles
    assert wide.raw != narrow.raw

    # A formatted amount alone cannot recover either original bit string:
    # it carries neither the business width nor the sign declaration.
    formatted = f"{wide.value:0{WIDE_WIDTH - 1}d}"
    assert formatted == "0000012"
    assert formatted not in (wide.nibbles, narrow.nibbles)


def check_bad_digit_rejected() -> None:
    """A non-decimal digit nibble is rejected instead of guessed."""
    try:
        parse_signed_amount(BAD_DIGIT_RAW, WIDE_WIDTH)
    except ValueError:
        pass
    else:  # pragma: no cover - defensive guard
        raise AssertionError("non-decimal digit nibble must raise ValueError")

    try:
        parse_signed_amount_nibbles(BAD_DIGIT_NIBBLES, WIDE_WIDTH)
    except ValueError:
        pass
    else:  # pragma: no cover - defensive guard
        raise AssertionError("non-decimal digit nibble must raise ValueError")


def run_all() -> List[SignedAmountReading]:
    """Run every cross-specification check and return reading results."""
    specs = {
        WIDE_FIELD: build_spec(
            WIDE_FIELD, WIDE_WIDTH, "Amount, Transaction (signed BCD)"
        ),
        NARROW_FIELD: build_spec(
            NARROW_FIELD, NARROW_WIDTH, "Amount, Settlement (signed BCD)"
        ),
    }

    readings: List[SignedAmountReading] = []
    for sample in SAMPLES:
        check_sample(specs[sample.field], sample)
        readings.append(SignedAmountReading.from_sample(sample))

    check_value_12_widths()
    check_bad_digit_rejected()
    return readings


@dataclass(frozen=True)
class SignedAmountReading:
    """One printed reading result row.

    Attributes
    ----------
    sample_id : str
        Sample identifier.
    width : int
        Field width in nibbles.
    raw_hex : str
        Original field bytes rendered as hex, or ``"-"`` when absent.
    nibbles : str
        Verbatim nibble string, or ``"-"`` when absent.
    sign_nibble : str
        Trailing sign nibble, or ``"-"`` when absent.
    status : str
        Parser classification.
    value : str
        Numeric view, or ``"-"`` when unavailable.
    decimal : str
        Fixed-point view, or ``"-"`` when unavailable.
    """

    sample_id: str
    width: int
    raw_hex: str
    nibbles: str
    sign_nibble: str
    status: str
    value: str
    decimal: str

    @classmethod
    def from_sample(cls, sample: Sample) -> "SignedAmountReading":
        if sample.raw is None:
            amount = parse_signed_amount(b"")
        else:
            amount = parse_signed_amount(sample.raw, sample.width)

        return cls(
            sample_id=sample.sample_id,
            width=sample.width,
            raw_hex="-" if sample.raw is None else sample.raw.hex(),
            nibbles="-" if sample.nibbles == "" else sample.nibbles,
            sign_nibble="-" if amount.sign_nibble == "" else amount.sign_nibble,
            status=amount.status.value,
            value="-" if amount.value is None else str(amount.value),
            decimal="-" if amount.to_decimal() is None else str(amount.to_decimal()),
        )


def _print_readings(readings: List[SignedAmountReading]) -> None:
    headers = (
        "sample_id",
        "width",
        "raw_hex",
        "nibbles",
        "sign",
        "status",
        "value",
        "decimal",
    )
    rows = [
        (
            reading.sample_id,
            str(reading.width),
            reading.raw_hex,
            reading.nibbles,
            reading.sign_nibble,
            reading.status,
            reading.value,
            reading.decimal,
        )
        for reading in readings
    ]
    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]

    def format_row(row: Tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    print(format_row(headers))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(format_row(row))

    print()
    print(
        f"rejected non-decimal digit sample: {BAD_DIGIT_RAW.hex()} "
        f"({BAD_DIGIT_NIBBLES})"
    )


if __name__ == "__main__":
    _print_readings(run_all())
