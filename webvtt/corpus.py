"""
Corpus indexing of captions.

The captions of languages such as Japanese pair the original words with
their reading in ruby annotations, written as cue tags::

    <ruby>原词<rt>ふりがな</rt></ruby>

A corpus needs the original words and the readings indexed separately, and
each reading linked back to the range of the original characters it
annotates. The tags can span several lines and the text can contain
character references, so every index entry keeps the position it comes from
and the cue it belongs to, which takes a hit back to the original cue.

A reading is only split over the characters of a word when the markup gives
a ruby annotation for each of them. A single reading over several characters
is kept as one annotation over the whole range, as the markup gives no
evidence for splitting it.
"""

import bisect
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from .models import Caption, _iter_cue_text_tokens

# the named character references WebVTT cue text allows, besides the
# numeric ones; an unknown reference is kept as the literal text
_NAMED_ENTITIES = {
    'amp': '&',
    'lt': '<',
    'gt': '>',
    }

# a character reference, numeric (decimal or hexadecimal) or named
_ENTITY_REFERENCE = re.compile(
    r'&(?:#(?:x[0-9a-fA-F]+|[0-9]+)|[a-zA-Z][a-zA-Z0-9]*);'
    )

# a maximal run of word characters, without underscores, so contiguous
# Japanese kana and kanji are indexed together without a tokenizer
_TERM_RUN = re.compile(r'[^\W_]+', re.UNICODE)

# the largest code point Unicode allows, so a larger numeric reference is
# kept as literal text instead of raising from chr()
_MAX_CODE_POINT = 0x10FFFF

# a numeric reference in the surrogate range is decoded as this character
_REPLACEMENT_CHARACTER = '�'


@dataclass(frozen=True)
class CueRef:
    """
    A reference to the original cue an index entry comes from.

    ``text`` is the cue text with its cue tags removed and its character
    references decoded, which the decoded offsets of `CueSpan` and
    `RubyAnnotation` index. Use ``caption.raw_text`` with the raw ranges for
    the text as written, including the tags.

    :param caption: the `Caption` the entry comes from
    :param caption_index: the index of the caption, from 0
    :param identifier: the cue identifier, or None when it has none
    :param start: the start time of the cue as text
    :param end: the end time of the cue as text
    :param text: the decoded text of the cue without cue tags
    """

    caption: Caption
    caption_index: int
    identifier: str | None
    start: str
    end: str
    text: str


@dataclass(frozen=True)
class CueSpan:
    """
    A span of the text of a cue.

    The offsets are character positions in the decoded text of the cue,
    available as `CueRef.text`, which has the cue tags removed and the
    character references decoded. The cue the span belongs to is found with
    `CorpusIndex.cue_for`.

    :param text: the decoded text of the span
    :param start: the start offset in the text of the cue
    :param end: the offset after the end of the span
    :param line: the line of the cue the span is on, from 0
    :param cue_index: the index of the cue, from 0
    :param lang: the language of a ``lang`` tag around the span, or None
    """

    text: str
    start: int
    end: int
    line: int
    cue_index: int
    lang: str | None = None


@dataclass(frozen=True)
class RubyAnnotation:
    """
    A ruby annotation pairing an original word with its reading.

    The ranges are character positions in the decoded text of the cue,
    available as `CueRef.text`, and the raw ranges are the positions in
    `Caption.raw_text`, including the tags. The reading annotates the whole
    base range: a reading over several characters is not split without a
    separate annotation per character.

    :param base: the decoded original text of the annotation
    :param reading: the decoded reading of the annotation
    :param base_range: the offsets of the base in the text of the cue
    :param reading_range: the offsets of the reading in the text of the cue
    :param base_raw_range: the offsets of the base in ``raw_text``
    :param reading_raw_range: the offsets of the reading in ``raw_text``
    :param line: the line of the cue the annotation starts on, from 0
    :param cue_index: the index of the cue, from 0
    :param lang: the language of a ``lang`` tag around the annotation, or
        None
    """

    base: str
    reading: str
    base_range: tuple[int, int]
    reading_range: tuple[int, int]
    base_raw_range: tuple[int, int]
    reading_raw_range: tuple[int, int]
    line: int
    cue_index: int
    lang: str | None = None


