"""
Persistence helpers for HPACK replay state.

The :class:`~hpack.Encoder` and :class:`~hpack.Decoder` classes expose
``snapshot``/``restore`` methods that produce plain, JSON-compatible state
dictionaries. This module wraps those dictionaries in a small typed envelope so
a batch processor can write checkpoints to disk between header blocks and
rebuild an identical coder after a restart without replaying the blocks it has
already consumed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .exceptions import InvalidTableStateError
from .hpack import DECODER_STATE_VERSION, ENCODER_STATE_VERSION, Decoder, Encoder

if TYPE_CHECKING:
    from os import PathLike

    from .hpack import DecoderState, EncoderState

ENCODER_KIND = "encoder"
DECODER_KIND = "decoder"


def _envelope(kind: str, state: EncoderState | DecoderState) -> dict[str, Any]:
    return {"kind": kind, "state": state}


def dumps_checkpoint(coder: Encoder | Decoder) -> str:
    """
    Serializes an encoder or decoder checkpoint to a JSON string.
    """
    if isinstance(coder, Encoder):
        envelope = _envelope(ENCODER_KIND, coder.snapshot())
    elif isinstance(coder, Decoder):
        envelope = _envelope(DECODER_KIND, coder.snapshot())
    else:
        msg = f"Cannot checkpoint object of type {type(coder).__name__}"
        raise TypeError(msg)
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True)


def loads_checkpoint(data: str | bytes) -> Encoder | Decoder:
    """
    Rebuilds an encoder or decoder from a checkpoint string produced by
    :func:`dumps_checkpoint`.
    """
    try:
        envelope = json.loads(data)
    except (ValueError, TypeError) as err:
        msg = "HPACK checkpoint is not valid JSON"
        raise InvalidTableStateError(msg) from err

    if not isinstance(envelope, dict) or "kind" not in envelope or "state" not in envelope:
        msg = "HPACK checkpoint is missing its kind or state envelope"
        raise InvalidTableStateError(msg)

    kind = envelope["kind"]
    state = envelope["state"]
    if kind == ENCODER_KIND:
        coder: Encoder | Decoder = Encoder()
        expected_version = ENCODER_STATE_VERSION
    elif kind == DECODER_KIND:
        coder = Decoder()
        expected_version = DECODER_STATE_VERSION
    else:
        msg = f"Unknown HPACK checkpoint kind: {kind!r}"
        raise InvalidTableStateError(msg)

    if not isinstance(state, dict) or state.get("version") != expected_version:
        msg = f"Unsupported {kind} checkpoint version"
        raise InvalidTableStateError(msg)

    coder.restore(state)  # type: ignore[arg-type]
    return coder


def save_checkpoint(path: str | PathLike[str], coder: Encoder | Decoder) -> None:
    """
    Writes a checkpoint for ``coder`` to ``path`` atomically enough for a
    restarted batch process to pick it up.
    """
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(dumps_checkpoint(coder), encoding="utf-8")
    temporary.replace(target)


def load_checkpoint(path: str | PathLike[str]) -> Encoder | Decoder:
    """
    Loads a checkpoint previously written by :func:`save_checkpoint`.
    """
    return loads_checkpoint(Path(path).read_text(encoding="utf-8"))
