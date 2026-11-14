r"""Parse partner-defined subitems inside an ISO8583 private field.

Field 48 (Additional Data - Private) often contains a partner-specific
sequence of subitems.  A partner schema may declare flat values, repeated
values, and groups nested inside other groups.  This module turns the raw
field-48 value into a flat table of :class:`Subitem` rows while preserving
exact links back to the original message.

Schema format
-------------
A schema is a JSON-compatible Python dictionary::

    {
        "version": "acquirer-2026.1",
        "children": [
            {
                "name": "01",
                "type": "data",
                "len_type": 3,
                "len_enc": "ascii",
                "max_len": 999,
                "data_enc": "ascii",
            },
            {
                "name": "02",
                "type": "group",
                "len_type": 3,
                "len_enc": "ascii",
                "max_len": 999,
                "repeat": 2,
                "children": [...],
            },
        ],
    }

Node properties
~~~~~~~~~~~~~~~
- **name** (or **id**) - subitem identifier.  It is used as the path
  component.  Names must be unique among siblings.
- **type** - ``"data"`` (default) or ``"group"``.
- **len_type** - width of the length prefix in bytes.  ``0`` means the node
  has no length prefix; a data node then uses **max_len** as its fixed
  length, while a group with no **max_len** consumes the rest of its parent.
- **len_enc** - length prefix encoding: ``"ascii"`` (default), ``"b"`` for
  binary, ``"bcd"``, or any Python text codec.
- **len_count** - ``"bytes"`` (default) or ``"nibbles"``.  It applies to
  **max_len** and to the declared value length.
- **max_len** - maximum declared length.  It is required when **len_type**
  is non-zero.  For fixed nodes it is the exact length.
- **data_enc** - value encoding for data nodes.  ``"b"``/``"bcd"`` values
  are represented as upper-case hex strings, like :mod:`iso8583` fields.
- **repeat** - number of occurrences, or ``"until_end"`` to consume the
  remaining bytes of the parent.  Defaults to ``1``.
- **left_pad** / **right_pad** - optional pad nibble used when an odd number
  of nibbles is encoded as binary data.
- **children** - child nodes for a group.

Boundaries
----------
Every node is parsed inside an explicit parent limit.  A node can never read
past that limit.  If a declared length claims more data than the parent can
provide, the node is emitted with ``truncated=True`` and parsing stops at the
tail of that parent.  When the parent is field 48 itself, this means the
subitem tail is marked incomplete and no byte from the following ISO field is
consumed.
"""

import codecs
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
    overload,
)

__all__ = [
    "Subitem",
    "SubitemTable",
    "SubitemError",
    "SubitemSchemaError",
    "parse_subitems",
    "decode_subitems",
]

SchemaDict = Mapping[str, Any]


class SubitemError(ValueError):
    r"""Subclass of ValueError that describes a subitem parsing error.

    Attributes
    ----------
    msg : str
        The unformatted error message
    path : tuple of str
        Path of the subitem being parsed
    offset : int or None
        Absolute offset in the ISO8583 message, if known
    """

    def __init__(
        self,
        msg: str,
        path: Tuple[str, ...] = (),
        offset: Optional[int] = None,
    ):
        details = []
        if path:
            details.append("path {}".format("/".join(path)))
        if offset is not None:
            details.append("offset {}".format(offset))
        if details:
            msg = "{} ({})".format(msg, ", ".join(details))
        ValueError.__init__(self, msg)
        self.msg = msg
        self.path = path
        self.offset = offset


class SubitemSchemaError(SubitemError):
    r"""Subclass of SubitemError that describes an invalid schema."""


