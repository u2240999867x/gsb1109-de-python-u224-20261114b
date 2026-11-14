"""Main webvtt package."""

__version__ = '0.8.0'

from .webvtt import WebVTT
from . import segmenter
from .corpus import (  # noqa
    CorpusIndex, CueRef, CueSpan, RubyAnnotation, index_captions
    )
from .models import (  # noqa
    Caption, CueSettings, ParseIssue, Region, Style, ValidationIssue
    )

__all__ = [
    'WebVTT', 'Caption', 'CorpusIndex', 'CueRef', 'CueSpan',
    'CueSettings', 'ParseIssue', 'Region', 'RubyAnnotation', 'Style',
    'ValidationIssue', 'index_captions'
    ]

read = WebVTT.read
read_buffer = WebVTT.read_buffer
from_buffer = WebVTT.from_buffer
from_srt = WebVTT.from_srt
from_sbv = WebVTT.from_sbv
from_string = WebVTT.from_string
segment = segmenter.segment
