hpack API
=========

This document provides the HPACK API.

.. autoclass:: hpack.Encoder
   :members: header_table_size, encode

.. autoclass:: hpack.Decoder
   :members: header_table_size, decode

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

Labeled Batch Decoding
----------------------

These helpers decode batches of header blocks that carry connection
generation, direction, stream and block-order labels.  Each
``(connection generation, direction)`` pair owns an independent compression
context, and every table index reference records the historical block that
introduced the referenced dynamic-table entry.

.. autoclass:: hpack.BlockDecoder
   :members: decode_block, decode_all, context, contexts, blocks, fields

.. autoclass:: hpack.CompressionContext
   :members: decode_block, blocks, header_table

.. autoclass:: hpack.BlockRef
   :members: connection_generation, direction, stream_id, sequence

.. autoclass:: hpack.DecodedBlock
   :members: ref, raw, fields, table_size_updates

.. autoclass:: hpack.DecodedField
   :members: name, value, representation, name_ref, value_ref

.. autoclass:: hpack.HeaderBlockRef
   :members: table_index, table_kind, name, value, block, field_index

.. autoclass:: hpack.FieldRow
   :members: connection_generation, direction, stream_id, block_sequence, field_index, name, value, representation, name_ref, value_ref

.. autoclass:: hpack.Representation
   :members:

.. autoclass:: hpack.BlockSequenceError