@dataclass(frozen=True)
class Subitem:
    r"""One row in a :class:`SubitemTable`.

    Offsets are absolute positions in the original ISO8583 message and use
    half-open byte ranges.  ``start``/``end`` cover the complete subitem,
    including its length prefix.  ``value_start``/``value_end`` cover only
    the value (or, for a group, its content).  For nibble-counted values,
    ``nibble_start``/``nibble_end`` provide nibble-granular coordinates.
    """

    path: Tuple[str, ...]
    name: str
    kind: str
    occurrence: int
    occurrences: Tuple[int, ...]
    path_repeatable: Tuple[bool, ...]
    sequence: int
    repeatable: bool
    start: int
    end: int
    length_start: int
    length_end: int
    value_start: int
    value_end: int
    declared_length: Optional[int]
    length_unit: str
    value_units: int
    pad_removed: Optional[str]
    value: Optional[str]
    raw: bytes
    value_raw: bytes
    truncated: bool
    truncation_reason: Optional[str]
    version: str

    @property
    def path_str(self) -> str:
        r"""Slash-separated path with indexes for repeated components."""
        parts = []
        for name, occurrence, repeatable in zip(
            self.path, self.occurrences, self.path_repeatable
        ):
            if repeatable:
                parts.append("{}[{}]".format(name, occurrence))
            else:
                parts.append(name)
        return "/".join(parts)

    @property
    def parent_path(self) -> Tuple[str, ...]:
        r"""Path components excluding this subitem."""
        return self.path[:-1]

    @property
    def depth(self) -> int:
        r"""Nesting depth below the containing ISO8583 field."""
        return len(self.path) - 1

    @property
    def nibble_start(self) -> Optional[int]:
        r"""Absolute start measured in nibbles, when the length unit is nibbles.

        A removed left pad nibble is not part of the value and is excluded.
        """
        if self.length_unit == "nibbles":
            return self.value_start * 2 + (1 if self.pad_removed == "left" else 0)
        return None

    @property
    def nibble_end(self) -> Optional[int]:
        r"""Absolute end measured in nibbles, when the length unit is nibbles.

        The range excludes any pad nibble used to fill the last byte.  When
        the row is truncated it ends at the last nibble actually retained.
        """
        if self.length_unit == "nibbles":
            available = self.value_units
            if self.declared_length is None:
                return self.nibble_start + available
            return self.nibble_start + min(self.declared_length, available)
        return None

    def as_dict(self) -> Dict[str, Any]:
        r"""Return a JSON-compatible representation of this row."""
        return {
            "path": self.path_str,
            "components": [
                {"name": name, "occurrence": occurrence}
                for name, occurrence in zip(self.path, self.occurrences)
            ],
            "name": self.name,
            "kind": self.kind,
            "occurrence": self.occurrence,
            "sequence": self.sequence,
            "repeatable": self.repeatable,
            "depth": self.depth,
            "start": self.start,
            "end": self.end,
            "length_start": self.length_start,
            "length_end": self.length_end,
            "value_start": self.value_start,
            "value_end": self.value_end,
            "nibble_start": self.nibble_start,
            "nibble_end": self.nibble_end,
            "declared_length": self.declared_length,
            "length_unit": self.length_unit,
            "value_units": self.value_units,
            "pad_removed": self.pad_removed,
            "value": self.value,
            "raw_hex": self.raw.hex().upper(),
            "value_hex": self.value_raw.hex().upper(),
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "version": self.version,
        }


