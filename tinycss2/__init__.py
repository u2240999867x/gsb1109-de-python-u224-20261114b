"""
tinycss2
========

tinycss2 is a low-level CSS parser and generator: it can parse strings, return
Python objects representing tokens and blocks, and generate CSS strings
corresponding to these objects.

"""

from .bytes import parse_stylesheet_bytes  # noqa
from .parser import (  # noqa
    parse_blocks_contents, parse_declaration_list, parse_one_component_value,
    parse_one_declaration, parse_one_rule, parse_rule_list, parse_stylesheet)
from .serializer import serialize, serialize_identifier  # noqa
from .tokenizer import parse_component_value_list  # noqa
from .variables import (  # noqa
    Resolution, Resolver, cascade_custom_properties, resolve_declaration,
    resolve_resource, resolve_variables)

VERSION = __version__ = '1.5.1'
