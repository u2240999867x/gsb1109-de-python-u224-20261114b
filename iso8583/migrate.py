r"""Offline migration rehearsal between two ISO8583 specifications.

This module never builds or emits a target message. It pre-encodes every
field of a decoded source message against a *target* specification and
reports, per field:

- how many bytes the field actually occupies once transcoded with the
  target data encoding (not the character count of the source string),
- whether the target length header is wide enough to express that
  length, given the target ``len_enc``/``len_type``,
- whether decoding the encoded bytes yields the original value again.

Each field is evaluated independently. A field that is missing from the
target specification is reported as :data:`UNDEFINED`; a field that exists
but cannot hold the data is reported separately as :data:`TOO_LONG`,
:data:`LENGTH_OVERFLOW`, :data:`LENGTH_MISMATCH` or
:data:`ROUNDTRIP_MISMATCH`. One failing field never prevents the remaining
fields from being rehearsed.

Examples
--------
>>> from iso8583.specs import default_ascii
>>> target = {key: dict(value) for key, value in default_ascii.items()}
>>> target["48"].update(len_type=2, max_len=99)
>>> rows = rehearse_message({"t": "0200", "48": "A" * 50}, target)
>>> row = next(item for item in rows if item["field"] == "48")
>>> row["status"]
'ok'
>>> row["encoded_bytes"], row["length_capacity"]
(50, 99)
>>> row["length_fits"], row["roundtrip_equal"]
(True, True)
>>> rehearse_field("193", "x", target)["status"]
'undefined'
"""

import binascii
from typing import Any, List, Literal, Mapping, Optional, Sequence, Tuple, TypedDict

__all__ = [
    "rehearse_field",
    "rehearse_message",
    "rehearse_batch",
    "length_header_capacity",
    "UNDEFINED",
    "ENCODE_ERROR",
    "TOO_LONG",
    "LENGTH_OVERFLOW",
    "LENGTH_MISMATCH",
    "ROUNDTRIP_MISMATCH",
    "OK",
]

Status = Literal[
    "undefined",
    "encode_error",
    "too_long",
    "length_overflow",
    "length_mismatch",
    "roundtrip_mismatch",
    "ok",
]

SpecDict = Mapping[str, Mapping[str, Any]]

#: Target specification does not define the field.
UNDEFINED: Status = "undefined"
#: Value cannot be encoded with the target data encoding.
ENCODE_ERROR: Status = "encode_error"
#: Encoded length exceeds target ``max_len``.
TOO_LONG: Status = "too_long"
#: Length fits ``max_len`` but not the target length header range.
LENGTH_OVERFLOW: Status = "length_overflow"
#: Fixed-length target field whose encoded size differs from ``max_len``.
LENGTH_MISMATCH: Status = "length_mismatch"
#: Decoding the encoded bytes does not return the source value.
ROUNDTRIP_MISMATCH: Status = "roundtrip_mismatch"
#: Field can be encoded, headed and read back unchanged.
OK: Status = "ok"


class RehearsalResult(TypedDict, total=False):
    """Per-field migration rehearsal outcome."""

    field: str
    status: Status
    data_enc: Optional[str]
    len_enc: Optional[str]
    len_type: Optional[int]
    length_unit: Optional[str]
    encoded_bytes: Optional[int]
    measured: Optional[int]
    max_len: Optional[int]
    max_data_bytes: Optional[int]
    length_capacity: Optional[int]
    length_fits: Optional[bool]
    roundtrip_equal: Optional[bool]
    error: Optional[str]


def length_header_capacity(len_enc: str, len_type: int) -> Optional[int]:
    r"""Largest length expressible by a target length header.

    Parameters
    ----------
    len_enc : str
        Target length encoding: ``b`` (binary), ``bcd`` or a text codec.
    len_type : int
        Target length header width in bytes. ``0`` denotes a fixed field,
        which has no length header.

    Returns
    -------
    int or None
        Maximum representable field length, or ``None`` for fixed fields.

    Examples
    --------
    >>> length_header_capacity("ascii", 2)
    99
    >>> length_header_capacity("bcd", 1)
    99
    >>> length_header_capacity("b", 1)
    255
    >>> length_header_capacity("ascii", 0) is None
    True
    """
    if len_type <= 0:
        return None

    if len_enc == "b":
        return int(2 ** (8 * len_type) - 1)

    if len_enc == "bcd":
        # Two decimal digits are packed into each BCD byte.
        return int(10 ** (2 * len_type) - 1)

    # Text codecs store one decimal digit per header byte.
    return int(10**len_type - 1)