@dataclass(frozen=True)
class SubitemTable:
    r"""A linkable table of subitems parsed from one ISO8583 field."""

    field: str
    version: str
    schema: SchemaDict
    source: bytes
    base_offset: int
    rows: Tuple[Subitem, ...]

    def __iter__(self) -> Iterator[Subitem]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    @overload
    def __getitem__(self, index: int) -> Subitem:
        ...

    @overload
    def __getitem__(self, index: slice) -> Tuple[Subitem, ...]:
        ...

    def __getitem__(
        self, index: Union[int, slice]
    ) -> Union[Subitem, Tuple[Subitem, ...]]:
        return self.rows[index]

    @property
    def truncated(self) -> bool:
        r"""True if any row is incomplete."""
        return any(row.truncated for row in self.rows)

    @property
    def complete(self) -> bool:
        r"""True if all rows were parsed within their declared bounds."""
        return not self.truncated

    @property
    def truncated_rows(self) -> Tuple[Subitem, ...]:
        r"""Rows that are marked incomplete."""
        return tuple(row for row in self.rows if row.truncated)

    def find(
        self,
        path: Union[str, Tuple[str, ...]],
        occurrence: Optional[int] = None,
    ) -> Optional[Subitem]:
        r"""Return the first matching row, or None.

        A path may include occurrence indexes for repeated components,
        for example ``"48/02[2]/A"``.  Without indexes the first matching
        occurrence is returned.
        """
        matches = self.find_all(path, occurrence)
        return matches[0] if matches else None

    def find_all(
        self,
        path: Union[str, Tuple[str, ...]],
        occurrence: Optional[int] = None,
    ) -> List[Subitem]:
        r"""Return all rows matching a path and optional occurrence."""
        pattern = _path_pattern(path)
        rows = []
        for row in self.rows:
            if _path_matches(row, pattern) and (
                occurrence is None or row.occurrence == occurrence
            ):
                rows.append(row)
        return rows

    def link(self, row: Union[int, Subitem]) -> bytes:
        r"""Return the original bytes covered by a row."""
        if isinstance(row, int):
            row = self.rows[row]
        return self.source[row.start - self.base_offset : row.end - self.base_offset]

    def to_dicts(self) -> List[Dict[str, Any]]:
        r"""Return the table as a list of JSON-compatible dictionaries."""
        return [row.as_dict() for row in self.rows]


