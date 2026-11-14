r"""Build a catalog of fields declared by an ISO8583 message.

The regular :func:`iso8583.decode` function returns field values, but not
where those values came from. Reconciliation and sample-analysis tools often
need the opposite view: for every field that the bitmaps declare, record the
bitmap position that declared it and the byte range that carries its length
prefix and data.

:func:`iso8583.catalog` returns such a directory as a :class:`Catalog` instance.
It can be queried by ISO8583 field number (:meth:`Catalog.by_field`) or by the
business name from the specification (:meth:`Catalog.by_name`).

The catalog deliberately separates bitmap indicator bits from business fields:

- Each bitmap is described by a :class:`BitmapLayer` in
  :attr:`Catalog.bitmaps`.
- DE 1 and DE 65 are presence indicators for the secondary and tertiary bitmaps.
  They never appear in :attr:`Catalog.fields`, so they cannot be mistaken for
  amounts or other business data elements.
- Fields 2-64, 66-128, and 130-192 appear in :attr:`Catalog.fields`.

A field that is declared with zero length is present with an empty
:class:`DataRange`. A field whose bit is not set is simply absent. The two cases
are therefore distinguishable.

When the final bitmap layer is shorter than expected, the catalog keeps the
declarations that the complete preceding layers already determined. The
incomplete layer consumes nothing and is not padded with the bytes that follow
it. Because the missing bitmap bytes leave the boundary between bitmap and data
unknown, the retained declarations have no byte ranges and the remaining bytes
stay in :attr:`Catalog.remaining`. Pass ``strict=True`` to reject a truncated
message instead.
"""

from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

__all__ = [
    "catalog",
    "field_catalog",
    "declared_fields",
    "Catalog",
    "CatalogError",
    "BitmapLayer",
    "BitmapPosition",
    "DataRange",
    "FieldDeclaration",
]

SpecDict = Mapping[str, Mapping[str, Any]]

_LAYER_NAMES = ("primary", "secondary", "tertiary")


class CatalogError(ValueError):
    r"""Subclass of ValueError that describes a field cataloging error.

    Attributes
    ----------
    msg : str
        The unformatted error message
    s : bytes or bytearray
        The ISO8583 bytes instance being parsed
    pos : int
        The start index where ISO8583 bytes data failed parsing
    field : str
        The ISO8583 field where parsing failed
    """

    def __init__(
        self,
        msg: str,
        s: Union[bytes, bytearray],
        pos: int,
        field: str,
    ):
        errmsg = f"{msg}: field {field} pos {pos}"
        ValueError.__init__(self, errmsg)
        self.msg = msg
        self.s = s
        self.pos = pos
        self.field = field


class DataRange(NamedTuple):
    r"""A half-open byte range ``[start, end)`` within the raw message.

    Attributes
    ----------
    start : int
        Offset of the first byte.
    end : int
        Offset after the last byte. Equal to ``start`` for an empty range.
    """

    start: int
    end: int

    @property
    def length(self) -> int:
        r"""Number of bytes in the range."""
        return self.end - self.start

    @property
    def is_empty(self) -> bool:
        r"""True when this range contains no bytes."""
        return self.start == self.end

    def __repr__(self) -> str:
        return f"DataRange({self.start}, {self.end})"


class BitmapPosition(NamedTuple):
    r"""Position of a declared field inside the ISO8583 bitmaps.

    Attributes
    ----------
    layer : int
        Bitmap layer: ``0`` primary, ``1`` secondary, ``2`` tertiary.
    layer_name : str
        Human-readable layer name: ``primary``, ``secondary``, or ``tertiary``.
    key : str
        Key of the bitmap in the decoded dictionary: ``p`` for the primary
        bitmap and ``1`` for the secondary/tertiary bitmap.
    de : int
        Global ISO8583 data element number, 1-192.
    byte : int
        Zero-based byte index inside this bitmap layer.
    bit : int
        One-based bit index inside ``byte``, 1-8.
    ordinal : int
        One-based bit index inside the layer, 1-64.
    mask : int
        Bit mask that selects this bit inside ``byte``.
    """

    layer: int
    layer_name: str
    key: str
    de: int
    byte: int
    bit: int
    ordinal: int
    mask: int

    def __repr__(self) -> str:
        return (
            f"BitmapPosition(layer={self.layer_name!r}, de={self.de}, "
            f"byte={self.byte}, bit={self.bit}, mask=0x{self.mask:02X})"
        )