def rehearse_field(
    field_key: str, value: str, target_spec: SpecDict
) -> RehearsalResult:
    r"""Rehearse one field against the target specification.

    Parameters
    ----------
    field_key : str
        ISO8583 field key, e.g. ``"48"``.
    value : str
        Decoded field value as held in a source ISO8583 dictionary.
    target_spec : dict
        Target ISO8583 specification.

    Returns
    -------
    RehearsalResult
        A dictionary describing the rehearsal outcome. The function never
        raises for ordinary data or specification problems; the verdict is
        carried in the ``status`` key.
    """
    if field_key not in target_spec:
        return RehearsalResult(
            field=field_key,
            status=UNDEFINED,
            data_enc=None,
            len_enc=None,
            len_type=None,
            length_unit=None,
            encoded_bytes=None,
            measured=None,
            max_len=None,
            max_data_bytes=None,
            length_capacity=None,
            length_fits=None,
            roundtrip_equal=None,
            error="Field is not defined in the target specification",
        )

    field_spec = target_spec[field_key]
    data_enc = field_spec["data_enc"]
    len_enc = field_spec["len_enc"]
    len_type = field_spec["len_type"]
    max_len = field_spec["max_len"]
    len_count = field_spec.get("len_count", "bytes")

    result = RehearsalResult(
        field=field_key,
        data_enc=data_enc,
        len_enc=len_enc,
        len_type=len_type,
        length_unit=len_count,
        max_len=max_len,
        max_data_bytes=_max_data_bytes(field_spec),
        length_capacity=length_header_capacity(len_enc, len_type),
        roundtrip_equal=None,
        error=None,
    )

    encoded_data, measured, encode_error = _encode_value(value, field_spec)
    if encode_error is not None:
        result["status"] = ENCODE_ERROR
        result["error"] = encode_error
        return result

    result["encoded_bytes"] = len(encoded_data)
    result["measured"] = measured

    if len_type == 0:
        if measured != max_len:
            result["status"] = LENGTH_MISMATCH
            result["length_fits"] = None
            result["error"] = (
                f"Field data is {measured} {len_count}, expecting {max_len}"
            )
            return result
        result["length_fits"] = None
    else:
        if measured > max_len:
            result["status"] = TOO_LONG
            result["length_fits"] = False
            result["error"] = (
                f"Field data is {measured} {len_count}, "
                f"larger than maximum {max_len}"
            )
            return result

        capacity = result["length_capacity"]
        length_fits = capacity is None or measured <= capacity
        result["length_fits"] = length_fits
        if not length_fits:
            result["status"] = LENGTH_OVERFLOW
            result["error"] = (
                f"Field length {measured} {len_count} does not fit into "
                f"{len_enc} length header of {len_type} byte(s), "
                f"capacity {capacity}"
            )
            return result

    readback, readback_error = _decode_value(encoded_data, field_spec, measured)
    if readback_error is not None:
        result["status"] = ROUNDTRIP_MISMATCH
        result["roundtrip_equal"] = False
        result["error"] = readback_error
        return result

    if readback != value:
        result["status"] = ROUNDTRIP_MISMATCH
        result["roundtrip_equal"] = False
        result["error"] = f"Readback {readback!r} does not equal source {value!r}"
        return result

    result["status"] = OK
    result["roundtrip_equal"] = True
    return result


def rehearse_message(
    doc_dec: Mapping[str, str],
    target_spec: SpecDict,
    fields: Optional[Sequence[str]] = None,
) -> List[RehearsalResult]:
    r"""Rehearse every field of one decoded message independently.

    A field missing from the target specification is reported as
    :data:`UNDEFINED` instead of aborting the run. Any unexpected failure
    while evaluating one field is captured in that field's row only, so a
    single overlong field never blocks the rest of the message.

    Parameters
    ----------
    doc_dec : dict
        Decoded source ISO8583 message.
    target_spec : dict
        Target ISO8583 specification.
    fields : sequence of str, optional
        Restrict the rehearsal to these field keys. By default every key in
        the message is rehearsed.

    Returns
    -------
    list of RehearsalResult
        One row per rehearsed field, ordered numerically for numeric keys
        and alphabetically afterwards.
    """
    if fields is None:
        field_keys = list(doc_dec.keys())
    else:
        field_keys = [field_key for field_key in fields if field_key in doc_dec]

    rows: List[RehearsalResult] = []
    for field_key in sorted(field_keys, key=_field_order):
        try:
            rows.append(rehearse_field(field_key, doc_dec[field_key], target_spec))
        except Exception as exc:
            rows.append(
                RehearsalResult(
                    field=field_key,
                    status=ENCODE_ERROR,
                    data_enc=None,
                    len_enc=None,
                    len_type=None,
                    length_unit=None,
                    encoded_bytes=None,
                    measured=None,
                    max_len=None,
                    max_data_bytes=None,
                    length_capacity=None,
                    length_fits=None,
                    roundtrip_equal=None,
                    error=f"Unexpected rehearsal failure: {exc}",
                )
            )

    return rows


