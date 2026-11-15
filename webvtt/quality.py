"""Quality index module."""

from collections.abc import Sequence
from dataclasses import dataclass

from .models import Caption, Region, Timestamp

DEFAULT_VIEWPORT_WIDTH = 1920
DEFAULT_VIEWPORT_HEIGHT = 1080

# the default height of a line of text, as a fraction of the viewport height
DEFAULT_LINE_HEIGHT_RATIO = 0.0533

# the default number of lines of a region, as in the WebVTT specification
_DEFAULT_REGION_LINES = 3


@dataclass
class Viewport:
    """
    The fixed area the captions are rendered on, in pixels.

    :param width: width of the viewport in pixels
    :param height: height of the viewport in pixels
    """

    width: float
    height: float


@dataclass
class LineBox:
    """
    The measured box of a line of text of a caption, in pixels.

    For a horizontal caption the lines are read top to bottom and the
    width is the inked width of the line. For a vertical caption the lines
    are the columns of text, read in the direction they are stacked, and
    the width is the thickness of the column.

    :param width: width of the box in pixels
    :param height: height of the box in pixels
    """

    width: float
    height: float


@dataclass
class Box:
    """
    A rectangle in the viewport, in pixels from its top left corner.

    :param x: horizontal offset of the left edge
    :param y: vertical offset of the top edge
    :param width: width of the rectangle
    :param height: height of the rectangle
    """

    x: float
    y: float
    width: float
    height: float

    def __str__(self) -> str:
        """Return the box as ``x,y widthxheight``."""
        return (
            f'{self.x:g},{self.y:g} {self.width:g}x{self.height:g}'
            )

    @property
    def right(self) -> float:
        """Return the horizontal offset of the right edge."""
        return self.x + self.width

    @property
    def bottom(self) -> float:
        """Return the vertical offset of the bottom edge."""
        return self.y + self.height

    def intersection(self, other: 'Box') -> 'Box | None':
        """
        Return the overlap with another box, or None when there is none.

        Boxes that only share an edge do not overlap: the intersection
        must have a width and a height greater than zero.

        :param other: the other box
        :returns: the box both boxes cover, or None
        """
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if x < right and y < bottom:
            return Box(_round(x), _round(y),
                       _round(right - x), _round(bottom - y))
        return None


@dataclass
class Overlap:
    """
    Record of two captions that occupy the same area at the same time.

    :param first: the index of the earlier caption in ``captions``, from 0
    :param second: the index of the later caption in ``captions``, from 0
    :param start: the start of the time both captions are shown
    :param end: the end of the time both captions are shown
    :param region: the identifier of the region both captions are in, or
        None when they are not in the same region
    :param area: the area of the viewport both captions cover
    """

    first: int
    second: int
    start: Timestamp
    end: Timestamp
    region: str | None
    area: Box

    def __str__(self) -> str:
        """Return a readable representation of the overlap."""
        return (
            f'captions {self.first + 1} and {self.second + 1} overlap '
            f'{self.start} --> {self.end} '
            f'in {self.region or "the viewport"} at {self.area}'
            )


@dataclass
class _Placement:
    """The line boxes of a caption in the viewport, and its region."""

    region: str | None
    lines: list[Box]


