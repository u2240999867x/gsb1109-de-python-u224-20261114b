"""CSS selectors and declarations of WebVTT cue styles."""

import re
import typing
from collections.abc import Sequence
from dataclasses import dataclass

# a character of a CSS identifier, or an escape, as in ``c\+1`` or ``\31 a``.
# Each character matches one alternative only, and a hex escape takes all
# its digits, so that a selector that does not match fails in linear time
_CSS_IDENTIFIER = (
    r'(?:[a-zA-Z0-9_-]|[^\x00-\x7f]|'
    r'\\(?:(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{1,5}(?![0-9a-fA-F]))[ \t\n\r\f]?|'
    r'[^\n\r\f0-9a-fA-F]))+'
    )

_CSS_CLASS = re.compile(rf'\.({_CSS_IDENTIFIER})')

_CSS_ID = re.compile(rf'#({_CSS_IDENTIFIER})')

_CSS_TYPE = re.compile(r'[a-zA-Z][a-zA-Z0-9_-]*')

_CSS_ESCAPE = re.compile(
    r'\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))', re.DOTALL
    )

# an attribute selector, with a quoted or unquoted value
_CSS_ATTRIBUTE = re.compile(
    rf"""\s*({_CSS_IDENTIFIER})\s*
    (?:
        (~=|\|=|\^=|\$=|\*=|=)\s*
        (?:
            "((?:\\.|[^"\\])*)" |
            '((?:\\.|[^'\\])*)' |
            ({_CSS_IDENTIFIER})
        )
        \s*([iIsS])?
    )?
    """,
    re.VERBOSE | re.DOTALL
    )

_CSS_LANG = re.compile(r':lang\(\s*([^)]*?)\s*\)')

_CSS_PSEUDO = re.compile(r'::?[a-zA-Z-]+')

# the part of a ``::cue`` selector that follows the pseudo-element
_CUE_SELECTOR = re.compile(r'\s*::cue\s*(?:\((.*)\))?\s*', re.DOTALL)

# the visibility values that hide a word
HIDING_VISIBILITY = ('hidden', 'collapse')

# the CSS-wide keywords, which do not state a value of their own
_CSS_WIDE_KEYWORDS = ('inherit', 'initial', 'unset', 'revert', 'revert-layer')


@dataclass(frozen=True)
class Declaration:
    """A declaration of a CSS rule."""

    name: str
    value: str
    important: bool


@dataclass(frozen=True)
class Attribute:
    """An attribute selector of a compound selector."""

    name: str
    operator: str | None
    value: str | None
    insensitive: bool


@dataclass(frozen=True)
class Compound:
    """A compound selector, as in ``c.loud[voice="Esme"]``."""

    tag: str | None
    universal: bool
    ids: tuple[str, ...]
    classes: frozenset[str]
    attributes: tuple[Attribute, ...]
    languages: tuple[str, ...]
    pseudo_classes: int
    pseudo_element: bool
    impossible: bool


@dataclass(frozen=True)
class Selector:
    """
    A selector of a style rule.

    ``compounds`` is empty for ``::cue``, which selects the root of the
    cue. ``supported`` tells whether the selector is one of the simple cue
    selectors whose matching is exact, ``matches_nothing`` whether it
    cannot select a cue text element at all, and ``unreadable`` whether it
    could not be parsed, so that it may match any element.
    """

    raw: str
    compounds: tuple[Compound, ...]
    combinators: tuple[str, ...]
    specificity: tuple[int, int, int]
    supported: bool
    matches_nothing: bool
    unreadable: bool = False

    def is_root(self) -> bool:
        """Return whether the selector is ``::cue``."""
        return (not self.compounds and not self.matches_nothing and
                not self.unreadable)


@dataclass(frozen=True)
class Rule:
    """A CSS rule with its selectors and declarations."""

    selectors: tuple[Selector, ...]
    declarations: tuple[Declaration, ...]
    order: int


class Element(typing.Protocol):
    """The cue text element that selectors are matched against."""

    tag: str | None
    classes: frozenset[str]
    voice: str | None
    language: str | None
    parent: 'Element | None'
    previous: Sequence['Element']


