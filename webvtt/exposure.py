"""Line exposure of captions rendered in WebVTT regions.

A region shows a fixed number of lines. When a new caption is shown in the
same region, its lines are added below the lines already there, and in a
scrolling region the oldest lines are pushed out when the region is full.
A caption that lasts ten seconds can therefore stop being visible after
three seconds, when the next caption in its region arrives.

The functions of this module simulate that rendering line by line, and give
the time each line is actually visible and the event that ends it. Each
region is simulated on its own, so captions in other regions never push a
line out of this one.
"""

import typing
from collections import deque
from collections.abc import (
    Callable, Iterable, Iterator, Mapping, Sequence
    )
from dataclasses import dataclass, replace

from .models import (
    Caption,
    Region,
    Timestamp,
    _Position,
    _optional_timestamp
    )

# the number of lines of a region without a lines setting, as WebVTT defines
DEFAULT_REGION_LINES = 3

_ExitReason = typing.Literal['end', 'scroll', 'overflow', 'window']

# how many lines each caption takes when rendered: a number for every
# caption, the number for the captions given in a mapping, or a function
_LineCounts = (
    Sequence[int] |
    Mapping[int, int] |
    Callable[[Caption], int] |
    None
    )


@dataclass(frozen=True)
class LineExposure:
    """
    A line of a caption and the time it is visible in its region.

    The visible interval starts at ``start_in_milliseconds`` and ends
    before ``end_in_milliseconds``, which is the part of the life of the
    line within the requested window. ``exit_in_milliseconds`` is when the
    line actually leaves the region, which is after the visible interval
    when a window cuts it short. The ``exit`` property says what ends the
    visible interval: ``end`` when the caption ends, ``scroll`` when newer
    lines in the same region push it out, ``overflow`` when it does not
    fit in a region that does not scroll, or ``window`` when the requested
    window ends first.

    :param region_id: identifier of the region
    :param caption_index: index of the caption in the list given, from 0
    :param line_index: index of the line in the caption, from 0
    :param text: the text of the line
    :param start_in_milliseconds: start of the visible interval
    :param end_in_milliseconds: end of the visible interval
    :param exit_reason: why the line leaves the region, without the window
    :param exit_in_milliseconds: when the line leaves the region
    :param exit_caption_index: the caption that ends the line, either the
        caption of the line when it ends or does not fit, or the caption
        whose lines push it out, or None
    :param line_count: the number of lines the caption takes when it was
        given, or None when it is the number of lines of its text
    """

    region_id: str
    caption_index: int
    line_index: int
    text: str
    start_in_milliseconds: int
    end_in_milliseconds: int
    exit_reason: _ExitReason
    exit_in_milliseconds: int
    exit_caption_index: int | None = None
    line_count: int | None = None

    @property
    def start(self) -> str:
        """Return the start of the visible interval as a timestamp."""
        return str(self.start_time)

    @property
    def end(self) -> str:
        """Return the end of the visible interval as a timestamp."""
        return str(self.end_time)

    @property
    def start_time(self) -> Timestamp:
        """Return the start of the visible interval as a `Timestamp`."""
        return Timestamp._from_milliseconds(self.start_in_milliseconds)

    @property
    def end_time(self) -> Timestamp:
        """Return the end of the visible interval as a `Timestamp`."""
        return Timestamp._from_milliseconds(self.end_in_milliseconds)

    @property
    def exit(self) -> _ExitReason:
        """
        Return the event that ends the visible interval.

        It is ``window`` when the line is still visible after the requested
        window, and the reason it leaves the region otherwise.
        """
        return ('window'
                if self.end_in_milliseconds < self.exit_in_milliseconds
                else self.exit_reason)

    @property
    def event(self) -> _ExitReason:
        """Return the event that ends the visible interval, as `exit`."""
        return self.exit

    @property
    def visible(self) -> bool:
        """Return whether the line is visible for any time."""
        return self.end_in_milliseconds > self.start_in_milliseconds

    @property
    def visible_start(self) -> int:
        """Return the start of the visible interval in milliseconds."""
        return self.start_in_milliseconds

    @property
    def visible_end(self) -> int:
        """Return the end of the visible interval in milliseconds."""
        return self.end_in_milliseconds

    @property
    def exit_time(self) -> Timestamp:
        """Return when the line leaves the region as a `Timestamp`."""
        return Timestamp._from_milliseconds(self.exit_in_milliseconds)

    @property
    def displaced_by(self) -> int | None:
        """
        Return the caption that pushed the line out, or None.

        Only a line pushed out of a scrolling region has one, which is the
        ``caption_index`` of the caption whose lines arrived.
        """
        return (self.exit_caption_index
                if self.exit_reason == 'scroll' else None)

    @property
    def duration(self) -> float:
        """Return the visible time of the line in seconds."""
        return max(self.end_in_milliseconds -
                   self.start_in_milliseconds, 0) / 1000

    @property
    def duration_in_milliseconds(self) -> int:
        """Return the visible time of the line in milliseconds."""
        return max(self.end_in_milliseconds -
                   self.start_in_milliseconds, 0)

    def to_dict(self) -> dict[str, object]:
        """Return the line exposure as a dictionary of plain values."""
        return {
            'region_id': self.region_id,
            'caption_index': self.caption_index,
            'line_index': self.line_index,
            'text': self.text,
            'start': self.start,
            'end': self.end,
            'start_in_milliseconds': self.start_in_milliseconds,
            'end_in_milliseconds': self.end_in_milliseconds,
            'exit': self.exit,
            'exit_reason': self.exit_reason,
            'exit_time': str(self.exit_time),
            'exit_in_milliseconds': self.exit_in_milliseconds,
            'exit_caption_index': self.exit_caption_index,
            'duration': self.duration,
            'line_count': self.line_count,
            }


@dataclass(frozen=True)
class RegionExposure:
    """
    The line exposures of a single region.

    :param region_id: identifier of the region
    :param lines: number of lines the region can show at once
    :param scrolling: whether the region scrolls up when it is full
    :param exposures: the lines that are visible for some time, by start
        time, caption order and line order
    :param overflows: the lines that never fit in a region that does not
        scroll, so they are never visible
    :param start_in_milliseconds: start of the requested window, or None
    :param end_in_milliseconds: end of the requested window, or None
    """

    region_id: str
    lines: int
    scrolling: bool
    exposures: list[LineExposure]
    overflows: list[LineExposure]
    start_in_milliseconds: int | None = None
    end_in_milliseconds: int | None = None

    def __iter__(self) -> Iterator[LineExposure]:
        """Iterate over the line exposures of the region."""
        return iter(self.exposures)

    def __len__(self) -> int:
        """Return the number of line exposures of the region."""
        return len(self.exposures)

    @property
    def total_duration(self) -> float:
        """Return the exposure of all the lines of the region in seconds."""
        return self.total_duration_in_milliseconds / 1000

    @property
    def total_duration_in_milliseconds(self) -> int:
        """Return the visible time of all the lines in milliseconds."""
        return sum(exposure.duration_in_milliseconds
                   for exposure in self.exposures)

    @property
    def window_duration(self) -> float | None:
        """Return the length of the window in seconds, or None."""
        if (self.start_in_milliseconds is None or
                self.end_in_milliseconds is None):
            return None
        return max(self.end_in_milliseconds -
                   self.start_in_milliseconds, 0) / 1000

    @property
    def window_duration_in_milliseconds(self) -> int | None:
        """Return the length of the window in milliseconds, or None."""
        if (self.start_in_milliseconds is None or
                self.end_in_milliseconds is None):
            return None
        return max(self.end_in_milliseconds -
                   self.start_in_milliseconds, 0)

    @property
    def maximum_duration(self) -> float | None:
        """
        Return the most the lines can be visible within the window.

        It is the number of lines of the region times the length of the
        window, or None when the window has no start or end, as there is
        then no fixed length to compare with.
        """
        window = self.window_duration
        return None if window is None else self.lines * window

    @property
    def maximum_duration_in_milliseconds(self) -> int | None:
        """
        Return the most the lines can be visible within the window, in ms.

        It is the number of lines of the region times the length of the
        window, or None when the window has no start or end.
        """
        window = self.window_duration_in_milliseconds
        return None if window is None else self.lines * window

    @property
    def within_limit(self) -> bool:
        """
        Return whether the exposure is within the capacity of the region.

        The lines visible together are never more than the lines of the
        region, so their total exposure cannot exceed the lines of the
        region times the length of the window.
        """
        maximum = self.maximum_duration_in_milliseconds
        return (maximum is None or
                self.total_duration_in_milliseconds <= maximum)

    def to_dict(self) -> dict[str, object]:
        """Return the region exposure as a dictionary of plain values."""
        return {
            'region_id': self.region_id,
            'lines': self.lines,
            'scroll': self.scrolling,
            'exposures': [exposure.to_dict()
                          for exposure in self.exposures],
            'overflows': [exposure.to_dict()
                          for exposure in self.overflows],
            'total_duration': self.total_duration,
            'window_start_in_milliseconds': self.start_in_milliseconds,
            'window_end_in_milliseconds': self.end_in_milliseconds,
            'within_limit': self.within_limit,
            }


@dataclass(frozen=True)
class _Cue:
    """A caption of a region, with the lines it shows."""

    index: int
    start: int
    end: int
    lines: tuple[str, ...]
    line_count: int


@dataclass
class _VisibleLine:
    """A line currently shown in a region, before it leaves."""

    caption_index: int
    line_index: int
    text: str
    start: int
    line_count: int


def _region_lines(region: Region) -> int:
    """
    Return the number of lines a region can show at once.

    A region without a valid ``lines`` setting shows 3 lines, as WebVTT
    defines.
    """
    return (DEFAULT_REGION_LINES if region.lines is None
            else int(region.lines))


def _caption_line_count(
        line_counts: _LineCounts,
        index: int,
        caption: Caption
        ) -> int:
    """
    Return the number of lines a caption takes when rendered.

    A caption without a count takes as many lines as its text has, which
    is 0 for a caption without text.

    :param line_counts: the counts given, or None
    :param index: the index of the caption in the captions, from 0
    :param caption: the caption
    :returns: the number of rendered lines, 0 or greater

    :raises TypeError: if a count is not an integer
    :raises ValueError: if a count is negative, or there are fewer counts
        than captions
    """
    if line_counts is None:
        return len(caption.lines)
    if isinstance(line_counts, Mapping):
        count = line_counts.get(index, len(caption.lines))
    elif callable(line_counts):
        count = line_counts(caption)
    else:
        if index >= len(line_counts):
            raise ValueError(
                f'There are fewer line counts than captions, {index} given'
                )
        count = line_counts[index]

    if type(count) is not int:
        raise TypeError(f'Invalid number of lines {count!r}')
    if count < 0:
        raise ValueError(f'Invalid number of lines {count!r}')
    return count


def _rendered_lines(caption: Caption, count: int) -> tuple[str, ...]:
    """
    Return the text of the rendered lines of a caption.

    The lines of the text are used in order. A rendered line past the text,
    from a line of the text that wrapped, has no text of its own, as where
    it wrapped is not known.

    :param caption: the caption
    :param count: the number of lines it takes when rendered
    :returns: the text of each rendered line
    """
    return tuple(
        caption.lines[index] if index < len(caption.lines) else ''
        for index in range(count)
        )


def _cues_of_region(
        captions: Sequence[Caption],
        region_id: str,
        line_counts: _LineCounts = None
        ) -> list[_Cue]:
    """
    Return the captions shown in a region, in document order at each time.

    Captions that end before they start, and those with no rendered line,
    show nothing and are left out. The order is by start time, and captions
    that start at the same time keep their order in the list, as WebVTT
    renders them in that order.
    """
    cues = []
    for index, caption in enumerate(captions):
        if (caption.settings.region != region_id or
                caption.end_in_milliseconds <=
                caption.start_in_milliseconds):
            continue
        count = _caption_line_count(line_counts, index, caption)
        if not count:
            continue
        cues.append(_Cue(
            index,
            caption.start_in_milliseconds,
            caption.end_in_milliseconds,
            _rendered_lines(caption, count),
            count
            ))
    cues.sort(key=lambda cue: (cue.start, cue.index))
    return cues


def _exposure(
        line: _VisibleLine,
        end: int,
        reason: _ExitReason,
        caption_index: int | None
        ) -> LineExposure:
    """Return the exposure of a visible line that leaves a region."""
    return LineExposure(
        region_id='',
        caption_index=line.caption_index,
        line_index=line.line_index,
        text=line.text,
        start_in_milliseconds=line.start,
        end_in_milliseconds=end,
        exit_reason=reason,
        exit_in_milliseconds=end,
        exit_caption_index=caption_index,
        line_count=line.line_count
        )


