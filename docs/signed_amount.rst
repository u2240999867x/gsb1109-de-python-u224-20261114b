Legacy Signed Amount Fields
============================

Some legacy and private ISO 8583 amount fields are fixed-length
binary-coded decimal (BCD) fields where the sign is carried in the last
nibble instead of in a separate character. For example the four bytes
``00 00 01 2C`` represent a positive amount of 12, while
``00 00 00 0D`` is a declared negative zero.

The :mod:`iso8583.signed_amount` module parses these fields without
losing the original declaration and provides a numeric view for analysis
on top of the verbatim bit string.

Sign convention
---------------

The trailing nibble follows the common mainframe overpunch table:

- ``C``, ``E`` and ``F`` mean positive; ``F`` also means unsigned.
- ``D`` and ``B`` mean negative; ``D`` is the standard code.
- Any other nibble, including a digit such as ``7`` or the letter ``A``,
  is an illegal sign code and yields no numeric view.

Field specification
-------------------

A trailing-sign field must be an even-width BCD field measured in
nibbles without a pad character. Configuring ``left_pad`` or
``right_pad`` would make the decoder treat the sign nibble as padding and
remove it. Use :func:`iso8583.signed_amount.signed_amount_field_spec` to
build a matching specification.

Distinct declarations
----------------------

The parser keeps four situations apart instead of collapsing them into a
plain number:

- a declared negative zero, with a negative sign nibble and zero magnitude;
- a normal zero, with a positive sign nibble and zero magnitude;
- an empty field, when the field is absent from the message;
- an illegal sign code, where the raw nibbles are retained but no value
  or sign is produced.

The numeric view of negative zero and normal zero is the same integer
``0``. The raw nibble strings (``0000000D`` and ``0000000C``) remain
distinct.

Verbatim re-encoding
--------------------

The same numeric value can appear in fields of different business widths.
The samples ship two value-12 fields: an 8-nibble transaction amount
(``0000012C``) and a 6-nibble settlement amount (``00012C``). A formatted
amount carries neither the width nor the sign declaration, so it cannot
recover the original bit string. Re-encoding always uses the retained
``nibbles`` or ``raw`` view, never a value reconstructed from the number.

An executable, checked set of these samples lives in
``examples/signed_amount_samples.py``::

    python examples/signed_amount_samples.py

Reference
---------

.. automodule:: iso8583.signed_amount
   :members:
   :undoc-members:
   :show-inheritance:
