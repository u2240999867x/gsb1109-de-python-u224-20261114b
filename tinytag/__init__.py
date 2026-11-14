# SPDX-FileCopyrightText: 2014-2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""Audio file metadata reader."""

__version__ = '2.3.2'

from .editlist import (
    EditEntry, EditList, EditListEntry, EditRecord, EditTrack,
    SUPPORTED_MEDIA_RATES, build_edit_list, build_edit_record,
    build_edit_track, edit_list_table, expand_edit_list,
    parse_edit_list, parse_elst, parse_elst_entries, read_edit_list,
)
from .tinytag import (
    TinyTag, Image, Images, OtherFields, OtherImages,
    TinyTagException, ParseError, UnsupportedFormatError
)
__all__ = (
    "TinyTag", "Image", "Images", "OtherFields", "OtherImages",
    "TinyTagException", "ParseError", "UnsupportedFormatError",
    "EditEntry", "EditList", "EditListEntry", "EditRecord", "EditTrack",
    "SUPPORTED_MEDIA_RATES",
    "build_edit_list", "build_edit_record", "build_edit_track",
    "edit_list_table", "expand_edit_list", "parse_edit_list",
    "parse_elst", "parse_elst_entries", "read_edit_list"
)
