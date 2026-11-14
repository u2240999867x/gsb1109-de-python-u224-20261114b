Migration Rehearsal
===================

.. currentmodule:: iso8583

When messages move from one specification to another, field 48 may be
described by both specs but use a different data encoding and a shorter
length header. :func:`rehearse_message` and :func:`rehearse_batch` perform
an offline dry run: they never build a target message, they only pre-encode
each field against the target specification and report whether the field can
be carried over.

Every field is rehearsed independently, so one overlong field never blocks
the remaining fields. Each result row reports:

- ``encoded_bytes``: the size of the data **after** transcoding with the
  target ``data_enc``. This is the real on-wire byte size, not the number of
  characters in the source string.
- ``length_capacity``: the largest length expressible by the target length
  header, derived from ``len_enc`` and ``len_type`` (see
  :func:`length_header_capacity`).
- ``length_fits``: whether the transcoded length fits both ``max_len`` and
  the length header range. Fixed-length fields carry ``None``.
- ``roundtrip_equal``: whether decoding the encoded bytes returns the
  original value.
- ``status``: one of ``ok``, ``undefined``, ``encode_error``, ``too_long``,
  ``length_overflow``, ``length_mismatch`` or ``roundtrip_mismatch``.

A field that the target specification does not define is reported as
``undefined``; a field that is defined but cannot hold the data is reported
with the fitting or roundtrip status instead. The two cases never collapse
into one another.

Example
-------

The source specification stores field 48 as ASCII with a three-digit length
header. The target keeps field 48 but stores binary/BCD data with only a
two-digit ASCII length header, so four ASCII digits become two BCD bytes.

.. code-block:: python

    >>> import copy
    >>> import iso8583
    >>> from iso8583.specs import default_ascii
    >>> target_spec = copy.deepcopy(default_ascii)
    >>> target_spec["48"].update(
    ...     data_enc="b", len_enc="ascii", len_type=2, max_len=99
    ... )
    >>> message = {"t": "0200", "48": "1234"}
    >>> rows = iso8583.rehearse_message(message, target_spec)
    >>> row = next(item for item in rows if item["field"] == "48")
    >>> row["status"], row["encoded_bytes"], row["length_capacity"]
    ('ok', 2, 99)
    >>> row["length_fits"], row["roundtrip_equal"]
    (True, True)

An overlong value is measured after transcoding (200 digits become 100 BCD
bytes), and a field missing from the target specification stays distinct:

.. code-block:: python

    >>> overlong = {"t": "0200", "48": "8" * 200, "130": "x"}
    >>> rows = {item["field"]: item for item in iso8583.rehearse_message(overlong, target_spec)}
    >>> rows["48"]["status"], rows["48"]["encoded_bytes"]
    ('too_long', 100)
    >>> rows["130"]["status"]
    'undefined'

Reference
---------
.. autofunction:: rehearse_field
.. autofunction:: rehearse_message
.. autofunction:: rehearse_batch
.. autofunction:: length_header_capacity
