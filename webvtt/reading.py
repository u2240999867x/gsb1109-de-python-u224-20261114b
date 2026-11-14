"""Reading speed of the captions, per language.

The text of a caption is counted with the rule of its language, by words or
by visible graphemes, and compared with the time in which it can be read:
the duration of the caption minus the narration pauses that fall in it.

The language of a run of text is given with the ``<lang>`` cue tag, as in
``Hello <lang es>hola</lang>``; text outside one is counted for the default
language, the first of the rules. Cue tags, including voice and class spans,
are not part of the text and are never counted.
"""

import enum
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .models import (
    Caption,
    Timestamp,
    _Position,
    _iter_cue_text_tokens,
    _position_in_milliseconds,
    )

# a language tag opens a span, e.g. ``lang en``, ``lang:en`` or ``lang.en``
_LANGUAGE_OPEN = re.compile(r'lang[ .:]([^\s>]+)')

# the code points of a Hangul syllable that continue the previous grapheme,
# the medial and final jamo in both the compatibility and conjoining blocks
_JAMO_CONTINUATION = (
    (0x1160, 0x11FF),   # Hangul Jungseong and Jongseong
    (0xD7B0, 0xD7FF),   # Hangul Jungseong-A and Jongseong-A
    )


class CountingRule(enum.Enum):
    """How the text of a language is counted."""

    WORDS = 'words'
    GRAPHEMES = 'graphemes'


class TimeSharing(enum.Enum):
    """How the languages of a caption share its readable time."""

    INDEPENDENT = 'independent'
    SHARED = 'shared'


class ReadingConfigError(ValueError):
    """Raised when the languages or the reading rules are not valid."""


@dataclass(frozen=True)
class LanguageSpec:
    """
    Reading rule of a language.

    :param language: the language code, as written in a ``<lang>`` tag
    :param counting: `CountingRule.WORDS` to count words, or
        `CountingRule.GRAPHEMES` to count visible graphemes
    :param rate: the reading budget, in words or graphemes per second
    :param sharing: `TimeSharing.INDEPENDENT` to give each language the full
        readable time of the caption, or `TimeSharing.SHARED` to split it
    :param weight: the share of the readable time given to the language when
        the time is shared

    :raises ReadingConfigError: if the rate or the weight is not a finite
        number greater than zero
    """

    language: str
    counting: CountingRule
    rate: float
    sharing: TimeSharing = TimeSharing.INDEPENDENT
    weight: float = 1.0

    def __post_init__(self) -> None:
        """Validate the rate and the weight."""
        if not _is_positive_number(self.rate):
            raise ReadingConfigError(
                f'Invalid reading rate for {self.language!r}: {self.rate!r}'
                )
        if not _is_positive_number(self.weight):
            raise ReadingConfigError(
                f'Invalid time weight for {self.language!r}: {self.weight!r}'
                )


@dataclass(frozen=True)
class LanguageLoad:
    """
    Reading load of one language in a caption.

    :param language: the language code
    :param counting: the rule used to count the text
    :param amount: the words or visible graphemes of the text
    :param readable_seconds: the readable time given to the language
    :param required_seconds: the time needed to read the text at its rate
    :param unreadable: whether the language gets no readable time at all
    """

    language: str
    counting: CountingRule
    amount: int
    readable_seconds: float
    required_seconds: float
    unreadable: bool

    @property
    def excess_seconds(self) -> float:
        """Return the time past the budget, zero when the text fits in it."""
        return max(self.required_seconds - self.readable_seconds, 0.0)

    @property
    def speed(self) -> float | None:
        """Return the words or graphemes per readable second, or None."""
        if self.readable_seconds == 0:
            return None
        return self.amount / self.readable_seconds


@dataclass(frozen=True)
class CueReading:
    """
    Reading load of one caption, with one `LanguageLoad` per language in it.

    :param cue: the caption
    :param index: the index of the caption in the captions, from zero
    :param start: the start time of the caption
    :param end: the end time of the caption
    :param duration_seconds: the duration of the caption
    :param readable_windows: the intervals in which the caption can be read,
        as pairs of `Timestamp`, after the pauses are removed
    :param readable_seconds: the readable time, the length of the windows
    :param unreadable: whether the whole caption is covered by pauses
    :param languages: the load of each language, in the order of the rules
    """

    cue: Caption
    index: int
    start: Timestamp
    end: Timestamp
    duration_seconds: float
    readable_windows: tuple[tuple[Timestamp, Timestamp], ...]
    readable_seconds: float
    unreadable: bool
    languages: tuple[LanguageLoad, ...] = field(default_factory=tuple)


