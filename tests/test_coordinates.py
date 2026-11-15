import os

import pytest

import pdfplumber
from pdfplumber.coords import CoordinateTransform
from pdfplumber.utils import get_bbox_overlap

HERE = os.path.abspath(os.path.dirname(__file__))


def _page(path):
    pdf = pdfplumber.open(os.path.join(HERE, "pdfs", path))
    return pdf, pdf.pages[0]


class TestCoordinateTransform:
    def test_identity(self):
        coords = CoordinateTransform((0, 0, 200, 300))
        assert coords.to_page_bbox((10, 20, 30, 40)) == (10, 260, 30, 280)
        assert coords.from_page_bbox((10, 260, 30, 280)) == (10, 20, 30, 40)
        assert coords.to_page_point((10, 20)) == (10, 280)
        assert coords.from_page_point((10, 280)) == (10, 20)

    def test_nonzero_origin(self):
        coords = CoordinateTransform((100, 200, 400, 700))
        page_bbox = coords.to_page_bbox((150, 300, 250, 400))
        assert page_bbox == (150, 100, 250, 200)
        assert coords.from_page_bbox(page_bbox) == (150, 300, 250, 400)

    @pytest.mark.parametrize("rotation", [0, 90, 180, 270])
    def test_all_rotations_round_trip(self, rotation):
        mediabox = (100, 200, 400, 700)
        coords = CoordinateTransform(mediabox, rotation)
        for bbox in [
            (120, 220, 180, 280),
            (100, 200, 400, 700),
            (150, 300, 250, 400),
        ]:
            page_bbox = coords.to_page_bbox(bbox)
            assert coords.from_page_bbox(page_bbox) == bbox

    def test_rotation_corner_mapping(self):
        coords = CoordinateTransform((0, 0, 100, 200), 90)
        assert coords.to_page_point((0, 0)) == (0, 0)
        assert coords.to_page_point((100, 0)) == (0, 100)
        assert coords.to_page_point((100, 200)) == (200, 100)
        assert coords.to_page_point((0, 200)) == (200, 0)

    def test_media_and_cropbox_spaces(self):
        coords = CoordinateTransform((100, 200, 400, 700))
        coords.set_cropbox((120, 240, 380, 660), is_page_space=False)
        assert coords.mediabox_relative == (0, 0, 300, 500)
        assert coords.to_mediabox_bbox((150, 350, 250, 450)) == (50, 150, 150, 250)
        assert coords.cropbox_pdf == (120, 240, 380, 660)
        assert coords.to_page_bbox(coords.cropbox_pdf) == (120, -160, 380, 260)

    def test_convert(self):
        coords = CoordinateTransform((100, 200, 400, 700))
        assert coords.convert((150, 100, 250, 200), "page", "original") == (
            150,
            300,
            250,
            400,
        )
        assert coords.convert((0, 0, 300, 500), "mediabox", "original") == (
            100,
            200,
            400,
            700,
        )
        assert coords.convert((0, 0), "mediabox", "original") == (100, 200)
        assert coords.convert(coords.page_mediabox, "page", "mediabox") == (
            0,
            0,
            300,
            500,
        )
        assert (
            coords.convert(coords.page_mediabox, "page", "original") == coords.mediabox
        )


class TestHalfOpen:
    def test_object_touching_far_edge(self):
        bbox = (0, 0, 20, 20)
        obj = (20, 0, 40, 20)
        assert get_bbox_overlap(obj, bbox, half_open=False) is not None
        assert get_bbox_overlap(obj, bbox, half_open=True) is None

    def test_object_on_origin_edge(self):
        bbox = (0, 0, 20, 20)
        assert get_bbox_overlap((0, 0, 20, 20), bbox, half_open=True) == (
            0,
            0,
            20,
            20,
        )

    def test_zero_width_line(self):
        bbox = (0, 0, 20, 20)
        assert get_bbox_overlap((20, 5, 20, 15), bbox, half_open=True) is None
        assert get_bbox_overlap((10, 5, 10, 15), bbox, half_open=True) == (
            10,
            5,
            10,
            15,
        )


