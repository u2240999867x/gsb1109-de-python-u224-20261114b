"""Derived data built from WebVTT captions and updated incrementally.

The captions are the source of truth and the full-text search index, the
word-level timings and the chapter summaries are derived from them. When the
captions are edited, the derived data is updated from the changes instead of
being generated again as a whole: only the captions that changed, and the
data that depends on the changed parts, are rebuilt.

Four kinds of cue edits are told apart:

* the body text, without any cue tag,
* the inline timestamp tags such as ``<00:17.500>``,
* the speakers of the voice spans such as ``<v Jane>``,
* the outer start and end times of the cue.

A change of the comment blocks attached to a cue is reported separately,
as it only updates the chapter summaries.

Cues without an identifier and with repeated text are followed with their
neighbours and the cues already matched by their identifier. A cue that
cannot be followed uniquely is reported, with the range that has to be
rebuilt, instead of being matched at random.
"""

import bisect
import dataclasses
import difflib
import re
import typing
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .errors import MalformedCaptionError
from .models import Caption, Timestamp, _iter_cue_text_tokens

if typing.TYPE_CHECKING:
    from .webvtt import WebVTT

# the letters of a word, without digits or the underscore, as a Unicode
# regular expression so words with accented letters are kept whole
_WORD_PATTERN = re.compile(r'[^\W\d_]+', re.UNICODE)


@dataclass(frozen=True)
class SearchHit:
    """
    A word found in the text of a caption by the search index.

    :param caption_index: the index of the caption, from 0
    :param start: the position of the first letter of the word in the text
        of the caption, without cue tags and with its line breaks
    :param end: the position after the last letter of the word
    """

    caption_index: int
    start: int
    end: int


@dataclass(frozen=True)
class WordTiming:
    """
    A word and the time at which it is spoken.

    The times are in milliseconds. A word is timed along the inline
    timestamp tags of its caption, interpolated between the two tags around
    it, or between the outer times of the caption when it has no tag around
    it.

    :param word: the word, as found in the text without cue tags
    :param start: the time at which the word starts, in milliseconds
    :param end: the time at which the word ends, in milliseconds
    :param caption_index: the index of the caption, from 0
    """

    word: str
    start: int
    end: int
    caption_index: int


@dataclass(frozen=True)
class Chapter:
    """
    A chapter summary, from a comment block attached to a caption.

    A ``NOTE`` block written before a cue is attached to its caption, and
    each comment is a chapter starting at the start time of its caption.

    :param title: the text of the comment
    :param caption_index: the index of the caption, from 0
    :param start: the start time of the caption, in milliseconds
    """

    title: str
    caption_index: int
    start: int


@dataclass(frozen=True)
class CueChange:
    """
    How a cue changed between two versions of the captions.

    A cue present only in the new captions is inserted, one present only in
    the old ones is deleted, and a paired one has the flags of the parts
    that changed.

    :param old_index: the index of the cue in the old captions, or None
    :param new_index: the index of the cue in the new captions, or None
    :param timing: true if the outer start or end time changed
    :param body: true if the text without cue tags changed
    :param inline: true if an inline timestamp tag changed
    :param speaker: true if a speaker of a voice span changed
    :param comment: true if a comment block attached to the cue changed,
        which updates its chapter summaries
    :param ambiguous: true if the cue could not be followed uniquely, so
        its range has to be rebuilt
    """

    old_index: int | None
    new_index: int | None
    timing: bool = False
    body: bool = False
    inline: bool = False
    speaker: bool = False
    comment: bool = False
    ambiguous: bool = False

    @property
    def inserted(self) -> bool:
        """Return whether the cue is only in the new captions."""
        return self.old_index is None and self.new_index is not None

    @property
    def deleted(self) -> bool:
        """Return whether the cue is only in the old captions."""
        return self.new_index is None and self.old_index is not None


