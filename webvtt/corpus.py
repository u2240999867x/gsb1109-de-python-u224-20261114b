"""Corpus of the words of captions and their visibility under style modes."""

import json
import re
import typing
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from . import css
from .models import Caption, Style

# a word segment: a run of characters that are not whitespace
_WORD = re.compile(r'\S+')

# the verdicts of a word segment
VISIBLE = 'visible'
HIDDEN = 'hidden'
PENDING = 'pending'

# the initial value of each property that the corpus reads
_INITIAL = {'display': 'shown', 'visibility': 'visible'}

# the columns of the tab-separated table, in their order
_TSV_HEADER = (
    'word', 'caption', 'line', 'start', 'end', 'text', 'tag', 'classes',
    'voice', 'language', 'mode', 'visibility', 'declarations', 'pending',
    )


class Mode:
    """
    A style environment of a corpus.

    A mode is a named set of styles, and the classes that every cue text
    element has in it, so a mode can be selected by a class name, as in
    ``::cue(.study)``. The ``STYLE`` blocks of the captions are used unless
    ``document_styles`` is false, followed by the styles of the mode, so
    the styles of the mode take precedence at the same specificity.

    :param name: the name of the mode, unique in a corpus
    :param styles: `Style` instances or CSS strings of the mode
    :param classes: the classes that every cue text element has in the mode
    :param voice: the voice of a cue that has no voice span
    :param language: the language of a cue that has no ``lang`` tag
    :param document_styles: use the ``STYLE`` blocks of the captions before
        the styles of the mode
    """

    def __init__(
            self,
            name: str,
            styles: Iterable[Style | str] = (),
            classes: Iterable[str] = (),
            voice: str | None = None,
            language: str | None = None,
            document_styles: bool = True
            ):
        """Initialize."""
        self.name = name
        self.styles = list(styles)
        self.classes = frozenset(classes)
        self.voice = voice
        self.language = language
        self.document_styles = document_styles

    def __repr__(self) -> str:
        """Return the string representation of the mode."""
        return (f'<{self.__class__.__name__} name={self.name!r} '
                f'classes={sorted(self.classes)!r}>'
                )

    def __eq__(self, other: object) -> bool:
        """Compare equality with another mode."""
        if not isinstance(other, Mode):
            return NotImplemented
        return (self.name == other.name and
                self.styles == other.styles and
                self.classes == other.classes and
                self.voice == other.voice and
                self.language == other.language and
                self.document_styles == other.document_styles
                )

    @classmethod
    def from_config(
            cls,
            name: str,
            config: Mapping[str, typing.Any]
            ) -> 'Mode':
        """
        Return a mode from a mapping, as read from JSON.

        ``styles`` can be CSS strings or mappings with ``text``, ``classes``
        can be a string or a list, and ``document_styles`` defaults to true.

        :param name: the name of the mode
        :param config: the settings of the mode
        :returns: a `Mode` instance

        :raises TypeError: if the config is not a mapping
        :raises ValueError: if a class or a style is not a string, a style
            mapping has no text, or document_styles is not a boolean
        """
        if not isinstance(config, Mapping):
            raise TypeError(f'Invalid config of mode {name!r}: {config!r}')

        raw_styles = config.get('styles', ())
        if isinstance(raw_styles, (str, Mapping)):
            raw_styles = [raw_styles]
        styles: list[Style | str] = []
        for style in raw_styles:
            if isinstance(style, str):
                styles.append(style)
            elif isinstance(style, Mapping) and isinstance(
                    style.get('text'), str):
                styles.append(Style(style['text']))
            else:
                raise ValueError(f'Invalid style {style!r} in mode {name!r}')

        raw_classes = config.get('classes', ())
        if isinstance(raw_classes, str):
            raw_classes = [raw_classes]
        if not all(isinstance(name, str) for name in raw_classes):
            raise ValueError(f'Invalid classes in mode {name!r}')
        classes = [name for name in raw_classes]

        voice = config.get('voice')
        language = config.get('language')
        if voice is not None and not isinstance(voice, str):
            raise ValueError(f'Invalid voice in mode {name!r}')
        if language is not None and not isinstance(language, str):
            raise ValueError(f'Invalid language in mode {name!r}')
        document_styles = config.get('document_styles', True)
        if not isinstance(document_styles, bool):
            raise ValueError(f'Invalid document_styles in mode {name!r}')

        return cls(name, styles, classes, voice, language, document_styles)


