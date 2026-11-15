"""Models module."""

import bisect
import copy
import datetime
import decimal
import fractions
import functools
import html
import math
import re
import typing
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace

from .errors import MalformedCaptionError, MalformedRegionError
from .utils import _split_lines

# a context of our own, so rounding does not depend on the decimal context
# of the caller
_DECIMAL_CONTEXT = decimal.Context()

# the largest timestamp, 9999999999:59:59.999, as hours have up to 10 digits,
# far more than any video lasts, so times past it are rejected the same on
# every Python version instead of converting huge numbers, which is slow
# before Python 3.11 and raises ValueError after it
_MAX_MILLISECONDS = 10 ** 10 * 3_600_000 - 1
_OUT_OF_RANGE = 'Time after the largest timestamp 9999999999:59:59.999'


@functools.total_ordering
class Timestamp:
    """Representation of a timestamp."""

    # hours have up to 10 digits, see _MAX_MILLISECONDS
    PATTERN = re.compile(
        r'(?:([0-9]{1,10}):)?([0-9]{1,2}):([0-9]{1,2})\.([0-9]{1,3})'
        )

    def __init__(
            self,
            hours: int = 0,
            minutes: int = 0,
            seconds: int = 0,
            milliseconds: int = 0
            ):
        """Initialize."""
        self.hours = hours
        self.minutes = minutes
        self.seconds = seconds
        self.milliseconds = milliseconds

    def __str__(self) -> str:
        """Return the string representation of the timestamp."""
        return (
            f'{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}'
            f'.{self.milliseconds:03d}'
            )

    def to_tuple(self) -> tuple[int, int, int, int]:
        """Return the timestamp in tuple form."""
        return self.hours, self.minutes, self.seconds, self.milliseconds

    def __repr__(self) -> str:
        """Return the string representation of the caption."""
        return (f'<{self.__class__.__name__} '
                f'hours={self.hours} '
                f'minutes={self.minutes} '
                f'seconds={self.seconds} '
                f'milliseconds={self.milliseconds}>'
                )

    def __eq__(self, other: object) -> bool:
        """Compare equality with other object."""
        if not isinstance(other, Timestamp):
            return NotImplemented
        return self.to_tuple() == other.to_tuple()

    def __lt__(self, other: 'Timestamp') -> bool:
        """Compare less than with other object."""
        if not isinstance(other, Timestamp):
            return NotImplemented
        return self.to_tuple() < other.to_tuple()

    @classmethod
    def from_string(cls, value: str) -> 'Timestamp':
        """
        Return a `Timestamp` instance from a string value.

        The fields can be shorter than the WebVTT format requires, as in
        ``0:1:5.2``, and the fraction of a second is padded on the right so
        ``.2`` is read as 200 milliseconds. The hours can have up to 10
        digits.

        :param value: the timestamp in ``[hh:]mm:ss.mmm`` format
        :returns: a `Timestamp` instance

        :raises MalformedCaptionError: if the timestamp is not valid
        """
        if type(value) is not str:
            raise MalformedCaptionError(f'Invalid timestamp {value!r}')

        match = cls.PATTERN.fullmatch(value)
        if not match:
            raise MalformedCaptionError(f'Invalid timestamp {value!r}')

        hours, minutes, seconds, fraction = match.groups()
        minutes, seconds = int(minutes), int(seconds)
        if minutes > 59 or seconds > 59:
            raise MalformedCaptionError(f'Invalid timestamp {value!r}')

        return cls(int(hours or 0),
                   minutes,
                   seconds,
                   int(fraction.ljust(3, '0'))
                   )

    @classmethod
    def from_seconds(cls, value: float) -> 'Timestamp':
        """
        Return a `Timestamp` instance from a number of seconds.

        The value is rounded to the nearest millisecond as written, with
        halves rounded up, so ``1.9996`` and ``1.9995`` are read as
        ``00:00:02.000``.

        :param value: the timestamp in seconds
        :returns: a `Timestamp` instance

        :raises MalformedCaptionError: if the value is not a finite number
            of seconds greater than or equal to zero, or is after the
            largest timestamp
        """
        if (
                type(value) is bool or
                not isinstance(value, (int, float)) or
                not _is_finite(value) or
                value < 0
                ):
            raise MalformedCaptionError(f'Invalid timestamp {value!r}')

        try:
            return cls._from_milliseconds(_in_milliseconds(value))
        except ValueError as error:
            raise MalformedCaptionError(str(error)) from None

    @classmethod
    def _from_milliseconds(cls, value: int) -> 'Timestamp':
        """
        Return a `Timestamp` instance from a number of milliseconds.

        :param value: the timestamp in milliseconds, zero or greater
        :returns: a `Timestamp` instance

        :raises ValueError: if the value is after the largest timestamp
        """
        if value > _MAX_MILLISECONDS:
            raise ValueError(_OUT_OF_RANGE)

        hours, remainder = divmod(value, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)

        return cls(hours, minutes, seconds, milliseconds)

    def in_seconds(self) -> int:
        """
        Return the timestamp in seconds.

        The milliseconds are left out, use `in_milliseconds` for the exact
        value.
        """
        return (self.hours * 3600 +
                self.minutes * 60 +
                self.seconds
                )

    def in_milliseconds(self) -> int:
        """Return the timestamp in milliseconds."""
        return self.in_seconds() * 1000 + self.milliseconds


def _is_finite(value: float | fractions.Fraction) -> bool:
    """
    Return whether a number is finite.

    Integers and fractions are always finite, and are not passed to
    ``math.isfinite``, which raises OverflowError for those too large to
    convert to a float.
    """
    return not isinstance(value, float) or math.isfinite(value)


def _in_milliseconds(value: float | datetime.timedelta) -> int:
    """
    Return a number of seconds or a ``timedelta`` in milliseconds.

    The value is rounded to the nearest millisecond with halves rounded away
    from zero. A float is rounded as written, from its shortest
    representation, so ``259.0395`` is 259040 milliseconds although the
    closest float is slightly smaller. A number past the range of a
    timestamp, in either direction, is returned as one millisecond past it,
    as converting a huge number can overflow.

    :param value: a finite number of seconds or a ``timedelta``
    :returns: the value in milliseconds
    """
    if isinstance(value, datetime.timedelta):
        seconds = decimal.Decimal(
            value // datetime.timedelta(microseconds=1)
            ).scaleb(-6, context=_DECIMAL_CONTEXT)
    elif abs(value) >= (_MAX_MILLISECONDS + 1) // 1000:
        return _MAX_MILLISECONDS + 1 if value > 0 else -_MAX_MILLISECONDS - 1
    elif isinstance(value, float):
        seconds = decimal.Decimal(float.__repr__(value))
    else:
        seconds = decimal.Decimal(value)

    return int(
        seconds.scaleb(3, context=_DECIMAL_CONTEXT).to_integral_value(
            rounding=decimal.ROUND_HALF_UP,
            context=_DECIMAL_CONTEXT
            )
        )


def _offset_in_milliseconds(
        offset: float | datetime.timedelta,
        name: str = 'offset'
        ) -> int:
    """
    Return a shift offset in milliseconds.

    :param offset: a number of seconds or a ``timedelta``
    :param name: what the value is, for the error messages
    :returns: the offset rounded to the nearest millisecond, halves away
        from zero

    :raises TypeError: if the offset is not a number or a ``timedelta``
    :raises ValueError: if the offset is not finite
    """
    if isinstance(offset, datetime.timedelta):
        return _in_milliseconds(offset)

    if type(offset) is bool or not isinstance(offset, (int, float)):
        raise TypeError(f'Invalid {name} {offset!r}')

    if not _is_finite(offset):
        raise ValueError(f'Invalid {name} {offset!r}')

    return _in_milliseconds(offset)