class BitmapLayer(NamedTuple):
    r"""Description of one bitmap layer found in the message.

    Attributes
    ----------
    key : str
        Key of the bitmap in the decoded dictionary: ``p`` or ``1``.
    layer : int
        Bitmap layer: ``0`` primary, ``1`` secondary, ``2`` tertiary.
    layer_name : str
        Human-readable layer name.
    offset : int
        First global data element number covered by this layer (0, 64, 128).
    start : int
        Offset of the layer in the raw message.
    end : int
        Offset after the bytes consumed for this layer. An incomplete layer
        consumes nothing, so this equals ``start``.
    expected_length : int
        Number of bytes a complete layer occupies.
    available_length : int
        Number of bytes that were available at ``start`` when the layer was
        read.
    complete : bool
        True when all expected bytes were present and the layer was consumed.
    trigger_de : int or None
        Data element whose presence bit requires this layer: ``1`` for the
        secondary bitmap, ``65`` for the tertiary bitmap, ``None`` for the
        primary bitmap.
    enabled : tuple of int
        Global data element numbers whose bits are set. Empty for an
        incomplete layer.
    """

    key: str
    layer: int
    layer_name: str
    offset: int
    start: int
    end: int
    expected_length: int
    available_length: int
    complete: bool
    trigger_de: Optional[int]
    enabled: Tuple[int, ...]

    @property
    def truncated(self) -> bool:
        r"""True when the layer was shorter than expected."""
        return not self.complete

    @property
    def range(self) -> DataRange:
        r"""Byte range consumed by this layer."""
        return DataRange(self.start, self.end)

    @property
    def available_range(self) -> DataRange:
        r"""Bytes that were present for this layer, consumed or not."""
        return DataRange(self.start, self.start + self.available_length)


class FieldDeclaration(NamedTuple):
    r"""A field declared by a bitmap, with its position and byte ranges.

    Attributes
    ----------
    field : str
        ISO8583 field number as a string, for example ``"4"``.
    name : str
        Business name from the specification ``desc`` property. Empty when
        the field is not present in the specification.
    bitmap : BitmapPosition
        Bitmap position that declared this field.
    length_prefix : DataRange or None
        Byte range of the encoded field length. Empty for fixed-length fields.
        ``None`` when the bytes of this field cannot be located.
    data_range : DataRange or None
        Byte range of the field data. ``None`` when the bytes of this field
        cannot be located. A declared zero-length field has an empty range.
    declared_length : int or None
        Length declared by the length prefix, measured in ``length_count``
        units. For fixed-length fields this is ``max_len``. ``None`` when the
        length is unknown.
    length_count : str
        ``bytes`` or ``nibbles``.
    value : str or None
        Decoded value. ``None`` when the data is truncated or not located.
    truncated : bool
        True when the length prefix or the data extends past the end of the
        raw message.
    known : bool
        True when the field exists in the specification. Cataloging always
        reports declared fields; a field missing from the specification is
        reported without byte ranges rather than raising.
    """

    field: str
    name: str
    bitmap: BitmapPosition
    length_prefix: Optional[DataRange]
    data_range: Optional[DataRange]
    declared_length: Optional[int]
    length_count: str
    value: Optional[str]
    truncated: bool
    known: bool

    @property
    def de(self) -> int:
        r"""Global ISO8583 data element number."""
        return self.bitmap.de

    @property
    def located(self) -> bool:
        r"""True when the byte ranges of this field are known."""
        return self.length_prefix is not None

    @property
    def bitmap_position(self) -> BitmapPosition:
        r"""Alias for :attr:`bitmap`."""
        return self.bitmap

    @property
    def business_name(self) -> str:
        r"""Alias for :attr:`name`."""
        return self.name

    @property
    def is_empty(self) -> bool:
        r"""True when the field is declared but carries no data bytes."""
        return self.data_range is not None and self.data_range.is_empty

    @property
    def start(self) -> Optional[int]:
        r"""Offset of the first data byte, if known."""
        return self.data_range.start if self.data_range is not None else None

    @property
    def end(self) -> Optional[int]:
        r"""Offset after the last data byte, if known."""
        return self.data_range.end if self.data_range is not None else None


