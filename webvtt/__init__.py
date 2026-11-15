"""Main webvtt package."""

__version__ = '0.8.0'

from .webvtt import WebVTT
from . import segmenter
from .models import (  # noqa
    Caption, CueSettings, ParseIssue, Region, Style, ValidationIssue
    )
from .corpus import Corpus, Mode, modes_from_config  # noqa
from .css import (  # noqa
    Declaration, Selector, parse_declarations, parse_selector, parse_styles
    )

__all__ = [
    'WebVTT', 'Caption', 'CueSettings', 'ParseIssue', 'Region', 'Style',
    'ValidationIssue', 'Corpus', 'Mode', 'Declaration', 'Selector',
    'parse_styles', 'parse_selector', 'parse_declarations',
    'modes_from_config'
    ]

read = WebVTT.read
read_buffer = WebVTT.read_buffer
from_buffer = WebVTT.from_buffer
from_srt = WebVTT.from_srt
from_sbv = WebVTT.from_sbv
from_string = WebVTT.from_string
segment = segmenter.segment