class TestPageProvenance:
    def test_nonzero_mediabox_page(self):
        pdf, page = _page("issue-1181.pdf")
        try:
            char = page.chars[0]
            bbox = (char["x0"], char["top"], char["x1"], char["bottom"])
            assert page.to_original_bbox(bbox) == (
                char["x0"],
                page.height - char["bottom"],
                char["x1"],
                page.height - char["top"],
            )
            assert page.to_original_bbox(bbox)[1] == char["y0"] + page.mediabox_raw[1]
            assert page.to_original_bbox(bbox)[3] == char["y1"] + page.mediabox_raw[1]
        finally:
            pdf.close()

    def test_nonzero_page_crop_is_invertible(self):
        pdf, page = _page("issue-1181.pdf")
        try:
            cropped = page.crop(page.bbox)
            assert cropped.chars
            char = cropped.chars[0]
            assert char["source_pdf_bbox"] == page.coords.to_original_bbox(
                (char["x0"], char["top"], char["x1"], char["bottom"])
            )
        finally:
            pdf.close()

    @pytest.mark.parametrize(
        "filename,rotation",
        [
            ("nics-background-checks-2015-11-rotated.pdf", 90),
            ("annotations-rotated-90.pdf", 90),
        ],
    )
    def test_rotated_page_round_trip(self, filename, rotation):
        pdf, page = _page(filename)
        try:
            assert page.rotation == rotation
            for char in page.chars[:5]:
                bbox = (char["x0"], char["top"], char["x1"], char["bottom"])
                pdf_bbox = page.to_original_bbox(bbox)
                assert page.from_original_bbox(pdf_bbox) == bbox
        finally:
            pdf.close()

    def test_cropbox_frame(self):
        pdf, page = _page("page-boxes-example.pdf")
        try:
            assert page.cropbox != page.mediabox
            assert page.coords.cropbox == page.cropbox
            assert page.coords.cropbox_pdf == pytest.approx(
                (14.17323, 14.17323, 581.10236, 827.71654)
            )
            cbox = page.crop((0, 0, 100, 100), relative_to="cropbox")
            assert cbox.bbox == (
                14.17323,
                42.519679999999994,
                114.17323,
                142.51968,
            )
        finally:
            pdf.close()


class TestClippedGlyphs:
    def test_half_clipped_glyph_records_both_boxes(self):
        pdf, page = _page("scotus-transcript-p1.pdf")
        try:
            cropped = page.crop((90, 70, page.width, 300))
            clipped = [c for c in cropped.chars if c["clipped"]]
            assert clipped
            char = clipped[0]
            assert char["source_bbox"] != char["visible_bbox"]
            assert char["source_bbox"][1] < char["visible_bbox"][1]
            assert char["visible_bbox"][1] == 70
            assert char["original_bbox"] == char["source_bbox"]
            assert char["original_visible_bbox"] == char["visible_bbox"]
            assert char["visible_pdf_bbox"][0] == char["source_pdf_bbox"][0]
            assert char["visible_pdf_bbox"][3] < char["source_pdf_bbox"][3]
            assert char["visible_pdf_bbox"][1] == char["source_pdf_bbox"][1]
        finally:
            pdf.close()

    def test_half_clipped_glyph_identical_under_all_crop_orders(self):
        pdf, page = _page("scotus-transcript-p1.pdf")
        try:
            a = (90, 60, 612, 350)
            b = (90, 70, 612, 300)
            single = page.crop(b)
            chained = page.crop(a).crop(b)
            clipped_single = [c for c in single.chars if c["clipped"]]
            clipped_chained = [c for c in chained.chars if c["clipped"]]
            assert len(clipped_single) == len(clipped_chained)
            for one, two in zip(clipped_single, clipped_chained):
                for key in (
                    "source_bbox",
                    "visible_bbox",
                    "source_pdf_bbox",
                    "visible_pdf_bbox",
                ):
                    assert one[key] == two[key]
        finally:
            pdf.close()

    def test_unclipped_glyph_boxes_match(self):
        pdf, page = _page("scotus-transcript-p1.pdf")
        try:
            cropped = page.crop((90, 70, page.width, 300))
            unclipped = [c for c in cropped.chars if not c["clipped"]]
            assert unclipped
            char = unclipped[0]
            assert char["source_bbox"] == char["visible_bbox"]
            assert char["source_pdf_bbox"] == char["visible_pdf_bbox"]
        finally:
            pdf.close()