class Catalog(Mapping[str, FieldDeclaration]):
    r"""Directory of fields declared in an ISO8583 message.

    The catalog maps field numbers to :class:`FieldDeclaration` entries and
    supports normal mapping access::

        >>> import iso8583
        >>> from iso8583.specs import default_ascii as spec
        >>> found = iso8583.catalog(b"02004000000000000000041234", spec)
        >>> found["2"].value
        '1234'
        >>> found["2"].bitmap.de
        2
        >>> found["2"].data_range
        DataRange(22, 26)

    Use :meth:`by_name` to look up entries from the business domain, for
    example ``found.by_name("Amount, Transaction")``.
    """

    def __init__(
        self,
        fields: Tuple[FieldDeclaration, ...] = (),
        bitmaps: Tuple[BitmapLayer, ...] = (),
        size: int = 0,
        consumed: int = 0,
        truncated: bool = False,
    ):
        self._fields = fields
        self._bitmaps = bitmaps
        self._by_field: Dict[str, FieldDeclaration] = {
            entry.field: entry for entry in fields
        }
        self._by_name: Dict[str, List[FieldDeclaration]] = {}
        for entry in fields:
            if entry.name:
                self._by_name.setdefault(entry.name, []).append(entry)
        self.size = size
        self.consumed = consumed
        self._truncated = truncated

    # Mapping interface

    def __getitem__(self, field: Union[str, int]) -> FieldDeclaration:
        return self._by_field[str(field)]

    def __iter__(self) -> Iterator[str]:
        return (entry.field for entry in self._fields)

    def __len__(self) -> int:
        return len(self._fields)

    # Catalog interface

    @property
    def fields(self) -> Tuple[FieldDeclaration, ...]:
        r"""Declared business fields in data element order.

        DE 1 and DE 65 are bitmap presence indicators and never appear here.
        """
        return self._fields

    @property
    def declarations(self) -> Tuple[FieldDeclaration, ...]:
        r"""Alias for :attr:`fields`."""
        return self._fields

    @property
    def bitmaps(self) -> Tuple[BitmapLayer, ...]:
        r"""Bitmap layers, including layers that were too short to consume."""
        return self._bitmaps

    @property
    def remaining(self) -> DataRange:
        r"""Bytes that were not consumed by bitmaps or located fields."""
        return DataRange(self.consumed, self.size)

    @property
    def bitmaps_complete(self) -> bool:
        r"""True when every bitmap layer present was complete."""
        return all(layer.complete for layer in self._bitmaps)

    @property
    def located(self) -> bool:
        r"""True when every declared field has a known byte range."""
        return self.located_fields == len(self._fields)

    @property
    def located_fields(self) -> int:
        r"""Number of declared fields whose byte ranges are known."""
        return sum(1 for entry in self._fields if entry.located)

    @property
    def truncated(self) -> bool:
        r"""True when a bitmap layer or a declared field is incomplete."""
        return (
            self._truncated
            or not self.bitmaps_complete
            or any(entry.truncated for entry in self._fields)
        )

    @property
    def complete(self) -> bool:
        r"""True when bitmaps and declared fields exactly cover the message."""
        return (
            not self.truncated
            and self.located
            and self.consumed == self.size
        )

    def by_field(self, field: Union[str, int]) -> FieldDeclaration:
        r"""Return the declaration for an ISO8583 field number.

        Parameters
        ----------
        field : str or int
            ISO8583 field number, for example ``"4"`` or ``4``.

        Raises
        ------
        KeyError
            The field is not declared by a bitmap layer.
        """
        return self._by_field[str(field)]

    def by_name(self, name: str) -> Tuple[FieldDeclaration, ...]:
        r"""Return declarations matching a business name.

        Specification descriptions are not unique (many reserved fields share
        a name), so a tuple is returned. An unknown name returns an empty
        tuple.

        Parameters
        ----------
        name : str
            Business name from the specification ``desc`` property.
        """
        return tuple(self._by_name.get(name, ()))

    def bitmap_position(self, field: Union[str, int]) -> BitmapPosition:
        r"""Return the bitmap position that declared ``field``."""
        return self.by_field(field).bitmap

    def data_range(self, field: Union[str, int]) -> Optional[DataRange]:
        r"""Return the data byte range for ``field``, if it can be located."""
        return self.by_field(field).data_range

    def lookup(self, value: Union[str, int]) -> Tuple[FieldDeclaration, ...]:
        r"""Resolve a field number or a business name.

        A field number (``"4"`` or ``4``) returns a one-item tuple. Anything
        else is treated as a business name. An unknown value returns an empty
        tuple, so callers can probe both domains without handling KeyError.
        """
        if isinstance(value, int) or value.isdigit():
            try:
                return (self.by_field(value),)
            except KeyError:
                return ()
        return self.by_name(value)

    @property
    def names(self) -> Tuple[str, ...]:
        r"""Business names represented in this message, without duplicates."""
        return tuple(self._by_name)

    @property
    def by_name_index(self) -> Mapping[str, Tuple[FieldDeclaration, ...]]:
        r"""Read-only mapping of business name to declarations."""
        return {
            name: tuple(entries) for name, entries in self._by_name.items()
        }

    def as_dict(self) -> Dict[str, Any]:
        r"""Return a JSON-compatible representation of the catalog."""

        def _range(value: Optional[DataRange]) -> Optional[Dict[str, int]]:
            if value is None:
                return None
            return {"start": value.start, "end": value.end}

        return {
            "fields": [
                {
                    "field": entry.field,
                    "name": entry.name,
                    "bitmap": {
                        "layer": entry.bitmap.layer,
                        "layer_name": entry.bitmap.layer_name,
                        "key": entry.bitmap.key,
                        "de": entry.bitmap.de,
                        "byte": entry.bitmap.byte,
                        "bit": entry.bitmap.bit,
                        "ordinal": entry.bitmap.ordinal,
                        "mask": entry.bitmap.mask,
                    },
                    "length_prefix": _range(entry.length_prefix),
                    "data_range": _range(entry.data_range),
                    "declared_length": entry.declared_length,
                    "length_count": entry.length_count,
                    "value": entry.value,
                    "truncated": entry.truncated,
                    "known": entry.known,
                }
                for entry in self._fields
            ],
            "bitmaps": [
                {
                    "key": layer.key,
                    "layer": layer.layer,
                    "layer_name": layer.layer_name,
                    "offset": layer.offset,
                    "start": layer.start,
                    "end": layer.end,
                    "expected_length": layer.expected_length,
                    "available_length": layer.available_length,
                    "complete": layer.complete,
                    "trigger_de": layer.trigger_de,
                    "enabled": list(layer.enabled),
                }
                for layer in self._bitmaps
            ],
            "size": self.size,
            "consumed": self.consumed,
            "complete": self.complete,
            "truncated": self.truncated,
        }