def modes_from_config(config: typing.Any) -> list[Mode]:
    """
    Return the modes of a configuration, as read from JSON.

    The configuration is a list of mappings with a ``name``, or a mapping
    of names to their settings, optionally wrapped in a ``modes`` item as
    in ``{"modes": [...]}``.

    :param config: the configuration
    :returns: the modes

    :raises TypeError: if the configuration is not a list or a mapping
    :raises ValueError: if a mode has no name or the names are repeated
    """
    if isinstance(config, Mapping) and 'modes' in config:
        config = config['modes']
    if isinstance(config, Mapping) and isinstance(config.get('name'), str):
        # a single mode, as in {"name": "study", "classes": ["study"]}
        entries: list[typing.Any] = [config]
    elif isinstance(config, Mapping):
        entries = [
            {**settings, 'name': name}
            if isinstance(settings, Mapping) else settings
            for name, settings in config.items()
            ]
    elif isinstance(config, Sequence) and not isinstance(config, str):
        entries = list(config)
    else:
        raise TypeError(f'Invalid mode configuration {config!r}')

    modes = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(
                entry.get('name'), str):
            raise ValueError(f'Invalid mode {entry!r}, a name is needed')
        name = entry['name']
        modes.append(Mode.from_config(
            name, {key: value for key, value in entry.items() if key != 'name'}
            ))
    names = [mode.name for mode in modes]
    if len(names) != len(set(names)):
        raise ValueError(f'Repeated mode name in {names!r}')
    return modes


@dataclass(frozen=True)
class DeclarationRecord:
    """A declaration stored with the result of a word segment."""

    property: str
    value: str
    important: bool
    selector: str
    specificity: tuple[int, int, int]
    order: int
    element: str
    inherited: bool

    def __str__(self) -> str:
        """Return the declaration as ``selector: property: value``."""
        return (f'{self.selector}: {self.property}: {self.value}'
                f'{"!important" if self.important else ""}')


@dataclass(frozen=True)
class SegmentResult:
    """
    The visibility of a word segment under a style mode.

    ``visibility`` is ``visible`` or ``hidden``, or ``pending`` when a
    selector that is not supported could change it. ``declarations`` has
    the declarations that determine the verdict, and ``pending`` the
    declarations of unsupported selectors that could change it.
    """

    visibility: str
    declarations: tuple[DeclarationRecord, ...] = ()
    pending: tuple[DeclarationRecord, ...] = ()


@dataclass
class CorpusRecord:
    """A word segment of a cue with its result under each mode."""

    index: int
    caption: int
    line: int
    start: int
    end: int
    text: str
    tag: str | None
    classes: tuple[str, ...]
    voice: str | None
    language: str | None
    results: dict[str, SegmentResult] = field(default_factory=dict)

    def visibility(self, mode: str) -> str:
        """
        Return the visibility of the segment under a mode.

        :param mode: the name of the mode
        :returns: ``visible``, ``hidden`` or ``pending``

        :raises KeyError: if the corpus has no mode with that name
        """
        return self.results[mode].visibility


