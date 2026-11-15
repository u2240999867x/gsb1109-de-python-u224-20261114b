"""Alignment of a word-timed transcript with millisecond-timed captions.

The transcript gives each word a coarse time and the captions give each cue
exact start and end times. The aligner keeps the order of both records: a
transcript word is associated with at most one caption and the associations
never cross, while one caption can keep the contribution of several
transcript words, as when a human subtitle joins two replies in a single cue.

Confirmed anchors pin a transcript word to a caption. They split the records
into independent ranges, and inside a range an association is made
automatically only when it is part of every best alignment of the range, so
repeated replies that the anchors do not distinguish are left unlinked
instead of being guessed.
"""

import dataclasses
import datetime
import re
from collections.abc import Mapping, Sequence

from .errors import MalformedCaptionError
from .models import (
    Caption,
    _Position,
    _offset_in_milliseconds,
    _position_in_milliseconds,
    )

# runs of letters, without digits or underscores, so CJK characters are kept
_WORD_RE = re.compile(r'[^\W\d_]+', re.UNICODE)


def _tokens(text: str) -> tuple[str, ...]:
    """
    Return the normalized words of a text.

    Cue tags are stripped before calling this, punctuation is removed and
    the letters are case-folded, so comparisons do not depend on case or
    punctuation.

    :param text: a line of transcript or caption text
    :returns: the normalized words in their original order
    """
    return tuple(part.casefold() for part in _WORD_RE.findall(text))


def caption_tokens(caption: Caption) -> tuple[str, ...]:
    """
    Return the normalized words of the text of a caption.

    Cue tags, voice spans, punctuation and case are ignored, as they are for
    matching a transcript word against a caption.

    :param caption: the caption
    :returns: the normalized words of the caption text in order
    """
    return _tokens(caption.text)


@dataclasses.dataclass(frozen=True)
class TranscriptWord:
    """
    A word of a transcript with its coarse timing.

    The times are kept in milliseconds. Use :meth:`from_times` to build one
    from a timestamp string, a :class:`~webvtt.models.Timestamp`, a number
    of seconds or a :class:`datetime.timedelta`.

    :param text: the word as transcribed
    :param start: start time in milliseconds
    :param end: end time in milliseconds, at or after the start
    """

    text: str
    start: int
    end: int

    def __post_init__(self) -> None:
        """Check the times."""
        if type(self.start) is not int or type(self.end) is not int:
            raise TypeError(
                f'Expected millisecond integers, got {self.start!r}, '
                f'{self.end!r}'
                )
        if self.start < 0 or self.end < self.start:
            raise ValueError(
                f'Invalid transcript word times {self.start!r}, {self.end!r}'
                )

    @classmethod
    def from_times(
            cls,
            text: str,
            start: _Position,
            end: _Position
            ) -> 'TranscriptWord':
        """
        Return a transcript word from times in any supported form.

        :param text: the word as transcribed
        :param start: start time, as a timestamp string, a `Timestamp`, a
            number of seconds or a ``timedelta``
        :param end: end time, as the start time, at or after it
        :returns: a `TranscriptWord` instance

        :raises MalformedCaptionError: if a time is not valid
        """
        try:
            start_ms = _position_in_milliseconds(start)
            end_ms = _position_in_milliseconds(end)
        except (TypeError, ValueError) as error:
            raise MalformedCaptionError(str(error)) from None
        return cls(text, start_ms, end_ms)


@dataclasses.dataclass(frozen=True)
class Anchor:
    """
    A confirmed association between a transcript word and a caption.

    Indexes are positions in the word sequence and the caption sequence.
    The line numbers or identifiers of the files are deliberately not used,
    as they do not determine the order of the records.

    :param word_index: index of the transcript word, from 0
    :param caption_index: index of the caption, from 0
    """

    word_index: int
    caption_index: int


@dataclasses.dataclass(frozen=True)
class Alignment:
    """
    An association between a transcript word and a caption.

    Several associations can point to the same caption, which keeps the
    contribution of every transcript word joined into one human cue. A word
    appears in at most one association, and associations follow the order of
    both records.

    :param word_index: index of the transcript word, from 0
    :param caption_index: index of the caption, from 0
    :param anchor: whether the association comes from a confirmed anchor
    :param automatic: whether the aligner found the association without an
        anchor, in which case it is uniquely determined by the surrounding
        anchors
    :param time_offset: caption start minus word start, in milliseconds
    """

    word_index: int
    caption_index: int
    anchor: bool
    automatic: bool
    time_offset: int