def catalog(
    s: Union[bytes, bytearray],
    spec: SpecDict,
    strict: bool = False,
) -> Catalog:
    r"""Catalog every field declared by the bitmaps in an ISO8583 message.

    Parameters
    ----------
    s : bytes or bytearray
        Encoded ISO8583 data.
    spec : dict
        A Python dict defining ISO8583 specification.
        See :mod:`iso8583.specs` module for examples.
    strict : bool, optional
        When ``True`` a truncated message raises :class:`CatalogError`.
        When ``False`` (default) an incomplete final bitmap layer consumes
        nothing, declarations made by complete preceding layers are kept
        without byte ranges, and the remaining bytes stay unconsumed.

    Returns
    -------
    Catalog
        Directory of declared fields, bitmap layers, and byte ranges.

    Raises
    ------
    CatalogError
        A bitmap, length prefix, or field cannot be parsed, or ``strict``
        is requested for a truncated message.
    TypeError
        `s` must be a bytes or bytearray instance.

    Notes
    -----
    DE 1 and DE 65 are bitmap presence indicators. They are reported in
    :attr:`Catalog.bitmaps` and never appear in :attr:`Catalog.fields`.

    A declared zero-length field appears in the catalog with an empty
    :attr:`FieldDeclaration.data_range`. An undeclared field is absent.

    Examples
    --------
    >>> import iso8583
    >>> from iso8583.specs import default_ascii as spec
    >>> found = iso8583.catalog(b"02004000000000000000041234", spec)
    >>> found["2"].value
    '1234'
    >>> found["2"].bitmap.layer_name
    'primary'
    >>> found["2"].bitmap.de
    2
    >>> found["2"].data_range
    DataRange(22, 26)
    """

    if not isinstance(s, (bytes, bytearray)):
        raise TypeError(
            f"Encoded ISO8583 data must be bytes or bytearray, not {s.__class__.__name__}"
        )

    return _CatalogParser(s, spec, strict).parse()


field_catalog = catalog
declared_fields = catalog


class _Layout(NamedTuple):
    prefix_start: int
    prefix_end: int
    data_start: Optional[int]
    data_end: Optional[int]
    declared_length: Optional[int]
    length_count: str
    truncated: bool