class Corpus:
    """
    A table of the word segments of captions and their visibility.

    Each segment keeps its text, the cue and the offsets it comes from, and,
    for each mode, whether it is shown and the declarations that decide
    it. The text is kept as written, so it can be searched whatever the
    verdicts are, and changing a mode only changes the words that depend
    on it.

    :param vtt: the captions
    :param modes: the style environments, by default a ``normal`` mode and
        a ``study`` mode that gives every cue text element the class
        ``study``
    """

    def __init__(
            self,
            vtt: typing.Any,
            modes: Iterable[Mode] | None = None
            ):
        """Initialize."""
        self.vtt = vtt
        self.modes: list[Mode] = (
            list(modes) if modes is not None
            else [Mode('normal'), Mode('study', classes=('study',))]
            )
        _check_modes(self.modes)

        self._roots = _parse_captions(vtt.captions)
        self._segments = _segments(self._roots)
        self._views: dict[str, dict[int, _View]] = {}
        self._states: dict[str, dict[int, _State]] = {}
        for mode in self.modes:
            self._evaluate(mode)
        self.records = [
            _record(index, segment, self.modes, self._views, self._states)
            for index, segment in enumerate(self._segments, 1)
            ]

    def __repr__(self) -> str:
        """Return the string representation of the corpus."""
        return (f'<{self.__class__.__name__} records={len(self.records)} '
                f'modes={[mode.name for mode in self.modes]!r}>'
                )

    def __len__(self) -> int:
        """Return the number of word segments."""
        return len(self.records)

    def __iter__(self) -> typing.Iterator[CorpusRecord]:
        """Iterate over the word segments."""
        return iter(self.records)

    @classmethod
    def from_config(
            cls,
            vtt: typing.Any,
            config: typing.Any
            ) -> 'Corpus':
        """
        Return the corpus of the captions with the modes of a configuration.

        :param vtt: the captions
        :param config: the mode configuration, as for `modes_from_config`
        :returns: a `Corpus` instance
        """
        return cls(vtt, modes_from_config(config))

    def _evaluate(self, mode: Mode) -> None:
        """Evaluate a mode and keep its views and states."""
        views, states = _evaluate(self.vtt, mode, self._roots)
        self._views[mode.name] = views
        self._states[mode.name] = states

    def replace_mode(self, mode: Mode) -> list[int]:
        """
        Replace a mode, or add it, and evaluate it again.

        The other modes are not evaluated again, so only the words whose
        result under this mode changes are reported, and the other modes
        keep their results.

        :param mode: the new mode, which replaces the mode of the same name
        :returns: the 1-based indices of the records whose result changed

        :raises ValueError: if the name of a new mode is already used
        """
        existing = next(
            (index for index, current in enumerate(self.modes)
             if current.name == mode.name),
            None
            )
        if existing is None:
            _check_modes([*self.modes, mode])

        self._evaluate(mode)
        changed = []
        for record, segment in zip(self.records, self._segments):
            result = _result(
                segment, self._views[mode.name], self._states[mode.name]
                )
            if record.results.get(mode.name) != result:
                changed.append(record.index)
            record.results[mode.name] = result

        if existing is None:
            self.modes.append(mode)
        else:
            self.modes[existing] = mode
        return changed

    def search(
            self,
            text: str,
            mode: str | None = None,
            visible: bool | None = None
            ) -> list[CorpusRecord]:
        """
        Return the segments whose text has a string.

        The text is searched as written, ignoring case, whatever the
        verdicts are, so a word hidden in a mode is still found.

        :param text: the text to search for
        :param mode: the mode of the visibility filter
        :param visible: only visible or only hidden segments of the mode,
            or all of them when None, which also leaves out pending segments
        :returns: the matching records, in their order

        :raises KeyError: if the corpus has no mode with that name
        :raises ValueError: if visible is given without a mode
        """
        if visible is not None and mode is None:
            raise ValueError('A mode is needed to filter by visibility')
        if mode is not None and mode not in self._states:
            raise KeyError(mode)

        found = []
        needle = text.casefold()
        for record in self.records:
            if needle not in record.text.casefold():
                continue
            if visible is not None:
                result = record.results[typing.cast(str, mode)].visibility
                if visible != (result == VISIBLE):
                    continue
            found.append(record)
        return found

    def rows(
            self,
            records: Iterable[CorpusRecord] | None = None
            ) -> list[dict[str, typing.Any]]:
        """
        Return the corpus table as a list of rows, one per segment and mode.

        The rows have plain values, so they can be written as JSON.

        :param records: the records to write, or all of them when None
        :returns: the rows
        """
        rows = []
        for record in self.records if records is None else records:
            for mode in self.modes:
                result = record.results[mode.name]
                rows.append({
                    'word': record.index,
                    'caption': record.caption,
                    'line': record.line,
                    'start': record.start,
                    'end': record.end,
                    'text': record.text,
                    'tag': record.tag,
                    'classes': list(record.classes),
                    'voice': record.voice,
                    'language': record.language,
                    'mode': mode.name,
                    'visibility': result.visibility,
                    'declarations': [
                        _declaration_row(declaration)
                        for declaration in result.declarations
                        ],
                    'pending': [
                        _declaration_row(declaration)
                        for declaration in result.pending
                        ]
                    })
        return rows

    def to_json(
            self,
            indent: int | None = None,
            records: Iterable[CorpusRecord] | None = None
            ) -> str:
        """
        Return the corpus table as JSON.

        :param indent: the indentation of the JSON, or None for one line
        :param records: the records to write, or all of them when None
        :returns: the table as JSON
        """
        return json.dumps(
            self.rows(records), ensure_ascii=False, indent=indent
            )

    def to_tsv(self, records: Iterable[CorpusRecord] | None = None) -> str:
        """
        Return the corpus table as tab-separated values, with a header.

        The declarations are joined per row as ``selector: property:
        value``, with ``!important`` after an important value. A word is a
        row for each mode, so the text is repeated, and can be searched
        with any tool that reads the table.

        :param records: the records to write, or all of them when None
        :returns: the table as tab-separated values
        """
        lines = ['\t'.join(_TSV_HEADER)]
        for row in self.rows(records):
            lines.append('\t'.join(
                _tsv_cell(row[name]) for name in _TSV_HEADER
                ))
        return '\n'.join(lines) + '\n'