_Position = str | Timestamp | float | datetime.timedelta


def _position_in_milliseconds(value: _Position) -> int:
    """
    Return a position in the captions in milliseconds.

    :param value: a timestamp, as a string or a `Timestamp`, a number of
        seconds or a ``timedelta``
    :returns: the position rounded to the nearest millisecond, halves away
        from zero

    :raises MalformedCaptionError: if the timestamp string is not valid
    :raises TypeError: if the value is not of a supported type
    :raises ValueError: if the value is not finite, is before zero or is
        after the largest timestamp
    """
    if isinstance(value, str):
        return Timestamp.from_string(value).in_milliseconds()

    if isinstance(value, Timestamp):
        if not all(type(field) is int for field in value.to_tuple()):
            raise TypeError(f'Invalid timestamp {value!r}')
        milliseconds = value.in_milliseconds()
    else:
        milliseconds = _offset_in_milliseconds(value, name='timestamp')

    if milliseconds < 0:
        raise ValueError(f'Invalid timestamp {value!r}')
    if milliseconds > _MAX_MILLISECONDS:
        raise ValueError(_OUT_OF_RANGE)

    return milliseconds


def _to_timestamp(value: _Position) -> Timestamp:
    """
    Return a time of a caption or of a range as a new `Timestamp`.

    The value is read as the times of ``sync``, and a `Timestamp` is
    copied, so it is not shared with where it comes from.

    :param value: a timestamp, as a string or a `Timestamp`, a number of
        seconds or a ``timedelta``
    :returns: the time rounded to the nearest millisecond, halves away from
        zero

    :raises MalformedCaptionError: if the value is not of a supported type,
        is not finite, is before zero or is after the largest timestamp
    """
    if isinstance(value, str):
        return Timestamp.from_string(value)

    try:
        milliseconds = _position_in_milliseconds(value)
    except (TypeError, ValueError) as error:
        raise MalformedCaptionError(str(error)) from None

    return Timestamp._from_milliseconds(milliseconds)


def _optional_timestamp(
        value: _Position | None
        ) -> Timestamp | None:
    """
    Return a time that may be missing as a new `Timestamp`.

    :param value: the time, or None or an empty string when it is missing,
        as times were only strings before and an empty one was accepted
    :returns: the time as with `_to_timestamp`, or None when it is missing

    :raises MalformedCaptionError: if the value is not a valid time
    """
    if value is None or value == '':
        return None
    return _to_timestamp(value)


def _time_or_zero(value: _Position | None) -> Timestamp:
    """
    Return a time that may be missing as a new `Timestamp`, or zero.

    :param value: the time, or None or an empty string when it is missing
    :returns: the time as with `_to_timestamp`, or ``00:00:00.000`` when it
        is missing

    :raises MalformedCaptionError: if the value is not a valid time
    """
    timestamp = _optional_timestamp(value)
    return Timestamp() if timestamp is None else timestamp


def _divide_rounded(numerator: int, denominator: int) -> int:
    """
    Divide two integers rounding to the nearest integer, halves away from zero.

    :param numerator: the dividend
    :param denominator: the divisor, greater than zero
    :returns: the rounded quotient
    """
    quotient, remainder = divmod(abs(numerator), denominator)
    quotient += 2 * remainder >= denominator
    return quotient if numerator >= 0 else -quotient


def _sync_mapping(
        points: Sequence[Sequence[_Position]]
        ) -> Callable[[int], int]:
    """
    Return the function mapping times in milliseconds for sync points.

    Times are mapped along the line between the two neighbouring points,
    and before the first or after the last point along the line of the
    outermost points.

    :param points: two or more ``(from, to)`` pairs
    :returns: the mapping of milliseconds to milliseconds

    :raises MalformedCaptionError: if a timestamp string is not valid
    :raises TypeError: if a point is not a pair of supported values
    :raises ValueError: if there are fewer than two points, two points have
        the same ``from`` time, or the points would change the order of the
        captions
    """
    if len(points) < 2:
        raise ValueError('At least two sync points are required')

    pairs = sorted(_sync_point(point) for point in points)
    for (from_1, to_1), (from_2, to_2) in zip(pairs, pairs[1:]):
        if from_1 == from_2:
            raise ValueError(
                'Two sync points have the same time '
                f'{Timestamp._from_milliseconds(from_1)}'
                )
        if to_1 >= to_2:
            raise ValueError(
                'Sync points must keep the order of the captions, '
                f'{Timestamp._from_milliseconds(from_1)} is moved to '
                f'{Timestamp._from_milliseconds(to_1)} but '
                f'{Timestamp._from_milliseconds(from_2)} to '
                f'{Timestamp._from_milliseconds(to_2)}'
                )

    from_times = [from_time for from_time, _ in pairs]

    def mapping(milliseconds: int) -> int:
        index = bisect.bisect_right(from_times, milliseconds) - 1
        index = min(max(index, 0), len(pairs) - 2)
        (from_1, to_1), (from_2, to_2) = pairs[index], pairs[index + 1]
        return _divide_rounded(
            to_1 * (from_2 - from_1) + (milliseconds - from_1) * (to_2 - to_1),
            from_2 - from_1
            )

    return mapping


def _sync_point(point: Sequence[_Position]) -> tuple[int, int]:
    """
    Return a ``(from, to)`` sync point in milliseconds.

    :param point: a pair of positions
    :returns: the pair in milliseconds

    :raises TypeError: if the point is not a pair
    """
    if not isinstance(point, (tuple, list)) or len(point) != 2:
        raise TypeError(f'Invalid sync point {point!r}')

    return (_position_in_milliseconds(point[0]),
            _position_in_milliseconds(point[1]))


_FrameRate = float | fractions.Fraction


def _framerate_mapping(
        from_fps: _FrameRate,
        to_fps: _FrameRate
        ) -> Callable[[int], int]:
    """
    Return the function mapping times in milliseconds between frame rates.

    :param from_fps: the frame rate the captions are timed for
    :param to_fps: the frame rate of the video
    :returns: the mapping of milliseconds to milliseconds

    :raises TypeError: if a frame rate is not a number
    :raises ValueError: if a frame rate is not finite or not above zero
    """
    ratio = _frame_rate(from_fps) / _frame_rate(to_fps)
    return lambda milliseconds: _divide_rounded(
        milliseconds * ratio.numerator,
        ratio.denominator
        )


def _frame_rate(value: _FrameRate) -> fractions.Fraction:
    """
    Return a frame rate as an exact fraction, as written.

    :param value: the frame rate, e.g. ``25``, ``23.976`` or
        ``Fraction(24000, 1001)``
    :returns: the frame rate as a fraction

    :raises TypeError: if the frame rate is not a number
    :raises ValueError: if the frame rate is not finite or not above zero
    """
    if (
            type(value) is bool or
            not isinstance(value, (int, float, fractions.Fraction))
            ):
        raise TypeError(f'Invalid frame rate {value!r}')

    if not _is_finite(value) or value <= 0:
        raise ValueError(f'Invalid frame rate {value!r}')

    if isinstance(value, float):
        return fractions.Fraction(decimal.Decimal(float.__repr__(value)))

    return fractions.Fraction(value)


