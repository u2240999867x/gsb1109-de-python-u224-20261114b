# flake8: noqa
import base64
import binascii
from collections import deque
import logging
from typing import NamedTuple, Optional, TypedDict

from .exceptions import InvalidTableIndex, InvalidTableStateError

log = logging.getLogger(__name__)

#: Reason recorded when an entry is evicted to make room for a newly inserted
#: entry (see RFC 7541 section 4.4).
EVICTION_INSERT = "insert"

#: Reason recorded when entries are evicted because the table max size was
#: shrunk (see RFC 7541 section 4.3).
EVICTION_RESIZE = "resize"

#: Version of the serialized table state produced by :meth:`HeaderTable.snapshot`.
TABLE_STATE_VERSION = 1

#: Default capacity of the ring buffer holding eviction records.
DEFAULT_EVICTION_LOG_SIZE = 1024


def _encode_bytes(value: bytes) -> str:
    """
    Renders raw header bytes as a JSON-safe base64 string.
    """
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: str) -> bytes:
    """
    Reverses :func:`_encode_bytes`.
    """
    return base64.b64decode(value.encode("ascii"))


class EvictionEntry(NamedTuple):
    """
    A single dynamic table entry that was evicted.

    ``sequence`` is a monotonically increasing counter shared by the owning
    :class:`HeaderTable`, so evictions recorded at different checkpoints can be
    ordered and compared across a replay.
    """

    sequence: int
    reason: str
    name: bytes
    value: bytes
    size: int


class EvictionRecord(TypedDict):
    sequence: int
    reason: str
    name: str
    value: str
    size: int


class HeaderTableState(TypedDict):
    """
    Serializable representation of a :class:`HeaderTable`.

    The dynamic entries are stored newest-first, matching the in-memory
    ordering of ``dynamic_entries``.
    """

    version: int
    maxsize: int
    current_size: int
    resized: bool
    dynamic_entries: list[list[str]]
    eviction_log_size: int
    eviction_sequence: int
    evictions: list[EvictionRecord]


def table_entry_size(name: bytes, value: bytes) -> int:
    """
    Calculates the size of a single entry

    This size is mostly irrelevant to us and defined
    specifically to accommodate memory management for
    lower level implementations. The 32 extra bytes are
    considered the "maximum" overhead that would be
    required to represent each entry in the table.

    See RFC7541 Section 4.1
    """
    return 32 + len(name) + len(value)


