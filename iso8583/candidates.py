r"""Decode one ISO8583 message against several candidate specifications.

An importer often has more than one plausible specification for the same
message.  This is common while a proprietary header or field layout changes
and both the old and the new layout are still in use.  :func:`decode_candidates`
runs the regular decoder against every candidate, records why each candidate
was accepted or rejected, and only selects a candidate when the result is
unambiguous.

The selection result never depends on the order in which candidate
specifications are supplied.  Candidates are identified by a stable name and
reported in name order.  When several candidates decode the same bytes
completely, no candidate wins by position.  An explicit ``priority`` may be
used to resolve an ambiguity; otherwise the report exposes the common fields
and the fields on which the candidates disagree.
"""

from collections.abc import Collection as CollectionABC
from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import re
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Pattern,
    Sequence,
    Set,
    Tuple,
    Union,
    cast as typing_cast,
)

from iso8583.decoder import DecodeError, decode

__all__ = [
    "CandidateCheck",
    "CandidateReport",
    "CandidateResult",
    "CandidateSpec",
    "FieldDivergence",
    "decode_candidates",
]

DecodedDict = Dict[str, str]
EncodedDict = Dict[str, Dict[str, bytes]]
SpecDict = Mapping[str, Mapping[str, Any]]
HeaderRule = Union[
    str,
    bytes,
    Pattern[str],
    Callable[[Optional[str]], bool],
    Collection[Any],
]
FieldRule = Union[Pattern[str], str, Callable[[str], bool], Collection[Any]]

_EXTRA_DATA_MSG = "Extra data after last field"
_STRUCTURAL_KEYS = frozenset(("h", "t", "p", "1"))


@dataclass(frozen=True)
class CandidateSpec:
    r"""One specification offered to :func:`decode_candidates`.

    Parameters
    ----------
    name : str
        Stable identifier for the specification.  It is used in reports and
        must be unique in a candidate collection.
    spec : dict
        An ISO8583 specification accepted by :func:`iso8583.decode`.
    header : optional
        Allowed decoded header value.  A ``str`` is compared by equality,
        ``bytes`` is compared with the raw header bytes, a compiled regular
        expression is matched with :meth:`re.Pattern.fullmatch`, a callable
        receives the decoded header, and any other collection is treated as
        a collection of allowed values.
    header_prefix : optional
        Allowed header prefix.  Uses the same value types as ``header`` but
        compares with ``startswith``.  ``header`` and ``header_prefix`` are
        applied together when both are supplied.
    allowed_fields : optional collection of int or str
        Data-field numbers the candidate may enable.  String values are
        accepted for convenience.  Field 1 (the secondary bitmap indicator)
        is structural and is not included.  When omitted, the field
        definitions present in ``spec`` define the supported range.
    required_fields, forbidden_fields : collection of str
        Field keys that must be present or absent in the decoded result.
    validators : mapping of str to rule
        Per-field value validation.  A rule may be a compiled regex (matched
        with ``fullmatch``), an exact string value, a callable returning
        truthy/falsey, or a collection of allowed values.  Validators apply
        only to fields present in the message; use ``required_fields`` to
        require presence.
    priority : int
        Optional semantic tie-breaker.  A unique highest priority among
        otherwise valid candidates selects that candidate.  Equal priorities
        leave the result ambiguous.
    destination : optional str
        Opaque routing label, such as the destination table for an import.
        It is reported but never used for arbitration.
    """

    name: str
    spec: SpecDict
    header: Optional[HeaderRule] = None
    header_prefix: Optional[HeaderRule] = None
    allowed_fields: Optional[Collection[Union[int, str]]] = None
    required_fields: Collection[str] = ()
    forbidden_fields: Collection[str] = ()
    validators: Mapping[str, FieldRule] = field(default_factory=dict)
    priority: int = 0
    destination: Optional[str] = None


