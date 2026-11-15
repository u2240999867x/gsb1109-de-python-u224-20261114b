"""
tinycss2
========

tinycss2 is a low-level CSS parser and generator: it can parse strings, return
Python objects representing tokens and blocks, and generate CSS strings
corresponding to these objects.

"""

from .bytes import (  # noqa
    BOM, CHARSET, DEFAULT, ENCODING_SOURCES, ENVIRONMENT, PROTOCOL,
    DecodedStylesheet, EncodingDecision, decode_stylesheet_bytes,
    decode_stylesheet_bytes_detailed, determine_encoding,
    parse_stylesheet_bytes, parse_stylesheet_bytes_detailed)
from .incremental import (  # noqa
    CollectionResult, ParseEvent, ParseKey, ParseRecord, SourceBytes,
    StylesheetCollector, extract_identifiers, extract_resource_references)
from .parser import (  # noqa
    parse_blocks_contents, parse_declaration_list, parse_one_component_value,
    parse_one_declaration, parse_one_rule, parse_rule_list, parse_stylesheet)
from .serializer import serialize, serialize_identifier  # noqa
from .tokenizer import parse_component_value_list  # noqa

VERSION = __version__ = '1.5.1'
