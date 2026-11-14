import copy as _copy
import json as _json
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    NoReturn,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

__all__ = ["decode", "DecodeError", "DecodeDiagnostic", "decode_diagnostic"]

DecodedDict = Dict[str, str]
EncodedDict = Dict[str, Dict[str, bytes]]
SpecDict = Mapping[str, Mapping[str, Any]]

_SECRET_SPEC_KEYS = (
    "sensitive",
    "sensitive_data",
    "secret",
    "secret_data",
    "redact",
    "redacted",
    "mask",
    "masked",
    "private",
    "pci",
    "is_sensitive",
    "is_secret",
)


class DecodeDiagnostic(Dict[str, Any]):
    r"""Serializable, field-aware description of a decode attempt.

    The diagnostic keeps offsets and structural ranges but never contains
    the value of a field declared secret in the specification. It is a
    ``dict`` subclass, so it can be indexed like a mapping and serialized
    with :func:`json.dumps`.

    Keys
    ----
    status : str
        ``"error"`` or ``"ok"``.
    kind : str
        Machine-readable error category, e.g. ``"truncated_field_length"``
        or ``"truncated_field_data"``.
    field : str
        Field that failed parsing.
    part : str
        Failing component: ``"length"``, ``"data"``, ``"bitmap"`` etc.
    pos : int
        Offset passed to :class:`DecodeError` (start of the failing field
        or component, depending on the error).
    completed : dict
        Inclusive-exclusive range ``{"start": ..., "end": ...}`` that was
        decoded and validated before the error.
    error : dict
        Description of the failing component including its own ``start``,
        ``end`` and, where known, ``expected_end``.
    undetermined : dict
        Range beginning at the failing component and ending at the last
        received byte. Its contents cannot be trusted after the error.
    fields : dict
        Per-field trace. Each entry has ``start``, ``end``, ``parts`` and
        ``status``. Non-secret entries may also contain ``value``. Secret
        entries only contain positions and structural information.
    """

    def __init__(self, data: Mapping[str, Any]) -> None:
        super().__init__(data)

    @property
    def status(self) -> str:
        return str(self["status"])

    @property
    def kind(self) -> str:
        return str(self["kind"])

    @property
    def msg(self) -> str:
        return str(self["msg"])

    @property
    def field(self) -> str:
        return str(self["field"])

    @property
    def part(self) -> str:
        return str(self["part"])

    @property
    def pos(self) -> int:
        return int(self["pos"])

    @property
    def completed_range(self) -> Mapping[str, int]:
        return self["completed"]  # type: ignore[no-any-return]

    @property
    def completed(self) -> Mapping[str, int]:
        return self.completed_range

    @property
    def completed_end(self) -> int:
        return int(self["completed"]["end"])

    @property
    def error_position(self) -> int:
        return int(self["error_position"])

    @property
    def error_range(self) -> Mapping[str, Any]:
        return self["error"]  # type: ignore[no-any-return]

    @property
    def error(self) -> Mapping[str, Any]:
        return self.error_range

    @property
    def undetermined_range(self) -> Mapping[str, int]:
        return self["undetermined"]  # type: ignore[no-any-return]

    @property
    def undetermined(self) -> Mapping[str, int]:
        return self.undetermined_range

    @property
    def undetermined_start(self) -> int:
        return int(self["undetermined"]["start"])

    @property
    def fields(self) -> Mapping[str, Mapping[str, Any]]:
        return self["fields"]  # type: ignore[no-any-return]

    @property
    def redacted_fields(self) -> List[str]:
        return self["redacted_fields"]  # type: ignore[no-any-return]

    def to_dict(self) -> Dict[str, Any]:
        r"""Return a JSON-serializable plain ``dict`` copy."""
        return _copy.deepcopy(dict(self))

    def as_dict(self) -> Dict[str, Any]:
        r"""Alias for :meth:`to_dict`."""
        return self.to_dict()

    def to_json(self, **kwargs: Any) -> str:
        r"""Serialize the diagnostic to JSON."""
        return _json.dumps(self.to_dict(), **kwargs)

    def __reduce__(self) -> Tuple[Type["DecodeDiagnostic"], Tuple[Dict[str, Any],]]:
        return self.__class__, (dict(self),)