class HeaderTable:
    """
    Implements the combined static and dynamic header table

    The name and value arguments for all the functions
    should ONLY be byte strings (b'') however this is not
    strictly enforced in the interface.

    See RFC7541 Section 2.3
    """
    #: Default maximum size of the dynamic table. See
    #:  RFC7540 Section 6.5.2.
    DEFAULT_SIZE = 4096

    #: Constant list of static headers. See RFC7541 Section
    #:  2.3.1 and Appendix A
    STATIC_TABLE = (
        (b':authority'                  , b''             ),  # noqa
        (b':method'                     , b'GET'          ),  # noqa
        (b':method'                     , b'POST'         ),  # noqa
        (b':path'                       , b'/'            ),  # noqa
        (b':path'                       , b'/index.html'  ),  # noqa
        (b':scheme'                     , b'http'         ),  # noqa
        (b':scheme'                     , b'https'        ),  # noqa
        (b':status'                     , b'200'          ),  # noqa
        (b':status'                     , b'204'          ),  # noqa
        (b':status'                     , b'206'          ),  # noqa
        (b':status'                     , b'304'          ),  # noqa
        (b':status'                     , b'400'          ),  # noqa
        (b':status'                     , b'404'          ),  # noqa
        (b':status'                     , b'500'          ),  # noqa
        (b'accept-charset'              , b''             ),  # noqa
        (b'accept-encoding'             , b'gzip, deflate'),  # noqa
        (b'accept-language'             , b''             ),  # noqa
        (b'accept-ranges'               , b''             ),  # noqa
        (b'accept'                      , b''             ),  # noqa
        (b'access-control-allow-origin' , b''             ),  # noqa
        (b'age'                         , b''             ),  # noqa
        (b'allow'                       , b''             ),  # noqa
        (b'authorization'               , b''             ),  # noqa
        (b'cache-control'               , b''             ),  # noqa
        (b'content-disposition'         , b''             ),  # noqa
        (b'content-encoding'            , b''             ),  # noqa
        (b'content-language'            , b''             ),  # noqa
        (b'content-length'              , b''             ),  # noqa
        (b'content-location'            , b''             ),  # noqa
        (b'content-range'               , b''             ),  # noqa
        (b'content-type'                , b''             ),  # noqa
        (b'cookie'                      , b''             ),  # noqa
        (b'date'                        , b''             ),  # noqa
        (b'etag'                        , b''             ),  # noqa
        (b'expect'                      , b''             ),  # noqa
        (b'expires'                     , b''             ),  # noqa
        (b'from'                        , b''             ),  # noqa
        (b'host'                        , b''             ),  # noqa
        (b'if-match'                    , b''             ),  # noqa
        (b'if-modified-since'           , b''             ),  # noqa
        (b'if-none-match'               , b''             ),  # noqa
        (b'if-range'                    , b''             ),  # noqa
        (b'if-unmodified-since'         , b''             ),  # noqa
        (b'last-modified'               , b''             ),  # noqa
        (b'link'                        , b''             ),  # noqa
        (b'location'                    , b''             ),  # noqa
        (b'max-forwards'                , b''             ),  # noqa
        (b'proxy-authenticate'          , b''             ),  # noqa
        (b'proxy-authorization'         , b''             ),  # noqa
        (b'range'                       , b''             ),  # noqa
        (b'referer'                     , b''             ),  # noqa
        (b'refresh'                     , b''             ),  # noqa
        (b'retry-after'                 , b''             ),  # noqa
        (b'server'                      , b''             ),  # noqa
        (b'set-cookie'                  , b''             ),  # noqa
        (b'strict-transport-security'   , b''             ),  # noqa
        (b'transfer-encoding'           , b''             ),  # noqa
        (b'user-agent'                  , b''             ),  # noqa
        (b'vary'                        , b''             ),  # noqa
        (b'via'                         , b''             ),  # noqa
        (b'www-authenticate'            , b''             ),  # noqa
    )  # noqa

    STATIC_TABLE_LENGTH = len(STATIC_TABLE)

    STATIC_TABLE_MAPPING: dict[bytes, tuple[int, dict[bytes, int]]]

    def __init__(self, eviction_log_size: int = DEFAULT_EVICTION_LOG_SIZE) -> None:
        self._maxsize = HeaderTable.DEFAULT_SIZE
        self._current_size = 0
        self.resized = False
        self.dynamic_entries: deque[tuple[bytes, bytes]] = deque()
        self._eviction_log_size = eviction_log_size
        self._eviction_sequence = 0
        self.eviction_log: deque[EvictionEntry] = deque(maxlen=eviction_log_size)

    def _record_eviction(self, name: bytes, value: bytes, reason: str) -> None:
        """
        Appends an eviction to the bounded replay log and bumps the global
        sequence counter.
        """
        self._eviction_sequence += 1
        self.eviction_log.append(
            EvictionEntry(
                sequence=self._eviction_sequence,
                reason=reason,
                name=name,
                value=value,
                size=table_entry_size(name, value),
            ),
        )

    def evictions_since(self, sequence: int) -> list[EvictionEntry]:
        """
        Returns all recorded evictions with a sequence number strictly greater
        than ``sequence``, in the order they happened.

        This makes the eviction list observable across checkpoints: a replay
        run can compare its list against an uninterrupted run even when the
        in-memory ring buffer has rotated.
        """
        return [entry for entry in self.eviction_log if entry.sequence > sequence]

    @property
    def eviction_sequence(self) -> int:
        """
        Sequence number of the most recently recorded eviction (0 if none).
        """
        return self._eviction_sequence

    def get_by_index(self, index: int) -> tuple[bytes, bytes]:
        """
        Returns the entry specified by index

        Note that the table is 1-based ie an index of 0 is
        invalid.  This is due to the fact that a zero value
        index signals that a completely unindexed header
        follows.

        The entry will either be from the static table or
        the dynamic table depending on the value of index.
        """
        original_index = index
        index -= 1
        if 0 <= index:
            if index < HeaderTable.STATIC_TABLE_LENGTH:
                return HeaderTable.STATIC_TABLE[index]

            index -= HeaderTable.STATIC_TABLE_LENGTH
            if index < len(self.dynamic_entries):
                return self.dynamic_entries[index]

        raise InvalidTableIndex("Invalid table index %d" % original_index)

    def __repr__(self) -> str:
        return "HeaderTable(%d, %s, %r)" % (
            self._maxsize,
            self.resized,
            self.dynamic_entries
        )

    def add(self, name: bytes, value: bytes) -> None:
        """
        Adds a new entry to the table

        We reduce the table size if the entry will make the
        table size greater than maxsize.
        """
        # We just clear the table if the entry is too big
        size = table_entry_size(name, value)
        if size > self._maxsize:
            self._clear(EVICTION_INSERT)
        else:
            # Add new entry
            self.dynamic_entries.appendleft((name, value))
            self._current_size += size
            self._shrink(EVICTION_INSERT)

    def search(self, name: bytes, value: bytes) -> Optional[tuple[int, bytes, Optional[bytes]]]:
        """
        Searches the table for the entry specified by name
        and value

        Returns one of the following:
            - ``None``, no match at all
            - ``(index, name, None)`` for partial matches on name only.
            - ``(index, name, value)`` for perfect matches.
        """
        partial = None

        header_name_search_result = HeaderTable.STATIC_TABLE_MAPPING.get(name)
        if header_name_search_result:
            index = header_name_search_result[1].get(value)
            if index is not None:
                return index, name, value
            else:
                partial = (header_name_search_result[0], name, None)

        offset = HeaderTable.STATIC_TABLE_LENGTH + 1
        for (i, (n, v)) in enumerate(self.dynamic_entries):
            if n == name:
                if v == value:
                    return i + offset, n, v
                elif partial is None:
                    partial = (i + offset, n, None)
        return partial

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @maxsize.setter
    def maxsize(self, newmax: int) -> None:
        newmax = int(newmax)
        log.debug("Resizing header table to %d from %d", newmax, self._maxsize)
        oldmax = self._maxsize
        self._maxsize = newmax
        self.resized = (newmax != oldmax)
        if newmax <= 0:
            self._clear(EVICTION_RESIZE)
        elif oldmax > newmax:
            self._shrink(EVICTION_RESIZE)

    def _clear(self, reason: str) -> None:
        """
        Removes every dynamic entry, recording each eviction.
        """
        while self.dynamic_entries:
            name, value = self.dynamic_entries.pop()
            self._record_eviction(name, value, reason)
        self._current_size = 0

    def _shrink(self, reason: str) -> None:
        """
        Shrinks the dynamic table to be at or below maxsize
        """
        cursize = self._current_size
        while cursize > self._maxsize:
            name, value = self.dynamic_entries.pop()
            cursize -= table_entry_size(name, value)
            self._record_eviction(name, value, reason)
            log.debug("Evicting %s: %s from the header table", name, value)
        self._current_size = cursize

    def snapshot(self) -> HeaderTableState:
        """
        Captures a fully serializable copy of the table state.

        The snapshot contains the effective dynamic table contents, the
        current max size, the pending ``resized`` flag and the eviction log
        (including the global eviction sequence). Restoring it yields a table
        indistinguishable from the original, without replaying the evictions a
        second time.
        """
        return {
            "version": TABLE_STATE_VERSION,
            "maxsize": self._maxsize,
            "current_size": self._current_size,
            "resized": self.resized,
            "dynamic_entries": [
                [_encode_bytes(name), _encode_bytes(value)]
                for name, value in self.dynamic_entries
            ],
            "eviction_log_size": self._eviction_log_size,
            "eviction_sequence": self._eviction_sequence,
            "evictions": [
                {
                    "sequence": entry.sequence,
                    "reason": entry.reason,
                    "name": _encode_bytes(entry.name),
                    "value": _encode_bytes(entry.value),
                    "size": entry.size,
                }
                for entry in self.eviction_log
            ],
        }

    def restore(self, state: HeaderTableState) -> None:
        """
        Rebuilds the table from a state produced by :meth:`snapshot`.

        Entries that were evicted before the snapshot was taken are not
        reintroduced: a later max-size growth only restores capacity, never
        evicted header values. The eviction log and sequence counter are
        restored verbatim so evictions observed after recovery continue the
        same ordering.

        Any malformed or internally inconsistent state raises
        :class:`~hpack.exceptions.InvalidTableStateError`.
        """
        try:
            self._restore(state)
        except InvalidTableStateError:
            raise
        except (KeyError, TypeError, ValueError, binascii.Error) as err:
            msg = "Corrupted header table state"
            raise InvalidTableStateError(msg) from err

    def _restore(self, state: HeaderTableState) -> None:
        if state.get("version") != TABLE_STATE_VERSION:
            msg = f"Unsupported header table state version: {state.get('version')!r}"
            raise InvalidTableStateError(msg)

        maxsize = state["maxsize"]
        current_size = state["current_size"]
        eviction_log_size = state["eviction_log_size"]
        eviction_sequence = state["eviction_sequence"]
        if not isinstance(maxsize, int) or maxsize < 0:
            msg = "Corrupted header table state: invalid max size"
            raise InvalidTableStateError(msg)
        if not isinstance(current_size, int) or current_size < 0:
            msg = "Corrupted header table state: invalid current size"
            raise InvalidTableStateError(msg)
        if not isinstance(eviction_log_size, int) or eviction_log_size < 0:
            msg = "Corrupted header table state: invalid eviction log size"
            raise InvalidTableStateError(msg)
        if not isinstance(eviction_sequence, int) or eviction_sequence < 0:
            msg = "Corrupted header table state: invalid eviction sequence"
            raise InvalidTableStateError(msg)

        entries: deque[tuple[bytes, bytes]] = deque(
            (_decode_bytes(name), _decode_bytes(value))
            for name, value in state["dynamic_entries"]
        )

        # Recomputing the size guards against tampered or truncated state and
        # keeps the invariant that current_size matches the stored entries.
        recomputed_size = sum(table_entry_size(name, value) for name, value in entries)
        if recomputed_size != current_size or (entries and current_size > maxsize):
            msg = "Corrupted header table state: stored size does not match entries"
            raise InvalidTableStateError(msg)

        restored_log: deque[EvictionEntry] = deque(maxlen=eviction_log_size)
        previous_sequence = 0
        for record in state["evictions"]:
            name = _decode_bytes(record["name"])
            value = _decode_bytes(record["value"])
            entry = EvictionEntry(
                sequence=record["sequence"],
                reason=record["reason"],
                name=name,
                value=value,
                size=record["size"],
            )
            if entry.size != table_entry_size(name, value):
                msg = "Corrupted header table state: eviction size mismatch"
                raise InvalidTableStateError(msg)
            if entry.sequence <= previous_sequence:
                msg = "Corrupted header table state: eviction sequence is not monotonic"
                raise InvalidTableStateError(msg)
            previous_sequence = entry.sequence
            restored_log.append(entry)

        if eviction_sequence < previous_sequence:
            msg = "Corrupted header table state: eviction sequence below log entries"
            raise InvalidTableStateError(msg)

        self._maxsize = maxsize
        self._current_size = current_size
        self.resized = state["resized"]
        self.dynamic_entries = entries
        self._eviction_log_size = eviction_log_size
        self.eviction_log = restored_log
        self._eviction_sequence = eviction_sequence


def _build_static_table_mapping() -> dict[bytes, tuple[int, dict[bytes, int]]]:
    """
    Build static table mapping from header name to tuple with next structure:
    (<minimal index of header>, <mapping from header value to it index>).

    static_table_mapping used for hash searching.
    """
    static_table_mapping: dict[bytes, tuple[int, dict[bytes, int]]] = {}
    for index, (name, value) in enumerate(HeaderTable.STATIC_TABLE, 1):
        header_name_search_result = static_table_mapping.setdefault(name, (index, {}))
        header_name_search_result[1][value] = index
    return static_table_mapping


HeaderTable.STATIC_TABLE_MAPPING = _build_static_table_mapping()