@dataclass
class CorpusIndex:
    """
    Index of the original words, readings and tags of captions.

    The terms and the readings are indexed separately. A term is either an
    original word outside a ruby annotation or the base of one, and a
    reading is the text of its ``rt`` tag. `rubies` links each reading back
    to the range of the original characters it annotates, `speakers` and
    `language_spans` index the voice and language tags, and `cues` gives the
    original cue of every entry, so a hit can be traced back to it.

    :param cues: the cues, in the order of the captions
    :param terms: the original terms mapped to their spans
    :param readings: the readings mapped to their spans
    :param rubies: the ruby annotations, in cue order
    :param speakers: the speakers of voice spans mapped to their spans
    :param language_spans: the spans wrapped in a ``lang`` tag
    """

    cues: list[CueRef] = field(default_factory=list)
    terms: dict[str, list[CueSpan]] = field(default_factory=dict)
    readings: dict[str, list[CueSpan]] = field(default_factory=dict)
    rubies: list[RubyAnnotation] = field(default_factory=list)
    speakers: dict[str, list[CueSpan]] = field(default_factory=dict)
    language_spans: list[CueSpan] = field(default_factory=list)

    def find_terms(self, text: str) -> list[CueSpan]:
        """
        Return the spans of an original term.

        :param text: the term to look for, as decoded text
        :returns: the spans indexed for the term, empty when it is absent
        """
        return list(self.terms.get(text, ()))

    def find_readings(self, text: str) -> list[CueSpan]:
        """
        Return the spans of a reading.

        :param text: the reading to look for, as decoded text
        :returns: the spans indexed for the reading, empty when absent
        """
        return list(self.readings.get(text, ()))

    def find_speaker(self, name: str) -> list[CueSpan]:
        """
        Return the spans spoken by a speaker of a voice span.

        :param name: the name of the speaker, as in the voice tag
        :returns: the spans spoken by the speaker, empty when absent
        """
        return list(self.speakers.get(name, ()))

    def cue_for(self, span: CueSpan | RubyAnnotation) -> CueRef:
        """
        Return the original cue a span or a ruby annotation comes from.

        :param span: an entry returned by the index
        :returns: the `CueRef` of the cue
        :raises IndexError: when the entry does not belong to the index
        """
        return self.cues[span.cue_index]


def index_captions(captions: Iterable[Caption]) -> CorpusIndex:
    """
    Build a `CorpusIndex` from captions.

    :param captions: the captions to index, in cue order
    :returns: the index of their terms, readings, ruby annotations, voice
        spans and language spans
    """
    index = CorpusIndex()
    for caption_index, caption in enumerate(captions):
        result = _CueScanner(caption, caption_index).scan()
        index.cues.append(result.cue)
        index.rubies.extend(result.rubies)
        index.language_spans.extend(result.language_spans)
        _merge_spans(index.terms, result.terms)
        _merge_spans(index.readings, result.readings)
        _merge_spans(index.speakers, result.speaker_spans)
    return index


@dataclass(frozen=True)
class _Segment:
    """A decoded piece of text with its raw and decoded offsets."""

    text: str
    raw_start: int
    raw_end: int
    dec_start: int
    dec_end: int
    line: int
    lang: str | None


@dataclass
class _RubyBuilder:
    """The ranges gathered while scanning a ruby annotation."""

    lang: str | None
    mode: str = 'base'
    base: list[_Segment] = field(default_factory=list)
    reading: list[_Segment] = field(default_factory=list)


@dataclass(frozen=True)
class _CueResult:
    """The entries gathered for one cue."""

    cue: CueRef
    rubies: list[RubyAnnotation]
    terms: dict[str, list[CueSpan]]
    readings: dict[str, list[CueSpan]]
    speaker_spans: dict[str, list[CueSpan]]
    language_spans: list[CueSpan]