class _CatalogParser:
    def __init__(
        self, s: Union[bytes, bytearray], spec: SpecDict, strict: bool
    ) -> None:
        self.s = s
        self.spec = spec
        self.strict = strict
        self.idx = 0

    def parse(self) -> Catalog:
        if not self._skip_header() or not self._skip_type():
            return Catalog(size=len(self.s), consumed=self.idx, truncated=True)

        primary = self._read_bitmap("p", 0, 0, None)
        layers: List[BitmapLayer] = [primary]

        if primary.complete and 1 in primary.enabled:
            secondary = self._read_bitmap("1", 1, 64, 1)
            layers.append(secondary)

            if secondary.complete and 65 in secondary.enabled:
                layers.append(self._read_bitmap("1", 2, 128, 65))

        declarations: List[Tuple[int, int, int]] = []
        for layer in layers:
            if not layer.complete:
                continue
            for de in layer.enabled:
                # DE 1 and DE 65 only announce the next bitmap layer.
                if de in (1, 65):
                    continue
                declarations.append((de, layer.layer, layer.offset))

        entries: List[FieldDeclaration] = []
        blocked = not all(layer.complete for layer in layers)
        for de, layer, offset in sorted(declarations):
            field = str(de)
            field_spec = self.spec.get(field)

            if field_spec is None:
                if self.strict:
                    raise CatalogError(
                        "Field is not defined in the specification",
                        self.s,
                        self.idx,
                        field,
                    )
                entries.append(_unknown_field(field, layer, offset))
                blocked = True
                continue

            if blocked:
                # A short bitmap layer leaves the boundary between bitmap
                # bytes and field data unknown. The declaration was made by a
                # complete earlier layer and is kept, but its byte range
                # cannot be determined and no trailing byte is consumed.
                entries.append(
                    _unlocated_field(field, field_spec, layer, offset)
                )
                continue

            entry = self._read_field(field, field_spec, layer, offset)
            entries.append(entry)
            blocked = entry.truncated

        return Catalog(
            fields=tuple(entries),
            bitmaps=tuple(layers),
            size=len(self.s),
            consumed=self.idx,
        )

    #
    # Header and message type
    #

    def _skip_header(self) -> bool:
        if self.spec["h"]["max_len"] <= 0:
            return True

        layout = self._layout("h", self.spec["h"])
        if layout.truncated or layout.data_end is None:
            if self.strict:
                raise CatalogError(
                    "Field data is truncated", self.s, self.idx, "h"
                )
            return False
        self.idx = layout.data_end
        return True

    def _skip_type(self) -> bool:
        expected = 2 if self.spec["t"]["data_enc"] == "b" else 4
        start = self.idx
        available = len(self.s) - start
        if available < expected:
            if self.strict:
                raise CatalogError(
                    f"Field data is {available} bytes, expecting {expected}",
                    self.s,
                    start,
                    "t",
                )
            return False
        self.idx = start + expected
        return True

    #
    # Bitmaps
    #

    def _read_bitmap(
        self,
        key: str,
        layer: int,
        offset: int,
        trigger_de: Optional[int],
    ) -> BitmapLayer:
        field_spec = self.spec[key]
        expected = 8 if field_spec["data_enc"] == "b" else 16
        start = self.idx
        available = len(self.s) - start

        if available < expected:
            if self.strict:
                raise CatalogError(
                    f"Field data is {available} bytes, expecting {expected}",
                    self.s,
                    start,
                    key,
                )
            # The layer is short of its expected length. The remaining
            # bytes cannot be split reliably into bitmap bits and the
            # business data that follows them, so none of them is consumed
            # and none of them is used to pad this layer. They stay in
            # Catalog.remaining.
            return BitmapLayer(
                key=key,
                layer=layer,
                layer_name=_LAYER_NAMES[layer],
                offset=offset,
                start=start,
                end=start,
                expected_length=expected,
                available_length=available,
                complete=False,
                trigger_de=trigger_de,
                enabled=(),
            )

        raw = bytes(self.s[start : start + expected])
        if field_spec["data_enc"] == "b":
            bitmap = raw
        else:
            try:
                decoded = raw.decode(field_spec["data_enc"])
            except LookupError:
                raise CatalogError(
                    "Failed to decode field, unknown encoding specified",
                    self.s,
                    start,
                    key,
                ) from None
            except Exception:
                raise CatalogError(
                    "Failed to decode field, invalid data",
                    self.s,
                    start,
                    key,
                ) from None

            try:
                bitmap = bytes.fromhex(decoded)
            except Exception:
                raise CatalogError(
                    "Failed to decode field, non-hex data",
                    self.s,
                    start,
                    key,
                ) from None

        enabled = tuple(
            offset + byte_idx * 8 + bit
            for byte_idx, byte in enumerate(bitmap)
            for bit in range(1, 9)
            if byte >> (8 - bit) & 1
        )

        self.idx = start + expected
        return BitmapLayer(
            key=key,
            layer=layer,
            layer_name=_LAYER_NAMES[layer],
            offset=offset,
            start=start,
            end=self.idx,
            expected_length=expected,
            available_length=expected,
            complete=True,
            trigger_de=trigger_de,
            enabled=enabled,
        )

    #
    # Fields
    #

    def _read_field(
        self,
        field: str,
        field_spec: Mapping[str, Any],
        layer: int,
        offset: int,
    ) -> FieldDeclaration:
        position = _bitmap_position(field, layer, offset)
        layout = self._layout(field, field_spec)

        if layout.data_start is None or layout.data_end is None:
            # The length prefix itself is missing.
            return _truncated_field(
                field,
                field_spec,
                position,
                DataRange(layout.prefix_start, layout.prefix_end),
                layout.declared_length,
                layout.length_count,
            )

        data_range = DataRange(layout.data_start, layout.data_end)
        value: Optional[str] = None
        if not layout.truncated:
            value = _decode_value(
                self.s,
                field,
                field_spec,
                bytes(self.s[layout.data_start : layout.data_end]),
                layout.declared_length or 0,
                layout.length_count,
                layout.data_start,
            )

        # Advance even for truncated data: the bytes that are present still
        # belong to this field. `truncated` records that the field is
        # incomplete.
        self.idx = layout.data_end

        return FieldDeclaration(
            field=field,
            name=str(field_spec.get("desc", "")),
            bitmap=position,
            length_prefix=DataRange(layout.prefix_start, layout.prefix_end),
            data_range=data_range,
            declared_length=layout.declared_length,
            length_count=layout.length_count,
            value=value,
            truncated=layout.truncated,
            known=True,
        )

    def _layout(
        self,
        field: str,
        field_spec: Mapping[str, Any],
    ) -> _Layout:
        len_type = field_spec["len_type"]
        len_count = field_spec.get("len_count", "bytes")
        prefix_start = self.idx
        prefix_end = prefix_start + len_type

        if len_type > 0 and prefix_end > len(self.s):
            if self.strict:
                raise CatalogError(
                    f"Field length is {len(self.s) - prefix_start} bytes wide, expecting {len_type}",
                    self.s,
                    prefix_start,
                    field,
                )
            return _Layout(
                prefix_start=prefix_start,
                prefix_end=len(self.s),
                data_start=None,
                data_end=None,
                declared_length=None,
                length_count=len_count,
                truncated=True,
            )

        if len_type == 0:
            declared_length: int = field_spec["max_len"]
        else:
            prefix = bytes(self.s[prefix_start:prefix_end])
            declared_length = _decode_length(
                self.s, field, field_spec, prefix, prefix_start
            )
            if declared_length > field_spec["max_len"]:
                raise CatalogError(
                    f"Field data is {declared_length} {len_count}, "
                    f"larger than maximum {field_spec['max_len']}",
                    self.s,
                    prefix_start,
                    field,
                )

        if declared_length == 0:
            byte_length = 0
        elif len_count == "nibbles":
            byte_length = (declared_length + 1) // 2
        else:
            byte_length = declared_length

        data_start = prefix_end
        data_end = data_start + byte_length
        truncated = data_end > len(self.s)
        if truncated and self.strict:
            actual = len(self.s) - data_start
            if len_count == "nibbles":
                actual *= 2
            raise CatalogError(
                f"Field data is {actual} {len_count}, expecting {declared_length}",
                self.s,
                data_start,
                field,
            )

        return _Layout(
            prefix_start=prefix_start,
            prefix_end=prefix_end,
            data_start=data_start,
            data_end=min(data_end, len(self.s)),
            declared_length=declared_length,
            length_count=len_count,
            truncated=truncated,
        )