def _exposures_of_region(
        cues: Sequence[_Cue],
        region_id: str,
        capacity: int,
        scrolling: bool
        ) -> tuple[list[LineExposure], list[LineExposure]]:
    """
    Return the line exposures of the captions of a region.

    The lines shown are kept in order, oldest first. A caption that ends
    frees its lines before the captions that start at the same time are
    added, so a caption starting when another ends does not push it out.
    In a scrolling region, lines are pushed out from the oldest when the
    region is full. In a region that does not scroll, the lines that do
    not fit are not shown at all, as the region keeps the lines it has.

    :returns: the visible lines, and the lines that never fit
    """
    starts: dict[int, list[_Cue]] = {}
    ends: dict[int, list[_Cue]] = {}
    for cue in cues:
        starts.setdefault(cue.start, []).append(cue)
        ends.setdefault(cue.end, []).append(cue)

    visible: deque[_VisibleLine] = deque()
    exposures: list[LineExposure] = []
    overflows: list[LineExposure] = []

    for time in sorted(set(starts) | set(ends)):
        # a line is no longer visible at its end time, so endings come
        # before the captions starting at the same time
        _end_cues(visible, ends.get(time, ()), time, region_id, exposures)
        for cue in starts.get(time, ()):
            _start_cue(visible, cue, time, region_id, capacity, scrolling,
                       exposures, overflows)

    exposures.sort(key=_line_order)
    overflows.sort(key=_line_order)
    return exposures, overflows


def _end_cues(
        visible: deque[_VisibleLine],
        cues: Iterable[_Cue],
        time: int,
        region_id: str,
        exposures: list[LineExposure]
        ) -> None:
    """End the lines of the captions that end at a time."""
    for cue in cues:
        kept: deque[_VisibleLine] = deque()
        for line in visible:
            if line.caption_index == cue.index:
                exposures.append(replace(
                    _exposure(line, time, 'end', cue.index),
                    region_id=region_id
                    ))
            else:
                kept.append(line)
        visible.clear()
        visible.extend(kept)


def _start_cue(
        visible: deque[_VisibleLine],
        cue: _Cue,
        time: int,
        region_id: str,
        capacity: int,
        scrolling: bool,
        exposures: list[LineExposure],
        overflows: list[LineExposure]
        ) -> None:
    """
    Add the lines of a caption that starts at a time to those shown.

    In a scrolling region, the oldest lines are pushed out when the region
    is full. In a region that does not scroll, the lines that do not fit
    are not shown at all, as the region keeps the lines it has.
    """
    for line_index, text in enumerate(cue.lines):
        line = _VisibleLine(
            cue.index, line_index, text, time, cue.line_count
            )
        visible.append(line)
        if scrolling:
            while len(visible) > capacity:
                pushed = visible.popleft()
                exposures.append(replace(
                    _exposure(pushed, time, 'scroll', cue.index),
                    region_id=region_id
                    ))
        elif len(visible) > capacity:
            visible.pop()
            overflows.append(replace(
                _exposure(line, time, 'overflow', cue.index),
                region_id=region_id
                ))


def _line_order(exposure: LineExposure) -> tuple[int, int, int, int]:
    """Return the order of a line exposure: time, caption and line."""
    return (exposure.start_in_milliseconds,
            exposure.caption_index,
            exposure.line_index,
            exposure.end_in_milliseconds)


def _in_window(
        start: int,
        end: int,
        window_start: int | None,
        window_end: int | None
        ) -> bool:
    """
    Return whether a visible interval has a part in a window.

    Intervals are half-open, so an interval that ends exactly at the start
    of the window is not in it, and two adjacent intervals do not share a
    time. An interval without duration is only in a window that contains
    its time.
    """
    if end <= start:
        if window_start is not None and start < window_start:
            return False
        if window_end is not None and start >= window_end:
            return False
        return True
    if window_end is not None and start >= window_end:
        return False
    if window_start is not None and end <= window_start:
        return False
    return True


def _clip_exposures(
        exposures: Iterable[LineExposure],
        window_start: int | None,
        window_end: int | None
        ) -> list[LineExposure]:
    """Return the line exposures limited to a window."""
    clipped = []
    for exposure in exposures:
        start = exposure.start_in_milliseconds
        end = exposure.end_in_milliseconds
        if not _in_window(start, end, window_start, window_end):
            continue
        if window_start is not None:
            start = max(start, window_start)
        if window_end is not None:
            end = min(end, window_end)
        clipped.append(replace(
            exposure,
            start_in_milliseconds=start,
            end_in_milliseconds=end
            ))
    return clipped


def _window(
        start: _Position | None,
        end: _Position | None
        ) -> tuple[int | None, int | None]:
    """
    Return the bounds of a window in milliseconds.

    :param start: start of the window, or None for no start
    :param end: end of the window, or None for no end
    :returns: the bounds in milliseconds

    :raises ValueError: if the start is after the end
    :raises MalformedCaptionError: if a bound is not a valid time
    """
    start_time = _optional_timestamp(start)
    end_time = _optional_timestamp(end)
    start_ms = None if start_time is None else start_time.in_milliseconds()
    end_ms = None if end_time is None else end_time.in_milliseconds()
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        raise ValueError('The start of the window is after its end')
    return start_ms, end_ms