def count_words(text: str) -> int:
    """
    Count the words of text.

    Words are runs of characters separated by whitespace, so punctuation
    attached to a word is part of it, as in ``don't`` or ``U.S.A.``, and a
    token without a letter or a digit, such as ``...`` or ``--``, is not a
    word.

    :param text: the text to count
    :returns: the number of words
    """
    return sum(
        1 for token in text.split() if any(char.isalnum() for char in token)
        )


def count_graphemes(text: str) -> int:
    """
    Count the visible graphemes of text.

    A combining mark extends the base character it follows instead of
    counting on its own, so an ``e`` with a combining acute accent, stored as
    two code points, is one grapheme. The medial and final jamo of a Hangul
    syllable extend its lead as well, as do joiners and other format
    characters. Whitespace is not visible and is not counted.

    :param text: the text to count
    :returns: the number of visible graphemes
    """
    return sum(
        1 for char in text
        if not char.isspace() and not _is_continuation_mark(char)
        )


def _is_continuation_mark(char: str) -> bool:
    """
    Return whether a code point extends the grapheme of the previous one.

    :param char: a single code point
    :returns: whether it is a combining mark, a format character such as a
        joiner or a continuation jamo
    """
    if unicodedata.combining(char):
        return True
    category = unicodedata.category(char)
    if category in ('Mn', 'Mc', 'Cf'):
        return True
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in _JAMO_CONTINUATION)


def merge_pauses(
        pauses: Iterable[Sequence[_Position]]
        ) -> list[tuple[int, int]]:
    """
    Merge the narration pauses that overlap or touch.

    Overlapping pauses are subtracted only once, so the pauses are merged
    before they are removed from the captions. A pause that ends at or
    before it starts is ignored.

    :param pauses: the pauses as pairs of positions, a timestamp string, a
        `Timestamp`, a number of seconds or a ``timedelta`` each
    :returns: the merged pauses as pairs of milliseconds, sorted

    :raises ReadingConfigError: if a pause is not a pair of positions
    :raises MalformedCaptionError: if a timestamp string is not valid
    :raises TypeError: if a position is not of a supported type
    :raises ValueError: if a position is not finite or before zero
    """
    intervals = []
    for pause in pauses:
        if not isinstance(pause, (tuple, list)) or len(pause) != 2:
            raise ReadingConfigError(f'Invalid pause {pause!r}')
        start = _position_in_milliseconds(pause[0])
        end = _position_in_milliseconds(pause[1])
        if end > start:
            intervals.append((start, end))
    intervals.sort()

    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def reading_table(
        captions: Iterable[Caption],
        languages: Sequence[LanguageSpec],
        pauses: Iterable[Sequence[_Position]] = ()
        ) -> list[CueReading]:
    """
    Return the reading load of each caption, one column per language.

    The text of each caption is split on its ``<lang>`` tags and counted with
    the rule of its language. Text without a tag is counted for the first
    language. The readable time is the duration of the caption minus the
    merged pauses that fall in it. Languages read independently each get the
    full readable time; languages that share it split it by their weights.

    :param captions: the captions to measure
    :param languages: the reading rule of each language, each with a unique
        language code
    :param pauses: the narration pauses, as pairs of positions, merged when
        they overlap
    :returns: the `CueReading` of each caption

    :raises ReadingConfigError: if no language is given, two rules have the
        same language code, the text of a caption uses a language without a
        rule, or the languages of a caption do not share one time strategy
    :raises MalformedCaptionError: if a pause timestamp string is not valid
    """
    specs = _language_specs(languages)
    merged_pauses = merge_pauses(pauses)
    default_language = specs[0].language

    rows: list[CueReading] = []
    for index, caption in enumerate(captions):
        texts = _caption_text_by_language(
            caption.raw_text, default_language
            )
        _check_languages(texts, specs, index)
        present = [spec for spec in specs if spec.language in texts]

        start_ms = caption.start_in_milliseconds
        end_ms = caption.end_in_milliseconds
        duration_ms = max(end_ms - start_ms, 0)
        readable = _subtract_pauses(start_ms, end_ms, merged_pauses)
        readable_ms = sum(end - start for start, end in readable)
        unreadable = readable_ms == 0

        loads = _language_loads(present, texts, readable_ms, unreadable)
        rows.append(CueReading(
            cue=caption,
            index=index,
            start=caption.start_time,
            end=caption.end_time,
            duration_seconds=duration_ms / 1000,
            readable_windows=tuple(
                (Timestamp._from_milliseconds(start),
                 Timestamp._from_milliseconds(end))
                for start, end in readable
                ),
            readable_seconds=readable_ms / 1000,
            unreadable=unreadable,
            languages=tuple(loads)
            ))
    return rows