class _DecodeContext:
    r"""Mutable bookkeeping used to build a decode diagnostic."""

    def __init__(
        self,
        s: Union[bytes, bytearray],
        spec: SpecDict,
        sensitive_fields: Iterable[str],
    ) -> None:
        self.s = s
        self.spec = spec
        self.size = len(s)
        self.secret_fields: Set[str] = _collect_secret_fields(spec, sensitive_fields)
        self.traces: Dict[str, Dict[str, Any]] = {}
        self.current: Optional[Dict[str, Any]] = None
        self.completed_end = 0
        self.pending_fields: Set[str] = set()
        self.fields_known = False

    def register_fields(self, fields: Set[int]) -> None:
        self.pending_fields = {str(f) for f in fields}
        self.fields_known = True

    def resolve_field(self, field_key: str) -> None:
        self.pending_fields.discard(field_key)

    def is_secret(self, field_key: str) -> bool:
        return field_key in self.secret_fields

    def begin(
        self,
        field_key: str,
        kind: str,
        start: int,
        len_type: int = 0,
        len_count: str = "bytes",
        expected_end: Optional[int] = None,
        is_extended: bool = False,
        part_name: str = "data",
    ) -> Dict[str, Any]:
        if is_extended and field_key in self.traces:
            existing = self.traces[field_key]
            existing["_extended"] = True
            existing["parts"]["bitmap_extension"] = {
                "start": start,
                "end": min(
                    self.size, expected_end if expected_end is not None else start
                ),
                "expected_end": expected_end,
                "status": "pending",
            }
            self.current = existing
            return existing

        frame: Dict[str, Any] = {
            "field": field_key,
            "kind": kind,
            "start": start,
            "end": start,
            "status": "pending",
            "parts": {},
            "_len_type": len_type,
            "_len_count": len_count,
            "_length_start": start,
            "_length_end": start + len_type,
            "_data_start": start + len_type,
            "_expected_data_end": expected_end,
            "_declared_length": None,
            "_byte_length": None,
            "_error": None,
        }
        if kind == "field" and len_type == 0:
            frame["parts"]["length"] = {
                "start": start,
                "end": start,
                "expected_end": start,
                "status": "not_present",
            }
        elif len_type > 0:
            frame["parts"]["length"] = {
                "start": start,
                "end": min(self.size, start + len_type),
                "expected_end": start + len_type,
                "status": "pending",
            }
        if kind != "field" or len_type == 0:
            frame["parts"][part_name] = {
                "start": start + len_type,
                "end": min(
                    self.size, expected_end if expected_end is not None else start
                ),
                "expected_end": expected_end,
                "status": "pending",
            }
        self.traces[field_key] = frame
        self.current = frame
        return frame

    def mark_length_ok(self, frame: Dict[str, Any], end: int) -> None:
        length = frame["parts"].get("length")
        if length is not None:
            length["status"] = "ok"
            length["end"] = min(end, self.size)
        self.completed_end = max(self.completed_end, min(end, self.size))

    def mark_data_position(
        self,
        frame: Dict[str, Any],
        start: int,
        expected_end: Optional[int],
        declared_length: Optional[int] = None,
        byte_length: Optional[int] = None,
    ) -> None:
        frame["_data_start"] = start
        frame["_expected_data_end"] = expected_end
        frame["_declared_length"] = declared_length
        frame["_byte_length"] = byte_length
        frame["parts"]["data"] = {
            "start": start,
            "end": min(self.size, expected_end if expected_end is not None else start),
            "expected_end": expected_end,
            "status": "pending",
        }
        self.completed_end = max(self.completed_end, min(start, self.size))

    def mark_error(
        self,
        frame: Dict[str, Any],
        part: str,
        kind: str,
        start: int,
        end: int,
        expected_end: Optional[int] = None,
    ) -> None:
        end = min(end, self.size)
        frame["status"] = "error"
        frame["end"] = end
        frame["_error"] = {
            "part": part,
            "kind": kind,
            "start": start,
            "end": end,
            "expected_end": expected_end,
        }
        target = frame["parts"].get(part)
        if target is None:
            target = {
                "start": start,
                "end": end,
                "expected_end": expected_end,
                "status": "error",
            }
            frame["parts"][part] = target
        else:
            target["status"] = "error"
            target["end"] = end
            target["expected_end"] = expected_end
        if part == "length" and "data" not in frame["parts"]:
            data_start = min(int(frame["_length_end"]), self.size)
            frame["parts"]["data"] = {
                "start": data_start,
                "end": data_start,
                "expected_end": None,
                "status": "not_reached",
            }
        self.current = frame

    def finish(self, frame: Dict[str, Any], end: int) -> None:
        frame["end"] = end
        frame["status"] = "ok"
        parts = frame["parts"]
        length = parts.get("length")
        if length is not None and length.get("status") == "pending":
            length["status"] = "ok"
            length["end"] = min(frame["_length_end"], self.size)
        if frame["kind"] == "field" and "data" not in parts:
            parts["data"] = {
                "start": min(end, self.size),
                "end": min(end, self.size),
                "expected_end": min(end, self.size),
                "status": "ok",
            }
        data = parts.get("data")
        if data is not None and data.get("status") == "pending":
            data["status"] = "ok"
            data["end"] = end
        for name, part in parts.items():
            if name in ("length", "data") or part.get("status") != "pending":
                continue
            part["status"] = "ok"
            part["end"] = min(end, self.size)
        self.completed_end = max(self.completed_end, min(end, self.size))
        self.current = None


class DecodeError(ValueError):
    r"""Subclass of ValueError that describes ISO8583 decoding error.

    Attributes
    ----------
    msg : str
        The unformatted error message
    s : bytes or bytearray
        The ISO8583 bytes instance being parsed. Content bytes of secret
        fields are replaced with ``*`` before the exception is exposed.
    doc_dec : dict
        Dict containing partially decoded ISO8583 data. Secret field values
        are removed.
    doc_enc : dict
        Dict containing partially encoded ISO8583 data. Secret field
        length/data bytes are removed.
    pos : int
        The start index where ISO8583 bytes data failed parsing
    field : str
        The ISO8583 field where parsing failed
    diagnostic : DecodeDiagnostic
        Structured, serializable decode diagnostic. Secret fields are
        represented by positions only.
    """

    def __init__(
        self,
        msg: str,
        s: Union[bytes, bytearray],
        doc_dec: DecodedDict,
        doc_enc: EncodedDict,
        pos: int,
        field: str,
        diagnostic: Optional[Mapping[str, Any]] = None,
    ):
        errmsg = f"{msg}: field {field} pos {pos}"
        ValueError.__init__(self, errmsg)
        self.msg = msg
        self.s = s
        self.doc_dec = doc_dec
        self.doc_enc = doc_enc
        self.field = field
        self.pos = pos
        self.diagnostic = DecodeDiagnostic(
            diagnostic
            if diagnostic is not None
            else _minimal_diagnostic(msg, pos, field, doc_dec, doc_enc, len(s))
        )
        self.redactions: List[Dict[str, Any]] = [
            {
                "field": key,
                "start": int(trace["start"]),
                "end": int(trace["end"]),
            }
            for key, trace in self.diagnostic.fields.items()
            if trace.get("redacted")
        ]

    @property
    def details(self) -> DecodeDiagnostic:
        r"""Alias for :attr:`diagnostic`."""
        return self.diagnostic

    @property
    def context(self) -> DecodeDiagnostic:
        r"""Alias for :attr:`diagnostic`."""
        return self.diagnostic

    @property
    def kind(self) -> str:
        return self.diagnostic.kind

    @property
    def part(self) -> str:
        return self.diagnostic.part

    @property
    def completed_range(self) -> Mapping[str, int]:
        return self.diagnostic.completed_range

    @property
    def undetermined_range(self) -> Mapping[str, int]:
        return self.diagnostic.undetermined_range

    def to_dict(self) -> Dict[str, Any]:
        r"""Return a JSON-serializable dict with the full diagnostic."""
        return {
            "msg": self.msg,
            "field": self.field,
            "pos": self.pos,
            "diagnostic": self.diagnostic.to_dict(),
        }

    def as_dict(self) -> Dict[str, Any]:
        r"""Alias for :meth:`to_dict`."""
        return self.to_dict()

    def __reduce__(
        self,
    ) -> Tuple[
        Type["DecodeError"],
        Tuple[
            str,
            Union[bytes, bytearray],
            DecodedDict,
            EncodedDict,
            int,
            str,
            DecodeDiagnostic,
        ],
    ]:
        return (
            self.__class__,
            (
                self.msg,
                self.s,
                self.doc_dec,
                self.doc_enc,
                self.pos,
                self.field,
                self.diagnostic,
            ),
        )