def _iter_cue_text_tokens(
        line: str
        ) -> Generator[tuple[str, bool], None, None]:
    """
    Iterate the text and the cue tags of a single line of text.

    A ``<`` without a ``>`` after it is part of the text. The scan stops at
    the first one, as no later ``<`` on the line has a ``>`` after it either,
    which keeps it linear in the length of the line.

    :param line: a line of cue text
    :returns: tuples of the text or the tag content and true for a tag
    """
    pos = 0
    while True:
        start = line.find('<', pos)
        if start < 0:
            break
        end = line.find('>', start + 1)
        if end < 0:
            break
        if start > pos:
            yield line[pos:start], False
        yield line[start + 1:end], True
        pos = end + 1
    if pos < len(line):
        yield line[pos:], False


def _strip_cue_text_tags(line: str) -> str:
    """
    Remove cue tags from a single line of text.

    Gives the same result as ``re.sub('<.*?>', '', line)`` for a line
    without a newline, in linear time. That regex rescans the rest of the
    line from every ``<`` that has no ``>`` after it, which is quadratic in
    the length of the line.
    """
    return ''.join(
        token for token, is_tag in _iter_cue_text_tokens(line) if not is_tag
        )


# a character reference, as in ``&amp;``, ``&#38;`` or ``&#x26;``
_ENTITY_REFERENCE = re.compile(
    r'&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});'
    )

_UnknownReason = typing.Literal['backwards', 'outside']


@dataclass(frozen=True)
class UnknownBoundary:
    """
    A part of a cue whose words have no known time they become visible.

    A timestamp tag that goes back in time or is outside the cue makes the
    text after it uncertain: the tag still says when the text may become
    visible, but that time cannot be trusted, so the text is neither shown
    nor hidden when a query falls in the uncertain range.

    ``after`` is inclusive and ``before`` is exclusive, as the times of a
    cue are: a part that may become visible at ``after`` is visible at
    that time, while one that may become visible at ``before`` is not.

    :param start: offset of the first character of the part in the raw text
        of the cue
    :param end: offset after the last character of the part in the raw text
        of the cue
    :param line: index of the line of the part in the cue, from 0
    :param column: offset of the part in its line, from 0
    :param reason: ``backwards`` for a timestamp tag before the previous
        one, or ``outside`` for one before the start or after the end of
        the cue
    :param after: the earliest time the part may become visible, or None
        when the boundaries are unknown, as for a tag outside the cue
    :param before: the latest time the part may become visible, or None
        when the boundaries are unknown
    :param cue_index: index of the caption in the captions of the `WebVTT`
        instance, from 0, or None for a caption
    :param cue_identifier: identifier of the caption, if any
    :param cue_line: line where the cue starts in the file it was read from,
        from 1, or None when it was not read from a file
    """

    start: int
    end: int
    line: int
    column: int
    reason: _UnknownReason
    after: Timestamp | None = None
    before: Timestamp | None = None
    cue_index: int | None = None
    cue_identifier: str | None = None
    cue_line: int | None = None

    @property
    def bounded(self) -> bool:
        """Return whether the possible times of the part are known."""
        return self.after is not None and self.before is not None

    def __str__(self) -> str:
        """Return the reason and the possible times of the part."""
        if self.after is None or self.before is None:
            return f'{self.reason}: unknown time'
        return f'{self.reason}: {self.after} to {self.before}'


@dataclass(frozen=True)
class WordTiming:
    """
    A word of a cue, with where it is and when it becomes visible.

    :param text: the word, with character references decoded and cue tags
        left out
    :param start: offset of the first character of the word in the raw text
        of the cue
    :param end: offset after the last character of the word in the raw text
        of the cue, which includes tags and references inside it
    :param text_start: offset of the first character of the word in the
        text of the cue with the tags left out and the references decoded
    :param text_end: offset after the last character of the word in that
        text
    :param line: index of the line of the word in the cue, from 0
    :param column: offset of the word in its line of the raw text, from 0
    :param visible_from: the time the word becomes visible, or None when
        the time is unknown, as for a word in an `UnknownBoundary`
    :param unknown: the uncertain part the word is in, or None
    :param cue_index: index of the caption in the captions of the `WebVTT`
        instance, from 0, or None for a caption
    :param cue_identifier: identifier of the caption, if any
    :param cue_line: line where the cue starts in the file it was read from,
        from 1, or None when it was not read from a file
    """

    text: str
    start: int
    end: int
    text_start: int
    text_end: int
    line: int
    column: int
    visible_from: Timestamp | None = None
    unknown: UnknownBoundary | None = None
    cue_index: int | None = None
    cue_identifier: str | None = None
    cue_line: int | None = None

    def is_visible_at(self, at: _Position) -> bool | None:
        """
        Return whether the word is visible at a time.

        :param at: the time, a timestamp string, a `Timestamp`, a number of
            seconds or a ``timedelta``
        :returns: True or False when the word is clearly visible or not,
            and None when its time is uncertain at that time

        :raises MalformedCaptionError: if the time is not valid
        """
        time = _to_timestamp(at)
        if self.visible_from is not None:
            return self.visible_from <= time
        unknown = self.unknown
        if unknown is None:
            return False
        if unknown.after is None or unknown.before is None:
            return None
        if time < unknown.after:
            return False
        if time >= unknown.before:
            return True
        return None


@dataclass(frozen=True)
class WordQuery:
    """
    The words of a cue or of a file that are visible at a time.

    :param at: the time of the query
    :param words: the words that are visible at the time
    :param unknown: the uncertain parts of the query time, which are left
        out of ``words`` as they may or may not be visible
    """

    at: Timestamp
    words: tuple[WordTiming, ...]
    unknown: tuple[UnknownBoundary, ...]

    def __iter__(self) -> Iterator[WordTiming]:
        """Iterate the visible words."""
        return iter(self.words)

    def __getitem__(
            self,
            index: int | slice
            ) -> WordTiming | tuple[WordTiming, ...]:
        """Return a visible word by index, or words by slice."""
        return self.words[index]

    def __len__(self) -> int:
        """Return the number of visible words."""
        return len(self.words)

    def __bool__(self) -> bool:
        """Return whether any word is visible."""
        return bool(self.words)

    @property
    def text(self) -> str:
        """Return the visible words separated by spaces."""
        return ' '.join(word.text for word in self.words)


@dataclass(frozen=True)
class WordTimeline:
    """
    The words of a cue, with their times and the uncertain parts.

    :param words: the words in the raw order of the cue
    :param unknown: the parts of the cue with no known time
    :param start: the start time of the cue
    :param end: the end time of the cue
    :param plain_text: the text of the cue without its tags and with its
        character references decoded, which the text offsets of the words
        refer to
    """

    words: tuple[WordTiming, ...]
    unknown: tuple[UnknownBoundary, ...]
    start: Timestamp
    end: Timestamp
    plain_text: str = ''

    def at(self, at: _Position) -> WordQuery:
        """
        Return the words visible at a time.

        Text before the first timestamp tag is visible from the start of
        the cue. A word in an uncertain part is left out when the time is
        in the uncertain range, and is only returned once the latest
        possible time has passed.

        :param at: the time, a timestamp string, a `Timestamp`, a number of
            seconds or a ``timedelta``
        :returns: a `WordQuery` with the visible words and the uncertain
            parts at that time

        :raises MalformedCaptionError: if the time is not valid
        """
        time = _to_timestamp(at)
        return _query_timeline(self, time)

    def __iter__(self) -> Iterator[WordTiming]:
        """Iterate the words in the raw order of the cue."""
        return iter(self.words)

    def __len__(self) -> int:
        """Return the number of words."""
        return len(self.words)

    @property
    def text(self) -> str:
        """Return the words separated by spaces."""
        return ' '.join(word.text for word in self.words)


@dataclass(frozen=True)
class _Segment:
    """A part of a cue between two timestamp tags, or the cue edges."""

    start: int
    end: int
    visible_from: Timestamp | None
    unknown: UnknownBoundary | None


