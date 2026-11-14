"""CSS counter engine: scopes, reset/set/increment and pseudo read timing.

The engine follows the CSS Lists and Counters model:

* ``counter-reset`` opens a new *scope* (a new counter instance),
* ``counter-set`` writes the innermost instance, creating one if needed,
* ``counter-increment`` updates the innermost instance (default step ``1``),
  implicitly creating a zero-valued instance first,
* ``reversed()`` counters count down; without an explicit start value the
  start value is the number of elements that increment the counter inside
  the scope (computed in a first pass),
* ``::before`` is evaluated as the first child of its originating element
  and ``::after`` as the last child, so both read the counter state at the
  correct point in document order.
"""

from .cssmodel import parse_counter_property
from .htmltree import Element

COUNTER_PROPERTIES = (
    'counter-reset', 'counter-set', 'counter-increment')


class CounterInstance:
    __slots__ = ('owner_id', 'value', 'reversed')

    def __init__(self, owner_id, value, reversed=False):
        self.owner_id = owner_id
        self.value = value
        self.reversed = reversed

    def __repr__(self):
        return 'CounterInstance(owner=%d, value=%d%s)' % (
            self.owner_id, self.value,
            ', reversed' if self.reversed else '')


class ElementCounterOps:
    """Counter operations applicable to one element or pseudo-element."""

    __slots__ = ('reset', 'set', 'increment')

    def __init__(self):
        self.reset = []
        self.set = []
        self.increment = []

    def __bool__(self):
        return bool(self.reset or self.set or self.increment)


def collect_counter_ops(stylesheet, element, pseudo=None):
    """Extract the cascaded counter operations for a node or pseudo-node."""
    style = stylesheet.effective_style(element, pseudo)
    ops = ElementCounterOps()
    for property_name, target in (
            ('counter-reset', ops.reset),
            ('counter-set', ops.set),
            ('counter-increment', ops.increment)):
        matched = style.get(property_name)
        if matched is None:
            continue
        parsed = parse_counter_property(matched.declaration)
        if parsed:
            target.extend(parsed)
    return ops


