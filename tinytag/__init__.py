# SPDX-FileCopyrightText: 2014-2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""Audio file metadata reader."""

__version__ = '2.3.2'

from .tinytag import (
    TinyTag, Image, Images, OtherFields, OtherImages,
    TinyTagException, ParseError, UnsupportedFormatError
)
from .bwf import (
    Reel, ReelEntry, ReelRelation, ReelLedger,
    build_ledger, candidate_ledgers,
)
__all__ = (
    "TinyTag", "Image", "Images", "OtherFields", "OtherImages",
    "TinyTagException", "ParseError", "UnsupportedFormatError",
    "Reel", "ReelEntry", "ReelRelation", "ReelLedger",
    "build_ledger", "candidate_ledgers",
)
