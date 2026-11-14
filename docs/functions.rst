=======================================================
Functions
=======================================================

Main Functions
--------------
.. currentmodule:: iso8583
.. autofunction:: decode
.. autofunction:: encode

Exceptions
----------
.. autoexception:: DecodeError
.. autoexception:: EncodeError

Helper Functions
----------------
.. autofunction:: pp

BCD Dialect Migration
----------------------
.. autoclass:: BcdSide
    :members:
.. autoclass:: BcdFieldEncoding
    :members:
.. autoclass:: BcdFieldConversion
    :members:
.. autofunction:: convert_field
.. autofunction:: encode_side
.. autofunction:: decode_side
.. autoexception:: BcdMigrationError
.. autoexception:: BcdAmbiguityError
