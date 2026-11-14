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

Sanitising collection reports
-----------------------------

.. autoclass:: hpack.ReportPolicy

.. autoclass:: hpack.Aggregate
   :members:

.. autoclass:: hpack.ValueState
   :members:

.. autofunction:: hpack.clean_records

.. autofunction:: hpack.clean_record

.. autofunction:: hpack.sanitize_records

.. autofunction:: hpack.sanitize_headers

.. autoclass:: hpack.CleanedReport
   :members: to_dict, to_json, statistics_record, statistics_json

.. autoclass:: hpack.CleanedRecord
   :members: to_dict

.. autoclass:: hpack.CleanedField
   :members: value_state, is_empty, is_redacted, to_dict

.. autoclass:: hpack.DuplicateFieldName
   :members: restricted_occurrences, empty_occurrences, to_dict

.. autoclass:: hpack.FieldStatistics
   :members: to_dict

.. autoclass:: hpack.ErrorSummary
   :members: to_dict

.. autoclass:: hpack.SanitizationError
   :members: summary, to_dict

.. autoclass:: hpack.HPACKError

.. autoclass:: hpack.HPACKDecodingError

.. autoclass:: hpack.InvalidTableIndexError

.. autoclass:: hpack.InvalidTableIndex

.. autoclass:: hpack.OversizedHeaderListError

.. autoclass:: hpack.InvalidTableSizeError