def _check_modes(modes: Sequence[Mode]) -> None:
    """Raise ValueError if a mode name is repeated."""
    names = [mode.name for mode in modes]
    if len(names) != len(set(names)):
        raise ValueError(f'Repeated mode name in {names!r}')


def _declaration_row(declaration: DeclarationRecord) -> dict[str, typing.Any]:
    """Return a declaration as plain values."""
    return {
        'property': declaration.property,
        'value': declaration.value,
        'important': declaration.important,
        'selector': declaration.selector,
        'specificity': list(declaration.specificity),
        'order': declaration.order,
        'element': declaration.element,
        'inherited': declaration.inherited,
        }


def _tsv_cell(value: typing.Any) -> str:
    """
    Return a row value for a tab-separated cell.

    :param value: the value of the row, which can be a list of declarations
    :returns: the value as a cell, without tabs or line breaks
    """
    if isinstance(value, list) and value and isinstance(value[0], dict):
        value = '; '.join(
            f'{declaration["selector"]}: {declaration["property"]}: '
            f'{declaration["value"]}'
            f'{"!important" if declaration["important"] else ""}'
            for declaration in value
            )
    elif isinstance(value, list):
        value = '.'.join(str(item) for item in value)
    elif value is None:
        value = ''
    return str(value).replace('\t', '\\t').replace('\n', '\\n')


class _Text:
    """A run of text of a cue and its offset in the text of the cue."""

    __slots__ = ('text', 'start')

    def __init__(self, text: str, start: int):
        self.text = text
        self.start = start


class _Element:
    """An element of the cue text."""

    __slots__ = ('tag', 'classes', 'voice', 'language', 'parent', 'children',
                 'caption', 'cue_text', 'has_voice_span')

    def __init__(
            self,
            tag: str | None,
            classes: frozenset[str],
            voice: str | None,
            language: str | None,
            parent: '_Element | None',
            caption: int
            ):
        self.tag = tag
        self.classes = classes
        self.voice = voice
        self.language = language
        self.parent = parent
        self.children: list[_Element | _Text] = []
        self.caption = caption
        self.cue_text = ''
        self.has_voice_span = False


class _View:
    """An element with the classes and defaults of a mode."""

    __slots__ = ('node', 'parent', 'previous', 'tag', 'classes', 'voice',
                 'language')

    def __init__(
            self,
            node: _Element,
            parent: '_View | None',
            previous: Sequence['_View'],
            mode: Mode
            ):
        self.node = node
        self.parent: _View | None = parent
        self.previous: tuple[_View, ...] = tuple(previous)
        self.tag: str | None = node.tag
        self.classes: frozenset[str] = node.classes | mode.classes
        self.voice: str | None
        self.language: str | None
        if parent is None and node.voice is None and not node.has_voice_span:
            # the voice of the mode is the voice of a cue without voice spans
            self.voice = mode.voice
        elif node.voice is not None:
            self.voice = node.voice
        elif parent is not None:
            self.voice = parent.voice
        else:
            self.voice = None
        if node.language is not None:
            self.language = node.language
        elif parent is not None:
            self.language = parent.language
        else:
            self.language = mode.language


class _Segment:
    """A word segment and the element that contains it."""

    __slots__ = ('caption', 'line', 'start', 'end', 'text', 'element')

    def __init__(
            self,
            caption: int,
            line: int,
            start: int,
            end: int,
            text: str,
            element: _Element
            ):
        self.caption = caption
        self.line = line
        self.start = start
        self.end = end
        self.text = text
        self.element = element


def _parse_captions(captions: Iterable[Caption]) -> list[_Element]:
    """
    Return the root of the cue text of each caption.

    :param captions: the captions
    :returns: the root element of each caption
    """
    roots = []
    for number, caption in enumerate(captions, 1):
        root = _Element(None, frozenset(), None, None, None, number)
        root.cue_text = caption.raw_text
        _parse_cue(root)
        roots.append(root)
    return roots