class CounterEngine:
    """Two-pass engine producing counter states at every read point."""

    def __init__(self, document, stylesheet):
        self.document = document
        self.stylesheet = stylesheet
        self._increment_tally = {}
        self._count_increments(document.root)

    # ------------------------------------------------------------------
    # Pass 1: count, per scope owner, how many elements increment a counter.
    # This gives reversed() counters their default start value.
    # ------------------------------------------------------------------
    def _count_increments(self, root):
        owners = {}

        def walk(element):
            ops = collect_counter_ops(self.stylesheet, element)
            pushed = []
            for operation in ops.reset:
                owners.setdefault(operation.name, []).append(element.doc_id)
                pushed.append(operation.name)
            for operation in ops.set:
                stack = owners.get(operation.name)
                if not stack:
                    owners.setdefault(operation.name, []).append(
                        element.doc_id)
            for operation in ops.increment:
                stack = owners.get(operation.name)
                if not stack:
                    owners.setdefault(operation.name, []).append(
                        element.doc_id)
                    stack = owners[operation.name]
                owner = stack[-1]
                bucket = self._increment_tally.setdefault(owner, {})
                bucket[operation.name] = bucket.get(operation.name, 0) + 1
            for pseudo in ('before', 'after'):
                pseudo_ops = collect_counter_ops(
                    self.stylesheet, element, pseudo)
                for operation in pseudo_ops.increment:
                    stack = owners.get(operation.name)
                    if not stack:
                        continue
                    owner = stack[-1]
                    bucket = self._increment_tally.setdefault(owner, {})
                    bucket[operation.name] = \
                        bucket.get(operation.name, 0) + 1
            for child in element.children:
                if isinstance(child, Element):
                    walk(child)
            for name in pushed:
                owners[name].pop()

        walk(root)

    def reversed_start(self, owner_id, name):
        """Default start value for a reversed counter without explicit value."""
        return self._increment_tally.get(owner_id, {}).get(name, 0)

    # ------------------------------------------------------------------
    # Pass 2: walk the tree maintaining name -> [CounterInstance, ...].
    # Scopes are pushed on counter-reset and popped when the owning element
    # (or pseudo-element) finishes, so an ::after reads the state after all
    # descendant operations.
    # ------------------------------------------------------------------
    def run(self, reader):
        """Walk the document, calling ``reader`` at each pseudo read point."""
        self._walk(self.document.root, {}, reader)

    def _walk(self, element, state, reader):
        ops = collect_counter_ops(self.stylesheet, element)
        pushed = self._apply_resets(element, ops, state)
        try:
            self._apply_sets(ops, state, element.doc_id)
            deferred = self._apply_increments(
                ops, state, element.doc_id, defer_reversed=True)

            before_ops = collect_counter_ops(
                self.stylesheet, element, 'before')
            before_pushed = self._apply_resets(
                None, before_ops, state, element.doc_id)
            self._apply_sets(before_ops, state, element.doc_id)
            self._apply_increments(
                before_ops, state, element.doc_id, defer_reversed=False)
            reader(element, 'before', state)

            # Reversed counters are decremented after the ::before marker
            # read, so the marker shows the pre-decrement value (N..1).
            self._apply_deferred(deferred, state)

            for child in element.children:
                if isinstance(child, Element):
                    self._walk(child, state, reader)

            after_ops = collect_counter_ops(
                self.stylesheet, element, 'after')
            after_pushed = self._apply_resets(
                None, after_ops, state, element.doc_id)
            self._apply_sets(after_ops, state, element.doc_id)
            self._apply_increments(
                after_ops, state, element.doc_id, defer_reversed=False)
            reader(element, 'after', state)
            for name in after_pushed:
                state[name].pop()
                if not state[name]:
                    del state[name]
            for name in before_pushed:
                state[name].pop()
                if not state[name]:
                    del state[name]
        finally:
            for name in pushed:
                state[name].pop()
                if not state[name]:
                    del state[name]

    def _apply_resets(self, element, ops, state, fallback_owner=None):
        owner_id = element.doc_id if element is not None else fallback_owner
        pushed = []
        for operation in ops.reset:
            if operation.value is not None:
                value = operation.value
            elif operation.reversed:
                value = self.reversed_start(owner_id, operation.name)
            else:
                value = 0
            state.setdefault(operation.name, []).append(
                CounterInstance(owner_id, value, operation.reversed))
            pushed.append(operation.name)
        return pushed

    def _apply_sets(self, ops, state, owner_id):
        for operation in ops.set:
            value = operation.value if operation.value is not None else 0
            instances = state.get(operation.name)
            if instances:
                instances[-1].value = value
            else:
                state.setdefault(operation.name, []).append(
                    CounterInstance(owner_id, value))

    def _apply_increments(self, ops, state, owner_id, defer_reversed):
        """Apply increments.

        Ordinary increments take effect before the ``::before`` read.
        A reversed counter's own decrement is deferred until after that read
        when ``defer_reversed`` is true (element-level processing only).
        """
        deferred = []
        for operation in ops.increment:
            instances = state.setdefault(operation.name, [])
            if not instances:
                instances.append(CounterInstance(owner_id, 0))
            target = instances[-1]
            if operation.value is not None:
                step = operation.value
            else:
                step = -1 if target.reversed else 1
            if defer_reversed and target.reversed and step < 0:
                deferred.append((operation.name, step))
            else:
                target.value += step
        return deferred

    def _apply_deferred(self, deferred, state):
        for name, step in deferred:
            target = state[name][-1]
            target.value = max(0, target.value + step)

    # ------------------------------------------------------------------
    # Reads used when evaluating the content property.
    # ------------------------------------------------------------------
    @staticmethod
    def counter_value(state, name):
        instances = state.get(name)
        if not instances:
            return 0
        return instances[-1].value

    @staticmethod
    def counter_values(state, name):
        return [instance.value for instance in state.get(name, [])]


# ---------------------------------------------------------------------------
# Counter styles (the formats accepted by counter() / counters()).
# ---------------------------------------------------------------------------

_ROMAN = (
    (1000, 'm'), (900, 'cm'), (500, 'd'), (400, 'cd'),
    (100, 'c'), (90, 'xc'), (50, 'l'), (40, 'xl'),
    (10, 'x'), (9, 'ix'), (5, 'v'), (4, 'iv'), (1, 'i'),
)

_GREEK = 'αβγδεζηθικλμνξοπρστυφχψω'


def format_counter(value, style='decimal'):
    """Render an integer counter value with the given CSS counter-style."""
    if style in ('decimal', 'arabic-indic', 'none'):
        if style == 'none':
            return ''
        return str(value)
    if style == 'decimal-leading-zero':
        text = str(abs(value))
        if len(text) < 2:
            text = '0' + text
        return ('-' if value < 0 else '') + text
    if style in ('lower-alpha', 'lower-latin'):
        return _alphabetic(abs(value), 'abcdefghijklmnopqrstuvwxyz', value < 0)
    if style in ('upper-alpha', 'upper-latin'):
        return _alphabetic(abs(value), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', value < 0)
    if style == 'lower-roman':
        return ('-' if value < 0 else '') + _roman(abs(value)).lower()
    if style == 'upper-roman':
        return ('-' if value < 0 else '') + _roman(abs(value)).upper()
    if style == 'lower-greek':
        return _alphabetic(abs(value), _GREEK, value < 0)
    return str(value)


def _alphabetic(value, letters, negative):
    if value <= 0:
        return ''
    base = len(letters)
    result = ''
    while value > 0:
        value, remainder = divmod(value - 1, base)
        result = letters[remainder] + result
    return ('-' if negative else '') + result


def _roman(value):
    if value <= 0:
        return ''
    result = ''
    for number, numeral in _ROMAN:
        while value >= number:
            result += numeral
            value -= number
    return result