def _bitmap_position(field: str, layer: int, offset: int) -> BitmapPosition:
    de = int(field)
    ordinal = de - offset
    byte = (ordinal - 1) // 8
    bit = (ordinal - 1) % 8 + 1
    return BitmapPosition(
        layer=layer,
        layer_name=_LAYER_NAMES[layer],
        key="p" if layer == 0 else "1",
        de=de,
        byte=byte,
        bit=bit,
        ordinal=ordinal,
        mask=0x80 >> (bit - 1),
    )


def _length_count(field_spec: Mapping[str, Any]) -> str:
    return str(field_spec.get("len_count", "bytes"))


def _truncated_field(
    field: str,
    field_spec: Mapping[str, Any],
    position: BitmapPosition,
    length_prefix: DataRange,
    declared_length: Optional[int],
    length_count: str,
) -> FieldDeclaration:
    return FieldDeclaration(
        field=field,
        name=str(field_spec.get("desc", "")),
        bitmap=position,
        length_prefix=length_prefix,
        data_range=None,
        declared_length=declared_length,
        length_count=length_count,
        value=None,
        truncated=True,
        known=True,
    )


def _unlocated_field(
    field: str,
    field_spec: Mapping[str, Any],
    layer: int,
    offset: int,
) -> FieldDeclaration:
    r"""Keep a declaration whose byte range cannot be determined."""
    return FieldDeclaration(
        field=field,
        name=str(field_spec.get("desc", "")),
        bitmap=_bitmap_position(field, layer, offset),
        length_prefix=None,
        data_range=None,
        declared_length=None,
        length_count=_length_count(field_spec),
        value=None,
        truncated=False,
        known=True,
    )