def _parse_cue(root: _Element) -> None:
    """
    Fill the root of a cue with its elements and text.

    :param root: the root element of the caption
    """
    stack = [root]
    text = root.cue_text
    pos = 0
    while pos < len(text):
        start = text.find('<', pos)
        if start < 0:
            stack[-1].children.append(_Text(text[pos:], pos))
            return
        end = text.find('>', start + 1)
        if end < 0:
            stack[-1].children.append(_Text(text[pos:], pos))
            return
        if start > pos:
            stack[-1].children.append(_Text(text[pos:start], pos))
        _handle_tag(stack, text[start + 1:end])
        pos = end + 1


def _handle_tag(stack: list[_Element], tag: str) -> None:
    """
    Open or close an element of the cue text.

    :param stack: the open elements, with the root first
    :param tag: the content of the tag, without the angle brackets
    """
    content = tag.strip()
    if not content:
        return
    if content.startswith('/'):
        _close_tag(stack, content[1:].strip())
        return

    head, _, rest = content.partition(' ')
    rest = rest.strip()
    name, *classes = head.split('.')
    name = name.lower()
    if not name or name[0].isdigit() or ':' in name:
        # a timestamp tag, which has no element
        return

    parent = stack[-1]
    if name == 'v':
        parent.has_voice_span = True
    voice = rest if name == 'v' and rest else parent.voice
    language = rest.split()[0] if name == 'lang' and rest else parent.language
    element = _Element(
        name,
        frozenset(classes),
        voice,
        language,
        parent,
        parent.caption
        )
    element.cue_text = parent.cue_text
    parent.children.append(element)
    stack.append(element)


def _close_tag(stack: list[_Element], name: str) -> None:
    """Close the last open element with a name, and those opened after it."""
    name = name.split('.')[0].split()[0].lower() if name else ''
    for index in range(len(stack) - 1, 0, -1):
        if stack[index].tag == name:
            del stack[index:]
            return


def _segments(roots: Sequence[_Element]) -> list[_Segment]:
    """
    Return the word segments of the captions, in order.

    A word segment is a run of characters that are not whitespace within a
    text node, so a cue tag in the middle of a word splits it into two
    segments, each with the visibility of its own element.

    :param roots: the root element of each caption
    :returns: the segments
    """
    segments: list[_Segment] = []

    def visit(element: _Element) -> None:
        for child in element.children:
            if isinstance(child, _Text):
                for match in _WORD.finditer(child.text):
                    start = child.start + match.start()
                    segments.append(_Segment(
                        element.caption,
                        element.cue_text.count('\n', 0, start) + 1,
                        start,
                        child.start + match.end(),
                        match.group(),
                        element
                        ))
            else:
                visit(child)

    for root in roots:
        visit(root)
    return segments


def _mode_rules(
        vtt: typing.Any,
        mode: Mode
        ) -> list[tuple[int, css.Selector, css.Declaration]]:
    """
    Return the declarations of a mode, with their order.

    The ``STYLE`` blocks of the captions come first, then the styles of the
    mode, each in its order, so a later declaration wins at the same
    specificity.

    :param vtt: the captions
    :param mode: the mode
    :returns: the order, selector and declaration of each declaration
    """
    texts: list[str] = []
    if mode.document_styles:
        texts.extend(style.text for style in vtt.styles)
    texts.extend(style.text if isinstance(style, Style) else str(style)
                 for style in mode.styles)
    rules = []
    order = 0
    for text in texts:
        for rule in css.parse_styles(text):
            for declaration in rule.declarations:
                order += 1
                for selector in rule.selectors:
                    rules.append((order, selector, declaration))
    return rules


class _Entry(typing.NamedTuple):
    """A declaration that can apply to an element."""

    name: str
    value: str
    declaration: css.Declaration
    selector: str
    specificity: tuple[int, int, int]
    order: int
    element: str
    inherited: bool = False

    @property
    def key(self) -> tuple[bool, tuple[int, int, int], int]:
        """Return the cascade key of the declaration."""
        return (
            self.declaration.important, self.specificity, self.order
            )


class _Property(typing.NamedTuple):
    """The possible values of a property at an element."""

    definite: _Entry | None
    unknown: list[_Entry]