def strip_comments(css: str) -> str:
    """
    Remove the comments from CSS.

    A comment without an end runs to the end of the CSS. This scans for
    the comments with ``str.find``, as a non-greedy regex for them rescans
    the rest of the CSS from every comment start without an end, which is
    quadratic in the length of the CSS.

    :param css: the CSS text
    :returns: the CSS without comments
    """
    parts = []
    pos = 0
    while True:
        start = css.find('/*', pos)
        if start < 0:
            break
        parts.append(css[pos:start])
        end = css.find('*/', start + 2)
        if end < 0:
            return ''.join(parts)
        pos = end + 2
    parts.append(css[pos:])
    return ''.join(parts)


def unescape(identifier: str) -> str:
    r"""
    Return a CSS identifier without its escapes.

    :param identifier: the identifier, as in ``c\+1`` or ``\31 a``
    :returns: the identifier, as in ``c+1`` or ``1a``
    """
    def replace(match: re.Match[str]) -> str:
        if match.group(2) is not None:
            return match.group(2)
        code = int(match.group(1), 16)
        if code == 0 or 0xd800 <= code <= 0xdfff or code > 0x10ffff:
            return '�'
        return chr(code)

    return _CSS_ESCAPE.sub(replace, identifier)


def parse_declarations(text: str) -> tuple[Declaration, ...]:
    """
    Return the declarations of a rule.

    The name is lowered, and ``!important`` is read whatever its case, but
    the value is kept as written.

    :param text: the declarations, as in ``color: red; display: none``
    :returns: the declarations, in their order, without those that are
        empty or have no name
    """
    declarations = []
    for declaration in text.split(';'):
        name, separator, value = declaration.partition(':')
        if not separator:
            continue
        name, value = name.strip().lower(), value.strip()
        important = False
        lowered = value.lower()
        if lowered.endswith('!important'):
            value = value[:-len('!important')].rstrip()
            important = True
        elif lowered.endswith('! important'):
            # a space is allowed after the exclamation mark
            value = value[:-len('! important')].rstrip()
            important = True
        if name and value:
            declarations.append(Declaration(name, value, important))
    return tuple(declarations)


def split_selectors(text: str) -> list[str]:
    """
    Split the selectors of a rule on the commas that are not escaped.

    Unlike a regex for the selectors, this does not rescan the rest of the
    text from every comma that has no selector after it, which is quadratic
    in the length of the text.

    :param text: the selectors of a rule, as in ``::cue(a), ::cue(b)``
    :returns: the selectors, stripped, without empty items
    """
    selectors = []
    current: list[str] = []
    pos = 0
    while pos < len(text):
        char = text[pos]
        if char == '\\':
            current.append(text[pos:pos + 2])
            pos += 2
            continue
        if char == ',':
            selector = ''.join(current).strip()
            if selector:
                selectors.append(selector)
            current = []
        else:
            current.append(char)
        pos += 1
    selector = ''.join(current).strip()
    if selector:
        selectors.append(selector)
    return selectors


def parse_styles(css: str) -> list[Rule]:
    """
    Return the rules of a style sheet.

    The rules are split on braces, as a regex for them is quadratic on text
    without braces. A rule without declarations is left out, as it cannot
    change a word.

    :param css: the CSS text
    :returns: the rules, in the order of the style sheet
    """
    rules: list[Rule] = []
    for text in strip_comments(css).split('}'):
        selectors, separator, declarations = text.rpartition('{')
        if not separator:
            continue
        parsed = parse_declarations(declarations)
        if not parsed:
            continue
        selector_text = split_selectors(selectors)
        if not selector_text:
            continue
        rules.append(Rule(
            tuple(parse_selector(selector) for selector in selector_text),
            parsed,
            len(rules) + 1
            ))
    return rules