class _UnionFind:
    """Small union-find structure for equivalence groups of words."""

    def __init__(self) -> None:
        """Initialize without elements."""
        self._parent: dict[str, str] = {}

    def add(self, element: str) -> None:
        """Add an element unless it is already there."""
        if element not in self._parent:
            self._parent[element] = element

    def find(self, element: str) -> str:
        """Return the representative of the group of an element."""
        parent = self._parent[element]
        if parent != element:
            parent = self.find(parent)
            self._parent[element] = parent
        return parent

    def union(self, first: str, second: str) -> None:
        """Merge the groups of two elements."""
        self.add(first)
        self.add(second)
        self._parent[self.find(first)] = self.find(second)

    def groups(self) -> dict[str, frozenset[str]]:
        """Return each element mapped to the frozen group it belongs to."""
        members: dict[str, set[str]] = {}
        for element in self._parent:
            members.setdefault(self.find(element), set()).add(element)
        return {
            element: frozenset(group)
            for group in members.values()
            for element in group
            }


def _members_of(
        word: str,
        equivalents_of_word: str | Sequence[str]
        ) -> tuple[str, ...]:
    """
    Return the equivalent words of an equivalence table entry.

    :param word: the key of the entry, for error messages
    :param equivalents_of_word: an equivalent word or a sequence of them
    :returns: the equivalent words as a tuple

    :raises TypeError: if the value is not text or a sequence of text
    """
    if isinstance(equivalents_of_word, str):
        return (equivalents_of_word,)
    if (isinstance(equivalents_of_word, Sequence) and
            not isinstance(equivalents_of_word, bytes) and
            all(isinstance(member, str)
                for member in equivalents_of_word)):
        return tuple(equivalents_of_word)
    raise TypeError(
        f'Invalid equivalents of {word!r}: {equivalents_of_word!r}'
        )


def _equivalence_groups(
        equivalents: Mapping[str, str | Sequence[str]] | None
        ) -> dict[str, frozenset[str]]:
    """
    Return the group of equivalent normalized words of each word.

    Equivalence is undirected, so giving ``{'OK': 'okay'}`` makes either
    word match the other, and groups connected through several entries are
    merged. Keys and values are normalized as the transcript and caption
    words, so case and punctuation do not matter.

    :param equivalents: maps a word to an equivalent word or several of them
    :returns: maps each normalized word to the group it belongs to

    :raises TypeError: if a key or a value is not text
    """
    union_find = _UnionFind()

    for word, equivalents_of_word in (equivalents or {}).items():
        if not isinstance(word, str):
            raise TypeError(f'Invalid equivalent word {word!r}')
        tokens = _tokens(word)
        if not tokens:
            continue
        first = tokens[0]
        union_find.add(first)
        for token in tokens[1:]:
            union_find.union(first, token)
        for member in _members_of(word, equivalents_of_word):
            for token in _tokens(member):
                union_find.union(first, token)

    return union_find.groups()


def _lexical_match(
        word_tokens: Sequence[str],
        caption_token_set: set[str],
        groups: Mapping[str, frozenset[str]]
        ) -> bool:
    """
    Return whether a word can match a caption on their text.

    A word matches when one of its normalized words appears in the caption,
    possibly through the equivalence table.

    :param word_tokens: normalized words of the transcript word
    :param caption_token_set: normalized words of the caption as a set
    :param groups: equivalence groups of normalized words
    :returns: whether the texts are compatible
    """
    for token in word_tokens:
        if token in caption_token_set:
            return True
        group = groups.get(token)
        if group is not None and any(
                caption_token in group for caption_token in caption_token_set
                ):
            return True
    return False


def _temporal_match(
        word: TranscriptWord,
        caption_start: int,
        caption_end: int,
        tolerance: int
        ) -> bool:
    """
    Return whether the times of a word and a caption are close enough.

    The intervals overlap or the gap between them is within the tolerance.

    :param word: the transcript word
    :param caption_start: caption start in milliseconds
    :param caption_end: caption end in milliseconds
    :param tolerance: allowed gap in milliseconds
    :returns: whether the times are compatible
    """
    return (word.start <= caption_end + tolerance and
            word.end >= caption_start - tolerance
            )