@dataclass
class UpdateReport:
    """
    The result of updating the derived data after an edit.

    :param changes: the changes of every cue that is not unchanged, in the
        order of the diff between the old and the new captions
    :param rebuild_ranges: the ranges of new caption indices, as
        ``(start, end_exclusive)`` pairs, that were rebuilt because their
        text changed, they were inserted or their cue is ambiguous
    :param ambiguous: true if at least one cue could not be followed
        uniquely
    """

    changes: list[CueChange] = field(default_factory=list)
    rebuild_ranges: list[tuple[int, int]] = field(default_factory=list)
    ambiguous: bool = False


@dataclass(frozen=True)
class _Snapshot:
    """The parts of a caption used to tell the edits apart."""

    identifier: str | None
    start: int
    end: int
    raw_text: str
    text: str
    inline: tuple[str, ...]
    voices: tuple[str, ...]
    comments: tuple[str, ...]


@dataclass(frozen=True)
class _Pair:
    """A caption paired between the old and new captions, or only on one."""

    old_index: int | None
    new_index: int | None
    ambiguous: bool = False


def _words_of(
        text: str, case_sensitive: bool
        ) -> list[tuple[str, int, int]]:
    """
    Return the words of a text with their positions.

    :param text: a text without cue tags
    :param case_sensitive: whether the words are kept as written
    :returns: tuples of the word, its start and its end position
    """
    occurrences = []
    for match in _WORD_PATTERN.finditer(text):
        word = match.group()
        if not case_sensitive:
            word = word.casefold()
        occurrences.append((word, match.start(), match.end()))
    return occurrences


def _is_timestamp(token: str) -> bool:
    """Return whether the content of a cue tag is a timestamp."""
    if not Timestamp.PATTERN.fullmatch(token):
        return False
    try:
        Timestamp.from_string(token)
    except MalformedCaptionError:
        return False
    return True


def _inline_timestamps(raw_text: str) -> tuple[str, ...]:
    """
    Return the inline timestamp tags of a caption, in their order.

    Only the content of the tags that is a timestamp is returned, so
    ``<00:17.500>`` gives ``('00:17.500',)`` and voice and class tags are
    ignored.

    :param raw_text: the text of the caption, including cue tags
    :returns: the inline timestamps as written
    """
    return tuple(
        token
        for token, is_tag in _iter_cue_text_tokens(raw_text)
        if is_tag and _is_timestamp(token)
        )


def _snapshot(caption: Caption) -> _Snapshot:
    """
    Return a snapshot of the parts of a caption used to detect edits.

    :param caption: the caption
    :returns: a snapshot
    """
    return _Snapshot(
        identifier=caption.identifier,
        start=caption.start_in_milliseconds,
        end=caption.end_in_milliseconds,
        raw_text=caption.raw_text,
        text=caption.text,
        inline=_inline_timestamps(caption.raw_text),
        voices=tuple(caption.voices),
        comments=tuple(caption.comments)
        )


def _scan_text(
        caption: Caption
        ) -> tuple[str, list[tuple[int, int]]]:
    """
    Return the text of a caption without tags and its inline timestamps.

    The positions are those of the text without tags, including its line
    breaks, so they are the same positions as in `Caption.text`. A tag does
    not take a position, and a timestamp tag standing before the following
    text is recorded at the position of that text.

    :param caption: the caption
    :returns: the text without tags and tuples of a position and the time of
        the timestamp at it, in milliseconds
    """
    pieces: list[str] = []
    timestamps: list[tuple[int, int]] = []
    position = 0
    for token, is_tag in _iter_cue_text_tokens(caption.raw_text):
        if is_tag:
            if _is_timestamp(token):
                timestamps.append(
                    (position, Timestamp.from_string(token).in_milliseconds())
                    )
            continue
        pieces.append(token)
        position += len(token)
    return ''.join(pieces), timestamps