def _query_timeline(
        timeline: WordTimeline,
        at: Timestamp
        ) -> WordQuery:
    """
    Return the words of a timeline visible at a time.

    Text in an uncertain part is left out while the time is in its
    uncertain range, and is returned once even the latest possible time
    it may become visible has passed.

    :param timeline: the timeline of the words
    :param at: the time of the query
    :returns: a `WordQuery` instance
    """
    if not (timeline.start <= at < timeline.end):
        return WordQuery(at, (), ())

    unknown = tuple(
        part for part in timeline.unknown if _is_uncertain(part, at)
        )
    words = []
    for word in timeline.words:
        if word.visible_from is not None:
            if word.visible_from <= at:
                words.append(word)
            continue
        if word.unknown is None or word.unknown.before is None:
            continue
        if at >= word.unknown.before:
            words.append(word)

    return WordQuery(at, tuple(words), unknown)


def _is_uncertain(part: UnknownBoundary, at: Timestamp) -> bool:
    """
    Return whether a part may become visible at a time.

    :param part: the uncertain part
    :param at: the time of the query
    :returns: true when the part has no known boundaries, or when at is
        from its earliest to its latest possible time
    """
    if part.after is None or part.before is None:
        return True
    return part.after <= at < part.before


def _iter_raw_tags(
        raw_text: str
        ) -> Generator[tuple[str, int, int], None, None]:
    """
    Iterate the tags of a cue text and where they are.

    Works as `_iter_cue_text_tokens` but with the offsets of the tags in
    the raw text, so a word can be traced back to where it is written. A
    ``<`` without a ``>`` after it on its line is part of the text, as a
    tag cannot span lines.

    :param raw_text: the raw text of a cue
    :returns: tuples of the content of a tag, its offset and the offset
        after its closing ``>``
    """
    line_start = 0
    while line_start <= len(raw_text):
        line_end = raw_text.find('\n', line_start)
        if line_end < 0:
            line_end = len(raw_text)
        pos = line_start
        while True:
            start = raw_text.find('<', pos, line_end)
            if start < 0:
                break
            end = raw_text.find('>', start + 1, line_end)
            if end < 0:
                break
            yield raw_text[start + 1:end], start, end + 1
            pos = end + 1
        line_start = line_end + 1


def _markers(
        raw_text: str
        ) -> list[tuple[int, int, Timestamp]]:
    """
    Return the timestamp tags of a cue text in their raw order.

    A tag whose content is a timestamp is a timestamp tag; other cue tags,
    such as ``<c.foo>`` or ``<v Bob>``, are left out. A timestamp that is
    not valid, as in ``<00:0x.000>``, is read as a cue tag, as the parser
    reads any other tag.

    :param raw_text: the raw text of a cue
    :returns: tuples of the offset of the tag, the offset after it and its
        time
    """
    markers = []
    for content, start, end in _iter_raw_tags(raw_text):
        try:
            time = Timestamp.from_string(content)
        except MalformedCaptionError:
            continue
        markers.append((start, end, time))
    return markers


def _trimmed_span(raw_text: str, start: int, end: int) -> tuple[int, int]:
    """
    Return the offsets of a part of a cue text without its spaces.

    :param raw_text: the raw text of the cue
    :param start: the offset of the first character of the part
    :param end: the offset after its last character
    :returns: the offsets of the first non-space character and after the
        last one, which are both ``start`` when there is no text
    """
    while start < end and raw_text[start].isspace():
        start += 1
    while end > start and raw_text[end - 1].isspace():
        end -= 1
    return start, end


def _line_starts(text: str) -> list[int]:
    """
    Return the offsets of the first character of each line.

    :param text: the text, with lines separated by LF
    :returns: the offsets, the first one zero
    """
    starts = [0]
    starts.extend(match.end() for match in re.finditer('\n', text))
    return starts


def _line_and_column(starts: Sequence[int], offset: int) -> tuple[int, int]:
    """
    Return the line and the column of an offset.

    :param starts: the offsets of the first character of each line
    :param offset: an offset in the text
    :returns: the index of the line, from 0, and the offset in it, from 0
    """
    line = bisect.bisect_right(starts, offset) - 1
    return line, offset - starts[line]


def _boundary_position(
        raw_text: str,
        starts: Sequence[int],
        offset: int
        ) -> tuple[int, int]:
    """
    Return the line and column of the first character of a part.

    A part that starts at the end of a line starts at the next one.

    :param raw_text: the raw text of the cue
    :param starts: the offsets of the first character of each line
    :param offset: the offset of the first character of the part
    :returns: the index of the line, from 0, and the offset in it, from 0
    """
    if offset < len(raw_text) and raw_text[offset] == '\n':
        offset += 1
    if offset == len(raw_text) and raw_text.endswith('\n'):
        return len(starts) - 1, 0
    return _line_and_column(starts, offset)


def _text_characters(
        raw_text: str,
        start: int,
        end: int
        ) -> Generator[tuple[str, int, int], None, None]:
    """
    Iterate the characters of a raw part of a cue text.

    Character references are decoded, so ``&lt;`` is one ``<``, and each
    decoded character carries the raw part it comes from. A reference that
    is not known is left as it is written, as `html.unescape` does.

    :param raw_text: the raw text of the cue
    :param start: the offset of the first character of the part
    :param end: the offset after its last character
    :returns: tuples of a character, its raw offset and the offset after it
    """
    pos = start
    for match in _ENTITY_REFERENCE.finditer(raw_text, start, end):
        for offset in range(pos, match.start()):
            yield raw_text[offset], offset, offset + 1
        decoded = html.unescape(match.group())
        for character in decoded:
            yield character, match.start(), match.end()
        pos = match.end()
    for offset in range(pos, end):
        yield raw_text[offset], offset, offset + 1


def _segment_characters(
        raw_text: str,
        segment: _Segment
        ) -> Generator[tuple[str, int, int], None, None]:
    """
    Iterate the text characters of a segment, without its cue tags.

    Cue tags take no room in the text, so a word broken by a style tag, as
    in ``he<b>llo``, is still one word, and the character references are
    decoded. A tag cannot span lines, so a ``<`` without a ``>`` after it
    on its line, and the text after it, are text.

    :param raw_text: the raw text of the cue
    :param segment: the part of the cue
    :returns: tuples of a character, its raw offset and the offset after it
    """
    pos = segment.start
    while pos < segment.end:
        tag_start = raw_text.find('<', pos, segment.end)
        if tag_start < 0:
            yield from _text_characters(raw_text, pos, segment.end)
            break
        line_end = raw_text.find('\n', tag_start, segment.end)
        if line_end < 0:
            line_end = segment.end
        tag_end = raw_text.find('>', tag_start + 1, line_end)
        if tag_end < 0:
            # the ``<`` is text, and so is the rest of the segment unless
            # a later ``<`` on the same line closes a tag
            yield from _text_characters(raw_text, pos, tag_start + 1)
            pos = tag_start + 1
            continue
        yield from _text_characters(raw_text, pos, tag_start)
        pos = tag_end + 1