def region_exposures(
        captions: Sequence[Caption],
        regions: Sequence[Region],
        *,
        start: _Position | None = None,
        end: _Position | None = None,
        line_counts: _LineCounts = None
        ) -> list[RegionExposure]:
    """
    Return the line exposures of the captions of every region.

    Each region is simulated on its own, so a caption in another region
    never pushes a line out of it. Captions in a region that is not
    defined, and captions without a region, are not rendered in a region
    and are left out.

    :param captions: the captions, in document order for equal times
    :param regions: the regions the captions use
    :param start: start of the window, as a timestamp, a `Timestamp`, a
        number of seconds or a ``timedelta``, or None for no start
    :param end: end of the window, as the start, or None for no end
    :param line_counts: how many lines each caption takes when rendered,
        when it is not the number of lines of its text: a sequence with a
        count for each caption by index, a mapping from the index of a
        caption to its count, with those not in it using the lines of the
        caption, or a function taking a caption. A caption that takes no
        line is not shown
    :returns: a `RegionExposure` for each region, in the order of
        ``regions``

    :raises ValueError: if the start of the window is after its end, or a
        count is negative or there are fewer counts than captions
    :raises TypeError: if a count is not an integer
    :raises MalformedCaptionError: if a bound of the window is not valid
    """
    window_start, window_end = _window(start, end)
    if line_counts is not None:
        # validate the counts of every caption, also those of other
        # regions and those that are not shown, so a count that is wrong
        # is reported wherever the caption is
        for index, caption in enumerate(captions):
            _caption_line_count(line_counts, index, caption)
    reports = []
    seen: set[str] = set()

    for region in regions:
        region_id = region.id
        if region_id is None or region_id in seen:
            continue
        seen.add(region_id)
        capacity = _region_lines(region)
        scrolling = region.scroll == 'up'
        exposures, overflows = _exposures_of_region(
            _cues_of_region(captions, region_id, line_counts),
            region_id,
            capacity,
            scrolling
            )
        reports.append(RegionExposure(
            region_id,
            capacity,
            scrolling,
            _clip_exposures(exposures, window_start, window_end),
            _clip_exposures(overflows, window_start, window_end),
            window_start,
            window_end
            ))
    return reports


def region_exposure(
        region: Region,
        captions: Sequence[Caption],
        *,
        start: _Position | None = None,
        end: _Position | None = None,
        line_counts: _LineCounts = None
        ) -> RegionExposure:
    """
    Return the line exposure of a single region.

    :param region: the region
    :param captions: the captions
    :param start: start of the window, or None for no start
    :param end: end of the window, or None for no end
    :param line_counts: how many lines each caption takes when rendered,
        as in `region_exposures`
    :returns: a `RegionExposure`

    :raises ValueError: if the start of the window is after its end, or a
        count is negative or there are fewer counts than captions
    :raises TypeError: if a count is not an integer
    :raises MalformedCaptionError: if a bound of the window is not valid
    """
    reports = region_exposures(
        captions, [region], start=start, end=end, line_counts=line_counts
        )
    if reports:
        return reports[0]
    # a region without an identifier shows nothing, but still has its
    # settings, so give it an empty report
    window_start, window_end = _window(start, end)
    return RegionExposure(
        '', _region_lines(region), region.scroll == 'up', [], [],
        window_start, window_end
        )


def line_exposures(
        captions: Sequence[Caption],
        regions: Sequence[Region],
        *,
        start: _Position | None = None,
        end: _Position | None = None,
        line_counts: _LineCounts = None
        ) -> list[LineExposure]:
    """
    Return the line exposures of the captions of every region, in one list.

    The lines are in the order of the regions, and by time, caption order
    and line order within each region.

    :param captions: the captions, in document order for equal times
    :param regions: the regions the captions use
    :param start: start of the window, or None for no start
    :param end: end of the window, or None for no end
    :param line_counts: how many lines each caption takes when rendered,
        as in `region_exposures`
    :returns: the `LineExposure` of every visible line

    :raises ValueError: if the start of the window is after its end, or a
        count is negative or there are fewer counts than captions
    :raises TypeError: if a count is not an integer
    :raises MalformedCaptionError: if a bound of the window is not valid
    """
    return [
        exposure
        for report in region_exposures(
            captions, regions, start=start, end=end,
            line_counts=line_counts
            )
        for exposure in report.exposures
        ]