def _anchor_pair(anchor: Anchor | tuple[int, int]) -> Anchor:
    """
    Return an anchor from an `Anchor` or an index pair.

    :param anchor: an `Anchor` instance or a ``(word, caption)`` pair
    :returns: the anchor

    :raises TypeError: if the anchor is not a pair of integers
    """
    if isinstance(anchor, Anchor):
        return anchor
    if (isinstance(anchor, tuple) and len(anchor) == 2 and
            all(type(value) is int for value in anchor)):
        return Anchor(anchor[0], anchor[1])
    raise TypeError(f'Invalid anchor {anchor!r}')


def _check_anchors(
        anchors: Sequence[Anchor | tuple[int, int]],
        number_of_words: int,
        number_of_captions: int
        ) -> list[Anchor]:
    """
    Validate the anchors and return them sorted on the word index.

    Two anchors may point to the same caption, as a cue can join several
    transcript words, but their word indexes must be distinct and their
    caption indexes cannot go backwards.

    :param anchors: anchors as `Anchor` instances or index pairs
    :param number_of_words: number of transcript words
    :param number_of_captions: number of captions
    :returns: the anchors sorted by word index

    :raises TypeError: if an anchor is not a pair of integers
    :raises ValueError: if an index is out of range, repeated or out of order
    """
    checked: list[Anchor] = []
    for raw_anchor in anchors:
        pair = _anchor_pair(raw_anchor)
        if not 0 <= pair.word_index < number_of_words:
            raise ValueError(
                f'Anchor word index {pair.word_index} is out of range'
                )
        if not 0 <= pair.caption_index < number_of_captions:
            raise ValueError(
                f'Anchor caption index {pair.caption_index} is out of range'
                )
        checked.append(pair)

    checked.sort(key=lambda anchor: anchor.word_index)
    for first, second in zip(checked, checked[1:]):
        if first.word_index == second.word_index:
            raise ValueError(
                f'Word {first.word_index} is anchored more than once'
                )
        if first.caption_index > second.caption_index:
            raise ValueError(
                'Anchors must keep the order of the captions: word '
                f'{first.word_index} points to caption '
                f'{first.caption_index} but word {second.word_index} to '
                f'caption {second.caption_index}'
                )
    return checked


def _compatibility_matrix(
        words: Sequence[TranscriptWord],
        captions: Sequence[Caption],
        caption_token_sets: Sequence[set[str]],
        groups: Mapping[str, frozenset[str]],
        tolerance: int,
        word_limits: tuple[int, int],
        caption_limits: tuple[int, int]
        ) -> list[list[bool]]:
    """
    Return whether each word of a range can match each caption of the range.

    :param words: all the transcript words
    :param captions: all the captions
    :param caption_token_sets: normalized words of each caption as a set
    :param groups: equivalence groups of normalized words
    :param tolerance: allowed gap in milliseconds
    :param word_limits: first word index and one past the last word index
    :param caption_limits: first caption index and one past the last
    :returns: a matrix indexed relative to the start of the range
    """
    word_lo, word_hi = word_limits
    caption_lo, caption_hi = caption_limits
    matrix: list[list[bool]] = []
    for word_index in range(word_lo, word_hi):
        word = words[word_index]
        word_tokens = _tokens(word.text)
        row: list[bool] = []
        for caption_index in range(caption_lo, caption_hi):
            caption = captions[caption_index]
            match = _lexical_match(
                word_tokens,
                caption_token_sets[caption_index],
                groups
                ) and _temporal_match(
                    word,
                    caption.start_in_milliseconds,
                    caption.end_in_milliseconds,
                    tolerance
                    )
            row.append(match)
        matrix.append(row)
    return matrix


def _forward_scores(
        compatibility: Sequence[Sequence[bool]]
        ) -> list[list[int]]:
    """
    Return the best scores of every prefix of a compatibility matrix.

    Caption indexes are non-decreasing in a match, so several words can join
    the same caption.

    :param compatibility: whether each word can match each caption
    :returns: the scores on a grid one row and one column larger
    """
    number_of_words = len(compatibility)
    number_of_captions = len(compatibility[0])
    forward = [
        [0] * (number_of_captions + 1)
        for _ in range(number_of_words + 1)
        ]
    for i in range(1, number_of_words + 1):
        for j in range(1, number_of_captions + 1):
            best = max(forward[i - 1][j], forward[i][j - 1])
            if compatibility[i - 1][j - 1]:
                best = max(best, forward[i - 1][j] + 1)
            forward[i][j] = best
    return forward


