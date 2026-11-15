API Reference
=============


Parsing
-------

tinycss2 is “low-level” in that it doesn’t parse all of CSS: it doesn’t know
about the syntax of any specific properties or at-rules. Instead, it provides
a set of functions that can be composed to support exactly the parts of CSS
you’re interested in, including new or non-standard rules or properties,
without modifying tinycss2 or having a complex hook/plugin system.

In many cases, parts of the parsed values (such as the
:attr:`~tinycss2.ast.AtRule.content` of a :class:`~tinycss2.ast.AtRule`) is
given as :term:`component values` that can be parsed further with other
functions.

.. module:: tinycss2
.. autofunction:: parse_stylesheet_bytes
.. autofunction:: parse_stylesheet
.. autofunction:: parse_rule_list
.. autofunction:: parse_one_rule
.. autofunction:: parse_blocks_contents
.. autofunction:: parse_declaration_list
.. autofunction:: parse_one_declaration
.. autofunction:: parse_component_value_list
.. autofunction:: parse_one_component_value


Serialization
-------------

In addition to each node’s a :meth:`~tinycss2.ast.Node.serialize` method, some
serialization-related functions are available:

.. autofunction:: serialize
.. autofunction:: serialize_identifier


Custom properties
-----------------

These optional helpers resolve ``var()`` references and select resource URLs
from declarations that have already been matched to a component.  They keep
the stylesheet base URL of every token, so a URL supplied by a custom property
in another stylesheet is resolved against that stylesheet.

.. module:: tinycss2.variables
.. autofunction:: resolve_variables
.. autofunction:: resolve_declaration
.. autofunction:: resolve_resource
.. autofunction:: cascade_custom_properties
.. autoclass:: Resolver
   :members:
.. autoclass:: Declaration
.. autoclass:: Resolution
.. autoclass:: FallbackUsage


Color Level 3
-------------

.. module:: tinycss2.color3
.. autofunction:: parse_color
.. autoclass:: RGBA


Color Level 4
-------------

.. module:: tinycss2.color4
.. autofunction:: parse_color
.. autoclass:: Color
   :members:
.. autodata:: COLOR_SPACES
.. autodata:: D50
.. autodata:: D65

Color Level 5
-------------

.. module:: tinycss2.color5
.. autofunction:: parse_color
.. autoclass:: Color
   :show-inheritance:
.. autodata:: COLOR_SPACES
.. autodata:: D50
.. autodata:: D65


<An+B>
------

.. module:: tinycss2.nth
.. autofunction:: parse_nth
.. autofunction:: serialize_nth


AST nodes
---------

.. module:: tinycss2.ast

Various parsing functions return a **node** or a list of nodes. Some types of
nodes contain nested nodes which may in turn contain more nodes, forming
together an **abstract syntax tree**.

Although you typically don’t need to import it, the :mod:`tinycss2.ast` module
defines a class for every type of node.

.. autoclass:: Node()

.. autoclass:: QualifiedRule()
.. autoclass:: AtRule()
.. autoclass:: Declaration()

.. autoclass:: ParseError()
.. autoclass:: Comment()
.. autoclass:: WhitespaceToken()
.. autoclass:: LiteralToken()
.. autoclass:: IdentToken()
.. autoclass:: AtKeywordToken()
.. autoclass:: HashToken()
.. autoclass:: StringToken()
.. autoclass:: URLToken()
.. autoclass:: UnicodeRangeToken()
.. autoclass:: NumberToken()
.. autoclass:: PercentageToken()
.. autoclass:: DimensionToken()
.. autoclass:: ParenthesesBlock()
.. autoclass:: SquareBracketsBlock()
.. autoclass:: CurlyBracketsBlock()
.. autoclass:: FunctionBlock()


Glossary
--------

.. currentmodule:: tinycss2.ast
.. glossary::

    component value
    component values
        A :class:`ParseError`,
        :class:`WhitespaceToken`,
        :class:`LiteralToken`,
        :class:`IdentToken`,
        :class:`AtKeywordToken`,
        :class:`HashToken`,
        :class:`StringToken`,
        :class:`URLToken`,
        :class:`NumberToken`,
        :class:`PercentageToken`,
        :class:`DimensionToken`,
        :class:`UnicodeRangeToken`,
        :class:`ParenthesesBlock`,
        :class:`SquareBracketsBlock`,
        :class:`CurlyBracketsBlock`,
        :class:`FunctionBlock`,
        or :class:`Comment`
        object.