def parse_selector(selector: str) -> Selector:
    """
    Return a selector of a rule.

    :param selector: the selector, as in ``::cue(c.loud)``
    :returns: the selector
    """
    match = _CUE_SELECTOR.fullmatch(selector)
    if match is None:
        # a selector of the page, such as ``html::cue`` or ``.class``,
        # cannot select the text of a cue
        return Selector(selector, (), (), (0, 0, 0), False, True)

    argument = match.group(1)
    if argument is None:
        # ::cue selects the root of the cue, which every word inherits from
        return Selector(selector, (), (), (0, 0, 0), True, False)
    if not argument.strip():
        # ::cue() is not a valid selector, so the rule is ignored
        return Selector(selector, (), (), (0, 0, 0), False, True)

    split = _split_complex(argument)
    if split is None:
        return Selector(selector, (), (), (0, 0, 0), False, False, True)
    compounds_text, combinators = split
    compounds = tuple(_parse_compound(text) for text in compounds_text)
    if any(compound is None for compound in compounds):
        return Selector(selector, (), (), (0, 0, 0), False, False, True)

    parsed = typing.cast(tuple[Compound, ...], compounds)
    specificity = (
        sum(len(compound.ids) for compound in parsed),
        sum(len(compound.classes) + len(compound.attributes) +
            len(compound.languages) + compound.pseudo_classes
            for compound in parsed),
        sum(1 for compound in parsed
            if compound.tag is not None and not compound.universal)
        )
    matches_nothing = any(
        compound.ids or compound.pseudo_element or compound.impossible
        for compound in parsed
        )
    supported = (
        len(parsed) == 1 and
        not matches_nothing and
        not parsed[0].pseudo_classes and
        all(attribute.name in ('voice', 'lang')
            for attribute in parsed[0].attributes)
        )
    return Selector(
        selector, parsed, tuple(combinators), specificity, supported,
        matches_nothing
        )


def _split_complex(argument: str) -> tuple[list[str], list[str]] | None:
    """
    Split the argument of a ``::cue()`` selector into compounds.

    Spaces and combinators inside brackets or parentheses are kept, and a
    space around a combinator is not a descendant combinator.

    :param argument: the argument, as in ``c.loud > b``
    :returns: the compounds and the combinators between them, or None when
        the argument cannot be split
    """
    compounds: list[str] = []
    combinators: list[str] = []
    current: list[str] = []
    bracket = paren = 0
    quote = None
    separator: str | None = None

    def flush() -> None:
        if current:
            compounds.append(''.join(current))
            current.clear()

    def start_compound() -> None:
        # the separator before a compound is settled when it starts
        if compounds:
            combinators.append(typing.cast(str, separator))

    index = 0
    while index < len(argument):
        char = argument[index]
        if quote is not None:
            current.append(char)
            if char == quote and argument[index - 1] != '\\':
                quote = None
        elif char in '"\'':
            quote = char
            current.append(char)
        elif char == '[':
            bracket += 1
            current.append(char)
        elif char == ']':
            bracket -= 1
            if bracket < 0:
                return None
            current.append(char)
        elif char == '(':
            paren += 1
            current.append(char)
        elif char == ')':
            paren -= 1
            if paren < 0:
                return None
            current.append(char)
        elif char == '\\' and bracket == 0 and paren == 0:
            # an escaped character, such as the dot or the space of a class
            if index + 1 >= len(argument):
                return None
            if not current:
                start_compound()
            separator = None
            current.append(argument[index:index + 2])
            index += 2
            continue
        elif char in '>+~' and bracket == 0 and paren == 0:
            # a combinator needs a compound before it, and another after it
            if separator not in (None, ' ') or not compounds:
                return None
            flush()
            separator = char
            index += 1
            continue
        elif char.isspace() and bracket == 0 and paren == 0:
            flush()
            if compounds:
                separator = ' ' if separator is None else separator
            index += 1
            continue
        else:
            if not current:
                start_compound()
            separator = None
            current.append(char)
        index += 1

    if quote is not None or bracket or paren:
        return None
    # whitespace after the last compound is fine, a combinator is not
    if separator not in (None, ' '):
        return None
    flush()
    if not compounds or len(combinators) != len(compounds) - 1:
        return None
    return compounds, combinators