def _timed_word_timeline(
        lines: Sequence[str],
        start: Timestamp,
        end: Timestamp
        ) -> WordTimeline:
    """
    Return the words of a cue with their times.

    A timestamp tag gives the time the text after it becomes visible. The
    text before the first tag is visible from the start of the cue. A tag
    before the previous one, or outside the cue, makes the part after it
    uncertain: its possible times are kept when the tag goes back, and are
    unknown when the tag is outside the cue.

    :param lines: the lines of the cue text
    :param start: the start time of the cue
    :param end: the end time of the cue
    :returns: a `WordTimeline` instance
    """
    raw_text = '\n'.join(lines)
    starts = _line_starts(raw_text)
    markers = _markers(raw_text)

    segments: list[_Segment] = []
    # the latest time a trusted tag can have, which the text before a tag
    # is known to be visible from
    floor = start
    first = markers[0][0] if markers else len(raw_text)
    segments.append(_Segment(0, first, start, None))

    for index, (_, marker_end, marker_time) in enumerate(markers):
        segment_end = (markers[index + 1][0]
                       if index + 1 < len(markers)
                       else len(raw_text)
                       )
        if marker_time < start or marker_time > end:
            # the time is not in the cue, so the text after it may be
            # visible at any time of the cue, or never
            line, column = _boundary_position(raw_text, starts, marker_end)
            part = UnknownBoundary(
                marker_end, segment_end, line, column, 'outside'
                )
            segments.append(_Segment(
                marker_end, segment_end, None, part
                ))
        elif marker_time < floor:
            # the tag goes back, so the text after it may be visible from
            # its time to the time of the tag before, and not earlier;
            # the earlier tag still holds its time
            line, column = _boundary_position(raw_text, starts, marker_end)
            part = UnknownBoundary(
                marker_end, segment_end, line, column, 'backwards',
                marker_time, floor
                )
            segments.append(_Segment(
                marker_end, segment_end, None, part
                ))
        else:
            floor = marker_time
            segments.append(_Segment(
                marker_end, segment_end, marker_time, None
                ))

    words: list[WordTiming] = []
    unknown: list[UnknownBoundary] = []
    plain_offset = 0
    for segment in segments:
        if segment.unknown is not None and not any(
                segment.unknown is seen for seen in unknown):
            start_offset, end_offset = _trimmed_span(
                raw_text, segment.unknown.start, segment.unknown.end
                )
            if start_offset < end_offset:
                line, column = _boundary_position(
                    raw_text, starts, start_offset
                    )
                part = replace(
                    segment.unknown,
                    start=start_offset,
                    end=end_offset,
                    line=line,
                    column=column
                    )
                unknown.append(part)
                segment = replace(segment, unknown=part)
            else:
                # the part is only spaces, so it has no words to be
                # uncertain about
                segment = replace(segment, unknown=None)

        current: list[str] = []
        raw_start = raw_end = segment.start
        text_start = plain_offset

        def flush() -> None:
            """Add the word collected so far, if any."""
            nonlocal current, plain_offset
            if not current:
                return
            line, column = _line_and_column(starts, raw_start)
            words.append(WordTiming(
                ''.join(current),
                raw_start,
                raw_end,
                text_start,
                text_start + len(current),
                line,
                column,
                segment.visible_from,
                segment.unknown
                ))
            plain_offset += len(current)
            current = []

        for character, raw_from, raw_to in _segment_characters(
                raw_text, segment):
            if character.isspace():
                flush()
                plain_offset += len(character)
                continue
            if not current:
                raw_start = raw_from
                text_start = plain_offset
            raw_end = raw_to
            current.append(character)

        flush()

    plain_text = ''.join(
        character
        for segment in segments
        for character, _, _ in _segment_characters(raw_text, segment)
        )
    return WordTimeline(
        tuple(words), tuple(unknown), start, end, plain_text
        )


# the characters WebVTT treats as spaces when splitting settings
_SETTINGS_SEPARATOR = re.compile('[ \t\n\f\r]+')
_PERCENTAGE = re.compile(r'[0-9]+(?:\.[0-9]+)?%')
_LINE_NUMBER = re.compile(r'-?[0-9]+')
_NUMBER_OF_LINES = re.compile(r'[0-9]+')


def _is_percentage(value: str) -> bool:
    """Return whether the value is a WebVTT percentage from 0 to 100."""
    return bool(_PERCENTAGE.fullmatch(value)) and float(value[:-1]) <= 100


def _is_valid_line(value: str) -> bool:
    """Return whether the value is valid for the line cue setting."""
    line, separator, alignment = value.partition(',')
    return (
        (_is_percentage(line) or bool(_LINE_NUMBER.fullmatch(line))) and
        (not separator or alignment in ('start', 'center', 'end'))
        )


def _is_valid_position(value: str) -> bool:
    """Return whether the value is valid for the position cue setting."""
    position, separator, alignment = value.partition(',')
    return (
        _is_percentage(position) and
        (not separator or
         alignment in ('line-left', 'center', 'line-right'))
        )


def _is_valid_anchor(value: str) -> bool:
    """Return whether the value is two percentages separated by a comma."""
    x, separator, y = value.partition(',')
    return bool(separator) and _is_percentage(x) and _is_percentage(y)


def _is_valid_identifier(value: str) -> bool:
    """Return whether the value is a valid region identifier."""
    return (
        bool(value) and
        '-->' not in value and
        not _SETTINGS_SEPARATOR.search(value)
        )


def _split_setting(setting: str) -> tuple[str, str] | None:
    """
    Split a setting in name and value at its first colon.

    :param setting: a setting as written, e.g. ``line:0``
    :returns: the name and value, or None when there is no colon or it is
        the first or last character
    """
    name, _, value = setting.partition(':')
    if not name or not value:
        return None
    return name, value


def _setting_name(setting: str) -> str | None:
    """Return the name of a setting, or None when it has no name."""
    parts = _split_setting(setting)
    return parts[0] if parts else None


def _setting_property(name: str, doc: str) -> property:
    """Return a property to get and set the setting with that name."""
    return property(
        lambda self: self._get(name),
        lambda self, value: self._set(name, value),
        doc=doc
        )


_SettingsT = typing.TypeVar('_SettingsT', bound='_Settings')


class _Settings:
    """
    Base for settings written as ``name:value`` and separated by spaces.

    The settings are kept as written and in their original order, so they
    are written back the same, including those that are not valid or not
    known. A property returns the value of its setting when it is valid,
    the last valid one when it is repeated, and validates a new value.
    """

    VALIDATORS: typing.ClassVar[dict[str, Callable[[str], bool]]] = {}

    # what a setting is called in error messages
    _KIND = 'setting'
    # the error raised for settings that are not valid
    _ERROR: typing.ClassVar[type[MalformedCaptionError]] = (
        MalformedCaptionError
        )

    def __init__(self) -> None:
        """Initialize without settings."""
        self._settings: list[str] = []

    def _set_given(self, **values: str | None) -> None:
        """
        Set the settings that are given, in order.

        The settings that are None are not set, as there are none to
        remove yet, which keeps creating empty settings fast.

        :raises MalformedCaptionError: if a value is not valid
        """
        for name, value in values.items():
            if value is not None:
                setattr(self, name, value)

    @classmethod
    def from_string(
            cls: type[_SettingsT],
            value: str,
            strict: bool = False
            ) -> _SettingsT:
        """
        Return an instance from settings separated by spaces.

        :param value: the settings, e.g. ``line:0 align:start``
        :param strict: raise an error on settings that are not known, not
            valid or repeated instead of keeping them
        :returns: an instance with the settings

        :raises MalformedCaptionError: if the value is not a string, or if
            strict and a setting is not known, not valid or repeated
        """
        if type(value) is not str:
            raise cls._ERROR(f'Invalid {cls._KIND}s {value!r}')

        settings = cls()
        settings._settings = [
            setting
            for setting in _SETTINGS_SEPARATOR.split(value)
            if setting
            ]

        if strict:
            names: set[str] = set()
            for setting in settings._settings:
                parts = _split_setting(setting)
                if (not parts or
                        parts[0] not in cls.VALIDATORS or
                        not cls.VALIDATORS[parts[0]](parts[1])):
                    raise cls._ERROR(f'Invalid {cls._KIND} {setting!r}')
                if parts[0] in names:
                    raise cls._ERROR(f'Repeated {cls._KIND} {setting!r}')
                names.add(parts[0])

        return settings

    def __str__(self) -> str:
        """Return the settings separated by a space."""
        return ' '.join(self._settings)

    def __repr__(self) -> str:
        """Return the string representation of the settings."""
        return f'<{self.__class__.__name__} text={str(self)!r}>'

    def __bool__(self) -> bool:
        """Return whether there is any setting."""
        return bool(self._settings)

    def __iter__(self) -> Iterator[str]:
        """Iterate the settings as written, e.g. ``line:0``."""
        return iter(self._settings)

    def __eq__(self, other: object) -> bool:
        """Compare equality with another object."""
        if not isinstance(other, type(self)):
            return False

        return self._settings == other._settings

    def _get(self, name: str) -> str | None:
        """Return the value of the last valid setting with that name."""
        value = None
        for setting in self._settings:
            parts = _split_setting(setting)
            if (parts and
                    parts[0] == name and
                    self.VALIDATORS[name](parts[1])):
                value = parts[1]
        return value

    def _set(self, name: str, value: str | None) -> None:
        """
        Set the setting with that name, or remove it when the value is None.

        Every setting with that name is replaced by a single one where the
        first of them was, or added at the end when there is none.

        :raises MalformedCaptionError: if the value is not valid
        """
        if value is not None and (
                type(value) is not str or
                not self.VALIDATORS[name](value)
                ):
            raise self._ERROR(f'Invalid {name} {self._KIND} {value!r}')

        indexes = [
            index
            for index, setting in enumerate(self._settings)
            if _setting_name(setting) == name
            ]
        for index in reversed(indexes):
            del self._settings[index]
        if value is not None:
            self._settings.insert(
                indexes[0] if indexes else len(self._settings),
                f'{name}:{value}'
                )