def _divide_rounded(numerator: int, denominator: int) -> int:
    """
    Divide two integers rounding to the nearest, halves away from zero.

    :param numerator: the dividend
    :param denominator: the divisor, greater than zero
    :returns: the rounded quotient
    """
    quotient, remainder = divmod(abs(numerator), denominator)
    quotient += 2 * remainder >= denominator
    return quotient if numerator >= 0 else -quotient


def _time_at(
        position: int,
        boundaries: Sequence[tuple[int, int]],
        end_position: int
        ) -> int:
    """
    Return the time of a position in a caption, interpolated between tags.

    The boundaries are the outer start time, the inline timestamps and the
    outer end time, ordered by their position. The time is rounded to the
    nearest millisecond, halves away from zero, as elsewhere in the package.

    :param position: the position in the text without tags
    :param boundaries: the positions and times of the boundaries
    :param end_position: the position after the last character
    :returns: the time in milliseconds
    """
    if position >= end_position:
        return boundaries[-1][1]

    positions = [boundary[0] for boundary in boundaries]
    index = max(bisect.bisect_right(positions, position) - 1, 0)
    pos_1, time_1 = boundaries[index]
    pos_2, time_2 = boundaries[min(index + 1, len(boundaries) - 1)]
    if pos_2 == pos_1:
        return time_1

    distance = position - pos_1
    return time_1 + _divide_rounded(
        distance * (time_2 - time_1), pos_2 - pos_1
        )


def _word_timings_of(caption: Caption, caption_index: int) -> list[WordTiming]:
    """
    Return the word-level timings of a caption as a full generation would.

    :param caption: the caption
    :param caption_index: the index of the caption, from 0
    :returns: the words with their start and end times
    """
    text, inline = _scan_text(caption)
    boundaries: list[tuple[int, int]] = [
        (0, caption.start_in_milliseconds),
        *sorted(inline),
        (len(text), caption.end_in_milliseconds)
        ]

    timings = []
    for word, start, end in _words_of(text, case_sensitive=True):
        timings.append(WordTiming(
            word=word,
            start=_time_at(start, boundaries, len(text)),
            end=_time_at(end, boundaries, len(text)),
            caption_index=caption_index
            ))
    return timings


