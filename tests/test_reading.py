#!/usr/bin/env python
import io
import unittest

import pdfplumber
import pytest

from pdfplumber.reading import get_reading_order

WIDTH, HEIGHT = 612, 792


def build_pdf(pages):
    """Build a minimal single-font PDF from declarative page descriptions."""
    objects = []

    def add(body):
        objects.append(body)
        return len(objects)

    page_ids = []
    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page in pages:
        stream_parts = []
        for x0, top, x1, bottom in page.get("rects", []):
            low = page["height"] - bottom
            stream_parts.append(
                f"{x0} {low} {x1 - x0} {bottom - top} re S".encode()
            )
        for item in page.get("texts", []):
            size = item.get("size", 10)
            baseline = page["height"] - item["top"] - size
            stream_parts.append(
                f"BT /F1 {size} Tf {item['x']} {baseline} Td "
                f"({item['text']}) Tj ET".encode()
            )
        stream = b"\n".join(stream_parts)
        content_id = add(
            b"<< /Length %d >>\nstream\n" % len(stream)
            + stream
            + b"\nendstream"
        )
        page_id = add(
            f"<< /Type /Page /Parent PLACEHOLDER "
            f"/MediaBox [0 0 {page['width']} {page['height']}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        )
        page_ids.append(page_id)
    pages_id = add(
        ("<< /Type /Pages /Kids [" + " ".join(f"{pid} 0 R" for pid in page_ids)
         + f"] /Count {len(page_ids)} >>").encode()
    )
    catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    for pid in page_ids:
        objects[pid - 1] = objects[pid - 1].replace(
            b"PLACEHOLDER", f"{pages_id} 0 R".encode()
        )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_position = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_position}\n%%EOF\n".encode()
    )
    return out.getvalue()


FRAMED_REPORT = {
    "width": WIDTH,
    "height": HEIGHT,
    "rects": [
        (72, 60, 540, 110),     # spanning title band
        (72, 130, 296, 400),    # upper-left column
        (316, 130, 540, 400),   # upper-right column
        (72, 420, 540, 470),    # spanning centered subtitle
        (72, 490, 540, 720),    # lower full-width column
    ],
    "texts": [
        {"x": 250, "top": 75, "text": "Report Title"},
        {"x": 90, "top": 150, "text": "Left line one"},
        {"x": 90, "top": 180, "text": "Left line two"},
        {"x": 335, "top": 150, "text": "Right line one"},
        {"x": 335, "top": 180, "text": "Right line two"},
        {"x": 235, "top": 435, "text": "Spanning Subheading"},
        {"x": 90, "top": 520, "text": "Bottom paragraph one"},
        {"x": 90, "top": 560, "text": "Bottom paragraph two"},
    ],
}