class CueSettings(_Settings):
    """
    Representation of the cue settings of a caption.

    The settings are kept as written and in their original order, so they
    are written back the same, including those that are not valid or not
    known. A property returns the value of its setting when it is valid,
    the last valid one when it is repeated, and validates a new value.
    """

    VALIDATORS = {
        'vertical': lambda value: value in ('rl', 'lr'),
        'line': _is_valid_line,
        'position': _is_valid_position,
        'size': _is_percentage,
        'align': lambda value: value in (
            'start', 'center', 'end', 'left', 'right'
            ),
        'region': _is_valid_identifier,
        }

    _KIND = 'cue setting'

    def __init__(self,
                 *,
                 vertical: str | None = None,
                 line: str | None = None,
                 position: str | None = None,
                 size: str | None = None,
                 align: str | None = None,
                 region: str | None = None
                 ):
        """
        Initialize.

        :param vertical: writing direction, ``rl`` or ``lr``
        :param line: line position, as a line number or a percentage with
            an optional alignment, e.g. ``-1`` or ``10%,end``
        :param position: position as a percentage with an optional
            alignment, e.g. ``50%,line-left``
        :param size: size as a percentage, e.g. ``80%``
        :param align: text alignment, ``start``, ``center``, ``end``,
            ``left`` or ``right``
        :param region: identifier of a region

        :raises MalformedCaptionError: if a value is not valid
        """
        super().__init__()
        self._set_given(vertical=vertical,
                        line=line,
                        position=position,
                        size=size,
                        align=align,
                        region=region
                        )

    @classmethod
    def from_string(cls, value: str, strict: bool = False) -> 'CueSettings':
        """
        Return a `CueSettings` instance from the settings of a cue.

        :param value: the cue settings, e.g. ``line:0 align:start``
        :param strict: raise an error on settings that are not known, not
            valid or repeated instead of keeping them
        :returns: a `CueSettings` instance

        :raises MalformedCaptionError: if the value is not a string, or if
            strict and a setting is not known, not valid or repeated
        """
        return super().from_string(value, strict)

    vertical = _setting_property(
        'vertical',
        'Writing direction, ``rl`` or ``lr``.'
        )
    line = _setting_property(
        'line',
        'Line position, as a line number or a percentage with an optional '
        'alignment.'
        )
    position = _setting_property(
        'position',
        'Position as a percentage with an optional alignment.'
        )
    size = _setting_property('size', 'Size as a percentage.')
    align = _setting_property(
        'align',
        'Text alignment, ``start``, ``center``, ``end``, ``left`` or '
        '``right``.'
        )
    region = _setting_property('region', 'Identifier of a region.')


def _to_cue_settings(
        value: str | CueSettings | None
        ) -> CueSettings:
    """
    Return a new `CueSettings` instance from the value.

    :param value: the cue settings as a string, a `CueSettings` instance
        that is copied, or None for no settings
    :returns: a `CueSettings` instance

    :raises MalformedCaptionError: if the value is not a string, a
        `CueSettings` instance or None
    """
    if isinstance(value, CueSettings):
        settings = CueSettings()
        settings._settings = list(value._settings)
        return settings
    if value is None:
        return CueSettings()
    return CueSettings.from_string(value)