def _backward_scores(
        compatibility: Sequence[Sequence[bool]]
        ) -> list[list[int]]:
    """
    Return the best scores of every suffix of a compatibility matrix.

    :param compatibility: whether each word can match each caption
    :returns: the scores on a grid one row and one column larger
    """
    number_of_words = len(compatibility)
    number_of_captions = len(compatibility[0])
    backward = [
        [0] * (number_of_captions + 1)
        for _ in range(number_of_words + 1)
        ]
    for i in range(number_of_words - 1, -1, -1):
        for j in range(number_of_captions - 1, -1, -1):
            best = max(backward[i + 1][j], backward[i][j + 1])
            if compatibility[i][j]:
                best = max(best, backward[i + 1][j] + 1)
            backward[i][j] = best
    return backward


def _optimal_word_edges(
        compatibility: Sequence[Sequence[bool]],
        forward: Sequence[Sequence[int]],
        backward: Sequence[Sequence[int]],
        best_score: int
        ) -> list[set[int]]:
    """
    Return the captions each word can take in a best alignment.

    :param compatibility: whether each word can match each caption
    :param forward: the best scores of every prefix
    :param backward: the best scores of every suffix
    :param best_score: the score of a best alignment
    :returns: the optimal caption indexes of each word
    """
    number_of_words = len(compatibility)
    number_of_captions = len(compatibility[0])
    word_edges: list[set[int]] = [
        set() for _ in range(number_of_words)
        ]
    for i in range(number_of_words):
        for j in range(number_of_captions):
            if (compatibility[i][j] and
                    forward[i][j + 1] + 1 + backward[i + 1][j]
                    == best_score):
                word_edges[i].add(j)
    return word_edges


def _forced_pairs(
        compatibility: Sequence[Sequence[bool]]
        ) -> list[tuple[int, int]]:
    """
    Return the associations uniquely determined in a range.

    The alignment with the most associations is found with dynamic
    programming, where caption indexes are non-decreasing so several words
    can join the same caption. An association is kept only when the word is
    part of every best alignment and has a single caption there, as a word
    is paired at most once while a caption can be shared by several words.

    :param compatibility: whether each word can match each caption, indexed
        relative to the range
    :returns: the unique ``(word, caption)`` pairs in range-local indexes
    """
    if not compatibility or not compatibility[0]:
        return []

    forward = _forward_scores(compatibility)
    number_of_words = len(compatibility)
    number_of_captions = len(compatibility[0])
    best_score = forward[number_of_words][number_of_captions]
    if best_score == 0:
        return []

    backward = _backward_scores(compatibility)
    word_edges = _optimal_word_edges(
        compatibility, forward, backward, best_score
        )

    # a word identifies its association, as it is paired at most once, while
    # a caption can be shared by several words, so the pair is forced when
    # the word is in every best alignment and has a single edge to take
    word_skippable = [
        any(forward[i][j] + backward[i + 1][j] == best_score
            for j in range(number_of_captions + 1))
        for i in range(number_of_words)
        ]

    return [
        (i, next(iter(word_edges[i])))
        for i in range(number_of_words)
        if not word_skippable[i] and len(word_edges[i]) == 1
        ]


def _checked_words(
        transcript_words: Sequence[TranscriptWord]
        ) -> list[TranscriptWord]:
    """
    Return the transcript words, checking their type.

    :param transcript_words: the transcript words
    :returns: the words as a list

    :raises TypeError: if an item is not a `TranscriptWord`
    """
    words = list(transcript_words)
    if not all(isinstance(item, TranscriptWord) for item in words):
        raise TypeError(
            'Expected the transcript as a sequence of TranscriptWord'
            )
    return words


def _checked_captions(captions: Sequence[Caption]) -> list[Caption]:
    """
    Return the captions, checking their type.

    :param captions: the captions
    :returns: the captions as a list

    :raises TypeError: if an item is not a `Caption`
    """
    caption_list = list(captions)
    if not all(isinstance(item, Caption) for item in caption_list):
        raise TypeError('Expected the captions as a sequence of Caption')
    return caption_list


def _build_alignment(
        words: Sequence[TranscriptWord],
        captions: Sequence[Caption],
        word_index: int,
        caption_index: int,
        anchor: bool
        ) -> Alignment:
    """
    Build an association between a word and a caption.

    :param words: all the transcript words
    :param captions: all the captions
    :param word_index: index of the transcript word
    :param caption_index: index of the caption
    :param anchor: whether the association is a confirmed anchor
    :returns: the `Alignment`
    """
    return Alignment(
        word_index=word_index,
        caption_index=caption_index,
        anchor=anchor,
        automatic=not anchor,
        time_offset=(
            captions[caption_index].start_in_milliseconds
            - words[word_index].start
            )
        )


