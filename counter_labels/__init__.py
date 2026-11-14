"""Offline labeling of CSS-generated clause markers.

The package reconstructs, without a browser, the counter values that
``::before`` / ``::after`` boxes render via the ``content`` property.  It
combines three offline artifacts:

* an offline DOM (:mod:`counter_labels.htmltree`),
* the tinycss2 parse result and the declarations that matched each node
  (:mod:`counter_labels.cssmodel`),
* a CSS counter engine supporting nested scopes, ``counter-reset``,
  ``counter-set``, ``counter-increment`` (including reversed counters) and
  the read timing of pseudo-elements (:mod:`counter_labels.counters`).
"""

from .cssmodel import Stylesheet
from .diff import LabelingDiff, diff_labelings
from .htmltree import Element, TextNode, parse_html
from .labeling import Label, Labeler, LabelingResult

__all__ = [
    'Element',
    'TextNode',
    'parse_html',
    'Stylesheet',
    'Labeler',
    'LabelingResult',
    'Label',
    'LabelingDiff',
    'diff_labelings',
]