class Caption:
    """Representation of a caption."""

    VOICE_SPAN_PATTERN = re.compile(r'<v(?:\.\w+)*\s+([^>\s][^>]*)>')

    def __init__(self,
                 start: _Position | None = None,
                 end: _Position | None = None,
                 text: str | Sequence[str] | None = None,
                 identifier: str | None = None,
                 settings: str | CueSettings | None = None
                 ):
        """
        Initialize.

        :param start: start time of the caption, a timestamp string, a
            `Timestamp`, a number of seconds or a ``timedelta``, rounded to
            the nearest millisecond. Default: ``00:00:00.000``
        :param end: end time of the caption, as the start time
        :param text: the text of the caption, or a sequence of its lines
        :param identifier: optional identifier
        :param settings: optional cue settings, as a string such as
            ``line:0 align:start`` or a `CueSettings` instance

        :raises MalformedCaptionError: if start or end are not valid, or
            settings is not a string, a `CueSettings` instance or None
        :raises TypeError: if text is not a string or a sequence of strings
        """
        self.start_time = _time_or_zero(start)
        self.end_time = _time_or_zero(end)
        self.identifier = identifier
        self._settings = _to_cue_settings(settings)
        self.lines = _caption_lines([] if text is None else text)
        self.comments: list[str] = []
        # the line where the cue starts in the file it was read from, which
        # is None for a caption that was not read from a file
        self.source_line: int | None = None

    def __repr__(self) -> str:
        """Return the string representation of the caption."""
        cleaned_text = self.text.replace('\n', '\\n')
        return (f'<{self.__class__.__name__} '
                f'start={self.start!r} '
                f'end={self.end!r} '
                f'text={cleaned_text!r} '
                f'identifier={self.identifier!r} '
                f'settings={str(self.settings)!r}>'
                )

    def __str__(self) -> str:
        """Return a readable representation of the caption."""
        cleaned_text = self.text.replace('\n', '\\n')
        return f'{self.start} {self.end} {cleaned_text}'

    def __eq__(self, other: object) -> bool:
        """Compare equality with another object."""
        if not isinstance(other, type(self)):
            return False

        return (self.start == other.start and
                self.end == other.end and
                self.raw_text == other.raw_text and
                self.identifier == other.identifier and
                self.settings == other.settings
                )

    @property
    def start(self) -> str:
        """Return the start time of the caption."""
        return str(self.start_time)

    @start.setter
    def start(self, value: _Position) -> None:
        """
        Set the start time of the caption.

        :param value: a timestamp string, a `Timestamp`, a number of seconds
            or a ``timedelta``, rounded to the nearest millisecond

        :raises MalformedCaptionError: if the value is not a valid time
        """
        self.start_time = _to_timestamp(value)

    @property
    def end(self) -> str:
        """Return the end time of the caption."""
        return str(self.end_time)

    @end.setter
    def end(self, value: _Position) -> None:
        """
        Set the end time of the caption.

        :param value: a timestamp string, a `Timestamp`, a number of seconds
            or a ``timedelta``, rounded to the nearest millisecond

        :raises MalformedCaptionError: if the value is not a valid time
        """
        self.end_time = _to_timestamp(value)

    @property
    def settings(self) -> CueSettings:
        """Return the cue settings of the caption."""
        return self._settings

    @settings.setter
    def settings(self, value: str | CueSettings | None) -> None:
        """
        Set the cue settings of the caption, or clear them when None.

        A `CueSettings` instance is copied, so it is not shared with the
        caption it may come from.

        :raises MalformedCaptionError: if the value is not a string, a
            `CueSettings` instance or None
        """
        self._settings = _to_cue_settings(value)

    @property
    def start_in_seconds(self) -> int:
        """Return the start time of the caption in seconds."""
        return self.start_time.in_seconds()

    @property
    def end_in_seconds(self) -> int:
        """Return the end time of the caption in seconds."""
        return self.end_time.in_seconds()

    @property
    def start_in_milliseconds(self) -> int:
        """Return the start time of the caption in milliseconds."""
        return self.start_time.in_milliseconds()

    @property
    def end_in_milliseconds(self) -> int:
        """Return the end time of the caption in milliseconds."""
        return self.end_time.in_milliseconds()

    @property
    def duration(self) -> float:
        """
        Return the duration of the caption in seconds.

        It is 0 for a caption that ends before it starts.
        """
        return max(
            self.end_in_milliseconds - self.start_in_milliseconds, 0
            ) / 1000

    def copy(self) -> 'Caption':
        """
        Return a copy of the caption that shares nothing with it.

        Changing the times, lines, settings or comments of the copy leaves
        the caption unchanged, unlike ``copy.copy``, which shares them.
        """
        return copy.deepcopy(self)

    def shift(self, offset: float | datetime.timedelta) -> None:
        """
        Shift the start and end time of the caption.

        The offset is rounded to the nearest millisecond, halves away from
        zero, and a timestamp that would fall before zero is set to
        ``00:00:00.000``.

        :param offset: a number of seconds or a ``timedelta``, negative
            values move the caption earlier

        :raises TypeError: if the offset is not a number or a ``timedelta``
        :raises ValueError: if the offset is not finite, or would move a
            time after the largest timestamp, leaving the caption unchanged
        """
        offset_ms = _offset_in_milliseconds(offset)
        _retime_captions([self], lambda milliseconds: milliseconds + offset_ms)

    def _retimed(
            self,
            mapping: Callable[[int], int]
            ) -> tuple[Timestamp, Timestamp]:
        """
        Return the start and end time of the caption mapped to new times.

        A time mapped before zero is set to ``00:00:00.000``.

        :param mapping: the function mapping a time in milliseconds to the
            new time in milliseconds
        :returns: the new start and end time

        :raises ValueError: if a time is mapped after the largest timestamp
        """
        return (
            Timestamp._from_milliseconds(
                max(mapping(self.start_time.in_milliseconds()), 0)
                ),
            Timestamp._from_milliseconds(
                max(mapping(self.end_time.in_milliseconds()), 0)
                )
            )

    @property
    def raw_text(self) -> str:
        """Return the text of the caption (including cue tags)."""
        return '\n'.join(self.lines)

    @property
    def text(self) -> str:
        """Return the text of the caption (without cue tags)."""
        return '\n'.join(
            _strip_cue_text_tags(line)
            for line in self.raw_text.split('\n')
        )

    @text.setter
    def text(self, value: str | Sequence[str]) -> None:
        """
        Set the text of the caption.

        :param value: the text, or a sequence of its lines

        :raises TypeError: if the value is not a string or a sequence of
            strings
        """
        self.lines = _caption_lines(value)

    @property
    def voice(self) -> str | None:
        """Return the voice span if present."""
        if self.lines and self.lines[0].startswith('<v'):
            match = self.VOICE_SPAN_PATTERN.match(self.lines[0])
            if match:
                return match.group(1)

        return None

    @property
    def voices(self) -> list[str]:
        """
        Return the voices of every voice span in the text, in order.

        Unlike `voice`, which only reads a voice span at the start of the
        first line, the spans can be anywhere in the text, and a voice is
        returned once for each of its spans. The names are read as for
        `voice`, so a caption that starts with a voice span has it first.
        """
        voices = []
        for line in self.lines:
            for token, is_tag in _iter_cue_text_tokens(line):
                if is_tag:
                    match = self.VOICE_SPAN_PATTERN.fullmatch(f'<{token}>')
                    if match:
                        voices.append(match.group(1))
        return voices

    @property
    def word_timeline(self) -> WordTimeline:
        """
        Return the words of the caption with their times.

        Text before the first timestamp tag is visible from the start of
        the caption, and a timestamp tag gives the time the text after it
        becomes visible. Character references count as one character, as
        in ``&lt;`` for one ``<``, and cue tags take no room.

        A timestamp tag before the previous one, or outside the caption,
        makes the text after it uncertain, which is returned in the
        ``unknown`` parts of the timeline, with the offsets in the raw text
        of the caption.
        """
        timeline = _timed_word_timeline(
            self.lines, self.start_time, self.end_time
            )
        return replace(
            timeline,
            words=tuple(
                replace(
                    word,
                    cue_identifier=self.identifier,
                    cue_line=self.source_line
                    )
                for word in timeline.words
                ),
            unknown=tuple(
                replace(
                    part,
                    cue_identifier=self.identifier,
                    cue_line=self.source_line
                    )
                for part in timeline.unknown
                )
            )

    def words_at(self, at: _Position) -> WordQuery:
        """
        Return the words of the caption visible at a time.

        The words are returned with their offsets in the raw text of the
        caption, so they can be highlighted where they are written. A word
        whose time is uncertain at the time, which is in an
        `UnknownBoundary` of the caption, is left out, and the uncertain
        parts are returned in ``unknown``.

        :param at: the time, a timestamp string, a `Timestamp`, a number of
            seconds or a ``timedelta``, rounded to the nearest millisecond
        :returns: a `WordQuery` with the visible words
        """
        return self.word_timeline.at(at)


def _caption_lines(
        text: str | Iterable[str]
        ) -> list[str]:
    """
    Return the lines of the text of a caption.

    :param text: the text, split on its line breaks, or its lines
    :returns: the lines

    :raises TypeError: if the text is not a string or an iterable of strings
    """
    if isinstance(text, str):
        return _split_lines(text)

    # bytes are iterable, as integers
    lines = (list(text)
             if isinstance(text, Iterable) and
             not isinstance(text, (bytes, bytearray))
             else None
             )
    if lines is None or not all(isinstance(line, str) for line in lines):
        raise TypeError(
            f'Expected the text as a string or a sequence of lines, '
            f'got {text!r}'
            )
    return lines


def _retime_captions(
        captions: Iterable[Caption],
        mapping: Callable[[int], int]
        ) -> None:
    """
    Change the start and end time of the captions with a mapping.

    The new times of every caption are computed before any caption is
    changed, so the captions are left unchanged when one cannot be.

    :param captions: the captions to change
    :param mapping: the function mapping a time in milliseconds to the new
        time in milliseconds

    :raises ValueError: if a time is mapped after the largest timestamp
    """
    captions = list(captions)
    times = [caption._retimed(mapping) for caption in captions]
    for caption, (start_time, end_time) in zip(captions, times):
        caption.start_time, caption.end_time = start_time, end_time