class _State(typing.NamedTuple):
    """The possible visibility of the text of an element."""

    none_possible: bool
    shown_possible: bool
    visibility: set[str]
    display: _Property
    visibility_property: _Property


def _evaluate(
        vtt: typing.Any,
        mode: Mode,
        roots: Sequence[_Element]
        ) -> tuple[dict[int, _View], dict[int, _State]]:
    """
    Return the views and the states of the elements under a mode.

    :param vtt: the captions
    :param mode: the mode
    :param roots: the root element of each caption
    :returns: the view of each element and its state, by the id of its node
    """
    rules = _mode_rules(vtt, mode)
    views: dict[int, _View] = {}
    states: dict[int, _State] = {}

    def visit(
            node: _Element,
            parent: _View | None,
            previous: Sequence[_View]
            ) -> None:
        view = _View(node, parent, previous, mode)
        views[id(node)] = view
        parent_state = states[id(parent.node)] if parent is not None else None
        states[id(node)] = _state(view, parent_state, rules)
        siblings: list[_View] = []
        for child in node.children:
            if isinstance(child, _Element):
                visit(child, view, siblings)
                siblings.append(views[id(child)])

    for root in roots:
        visit(root, None, ())
    return views, states


def _state(
        view: _View,
        parent: _State | None,
        rules: Sequence[tuple[int, css.Selector, css.Declaration]]
        ) -> _State:
    """
    Return the state of an element, given the state of its parent.

    :param view: the element
    :param parent: the state of the parent, or None for a cue root
    :param rules: the declarations of the mode
    :returns: the state
    """
    display = _property(view, 'display', rules)
    visibility = _property(view, 'visibility', rules)

    display_values = _values(display, {'shown'})
    none_possible = (
        'none' in display_values or
        (parent is not None and parent.none_possible)
        )
    shown_possible = (
        'shown' in display_values and
        (parent is None or parent.shown_possible)
        )

    inherited = (
        parent.visibility if parent is not None
        else {_INITIAL['visibility']}
        )
    values = _values(visibility, inherited)

    return _State(
        none_possible, shown_possible, values, display, visibility
        )


def _values(prop: _Property, fallback: set[str]) -> set[str]:
    """
    Return the possible values of a property at an element.

    A declaration that definitely applies sets the value, and only the
    unsupported selectors with a higher cascade key can override it. With
    no declaration, the value is the fallback, which is the value of the
    parent for visibility and the initial value for display.

    :param prop: the declarations of the property at the element
    :param fallback: the values when no declaration applies
    :returns: the possible values
    """
    if prop.definite is not None:
        definite = prop.definite
        values = {definite.value}
        values.update(
            entry.value for entry in prop.unknown
            if entry.key > definite.key
            )
        return values

    values = set(fallback)
    values.update(entry.value for entry in prop.unknown)
    return values


def _property(
        view: _View,
        prop: str,
        rules: Sequence[tuple[int, css.Selector, css.Declaration]]
        ) -> _Property:
    """
    Return the possible values of a property at an element.

    A supported selector that matches gives a value, of which the one with
    the highest cascade key applies. An unsupported selector that could
    match gives a value too when its key is higher than the one that
    applies, as it wins in the worlds where it matches.

    :param view: the element
    :param prop: the property
    :param rules: the declarations of the mode
    :returns: the possible values, the declaration that definitely applies
        and the declarations of unsupported selectors that may override it
    """
    definite: _Entry | None = None
    unknown: list[_Entry] = []
    for order, selector, declaration in rules:
        if declaration.name != prop:
            continue
        value = css.effective_visibility(prop, declaration.value)
        if value is None:
            continue
        match = css.matches(selector, typing.cast(css.Element, view))
        if match is False:
            continue
        entry = _Entry(
            prop, value, declaration, selector.raw, selector.specificity,
            order, _describe(view)
            )
        if match is True:
            if definite is None or entry.key > definite.key:
                definite = entry
        else:
            # None, as the selector may match when a pseudo-class does
            unknown.append(entry)

    unknown = [
        entry for entry in unknown
        if (definite is None or entry.key > definite.key) and
        (definite is None or entry.value != definite.value)
        ]
    return _Property(definite, unknown)


def _describe(view: _View) -> str:
    """Return an element as it is written in a cue tag."""
    if view.tag is None:
        return 'cue'
    classes = '.'.join(sorted(view.node.classes))
    return f'{view.tag}.{classes}' if classes else view.tag


