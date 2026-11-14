# SPDX-FileCopyrightText: 2014-2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""Audio file metadata reader."""

__version__ = '2.3.2'

from .replaygain import (
    AlbumGainReport, TrackGainInfo, TrackTags,
    STANDARD_REFERENCE_LOUDNESS_DB,
    compute_album_gain, format_report, parse_gain_db, parse_peak,
    track_tags_from_tinytag
)
from .tinytag import (
    TinyTag, Image, Images, OtherFields, OtherImages,
    TinyTagException, ParseError, UnsupportedFormatError
)
__all__ = (
    "TinyTag", "Image", "Images", "OtherFields", "OtherImages",
    "TinyTagException", "ParseError", "UnsupportedFormatError",
    "AlbumGainReport", "TrackGainInfo", "TrackTags",
    "STANDARD_REFERENCE_LOUDNESS_DB",
    "compute_album_gain", "format_report", "parse_gain_db", "parse_peak",
    "track_tags_from_tinytag"
)
