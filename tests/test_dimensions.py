#!/usr/bin/env python
import math

from pdfplumber.dimensions import (
    DimensionIndex,
    build_dimension_index,
    dimension_pairs,
    parse_dimension,
    segment_angle,
    segment_endpoints,
    text_angle,
)


def rotate_point(point, degrees, origin=(0.0, 0.0)):
    radians = math.radians(degrees)
    cos, sin = math.cos(radians), math.sin(radians)
    x, y = point[0] - origin[0], point[1] - origin[1]
    return (x * cos - y * sin + origin[0], x * sin + y * cos + origin[1])


def rotate_line(line, degrees):
    p0, p1 = segment_endpoints(line)
    return {"p0": rotate_point(p0, degrees), "p1": rotate_point(p1, degrees)}


def rotate_text(text, degrees):
    text = dict(text)
    corners = text.get("corners")
    if corners is not None:
        text["corners"] = [rotate_point(point, degrees) for point in corners]
    elif "bbox" in text:
        x0, y0, x1, y1 = text["bbox"]
        text["corners"] = [
            rotate_point(point, degrees)
            for point in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        ]
        del text["bbox"]
    if "anchor" in text:
        text["anchor"] = rotate_point(text["anchor"], degrees)
    if "angle" in text:
        text["angle"] = (text["angle"] + degrees) % 180
    return text


def horizontal_line(x0, x1, y):
    return {"p0": (x0, y), "p1": (x1, y)}


def text_above(text, center_x, line_y, half_width=10, gap=1, height=6):
    return {
        "text": text,
        "bbox": (
            center_x - half_width,
            line_y - gap - height,
            center_x + half_width,
            line_y - gap,
        ),
        "angle": 0,
    }


class TestParsing:
    def test_units_and_values(self):
        assert parse_dimension("200 mm") == (200.0, "mm")
        assert parse_dimension("12.5cm") == (12.5, "cm")
        assert parse_dimension("1 1/2 in") == (1.5, "in")
        assert parse_dimension("Ø50") == (50.0, "")
        assert parse_dimension("45°") == (45.0, "deg")
        assert parse_dimension("R 7,5 mm") == (7.5, "mm")
        assert parse_dimension("100 ±0.2 mm") == (100.0, "mm")
        assert parse_dimension("100 ±0.2 mm").tolerance == 0.2
        assert parse_dimension("100 +0.1/-0.2 mm") == (100.0, "mm")
        assert parse_dimension("note").value is None

    def test_text_rotation_and_mirroring(self):
        assert segment_endpoints({"x0": 0, "top": 5, "x1": 10, "bottom": 5}) == (
            (0.0, 5.0),
            (10.0, 5.0),
        )
        assert segment_angle(((0, 0), (10, 10))) == 45.0
        assert text_angle({"angle": 45}) == 45.0
        assert text_angle({"angle": 45, "mirrored": True}) == 135.0
        assert text_angle({"matrix": (0, -2, 2, 0, 0, 0)}) == 90.0
        assert text_angle({"matrix": (-1, 0, 0, 1, 0, 0)}) == 0.0