def _path(segment: _Segment, views: dict[int, _View]) -> list[_View]:
    """Return the element of a segment and its ancestors, from the root."""
    path = []
    current: _View | None = views[id(segment.element)]
    while current is not None:
        path.append(current)
        current = current.parent
    path.reverse()
    return path


def _result(
        segment: _Segment,
        views: dict[int, _View],
        states: dict[int, _State]
        ) -> SegmentResult:
    """
    Return the visibility of a word segment.

    :param segment: the segment
    :param views: the views of the mode
    :param states: the states of the mode
    :returns: the result of the mode
    """
    path = _path(segment, views)
    state = states[id(segment.element)]
    hidden = state.none_possible or 'hidden' in state.visibility
    visible = state.shown_possible and 'visible' in state.visibility

    if hidden and visible:
        return SegmentResult(
            PENDING, _baseline(path, states), _pending(path, states)
            )
    if hidden:
        return SegmentResult(HIDDEN, _hidden(path, states), ())
    # a word is always shown or hidden, as visibility starts visible and
    # display starts inline, so it cannot be both hidden and shown here
    return SegmentResult(VISIBLE, _visible(path, states), ())


def _display_none(
        path: Sequence[_View],
        states: dict[int, _State]
        ) -> _Entry | None:
    """Return the declaration that hides a subtree, if any."""
    for view in path:
        entry = states[id(view.node)].display.definite
        if entry is not None and entry.value == 'none':
            return entry
    return None


def _visibility_at(
        path: Sequence[_View],
        states: dict[int, _State],
        value: str
        ) -> _Entry | None:
    """Return the nearest declaration that sets a visibility value."""
    for view in reversed(path):
        entry = states[id(view.node)].visibility_property.definite
        if entry is not None and entry.value == value:
            return entry
    return None


def _baseline(
        path: Sequence[_View],
        states: dict[int, _State]
        ) -> tuple[DeclarationRecord, ...]:
    """Return the declarations that apply where no unsupported one does."""
    entry = _display_none(path, states)
    if entry is not None:
        return (_record_of(entry),)
    for view in reversed(path):
        entry = states[id(view.node)].visibility_property.definite
        if entry is not None:
            return (_record_of(entry),)
    return ()


def _hidden(
        path: Sequence[_View],
        states: dict[int, _State]
        ) -> tuple[DeclarationRecord, ...]:
    """Return the declarations that hide a word."""
    entry = _display_none(path, states)
    if entry is not None:
        return (_record_of(entry),)
    entry = _visibility_at(path, states, 'hidden')
    assert entry is not None
    return (_record_of(entry),)


def _visible(
        path: Sequence[_View],
        states: dict[int, _State]
        ) -> tuple[DeclarationRecord, ...]:
    """Return the declarations that show a word."""
    entry = _visibility_at(path, states, 'visible')
    if entry is not None:
        return (_record_of(entry),)
    return ()


def _pending(
        path: Sequence[_View],
        states: dict[int, _State]
        ) -> tuple[DeclarationRecord, ...]:
    """Return the declarations of unsupported selectors that may decide."""
    records: list[DeclarationRecord] = []
    for view in path:
        state = states[id(view.node)]
        for entry in state.display.unknown:
            records.append(_record_of(entry))
        for entry in state.visibility_property.unknown:
            records.append(_record_of(entry))
    return tuple(records)


def _record_of(entry: _Entry) -> DeclarationRecord:
    """Return an entry as a declaration record, with the value as written."""
    return DeclarationRecord(
        entry.name,
        entry.declaration.value,
        entry.declaration.important,
        entry.selector,
        entry.specificity,
        entry.order,
        entry.element,
        entry.inherited
        )


def _record(
        index: int,
        segment: _Segment,
        modes: Sequence[Mode],
        views: dict[str, dict[int, _View]],
        states: dict[str, dict[int, _State]]
        ) -> CorpusRecord:
    """Return the record of a word segment."""
    record = CorpusRecord(
        index=index,
        caption=segment.caption,
        line=segment.line,
        start=segment.start,
        end=segment.end,
        text=segment.text,
        tag=segment.element.tag,
        classes=tuple(sorted(segment.element.classes)),
        voice=segment.element.voice,
        language=segment.element.language
        )
    for mode in modes:
        record.results[mode.name] = _result(
            segment, views[mode.name], states[mode.name]
            )
    return record