def rehearse_batch(
    messages: Sequence[Mapping[str, str]],
    target_spec: SpecDict,
    fields: Optional[Sequence[str]] = None,
) -> List[List[RehearsalResult]]:
    r"""Rehearse a whole batch of decoded messages offline.

    Parameters
    ----------
    messages : sequence of dict
        Decoded source ISO8583 messages.
    target_spec : dict
        Target ISO8583 specification.
    fields : sequence of str, optional
        Restrict every message to these field keys.

    Returns
    -------
    list of list of RehearsalResult
        One ordered list of per-field rows per input message.
    """
    return [rehearse_message(message, target_spec, fields) for message in messages]


def _field_order(field_key: str) -> Any:
    if field_key.isnumeric():
        return (0, int(field_key))
    return (1, field_key)


def _max_data_bytes(field_spec: Mapping[str, Any]) -> int:
    max_len = int(field_spec["max_len"])
    if field_spec.get("len_count", "bytes") == "nibbles":
        return (max_len + 1) // 2
    return max_len


def _with_nibble_pad(value: str, field_spec: Mapping[str, Any]) -> str:
    pad = str(field_spec.get("left_pad", ""))[:1]
    if pad:
        return pad + value

    pad = str(field_spec.get("right_pad", ""))[:1]
    if pad:
        return value + pad

    return value


def _encode_value(
    value: str, field_spec: Mapping[str, Any]
) -> Tuple[bytes, int, Optional[str]]:
    data_enc = field_spec["data_enc"]
    len_count = field_spec.get("len_count", "bytes")

    if data_enc == "b":
        data_to_encode = value
        if len_count == "nibbles" and len(value) & 1:
            data_to_encode = _with_nibble_pad(value, field_spec)

        try:
            encoded_data = binascii.a2b_hex(data_to_encode)
        except Exception:
            if len(data_to_encode) % 2 == 1:
                if len_count == "nibbles" and not (
                    field_spec.get("left_pad") or field_spec.get("right_pad")
                ):
                    error = (
                        "Failed to encode field, odd-length nibble data, specify pad"
                    )
                else:
                    error = "Failed to encode field, odd-length hex data"
            else:
                error = "Failed to encode field, non-hex data"
            return b"", 0, error

        measured = len(value) if len_count == "nibbles" else len(encoded_data)
        return encoded_data, measured, None

    try:
        encoded_data = value.encode(data_enc)
    except LookupError:
        return b"", 0, "Failed to encode field, unknown encoding specified"
    except Exception:
        return b"", 0, "Failed to encode field, invalid data"

    measured = len(encoded_data) * 2 if len_count == "nibbles" else len(encoded_data)
    return encoded_data, measured, None


def _decode_value(
    encoded_data: bytes,
    field_spec: Mapping[str, Any],
    measured: int,
) -> Tuple[Optional[str], Optional[str]]:
    data_enc = field_spec["data_enc"]
    len_count = field_spec.get("len_count", "bytes")

    if data_enc == "b":
        readback = encoded_data.hex().upper()

        if len_count == "nibbles" and measured & 1:
            pad = field_spec.get("left_pad", "")[:1]
            if pad and readback[:1] == pad:
                readback = readback[1:]
            else:
                pad = field_spec.get("right_pad", "")[:1]
                if pad and readback[-1:] == pad:
                    readback = readback[:-1]
                else:
                    return None, (
                        "Failed to decode field, nibble pad configured in the "
                        "target specification is missing from the encoded data"
                    )

        return readback, None

    try:
        return encoded_data.decode(data_enc), None
    except LookupError:
        return None, "Failed to decode field, unknown encoding specified"
    except Exception:
        return None, "Failed to decode field, invalid data"
