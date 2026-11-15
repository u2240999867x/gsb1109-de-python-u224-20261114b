"""
Reassemble chunked, already decoded header archives into per-stream call facts.

Input chunks are the local test-archive representation of HTTP/2 HEADERS and
CONTINUATION frames: every chunk belongs to a ``(stream_id, direction)`` frame,
carries its already decoded ordered header fields, an end-of-headers marker and
an end-of-stream marker. Chunks of the two streams may arrive interleaved; frame
reassembly is therefore keyed per stream and direction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from callfacts.model import (
    CallFact,
    Direction,
    Frame,
    Header,
    OpenFrame,
    RequestFacts,
    ResponseFacts,
    StatsSummary,
)

__all__ = [
    "ArchiveInputError",
    "ArchiveResult",
    "Chunk",
    "chunks_from_dicts",
    "parse_archive",
]

_STATUS_PSEUDO_HEADER = ":status"


class ArchiveInputError(ValueError):
    """Raised when an archive cannot be reassembled into consistent call facts."""


@dataclass(frozen=True, slots=True)
class Chunk:
    """One decoded archive block, positioned by its original block number."""

    block_no: int
    stream_id: int
    direction: Direction
    end_headers: bool
    end_stream: bool
    headers: tuple[Header, ...]

    @classmethod
    def from_dict(cls, record: Mapping[str, object]) -> Chunk:
        """Build a chunk from a decoded JSON object using the archive schema."""
        try:
            block_no = record["block_no"]
            stream_id = record["stream_id"]
            direction = record["direction"]
            end_headers = record["end_headers"]
            end_stream = record["end_stream"]
            raw_headers = record["headers"]
        except KeyError as exc:
            msg = f"chunk record is missing required field: {exc.args[0]!r}"
            raise ArchiveInputError(msg) from exc
        if not isinstance(block_no, int) or isinstance(block_no, bool):
            msg = f"block_no must be an integer, got {type(block_no).__name__}"
            raise ArchiveInputError(msg)
        if not isinstance(stream_id, int) or isinstance(stream_id, bool):
            msg = f"stream_id must be an integer, got {type(stream_id).__name__}"
            raise ArchiveInputError(msg)
        if direction not in ("request", "response"):
            msg = f"direction must be 'request' or 'response', got {direction!r}"
            raise ArchiveInputError(msg)
        if not isinstance(end_headers, bool) or not isinstance(end_stream, bool):
            msg = "end_headers and end_stream must be booleans"
            raise ArchiveInputError(msg)
        if not isinstance(raw_headers, Sequence) or isinstance(raw_headers, (str, bytes)):
            msg = "headers must be a sequence of (name, value) pairs"
            raise ArchiveInputError(msg)
        return cls(
            block_no=block_no,
            stream_id=stream_id,
            direction=direction,  # type: ignore[arg-type]  # narrowed above
            end_headers=end_headers,
            end_stream=end_stream,
            headers=tuple(_coerce_header(item) for item in raw_headers),
        )


def chunks_from_dicts(records: Iterable[Mapping[str, object]]) -> list[Chunk]:
    """Convert decoded JSON archive records into validated chunks."""
    return [Chunk.from_dict(record) for record in records]


@dataclass(slots=True)
class _OpenFrame:
    direction: Direction
    block_numbers: list[int] = field(default_factory=list)
    headers: list[Header] = field(default_factory=list)
    end_stream: bool = False


@dataclass(slots=True)
class _StreamAccumulator:
    frame_counters: dict[Direction, int] = field(default_factory=dict)
    end_stream_seen: dict[Direction, bool] = field(default_factory=dict)
    request: RequestFacts | None = None
    informational: list[ResponseFacts] = field(default_factory=list)
    final: ResponseFacts | None = None
    trailers: list[ResponseFacts] = field(default_factory=list)
    open_frames: list[OpenFrame] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """The call facts of one archive together with aggregate statistics."""

    facts: tuple[CallFact, ...]
    stats: StatsSummary


def parse_archive(chunks: Iterable[Chunk]) -> ArchiveResult:
    """
    Reassemble archive chunks into per-stream call facts.

    Chunks are consumed in archive order. Frames are reassembled independently
    per ``(stream_id, direction)``, so chunks belonging to different streams may
    interleave freely. A stream whose response side never reaches a non-1xx
    status remains ``undetermined`` and is kept out of the success-rate math.
    """
    open_frames: dict[tuple[int, Direction], _OpenFrame] = {}
    streams: dict[int, _StreamAccumulator] = {}
    stream_order: list[int] = []
    seen_block_numbers: set[int] = set()

    for chunk in chunks:
        if chunk.block_no in seen_block_numbers:
            msg = f"duplicate block number {chunk.block_no}"
            raise ArchiveInputError(msg)
        seen_block_numbers.add(chunk.block_no)

        key = (chunk.stream_id, chunk.direction)
        accumulator = streams.get(chunk.stream_id)
        if accumulator is None:
            accumulator = _StreamAccumulator()
            streams[chunk.stream_id] = accumulator
            stream_order.append(chunk.stream_id)

        open_frame = open_frames.get(key)
        if open_frame is None:
            open_frame = _OpenFrame(direction=chunk.direction)
            open_frames[key] = open_frame
        open_frame.block_numbers.append(chunk.block_no)
        open_frame.headers.extend(chunk.headers)
        open_frame.end_stream = open_frame.end_stream or chunk.end_stream

        if not chunk.end_headers:
            continue

        frame = _close_frame(open_frame, accumulator)
        del open_frames[key]
        _classify_frame(frame, accumulator, chunk.stream_id)

    for (stream_id, direction), open_frame in open_frames.items():
        accumulator = streams[stream_id]
        accumulator.open_frames.append(
            OpenFrame(
                direction=direction,
                block_numbers=tuple(open_frame.block_numbers),
                headers=tuple(open_frame.headers),
                end_stream=open_frame.end_stream,
            ),
        )

    facts = tuple(_build_fact(stream_id, streams[stream_id]) for stream_id in stream_order)
    return ArchiveResult(facts=facts, stats=StatsSummary.from_facts(facts))


def _close_frame(open_frame: _OpenFrame, accumulator: _StreamAccumulator) -> Frame:
    frame_no = accumulator.frame_counters.get(open_frame.direction, 0) + 1
    accumulator.frame_counters[open_frame.direction] = frame_no
    block_numbers = tuple(open_frame.block_numbers)
    return Frame(
        direction=open_frame.direction,
        frame_no=frame_no,
        block_numbers=block_numbers,
        headers=tuple(open_frame.headers),
        end_stream=open_frame.end_stream,
    )


def _classify_frame(frame: Frame, accumulator: _StreamAccumulator, stream_id: int) -> None:
    if accumulator.end_stream_seen.get(frame.direction, False):
        msg = f"stream {stream_id} {frame.direction} frame {frame.frame_no} arrives after END_STREAM"
        raise ArchiveInputError(msg)
    accumulator.end_stream_seen[frame.direction] = frame.end_stream

    if frame.direction == "request":
        _classify_request_frame(frame, accumulator, stream_id)
    else:
        _classify_response_frame(frame, accumulator, stream_id)


def _classify_request_frame(frame: Frame, accumulator: _StreamAccumulator, stream_id: int) -> None:
    if accumulator.request is not None:
        msg = f"stream {stream_id} carries more than one request frame"
        raise ArchiveInputError(msg)
    accumulator.request = RequestFacts(
        frame_no=frame.frame_no,
        block_numbers=frame.block_numbers,
        headers=frame.headers,
        end_stream=frame.end_stream,
    )


def _classify_response_frame(frame: Frame, accumulator: _StreamAccumulator, stream_id: int) -> None:
    status = _extract_status(frame, stream_id)

    if accumulator.final is not None:
        if status is not None:
            msg = f"stream {stream_id} response frame {frame.frame_no} carries :status after the final response"
            raise ArchiveInputError(msg)
        accumulator.trailers.append(
            ResponseFacts(
                phase="trailer",
                status=None,
                frame_no=frame.frame_no,
                block_numbers=frame.block_numbers,
                headers=frame.headers,
                end_stream=frame.end_stream,
            ),
        )
        return

    if status is None:
        msg = f"stream {stream_id} response frame {frame.frame_no} is missing :status"
        raise ArchiveInputError(msg)

    if 100 <= status < 200:
        if frame.end_stream:
            msg = f"stream {stream_id} informational response {status} must not carry END_STREAM"
            raise ArchiveInputError(msg)
        response = ResponseFacts(
            phase="informational",
            status=status,
            frame_no=frame.frame_no,
            block_numbers=frame.block_numbers,
            headers=frame.headers,
            end_stream=frame.end_stream,
        )
        accumulator.informational.append(response)
        return

    accumulator.final = ResponseFacts(
        phase="final",
        status=status,
        frame_no=frame.frame_no,
        block_numbers=frame.block_numbers,
        headers=frame.headers,
        end_stream=frame.end_stream,
    )


def _extract_status(frame: Frame, stream_id: int) -> int | None:
    status_values = [value for name, value in frame.headers if name == _STATUS_PSEUDO_HEADER]
    if not status_values:
        return None
    if len(status_values) > 1:
        msg = f"stream {stream_id} response frame {frame.frame_no} has multiple :status fields"
        raise ArchiveInputError(msg)
    raw_status = status_values[0]
    try:
        status = int(raw_status)
    except ValueError as exc:
        msg = f"stream {stream_id} response frame {frame.frame_no} has non-integer :status {raw_status!r}"
        raise ArchiveInputError(msg) from exc
    if not 100 <= status <= 599:
        msg = f"stream {stream_id} response frame {frame.frame_no} has out-of-range :status {status}"
        raise ArchiveInputError(msg)
    return status


def _build_fact(stream_id: int, accumulator: _StreamAccumulator) -> CallFact:
    return CallFact(
        stream_id=stream_id,
        request=accumulator.request,
        informational=tuple(accumulator.informational),
        final=accumulator.final,
        trailers=tuple(accumulator.trailers),
        open_frames=tuple(accumulator.open_frames),
    )


def _coerce_header(item: object) -> Header:
    if isinstance(item, Mapping):
        name = item.get("name")
        value = item.get("value")
    elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
        name, value = item
    else:
        msg = f"header entry must be a (name, value) pair or name/value mapping, got {item!r}"
        raise ArchiveInputError(msg)
    if not isinstance(name, str) or not isinstance(value, str):
        msg = f"header name and value must be strings, got {item!r}"
        raise ArchiveInputError(msg)
    return name, value