def format_reading_table(
        rows: Sequence[CueReading],
        *,
        seconds_decimals: int = 3
        ) -> str:
    """
    Format the reading loads as a table with a column per language.

    Each language has three columns: its amount of words or graphemes, the
    readable time given to it and its time past the reading budget. A caption
    fully covered by pauses is marked in the ``Unreadable`` column.

    :param rows: the reading loads, as returned by `reading_table`
    :param seconds_decimals: the number of decimals of the seconds
    :returns: the table as text, one line per caption
    """
    languages = list(dict.fromkeys(
        load.language for row in rows for load in row.languages
        ))
    rules = {
        load.language: load.counting.value
        for row in rows for load in row.languages
        }

    header = ['Cue', 'Start', 'End', 'Unreadable']
    for language in languages:
        header.extend((
            f'{language} {rules[language]}',
            f'{language} readable (s)',
            f'{language} excess (s)'
            ))

    lines = [' | '.join(header)]
    for row in rows:
        values = [
            str(row.index + 1),
            str(row.start),
            str(row.end),
            'yes' if row.unreadable else 'no'
            ]
        loads = {load.language: load for load in row.languages}
        for language in languages:
            load = loads.get(language)
            if load is None:
                values.extend(('-', '-', '-'))
            else:
                values.extend((
                    str(load.amount),
                    _format_seconds(
                        load.readable_seconds, seconds_decimals
                        ),
                    _format_seconds(
                        load.excess_seconds, seconds_decimals
                        )
                    ))
        lines.append(' | '.join(values))
    return '\n'.join(lines)


def _language_specs(
        languages: Sequence[LanguageSpec]
        ) -> list[LanguageSpec]:
    """
    Validate the reading rules and return them.

    :param languages: the reading rule of each language
    :returns: the rules
    :raises ReadingConfigError: if there is no rule or two share a code
    """
    if not languages:
        raise ReadingConfigError('At least one language rule is required')
    if not all(isinstance(spec, LanguageSpec) for spec in languages):
        raise ReadingConfigError(
            'Each language rule must be a LanguageSpec instance'
            )

    codes = [spec.language for spec in languages]
    if len(set(codes)) != len(codes):
        duplicates = sorted({
            code for code in codes if codes.count(code) > 1
            })
        raise ReadingConfigError(
            f'Duplicate language rules: {", ".join(duplicates)}'
            )
    return list(languages)


def _check_languages(
        texts: dict[str, str],
        specs: Sequence[LanguageSpec],
        index: int
        ) -> None:
    """
    Check that every language of a caption has a rule and one time strategy.

    :param texts: the text of each language of the caption
    :param specs: the reading rules
    :param index: the index of the caption, for the error message
    :raises ReadingConfigError: if a language has no rule or the strategies
        of the languages of the caption differ
    """
    known = {spec.language: spec for spec in specs}
    unknown = sorted(set(texts) - set(known))
    if unknown:
        raise ReadingConfigError(
            f'Caption {index + 1} uses language without a reading rule: '
            f'{", ".join(unknown)}'
            )

    strategies = {known[language].sharing for language in texts}
    if len(strategies) > 1:
        raise ReadingConfigError(
            f'Caption {index + 1} mixes independent and shared reading '
            'times'
            )


def _caption_text_by_language(
        raw_text: str,
        default_language: str
        ) -> dict[str, str]:
    """
    Split the raw text of a caption on its ``<lang>`` tags.

    The other cue tags are removed and their text is kept. Text outside a
    language span is attributed to the default language.

    :param raw_text: the text of the caption including its cue tags
    :param default_language: the language of text outside a ``<lang>`` span
    :returns: the text of each language, in the order in which it appears
    """
    pieces: dict[str, list[str]] = {}
    stack: list[str] = []

    def add(text: str) -> None:
        if text:
            language = stack[-1] if stack else default_language
            pieces.setdefault(language, []).append(text)

    for line in raw_text.split('\n'):
        for token, is_tag in _iter_cue_text_tokens(line):
            if not is_tag:
                add(token)
                continue
            content = token.strip()
            if content == '/lang':
                if stack:
                    stack.pop()
                continue
            match = _LANGUAGE_OPEN.fullmatch(content)
            if match:
                stack.append(match.group(1))
    return {
        language: normalized
        for language, parts in pieces.items()
        if (normalized := ' '.join(' '.join(parts).split()))
        }