@dataclass(frozen=True)
class CandidateCheck:
    r"""Outcome of one filtering dimension for a candidate."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class CandidateResult:
    r"""Decoding and filtering outcome for one candidate specification."""

    name: str
    priority: int
    spec: SpecDict
    status: str
    checks: Tuple[CandidateCheck, ...]
    decoded: Optional[DecodedDict]
    encoded: Optional[EncodedDict]
    consumed: int
    total: int
    remaining: bytes
    destination: Optional[str] = None
    error: Optional[str] = None

    @property
    def eligible(self) -> bool:
        r"""Whether this candidate passed every filter and consumed all data."""
        return self.status in ("selected", "valid")

    @property
    def reasons(self) -> Tuple[str, ...]:
        r"""Human-readable failure details, one per failed check."""
        return tuple(check.detail for check in self.checks if not check.ok)

    @property
    def remaining_hex(self) -> str:
        r"""Unconsumed trailing bytes as an upper-case hexadecimal string."""
        return self.remaining.hex().upper()


@dataclass(frozen=True)
class FieldDivergence:
    r"""A field on which valid candidates do not agree.

    ``kind`` is ``"meaning"`` when the field definitions differ, ``"value"``
    when values differ but definitions agree, and ``"presence"`` when the
    field appears under some candidates only.  ``meanings`` maps each
    candidate name to a canonical description of its field definition.
    ``values`` maps each candidate name to the decoded value under that
    candidate.
    """

    field: str
    kind: str
    values: Mapping[str, Optional[str]]
    meanings: Mapping[str, Optional[str]]
    descriptions: Mapping[str, Optional[str]]


@dataclass(frozen=True)
class CandidateReport:
    r"""Result of evaluating a message against all candidate specifications."""

    candidates: Tuple[CandidateResult, ...]
    selected: Optional[CandidateResult]
    ambiguous: bool
    common_fields: Mapping[str, str]
    divergences: Tuple[FieldDivergence, ...]

    @property
    def winner(self) -> Optional[CandidateResult]:
        r"""Alias for :attr:`selected`."""
        return self.selected

    @property
    def best(self) -> Optional[CandidateResult]:
        r"""Alias for :attr:`selected`.  Kept for readability at call sites."""
        return self.selected

    @property
    def selected_name(self) -> Optional[str]:
        r"""Name of the winning candidate, or ``None`` when unresolved."""
        return self.selected.name if self.selected is not None else None

    @property
    def outcome(self) -> str:
        r"""One of ``"selected"``, ``"ambiguous"``, or ``"unresolved"``."""
        if self.selected is not None:
            return "selected"
        return "ambiguous" if self.ambiguous else "unresolved"

    @property
    def valid_candidates(self) -> Tuple[CandidateResult, ...]:
        r"""Candidates that passed every filter and consumed all data."""
        return tuple(candidate for candidate in self.candidates if candidate.eligible)

    @property
    def destinations(self) -> Tuple[Optional[str], ...]:
        r"""Unique destinations of eligible candidates, in name order."""
        seen: List[Optional[str]] = []
        for candidate in self.valid_candidates:
            if candidate.destination not in seen:
                seen.append(candidate.destination)
        return tuple(seen)

    @property
    def common_destination(self) -> Optional[str]:
        r"""Destination shared by all eligible candidates, if there is one.

        Returns ``None`` when eligible candidates disagree, when no candidate
        declares a destination, or when there is nothing eligible.
        """
        destinations = self.destinations
        if len(destinations) != 1:
            return None
        return destinations[0]

    @property
    def rejected_candidates(self) -> Tuple[CandidateResult, ...]:
        r"""Candidates that failed at least one filter."""
        return tuple(
            candidate for candidate in self.candidates if not candidate.eligible
        )

    @property
    def trailing_candidates(self) -> Tuple[CandidateResult, ...]:
        r"""Candidates that decoded fields but left unconsumed bytes."""
        return tuple(
            candidate for candidate in self.candidates if candidate.status == "trailing"
        )

    def as_dict(self) -> Dict[str, Any]:
        r"""Return a JSON-compatible representation of this report."""

        return {
            "selected": self.selected_name,
            "selected_destination": (
                self.selected.destination if self.selected is not None else None
            ),
            "ambiguous": self.ambiguous,
            "outcome": self.outcome,
            "common_fields": dict(self.common_fields),
            "common_destination": self.common_destination,
            "divergences": [
                {
                    "field": divergence.field,
                    "kind": divergence.kind,
                    "values": dict(divergence.values),
                    "meanings": dict(divergence.meanings),
                    "descriptions": dict(divergence.descriptions),
                }
                for divergence in self.divergences
            ],
            "candidates": [
                {
                    "name": candidate.name,
                    "priority": candidate.priority,
                    "destination": candidate.destination,
                    "status": candidate.status,
                    "checks": [
                        {
                            "name": check.name,
                            "ok": check.ok,
                            "detail": check.detail,
                        }
                        for check in candidate.checks
                    ],
                    "decoded": candidate.decoded,
                    "consumed": candidate.consumed,
                    "total": candidate.total,
                    "remaining": candidate.remaining_hex,
                    "error": candidate.error,
                }
                for candidate in self.candidates
            ],
        }

    def explain(self, line_width: int = 100) -> str:
        r"""Render a human-readable account of the arbitration.

        The output names the outcome, the selected destination when one
        exists, and every candidate's filter trail.  It is meant for importer
        diagnostics and logs rather than machine processing; use
        :meth:`as_dict` for that.
        """

        lines: List[str] = []
        if self.selected is not None:
            lines.append(f"selected: {self.selected.name}")
            if self.selected.destination is not None:
                lines.append(f"destination: {self.selected.destination}")
        elif self.ambiguous:
            lines.append("selected: none (ambiguous)")
            lines.append(
                f"independent of candidate order; {len(self.valid_candidates)} "
                "candidates decode the message completely"
            )
        else:
            lines.append("selected: none (no eligible candidate)")

        if self.common_fields:
            lines.append(
                "common fields: " + ", ".join(self.common_fields.keys())
            )
        if self.divergences:
            lines.append("divergent fields:")
            for divergence in self.divergences:
                detail = "; ".join(
                    f"{name}={value!r}"
                    for name, value in sorted(divergence.values.items())
                )
                lines.append(f"  {divergence.field} ({divergence.kind}): {detail}")

        for candidate in self.candidates:
            lines.append(f"[{candidate.name}] {candidate.status}")
            for check in candidate.checks:
                mark = "ok" if check.ok else "failed"
                text = f"  {check.name}: {mark} - {check.detail}"
                if len(text) > line_width:
                    text = text[: max(line_width - 3, 0)] + "..."
                lines.append(text)

        return "\n".join(lines)


def decode_candidates(
    s: Union[bytes, bytearray],
    candidates: Union[Mapping[str, Any], Iterable[Any]],
) -> CandidateReport:
    r"""Decode ISO8583 data against several candidate specifications.

    Each candidate is decoded independently.  A candidate is eligible only
    when its specification decodes the message, all configured header,
    bitmap, and field checks pass, and the decoded fields consume every input
    byte.  A candidate that decodes fields but leaves trailing bytes is
    reported as ``"trailing"`` and can never win.

    If exactly one candidate is eligible it is selected.  If several are
    eligible, the unique highest ``priority`` is selected; if priorities
    tie, ``selected`` is ``None`` and ``ambiguous`` is true.  In that case
    :attr:`CandidateReport.common_fields` contains fields whose decoded
    value and definition agree across all eligible candidates, while
    :attr:`CandidateReport.divergences` lists fields where values, meanings,
    or presence differ.

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data.
    candidates : mapping or iterable
        Candidate specifications.  Accepted forms are:

        - a mapping of candidate name to a specification dictionary or
          :class:`CandidateSpec`;
        - a single specification dictionary, which is treated as one
          candidate with a content-derived name;
        - an iterable of :class:`CandidateSpec` instances;
        - an iterable of ``(name, specification)`` pairs;
        - an iterable of candidate definition dictionaries with ``name``
          and ``spec`` keys;
        - an iterable of plain specification dictionaries.  In this case
          each candidate receives a stable name derived from the canonical
          specification content, so reordering the iterable does not change
          the report.

    Returns
    -------
    CandidateReport
        Filtering explanations for every candidate and the arbitration result.

    Raises
    ------
    TypeError
        ``s`` is not bytes or bytearray, or candidates have an unsupported
        shape.
    ValueError
        Candidate names are not unique or a candidate definition is invalid.

    Examples
    --------
    >>> import copy
    >>> import iso8583
    >>> from iso8583.specs import default_ascii as spec
    >>> llvar = copy.deepcopy(spec)
    >>> fixed = copy.deepcopy(llvar)
    >>> fixed["2"]["len_type"] = 0
    >>> fixed["2"]["max_len"] = 10
    >>> s = b"02004000000000000000101234567890"
    >>> report = iso8583.decode_candidates(s, {"llvar": llvar, "fixed": fixed})
    >>> report.selected_name
    'llvar'
    >>> [(c.name, c.status) for c in report.candidates]
    [('fixed', 'trailing'), ('llvar', 'selected')]
    >>> report.candidates[0].reasons
    ('2 trailing bytes after decoded fields: 3930',)
    """

    if not isinstance(s, (bytes, bytearray)):
        raise TypeError(
            "Encoded ISO8583 data must be bytes or bytearray, "
            f"not {s.__class__.__name__}"
        )

    normalized = _normalize_candidates(candidates)
    if not normalized:
        raise ValueError("At least one candidate specification is required")

    results = tuple(_evaluate_candidate(s, candidate) for candidate in normalized)

    selected = _select(results)
    if selected is not None:
        results = tuple(
            replace(result, status="selected")
            if result.name == selected.name
            else result
            for result in results
        )
        selected = next(result for result in results if result.name == selected.name)

    valid = tuple(result for result in results if result.eligible)
    common, divergences = _compare(valid)

    return CandidateReport(
        candidates=results,
        selected=selected,
        ambiguous=selected is None and len(valid) > 1,
        common_fields=common,
        divergences=divergences,
    )


#
# Private interface
#


def _normalize_candidates(
    candidates: Union[Mapping[str, Any], Iterable[Any]],
) -> Tuple[CandidateSpec, ...]:
    if isinstance(candidates, Mapping):
        if _looks_like_spec(candidates):
            normalized = [
                CandidateSpec(name=_automatic_name(candidates), spec=candidates)
            ]
        else:
            normalized = [
                _candidate_from_mapping(name, value)
                for name, value in candidates.items()
            ]
    elif isinstance(candidates, (str, bytes, bytearray)):
        raise TypeError("Candidate specifications must be a mapping or an iterable")
    else:
        try:
            items = list(candidates)
        except TypeError:
            raise TypeError(
                "Candidate specifications must be a mapping or an iterable"
            ) from None
        normalized = [_candidate_from_item(item) for item in items]

    # A plain spec dictionary gets a content-derived name.  Supplying the
    # same spec twice adds no information, so collapse such duplicates.
    checked: List[CandidateSpec] = []
    for item in normalized:
        if not isinstance(item.name, str) or not item.name:
            raise ValueError("Every candidate specification must have a non-empty name")
        if not isinstance(item.spec, Mapping):
            raise TypeError(f"Candidate {item.name!r} specification must be a mapping")
        if not isinstance(item.priority, int) or isinstance(item.priority, bool):
            raise TypeError(f"Candidate {item.name!r} priority must be an int")
        checked.append(_normalize_candidate(item))

    by_name: Dict[str, CandidateSpec] = {}
    for item in checked:
        existing = by_name.get(item.name)
        if existing is None:
            by_name[item.name] = item
        elif not _same_candidate(existing, item):
            raise ValueError(f"Duplicate candidate name: {item.name!r}")

    # Report order is name order, never input order.
    return tuple(sorted(by_name.values(), key=lambda candidate: candidate.name))


def _looks_like_spec(value: Mapping[Any, Any]) -> bool:
    message_type = value.get("t")
    primary_bitmap = value.get("p")
    if not isinstance(message_type, Mapping) or not isinstance(primary_bitmap, Mapping):
        return False
    return any(
        key in message_type or key in primary_bitmap
        for key in ("data_enc", "len_type", "max_len")
    )


def _same_candidate(left: CandidateSpec, right: CandidateSpec) -> bool:
    fields = (
        "header",
        "header_prefix",
        "allowed_fields",
        "required_fields",
        "forbidden_fields",
        "validators",
        "priority",
        "destination",
    )
    if _canonical_spec(left.spec) != _canonical_spec(right.spec):
        return False
    return all(
        _values_equal(getattr(left, name), getattr(right, name)) for name in fields
    )


def _values_equal(left: Any, right: Any) -> bool:
    try:
        return bool(left == right)
    except Exception:  # pragma: no cover - defensive for hostile rule objects
        return False


def _canonical_spec(spec: SpecDict) -> str:
    try:
        return json.dumps(spec, sort_keys=True, default=repr, separators=(",", ":"))
    except (TypeError, ValueError):  # pragma: no cover - defensive fallback
        return repr(spec)


def _normalize_candidate(candidate: CandidateSpec) -> CandidateSpec:
    allowed_fields: Optional[Collection[Union[int, str]]] = candidate.allowed_fields
    if allowed_fields is not None:
        allowed_values: Collection[Union[int, str]]
        if isinstance(allowed_fields, str):
            allowed_values = [allowed_fields]
        else:
            allowed_values = allowed_fields
        allowed = {
            _field_number(candidate.name, field_key, "allowed_fields")
            for field_key in allowed_values
        }
        allowed_fields = frozenset(allowed)

    required_fields = _normalize_field_keys(
        candidate.name, candidate.required_fields, "required_fields"
    )
    forbidden_fields = _normalize_field_keys(
        candidate.name, candidate.forbidden_fields, "forbidden_fields"
    )
    validators = {
        _field_key(key): rule for key, rule in candidate.validators.items()
    }

    return replace(
        candidate,
        allowed_fields=allowed_fields,
        required_fields=required_fields,
        forbidden_fields=forbidden_fields,
        validators=validators,
    )


def _normalize_field_keys(
    candidate_name: str,
    values: Collection[str],
    dimension: str,
) -> Collection[str]:
    if isinstance(values, (str, bytes)):
        values = [values]
    return frozenset(_field_key(value) for value in values)


def _field_key(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and value:
        return value
    raise TypeError(f"Field key must be a non-empty str or int, not {value!r}")


def _field_number(candidate_name: str, value: Any, dimension: str) -> int:
    number = value
    if isinstance(value, str) and value.isdigit():
        number = int(value)
    if not isinstance(number, int) or isinstance(number, bool) or number < 0:
        raise TypeError(
            f"Candidate {candidate_name!r} {dimension} must contain field numbers"
        )
    return number


def _candidate_from_mapping(name: Any, value: Any) -> CandidateSpec:
    if not isinstance(name, str) or not name:
        raise ValueError("Candidate mapping keys must be non-empty strings")
    if isinstance(value, CandidateSpec):
        if value.name != name:
            raise ValueError(
                f"Candidate name {value.name!r} does not match mapping key {name!r}"
            )
        return value
    if not isinstance(value, Mapping):
        raise TypeError(f"Candidate {name!r} specification must be a mapping")
    return CandidateSpec(name=name, spec=value)


def _candidate_from_item(item: Any) -> CandidateSpec:
    if isinstance(item, CandidateSpec):
        return item

    if isinstance(item, Mapping):
        definition_keys = {
            "name",
            "spec",
            "header",
            "header_prefix",
            "allowed_fields",
            "required_fields",
            "forbidden_fields",
            "validators",
            "priority",
            "destination",
        }
        if "name" in item and "spec" in item and set(item).issubset(definition_keys):
            return CandidateSpec(**dict(item))
        return CandidateSpec(name=_automatic_name(item), spec=item)

    if isinstance(item, (tuple, list)) and len(item) == 2:
        name, spec = item
        if isinstance(name, str) and isinstance(spec, Mapping):
            return CandidateSpec(name=name, spec=spec)

    raise TypeError(
        "Candidate must be a CandidateSpec, a (name, spec) pair, "
        "a candidate definition dictionary, or a specification dictionary"
    )


def _automatic_name(spec: Any) -> str:
    try:
        canonical = json.dumps(
            spec, sort_keys=True, default=repr, separators=(",", ":")
        )
    except (TypeError, ValueError):
        canonical = repr(spec)
    digest = sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"spec-{digest}"


def _evaluate_candidate(
    s: Union[bytes, bytearray], candidate: CandidateSpec
) -> CandidateResult:
    checks: List[CandidateCheck] = []
    decoded: Optional[DecodedDict] = None
    encoded: Optional[EncodedDict] = None
    consumed = 0
    remaining = b""
    error: Optional[str] = None
    trailing = False

    try:
        decoded, encoded = decode(s, candidate.spec)
        consumed = len(s)
        checks.append(
            CandidateCheck(
                "decode",
                True,
                f"decoded {len(decoded)} fields from {len(s)} bytes",
            )
        )
    except DecodeError as exc:
        decoded = exc.doc_dec
        encoded = exc.doc_enc
        consumed = exc.pos
        if exc.msg == _EXTRA_DATA_MSG:
            trailing = True
            remaining = bytes(s[exc.pos :])
            checks.append(
                CandidateCheck(
                    "decode",
                    True,
                    f"decoded fields through byte {exc.pos}; data remains",
                )
            )
        else:
            error = str(exc)
            checks.append(CandidateCheck("decode", False, error))
    except KeyError as exc:
        error = f"spec has no definition for field {exc.args[0]!r}"
        checks.append(CandidateCheck("decode", False, error))
    except Exception as exc:  # candidate filtering must not abort the import
        error = f"{exc.__class__.__name__}: {exc}"
        checks.append(CandidateCheck("decode", False, error))

    if checks[0].ok and decoded is not None and encoded is not None:
        checks.append(_check_header(candidate, decoded, encoded))
        checks.append(_check_bitmap(candidate, decoded))
        checks.append(_check_fields(candidate, decoded))
        checks.append(_check_consumption(candidate, decoded, consumed, remaining))

    other_checks_ok = all(
        check.ok for check in checks[1:] if check.name != "consumption"
    )
    if not checks[0].ok or not other_checks_ok:
        status = "rejected"
    elif trailing:
        status = "trailing"
    elif any(not check.ok for check in checks[1:]):
        status = "rejected"
    else:
        status = "valid"

    return CandidateResult(
        name=candidate.name,
        priority=candidate.priority,
        spec=candidate.spec,
        status=status,
        checks=tuple(checks),
        decoded=decoded,
        encoded=encoded,
        consumed=consumed,
        total=len(s),
        remaining=remaining,
        destination=candidate.destination,
        error=error,
    )


def _check_header(
    candidate: CandidateSpec,
    decoded: DecodedDict,
    encoded: EncodedDict,
) -> CandidateCheck:
    if candidate.header is None and candidate.header_prefix is None:
        return CandidateCheck("header", True, "no header constraint")

    if "h" not in decoded:
        return CandidateCheck(
            "header",
            False,
            "spec does not decode a header, but an allowed header is configured",
        )

    decoded_header = decoded["h"]
    raw_header = encoded.get("h", {}).get("data", b"")
    failures: List[str] = []

    if candidate.header is not None:
        if not _rule_matches(candidate.header, decoded_header, raw_header):
            failures.append(f"header {decoded_header!r} is not allowed")

    if candidate.header_prefix is not None:
        if not _rule_matches(
            candidate.header_prefix, decoded_header, raw_header, prefix=True
        ):
            failures.append(
                f"header {decoded_header!r} does not match allowed prefix"
            )

    if failures:
        return CandidateCheck("header", False, "; ".join(failures))

    return CandidateCheck("header", True, f"header {decoded_header!r} allowed")


def _rule_matches(
    rule: HeaderRule,
    decoded_header: Optional[str],
    raw_header: bytes,
    prefix: bool = False,
) -> bool:
    if isinstance(rule, re.Pattern):
        if decoded_header is None:
            return False
        if prefix:
            return rule.match(decoded_header) is not None
        return rule.fullmatch(decoded_header) is not None

    if isinstance(rule, str):
        if decoded_header is None:
            return False
        if prefix:
            return decoded_header.startswith(rule)
        return decoded_header == rule

    if isinstance(rule, bytes):
        if prefix:
            return raw_header.startswith(rule)
        return raw_header == rule

    if callable(rule):
        try:
            return bool(rule(decoded_header))
        except Exception:
            return False

    if isinstance(rule, CollectionABC) and not isinstance(rule, Mapping):
        return any(
            _rule_matches(value, decoded_header, raw_header, prefix)
            for value in typing_cast(Collection[Any], rule)
        )

    raise TypeError(f"Unsupported header rule type: {rule.__class__.__name__}")


def _check_bitmap(candidate: CandidateSpec, decoded: DecodedDict) -> CandidateCheck:
    fields = _data_fields(decoded)
    allowed = candidate.allowed_fields

    if allowed is None:
        supported = _supported_fields(candidate.spec)
        unsupported = sorted(set(fields).difference(supported))
        if unsupported:
            return CandidateCheck(
                "bitmap",
                False,
                f"fields outside specification range: {_format_fields(unsupported)}",
            )
        if fields:
            lower = min(fields)
            upper = max(fields)
            span = f"{lower}-{upper}" if lower != upper else str(lower)
        else:
            span = "(none)"
        return CandidateCheck(
            "bitmap",
            True,
            f"fields {_format_fields(fields)} within spec range {span}",
        )

    numeric_allowed = {number for number in allowed if isinstance(number, int)}
    outside = sorted(field for field in fields if field not in numeric_allowed)
    if outside:
        return CandidateCheck(
            "bitmap",
            False,
            "fields outside allowed bitmap range: "
            f"{_format_fields(outside)}; allowed {_format_collection(numeric_allowed)}",
        )

    return CandidateCheck(
        "bitmap",
        True,
        f"fields {_format_fields(fields)} within allowed range "
        f"{_format_collection(numeric_allowed)}",
    )


def _check_fields(candidate: CandidateSpec, decoded: DecodedDict) -> CandidateCheck:
    present = set(decoded.keys())
    failures: List[str] = []

    missing = sorted(set(candidate.required_fields).difference(present))
    if missing:
        failures.append(f"required fields missing: {', '.join(missing)}")

    forbidden = sorted(set(candidate.forbidden_fields).intersection(present))
    if forbidden:
        failures.append(f"forbidden fields present: {', '.join(forbidden)}")

    for field_key, rule in sorted(candidate.validators.items()):
        if field_key not in decoded:
            continue
        if not _field_value_matches(decoded[field_key], rule):
            failures.append(f"field {field_key} failed validation")

    if failures:
        return CandidateCheck("fields", False, "; ".join(failures))

    return CandidateCheck("fields", True, "field validation passed")


def _field_value_matches(value: str, rule: FieldRule) -> bool:
    if isinstance(rule, re.Pattern):
        return rule.fullmatch(value) is not None

    if isinstance(rule, str):
        return value == rule

    if callable(rule):
        try:
            return bool(rule(value))
        except Exception:
            return False

    if isinstance(rule, CollectionABC) and not isinstance(rule, Mapping):
        return value in typing_cast(Collection[Any], rule)

    return value == typing_cast(str, rule)


def _check_consumption(
    candidate: CandidateSpec,
    decoded: DecodedDict,
    consumed: int,
    remaining: bytes,
) -> CandidateCheck:
    if remaining:
        return CandidateCheck(
            "consumption",
            False,
            f"{len(remaining)} trailing bytes after decoded fields: "
            f"{remaining[:16].hex().upper()}"
            f"{'...' if len(remaining) > 16 else ''}",
        )

    return CandidateCheck(
        "consumption",
        True,
        f"all {consumed} bytes consumed",
    )


def _select(candidates: Sequence[CandidateResult]) -> Optional[CandidateResult]:
    eligible = [candidate for candidate in candidates if candidate.eligible]
    if len(eligible) == 1:
        return eligible[0]
    if not eligible:
        return None

    highest_priority = max(candidate.priority for candidate in eligible)
    top = [
        candidate for candidate in eligible if candidate.priority == highest_priority
    ]
    if len(top) == 1:
        return top[0]
    return None


def _compare(
    candidates: Sequence[CandidateResult],
) -> Tuple[Dict[str, str], Tuple[FieldDivergence, ...]]:
    if not candidates:
        return {}, ()

    keys: List[str] = []
    seen: Set[str] = set()
    for candidate in candidates:
        assert candidate.decoded is not None
        for key in candidate.decoded:
            if key not in seen:
                seen.add(key)
                keys.append(key)

    common: Dict[str, str] = {}
    divergences: List[FieldDivergence] = []

    for key in sorted(keys, key=_field_sort_key):
        values: Dict[str, Optional[str]] = {}
        meanings: Dict[str, Optional[str]] = {}
        descriptions: Dict[str, Optional[str]] = {}
        missing = False

        for candidate in candidates:
            assert candidate.decoded is not None
            value = candidate.decoded.get(key)
            values[candidate.name] = value
            descriptions[candidate.name] = _field_description(candidate.spec, key)
            meanings[candidate.name] = _field_meaning(candidate.spec, key)
            if key not in candidate.decoded:
                missing = True

        if missing:
            kind = "presence"
        elif len(set(meanings.values())) > 1:
            kind = "meaning"
        elif len(set(values.values())) > 1:
            kind = "value"
        else:
            common[key] = values[candidates[0].name] or ""
            continue

        divergences.append(
            FieldDivergence(
                field=key,
                kind=kind,
                values=values,
                meanings=meanings,
                descriptions=descriptions,
            )
        )

    return common, tuple(divergences)


def _data_fields(decoded: DecodedDict) -> List[int]:
    return sorted(
        int(key)
        for key in decoded
        if key.isdigit() and key not in _STRUCTURAL_KEYS
    )


def _supported_fields(spec: SpecDict) -> Set[int]:
    return {
        int(key)
        for key in spec
        if key.isdigit() and key not in _STRUCTURAL_KEYS
    }


def _field_description(spec: SpecDict, key: str) -> Optional[str]:
    field_spec = spec.get(key)
    if not isinstance(field_spec, Mapping):
        return None
    description = field_spec.get("desc")
    return str(description) if description is not None else None


def _field_meaning(spec: SpecDict, key: str) -> Optional[str]:
    field_spec = spec.get(key)
    if not isinstance(field_spec, Mapping):
        return None

    meaning_keys = (
        "data_enc",
        "len_enc",
        "len_type",
        "max_len",
        "len_count",
        "left_pad",
        "right_pad",
        "desc",
    )
    return "|".join(
        f"{name}={field_spec.get(name, '')}" for name in meaning_keys
    )


def _field_sort_key(key: str) -> Tuple[int, Any]:
    structural_order = {"h": 0, "t": 1, "p": 2, "1": 3}
    if key in structural_order:
        return structural_order[key], key
    if key.isdigit():
        return 10, int(key)
    return 20, key


def _format_fields(fields: Collection[int]) -> str:
    if not fields:
        return "(none)"
    values = sorted(fields)
    if values == list(range(values[0], values[-1] + 1)):
        return f"{values[0]}-{values[-1]}" if len(values) > 1 else str(values[0])
    return ", ".join(str(value) for value in values)


def _format_collection(values: Collection[int]) -> str:
    if not values:
        return "(none)"
    return _format_fields(values)