# the separators of the fields of an SRT or SBV timing
_TIMING_FIELDS = re.compile('[:,.]')


def _timing_in_milliseconds(value: str) -> int:
    """
    Return the milliseconds of a SRT or SBV timestamp.

    As in most SRT readers, the separator before the milliseconds can be a
    comma or a dot, the milliseconds are read as a number, so ``07,5`` is 5
    milliseconds, and fields over their range carry over, so ``07,1500``
    is 8.5 seconds.

    :param value: the timestamp in ``hh:mm:ss,mmm`` format
    :returns: the timestamp in milliseconds

    :raises ValueError: if a field is past the range of a timestamp
    """
    fields = _TIMING_FIELDS.split(value)
    # a field with more than 17 significant digits is past the largest
    # timestamp whatever its unit, so it is rejected without converting it
    if (max(map(len, fields)) > 17 and
            any(len(field.lstrip('0')) > 17 for field in fields)):
        raise ValueError(_OUT_OF_RANGE)

    hours, minutes, seconds, milliseconds = map(int, fields)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _caption_from_timings(
        start: str,
        end: str,
        lines: Sequence[str]
        ) -> Caption | None:
    """
    Return a caption from the cue timings of SRT or SBV.

    :param start: the start time in ``hh:mm:ss,mmm`` format
    :param end: the end time in ``hh:mm:ss,mmm`` format
    :param lines: the lines of the caption text
    :returns: a `Caption` instance, or None if a time cannot be read or
        is after the largest timestamp
    """
    try:
        start_time = Timestamp._from_milliseconds(
            _timing_in_milliseconds(start)
            )
        end_time = Timestamp._from_milliseconds(_timing_in_milliseconds(end))
    except ValueError:
        return None

    caption = Caption(text=lines)
    # the timestamps are new, so the caption keeps them instead of copies
    caption.start_time = start_time
    caption.end_time = end_time
    return caption


@dataclass
class ParseIssue:
    """
    Record of a block that was skipped when reading captions.

    :param line: the line number where the block starts, from 1
    :param message: why the block was skipped
    :param lines: the lines of the block
    """

    line: int
    message: str
    lines: list[str]

    def __str__(self) -> str:
        """Return the line number and the message."""
        return f'line {self.line}: {self.message}'


_ValidationCode = typing.Literal[
    'timing', 'order', 'region', 'overlap', 'empty'
    ]
_Severity = typing.Literal['error', 'warning']


@dataclass
class ValidationIssue:
    """
    Record of a problem found in the captions by ``WebVTT.validate``.

    :param index: the index of the caption in ``captions``, from 0
    :param code: the kind of problem: ``timing`` for an end that is not
        after the start, ``order`` for a caption that starts before the
        previous one, ``region`` for a region that is not defined,
        ``overlap`` for a caption that overlaps an earlier one, or
        ``empty`` for a caption without text
    :param severity: ``error`` when the captions are not valid WebVTT, or
        ``warning`` when they are valid but likely have a mistake
    :param message: a description of the problem, where captions are
        numbered from 1 as in a file
    """

    index: int
    code: _ValidationCode
    severity: _Severity
    message: str

    def __str__(self) -> str:
        """Return the caption number, from 1, the severity and the message."""
        return f'caption {self.index + 1}: {self.severity}: {self.message}'


def _skipped_block(
        number: int,
        kind: str,
        lines: list[str]
        ) -> ParseIssue:
    """
    Return the record of a block of SRT or SBV that is skipped.

    :param number: the line number where the block starts
    :param kind: the description of what is wrong with the block
    :param lines: the lines of the block
    :returns: a `ParseIssue` instance
    """
    return ParseIssue(number, f'{kind} {lines!r}', lines)


class Style:
    """Representation of a style."""

    def __init__(self, text: str | list[str]):
        """Initialize.

        :param text: the style text, or a list of its lines, which is
            copied, as for a caption
        """
        self.lines = (_split_lines(text)
                      if isinstance(text, str)
                      else list(text)
                      )
        self.comments: list[str] = []

    def __repr__(self) -> str:
        """Return the string representation of the style."""
        return f'<{self.__class__.__name__} text={self.text!r}>'

    def __eq__(self, other: object) -> bool:
        """
        Compare equality with another object.

        Styles are equal when they have the same lines. The comments are not
        compared, as for captions and regions.
        """
        if not isinstance(other, Style):
            return NotImplemented

        return self.lines == other.lines

    @property
    def text(self) -> str:
        """Return the text of the style."""
        return '\n'.join(self.lines)


class Region(_Settings):
    """
    Representation of a region.

    A region is an area of the video where captions are rendered, defined
    in a REGION block of the header and referred to by its identifier in
    the ``region`` cue setting of the captions.

    The settings are kept as written and in their original order, so they
    are written back the same, including those that are not valid or not
    known. A property returns the value of its setting when it is valid,
    the last valid one when it is repeated, and validates a new value.
    """

    VALIDATORS = {
        'id': _is_valid_identifier,
        'width': _is_percentage,
        'lines': lambda value: bool(_NUMBER_OF_LINES.fullmatch(value)),
        'regionanchor': _is_valid_anchor,
        'viewportanchor': _is_valid_anchor,
        'scroll': lambda value: value == 'up',
        }

    _KIND = 'region setting'
    _ERROR = MalformedRegionError

    def __init__(self,
                 *,
                 id: str | None = None,
                 width: str | None = None,
                 lines: str | None = None,
                 regionanchor: str | None = None,
                 viewportanchor: str | None = None,
                 scroll: str | None = None
                 ):
        """
        Initialize.

        :param id: identifier of the region
        :param width: width as a percentage of the video, e.g. ``40%``
        :param lines: number of lines of text, e.g. ``3``
        :param regionanchor: point of the region anchored to the video, as
            two percentages of the region, e.g. ``0%,100%``
        :param viewportanchor: point of the video the region is anchored
            to, as two percentages of the video, e.g. ``10%,90%``
        :param scroll: ``up`` for the lines to scroll up

        :raises MalformedRegionError: if a value is not valid
        """
        super().__init__()
        self._set_given(id=id,
                        width=width,
                        lines=lines,
                        regionanchor=regionanchor,
                        viewportanchor=viewportanchor,
                        scroll=scroll
                        )
        self.comments: list[str] = []

    @classmethod
    def from_string(cls, value: str, strict: bool = False) -> 'Region':
        """
        Return a `Region` instance from the settings of a region block.

        :param value: the region settings separated by spaces or line
            breaks, e.g. ``id:fred width:40%``
        :param strict: raise an error on settings that are not known, not
            valid or repeated instead of keeping them
        :returns: a `Region` instance

        :raises MalformedRegionError: if the value is not a string or
            contains ``-->``, which cannot be in a region block, or if strict
            and a setting is not known, not valid or repeated
        """
        if type(value) is str and '-->' in value:
            raise MalformedRegionError(f'Invalid region settings {value!r}')
        return super().from_string(value, strict)

    id = _setting_property('id', 'Identifier of the region.')
    width = _setting_property(
        'width',
        'Width as a percentage of the video.'
        )
    lines = _setting_property('lines', 'Number of lines of text.')
    regionanchor = _setting_property(
        'regionanchor',
        'Point of the region anchored to the video, as two percentages.'
        )
    viewportanchor = _setting_property(
        'viewportanchor',
        'Point of the video the region is anchored to, as two percentages.'
        )
    scroll = _setting_property('scroll', '``up`` if the lines scroll up.')