def _parse_compound(text: str) -> Compound | None:
    """
    Parse a compound selector.

    :param text: the compound, as in ``c.loud[voice="Esme"]``
    :returns: the compound, or None if it cannot be read
    """
    tag = None
    universal = False
    ids: list[str] = []
    classes: set[str] = set()
    attributes: list[Attribute] = []
    languages: list[str] = []
    pseudo_classes = 0
    pseudo_element = False
    impossible = False
    pos = 0

    if text.startswith('*'):
        universal = True
        pos = 1
    else:
        match = _CSS_TYPE.match(text)
        if match is not None:
            tag = match.group().lower()
            pos = match.end()

    while pos < len(text):
        char = text[pos]
        if char == '.':
            match = _CSS_CLASS.match(text, pos)
            if match is None:
                return None
            classes.add(unescape(match.group(1)))
            pos = match.end()
        elif char == '#':
            match = _CSS_ID.match(text, pos)
            if match is None:
                return None
            ids.append(unescape(match.group(1)))
            pos = match.end()
        elif char == '[':
            end = _closing_bracket(text, pos)
            if end is None:
                return None
            attribute = _parse_attribute(text[pos + 1:end])
            if attribute is None:
                return None
            attributes.append(attribute)
            impossible = impossible or attribute.name not in ('voice', 'lang')
            pos = end + 1
        elif char == ':':
            parsed = _parse_pseudo(text, pos)
            if parsed is None:
                return None
            is_element, language, pos = parsed
            if is_element:
                pseudo_element = True
            elif language is not None:
                languages.append(language)
            else:
                pseudo_classes += 1
        else:
            return None

    if (tag is None and not universal and not ids and not classes and
            not attributes and not languages and not pseudo_classes and
            not pseudo_element):
        return None
    return Compound(
        tag, universal, tuple(ids), frozenset(classes), tuple(attributes),
        tuple(languages), pseudo_classes, pseudo_element, impossible
        )


def _closing_bracket(text: str, start: int) -> int | None:
    """
    Return the index of the bracket that closes an attribute selector.

    :param text: the compound selector
    :param start: the index of the opening bracket
    :returns: the index of the closing bracket, or None if there is none
    """
    quote = None
    index = start + 1
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == quote and text[index - 1] != '\\':
                quote = None
        elif char in '"\'':
            quote = char
        elif char == ']':
            return index
        index += 1
    return None


def _parse_attribute(content: str) -> Attribute | None:
    """
    Parse the content of an attribute selector.

    :param content: the content between the brackets
    :returns: the attribute, or None if it cannot be read
    """
    match = _CSS_ATTRIBUTE.fullmatch(content)
    if match is None:
        return None
    name, operator, double, single, unquoted, flag = match.groups()
    value = next((group for group in (double, single, unquoted)
                  if group is not None), None)
    if value is not None:
        value = unescape(value)
    return Attribute(
        name.lower(), operator, value, (flag or '').lower() == 'i'
        )


def _parse_pseudo(
        text: str,
        start: int
        ) -> tuple[bool, str | None, int] | None:
    """
    Parse a pseudo-class or a pseudo-element of a compound selector.

    :param text: the compound selector
    :param start: the index of the first colon
    :returns: whether it is a pseudo-element, the language of ``:lang()``
        or None, and the index after it, or None if it cannot be read
    """
    if text.startswith('::', start):
        match = _CSS_PSEUDO.match(text, start)
        if match is None:
            return None
        return True, None, _skip_function(text, match.end())
    match = _CSS_LANG.match(text, start)
    if match is not None:
        return False, match.group(1).lower(), match.end()
    match = _CSS_PSEUDO.match(text, start)
    if match is None:
        return None
    return False, None, _skip_function(text, match.end())


def _skip_function(text: str, start: int) -> int:
    """
    Return the index after a parenthesized argument that follows a name.

    :param text: the compound selector
    :param start: the index after the name
    :returns: the index after the argument, or after the name if there is
        no argument or it is not closed
    """
    if start >= len(text) or text[start] != '(':
        return start
    depth = 0
    index = start
    while index < len(text):
        if text[index] == '(':
            depth += 1
        elif text[index] == ')':
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return start


def matches(selector: Selector, element: Element) -> bool | None:
    """
    Return whether a selector matches a cue text element.

    :param selector: the selector
    :param element: the element
    :returns: true or false, or None when an unsupported pseudo-class makes
        it unknown whether the selector matches
    """
    if selector.matches_nothing:
        return False
    if selector.unreadable:
        return None
    if not selector.compounds:
        return element.parent is None
    return _match_from(selector, len(selector.compounds) - 1, element)


def _match_from(
        selector: Selector,
        index: int,
        element: Element
        ) -> bool | None:
    """
    Match the compounds of a selector up to an index against an element.

    :param selector: the selector
    :param index: the index of the compound that matches the element
    :param element: the element
    :returns: true or false, or None when it cannot be known
    """
    result = _match_compound(selector.compounds[index], element)
    if result is False or index == 0:
        return result
    related = _related(selector.combinators[index - 1], element)
    outcomes = [_match_from(selector, index - 1, other) for other in related]
    return _and(result, _any(outcomes))