def decode(
    s: Union[bytes, bytearray],
    spec: SpecDict,
    sensitive_fields: Optional[Iterable[str]] = None,
    *,
    secret_fields: Optional[Iterable[str]] = None,
    redact_fields: Optional[Iterable[str]] = None,
) -> Tuple[DecodedDict, EncodedDict]:
    r"""Deserialize a bytes or bytearray instance containing
    ISO8583 data to a Python dict.

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data
    spec : dict
        A Python dict defining ISO8583 specification.
        See :mod:`iso8583.specs` module for examples.
    sensitive_fields : iterable of str, optional
        Field IDs whose values must not be exposed in returned error
        objects or diagnostics. Fields can also be marked secret in the
        specification with a truthy ``sensitive`` (or ``secret``,
        ``redact``, ``masked``) property.
    secret_fields : iterable of str, optional
        Alias for ``sensitive_fields``.
    redact_fields : iterable of str, optional
        Alias for ``sensitive_fields``.

    Returns
    -------
    doc_dec : dict
        Dict containing decoded ISO8583 data
    doc_enc : dict
        Dict containing encoded ISO8583 data

    Raises
    ------
    DecodeError
        An error decoding ISO8583 bytearray. The exception carries a
        structured :attr:`DecodeError.diagnostic` that separates field
        length headers from field data and reports completed, error and
        undetermined byte ranges. Secret field values are not included.
    TypeError
        `s` must be a bytes or bytearray instance

    Examples
    --------
    >>> import pprint
    >>> import iso8583
    >>> from iso8583.specs import default_ascii as spec
    >>> s = b"02004010100000000000161234567890123456123456111"
    >>> doc_dec, doc_enc = iso8583.decode(s, spec)
    >>> pprint.pprint(doc_dec)
    {'12': '123456',
     '2': '1234567890123456',
     '20': '111',
     'p': '4010100000000000',
     't': '0200'}
    """

    if not isinstance(s, (bytes, bytearray)):
        raise TypeError(
            f"Encoded ISO8583 data must be bytes or bytearray, not {s.__class__.__name__}"
        )

    doc_dec, doc_enc, _ = _decode(
        s,
        spec,
        _merge_sensitive_fields(sensitive_fields, secret_fields, redact_fields),
    )
    return doc_dec, doc_enc


def decode_diagnostic(
    s: Union[bytes, bytearray],
    spec: SpecDict,
    sensitive_fields: Optional[Iterable[str]] = None,
    *,
    secret_fields: Optional[Iterable[str]] = None,
    redact_fields: Optional[Iterable[str]] = None,
) -> DecodeDiagnostic:
    r"""Decode ``s`` and return a serializable field-aware diagnostic.

    Unlike :func:`decode` this function never raises :class:`DecodeError`
    for malformed input. It returns a :class:`DecodeDiagnostic` describing
    successful fields, the error point, the completed byte range and the
    subsequent undetermined region. Secret fields are represented by
    positions only.

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data
    spec : dict
        A Python dict defining ISO8583 specification
    sensitive_fields : iterable of str, optional
        Field IDs that must not expose values

    Returns
    -------
    DecodeDiagnostic
        Mapping-compatible and JSON-serializable diagnostic
    """

    if not isinstance(s, (bytes, bytearray)):
        raise TypeError(
            f"Encoded ISO8583 data must be bytes or bytearray, not {s.__class__.__name__}"
        )

    sensitive = _merge_sensitive_fields(sensitive_fields, secret_fields, redact_fields)
    try:
        doc_dec, _, ctx = _decode(s, spec, sensitive)
    except DecodeError as e:
        return e.diagnostic
    return DecodeDiagnostic(_success_diagnostic(ctx, doc_dec))


