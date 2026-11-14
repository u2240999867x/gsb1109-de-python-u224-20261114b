# SPDX-FileCopyrightText: 2014-2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""Compatibility module for :mod:`tinytag.editlist`."""

from .editlist import *  # noqa: F401, F403
from .editlist import (  # noqa: F401
    EditEntry, EditList, EditListEntry, EditRecord, EditTrack,
    SUPPORTED_MEDIA_RATES, build_edit_list, build_edit_record,
    build_edit_track, edit_list_table, expand_edit_list, handler_media_type,
    parse_edit_list, parse_elst, parse_elst_entries,
    parse_handler_type, parse_movie_header, parse_track_header,
    read_edit_list,
)
from .editlist import __all__  # noqa: F401