def _related(combinator: str, element: Element) -> list[Element]:
    """
    Return the elements that a combinator allows before an element.

    :param combinator: the combinator
    :param element: the element
    :returns: the parent, the previous siblings or all the ancestors
    """
    if combinator == '>':
        return [] if element.parent is None else [element.parent]
    if combinator == '+':
        return list(element.previous[-1:])
    if combinator == '~':
        return list(element.previous)
    related = []
    parent = element.parent
    while parent is not None:
        related.append(parent)
        parent = parent.parent
    return related


def _any(outcomes: list[bool | None]) -> bool | None:
    """Return whether any outcome is true, None if one is unknown."""
    if any(outcome is True for outcome in outcomes):
        return True
    if any(outcome is None for outcome in outcomes):
        return None
    return False


def _and(left: bool | None, right: bool | None) -> bool | None:
    """Return the conjunction of two three-valued outcomes."""
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def _match_compound(compound: Compound, element: Element) -> bool | None:
    """
    Return whether a compound selector matches an element.

    :param compound: the compound
    :param element: the element
    :returns: true or false, or None when it cannot be known
    """
    if compound.pseudo_element or compound.ids:
        # cue text elements have no identifier and no pseudo-element
        return False
    if (compound.tag is not None and not compound.universal and
            (element.tag or '').lower() != compound.tag):
        return False
    if not compound.classes <= element.classes:
        return False
    for attribute in compound.attributes:
        if not _attribute_matches(attribute, element):
            return False
    for language in compound.languages:
        if not language_matches(element.language, language):
            return False
    if compound.pseudo_classes:
        return None
    return True


def _attribute_matches(attribute: Attribute, element: Element) -> bool:
    """
    Return whether an attribute selector matches an element.

    :param attribute: the attribute selector
    :param element: the element
    :returns: true if it matches
    """
    if attribute.name == 'voice':
        actual = element.voice
    elif attribute.name == 'lang':
        actual = element.language
    else:
        # cue text elements have no other attribute
        return False

    return attribute_matches(attribute, actual)


def attribute_matches(attribute: Attribute, actual: str | None) -> bool:
    """
    Return whether an attribute selector matches a value.

    :param attribute: the attribute selector
    :param actual: the value of the attribute, or None if it is not set
    :returns: true if it matches
    """
    if attribute.operator is None:
        return actual is not None
    if actual is None or attribute.value is None:
        return False

    expected = attribute.value
    if attribute.insensitive or attribute.name == 'lang':
        actual, expected = actual.lower(), expected.lower()
    if attribute.operator == '=':
        return actual == expected
    if attribute.operator == '~=':
        return expected in actual.split()
    if attribute.operator == '|=':
        return actual == expected or actual.startswith(expected + '-')
    if not expected:
        return False
    if attribute.operator == '^=':
        return actual.startswith(expected)
    if attribute.operator == '$=':
        return actual.endswith(expected)
    return expected in actual


def language_matches(language: str | None, required: str) -> bool:
    """
    Return whether a language matches a ``:lang()`` condition.

    ``:lang(de)`` matches ``de`` and its variants such as ``de-CH``.

    :param language: the language of the element, or None
    :param required: the language of the condition
    :returns: true if the element language matches
    """
    if language is None:
        return False
    language = language.lower()
    return language == required or language.startswith(required + '-')


def effective_visibility(name: str, value: str) -> str | None:
    """
    Return how a declaration changes the visibility of a word.

    ``display: none`` hides a word and its descendants. ``visibility`` is
    inherited, with ``hidden`` and ``collapse`` hiding a word until a
    descendant sets ``visible``. Other values and properties do not change
    whether a word is shown.

    :param name: the name of the declaration
    :param value: the value of the declaration
    :returns: ``none`` or ``shown`` for display, ``hidden`` or ``visible``
        for visibility, or None when the declaration does not change
        visibility
    """
    value = value.strip().lower()
    if value in _CSS_WIDE_KEYWORDS:
        # the value of the parent, or the initial value, which the corpus
        # does not follow for a single declaration
        return None
    if name == 'display':
        return 'none' if value == 'none' else 'shown'
    if name != 'visibility':
        return None
    if value in HIDING_VISIBILITY:
        return 'hidden'
    if value == 'visible':
        return 'visible'
    return None