def _decode(
    s: Union[bytes, bytearray],
    spec: SpecDict,
    sensitive_fields: Iterable[str],
) -> Tuple[DecodedDict, EncodedDict, "_DecodeContext"]:
    doc_dec: DecodedDict = {}
    doc_enc: EncodedDict = {}
    fields: Set[int] = set()
    idx = 0

    ctx = _DecodeContext(s, spec, sensitive_fields)

    idx = _decode_header(s, doc_dec, doc_enc, idx, spec, ctx)
    idx = _decode_type(s, doc_dec, doc_enc, idx, spec, ctx)
    field = "p"
    idx = _decode_bitmap(
        s,
        doc_dec,
        doc_enc,
        idx,
        field,
        spec[field],
        0,
        False,
        fields,
        ctx,
    )

    if 1 in fields:
        field = "1"
        idx = _decode_bitmap(
            s,
            doc_dec,
            doc_enc,
            idx,
            field,
            spec[field],
            64,
            False,
            fields,
            ctx,
        )
        fields.remove(1)

    if 65 in fields:
        # Secondary bitmap is extended to contain tertiary fields
        idx = _decode_bitmap(
            s,
            doc_dec,
            doc_enc,
            idx,
            field,
            spec[field],
            128,
            True,
            fields,
            ctx,
        )
        fields.remove(65)

    ctx.register_fields(fields)

    for field in [str(i) for i in sorted(fields)]:
        idx = _decode_field(s, doc_dec, doc_enc, idx, field, spec[field], ctx)
        ctx.resolve_field(field)

    for field in list(ctx.pending_fields):
        ctx.resolve_field(field)

    if idx != len(s):
        _raise_decode(
            ctx,
            "Extra data after last field",
            s,
            doc_dec,
            doc_enc,
            idx,
            field,
            "trailing",
            "extra_data",
            idx,
            len(s),
            len(s),
            mark_field=False,
        )

    return doc_dec, doc_enc, ctx


#
# Private interface
#

_FieldSpecDict = Mapping[str, Any]


def _merge_sensitive_fields(*groups: Optional[Iterable[str]]) -> Set[str]:
    fields: Set[str] = set()
    for group in groups:
        if group is None:
            continue
        if isinstance(group, str):
            fields.add(group)
        else:
            fields.update(str(f) for f in group)
    return fields


def _collect_secret_fields(
    spec: SpecDict, extra_fields: Iterable[str] = ()
) -> Set[str]:
    secret: Set[str] = {str(f) for f in extra_fields}
    for field_key, field_spec in spec.items():
        if not isinstance(field_spec, Mapping):
            continue
        if any(bool(field_spec.get(key)) for key in _SECRET_SPEC_KEYS):
            secret.add(str(field_key))
    return secret


def _public_trace(frame: Mapping[str, Any]) -> Dict[str, Any]:
    parts: Dict[str, Any] = {}
    for name, part in frame.get("parts", {}).items():
        parts[name] = {
            "start": int(part["start"]),
            "end": int(part["end"]),
            "expected_end": (
                None if part.get("expected_end") is None else int(part["expected_end"])
            ),
            "status": str(part.get("status", "pending")),
        }

    trace: Dict[str, Any] = {
        "field": frame["field"],
        "kind": frame["kind"],
        "start": int(frame["start"]),
        "end": int(frame["end"]),
        "status": frame["status"],
        "parts": parts,
    }
    if frame.get("_error") is not None:
        err = frame["_error"]
        trace["error"] = {
            "part": err["part"],
            "kind": err["kind"],
            "start": err["start"],
            "end": err["end"],
            "expected_end": err["expected_end"],
        }
    return trace