class _CueScanner:
    """Scan the raw text of one cue into decoded segments and tags."""

    def __init__(self, caption: Caption, caption_index: int):
        """
        Initialize.

        :param caption: the caption to scan
        :param caption_index: the index of the caption, from 0
        """
        self._caption = caption
        self._caption_index = caption_index
        self._text = ''
        self._segments: list[_Segment] = []
        self._rubies: list[RubyAnnotation] = []
        self._language_spans: list[CueSpan] = []
        self._speaker_spans: dict[str, list[CueSpan]] = {}
        self._ruby_stack: list[_RubyBuilder] = []
        self._lang_stack: list[tuple[str, int]] = []
        self._voice_stack: list[tuple[str, int]] = []
        self._rp_depth = 0
        self._cursor = 0

    @property
    def _lang(self) -> str | None:
        """Return the language of the innermost open lang tag, or None."""
        return self._lang_stack[-1][0] if self._lang_stack else None

    def scan(self) -> _CueResult:
        """
        Scan the lines of the caption.

        The line terminators are one decoded character each, as in
        `Caption.text`, so the decoded offsets stay aligned across lines.
        The decoded text also expands the character references and is kept
        as `CueRef.text`, while the raw offsets count the tags and the
        references of `Caption.raw_text`.

        :returns: the entries gathered for the cue
        """
        raw_line_start = 0
        for line_index, line in enumerate(self._caption.lines):
            self._scan_line(line, line_index, raw_line_start)
            raw_line_start += len(line) + 1
            if line_index < len(self._caption.lines) - 1:
                self._text += '\n'
                self._cursor += 1
        self._close_open_spans()
        terms, readings = self._term_indexes()
        return _CueResult(
            cue=CueRef(
                caption=self._caption,
                caption_index=self._caption_index,
                identifier=self._caption.identifier,
                start=self._caption.start,
                end=self._caption.end,
                text=self._text
                ),
            rubies=list(self._rubies),
            terms=terms,
            readings=readings,
            speaker_spans=self._speaker_spans,
            language_spans=list(self._language_spans)
            )

    def _scan_line(
            self, line: str, line_index: int, raw_line_start: int
            ) -> None:
        """
        Scan the text and tags of one line.

        :param line: the line of raw cue text
        :param line_index: the index of the line, from 0
        :param raw_line_start: the offset of the line in ``raw_text``
        """
        raw_cursor = 0
        for token, is_tag in _iter_cue_text_tokens(line):
            if is_tag:
                self._handle_tag(token)
                raw_cursor += len(token) + 2
            else:
                self._handle_text(token, line_index,
                                  raw_line_start + raw_cursor)
                raw_cursor += len(token)

    def _handle_tag(self, content: str) -> None:
        """
        Handle a cue tag.

        :param content: the text between the angle brackets
        """
        name, closing, attribute = _parse_tag(content)
        if name == 'ruby':
            self._handle_ruby(closing)
        elif name == 'rt' and self._ruby_stack:
            self._ruby_stack[-1].mode = 'base' if closing else 'rt'
        elif name == 'rp':
            self._handle_rp(closing)
        elif name == 'lang':
            self._handle_lang(closing, attribute)
        elif name == 'v':
            self._handle_voice(content, closing)

    def _handle_ruby(self, closing: bool) -> None:
        """
        Open or close a ruby annotation.

        :param closing: whether the tag closes an annotation
        """
        if closing:
            self._close_ruby()
        else:
            self._ruby_stack.append(_RubyBuilder(self._lang))

    def _handle_rp(self, closing: bool) -> None:
        """
        Track the ruby parentheses, whose text is not a base or reading.

        :param closing: whether the tag closes the parentheses
        """
        if closing:
            if self._rp_depth:
                self._rp_depth -= 1
        else:
            self._rp_depth += 1

    def _handle_lang(self, closing: bool, lang: str) -> None:
        """
        Open or close a language span.

        :param closing: whether the tag closes a language span
        :param lang: the language of the tag, empty when it has none
        """
        if not closing and lang:
            self._lang_stack.append((lang, self._cursor))
        elif closing and self._lang_stack:
            self._close_lang()

    def _handle_voice(self, content: str, closing: bool) -> None:
        """
        Open or close a voice span.

        :param content: the text between the angle brackets
        :param closing: whether the tag closes a voice span
        """
        if closing:
            if self._voice_stack:
                speaker, start = self._voice_stack.pop()
                self._add_speaker(speaker, start)
            return
        match = Caption.VOICE_SPAN_PATTERN.fullmatch(f'<{content}>')
        if match:
            self._voice_stack.append((match.group(1), self._cursor))

    def _handle_text(
            self, text: str, line_index: int, raw_start: int
            ) -> None:
        """
        Handle a piece of text, decoding its character references.

        :param text: the raw text between tags
        :param line_index: the index of the line, from 0
        :param raw_start: the offset of the text in ``raw_text``
        """
        for decoded, rel_start, rel_end in _iter_decoded(text):
            segment = _Segment(
                text=decoded,
                raw_start=raw_start + rel_start,
                raw_end=raw_start + rel_end,
                dec_start=self._cursor,
                dec_end=self._cursor + len(decoded),
                line=line_index,
                lang=self._lang
                )
            self._segments.append(segment)
            self._text += decoded
            self._attribute_to_ruby(segment)
            self._cursor = segment.dec_end

    def _attribute_to_ruby(self, segment: _Segment) -> None:
        """
        Add a text segment to the open ruby annotation, unless in rp.

        :param segment: the decoded text segment
        """
        if not self._ruby_stack or self._rp_depth:
            return
        builder = self._ruby_stack[-1]
        if builder.mode == 'rt':
            builder.reading.append(segment)
        else:
            builder.base.append(segment)

    def _close_ruby(self) -> None:
        """
        Finalize the innermost open ruby annotation.

        An annotation without both a base and a reading is ignored, as the
        markup gives no reading to link to a range.
        """
        if not self._ruby_stack:
            return
        builder = self._ruby_stack.pop()
        if not builder.base or not builder.reading:
            return
        self._rubies.append(RubyAnnotation(
            base=''.join(segment.text for segment in builder.base),
            reading=''.join(segment.text for segment in builder.reading),
            base_range=(builder.base[0].dec_start,
                        builder.base[-1].dec_end),
            reading_range=(builder.reading[0].dec_start,
                           builder.reading[-1].dec_end),
            base_raw_range=(builder.base[0].raw_start,
                            builder.base[-1].raw_end),
            reading_raw_range=(builder.reading[0].raw_start,
                               builder.reading[-1].raw_end),
            line=builder.base[0].line,
            cue_index=self._caption_index,
            lang=builder.lang
            ))

    def _close_lang(self) -> None:
        """Add the span of a closed language tag."""
        lang, start = self._lang_stack.pop()
        self._add_language_span(lang, start)

    def _add_language_span(self, lang: str, start: int) -> None:
        """
        Record a span wrapped in a language tag.

        :param lang: the language of the tag
        :param start: the decoded offset where the span starts
        """
        if start == self._cursor:
            return
        self._language_spans.append(self._span(lang, start))

    def _add_speaker(self, speaker: str, start: int) -> None:
        """
        Record a span spoken by a speaker.

        :param speaker: the name of the speaker
        :param start: the decoded offset where the voice span starts
        """
        if start == self._cursor:
            return
        self._speaker_spans.setdefault(speaker, []).append(
            self._span(self._lang, start)
            )

    def _span(self, lang: str | None, start: int) -> CueSpan:
        """
        Build a cue span from a start offset to the cursor.

        :param lang: the language of the span, or None
        :param start: the start offset in the decoded text
        :returns: the `CueSpan`
        """
        return CueSpan(
            text=self._text[start:self._cursor],
            start=start,
            end=self._cursor,
            line=self._line_at(start),
            cue_index=self._caption_index,
            lang=lang
            )

    def _close_open_spans(self) -> None:
        """Finalize tags left open at the end of the cue."""
        while self._ruby_stack:
            self._close_ruby()
        while self._lang_stack:
            self._close_lang()
        while self._voice_stack:
            speaker, start = self._voice_stack.pop()
            self._add_speaker(speaker, start)

    def _term_indexes(
            self
            ) -> tuple[dict[str, list[CueSpan]],
                       dict[str, list[CueSpan]]]:
        """
        Build the original term and reading indexes of the cue.

        Each ruby base is indexed as a term and each ruby reading as a
        reading, and the text outside the annotations is split into word
        runs.

        :returns: the terms and the readings, each mapped to its spans
        """
        terms: dict[str, list[CueSpan]] = {}
        readings: dict[str, list[CueSpan]] = {}
        # both the bases and the readings are part of the annotations, so
        # the ordinary terms are only taken from the gaps between them; a
        # word run touching an annotation is split there instead of mixing
        # the reading into the term
        covered = _merge_ranges(
            [ruby.base_range for ruby in self._rubies]
            + [ruby.reading_range for ruby in self._rubies]
            )
        for ruby in self._rubies:
            start, end = ruby.base_range
            _add_span(terms, CueSpan(
                text=ruby.base,
                start=start,
                end=end,
                line=ruby.line,
                cue_index=self._caption_index,
                lang=ruby.lang
                ))
            read_start, read_end = ruby.reading_range
            _add_span(readings, CueSpan(
                text=ruby.reading,
                start=read_start,
                end=read_end,
                line=self._line_at(read_start),
                cue_index=self._caption_index,
                lang=ruby.lang
                ))
        for gap_start, gap_end in _uncovered_gaps(
                covered, len(self._text)
                ):
            gap = self._text[gap_start:gap_end]
            for match in _TERM_RUN.finditer(gap):
                start = gap_start + match.start()
                end = gap_start + match.end()
                _add_span(terms, CueSpan(
                    text=match.group(),
                    start=start,
                    end=end,
                    line=self._line_at(start),
                    cue_index=self._caption_index,
                    lang=self._lang_at(start)
                    ))
        return terms, readings

    def _line_at(self, offset: int) -> int:
        """
        Return the line index of a decoded offset.

        Only called while building a span, so the cue has decoded text.

        :param offset: an offset in the decoded text
        :returns: the index of the line, from 0
        """
        index = bisect.bisect_right(
            self._segments, offset, key=lambda segment: segment.dec_start
            ) - 1
        return self._segments[index].line

    def _lang_at(self, offset: int) -> str | None:
        """
        Return the language of a decoded offset, or None.

        Only called while building a span, so the cue has decoded text.

        :param offset: an offset in the decoded text
        :returns: the language of the innermost lang tag, or None
        """
        index = bisect.bisect_right(
            self._segments, offset, key=lambda segment: segment.dec_start
            ) - 1
        return self._segments[index].lang