class TestCropOrder:
    def test_two_crop_orders_select_same_glyphs(self):
        pdf, page = _page("scotus-transcript-p1.pdf")
        try:
            a = (90, 70, 612, 350)
            b = (100, 100, 600, 300)
            ab = page.crop(a).crop(b)
            ba = page.crop(b).crop(a, strict=False)
            single = page.crop(b)

            def signature(chars):
                return [
                    (
                        c["text"],
                        tuple(round(v, 6) for v in c["source_bbox"]),
                        tuple(round(v, 6) for v in c["visible_bbox"]),
                        tuple(round(v, 6) for v in c["original_bbox"]),
                        tuple(round(v, 6) for v in c["visible_original_bbox"]),
                    )
                    for c in chars
                ]

            assert signature(ab.chars) == signature(ba.chars) == signature(single.chars)
        finally:
            pdf.close()

    def test_half_open_edges_across_chained_crops(self):
        pdf, page = _page("scotus-transcript-p1.pdf")
        try:
            cropped = page.crop((90, 70, 612, 300))
            far_edges = [
                c
                for c in cropped.chars
                if c["source_bbox"][2] == 612 or c["source_bbox"][3] == 300
            ]
            assert far_edges == []
        finally:
            pdf.close()

    def test_closed_edges_retains_touching_objects(self):
        pdf, page = _page("scotus-transcript-p1.pdf")
        try:
            half_open = page.crop((90, 70, 612, 300))
            closed = page.crop((90, 70, 612, 300), half_open=False)
            assert len(closed.chars) >= len(half_open.chars)
        finally:
            pdf.close()

    def test_chained_crop_keeps_both_crop_boxes(self):
        pdf, page = _page("scotus-transcript-p1.pdf")
        try:
            a = (90, 70, 612, 350)
            b = (100, 100, 600, 300)
            ab = page.crop(a).crop(b)
            char = ab.chars[0]
            assert char["crop_bboxes"] == [a, b]
        finally:
            pdf.close()

    def test_cropbox_relative_crop_after_cropbox_crop(self):
        pdf, page = _page("page-boxes-example.pdf")
        try:
            a = page.crop(page.cropbox)
            b = a.crop((0, 0, 200, 200), relative_to="cropbox")
            assert b.bbox[0] == pytest.approx(page.cropbox[0])
            assert b.bbox[1] == pytest.approx(page.cropbox[1])
            assert b.bbox[2] == pytest.approx(page.cropbox[0] + 200)
            assert b.bbox[3] == pytest.approx(page.cropbox[1] + 200)

            c = page.crop((0, 0, 200, 200), space="cropbox")
            assert c.bbox[0] == pytest.approx(page.cropbox[0])
            assert c.bbox[2] == pytest.approx(page.cropbox[0] + 200)
            assert c.bbox[1] == pytest.approx(page.cropbox[3] - 200)
            assert c.bbox[3] == pytest.approx(page.cropbox[3])
        finally:
            pdf.close()

    def test_original_and_mediabox_spaces_match_page_space(self):
        pdf, page = _page("issue-1181.pdf")
        try:
            expected = page.crop(page.bbox)
            in_original = page.crop(page.mediabox_raw, space="original")
            in_media = page.crop(page.coords.mediabox_relative, space="mediabox")
            for other in (in_original, in_media):
                assert other.bbox == pytest.approx(expected.bbox)
                assert [c["text"] for c in other.chars] == [
                    c["text"] for c in expected.chars
                ]
        finally:
            pdf.close()