class TestReadingOrder(unittest.TestCase):
    def open_page(self, page):
        pdf = pdfplumber.PDF(io.BytesIO(build_pdf([page])))
        return pdf, pdf.pages[0]

    def test_framed_report_order_and_boxes(self):
        pdf, page = self.open_page(FRAMED_REPORT)
        order = page.get_reading_order()

        assert [s.segment_id for s in order.segments] == [1, 2, 3, 4, 5]
        assert [s.layout for s in order.segments] == [
            "full_width",
            "column",
            "column",
            "full_width",
            "full_width",
        ]
        # Original frame bounding boxes are preserved verbatim.
        assert order.segments[1].bbox == (72, 130, 296, 400)
        assert order.segments[2].bbox == (316, 130, 540, 400)
        assert order.segments[4].bbox == (72, 490, 540, 720)
        # Every page character lands in exactly one segment.
        assert sum(s.char_count for s in order.segments) == len(page.chars)
        # Texts stay inside their local reading boundaries.
        assert "Report Title" in order.segments[0].text
        assert order.segments[1].text.startswith("Left line one")
        assert order.segments[2].text.startswith("Right line one")
        assert "Spanning Subheading" in order.segments[3].text
        assert order.segments[4].text.startswith("Bottom paragraph one")
        assert order.ambiguities == []
        pdf.close()

    def test_upper_right_column_precedes_lower_band(self):
        pdf, page = self.open_page(FRAMED_REPORT)
        order = page.get_reading_order()
        ids = {s.segment_id for s in order.segments}
        right_column = next(s for s in order.segments if s.layout == "column"
                            and s.bbox[0] > 300)
        lower_band = next(s for s in order.segments
                          if s.bbox[1] > 480 and s.layout == "full_width")
        assert right_column.segment_id < lower_band.segment_id
        # A page-global x sort gets this wrong; the reading order must not.
        naive = sorted(page.extract_words(), key=lambda w: (w["x0"], w["top"]))
        assert naive[0]["text"] != "Right"
        assert ids == set(range(1, 6))
        pdf.close()

    def test_spanning_subheading_is_a_local_boundary(self):
        pdf, page = self.open_page(FRAMED_REPORT)
        order = page.get_reading_order()
        subheading = order.segments[3]
        assert subheading.layout == "full_width"
        assert subheading.bbox == (72, 420, 540, 470)
        # It interrupts the flow: columns before, lower block after.
        assert order.segments[2].bbox[3] < subheading.bbox[1]
        assert subheading.bbox[3] < order.segments[4].bbox[1]
        pdf.close()

    def test_extract_text_and_dict(self):
        pdf, page = self.open_page(FRAMED_REPORT)
        order = page.get_reading_order()
        joined = order.extract_text()
        assert joined.index("Right line two") < joined.index(
            "Spanning Subheading"
        )
        assert joined.index("Spanning Subheading") < joined.index(
            "Bottom paragraph one"
        )
        custom = order.extract_text(separator=" | ")
        assert custom.count(" | ") == 4

        serialized = order.to_dict(include_chars=False)
        assert serialized["page_number"] == 1
        assert len(serialized["segments"]) == 5
        assert "chars" not in serialized["segments"][0]
        with_chars = order.to_dict()
        assert with_chars["segments"][1]["chars"] == order.segments[1].chars
        pdf.close()

    def test_ambiguous_geometry_is_flagged_not_guessed(self):
        ambiguous_page = {
            "width": WIDTH,
            "height": HEIGHT,
            "rects": [
                (72, 100, 300, 500),    # tall left frame
                (330, 470, 540, 540),   # short right frame, slight y overlap
            ],
            "texts": [
                {"x": 90, "top": 130, "text": "Tall left start"},
                {"x": 90, "top": 460, "text": "Tall left end"},
                {"x": 340, "top": 490, "text": "Short right note"},
            ],
        }
        pdf, page = self.open_page(ambiguous_page)
        order = page.get_reading_order()
        assert len(order.ambiguities) == 1
        ambiguity = order.ambiguities[0]
        assert ambiguity.index == 1
        assert ambiguity.segment_a == 1
        assert ambiguity.segment_b == 2
        assert ambiguity.candidates == [[1, 2], [2, 1]]
        assert "both" in ambiguity.reason
        # The default geometric order is still emitted, just flagged.
        assert [s.segment_id for s in order.segments] == [1, 2]
        pdf.close()

    def test_precedence_resolves_ambiguity(self):
        ambiguous_page = {
            "width": WIDTH,
            "height": HEIGHT,
            "rects": [
                (72, 100, 300, 500),
                (330, 470, 540, 540),
            ],
            "texts": [
                {"x": 90, "top": 130, "text": "Tall left start"},
                {"x": 340, "top": 490, "text": "Short right note"},
            ],
        }
        pdf, page = self.open_page(ambiguous_page)
        order = page.get_reading_order(precedence=[(2, 1)])
        assert [s.segment_id for s in order.segments] == [2, 1]
        # Segment ids stay stable; only their ordering changes.
        assert order.segments[0].text.startswith("Short right note")
        # The ambiguity now sits at the new adjacent position.
        assert order.ambiguities[0].index == 1
        pdf.close()

    def test_precedence_validation(self):
        pdf, page = self.open_page(FRAMED_REPORT)
        with pytest.raises(ValueError):
            page.get_reading_order(precedence=[(1, 99)])
        with pytest.raises(ValueError):
            page.get_reading_order(precedence=[(2, 2)])
        with pytest.raises(ValueError):
            page.get_reading_order(precedence=[(1, 2), (2, 1)])
        pdf.close()

    def test_explicit_frames(self):
        explicit_frames = [
            (72, 130, 296, 400),
            (316, 130, 540, 400),
            (72, 490, 540, 720),
        ]
        pdf, page = self.open_page(FRAMED_REPORT)
        order = page.get_reading_order(frames=explicit_frames)
        assert all(frame["source"] == "explicit" for frame in order.frames)
        assert [s.bbox for s in order.segments] == explicit_frames
        assert [s.layout for s in order.segments] == [
            "column",
            "column",
            "full_width",
        ]
        pdf.close()

    def test_outer_container_border_is_removed(self):
        bordered = {
            "width": WIDTH,
            "height": HEIGHT,
            "rects": [
                (50, 40, 562, 750),    # outer page border enclosing all
                (72, 100, 296, 400),
                (316, 100, 540, 400),
                (72, 430, 540, 700),
            ],
            "texts": [
                {"x": 90, "top": 130, "text": "Left"},
                {"x": 340, "top": 130, "text": "Right"},
                {"x": 90, "top": 460, "text": "Bottom"},
            ],
        }
        pdf, page = self.open_page(bordered)
        order = page.get_reading_order()
        assert len(order.frames) == 3
        assert (50, 40, 562, 750) not in [
            tuple(frame["bbox"]) for frame in order.frames
        ]
        assert [s.layout for s in order.segments] == [
            "column",
            "column",
            "full_width",
        ]
        pdf.close()

    def test_frameless_virtual_frames(self):
        frameless = {
            "width": WIDTH,
            "height": HEIGHT,
            "rects": [],
            "texts": [
                {"x": 150, "top": 70, "text": "Spanning Headline Across Page"},
                {"x": 80, "top": 140, "text": "Left column text alpha"},
                {"x": 80, "top": 170, "text": "Left column text beta"},
                {"x": 360, "top": 140, "text": "Right column alpha"},
                {"x": 360, "top": 170, "text": "Right column beta"},
                {"x": 120, "top": 330, "text": "Centered spanning subhead here"},
                {"x": 80, "top": 400, "text": "Full width lower block line one"},
                {"x": 80, "top": 430, "text": "Full width lower block line two"},
            ],
        }
        pdf, page = self.open_page(frameless)
        order = page.get_reading_order()
        assert all(frame["source"] == "virtual" for frame in order.frames)
        texts = [s.text for s in order.segments]
        assert texts[0].startswith("Spanning Headline")
        assert texts[1].startswith("Left column text alpha")
        assert texts[2].startswith("Right column alpha")
        assert texts[3].startswith("Centered spanning subhead")
        assert texts[4].startswith("Full width lower block")
        assert [s.layout for s in order.segments] == [
            "full_width",
            "column",
            "column",
            "full_width",
            "full_width",
        ]
        pdf.close()

    def test_column_geometry_varies_per_page(self):
        page_one = {
            "width": WIDTH,
            "height": HEIGHT,
            "rects": [
                (60, 90, 280, 380),
                (300, 90, 552, 380),
                (60, 400, 552, 700),
            ],
            "texts": [
                {"x": 80, "top": 120, "text": "Page one left"},
                {"x": 320, "top": 120, "text": "Page one right"},
                {"x": 80, "top": 430, "text": "Page one bottom"},
            ],
        }
        page_two = {
            "width": WIDTH,
            "height": HEIGHT,
            "rects": [
                (72, 110, 200, 600),    # narrow tall left column
                (240, 110, 540, 600),   # wide right column
                (72, 620, 540, 740),
            ],
            "texts": [
                {"x": 90, "top": 140, "text": "Page two left"},
                {"x": 260, "top": 140, "text": "Page two right"},
                {"x": 90, "top": 650, "text": "Page two bottom"},
            ],
        }
        pdf = pdfplumber.PDF(io.BytesIO(build_pdf([page_one, page_two])))
        order_one = pdf.pages[0].get_reading_order()
        order_two = pdf.pages[1].get_reading_order()

        assert [s.layout for s in order_one.segments] == [
            "column",
            "column",
            "full_width",
        ]
        assert order_one.segments[0].bbox == (60, 90, 280, 380)
        assert order_one.segments[1].bbox == (300, 90, 552, 380)

        assert [s.layout for s in order_two.segments] == [
            "column",
            "column",
            "full_width",
        ]
        assert order_two.segments[0].bbox == (72, 110, 200, 600)
        assert order_two.segments[1].bbox == (240, 110, 540, 600)
        for order in (order_one, order_two):
            right = next(s for s in order.segments if s.bbox[0] > 220
                         and s.layout == "column")
            bottom = order.segments[-1]
            assert right.segment_id < bottom.segment_id
        pdf.close()

    def test_frameless_single_column_is_not_split(self):
        single_column = {
            "width": WIDTH,
            "height": HEIGHT,
            "rects": [],
            "texts": [
                {"x": 80, "top": 100 + index * 20,
                 "text": f"single column paragraph line number {index} here"}
                for index in range(6)
            ],
        }
        pdf, page = self.open_page(single_column)
        order = page.get_reading_order()
        assert len(order.segments) == 1
        assert order.segments[0].layout == "full_width"
        assert order.ambiguities == []
        pdf.close()

    def test_empty_page_falls_back_to_single_boundary(self):
        blank = {"width": WIDTH, "height": HEIGHT, "rects": [], "texts": []}
        pdf, page = self.open_page(blank)
        order = page.get_reading_order()
        assert len(order.segments) == 1
        assert order.segments[0].layout == "full_width"
        assert order.segments[0].text == ""
        assert order.frames[0]["source"] == "page"
        pdf.close()

    def test_module_level_entry_point_matches_page_method(self):
        pdf, page = self.open_page(FRAMED_REPORT)
        direct = get_reading_order(page)
        via_method = page.get_reading_order()
        assert [s.bbox for s in direct.segments] == [
            s.bbox for s in via_method.segments
        ]
        pdf.close()