def _chapters_of(caption: Caption, caption_index: int) -> list[Chapter]:
    """
    Return the chapter summaries of a caption, one per comment.

    :param caption: the caption
    :param caption_index: the index of the caption, from 0
    :returns: the chapters
    """
    return [
        Chapter(title=comment,
                caption_index=caption_index,
                start=caption.start_in_milliseconds)
        for comment in caption.comments
        ]


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """
    Merge overlapping or adjacent ranges of indices.

    :param ranges: the ranges as ``(start, end_exclusive)`` pairs
    :returns: the merged ranges, ordered
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _captions_of(
        source: 'WebVTT | Sequence[Caption]'
        ) -> list[Caption]:
    """
    Return the captions of a WebVTT instance or a sequence.

    :param source: a `WebVTT` instance or the captions
    :returns: the captions
    """
    captions = getattr(source, 'captions', None)
    if captions is not None:
        return list(captions)
    return list(typing.cast(Sequence[Caption], source))


def _match_keys(snapshots: Sequence[_Snapshot]) -> list[tuple[str, str]]:
    """
    Return the key used to pair each caption.

    A caption with an identifier that is unique is paired by it, so editing
    its text does not lose it. Every other caption is paired by its text.

    :param snapshots: the snapshots
    :returns: a ``(kind, value)`` key for each caption
    """
    counts = Counter(
        snapshot.identifier
        for snapshot in snapshots
        if snapshot.identifier is not None
        )
    return [
        ('id', snapshot.identifier)
        if snapshot.identifier is not None and
        counts[snapshot.identifier] == 1
        else ('text', snapshot.raw_text)
        for snapshot in snapshots
        ]


def _pair_captions(
        old_snapshots: Sequence[_Snapshot],
        new_snapshots: Sequence[_Snapshot]
        ) -> list[_Pair]:
    """
    Pair the captions of two versions, following repeated text by neighbour.

    Cues with a unique identifier are paired by it, the others with the
    longest matching blocks of equal text, which keeps the order and uses
    the neighbours to follow repeated text. An isolated cue whose text is
    repeated is paired but marked ambiguous, so its range is rebuilt.

    :param old_snapshots: the snapshots of the old captions
    :param new_snapshots: the snapshots of the new captions
    :returns: the pairs, in caption order
    """
    old_keys = _match_keys(old_snapshots)
    new_keys = _match_keys(new_snapshots)
    matcher = difflib.SequenceMatcher(
        a=old_keys, b=new_keys, autojunk=False
        )

    repeated_text = _repeated_text(old_snapshots, new_snapshots)
    pairs: list[_Pair] = []
    for opcode in matcher.get_opcodes():
        pairs.extend(_pairs_for_opcode(*opcode))

    candidates = [
        position
        for position, pair in enumerate(pairs)
        if pair.old_index is not None and
        pair.new_index is not None and
        old_keys[pair.old_index][0] == 'text' and
        old_snapshots[pair.old_index].raw_text in repeated_text
        ]
    return _mark_ambiguous(pairs, candidates, len(old_snapshots))


def _repeated_text(
        old_snapshots: Sequence[_Snapshot],
        new_snapshots: Sequence[_Snapshot]
        ) -> set[str]:
    """
    Return the text repeated among cues paired by their text, on either side.

    Cues with a unique identifier are paired by it and are not candidates,
    so every repeated raw text is collected here and the identifier is
    checked again when the candidates are selected. This also covers cues
    whose identifier is repeated and which therefore fall back to text.

    :param old_snapshots: the snapshots of the old captions
    :param new_snapshots: the snapshots of the new captions
    :returns: the raw text that occurs more than once
    """
    repeated_text: set[str] = set()
    for snapshots in (old_snapshots, new_snapshots):
        counts = Counter(snapshot.raw_text for snapshot in snapshots)
        repeated_text.update(
            text for text, count in counts.items() if count > 1
            )
    return repeated_text


def _pairs_for_opcode(
        tag: str,
        old_1: int,
        old_2: int,
        new_1: int,
        new_2: int
        ) -> list[_Pair]:
    """
    Return the pairs of a diff opcode.

    Cues replaced one for another are paired in order, and the extra cues
    of the longer side are reported as deleted or inserted.

    :param tag: the diff opcode, equal, replace, delete or insert
    :param old_1: the first old index of the block
    :param old_2: the index after the old block
    :param new_1: the first new index of the block
    :param new_2: the index after the new block
    :returns: the pairs of the block
    """
    if tag == 'equal':
        return [
            _Pair(old_1 + offset, new_1 + offset)
            for offset in range(old_2 - old_1)
            ]
    if tag == 'replace':
        paired = min(old_2 - old_1, new_2 - new_1)
        pairs = [
            _Pair(old_1 + offset, new_1 + offset)
            for offset in range(paired)
            ]
        pairs.extend(
            _Pair(old_index, None)
            for old_index in range(old_1 + paired, old_2)
            )
        pairs.extend(
            _Pair(None, new_index)
            for new_index in range(new_1 + paired, new_2)
            )
        return pairs
    if tag == 'delete':
        return [_Pair(old_index, None) for old_index in range(old_1, old_2)]
    return [_Pair(None, new_index) for new_index in range(new_1, new_2)]


def _mark_ambiguous(
        pairs: list[_Pair],
        candidates: Sequence[int],
        old_length: int
        ) -> list[_Pair]:
    """
    Mark the repeated cues that their neighbours do not pin as ambiguous.

    A repeated cue matched on its own is a unique continuation when a cue
    actually next to it is paired with the cue at the same relative
    position. A boundary does not pin it, as the cue could equally continue
    another copy of the same text. Otherwise it cannot be followed and is
    marked ambiguous, so its range is rebuilt.

    :param pairs: the pairs in caption order
    :param candidates: the positions in the pairs of repeated cues matched
        on their own
    :param old_length: the number of old captions
    :returns: the pairs with the ambiguous flags set
    """
    old_to_new = {
        pair.old_index: pair.new_index
        for pair in pairs
        if pair.old_index is not None and pair.new_index is not None
        }

    for position in candidates:
        pair = pairs[position]
        old_index = typing.cast(int, pair.old_index)
        new_index = typing.cast(int, pair.new_index)
        left_pinned = (
            old_index > 0 and
            old_to_new.get(old_index - 1) == new_index - 1
            )
        right_pinned = (
            old_index < old_length - 1 and
            old_to_new.get(old_index + 1) == new_index + 1
            )
        if not left_pinned and not right_pinned:
            pairs[position] = dataclasses.replace(pair, ambiguous=True)
    return pairs


class DerivedData:
    """
    The data derived from captions and kept up to date incrementally.

    Build the data once with `build`, edit the captions, and call `update`
    with the new captions. The search index, the word-level timings and the
    chapter summaries then match a generation from the new captions, but
    only the parts affected by the edit are rebuilt.

    Cues are paired by their identifier, or by their text when they have
    none, using their neighbours to follow cues with repeated text. A cue
    that cannot be followed uniquely is reported in the `UpdateReport`,
    with the range to rebuild.
    """

    def __init__(self, case_sensitive: bool = False) -> None:
        """
        Initialize empty derived data.

        Use `build` to create the data of captions instead of building the
        instance by hand.

        :param case_sensitive: whether the search index distinguishes the
            case of words
        """
        self._case_sensitive = case_sensitive
        self._snapshots: list[_Snapshot] = []
        self._occurrences: list[list[tuple[str, int, int]]] = []
        self._word_timings: list[list[WordTiming]] = []
        self._chapters: list[list[Chapter]] = []
        self._search_index: dict[str, list[SearchHit]] = {}

    @classmethod
    def build(
            cls,
            source: 'WebVTT | Sequence[Caption]',
            *,
            case_sensitive: bool = False
            ) -> 'DerivedData':
        """
        Build the derived data of captions, as a full generation.

        :param source: a `WebVTT` instance or the captions
        :param case_sensitive: whether the search index distinguishes the
            case of words
        :returns: the derived data
        """
        data = cls(case_sensitive=case_sensitive)
        captions = _captions_of(source)
        data._snapshots = [_snapshot(caption) for caption in captions]
        data._occurrences = [
            _words_of(caption.text, case_sensitive) for caption in captions
            ]
        data._word_timings = [
            _word_timings_of(caption, index)
            for index, caption in enumerate(captions)
            ]
        data._chapters = [
            _chapters_of(caption, index)
            for index, caption in enumerate(captions)
            ]
        data._search_index = data._build_postings(data._occurrences)
        return data

    @property
    def case_sensitive(self) -> bool:
        """Return whether the search distinguishes the case of words."""
        return self._case_sensitive

    @property
    def search_index(self) -> dict[str, list[SearchHit]]:
        """
        Return the search index, mapping each word to its hits.

        The hits of a word are ordered by caption and by position in the
        text. The index is a copy of the mapping, but its hits are shared
        with the derived data.
        """
        return dict(self._search_index)

    @property
    def word_timings(self) -> list[list[WordTiming]]:
        """
        Return the word-level timings, one list per caption.

        The lists are in the order of the captions and their words in the
        order of the text. They are shared with the derived data.
        """
        return self._word_timings

    @property
    def chapters(self) -> list[Chapter]:
        """
        Return the chapter summaries, ordered by caption and comment.
        """
        return [
            chapter
            for chapters in self._chapters
            for chapter in chapters
            ]

    def search(self, query: str) -> list[SearchHit]:
        """
        Search the captions for the words of a query.

        Every word of the query has to be found in a caption for it to
        match, as in an AND search. The hits of the words of the query in
        the matching captions are returned, ordered by caption and by
        position. An empty query gives no hit.

        :param query: the words to search
        :returns: the hits
        """
        terms = [
            word for word, _, _ in _words_of(query, self._case_sensitive)
            ]
        if not terms:
            return []

        postings = [self._search_index.get(term, []) for term in terms]
        if any(not posting for posting in postings):
            return []

        matching = set.intersection(*(
            {hit.caption_index for hit in posting} for posting in postings
            ))
        hits = [
            hit
            for posting in postings
            for hit in posting
            if hit.caption_index in matching
            ]
        hits.sort(key=lambda hit: (hit.caption_index, hit.start))
        return hits

    def update(
            self,
            source: 'WebVTT | Sequence[Caption]'
            ) -> UpdateReport:
        """
        Update the derived data after the captions were edited.

        The captions are paired with those given to `build` or to the
        previous update. Unchanged captions keep their derived data, a
        caption whose outer times alone changed keeps its search hits and
        only gets its timings recomputed, and a caption whose text changed
        is rebuilt locally. The result is the same as building the data
        again from the new captions.

        :param source: the new captions, as a `WebVTT` instance or a
            sequence
        :returns: a report of the changes and the ranges that were rebuilt
        """
        captions = _captions_of(source)
        new_snapshots = [_snapshot(caption) for caption in captions]
        pairs = _pair_captions(self._snapshots, new_snapshots)

        old_for_new = {
            pair.new_index: pair.old_index
            for pair in pairs
            if pair.new_index is not None
            }
        ambiguous_new = {
            pair.new_index
            for pair in pairs
            if pair.ambiguous and pair.new_index is not None
            }

        report = self._report(pairs, new_snapshots, ambiguous_new)
        old_occurrences = self._occurrences

        occurrences, word_timings, chapters = self._rebuild_caches(
            captions, new_snapshots, old_for_new, ambiguous_new
            )
        self._update_search_index(
            old_occurrences, occurrences, new_snapshots, old_for_new,
            ambiguous_new
            )

        self._occurrences = occurrences
        self._word_timings = word_timings
        self._chapters = chapters
        self._snapshots = new_snapshots
        return report

    def _build_postings(
            self,
            occurrences: Sequence[Sequence[tuple[str, int, int]]]
            ) -> dict[str, list[SearchHit]]:
        """
        Build the search index from the words of every caption.

        :param occurrences: the words with their positions, per caption
        :returns: the words mapped to their hits
        """
        postings: dict[str, list[SearchHit]] = {}
        for caption_index, caption_words in enumerate(occurrences):
            for word, start, end in caption_words:
                postings.setdefault(word, []).append(
                    SearchHit(caption_index, start, end)
                    )
        return postings

    def _report(
            self,
            pairs: Sequence[_Pair],
            new_snapshots: Sequence[_Snapshot],
            ambiguous_new: set[int]
            ) -> UpdateReport:
        """
        Build the report of the changes between the captions.

        :param pairs: the paired captions
        :param new_snapshots: the snapshots of the new captions
        :param ambiguous_new: the new indices of ambiguous captions
        :returns: the report
        """
        changes = []
        rebuild_ranges = []
        for pair in pairs:
            if pair.old_index is None:
                new_index = typing.cast(int, pair.new_index)
                changes.append(CueChange(None, new_index,
                                         ambiguous=pair.ambiguous))
                rebuild_ranges.append((new_index, new_index + 1))
                continue
            if pair.new_index is None:
                changes.append(CueChange(pair.old_index, None,
                                         ambiguous=pair.ambiguous))
                continue

            old = self._snapshots[pair.old_index]
            new = new_snapshots[pair.new_index]
            change = CueChange(
                pair.old_index,
                pair.new_index,
                timing=old.start != new.start or old.end != new.end,
                body=old.text != new.text,
                inline=old.inline != new.inline,
                speaker=old.voices != new.voices,
                comment=old.comments != new.comments,
                ambiguous=pair.ambiguous
                )
            if (change.timing or change.body or change.inline or
                    change.speaker or change.comment or
                    change.ambiguous):
                changes.append(change)
            if (change.ambiguous or old.raw_text != new.raw_text):
                rebuild_ranges.append(
                    (pair.new_index, pair.new_index + 1)
                    )

        return UpdateReport(
            changes=changes,
            rebuild_ranges=_merge_ranges(rebuild_ranges),
            ambiguous=bool(ambiguous_new)
            )

    def _rebuild_caches(
            self,
            captions: Sequence[Caption],
            new_snapshots: Sequence[_Snapshot],
            old_for_new: dict[int, int | None],
            ambiguous_new: set[int]
            ) -> tuple[
                list[list[tuple[str, int, int]]],
                list[list[WordTiming]],
                list[list[Chapter]]
                ]:
        """
        Rebuild the per-caption caches, keeping the entries that did not.

        A caption with the same text keeps its occurrences, and one with the
        same text and times keeps its timings, with the caption index
        rewritten when the caption moved.

        :param captions: the new captions
        :param new_snapshots: their snapshots
        :param old_for_new: the old index paired with each new index
        :param ambiguous_new: the new indices that are ambiguous
        :returns: the occurrences, word timings and chapters
        """
        occurrences: list[list[tuple[str, int, int]]] = []
        word_timings: list[list[WordTiming]] = []
        chapters: list[list[Chapter]] = []

        for new_index, caption in enumerate(captions):
            old_index = old_for_new.get(new_index)
            snapshot = new_snapshots[new_index]
            old_snapshot = (
                self._snapshots[old_index]
                if old_index is not None
                else None
                )
            text_unchanged = (
                old_snapshot is not None and
                old_snapshot.raw_text == snapshot.raw_text and
                new_index not in ambiguous_new
                )

            if text_unchanged:
                occurrences.append(self._occurrences[typing.cast(int,
                                                                 old_index)])
            else:
                occurrences.append(
                    _words_of(snapshot.text, self._case_sensitive)
                    )

            timing_unchanged = (
                text_unchanged and
                old_snapshot is not None and
                old_snapshot.start == snapshot.start and
                old_snapshot.end == snapshot.end
                )
            if timing_unchanged:
                word_timings.append(self._reindexed_timings(
                    self._word_timings[typing.cast(int, old_index)],
                    new_index
                    ))
            else:
                word_timings.append(
                    _word_timings_of(caption, new_index)
                    )

            chapters.append(self._reused_chapters(
                caption, new_index, old_index, old_snapshot, snapshot,
                ambiguous_new
                ))

        return occurrences, word_timings, chapters

    def _reused_chapters(
            self,
            caption: Caption,
            new_index: int,
            old_index: int | None,
            old_snapshot: _Snapshot | None,
            new_snapshot: _Snapshot,
            ambiguous_new: set[int]
            ) -> list[Chapter]:
        """
        Return the chapters of a caption, reusing those with the same comments.

        Only the caption index and the start time are rewritten when the
        caption moved or its outer time changed, and the chapters are built
        again when the comments changed or the caption is ambiguous.

        :param caption: the new caption
        :param new_index: its index
        :param old_index: the index of its pair, or None
        :param old_snapshot: the snapshot of its pair, or None
        :param new_snapshot: the snapshot of the caption
        :param ambiguous_new: the new indices that are ambiguous
        :returns: the chapters
        """
        if (old_snapshot is None or
                new_index in ambiguous_new or
                old_snapshot.comments != new_snapshot.comments):
            return _chapters_of(caption, new_index)

        old_chapters = self._chapters[typing.cast(int, old_index)]
        if old_index == new_index and old_snapshot.start == new_snapshot.start:
            return old_chapters
        return [
            dataclasses.replace(
                chapter,
                caption_index=new_index,
                start=new_snapshot.start
                )
            for chapter in old_chapters
            ]

    @staticmethod
    def _reindexed_timings(
            timings: Sequence[WordTiming], caption_index: int
            ) -> list[WordTiming]:
        """
        Return word timings with another caption index.

        :param timings: the timings of a caption
        :param caption_index: the new caption index
        :returns: copies with the new index, or the same list
        """
        if timings and timings[0].caption_index == caption_index:
            return list(timings)
        return [
            dataclasses.replace(timing, caption_index=caption_index)
            for timing in timings
            ]

    def _update_search_index(
            self,
            old_occurrences: Sequence[Sequence[tuple[str, int, int]]],
            new_occurrences: Sequence[Sequence[tuple[str, int, int]]],
            new_snapshots: Sequence[_Snapshot],
            old_for_new: dict[int, int | None],
            ambiguous_new: set[int]
            ) -> None:
        """
        Rebuild the postings of the changed captions, keeping the others.

        The hits of words untouched by the edit have their caption index
        remapped. The words present in a deleted, inserted or changed
        caption are rebuilt from the new captions, so the index is the same
        as a full generation.

        :param old_occurrences: the words of the old captions
        :param new_occurrences: the words of the new captions
        :param new_snapshots: the snapshots of the new captions
        :param old_for_new: the old index paired with each new index
        :param ambiguous_new: the new indices that are ambiguous
        """
        changed_new = {
            new_index
            for new_index in range(len(new_snapshots))
            if self._caption_changed(
                new_index, old_for_new, ambiguous_new, new_snapshots
                )
            }
        kept_old = {
            old_index
            for old_index in old_for_new.values()
            if old_index is not None
            }
        deleted_old = set(range(len(self._snapshots))) - kept_old

        touched_words: set[str] = set()
        for old_index in deleted_old:
            touched_words.update(
                word for word, _, _ in old_occurrences[old_index]
                )
        for new_index in changed_new:
            paired_old = old_for_new.get(new_index)
            if paired_old is not None:
                touched_words.update(
                    word for word, _, _ in old_occurrences[paired_old]
                    )
            touched_words.update(
                word for word, _, _ in new_occurrences[new_index]
                )

        new_index_for_old = {
            old_index: new_index
            for new_index, old_index in old_for_new.items()
            if old_index is not None
            }
        postings: dict[str, list[SearchHit]] = {}
        for word, hits in self._search_index.items():
            if word in touched_words:
                continue
            remapped = [
                SearchHit(new_index_for_old[hit.caption_index],
                          hit.start,
                          hit.end)
                for hit in hits
                if hit.caption_index in new_index_for_old
                ]
            if remapped:
                postings[word] = remapped

        for word in touched_words:
            rebuilt = [
                SearchHit(caption_index, start, end)
                for caption_index, words in enumerate(new_occurrences)
                for occurrence_word, start, end in words
                if occurrence_word == word
                ]
            if rebuilt:
                postings[word] = rebuilt

        self._search_index = postings

    def _caption_changed(
            self,
            new_index: int,
            old_for_new: dict[int, int | None],
            ambiguous_new: set[int],
            new_snapshots: Sequence[_Snapshot]
            ) -> bool:
        """
        Return whether the text of a new caption changed against its pair.

        :param new_index: the new caption index
        :param old_for_new: the old index paired with each new index
        :param ambiguous_new: the new indices that are ambiguous
        :param new_snapshots: the snapshots of the new captions
        :returns: true when the caption is inserted, ambiguous or edited
        """
        old_index = old_for_new.get(new_index)
        if old_index is None or new_index in ambiguous_new:
            return True
        return (
            self._snapshots[old_index].raw_text !=
            new_snapshots[new_index].raw_text
            )