def parse_subitems(
    data: Union[bytes, bytearray],
    schema: SchemaDict,
    *,
    base_offset: int = 0,
    field: str = "48",
    strict: bool = False,
) -> SubitemTable:
    r"""Parse a field-48 value into a linkable subitem table.

    Parameters
    ----------
    data : bytes or bytearray
        Raw value of field 48, without its ISO8583 length prefix.
    schema : dict
        Partner schema.  See the module documentation for the format.
    base_offset : int, optional
        Absolute offset of `data` in the original ISO8583 message (default 0).
        All row offsets are reported relative to the message, not to `data`.
    field : str, optional
        Field name used as the first path component (default ``"48"``).
    strict : bool, optional
        When true, malformed length prefixes and unknown encodings raise
        :class:`SubitemError` instead of producing truncated rows.

    Returns
    -------
    SubitemTable
        Rows in document (preorder) order.

    Raises
    ------
    TypeError
        `data` must be bytes or bytearray.
    SubitemSchemaError
        The schema is invalid.
    SubitemError
        Parsing failed in strict mode.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            "Subitem data must be bytes or bytearray, not {}".format(
                data.__class__.__name__
            )
        )
    if not isinstance(base_offset, int) or base_offset < 0:
        raise ValueError("base_offset must be a non-negative integer")

    _validate_schema(schema)

    source = bytes(data)
    parser = _Parser(
        source=source,
        base_offset=base_offset,
        field=field,
        version=str(schema["version"]),
        strict=strict,
    )
    parser.parse_children(
        schema["children"],
        0,
        len(source),
        len(source) * 2,
        (field,),
        (1,),
        (False,),
        "field_boundary",
    )
    return SubitemTable(
        field=field,
        version=str(schema["version"]),
        schema=schema,
        source=source,
        base_offset=base_offset,
        rows=tuple(parser.rows),
    )


def decode_subitems(
    data: Union[bytes, bytearray],
    spec: Mapping[str, Mapping[str, Any]],
    schema: Optional[SchemaDict] = None,
    field: str = "48",
    *,
    strict: bool = False,
) -> SubitemTable:
    r"""Decode a complete ISO8583 message and return its subitem table.

    The message is decoded with the regular ISO8583 specification first, so
    every following field keeps its normal meaning and offset.  If `schema`
    is omitted, ``spec[field]["subitems"]`` is used.
    """
    from iso8583.decoder import decode

    _, doc_enc = decode(
        data,
        spec,
        subitem_schema=schema,
        subitem_field=field,
        subitem_strict=strict,
    )
    table = doc_enc[field].get("subitems")
    if table is None:
        raise SubitemError(
            "No subitem schema configured for field {}".format(field),
            (field,),
        )
    return table


#
# Schema validation
#


def _node_name(node: SchemaDict) -> str:
    name = node.get("name", node.get("id"))
    return str(name) if name is not None else ""


def _node_kind(node: SchemaDict) -> str:
    return str(node.get("type", node.get("kind", "data")))


def _node_unit(node: SchemaDict) -> str:
    return str(node.get("len_count", "bytes"))


def _node_repeat(node: SchemaDict) -> Union[int, str]:
    return node.get("repeat", 1)


def _validate_schema(schema: SchemaDict) -> None:
    if not isinstance(schema, Mapping):
        raise SubitemSchemaError("Schema must be a mapping")

    version = schema.get("version")
    if not isinstance(version, str) or not version:
        raise SubitemSchemaError("Schema must define a non-empty 'version'")

    children = schema.get("children")
    if not isinstance(children, (list, tuple)):
        raise SubitemSchemaError("Schema must define a 'children' list")

    _validate_children(children, ("schema",))


def _validate_children(children: Any, path: Tuple[str, ...]) -> None:
    if not isinstance(children, (list, tuple)):
        raise SubitemSchemaError("Group must define a 'children' list", path)

    names = []
    for node in children:
        if not isinstance(node, Mapping):
            raise SubitemSchemaError("Every schema node must be a mapping", path)

        name = _node_name(node)
        if not name:
            raise SubitemSchemaError("Every schema node must have a name", path)
        names.append(name)
        node_path = path + (name,)

        kind = _node_kind(node)
        if kind not in ("data", "group"):
            raise SubitemSchemaError(
                "Node type must be 'data' or 'group', not {!r}".format(kind),
                node_path,
            )

        len_type = node.get("len_type", 0)
        if not isinstance(len_type, int) or len_type < 0:
            raise SubitemSchemaError(
                "len_type must be a non-negative integer", node_path
            )

        unit = _node_unit(node)
        if unit not in ("bytes", "nibbles"):
            raise SubitemSchemaError(
                "len_count must be 'bytes' or 'nibbles'", node_path
            )

        repeat = _node_repeat(node)
        if repeat != "until_end" and (
            not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1
        ):
            raise SubitemSchemaError(
                "repeat must be a positive integer or 'until_end'", node_path
            )

        max_len = node.get("max_len")
        if max_len is not None and (
            not isinstance(max_len, int)
            or isinstance(max_len, bool)
            or max_len < 0
        ):
            raise SubitemSchemaError(
                "max_len must be a non-negative integer", node_path
            )

        if len_type > 0 and max_len is None:
            raise SubitemSchemaError(
                "max_len is required for variable-length nodes", node_path
            )
        if kind == "data" and len_type == 0 and max_len is None:
            raise SubitemSchemaError(
                "Fixed-length data nodes must define max_len", node_path
            )

        if kind == "group" and len_type == 0 and max_len is None and repeat != 1:
            raise SubitemSchemaError(
                "A group without a declared length can only occur once", node_path
            )

        len_enc = str(node.get("len_enc", "ascii"))
        if not len_enc:
            raise SubitemSchemaError("len_enc cannot be empty", node_path)

        if kind == "data":
            data_enc = str(node.get("data_enc", "ascii"))
            if not data_enc:
                raise SubitemSchemaError("data_enc cannot be empty", node_path)
            if "children" in node and node["children"]:
                raise SubitemSchemaError(
                    "Data nodes cannot have children", node_path
                )
        else:
            if "children" not in node:
                raise SubitemSchemaError(
                    "Group nodes must define children", node_path
                )
            _validate_children(node["children"], node_path)

    if len(set(names)) != len(names):
        raise SubitemSchemaError(
            "Sibling subitem names must be unique", path
        )


def _validate_codec(value: str, prop: str, path: Tuple[str, ...]) -> None:
    if value in ("b", "bcd"):
        return
    try:
        codecs.lookup(value)
    except LookupError:
        raise SubitemError(
            "{} specifies an unknown encoding {!r}".format(prop, value),
            path,
        ) from None


#
# Parser
#


class _Parser:
    def __init__(
        self,
        source: bytes,
        base_offset: int,
        field: str,
        version: str,
        strict: bool,
    ):
        self.source = source
        self.base_offset = base_offset
        self.field = field
        self.version = version
        self.strict = strict
        self.rows: List[Subitem] = []
        self._sequence = 0

    def absolute(self, pos: int) -> int:
        return self.base_offset + pos

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def emit(
        self,
        node: SchemaDict,
        path: Tuple[str, ...],
        occurrence: int,
        occurrences: Tuple[int, ...],
        path_repeatable: Tuple[bool, ...],
        start: int,
        end: int,
        length_start: int,
        length_end: int,
        value_start: int,
        value_end: int,
        declared_length: Optional[int],
        value_units: int,
        value: Optional[str],
        truncated: bool,
        truncation_reason: Optional[str],
        pad_removed: Optional[str] = None,
    ) -> None:
        start = min(max(start, 0), len(self.source))
        end = min(max(end, start), len(self.source))
        length_start = min(max(length_start, start), end)
        length_end = min(max(length_end, length_start), end)
        value_start = min(max(value_start, length_end), end)
        value_end = min(max(value_end, value_start), end)

        self.rows.append(
            Subitem(
                path=path,
                name=path[-1],
                kind=_node_kind(node),
                occurrence=occurrence,
                occurrences=occurrences,
                path_repeatable=path_repeatable,
                sequence=self.next_sequence(),
                repeatable=path_repeatable[-1],
                start=self.absolute(start),
                end=self.absolute(end),
                length_start=self.absolute(length_start),
                length_end=self.absolute(length_end),
                value_start=self.absolute(value_start),
                value_end=self.absolute(value_end),
                declared_length=declared_length,
                length_unit=_node_unit(node),
                value_units=value_units,
                pad_removed=pad_removed,
                value=value,
                raw=self.source[start:end],
                value_raw=self.source[value_start:value_end],
                truncated=truncated,
                truncation_reason=truncation_reason,
                version=self.version,
            )
        )

    def parse_children(
        self,
        children: Any,
        cursor: int,
        limit: int,
        limit_nibbles: int,
        parent_path: Tuple[str, ...],
        parent_occurrences: Tuple[int, ...],
        parent_repeatable: Tuple[bool, ...],
        boundary_reason: str,
    ) -> Tuple[int, bool]:
        for node in children:
            repeat = _node_repeat(node)
            repeatable = repeat == "until_end" or (
                isinstance(repeat, int) and repeat > 1
            )

            if repeat == "until_end":
                occurrence = 1
                while cursor < limit:
                    before = cursor
                    cursor, stopped = self.parse_node(
                        node,
                        cursor,
                        limit,
                        limit_nibbles,
                        parent_path,
                        parent_occurrences,
                        parent_repeatable,
                        occurrence,
                        repeatable,
                        boundary_reason,
                    )
                    occurrence += 1
                    if stopped or cursor <= before:
                        return cursor, stopped or cursor < limit
                return cursor, False

            for occurrence in range(1, int(repeat) + 1):
                if cursor >= limit:
                    self.emit_missing(
                        node,
                        parent_path + (_node_name(node),),
                        parent_occurrences + (occurrence,),
                        parent_repeatable + (repeatable,),
                        cursor,
                        boundary_reason,
                    )
                    return limit, True

                cursor, stopped = self.parse_node(
                    node,
                    cursor,
                    limit,
                    limit_nibbles,
                    parent_path,
                    parent_occurrences,
                    parent_repeatable,
                    occurrence,
                    repeatable,
                    boundary_reason,
                )
                if stopped:
                    return cursor, True

        return cursor, False

    def emit_missing(
        self,
        node: SchemaDict,
        path: Tuple[str, ...],
        occurrences: Tuple[int, ...],
        path_repeatable: Tuple[bool, ...],
        cursor: int,
        reason: str,
    ) -> None:
        self.emit(
            node=node,
            path=path,
            occurrence=occurrences[-1],
            occurrences=occurrences,
            path_repeatable=path_repeatable,
            start=cursor,
            end=cursor,
            length_start=cursor,
            length_end=cursor,
            value_start=cursor,
            value_end=cursor,
            declared_length=None,
            value_units=0,
            value=None,
            truncated=True,
            truncation_reason=reason,
        )

    def parse_node(
        self,
        node: SchemaDict,
        cursor: int,
        limit: int,
        limit_nibbles: int,
        parent_path: Tuple[str, ...],
        parent_occurrences: Tuple[int, ...],
        parent_repeatable: Tuple[bool, ...],
        occurrence: int,
        repeatable: bool,
        boundary_reason: str,
    ) -> Tuple[int, bool]:
        path = parent_path + (_node_name(node),)
        occurrences = parent_occurrences + (occurrence,)
        path_repeatable = parent_repeatable + (repeatable,)
        kind = _node_kind(node)
        unit = _node_unit(node)
        len_type = int(node.get("len_type", 0))
        max_len = node.get("max_len")
        start = cursor

        if len_type:
            length_start = cursor
            length_end = cursor + len_type
            if length_end > limit:
                self.emit(
                    node=node,
                    path=path,
                    occurrence=occurrence,
                    occurrences=occurrences,
                    path_repeatable=path_repeatable,
                    start=start,
                    end=limit,
                    length_start=length_start,
                    length_end=limit,
                    value_start=limit,
                    value_end=limit,
                    declared_length=None,
                    value_units=0,
                    value=None,
                    truncated=True,
                    truncation_reason="length_prefix",
                )
                return limit, True

            try:
                declared = _decode_length(
                    self.source[length_start:length_end], node, path
                )
            except SubitemError:
                if self.strict:
                    raise
                self.emit(
                    node=node,
                    path=path,
                    occurrence=occurrence,
                    occurrences=occurrences,
                    path_repeatable=path_repeatable,
                    start=start,
                    end=limit,
                    length_start=length_start,
                    length_end=length_end,
                    value_start=length_end,
                    value_end=limit,
                    declared_length=None,
                    value_units=0,
                    value=None,
                    truncated=True,
                    truncation_reason="length_prefix",
                )
                return limit, True
            content_start = length_end
        else:
            declared = max_len
            length_start = cursor
            length_end = cursor
            content_start = cursor

        if len_type == 0 and max_len is None:
            effective_units = _available_units(
                content_start, limit, limit_nibbles, unit
            )
            declared_units = effective_units
            content_end = limit
            truncated = False
            stop = False
            reason = None
        else:
            declared_units = int(declared if declared is not None else 0)
            truncated = False
            reason = None

            # A declared length above the schema maximum is a schema
            # violation.  Still consume the declared bytes when they are
            # present so that later siblings keep their positions.
            if max_len is not None and declared_units > max_len:
                truncated = True
                reason = "max_length"

            effective_units = declared_units
            available_units = _available_units(
                content_start, limit, limit_nibbles, unit
            )
            if effective_units > available_units:
                truncated = True
                reason = boundary_reason
                effective_units = available_units

            # Only an inability to consume the declared length stops the
            # surrounding sequence.  A pure max_len violation does not.
            stop = effective_units < declared_units
            content_end = content_start + _nibbles_to_bytes(effective_units, unit)

        value_raw = self.source[content_start:content_end]

        if kind == "data":
            value, pad_removed = _decode_value(
                value_raw,
                node,
                effective_units,
                unit,
                strict=self.strict,
                path=path,
                offset=self.absolute(content_start),
            )
            self.emit(
                node=node,
                path=path,
                occurrence=occurrence,
                occurrences=occurrences,
                path_repeatable=path_repeatable,
                start=start,
                end=content_end,
                length_start=length_start,
                length_end=length_end,
                value_start=content_start,
                value_end=content_end,
                declared_length=declared_units,
                value_units=effective_units,
                value=value,
                truncated=truncated,
                truncation_reason=reason,
                pad_removed=pad_removed,
            )
        else:
            self.emit(
                node=node,
                path=path,
                occurrence=occurrence,
                occurrences=occurrences,
                path_repeatable=path_repeatable,
                start=start,
                end=content_end,
                length_start=length_start,
                length_end=length_end,
                value_start=content_start,
                value_end=content_end,
                declared_length=declared_units,
                value_units=effective_units,
                value=None,
                truncated=truncated,
                truncation_reason=reason,
            )
            self.parse_children(
                node["children"],
                content_start,
                content_end,
                _child_limit_nibbles(content_start, effective_units, unit),
                path,
                occurrences,
                path_repeatable,
                "parent_boundary",
            )

        return content_end, stop


def _path_pattern(
    path: Union[str, Tuple[str, ...]]
) -> Tuple[Tuple[str, Optional[int]], ...]:
    if not isinstance(path, str):
        path = "/".join(path)

    pattern = []
    for part in path.strip("/").split("/"):
        if not part:
            continue
        occurrence = None
        if part.endswith("]") and "[" in part:
            name, _, index = part[:-1].partition("[")
            if index.isdigit():
                occurrence = int(index)
                part = name
        pattern.append((part, occurrence))
    return tuple(pattern)


def _path_matches(
    row: Subitem, pattern: Tuple[Tuple[str, Optional[int]], ...]
) -> bool:
    if len(row.path) != len(pattern):
        return False
    for name, occurrence, expected in zip(
        row.path, row.occurrences, pattern
    ):
        if name != expected[0]:
            return False
        if expected[1] is not None and occurrence != expected[1]:
            return False
    return True


def _available_units(
    start: int, limit: int, limit_nibbles: int, unit: str
) -> int:
    if unit == "nibbles":
        return max(limit_nibbles - start * 2, 0)
    return max(limit - start, 0)


def _child_limit_nibbles(start: int, units: int, unit: str) -> int:
    return start * 2 + units


def _nibbles_to_bytes(value: int, unit: str) -> int:
    if unit == "nibbles":
        return (value + 1) // 2
    return value


def _decode_length(
    raw: bytes,
    node: SchemaDict,
    path: Tuple[str, ...],
) -> int:
    encoding = str(node.get("len_enc", "ascii"))
    if encoding not in ("b", "bcd"):
        _validate_codec(encoding, "len_enc", path)
    try:
        if encoding == "b":
            value = int.from_bytes(raw, "big", signed=False)
        elif encoding == "bcd":
            digits = raw.hex()
            if any(char not in "0123456789" for char in digits):
                raise ValueError("non-BCD length")
            value = int(digits, 10)
        else:
            text = raw.decode(encoding)
            if not text.isdigit():
                raise ValueError("non-numeric length")
            value = int(text)
    except SubitemError:
        raise
    except Exception:
        raise SubitemError("Failed to decode subitem length prefix", path) from None

    if value < 0:
        raise SubitemError("Subitem length cannot be negative", path)
    return value


def _decode_value(
    raw: bytes,
    node: SchemaDict,
    value_units: int,
    unit: str,
    strict: bool,
    path: Tuple[str, ...],
    offset: int,
) -> Tuple[Optional[str], Optional[str]]:
    encoding = str(node.get("data_enc", "ascii"))
    if encoding in ("b", "bcd"):
        value = raw.hex().upper()
        pad_removed: Optional[str] = None
        if unit == "nibbles" and value_units % 2 == 1:
            left_pad = str(node.get("left_pad", ""))[:1]
            right_pad = str(node.get("right_pad", ""))[:1]
            if left_pad and value.startswith(left_pad):
                value = value[1:]
                pad_removed = "left"
            elif right_pad and value.endswith(right_pad):
                value = value[:-1]
                pad_removed = "right"
            else:
                # The last nibble only fills a byte and is not part of the
                # declared value.
                value = value[:-1]
                pad_removed = "storage"
        return value, pad_removed

    try:
        return raw.decode(encoding), None
    except LookupError:
        if strict:
            raise SubitemError(
                "Unknown data encoding {!r}".format(encoding), path, offset
            ) from None
        return None, None
    except Exception:
        if strict:
            raise SubitemError(
                "Failed to decode subitem value", path, offset
            ) from None
        return raw.decode(encoding, errors="replace"), None