def _field_traces(
    ctx: "_DecodeContext", doc_dec: Optional[Mapping[str, str]]
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for key, frame in ctx.traces.items():
        trace = _public_trace(frame)
        if ctx.is_secret(key):
            trace["redacted"] = True
            fields[key] = trace
            continue
        if doc_dec is not None and frame["status"] == "ok" and key in doc_dec:
            trace["value"] = doc_dec[key]
        if frame.get("_declared_length") is not None:
            trace["declared_length"] = frame["_declared_length"]
            trace["length_count"] = frame["_len_count"]
        fields[key] = trace
    return fields


def _build_diagnostic(
    ctx: "_DecodeContext",
    msg: str,
    field_key: str,
    part: str,
    kind: str,
    pos: int,
    error_start: int,
    error_end: Optional[int] = None,
    expected_end: Optional[int] = None,
    status: str = "error",
    doc_dec: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    size = ctx.size
    err_end = size if error_end is None else min(error_end, size)
    completed_end = min(ctx.completed_end, size)
    if status == "ok":
        completed_end = size

    return {
        "status": status,
        "kind": kind,
        "msg": msg,
        "field": field_key,
        "part": part,
        "pos": pos,
        "error_position": error_start,
        "completed": {"start": 0, "end": completed_end},
        "error": {
            "part": part,
            "kind": kind,
            "start": error_start,
            "end": err_end,
            "expected_end": expected_end,
        },
        "undetermined": {"start": error_start, "end": size},
        "fields": _field_traces(ctx, doc_dec),
        "redacted_fields": sorted(ctx.secret_fields),
    }


def _success_diagnostic(
    ctx: "_DecodeContext", doc_dec: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    for key in ctx.traces:
        ctx.resolve_field(key)
    return {
        "status": "ok",
        "kind": "ok",
        "msg": "",
        "field": "",
        "part": "",
        "pos": ctx.size,
        "error_position": ctx.size,
        "completed": {"start": 0, "end": ctx.size},
        "error": {},
        "undetermined": {"start": ctx.size, "end": ctx.size},
        "fields": _field_traces(ctx, doc_dec),
        "redacted_fields": sorted(ctx.secret_fields),
    }


def _redacted_bytes(
    s: Union[bytes, bytearray], ranges: Iterable[Tuple[int, int]]
) -> Union[bytes, bytearray]:
    data = bytes(s)
    markers = sorted((max(0, start), min(len(data), end)) for start, end in ranges)
    for start, end in markers:
        if end <= start:
            continue
        data = data[:start] + b"*" * (end - start) + data[end:]
    return bytearray(data) if isinstance(s, bytearray) else data


def _redact_attachments(
    ctx: "_DecodeContext",
    s: Union[bytes, bytearray],
    doc_dec: DecodedDict,
    doc_enc: EncodedDict,
) -> Tuple[Union[bytes, bytearray], DecodedDict, EncodedDict]:
    if not ctx.secret_fields:
        return s, doc_dec, doc_enc

    ranges: List[Tuple[int, int]] = []
    for key, frame in ctx.traces.items():
        if key not in ctx.secret_fields:
            continue
        start = int(frame["_length_start"])
        if frame["status"] == "ok":
            end = int(frame["end"])
            if end <= start:
                end = min(
                    ctx.size,
                    max(
                        int(frame["_length_end"]),
                        int(frame["_expected_data_end"] or frame["_data_start"]),
                    ),
                )
        else:
            # Field length was invalid or data was cut short, so the
            # remaining bytes may still belong to the secret payload.
            end = ctx.size
        ranges.append((start, end))

    # Anything after the last trusted field may contain secret material.
    undetermined = _undetermined_start(ctx)
    if ctx.secret_fields and (
        not ctx.fields_known
        or any(field in ctx.secret_fields for field in ctx.pending_fields)
        or any(
            frame.get("_error") is not None
            and frame.get("_error", {}).get("kind") == "extra_data"
            for frame in ctx.traces.values()
        )
        or any(
            field in ctx.secret_fields and frame.get("_error") is not None
            for field, frame in ctx.traces.items()
        )
    ):
        if undetermined < ctx.size:
            ranges.append((undetermined, ctx.size))

    redacted_s = _redacted_bytes(s, ranges)
    redacted_dec = {
        key: value for key, value in doc_dec.items() if key not in ctx.secret_fields
    }
    redacted_enc = {
        key: value for key, value in doc_enc.items() if key not in ctx.secret_fields
    }
    return redacted_s, redacted_dec, redacted_enc


def _undetermined_start(ctx: "_DecodeContext") -> int:
    for frame in ctx.traces.values():
        if frame.get("_error") is not None:
            return int(frame["_error"]["start"])
    return min(ctx.completed_end, ctx.size)


def _raise_decode(
    ctx: "_DecodeContext",
    msg: str,
    s: Union[bytes, bytearray],
    doc_dec: DecodedDict,
    doc_enc: EncodedDict,
    pos: int,
    field_key: str,
    part: str,
    kind: str,
    error_start: int,
    error_end: int,
    expected_end: Optional[int],
    mark_field: bool = True,
) -> NoReturn:
    frame = ctx.traces.get(field_key)
    if mark_field:
        if frame is None:
            frame = ctx.begin(field_key, "field", error_start)
        ctx.mark_error(frame, part, kind, error_start, error_end, expected_end)

    public_msg = msg
    if mark_field and ctx.is_secret(field_key):
        public_msg = f"Failed to decode secret field, {part}"

    diagnostic = _build_diagnostic(
        ctx,
        msg=msg,
        field_key=field_key,
        part=part,
        kind=kind,
        pos=pos,
        error_start=error_start,
        error_end=error_end,
        expected_end=expected_end,
        status="error",
        doc_dec=doc_dec,
    )

    redacted_s, redacted_dec, redacted_enc = _redact_attachments(
        ctx, s, doc_dec, doc_enc
    )
    diagnostic["msg"] = public_msg
    raise DecodeError(
        public_msg,
        redacted_s,
        redacted_dec,
        redacted_enc,
        pos,
        field_key,
        diagnostic,
    ) from None


def _minimal_diagnostic(
    msg: str,
    pos: int,
    field_key: str,
    doc_dec: Mapping[str, str],
    doc_enc: Mapping[str, Mapping[str, bytes]],
    size: int,
) -> Dict[str, Any]:
    if msg.startswith("Field length is"):
        part = "length"
        kind = "truncated_field_length"
    elif msg.startswith("Field data is") and "larger than maximum" not in msg:
        part = "data"
        kind = "truncated_field_data"
    elif msg == "Extra data after last field":
        part = "trailing"
        kind = "extra_data"
    elif msg.startswith("Failed to decode field length"):
        part = "length"
        kind = "invalid_field_length"
    elif msg.startswith("Failed to decode field"):
        part = "data"
        kind = "invalid_field_data"
    else:
        part = "data"
        kind = "decode_error"

    return {
        "status": "error",
        "kind": kind,
        "msg": msg,
        "field": field_key,
        "part": part,
        "pos": pos,
        "error_position": pos,
        "completed": {"start": 0, "end": pos},
        "error": {
            "part": part,
            "kind": kind,
            "start": pos,
            "end": size,
            "expected_end": None,
        },
        "undetermined": {"start": pos, "end": size},
        "fields": {},
        "redacted_fields": [],
    }


def _decode_header(
    s: Union[bytes, bytearray],
    doc_dec: DecodedDict,
    doc_enc: EncodedDict,
    idx: int,
    spec: SpecDict,
    ctx: "_DecodeContext",
) -> int:
    r"""Decode ISO8583 header data if present.

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data
    doc_dec : dict
        Dict containing decoded ISO8583 data
    doc_enc : dict
        Dict containing encoded ISO8583 data
    idx : int
        Current index in ISO8583 byte array
    spec : dict
        A Python dict defining ISO8583 specification.
        See :mod:`iso8583.specs` module for examples.
    ctx : _DecodeContext
        Diagnostic bookkeeping

    Returns
    -------
    int
        Index in ISO8583 byte array where parsing of the header ended

    Raises
    ------
    DecodeError
        An error decoding ISO8583 bytearray.
    """

    # Header is not expected according to specifications
    if spec["h"]["max_len"] <= 0:
        return idx

    return _decode_field(s, doc_dec, doc_enc, idx, "h", spec["h"], ctx)


def _decode_type(
    s: Union[bytes, bytearray],
    doc_dec: DecodedDict,
    doc_enc: EncodedDict,
    idx: int,
    spec: SpecDict,
    ctx: "_DecodeContext",
) -> int:
    r"""Decode ISO8583 message type.

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data
    doc_dec : dict
        Dict containing decoded ISO8583 data
    doc_enc : dict
        Dict containing encoded ISO8583 data
    idx : int
        Current index in ISO8583 byte array
    spec : dict
        A Python dict defining ISO8583 specification.
        See :mod:`iso8583.specs` module for examples.
    ctx : _DecodeContext
        Diagnostic bookkeeping

    Returns
    -------
    int
        Index in ISO8583 byte array where parsing of message type ended

    Raises
    ------
    DecodeError
        An error decoding ISO8583 bytearray.
    """

    # Message type is a set length in ISO8583
    if spec["t"]["data_enc"] == "b":
        expected_field_len = 2
    else:
        expected_field_len = 4

    frame = ctx.begin(
        "t",
        "type",
        idx,
        0,
        "bytes",
        expected_end=idx + expected_field_len,
        part_name="data",
    )

    encoded_field_data = s[idx : idx + expected_field_len]
    doc_dec["t"] = ""
    doc_enc["t"] = {"len": b"", "data": bytes(encoded_field_data)}

    if len(encoded_field_data) != expected_field_len:
        _raise_decode(
            ctx,
            f"Field data is {len(encoded_field_data)} bytes, expecting {expected_field_len}",
            s,
            doc_dec,
            doc_enc,
            idx,
            "t",
            "data",
            "truncated_field_data",
            idx,
            idx + len(encoded_field_data),
            idx + expected_field_len,
        )

    if spec["t"]["data_enc"] == "b":
        doc_dec["t"] = encoded_field_data.hex().upper()
    else:
        doc_dec["t"] = _decode_text_data(
            s,
            encoded_field_data,
            idx,
            doc_dec,
            doc_enc,
            "t",
            spec["t"],
            ctx,
            idx + expected_field_len,
        )

    ctx.finish(frame, idx + expected_field_len)
    return idx + expected_field_len


def _decode_bitmap(
    s: Union[bytes, bytearray],
    doc_dec: DecodedDict,
    doc_enc: EncodedDict,
    idx: int,
    field_key: str,
    field_spec: _FieldSpecDict,
    field_offset: Literal[0, 64, 128],
    is_extended: bool,
    fields: Set[int],
    ctx: "_DecodeContext",
) -> int:
    r"""Decode ISO8583 a bitmap.

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data
    doc_dec : dict
        Dict containing decoded ISO8583 data
    doc_enc : dict
        Dict containing encoded ISO8583 data
    idx : int
        Current index in ISO8583 byte array
    field_key : str
        Field ID to be decoded
    field_spec : dict
        A Python dict defining ISO8583 specification for this field.
        See :mod:`iso8583.specs` module for examples.
    field_offset : int
        Offset by which to adjust fields from 1-64 range.
    is_extended : bool
        If true then processing an extension of an already processed bitmap
    fields: set
        Will be populated with enabled field numbers
    ctx : _DecodeContext
        Diagnostic bookkeeping

    Returns
    -------
    int
        Index in ISO8583 byte array where parsing of bitmaps ended

    Raises
    ------
    DecodeError
        An error decoding ISO8583 bytearray.
    """

    # Primary/Secondary bitmap is a set length in ISO8583
    if field_spec["data_enc"] == "b":
        expected_field_len = 8
    else:
        expected_field_len = 16

    frame = ctx.begin(
        field_key,
        "bitmap",
        idx,
        0,
        "bytes",
        expected_end=idx + expected_field_len,
        is_extended=is_extended,
        part_name="bitmap_extension" if is_extended else "bitmap",
    )

    encoded_field_data = s[idx : idx + expected_field_len]

    if is_extended:
        doc_enc[field_key]["data"] = doc_enc[field_key]["data"] + bytes(
            encoded_field_data
        )
    else:
        doc_dec[field_key] = ""
        doc_enc[field_key] = {"len": b"", "data": bytes(encoded_field_data)}

    if len(encoded_field_data) != expected_field_len:
        _raise_decode(
            ctx,
            f"Field data is {len(doc_enc[field_key]['data'])} bytes, expecting {expected_field_len * 2 if is_extended else expected_field_len}",
            s,
            doc_dec,
            doc_enc,
            idx,
            field_key,
            "bitmap_extension" if is_extended else "bitmap",
            "truncated_bitmap",
            idx,
            idx + len(encoded_field_data),
            idx + expected_field_len,
        )

    if field_spec["data_enc"] == "b":
        decoded_field_data = encoded_field_data.hex().upper()
        doc_dec[field_key] = (
            doc_dec[field_key] + decoded_field_data
            if is_extended
            else decoded_field_data
        )
        bitmap = encoded_field_data
    else:
        decoded_field_data = _decode_text_data(
            s,
            encoded_field_data,
            idx,
            doc_dec,
            doc_enc,
            field_key,
            field_spec,
            ctx,
            idx + expected_field_len,
            "bitmap_extension" if is_extended else "bitmap",
        )
        doc_dec[field_key] = (
            doc_dec[field_key] + decoded_field_data
            if is_extended
            else decoded_field_data
        )

        try:
            bitmap = bytes.fromhex(decoded_field_data)
        except Exception:
            if len(encoded_field_data) == expected_field_len:
                _raise_decode(
                    ctx,
                    "Failed to decode field, non-hex data",
                    s,
                    doc_dec,
                    doc_enc,
                    idx,
                    field_key,
                    "bitmap_extension" if is_extended else "bitmap",
                    "invalid_bitmap",
                    idx,
                    idx + len(encoded_field_data),
                    idx + expected_field_len,
                )
            _raise_decode(
                ctx,
                "Failed to decode field, non-hex data",
                s,
                doc_dec,
                doc_enc,
                idx,
                field_key,
                "bitmap_extension" if is_extended else "bitmap",
                "truncated_bitmap",
                idx,
                idx + len(encoded_field_data),
                idx + expected_field_len,
            )

    fields.update(
        [
            field_offset + byte_idx * 8 + bit
            for bit in range(1, 9)
            for byte_idx, byte in enumerate(bitmap)
            if byte >> (8 - bit) & 1
        ]
    )

    ctx.finish(frame, idx + expected_field_len)
    return idx + expected_field_len


def _decode_field(
    s: Union[bytes, bytearray],
    doc_dec: DecodedDict,
    doc_enc: EncodedDict,
    idx: int,
    field_key: str,
    field_spec: _FieldSpecDict,
    ctx: "_DecodeContext",
) -> int:
    r"""Decode ISO8583 individual fields.

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data
    doc_dec : dict
        Dict containing decoded ISO8583 data
    doc_enc : dict
        Dict containing encoded ISO8583 data
    idx : int
        Current index in ISO8583 byte array
    field_key : str
        Field ID to be decoded
    field_spec : dict
        A Python dict defining ISO8583 specification for this field.
        See :mod:`iso8583.specs` module for examples.
    ctx : _DecodeContext
        Diagnostic bookkeeping

    Returns
    -------
    int
        Index in ISO8583 byte array where parsing of the field ended

    Raises
    ------
    DecodeError
        An error decoding ISO8583 bytearray.
    """
    len_type = int(field_spec["len_type"])

    # Optional field added in v2.1. Prior specs do not have it.
    len_count = field_spec.get("len_count", "bytes")

    frame = ctx.begin(field_key, "field", idx, len_type, len_count)

    doc_dec[field_key] = ""
    doc_enc[field_key] = {"len": bytes(s[idx : idx + len_type]), "data": b""}

    length_bytes = s[idx : idx + len_type]
    if len(length_bytes) != len_type:
        _raise_decode(
            ctx,
            f"Field length is {len(length_bytes)} bytes wide, expecting {len_type}",
            s,
            doc_dec,
            doc_enc,
            idx,
            field_key,
            "length",
            "truncated_field_length",
            idx,
            idx + len(length_bytes),
            idx + len_type,
        )

    # Parse field length if present.
    # For fixed-length fields max_len is the length.
    if len_type == 0:
        enc_field_len: int = field_spec["max_len"]
    # Variable field length
    else:
        # Binary length
        if field_spec["len_enc"] == "b":
            try:
                enc_field_len = int.from_bytes(length_bytes, "big", signed=False)
            # It does not seem to be possible to hit this unless
            # it's a type or input parameter error.
            # However, keeping this, because you just never know.
            except Exception as e:  # pragma: no cover
                _raise_decode(
                    ctx,
                    f"Failed to decode field length, {e}",
                    s,
                    doc_dec,
                    doc_enc,
                    idx,
                    field_key,
                    "length",
                    "invalid_field_length",
                    idx,
                    idx + len_type,
                    idx + len_type,
                )
        # BCD length
        elif field_spec["len_enc"] == "bcd":
            try:
                enc_field_len = int(length_bytes.hex(), 10)
            except Exception:
                _raise_decode(
                    ctx,
                    "Failed to decode field length, invalid BCD data",
                    s,
                    doc_dec,
                    doc_enc,
                    idx,
                    field_key,
                    "length",
                    "invalid_field_length",
                    idx,
                    idx + len_type,
                    idx + len_type,
                )
        # Text length
        else:
            try:
                decoded_length = length_bytes.decode(field_spec["len_enc"])
            except LookupError:
                _raise_decode(
                    ctx,
                    "Failed to decode field length, unknown encoding specified",
                    s,
                    doc_dec,
                    doc_enc,
                    idx,
                    field_key,
                    "length",
                    "invalid_field_length",
                    idx,
                    idx + len_type,
                    idx + len_type,
                )
            except Exception:
                _raise_decode(
                    ctx,
                    "Failed to decode field length, invalid data",
                    s,
                    doc_dec,
                    doc_enc,
                    idx,
                    field_key,
                    "length",
                    "invalid_field_length",
                    idx,
                    idx + len_type,
                    idx + len_type,
                )

            try:
                enc_field_len = int(decoded_length)
            except Exception:
                _raise_decode(
                    ctx,
                    "Failed to decode field length, non-numeric data",
                    s,
                    doc_dec,
                    doc_enc,
                    idx,
                    field_key,
                    "length",
                    "invalid_field_length",
                    idx,
                    idx + len_type,
                    idx + len_type,
                )

        if enc_field_len > field_spec["max_len"]:
            _raise_decode(
                ctx,
                f"Field data is {enc_field_len} {len_count}, larger than maximum {field_spec['max_len']}",
                s,
                doc_dec,
                doc_enc,
                idx,
                field_key,
                "length",
                "field_length_exceeds_maximum",
                idx,
                idx + len_type,
                idx + len_type,
            )

    data_idx = idx + len_type
    ctx.mark_length_ok(frame, data_idx)

    # Do not parse zero-length field
    if enc_field_len == 0:
        ctx.finish(frame, data_idx)
        return data_idx

    # Encoded field length can be in bytes or half bytes (nibbles)
    # Convert nibbles to bytes if needed
    if len_count == "nibbles":
        if enc_field_len & 1:
            byte_field_len = (enc_field_len + 1) // 2
        else:
            byte_field_len = enc_field_len // 2
    else:
        byte_field_len = enc_field_len

    data_end = data_idx + byte_field_len
    ctx.mark_data_position(
        frame,
        data_idx,
        data_end,
        declared_length=enc_field_len,
        byte_length=byte_field_len,
    )

    # Parse field data
    doc_enc[field_key]["data"] = bytes(s[data_idx:data_end])
    if len(doc_enc[field_key]["data"]) != byte_field_len:
        if len_count == "nibbles":
            actual_field_len = len(doc_enc[field_key]["data"]) * 2
        else:
            actual_field_len = len(doc_enc[field_key]["data"])

        _raise_decode(
            ctx,
            f"Field data is {actual_field_len} {len_count}, expecting {enc_field_len}",
            s,
            doc_dec,
            doc_enc,
            data_idx,
            field_key,
            "data",
            "truncated_field_data",
            data_idx,
            data_idx + len(doc_enc[field_key]["data"]),
            data_end,
        )

    if field_spec["data_enc"] == "b":
        doc_dec[field_key] = doc_enc[field_key]["data"].hex().upper()
        if len_count == "nibbles" and enc_field_len & 1:
            doc_dec[field_key] = _remove_pad_field(
                s,
                data_idx,
                doc_dec,
                doc_enc,
                field_key,
                field_spec,
                enc_field_len,
                ctx,
                data_end,
            )
    else:
        doc_dec[field_key] = _decode_text_data(
            s,
            doc_enc[field_key]["data"],
            data_idx,
            doc_dec,
            doc_enc,
            field_key,
            field_spec,
            ctx,
            data_end,
        )

    ctx.finish(frame, data_end)
    return data_end


def _remove_pad_field(
    s: Union[bytes, bytearray],
    idx: int,
    doc_dec: DecodedDict,
    doc_enc: EncodedDict,
    field_key: str,
    field_spec: _FieldSpecDict,
    enc_field_len: int,
    ctx: Optional["_DecodeContext"] = None,
    data_end: Optional[int] = None,
) -> str:
    r"""Remove left or right pad from a BCD or hex field.

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data
    idx : int
        Current index in ISO8583 byte array
    doc_dec : dict
        Dict containing decoded ISO8583 data
    doc_enc : dict
        Dict containing encoded ISO8583 data
    field_key : str
        Field ID to remove pad from
    field_spec : dict
        A Python dict defining ISO8583 specification for this field.
        See :mod:`iso8583.specs` module for examples.
    enc_field_len : int
        Number of nibbles expected in the field
    ctx : _DecodeContext, optional
        Diagnostic bookkeeping
    data_end : int, optional
        Expected end of the field data

    Returns
    -------
    str
        Field data without pad

    Raises
    ------
    DecodeError
        An error decoding ISO8583 bytearray.
    """
    pad: str = field_spec.get("left_pad", "")[:1]
    if len(pad) > 0 and doc_dec[field_key][:1] == pad:
        return doc_dec[field_key][1:]

    pad = field_spec.get("right_pad", "")[:1]
    if len(pad) > 0 and doc_dec[field_key][-1:] == pad:
        return doc_dec[field_key][:-1]

    msg = f"Field data is {len(doc_dec[field_key])} nibbles, expecting {enc_field_len}"
    if ctx is not None and data_end is not None:
        _raise_decode(
            ctx,
            msg,
            s,
            doc_dec,
            doc_enc,
            idx,
            field_key,
            "data",
            "invalid_field_pad",
            idx,
            data_end,
            data_end,
        )
    raise DecodeError(
        msg,
        s,
        doc_dec,
        doc_enc,
        idx,
        field_key,
        _minimal_diagnostic(msg, idx, field_key, doc_dec, doc_enc, len(s)),
    )


def _decode_text_data(
    s: Union[bytes, bytearray],
    data: Union[bytes, bytearray],
    idx: int,
    doc_dec: DecodedDict,
    doc_enc: EncodedDict,
    field_key: str,
    field_spec: _FieldSpecDict,
    ctx: Optional["_DecodeContext"] = None,
    data_end: Optional[int] = None,
    part_name: str = "data",
) -> str:
    r"""Decode non-binary field and handle errors if any

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data
    data : bytes or bytearray
        Encoded ISO8583 data to decode
    idx : int
        Current index in ISO8583 byte array
    doc_dec : dict
        Dict containing decoded ISO8583 data
    doc_enc : dict
        Dict containing encoded ISO8583 data
    field_key : str
        Field ID to remove pad from
    field_spec : dict
        A Python dict defining ISO8583 specification for this field.
        See :mod:`iso8583.specs` module for examples.
    ctx : _DecodeContext, optional
        Diagnostic bookkeeping
    data_end : int, optional
        End of the field data being decoded

    Returns
    -------
    str
        Decoded ISO8583 field data

    Raises
    ------
    DecodeError
        An error decoding ISO8583 bytearray.
    """
    try:
        return data.decode(field_spec["data_enc"])
    except LookupError:
        msg = "Failed to decode field, unknown encoding specified"
    except Exception:
        msg = "Failed to decode field, invalid data"

    if ctx is not None and data_end is not None:
        _raise_decode(
            ctx,
            msg,
            s,
            doc_dec,
            doc_enc,
            idx,
            field_key,
            part_name,
            "invalid_field_data",
            idx,
            data_end,
            data_end,
        )
    raise DecodeError(
        msg,
        s,
        doc_dec,
        doc_enc,
        idx,
        field_key,
        _minimal_diagnostic(msg, idx, field_key, doc_dec, doc_enc, len(s)),
    ) from None
