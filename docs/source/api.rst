hpack API
=========

This document provides the HPACK API.

.. autoclass:: hpack.Encoder
   :members: header_table_size, encode, snapshot, restore

.. autoclass:: hpack.Decoder
   :members: header_table_size, decode, snapshot, restore

.. automodule:: hpack.checkpoint
   :members:

.. autoclass:: hpack.HeaderTuple
   :members: indexable

.. autoclass:: hpack.NeverIndexedHeaderTuple
   :members: indexable

.. autoclass:: hpack.HPACKError

.. autoclass:: hpack.HPACKDecodingError

.. autoclass:: hpack.InvalidTableIndexError

.. autoclass:: hpack.InvalidTableIndex

.. autoclass:: hpack.OversizedHeaderListError

.. autoclass:: hpack.InvalidTableSizeError

.. autoclass:: hpack.InvalidTableStateError
