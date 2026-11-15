from typing import Dict, Iterable, Optional, Tuple, Union

from ._typing import T_bbox, T_num, T_point

T_bbox_or_point = Union[T_bbox, T_point]

# The PDF coordinate system used by this module has its origin at the
# bottom-left of the page, matching the raw /MediaBox values.
ORIGINAL_SPACE = "original"
PDF_SPACE = "pdf"
RAW_SPACE = "raw"
PAGE_SPACE = "page"
DISPLAY_SPACE = "display"
MEDIABOX_SPACE = "mediabox"
MEDIA_SPACE = "media"


def _is_bbox(value: Iterable[T_num]) -> bool:
    return len(tuple(value)) == 4


class CoordinateTransform:
    """
    An invertible transform between page coordinates and the original
    (unrotated) PDF coordinate system.

    Page coordinates are the values exposed by ``pdfplumber`` on page and
    object dictionaries: ``x`` grows to the right, ``top`` grows downward,
    and the axes follow the page's ``/Rotate`` value. Original PDF
    coordinates are the raw bottom-left coordinates of the page's
    ``/MediaBox``; they are independent of ``/Rotate`` and retain any
    non-zero MediaBox origin.

    The transform also exposes a MediaBox-relative bottom-left space, whose
    origin is the MediaBox's lower-left corner.
    """

    def __init__(self, mediabox: T_bbox, rotation: T_num = 0):
        x0, y0, x1, y1 = (float(v) for v in mediabox)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        self.mediabox = (x0, y0, x1, y1)
        self.rotation = int(rotation) % 360
        if self.rotation not in (0, 90, 180, 270):
            raise ValueError("rotation must be a multiple of 90")

        # pdfplumber's page-space representation of the MediaBox. This
        # mirrors Page.mediabox, including its possibly non-zero top/left.
        if self.rotation in (90, 270):
            normalized = (y0, x0, y1, x1)
        else:
            normalized = (x0, y0, x1, y1)
        mb_height = normalized[3] - normalized[1]
        self.page_mediabox = (
            normalized[0],
            mb_height - normalized[3],
            normalized[2],
            mb_height - normalized[1],
        )
        self.page_x0 = self.page_mediabox[0]
        self.page_top = self.page_mediabox[1]
        self.width = self.page_mediabox[2] - self.page_mediabox[0]
        self.height = self.page_mediabox[3] - self.page_mediabox[1]
        self._cropbox: Optional[T_bbox] = None
        self._cropbox_pdf: Optional[T_bbox] = None

    @property
    def bbox(self) -> T_bbox:
        """The MediaBox in page space."""
        return self.page_mediabox

    @property
    def media_bbox(self) -> T_bbox:
        """The MediaBox in original PDF space."""
        return self.mediabox

    @property
    def mediabox_pdf(self) -> T_bbox:
        return self.mediabox

    @property
    def mediabox_relative(self) -> T_bbox:
        """The MediaBox in origin-normalized bottom-left coordinates."""
        x0, y0, x1, y1 = self.mediabox
        return (0, 0, x1 - x0, y1 - y0)

    @property
    def cropbox(self) -> Tuple[float, float, float, float]:
        """The CropBox in page space, if configured, else the MediaBox."""
        return self._cropbox if self._cropbox is not None else self.page_mediabox

    @property
    def cropbox_pdf(self) -> T_bbox:
        """The CropBox in original PDF coordinates, if configured."""
        return self._cropbox_pdf if self._cropbox_pdf is not None else self.mediabox

    def set_cropbox(self, cropbox: T_bbox, is_page_space: bool = True) -> None:
        """
        Record the page's CropBox so provenance can identify both the
        MediaBox and CropBox frames of reference.
        """
        x0, y0, x1, y1 = (float(v) for v in cropbox)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        cropbox = (x0, y0, x1, y1)
        if is_page_space:
            self._cropbox = cropbox
            self._cropbox_pdf = self.from_page_bbox(cropbox)
        else:
            self._cropbox_pdf = cropbox
            self._cropbox = self.to_page_bbox(cropbox)

    def prepare_bbox(
        self,
        bbox: T_bbox,
        space: Optional[str] = None,
        relative_to: Optional[str] = None,
    ) -> T_bbox:
        """
        Normalize a caller-supplied bbox to page space.

        ``space`` describes the bbox's coordinate system: ``page`` (default),
        ``original``/``pdf``/``raw``, or ``mediabox``/``media`` (bottom-left,
        MediaBox-relative), or ``cropbox`` (bottom-left, CropBox-relative).

        ``relative_to`` optionally shifts the bbox's origin before conversion;
        ``cropbox`` treats its coordinates as offsets from the CropBox's
        top-left corner (or its lower-left corner when combined with an
        explicit PDF ``space``).
        """
        if relative_to is not None:
            relative_to = relative_to.lower()
            if relative_to not in ("cropbox", "mediabox", "media"):
                raise ValueError(f"Unknown relative_to value: {relative_to}")
            if space is None or space.lower() in (PAGE_SPACE, DISPLAY_SPACE):
                # By default, offsets are measured from the reference box's
                # top-left corner, in page space (matching Page.crop's
                # ``relative=True`` behavior).
                ref = self.cropbox if relative_to == "cropbox" else self.page_mediabox
                return (
                    bbox[0] + ref[0],
                    bbox[1] + ref[1],
                    bbox[2] + ref[0],
                    bbox[3] + ref[1],
                )
            # With an explicit original/PDF space, offsets are measured from
            # the reference box's lower-left corner.
            ref = self.cropbox_pdf if relative_to == "cropbox" else self.mediabox
            bbox = (
                bbox[0] + ref[0],
                bbox[1] + ref[1],
                bbox[2] + ref[0],
                bbox[3] + ref[1],
            )
            space = ORIGINAL_SPACE

        if space is not None:
            space = space.lower()
            if space == "cropbox":
                cb = self.cropbox_pdf
                bbox = (
                    bbox[0] + cb[0],
                    bbox[1] + cb[1],
                    bbox[2] + cb[0],
                    bbox[3] + cb[1],
                )
                space = ORIGINAL_SPACE
            bbox = self.transform_bbox(
                bbox,
                from_space=space,
                to_space=PAGE_SPACE,
            )
        return bbox

    @property
    def size(self) -> T_point:
        return (self.width, self.height)

    def _to_display_point(self, point: T_point) -> T_point:
        """Map an original-PDF point to pdfminer's display coordinates."""
        x, y = point
        x0, y0, x1, y1 = self.mediabox
        if self.rotation == 90:
            return (y - y0, x1 - x)
        if self.rotation == 180:
            return (x1 - x, y1 - y)
        if self.rotation == 270:
            return (y1 - y, x - x0)
        return (x - x0, y - y0)

    def _from_display_point(self, point: T_point) -> T_point:
        """Map pdfminer display coordinates to original PDF coordinates."""
        x, y = point
        x0, y0, x1, y1 = self.mediabox
        if self.rotation == 90:
            return (x1 - y, x + y0)
        if self.rotation == 180:
            return (x1 - x, y1 - y)
        if self.rotation == 270:
            return (y + x0, y1 - x)
        return (x + x0, y + y0)

    def _display_to_page_point(self, point: T_point) -> T_point:
        x, y = point
        return (x + self.page_x0, self.height - y + self.page_top)

    def _page_to_display_point(self, point: T_point) -> T_point:
        x, top = point
        return (x - self.page_x0, self.height - (top - self.page_top))

    def to_page_point(self, point: T_point) -> T_point:
        """Map an original-PDF point to page space."""
        return self._display_to_page_point(self._to_display_point(point))

    def from_page_point(self, point: T_point) -> T_point:
        """Map a page-space point back to original PDF coordinates."""
        return self._from_display_point(self._page_to_display_point(point))

    def to_page_bbox(self, bbox: T_bbox) -> T_bbox:
        """
        Map an original-PDF bbox to page space.

        For rotated pages this maps all four corners and returns the
        axis-aligned bounding box of the result.
        """
        x0, y0, x1, y1 = bbox
        points = [
            self.to_page_point((x0, y0)),
            self.to_page_point((x0, y1)),
            self.to_page_point((x1, y0)),
            self.to_page_point((x1, y1)),
        ]
        xs, tops = zip(*points)
        return (min(xs), min(tops), max(xs), max(tops))

    def from_page_bbox(self, bbox: T_bbox) -> T_bbox:
        """Map a page-space bbox back to original PDF coordinates."""
        x0, top, x1, bottom = bbox
        points = [
            self.from_page_point((x0, top)),
            self.from_page_point((x0, bottom)),
            self.from_page_point((x1, top)),
            self.from_page_point((x1, bottom)),
        ]
        xs, ys = zip(*points)
        return (min(xs), min(ys), max(xs), max(ys))

    def to_mediabox_point(self, point: T_point) -> T_point:
        """Map an original-PDF point to MediaBox-relative bottom-left space."""
        return (point[0] - self.mediabox[0], point[1] - self.mediabox[1])

    def from_mediabox_point(self, point: T_point) -> T_point:
        """Map a MediaBox-relative bottom-left point to original PDF space."""
        return (point[0] + self.mediabox[0], point[1] + self.mediabox[1])

    def to_mediabox_bbox(self, bbox: T_bbox) -> T_bbox:
        x0, y0, x1, y1 = bbox
        return (
            x0 - self.mediabox[0],
            y0 - self.mediabox[1],
            x1 - self.mediabox[0],
            y1 - self.mediabox[1],
        )

    def from_mediabox_bbox(self, bbox: T_bbox) -> T_bbox:
        x0, y0, x1, y1 = bbox
        return (
            x0 + self.mediabox[0],
            y0 + self.mediabox[1],
            x1 + self.mediabox[0],
            y1 + self.mediabox[1],
        )

    def convert(
        self,
        value: Union[T_bbox, T_point],
        from_space: str = PAGE_SPACE,
        to_space: str = ORIGINAL_SPACE,
    ) -> Union[T_bbox, T_point]:
        """
        Convert a bbox or point between named coordinate spaces.

        Supported spaces: ``page``/``display``, ``original``/``pdf``/``raw``,
        and ``mediabox``/``media`` (bottom-left, MediaBox-relative).
        """
        from_space = from_space.lower()
        to_space = to_space.lower()
        is_bbox = _is_bbox(value)

        if from_space == to_space:
            return tuple(value)  # type: ignore

        page_spaces = (PAGE_SPACE, DISPLAY_SPACE)
        original_spaces = (ORIGINAL_SPACE, PDF_SPACE, RAW_SPACE)
        media_spaces = (MEDIABOX_SPACE, MEDIA_SPACE)

        intermediate: Union[T_bbox, T_point]
        if from_space in page_spaces:
            if is_bbox:
                intermediate = self.from_page_bbox(value)  # type: ignore
            else:
                intermediate = self.from_page_point(value)  # type: ignore
        elif from_space in original_spaces:
            intermediate = tuple(value)  # type: ignore
        elif from_space in media_spaces:
            if is_bbox:
                intermediate = self.from_mediabox_bbox(value)  # type: ignore
            else:
                intermediate = self.from_mediabox_point(value)  # type: ignore
        else:
            raise ValueError(f"Unknown coordinate space: {from_space}")

        if to_space in page_spaces:
            if is_bbox:
                return self.to_page_bbox(intermediate)
            return self.to_page_point(intermediate)
        elif to_space in original_spaces:
            return intermediate
        elif to_space in media_spaces:
            if is_bbox:
                return self.to_mediabox_bbox(intermediate)
            return self.to_mediabox_point(intermediate)
        else:
            raise ValueError(f"Unknown coordinate space: {to_space}")

    def transform_bbox(
        self,
        bbox: T_bbox,
        from_space: str = PAGE_SPACE,
        to_space: str = ORIGINAL_SPACE,
    ) -> T_bbox:
        return self.convert(  # type: ignore
            bbox, from_space=from_space, to_space=to_space
        )

    def transform_point(
        self,
        point: T_point,
        from_space: str = PAGE_SPACE,
        to_space: str = ORIGINAL_SPACE,
    ) -> T_point:
        return self.convert(  # type: ignore
            point, from_space=from_space, to_space=to_space
        )

    def describe(self) -> Dict[str, Union[T_bbox, T_num]]:
        return {
            "mediabox": self.mediabox,
            "page_mediabox": self.page_mediabox,
            "rotation": self.rotation,
            "width": self.width,
            "height": self.height,
        }

    # Explicit aliases, for callers that prefer different terminology.
    to_original_point = from_page_point
    to_original_bbox = from_page_bbox
    from_original_point = to_page_point
    from_original_bbox = to_page_bbox
    to_pdf_point = from_page_point
    to_pdf_bbox = from_page_bbox
    from_pdf_point = to_page_point
    from_pdf_bbox = to_page_bbox
    to_raw_point = from_page_point
    to_raw_bbox = from_page_bbox
    from_raw_point = to_page_point
    from_raw_bbox = to_page_bbox
    to_source_point = from_page_point
    to_source_bbox = from_page_bbox
    from_source_point = to_page_point
    from_source_bbox = to_page_bbox
    to_media_point = to_mediabox_point
    to_media_bbox = to_mediabox_bbox
    from_media_point = from_mediabox_point
    from_media_bbox = from_mediabox_bbox

    def __call__(self, value: Union[T_bbox, T_point]) -> Union[T_bbox, T_point]:
        if _is_bbox(value):
            return self.to_page_bbox(value)  # type: ignore
        return self.to_page_point(value)  # type: ignore

    def __repr__(self) -> str:  # pragma: nocover
        return (
            f"<CoordinateTransform mediabox={self.mediabox} rotation={self.rotation}>"
        )


# Descriptive aliases.
PageCoordinates = CoordinateTransform
CoordinateSystem = CoordinateTransform
