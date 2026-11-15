Common Use Cases
================

tinycss2 has been created for WeasyPrint_, and many common use cases can thus
be found in `its repository`_.

.. _WeasyPrint: https://weasyprint.org/
.. _its repository: https://github.com/Kozea/WeasyPrint


Parsing Stylesheets
-------------------

Parsing whole stylesheets is done using :func:`tinycss2.parse_stylesheet` and
:func:`tinycss2.parse_stylesheet_bytes`.

When CSS comes as Unicode strings, :func:`tinycss2.parse_stylesheet` is the
function to use. If you know for sure how to decode CSS bytes, or if you want
to parse CSS in Unicode strings, you can safely use this function to get rules
out of CSS text.

.. code-block:: python

   tinycss2.parse_stylesheet('body div { width: 50% }')
   # [<QualifiedRule … { … }>]

:func:`tinycss2.parse_stylesheet` has two extra parameters: ``skip_comments``
if you do not want to generate comment tokens at the top-level of the
stylesheet, and ``skip_whitespace`` if you want to ignore top-level extra
whitespaces.

In many cases, it is hard to know which charset to use. When downloading
stylesheets from an online HTML document, you may have a :abbr:`BOM (Byte Order
Mark)`, a ``@charset`` rule, a protocol encoding defined by HTTP and an
`environment encoding
<https://www.w3.org/TR/css-syntax/#environment-encoding>`_.

tinycss2 provides :func:`tinycss2.parse_stylesheet_bytes` that knows how to
handle these different hints. Use it when your CSS is stored offline or online
in a file, it may solve many decoding problems for you.

.. code-block:: python

   with open('file.css', 'rb') as fd:
       css = fd.read()
   tinycss2.parse_stylesheet_bytes(css)
   # [<QualifiedRule … { … }>]

:func:`tinycss2.parse_stylesheet_bytes` allows two extra optional arguments:
``protocol_encoding`` that may be provided by your network protocol, and
``environment_encoding`` used as a fallback encoding if the other ones failed.


Parsing Rules
-------------

Parsing a list of declarations is possible from a list of tokens (given by the
``content`` attribute of :func:`tinycss2.parse_stylesheet` rules) or from a
string (given by the ``style`` attribute of an HTML element, for example).

The high-level function used to parse declarations is
:func:`tinycss2.parse_blocks_contents`.

.. code-block:: python

   rules = tinycss2.parse_stylesheet('body div {width: 50%;height: 50%}')
   tinycss2.parse_blocks_contents(rules[0].content)
   # [<Declaration width: …>, <Declaration height: …>]

   tinycss2.parse_blocks_contents('width: 50%;height: 50%')
   # [<Declaration width: …>, <Declaration height: …>]

You can then get the name and value of each declaration:

.. code-block:: python

   declarations = tinycss2.parse_blocks_contents('width: 50%;height: 50%')
   declarations[0].name, declarations[0].value
   # ('width', [<WhitespaceToken>, <PercentageToken 50%>])

This function has the same ``skip_comments`` and ``skip_whitespace`` parameters
as the ``parse_stylesheet*`` functions.


Cleaning declarations
---------------------

Declaration names may be written with escapes, as in ``\63 olor: red``.
:func:`~tinycss2.declaration.declaration_key` returns the logical name to use
for grouping: ordinary properties are ASCII case-insensitive, while custom
properties starting with ``--`` stay case-sensitive.

.. code-block:: python

   from tinycss2.declaration import declaration_key, declaration_raw_name

   declaration = tinycss2.parse_one_declaration(r'c\6flor: red')
   declaration_key(declaration)
   # 'color'
   declaration_raw_name(declaration)
   # 'c\\6flor'

.. code-block:: python

   declarations = tinycss2.parse_blocks_contents(
       'COLOR: red; color: blue; --Foo: 1; --foo: 2', skip_whitespace=True)
   for declaration in declarations:
       print(declaration_key(declaration), declaration.name)
   # color COLOR
   # color color
   # --Foo --Foo
   # --foo --foo

:func:`~tinycss2.declaration.clean_declarations` parses a declaration list and
resolves duplicates within that scope. Later declarations win, except that an
``!important`` declaration is not overridden by a later normal one:

.. code-block:: python

   cleaned = tinycss2.clean_declarations(
       'COLOR: red; color: blue; color: green !important')
   [(d.name, d.raw_name) for d in cleaned]
   # [('color', 'color')]
   tinycss2.serialize(cleaned)
   # 'color: green !important;'

For a whole stylesheet, :func:`~tinycss2.declaration.clean_stylesheet` resolves
duplicates independently in each rule. Use
:func:`~tinycss2.declaration.reconcile_declarations` to re-export cleaned
declarations, tokenize them again, and check that logical keys, order and
values still match; its ``errors`` list is empty when reconciliation succeeds.


Serializing
-----------

tinycss2 is also able to generate CSS strings out of abstact Python trees. You
can use :func:`tinycss2.ast.Node.serialize` to generate a CSS string that would
give the same serialization as the original string.

.. code-block:: python

   rules = tinycss2.parse_stylesheet('body div { width: 50% }')
   rule = rules[0]

   print(rule.serialize())
   # 'body div { width: 50% }'