class TestMatching:
    def test_matches_parallel_dimension_line(self):
        lines = [horizontal_line(0, 100, 10)]
        texts = [text_above("100 mm", 50, 10)]
        records = build_dimension_index(
            lines, texts, angle_tolerance=1, distance_tolerance=3
        )
        assert len(records) == 1
        record = records[0]
        assert record["value"] == 100.0
        assert record["unit"] == "mm"
        assert record["line"] is lines[0]
        assert record["line_index"] == 0
        assert record["projection_overlap"] == 1.0

    def test_angle_tolerance_is_respected(self):
        lines = [{"p0": (0, 0), "p1": (100, 10)}]
        texts = [{"text": "100 mm", "bbox": (40, -5, 60, 5), "angle": 0}]
        assert build_dimension_index(lines, texts, angle_tolerance=5) == []
        assert len(build_dimension_index(lines, texts, angle_tolerance=6)) == 1

    def test_distance_tolerance_is_respected(self):
        line = horizontal_line(0, 100, 10)
        text = {
            "text": "100 mm",
            "bbox": (40, 2, 60, 4),
            "anchor": (50, 3),
            "angle": 0,
        }
        assert build_dimension_index([line], [text], distance_tolerance=3) == []
        assert len(build_dimension_index([line], [text], distance_tolerance=6)) == 1

    def test_projection_must_meet_endpoint_range(self):
        line = horizontal_line(0, 100, 10)
        centered = text_above("100 mm", 100, 10)
        beyond = text_above("100 mm", 120, 10)
        assert len(build_dimension_index([line], [centered], distance_tolerance=3)) == 1
        assert build_dimension_index([line], [beyond], distance_tolerance=3) == []
        assert (
            len(build_dimension_index([line], [beyond], distance_tolerance=21)) == 1
        )

    def test_text_center_must_stay_within_endpoint_range(self):
        line = horizontal_line(0, 100, 10)
        overlapping_but_centered_past = {
            "text": "100 mm",
            "bbox": (95, 3, 115, 9),
            "angle": 0,
        }
        assert (
            build_dimension_index(
                [line], [overlapping_but_centered_past], distance_tolerance=3
            )
            == []
        )
        assert (
            len(
                build_dimension_index(
                    [line], [overlapping_but_centered_past], distance_tolerance=6
                )
            )
            == 1
        )

    def test_text_may_be_rotated_or_mirrored(self):
        rising = {"p0": (0, 0), "p1": (100, 100)}
        falling = {"p0": (0, 100), "p1": (100, 0)}
        rotated = {"text": "100 mm", "anchor": (50, 50), "angle": 45}
        mirrored = {**rotated, "mirrored": True}

        def index(*args):
            return build_dimension_index(*args, distance_tolerance=1)

        assert len(index([rising], [rotated])) == 1
        assert len(index([rising], [mirrored])) == 0
        assert len(index([falling], [mirrored])) == 1

    def test_parallel_lines_with_equal_evidence_both_survive(self):
        lines = [horizontal_line(0, 100, 10), horizontal_line(0, 100, 10)]
        texts = [text_above("100 mm", 50, 10)]
        records = build_dimension_index([lines[0], lines[1]], texts)
        assert len(records) == 2
        assert [record["line_index"] for record in records] == [0, 1]
        assert all(record["group_size"] == 2 for record in records)
        assert records[0]["line"] is lines[0]
        assert records[1]["line"] is lines[1]

    def test_whole_page_rotation_preserves_values_units_and_objects(self):
        lines = [horizontal_line(0, 100, 10), horizontal_line(0, 100, 10)]
        texts = [text_above("100 mm", 50, 10)]
        before = build_dimension_index(lines, texts)

        degrees = 37
        rotated_lines = [rotate_line(line, degrees) for line in lines]
        rotated_texts = [rotate_text(text, degrees) for text in texts]
        after = build_dimension_index(rotated_lines, rotated_texts)

        assert len(after) == len(before) == 2
        for old, new in zip(before, after):
            assert (new["value"], new["unit"]) == (old["value"], old["unit"])
            assert new["line"] is rotated_lines[old["line_index"]]
            assert new["line_index"] == old["line_index"]
            assert new["group"] == old["group"]

    def test_rotated_and_mirrored_text_keep_page_axis(self):
        lines = [
            horizontal_line(0, 120, 100),
            {"p0": (300, 0), "p1": (300, 80)},
        ]
        texts = [
            {"text": "120 mm", "bbox": (50, 90, 70, 98), "angle": 0},
            {"text": "80 mm", "bbox": (290, 30, 298, 50), "angle": 90},
        ]
        base = build_dimension_index(lines, texts)
        assert [(r["text_index"], r["line_index"]) for r in base] == [(0, 0), (1, 1)]

        for degrees in (13, 45, 77, 135, 222, 301):
            rotated_lines = [rotate_line(line, degrees) for line in lines]
            rotated_texts = [rotate_text(text, degrees) for text in texts]
            rotated = build_dimension_index(rotated_lines, rotated_texts)
            assert [(r["text_index"], r["line_index"]) for r in rotated] == [
                (0, 0),
                (1, 1),
            ]
            assert [(r["value"], r["unit"]) for r in rotated] == [
                (r["value"], r["unit"]) for r in base
            ]

    def test_endpoint_range_uses_text_center(self):
        line = horizontal_line(0, 100, 10)
        straddling_end = {"text": "100 mm", "bbox": (95, 3, 111, 9), "angle": 0}
        beyond = {"text": "100 mm", "bbox": (112, 3, 128, 9), "angle": 0}
        assert (
            len(build_dimension_index([line], [straddling_end], distance_tolerance=6))
            == 1
        )
        assert build_dimension_index([line], [beyond], distance_tolerance=6) == []

    def test_mapping_and_pair_helpers(self):
        line = horizontal_line(0, 100, 10)
        text = text_above("100 mm", 50, 10)
        records = build_dimension_index({"segments": [line], "annotations": [text]})
        assert len(records) == 1
        assert dimension_pairs([line], [text]) == [("100 mm", line)]
        index = DimensionIndex([line], [text])
        assert len(index) == 1 and index[0]["value"] == 100.0
        assert index.to_pairs() == [("100 mm", line)]
        assert index.groups()[0][0]["line"] is line

    def test_page_like_object_input(self):
        class FakePage:
            lines = [horizontal_line(0, 100, 10)]

            def extract_text_lines(self, return_chars=True):
                return [
                    {
                        "text": "100 mm",
                        "x0": 40,
                        "x1": 60,
                        "top": 3,
                        "bottom": 9,
                        "chars": [{"matrix": (2, 0, 0, 2, 40, 9)}],
                    }
                ]

        assert len(build_dimension_index(FakePage())) == 1
        assert len(build_dimension_index(page=FakePage())) == 1

    def test_tuple_text_input(self):
        line = horizontal_line(0, 100, 10)
        records = build_dimension_index(
            [line], [(50, 6, "100 mm", 0)], distance_tolerance=5
        )
        assert len(records) == 1
        assert records[0]["value"] == 100.0

    def test_invalid_line_entries_are_skipped(self):
        line = horizontal_line(0, 100, 10)
        text = text_above("100 mm", 50, 10)
        records = build_dimension_index([line, {"p0": (1, 1)}, None], [text])
        assert len(records) == 1
        assert records[0]["line_index"] == 0
        assert records[0]["line"] is line

    def test_unmatched_text_is_optional(self):
        lines = [horizontal_line(0, 100, 100)]
        texts = [text_above("100 mm", 50, 10)]
        assert build_dimension_index(lines, texts) == []
        records = build_dimension_index(lines, texts, include_unmatched=True)
        assert len(records) == 1
        assert records[0]["matched"] is False
        assert records[0]["line"] is None

    def test_text_touching_line_from_either_side_matches(self):
        line = horizontal_line(0, 100, 100)
        above = {"text": "100 mm", "bbox": (40, 92, 60, 99), "angle": 0}
        below = {"text": "100 mm", "bbox": (40, 101, 60, 108), "angle": 0}
        straddling = {"text": "100 mm", "bbox": (40, 96, 60, 104), "angle": 0}
        far_above = {"text": "100 mm", "bbox": (40, 80, 60, 90), "angle": 0}
        for text in (above, below, straddling):
            assert len(build_dimension_index([line], [text], distance_tolerance=1)) == 1
        assert build_dimension_index([line], [far_above], distance_tolerance=1) == []

    def test_rotated_axis_aligned_text_box(self):
        line = {"p0": (0, 0), "p1": (100, 100)}
        text = {"text": "100 mm", "bbox": (45, 45, 55, 55), "angle": 45}
        records = build_dimension_index([line], [text], distance_tolerance=5)
        assert len(records) == 1
        assert records[0]["line_endpoints"] == ((0.0, 0.0), (100.0, 100.0))