def overlap_index(
        captions: Sequence[Caption],
        *,
        regions: Sequence[Region] = (),
        viewport: Viewport | None = None,
        line_height: float | None = None,
        measured_lines: Sequence[Sequence[LineBox | tuple[float, float]]
                                 | None] | None = None
        ) -> list[Overlap]:
    """
    Return the pairs of captions that overlap on screen, as `Overlap`.

    The captions are placed in the viewport from their cue settings and
    the regions, with every position resolved to the same coordinates in
    pixels: the ``line`` and ``position`` percentages and the line numbers
    share them, and vertical captions are placed on their own axis, with
    ``line`` across the viewport and ``position`` along it. The boxes of
    the lines of text are the measured ones when given, or fill the cue
    box with lines of ``line_height`` pixels when they are not.

    Two captions are a pair when they are shown at the same time and
    their lines of text cover a common area. Pairs that only touch are
    not included: a caption that starts when the other ends does not
    overlap it in time, and boxes that only share an edge do not overlap
    on screen.

    :param captions: the captions to check
    :param regions: the regions the captions refer to
    :param viewport: the viewport, default: 1920x1080 pixels
    :param line_height: the height of a line of text in pixels, default:
        5.33% of the viewport height
    :param measured_lines: the measured box of each line of text of each
        caption, as a sequence parallel to ``captions`` with a sequence
        of `LineBox` or ``(width, height)`` pairs per caption, or None
        for a caption whose lines fill its cue box
    :returns: the overlaps, ordered by their start time

    :raises ValueError: if ``measured_lines`` is not parallel to
        ``captions``
    """
    if viewport is None:
        viewport = Viewport(DEFAULT_VIEWPORT_WIDTH, DEFAULT_VIEWPORT_HEIGHT)
    if line_height is None:
        line_height = viewport.height * DEFAULT_LINE_HEIGHT_RATIO
    if measured_lines is not None and len(measured_lines) != len(captions):
        raise ValueError(
            f'Expected measured lines for {len(captions)} captions, '
            f'got {len(measured_lines)}'
            )

    region_boxes = _region_boxes(regions, viewport, line_height)
    placements = [
        _place(
            caption,
            region_boxes,
            viewport,
            line_height,
            _line_boxes(
                None if measured_lines is None else measured_lines[index]
                )
            )
        for index, caption in enumerate(captions)
        ]

    overlaps = []
    for second in range(len(captions)):
        for first in range(second):
            start = max(captions[first].start_in_milliseconds,
                        captions[second].start_in_milliseconds)
            end = min(captions[first].end_in_milliseconds,
                      captions[second].end_in_milliseconds)
            if start >= end:
                # back to back captions do not overlap in time
                continue
            area = _shared_area(placements[first], placements[second])
            if area is None:
                continue
            overlaps.append(Overlap(
                first,
                second,
                Timestamp._from_milliseconds(start),
                Timestamp._from_milliseconds(end),
                (placements[first].region
                 if placements[first].region == placements[second].region
                 else None),
                area
                ))

    return sorted(
        overlaps,
        key=lambda overlap: (overlap.start.in_milliseconds(),
                             overlap.end.in_milliseconds(),
                             overlap.first,
                             overlap.second)
        )


def _line_boxes(
        boxes: Sequence[LineBox | tuple[float, float]] | None
        ) -> list[LineBox] | None:
    """Return the measured boxes as `LineBox`, or None when not given."""
    if boxes is None:
        return None
    return [
        box if isinstance(box, LineBox) else LineBox(*box)
        for box in boxes
        ]


def _region_boxes(
        regions: Sequence[Region],
        viewport: Viewport,
        line_height: float
        ) -> dict[str, Box]:
    """Return the box of each region in the viewport, by identifier."""
    boxes = {}
    for region in regions:
        if region.id is None:
            continue
        width = _percentage(region.width, 100.0) / 100 * viewport.width
        lines = int(region.lines) if region.lines else _DEFAULT_REGION_LINES
        height = lines * line_height
        anchor_x, anchor_y = _anchor(region.regionanchor, 0.0, 100.0)
        view_x, view_y = _anchor(region.viewportanchor, 0.0, 100.0)
        boxes[region.id] = Box(
            view_x / 100 * viewport.width - anchor_x / 100 * width,
            view_y / 100 * viewport.height - anchor_y / 100 * height,
            width,
            height
            )
    return boxes


def _place(
        caption: Caption,
        region_boxes: dict[str, Box],
        viewport: Viewport,
        line_height: float,
        measured: list[LineBox] | None
        ) -> _Placement:
    """Return the line boxes of a caption in the viewport."""
    settings = caption.settings
    region = settings.region
    area = region_boxes.get(region) if region else None
    if area is None:
        # a region that is not defined is rendered as the whole viewport
        region = None
        area = Box(0.0, 0.0, viewport.width, viewport.height)

    size = _percentage(settings.size, 100.0) / 100
    position, position_alignment = _alignment(settings.position, 'center')
    line, line_alignment = _alignment(settings.line, 'start')
    align = settings.align or 'center'
    vertical = settings.vertical

    if vertical not in ('rl', 'lr'):
        cue_width = size * area.width
        boxes = (measured if measured is not None else
                 [LineBox(cue_width, line_height)] * len(caption.lines))
        cue_height = sum(box.height for box in boxes)
        x = _inline_offset(
            position, position_alignment, cue_width, area.x, area.width
            )
        y = _block_offset(
            line, line_alignment, cue_height, area.y, area.height,
            line_height, default_line=-1
            )
        x = _clamp(x, area.x, area.width, cue_width)
        y = _clamp(y, area.y, area.height, cue_height)
        lines = []
        top = y
        for box in boxes:
            lines.append(Box(
                x + _text_offset(align, cue_width - box.width),
                top,
                box.width,
                box.height
                ))
            top += box.height
        return _Placement(region, lines)

    cue_height = size * area.height
    boxes = (measured if measured is not None else
             [LineBox(line_height, cue_height)] * len(caption.lines))
    cue_width = sum(box.width for box in boxes)
    y = _inline_offset(
        position, position_alignment, cue_height, area.y, area.height
        )
    x = _block_offset(
        line, line_alignment, cue_width, area.x, area.width, line_height,
        default_line=-1 if vertical == 'rl' else 0
        )
    x = _clamp(x, area.x, area.width, cue_width)
    y = _clamp(y, area.y, area.height, cue_height)
    lines = []
    if vertical == 'rl':
        # the columns of text are stacked from the right
        edge = x + cue_width
        for box in boxes:
            edge -= box.width
            lines.append(Box(
                edge,
                y + _text_offset(align, cue_height - box.height),
                box.width,
                box.height
                ))
    else:
        edge = x
        for box in boxes:
            lines.append(Box(
                edge,
                y + _text_offset(align, cue_height - box.height),
                box.width,
                box.height
                ))
            edge += box.width
    return _Placement(region, lines)


