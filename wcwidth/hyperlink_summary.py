r"""
Terminal capture summaries with OSC 8 hyperlink attribution.

This module turns a terminal capture (a string that may contain SGR styling and
OSC 8 hyperlinks) into deduplicating *keys*:

- A **visible text key** identifies what is displayed on screen.  Only the visible
  characters matter, so changing colors around the same text produces the same key.
- A **clickable content key** additionally records every OSC 8 hyperlink
  region: its target URL and the range of visible characters it covers.  Two lines
  that display identical text but make different parts clickable get different keys.

Counting each kind of key separately explains why a capture may contain more
clickable regions than distinct visible lines: the same visible text can back
several distinct hyperlink regions.
"""

from __future__ import annotations

# std imports
from collections import Counter

import typing

# local
from .escape_sequences import _SEQUENCE_CLASSIFY, strip_sequences
from .hyperlink import HYPERLINK_CLOSE_RE, HYPERLINK_OPEN_RE, HyperlinkParams


class HyperlinkRegion(typing.NamedTuple):
    r"""
    A clickable region over the visible characters of a terminal capture.

    Character ranges are half-open offsets into the *visible text*, i.e. the
    capture with every escape sequence removed, as returned by
    :func:`wcwidth.strip_sequences`.  Offsets count Python characters rather than
    terminal cells.

    :param url: Hyperlink target from the OSC 8 open sequence; an empty string
        means the link has no target.
    :param start: Offset of the first visible character covered by the region.
    :param end: Offset just past the last visible character covered by the region.

    Example::

        >>> region = HyperlinkRegion(url='http://example.com', start=0, end=5)
        >>> region.is_empty
        False
        >>> visible = 'Hello'
        >>> visible[region.start:region.end]
        'Hello'
    """

    url: str
    start: int
    end: int

    @property
    def is_empty(self) -> bool:
        """Return ``True`` when the region covers no visible characters."""
        return self.start >= self.end


def iter_hyperlink_regions(text: str) -> typing.Iterator[HyperlinkRegion]:
    r"""
    Iterate OSC 8 hyperlink regions over *text*'s visible characters.

    The capture is scanned left to right while tracking the active OSC 8
    hyperlink and the number of visible characters emitted.  Every time the
    hyperlink state changes (an open or close sequence), the region that just
    ended is emitted first, including regions that cover no visible characters.

    OSC 8 is a state attribute, so the rules follow what a terminal emulator does:

    - An open sequence with an empty URL (it must still carry parameters, since
      the bare ``ESC ] 8 ; ;`` form is defined as a close) opens an empty-target
      link whose region URL is the empty string.
    - A close sequence closes the active link regardless of what preceded it.
    - Opening a new link without first closing replaces the active link; the
      replaced region is still attributed.
    - An open sequence while an identical link is already active closes and
      reopens it, so two adjacent regions are reported.
    - A link left open at end of text runs to the final visible character.
    - Regions spanning line boundaries keep their range across the newline.

    :param text: Terminal capture, possibly containing escape sequences.
    :returns: :class:`HyperlinkRegion` objects in source order.  Ranges index the
        visible text (``strip_sequences(text)``), not *text* itself.

    Example::

        >>> capture = '\x1b]8;;http://example.com\x07Hello\x1b]8;;\x07 world'
        >>> list(iter_hyperlink_regions(capture))
        [HyperlinkRegion(url='http://example.com', start=0, end=5)]
    """
    active: HyperlinkParams | None = None
    region_start = 0
    visible_len = 0
    position = 0
    text_len = len(text)

    while position < text_len:
        if text[position] == '\x1b':
            match = _SEQUENCE_CLASSIFY.match(text, position)
            if match is not None:
                sequence = match.group()
                # Check close first: HYPERLINK_OPEN_RE also matches the empty-URL
                # form, which per OSC 8 is defined as the close sequence.
                close_match = HYPERLINK_CLOSE_RE.fullmatch(sequence)
                if close_match is not None:
                    if active is not None:
                        yield HyperlinkRegion(active.url, region_start, visible_len)
                    active = None
                    region_start = visible_len
                else:
                    open_match = HYPERLINK_OPEN_RE.fullmatch(sequence)
                    if open_match is not None:
                        if active is not None:
                            yield HyperlinkRegion(active.url, region_start, visible_len)
                        active = HyperlinkParams(
                            url=open_match.group(2),
                            params=open_match.group(1),
                            terminator=open_match.group(3),
                        )
                        region_start = visible_len

                position = match.end()
                continue

        # Ordinary character, or a lone/unrecognized ESC that strip_sequences()
        # would likewise leave in place.
        visible_len += 1
        position += 1

    if active is not None:
        yield HyperlinkRegion(active.url, region_start, visible_len)


