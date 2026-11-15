hpack API
=========

This document provides the HPACK API.

.. autoclass:: hpack.Encoder
   :members: header_table_size, encode

.. autoclass:: hpack.Decoder
   :members: header_table_size, decode, recover

.. autofunction:: hpack.decode_with_recovery

.. autoclass:: hpack.RecoveryResult
   :members:

.. autoclass:: hpack.RecoveryEvidence
   :members:

.. autoclass:: hpack.RecoveredHeader
   :members:

.. autoclass:: hpack.RecoveredNeverIndexedHeader
   :members:

.. autoclass:: hpack.RecoveryEvent
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