def _shared_area(first: _Placement, second: _Placement) -> Box | None:
    """
    Return the area the lines of two placements share, or None.

    The area is the smallest box that covers every overlap between a line
    of one placement and a line of the other.
    """
    overlaps = [
        overlap
        for line in first.lines
        for other in second.lines
        if (overlap := line.intersection(other)) is not None
        ]
    if not overlaps:
        return None
    x = min(overlap.x for overlap in overlaps)
    y = min(overlap.y for overlap in overlaps)
    return Box(
        _round(x),
        _round(y),
        _round(max(overlap.right for overlap in overlaps) - x),
        _round(max(overlap.bottom for overlap in overlaps) - y)
        )


def _round(value: float) -> float:
    """Return a coordinate rounded to a micron, for clean output."""
    return round(value, 6)


def _percentage(value: str | None, default: float) -> float:
    """Return a percentage setting as a number, or the default."""
    if value is None:
        return default
    return float(value[:-1])


def _anchor(
        value: str | None,
        default_x: float,
        default_y: float
        ) -> tuple[float, float]:
    """Return the two percentages of an anchor setting, or the defaults."""
    if value is None:
        return default_x, default_y
    x, _, y = value.partition(',')
    return float(x[:-1]), float(y[:-1])


def _alignment(setting: str | None, default: str) -> tuple[str | None, str]:
    """Return the value and the alignment of a setting, or the default."""
    if setting is None:
        return None, default
    value, separator, alignment = setting.partition(',')
    return value, alignment if separator else default


def _inline_offset(
        position: str | None,
        alignment: str,
        extent: float,
        area_start: float,
        area_size: float
        ) -> float:
    """
    Return the offset of the cue box along its inline axis.

    The position is a percentage of the rendering area, with the
    alignment choosing the point of the box it anchors: ``line-left``
    the start, ``center`` the middle and ``line-right`` the end.
    """
    reference = _percentage(position, 50.0) / 100 * area_size
    offset = {'line-left': 0.0,
              'center': -extent / 2,
              'line-right': -extent}[alignment]
    return area_start + reference + offset


def _block_offset(
        line: str | None,
        alignment: str,
        extent: float,
        area_start: float,
        area_size: float,
        line_height: float,
        default_line: int
        ) -> float:
    """
    Return the offset of the cue box along its block axis.

    A percentage and a line number are resolved to the same coordinates:
    the percentage of the rendering area, or the line number times the
    line height from its start, or from its end when negative.
    """
    if line is None:
        line = str(default_line)
    if line.endswith('%'):
        reference = _percentage(line, 0.0) / 100 * area_size
        offset = {'start': 0.0,
                  'center': -extent / 2,
                  'end': -extent}[alignment]
        return area_start + reference + offset
    number = int(line)
    if number >= 0:
        return area_start + number * line_height
    return area_start + area_size + (number + 1) * line_height - extent


def _clamp(
        offset: float,
        area_start: float,
        area_size: float,
        extent: float
        ) -> float:
    """Return the offset with the box kept inside the rendering area."""
    if extent >= area_size:
        return area_start
    return min(max(offset, area_start), area_start + area_size - extent)


def _text_offset(align: str, extra: float) -> float:
    """Return the offset of a line of text in the cue box for the align."""
    if align in ('end', 'right'):
        return extra
    if align == 'center':
        return extra / 2
    return 0.0