class HyperlinkSummary(typing.NamedTuple):
    r"""
    Deduplicating keys of a terminal capture.

    The two keys are hashable and designed for use as dictionary keys or
    :class:`collections.Counter` entries:

    :param visible_key: The capture with all escape sequences removed, i.e. the
        visible text.  Captures differing only in color share this key.
    :param clickable_key: The visible text paired with a tuple of
        :class:`HyperlinkRegion`.  Captures whose visible intervals carry
        different hyperlink targets get different keys.

    Example::

        >>> summary = HyperlinkSummary.from_capture(
        ...     '\x1b[31m\x1b]8;;http://example.com\x07Hi\x1b]8;;\x07\x1b[0m')
        >>> summary.visible_key
        'Hi'
        >>> summary.clickable_key
        ('Hi', (HyperlinkRegion(url='http://example.com', start=0, end=2),))
    """

    visible_key: str
    clickable_key: tuple[str, tuple[HyperlinkRegion, ...]]

    @classmethod
    def from_capture(cls, text: str) -> HyperlinkSummary:
        r"""
        Build the visible and clickable content keys of terminal capture *text*.

        :param text: Terminal capture, possibly containing escape sequences.
        :returns: A summary whose keys describe *text*.

        Example::

            >>> red = '\x1b[31m详情\x1b[0m'
            >>> blue = '\x1b[34m详情\x1b[0m'
            >>> HyperlinkSummary.from_capture(red).visible_key == (
            ...     HyperlinkSummary.from_capture(blue).visible_key)
            True
        """
        visible = strip_sequences(text)
        regions = tuple(iter_hyperlink_regions(text))
        return cls(visible_key=visible, clickable_key=(visible, regions))


def _count_keys(
    lines: typing.Iterable[str],
    attribute: str,
) -> Counter[typing.Any]:
    """Count one of the two summary keys across capture lines."""
    counter: Counter[typing.Any] = Counter()
    for line in lines:
        counter[getattr(HyperlinkSummary.from_capture(line), attribute)] += 1
    return counter


def count_visible_keys(lines: typing.Iterable[str]) -> Counter[str]:
    r"""
    Count lines by their visible text key.

    Lines that differ only in escape sequences (for example a color change) are
    counted together.

    :param lines: Iterable of terminal capture lines.
    :returns: :class:`collections.Counter` keyed by visible text.

    Example::

        >>> count_visible_keys(['\x1b[31mHi\x1b[0m', '\x1b[34mHi\x1b[0m', 'Yo'])
        Counter({'Hi': 2, 'Yo': 1})
    """
    return _count_keys(lines, 'visible_key')


def count_clickable_keys(
    lines: typing.Iterable[str],
) -> Counter[tuple[str, tuple[HyperlinkRegion, ...]]]:
    r"""
    Count lines by their clickable content key.

    Lines are only counted together when they display the same text *and* place
    the same hyperlink targets over the same visible character ranges.  This is
    how identical-looking lines backed by different OSC 8 links are separated.

    :param lines: Iterable of terminal capture lines.
    :returns: :class:`collections.Counter` keyed by
        :attr:`HyperlinkSummary.clickable_key`.

    Example::

        >>> a = '\x1b]8;;http://a\x07详情\x1b]8;;\x07'
        >>> b = '\x1b]8;;http://b\x07详情\x1b]8;;\x07'
        >>> len(count_clickable_keys([a, b]))
        2
    """
    return _count_keys(lines, 'clickable_key')
