# SPDX-FileCopyrightText: 2014-2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""Audio file metadata reader."""

__version__ = '2.3.2'

from .credits import (
    ALBUM_SCOPE, TRACK_SCOPE, Credit, CreditIndex, Credits, RoleSpec,
    extract_credits
)
from .tinytag import (
    TinyTag, Image, Images, OtherFields, OtherImages,
    TinyTagException, ParseError, UnsupportedFormatError
)
__all__ = (
    "ALBUM_SCOPE", "TRACK_SCOPE", "Credit", "CreditIndex", "Credits",
    "RoleSpec", "extract_credits",
    "TinyTag", "Image", "Images", "OtherFields", "OtherImages",
    "TinyTagException", "ParseError", "UnsupportedFormatError"
)
