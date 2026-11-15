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


Resolving Custom Properties
---------------------------

When declarations matched to a component are already known, the helpers in
:mod:`tinycss2.variables` select the winner, substitute ``var()`` references,
and report which variables and fallbacks were used.  URLs keep the base URL of
the stylesheet that supplied them.

.. code-block:: python

   from tinycss2.variables import Declaration, resolve_resource

   declarations = [
       Declaration('background-image', 'url(grey.png)',
                   'https://example.com/css/base.css'),
       Declaration('background-image', 'var(--brand)',
                   'https://example.com/css/product.css'),
   ]
   custom_properties = [
       Declaration('--brand', 'url(brand.png)',
                   'https://cdn.example.com/theme/tokens.css'),
   ]

   resolution = resolve_resource(
       declarations, custom_properties, name='background-image')
   resolution.url
   # 'https://cdn.example.com/theme/brand.png'
   resolution.used_variables
   # ('--brand',)
   resolution.used_fallbacks
   # ()

A fallback is reported only when it was actually used; URLs in unused
fallbacks and in losing declarations are not included in
:attr:`~tinycss2.variables.Resolution.urls`.  Self and mutual references make
the value invalid, and an invalid value can fall back to another value.