def _merge_ranges(
        ranges: list[tuple[int, int]]
        ) -> list[tuple[int, int]]:
    """
    Merge overlapping ranges into disjoint ones, in start order.

    :param ranges: the ranges to merge
    :returns: the disjoint ranges, in order
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _uncovered_gaps(
        ranges: list[tuple[int, int]], length: int
        ) -> list[tuple[int, int]]:
    """
    Return the gaps before, between and after the disjoint ranges.

    :param ranges: the disjoint ranges, in start order
    :param length: the length of the text the ranges are in
    :returns: the non-empty ranges outside all of them
    """
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in ranges:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < length:
        gaps.append((cursor, length))
    return gaps


def _parse_tag(content: str) -> tuple[str, bool, str]:
    """
    Split a cue tag into its name, whether it closes and its attribute.

    The name is lower-cased and without its classes, as in ``v`` for
    ``v.quiet.slow Lisa``. The attribute is the text after the name, as the
    language of ``lang ja`` or the speaker of a voice tag.

    :param content: the text between the angle brackets
    :returns: the tag name, whether it is a closing tag and its attribute
    """
    body = content.strip()
    closing = body.startswith('/')
    if closing:
        body = body[1:]
    head, separator, attribute = body.partition(' ')
    name = head.split('.', 1)[0].lower()
    return name, closing, attribute.strip() if separator else ''


def _iter_decoded(text: str) -> Iterator[tuple[str, int, int]]:
    """
    Iterate the decoded pieces of text with character references.

    :param text: the raw text of a piece between tags
    :returns: tuples of the decoded text and its relative raw start and end
    """
    position = 0
    for match in _ENTITY_REFERENCE.finditer(text):
        if match.start() > position:
            yield text[position:match.start()], position, match.start()
        decoded = _decode_entity(match.group())
        if decoded is None:
            decoded = match.group()
        yield decoded, match.start(), match.end()
        position = match.end()
    if position < len(text):
        yield text[position:], position, len(text)


def _decode_entity(entity: str) -> str | None:
    """
    Decode a character reference.

    :param entity: the reference, including the ampersand and semicolon
    :returns: the decoded character, or None for an unknown named reference
        or a numeric reference past the Unicode range
    """
    if entity.startswith('&#'):
        body = entity[2:-1]
        if body[:1] in ('x', 'X'):
            code_point = int(body[1:], 16)
        else:
            code_point = int(body)
        if code_point > _MAX_CODE_POINT:
            return None
        if 0xD800 <= code_point <= 0xDFFF:
            return _REPLACEMENT_CHARACTER
        return chr(code_point)
    return _NAMED_ENTITIES.get(entity[1:-1])


def _add_span(
        index: dict[str, list[CueSpan]], span: CueSpan
        ) -> None:
    """
    Add a span to an index, keeping the spans of a term together.

    :param index: the index of terms mapped to their spans
    :param span: the span to add
    """
    index.setdefault(span.text, []).append(span)


def _merge_spans(
        target: dict[str, list[CueSpan]],
        source: dict[str, list[CueSpan]]
        ) -> None:
    """
    Merge the per-cue spans of an index into the index of the corpus.

    :param target: the index of the corpus to extend
    :param source: the spans indexed for one cue
    """
    for term, spans in source.items():
        target.setdefault(term, []).extend(spans)
