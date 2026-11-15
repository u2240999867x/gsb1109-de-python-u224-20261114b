"""
Reconstruct HTTP/2-style call facts from chunked local test archives.

Typical use::

    from callfacts import load_archive

    result = load_archive("capture.json")
    for fact in result.facts:
        print(fact.stream_id, fact.state)
    print(result.stats.success_rate)
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from callfacts.model import (
    CallFact,
    Direction,
    Frame,
    Header,
    OpenFrame,
    Phase,
    RequestFacts,
    ResponseFacts,
    StatsSummary,
    StreamState,
)
from callfacts.parser import (
    ArchiveInputError,
    ArchiveResult,
    Chunk,
    chunks_from_dicts,
    parse_archive,
)

__all__ = [
    "ArchiveInputError",
    "ArchiveResult",
    "CallFact",
    "Chunk",
    "Direction",
    "Frame",
    "Header",
    "OpenFrame",
    "Phase",
    "RequestFacts",
    "ResponseFacts",
    "StatsSummary",
    "StreamState",
    "chunks_from_dicts",
    "load_archive",
    "parse_archive",
    "parse_records",
]


def parse_records(records: Iterable[Mapping[str, Any]]) -> ArchiveResult:
    """Validate raw archive records and reassemble them into call facts."""
    return parse_archive(chunks_from_dicts(records))


def load_archive(source: str | Path | Any) -> ArchiveResult:
    """
    Load a local test archive from a path or an open text/JSON file object.

    The archive may be either a bare list of chunk records or an object with a
    ``chunks`` (or ``blocks``) key holding that list. Chunk order inside the
    document is preserved as archive order.
    """
    if isinstance(source, (str, Path)):
        with Path(source).open(encoding="utf-8") as handle:
            document = json.load(handle)
    else:
        document = json.load(source)

    if isinstance(document, Mapping):
        records = document.get("chunks", document.get("blocks"))
        if records is None:
            msg = "archive object must contain a 'chunks' list"
            raise ArchiveInputError(msg)
    elif isinstance(document, list):
        records = document
    else:
        msg = "archive must be a list of chunk records or an object with 'chunks'"
        raise ArchiveInputError(msg)

    return parse_records(records)