def _build_alignments(
        words: Sequence[TranscriptWord],
        captions: Sequence[Caption],
        caption_token_sets: Sequence[set[str]],
        groups: Mapping[str, frozenset[str]],
        tolerance: int,
        anchors: Sequence[Anchor]
        ) -> list[Alignment]:
    """
    Build the associations range by range between the anchors.

    :param words: the transcript words
    :param captions: the captions
    :param caption_token_sets: normalized words of each caption as a set
    :param groups: equivalence groups of normalized words
    :param tolerance: allowed gap in milliseconds
    :param anchors: the validated anchors, sorted by word index
    :returns: the associations, ordered by transcript word index
    """
    alignments: list[Alignment] = []
    previous_word = -1
    previous_caption = -1
    boundaries = list(anchors) + [Anchor(len(words), len(captions))]
    for anchor in boundaries:
        # both boundary captions belong to the range, so a free word joined
        # into the cue of a neighbouring anchor is kept instead of excluded
        caption_lo = max(previous_caption, 0)
        caption_hi = (anchor.caption_index + 1
                      if anchor.word_index < len(words)
                      else anchor.caption_index)
        compatibility = _compatibility_matrix(
            words,
            captions,
            caption_token_sets,
            groups,
            tolerance,
            (previous_word + 1, anchor.word_index),
            (caption_lo, caption_hi)
            )
        for local_word, local_caption in _forced_pairs(compatibility):
            alignments.append(
                _build_alignment(
                    words,
                    captions,
                    previous_word + 1 + local_word,
                    caption_lo + local_caption,
                    anchor=False
                    )
                )

        if anchor.word_index < len(words):
            alignments.append(
                _build_alignment(
                    words,
                    captions,
                    anchor.word_index,
                    anchor.caption_index,
                    anchor=True
                    )
                )

        previous_word = anchor.word_index
        previous_caption = anchor.caption_index

    alignments.sort(key=lambda alignment: alignment.word_index)
    return alignments


def align_transcript(
        transcript_words: Sequence[TranscriptWord],
        captions: Sequence[Caption],
        *,
        tolerance: float | datetime.timedelta = 1.0,
        equivalents: Mapping[str, str | Sequence[str]] | None = None,
        anchors: Sequence[Anchor | tuple[int, int]] = ()
        ) -> list[Alignment]:
    """
    Align a word-timed transcript with captions while keeping their order.

    A transcript word is associated with a caption when their texts match
    and their times overlap or are within the tolerance of each other.
    Matching ignores case, punctuation and cue tags, and uses the
    equivalence table for different wordings. The associations never cross,
    a word is associated at most once and a caption may receive several
    words.

    The anchors are confirmed associations. They split the records into
    ranges, and an association inside a range is returned as automatic only
    when the surrounding anchors uniquely determine it, so repeated replies
    that could belong to more than one cue are left unlinked. Anchors are
    always returned. Indexes are positions in the sequences, never the line
    numbers or identifiers of the files.

    :param transcript_words: the transcript words in order, with coarse times
    :param captions: the captions in order, with exact times
    :param tolerance: maximum gap between a word and a caption, as a number
        of seconds or a ``timedelta``. Default: one second
    :param equivalents: maps a word to another word or several words that
        count as the same text, e.g. ``{'OK': ['okay']}``
    :param anchors: confirmed `Anchor` instances or ``(word, caption)``
        index pairs
    :returns: the associations, ordered by transcript word index

    :raises MalformedCaptionError: if a time is not valid
    :raises TypeError: if an argument has the wrong type
    :raises ValueError: if the tolerance is negative, an anchor is invalid or
        two anchors do not keep the order of the records
    """
    words = _checked_words(transcript_words)
    caption_list = _checked_captions(captions)

    tolerance_ms = _offset_in_milliseconds(tolerance, name='tolerance')
    if tolerance_ms < 0:
        raise ValueError(f'Invalid tolerance {tolerance!r}')
    groups = _equivalence_groups(equivalents)
    checked_anchors = _check_anchors(
        anchors, len(words), len(caption_list)
        )

    caption_token_sets = [
        set(caption_tokens(caption)) for caption in caption_list
        ]

    return _build_alignments(
        words,
        caption_list,
        caption_token_sets,
        groups,
        tolerance_ms,
        checked_anchors
        )