def _unknown_field(
    field: str,
    layer: int,
    offset: int,
) -> FieldDeclaration:
    r"""Keep a declaration for a field that the specification does not define."""
    return FieldDeclaration(
        field=field,
        name="",
        bitmap=_bitmap_position(field, layer, offset),
        length_prefix=None,
        data_range=None,
        declared_length=None,
        length_count="bytes",
        value=None,
        truncated=False,
        known=False,
    )


def _decode_length(
    s: Union[bytes, bytearray],
    field: str,
    field_spec: Mapping[str, Any],
    prefix: bytes,
    pos: int,
) -> int:
    len_enc = field_spec["len_enc"]

    if len_enc == "b":
        return int.from_bytes(prefix, "big", signed=False)

    if len_enc == "bcd":
        try:
            return int(prefix.hex(), 10)
        except Exception:
            raise CatalogError(
                "Failed to decode field length, invalid BCD data",
                s,
                pos,
                field,
            ) from None

    try:
        decoded = prefix.decode(len_enc)
    except LookupError:
        raise CatalogError(
            "Failed to decode field length, unknown encoding specified",
            s,
            pos,
            field,
        ) from None
    except Exception:
        raise CatalogError(
            "Failed to decode field length, invalid data",
            s,
            pos,
            field,
        ) from None

    try:
        return int(decoded)
    except Exception:
        raise CatalogError(
            "Failed to decode field length, non-numeric data",
            s,
            pos,
            field,
        ) from None


def _decode_value(
    s: Union[bytes, bytearray],
    field: str,
    field_spec: Mapping[str, Any],
    data: bytes,
    declared_length: int,
    len_count: str,
    pos: int,
) -> str:
    if field_spec["data_enc"] == "b":
        value = data.hex().upper()
        if len_count == "nibbles" and declared_length & 1:
            value = _remove_pad(s, field, field_spec, value, declared_length, pos)
        return value

    try:
        return data.decode(field_spec["data_enc"])
    except LookupError:
        raise CatalogError(
            "Failed to decode field, unknown encoding specified",
            s,
            pos,
            field,
        ) from None
    except Exception:
        raise CatalogError(
            "Failed to decode field, invalid data",
            s,
            pos,
            field,
        ) from None


def _remove_pad(
    s: Union[bytes, bytearray],
    field: str,
    field_spec: Mapping[str, Any],
    value: str,
    declared_length: int,
    pos: int,
) -> str:
    pad: str = field_spec.get("left_pad", "")[:1]
    if len(pad) > 0 and value[:1] == pad:
        return value[1:]

    pad = field_spec.get("right_pad", "")[:1]
    if len(pad) > 0 and value[-1:] == pad:
        return value[:-1]

    raise CatalogError(
        f"Field data is {len(value)} nibbles, expecting {declared_length}",
        s,
        pos,
        field,
    )
