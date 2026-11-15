__all__ = [
    "__version__",
    "utils",
    "dimension",
    "dimensions",
    "pdfminer",
    "open",
    "repair",
    "set_debug",
    "build_dimension_index",
    "build_dimension_pairs",
    "collect_dimension_texts",
    "dimension_index",
    "dimension_pairs",
    "find_dimensions",
    "link_dimensions",
    "match_dimensions",
    "parse_dimension",
    "parse_dimension_text",
    "segment_endpoints",
    "text_angle",
    "text_direction",
]

import pdfminer
import pdfminer.pdftypes

from . import dimension, dimensions, utils
from .dimensions import (
    build_dimension_index,
    build_dimension_pairs,
    collect_dimension_texts,
    dimension_index,
    dimension_pairs,
    find_dimensions,
    link_dimensions,
    match_dimensions,
    parse_dimension,
    parse_dimension_text,
    segment_endpoints,
    text_angle,
    text_direction,
)
from ._version import __version__
from .pdf import PDF
from .repair import repair

open = PDF.open
