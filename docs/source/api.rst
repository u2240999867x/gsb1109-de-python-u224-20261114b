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

Archive planning
================

:class:`hpack.ArchivePlanner` chooses the cheapest combination of raw header
blocks and dynamic-table checkpoints that can be replayed in a blank environment
to recover a set of target blocks.  :func:`hpack.replay` verifies such a plan
with a real :class:`hpack.Decoder`.

.. autoclass:: hpack.ArchivePlanner
   :members: plan, explain

.. autoclass:: hpack.ArchivePlan

.. autoclass:: hpack.Checkpoint

.. autoclass:: hpack.Target

.. autoclass:: hpack.TargetSource

.. autoclass:: hpack.TableState
   :members: stored_size

.. autofunction:: hpack.replay

.. autofunction:: hpack.brute_force_minimum