def _subtract_pauses(
        start_ms: int,
        end_ms: int,
        pauses: Sequence[tuple[int, int]]
        ) -> list[tuple[int, int]]:
    """
    Return the readable intervals of a caption after the pauses are removed.

    :param start_ms: the start of the caption in milliseconds
    :param end_ms: the end of the caption in milliseconds
    :param pauses: the merged narration pauses in milliseconds
    :returns: the intervals of the caption not covered by a pause
    """
    if end_ms <= start_ms:
        return []

    intervals: list[tuple[int, int]] = [(start_ms, end_ms)]
    for pause_start, pause_end in pauses:
        remaining: list[tuple[int, int]] = []
        for seg_start, seg_end in intervals:
            if pause_end <= seg_start or pause_start >= seg_end:
                remaining.append((seg_start, seg_end))
                continue
            if pause_start > seg_start:
                remaining.append((seg_start, pause_start))
            if pause_end < seg_end:
                remaining.append((pause_end, seg_end))
        intervals = remaining
    return intervals


def _language_loads(
        specs: Sequence[LanguageSpec],
        texts: dict[str, str],
        readable_ms: int,
        unreadable: bool
        ) -> list[LanguageLoad]:
    """
    Return the load of each language present in a caption.

    :param specs: the rules of the languages present in the caption
    :param texts: the text of each language
    :param readable_ms: the readable time of the caption in milliseconds
    :param unreadable: whether the whole caption is covered by pauses
    :returns: the load of each language, in the order of the rules
    """
    if not specs:
        return []

    if specs[0].sharing is TimeSharing.SHARED:
        allocations = _shared_time(specs, readable_ms)
    else:
        allocations = {spec.language: readable_ms for spec in specs}

    loads = []
    for spec in specs:
        amount = _count(texts[spec.language], spec.counting)
        allowed_seconds = allocations[spec.language] / 1000
        loads.append(LanguageLoad(
            language=spec.language,
            counting=spec.counting,
            amount=amount,
            readable_seconds=allowed_seconds,
            required_seconds=amount / spec.rate,
            unreadable=unreadable or allocations[spec.language] == 0
            ))
    return loads


def _shared_time(
        specs: Sequence[LanguageSpec],
        readable_ms: int
        ) -> dict[str, int]:
    """
    Allocate the readable time between languages that share it.

    The time is split by the weights of the languages. Each share is rounded
    down to a millisecond and the milliseconds left are given to the shares
    with the largest remainders, so the allocations add up to the readable
    time.

    :param specs: the rules of the languages present in the caption
    :param readable_ms: the readable time in milliseconds
    :returns: the milliseconds given to each language code
    """
    total_weight = sum(spec.weight for spec in specs)
    raw = {
        spec.language: readable_ms * spec.weight / total_weight
        for spec in specs
        }
    allocations = {language: int(share) for language, share in raw.items()}
    remainder = readable_ms - sum(allocations.values())
    for language in sorted(
            raw, key=lambda code: raw[code] % 1, reverse=True
            ):
        if remainder == 0:
            break
        allocations[language] += 1
        remainder -= 1
    return allocations


def _count(text: str, rule: CountingRule) -> int:
    """
    Count text with the rule of its language.

    :param text: the text to count
    :param rule: words or visible graphemes
    :returns: the amount
    """
    if rule is CountingRule.WORDS:
        return count_words(text)
    return count_graphemes(text)


def _is_positive_number(value: float) -> bool:
    """
    Return whether a value is a finite number greater than zero.

    :param value: the value to check
    :returns: whether it is a positive int or float, booleans excluded
    """
    return (
        type(value) in (int, float) and
        not isinstance(value, bool) and
        math.isfinite(value) and
        value > 0
        )


def _format_seconds(seconds: float, decimals: int) -> str:
    """
    Format a number of seconds with a fixed number of decimals.

    :param seconds: the value in seconds
    :param decimals: the number of decimals
    :returns: the formatted value
    """
    return f'{seconds:.{decimals}f}'
