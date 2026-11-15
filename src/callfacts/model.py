"""
Facts reconstructed from chunked, already decoded HTTP/2-style header archives.

The public surface intentionally uses immutable value objects so that downstream
consumers can share facts freely and always trace each fact back to the original
chunk numbers carried by the local test archive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Sequence  # pragma: no cover

Direction: TypeAlias = Literal["request", "response"]
Phase: TypeAlias = Literal["informational", "final", "trailer"]
StreamState: TypeAlias = Literal["undetermined", "complete"]
Header: TypeAlias = tuple[str, str]

__all__ = [
    "CallFact",
    "Direction",
    "Frame",
    "Header",
    "OpenFrame",
    "Phase",
    "RequestFacts",
    "ResponseFacts",
    "StatsSummary",
    "StreamState",
]


@dataclass(frozen=True, slots=True)
class Frame:
    """A fully terminated header frame assembled from one or more chunks."""

    direction: Direction
    frame_no: int
    block_numbers: tuple[int, ...]
    headers: tuple[Header, ...]
    end_stream: bool


@dataclass(frozen=True, slots=True)
class RequestFacts:
    """The ordered fields of the single request frame of a stream."""

    frame_no: int
    block_numbers: tuple[int, ...]
    headers: tuple[Header, ...]
    end_stream: bool


@dataclass(frozen=True, slots=True)
class ResponseFacts:
    """One response frame with the phase assigned by the stream state machine."""

    phase: Phase
    status: int | None
    frame_no: int
    block_numbers: tuple[int, ...]
    headers: tuple[Header, ...]
    end_stream: bool


@dataclass(frozen=True, slots=True)
class OpenFrame:
    """A frame whose chunks never carried an end-of-headers marker."""

    direction: Direction
    block_numbers: tuple[int, ...]
    headers: tuple[Header, ...]
    end_stream: bool


@dataclass(frozen=True, slots=True)
class CallFact:
    """
    Per-stream call fact.

    Each phase keeps its own ordered fields and the chunk numbers it came from.
    A stream without a final response stays ``undetermined`` even when one or
    more informational responses (for example 103 Early Hints) arrived first.
    """

    stream_id: int
    request: RequestFacts | None
    informational: tuple[ResponseFacts, ...]
    final: ResponseFacts | None
    trailers: tuple[ResponseFacts, ...]
    open_frames: tuple[OpenFrame, ...]

    @property
    def state(self) -> StreamState:
        """Return ``complete`` only when a final response was observed."""
        return "complete" if self.final is not None else "undetermined"


@dataclass(frozen=True, slots=True)
class StatsSummary:
    """Aggregate statistics that deliberately exclude undetermined streams."""

    total_streams: int
    finalized_streams: int
    undetermined_streams: int
    successful_streams: int
    failed_streams: int
    success_rate: float

    @classmethod
    def from_facts(cls, facts: Sequence[CallFact]) -> StatsSummary:
        """
        Compute statistics over finalized streams only.

        A stream counts as successful when its final response is 2xx. Streams
        without a final response stay undetermined and never enter the
        success-rate denominator, so an early-arriving 103 cannot inflate it.
        """
        total = len(facts)
        finalized = [fact for fact in facts if fact.final is not None]
        successful = sum(1 for fact in finalized if _is_successful(fact))
        failed = len(finalized) - successful
        rate = successful / len(finalized) if finalized else 0.0
        return cls(
            total_streams=total,
            finalized_streams=len(finalized),
            undetermined_streams=total - len(finalized),
            successful_streams=successful,
            failed_streams=failed,
            success_rate=rate,
        )


def _is_successful(fact: CallFact) -> bool:
    final = fact.final
    return final is not None and final.status is not None and 200 <= final.status < 300
