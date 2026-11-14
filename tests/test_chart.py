#!/usr/bin/env python
import logging
import os
import sys
import unittest

import pdfplumber

from pdfplumber.chart import (
    AmbiguousSegment,
    AxisTick,
    DataPoint,
    LinearScale,
    colors_match,
    dash_arrays_match,
    dash_signature,
    detect_axis_ticks,
    detect_frame,
    extract_chart,
    split_subpaths,
)

logging.disable(logging.ERROR)

HERE = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(HERE, "pdfs"))

import make_annual_report_chart as fixture  # noqa: E402

FIXTURE_PATH = os.path.join(HERE, "pdfs", "annual-report-chart.pdf")
BLUE = fixture.BLUE


class TestFixture(unittest.TestCase):
    def test_generator_matches_committed_pdf(self):
        with open(FIXTURE_PATH, "rb") as fh:
            committed = fh.read()
        self.assertEqual(committed, fixture.build_pdf())

    def test_pdf_is_deterministic(self):
        self.assertEqual(fixture.build_pdf(), fixture.build_pdf())

    def test_fixture_opens_with_one_page(self):
        with pdfplumber.open(FIXTURE_PATH) as pdf:
            self.assertEqual(len(pdf.pages), 1)


class TestChartExtraction(unittest.TestCase):
    @classmethod
    def setup_class(cls):
        cls.pdf = pdfplumber.open(FIXTURE_PATH)
        cls.page = cls.pdf.pages[0]
        cls.result = extract_chart(cls.page, BLUE)

    @classmethod
    def teardown_class(cls):
        cls.pdf.close()

    def test_legend_maps_dash_arrays_to_names(self):
        legend = {(entry.dash_array, entry.name) for entry in self.result.legend}
        self.assertEqual(
            legend,
            {
                (tuple(fixture.DOTTED), fixture.SERIES_REVENUE),
                (tuple(fixture.LONG_DASH), fixture.SERIES_PROFIT),
            },
        )

    def test_legend_phase_differs_from_plot_but_still_matches(self):
        legend_phase = next(
            entry.phase
            for entry in self.result.legend
            if entry.name == fixture.SERIES_PROFIT
        )
        plot_phases = {
            point.source["dash"][1]
            for point in self.result.series_points[fixture.SERIES_PROFIT]
        }
        self.assertEqual(legend_phase, fixture.LEGEND_LONG_DASH_PHASE)
        self.assertNotIn(legend_phase, plot_phases)
        self.assertEqual(
            plot_phases,
            {
                fixture.PLOT_LONG_DASH_PHASE,
                fixture.PLOT_LONG_DASH_RESUME_PHASE,
            },
        )

    def test_dotted_resume_also_uses_a_different_phase(self):
        phases = {
            point.source["dash"][1]
            for point in self.result.series_points[fixture.SERIES_REVENUE]
        }
        self.assertEqual(
            phases,
            {
                fixture.PLOT_DOTTED_PHASE,
                fixture.PLOT_DOTTED_RESUME_PHASE,
            },
        )
        legend_phase = next(
            entry.phase
            for entry in self.result.legend
            if entry.name == fixture.SERIES_REVENUE
        )
        self.assertEqual(legend_phase, fixture.PLOT_DOTTED_PHASE)

    def test_axis_scales_recover_tick_values(self):
        self.assertEqual(
            [tick.value for tick in self.result.x_scale.ticks],
            [float(year) for year in fixture.YEARS],
        )
        self.assertEqual(
            [tick.value for tick in self.result.y_scale.ticks],
            [float(value) for value in reversed(fixture.Y_TICK_VALUES)],
        )
        first = self.result.y_scale.ticks[-1]
        self.assertAlmostEqual(self.result.y_scale.to_data(first.coord), 0.0)

    def test_named_revenue_points_match_ground_truth(self):
        expected = {
            year: value
            for year, value in fixture.REVENUE_SAMPLES.items()
            if value is not None and year not in fixture.AMBIGUOUS_YEARS
        }
        actual = {
            int(point.x_value): point.y_value
            for point in self.result.series_points[fixture.SERIES_REVENUE]
        }
        self.assertEqual(actual, expected)
        for point in self.result.series_points[fixture.SERIES_REVENUE]:
            self.assertEqual(point.status, "named")
            self.assertIsNone(point.ambiguity_reason)

    def test_named_profit_points_match_ground_truth(self):
        expected = {
            year: value
            for year, value in fixture.PROFIT_SAMPLES.items()
            if value is not None and year not in fixture.AMBIGUOUS_YEARS
        }
        actual = {
            int(point.x_value): point.y_value
            for point in self.result.series_points[fixture.SERIES_PROFIT]
        }
        self.assertEqual(actual, expected)

    def test_gaps_are_reported_but_never_connected(self):
        # One genuine internal hole per series.
        self.assertEqual(
            self.result.gaps,
            {
                fixture.SERIES_REVENUE: [2017.0],
                fixture.SERIES_PROFIT: [2020.0],
            },
        )
        for name, gap_year in (
            (fixture.SERIES_REVENUE, 2017),
            (fixture.SERIES_PROFIT, 2020),
        ):
            points = self.result.series_points[name]
            years = [int(point.x_value) for point in points]
            self.assertNotIn(gap_year, years)
            # Samples on either side of the hole come from separate stroked
            # objects, so no recovered object carries a segment across it.
            before = max(
                (point for point in points if point.x_value < gap_year),
                key=lambda point: point.x_value,
            )
            after = min(
                (point for point in points if point.x_value > gap_year),
                key=lambda point: point.x_value,
            )
            self.assertIsNot(before.source, after.source)
            self.assertFalse(before.connected_to_next)
            self.assertEqual(after.point_index, 0)
            # The hole is a hole for this series only; the other indicator
            # may legitimately have a real sample in the same year.
            self.assertFalse(
                any(point.x_value == gap_year for point in points)
            )

    def test_named_points_run_through_the_year_before_the_tail(self):
        for name in (fixture.SERIES_REVENUE, fixture.SERIES_PROFIT):
            years = sorted(
                int(point.x_value)
                for point in self.result.series_points[name]
            )
            self.assertEqual(years[-1], 2022)
            self.assertNotIn(2023, years)
            self.assertNotIn(2024, years)

    def test_ambiguous_tails_are_flagged_not_guessed(self):
        segments = self.result.ambiguous_segments
        self.assertEqual(len(segments), 2)
        tail_years = sorted(
            point.x_value
            for segment in segments
            for point in segment.points
        )
        self.assertEqual(tail_years, [2023.0, 2023.0, 2024.0, 2024.0])
        both_names = sorted([fixture.SERIES_REVENUE, fixture.SERIES_PROFIT])
        for segment in segments:
            self.assertIsInstance(segment, AmbiguousSegment)
            self.assertEqual(segment.dash_array, tuple(fixture.SHARED_TAIL))
            # Both continuations have geometric support, so the segment must
            # stay unnamed while the candidates are recorded explicitly.
            self.assertEqual(sorted(segment.candidate_series), both_names)
            for point in segment.points:
                self.assertEqual(point.status, "ambiguous")
                self.assertIsNone(point.series)
                self.assertEqual(sorted(point.candidate_series), both_names)
                self.assertIn("legend", point.ambiguity_reason)

    def test_identifiable_points_before_crossing_remain_tabled(self):
        named_count = sum(
            len(points) for points in self.result.series_points.values()
        )
        # 8 named samples per series (each has one internal gap and stops
        # before the ambiguous 2023-2024 tail).
        self.assertEqual(named_count, 16)

    def test_named_points_carry_no_candidates(self):
        for points in self.result.series_points.values():
            for point in points:
                self.assertIsNone(point.candidate_series)

    def test_gray_gridlines_share_dash_but_are_excluded_by_color(self):
        gray_gridlines = [
            line
            for line in self.page.lines
            if line["dash"] == ([1, 3], 0)
            and not colors_match(line["stroking_color"], BLUE)
        ]
        self.assertTrue(gray_gridlines)
        # Same dash rhythm, different color: none may be treated as data.
        self.assertEqual(
            set(map(id, gray_gridlines))
            & set(map(id, self.result.sources)),
            set(),
        )

    def test_source_refs_point_back_at_live_page_objects(self):
        for point in self.result.points:
            object_type = point.source["object_type"]
            index = int(point.source_ref.split("[")[1].rstrip("]"))
            self.assertIs(
                point.source, self.page.objects[object_type][index]
            )
        self.assertTrue(
            all(
                source in self.page.curves + self.page.lines
                for source in self.result.sources
            )
        )

    def test_extraction_does_not_mutate_the_page(self):
        curve_count = len(self.page.curves)
        line_count = len(self.page.lines)
        extract_chart(self.page, BLUE)
        self.assertEqual(len(self.page.curves), curve_count)
        self.assertEqual(len(self.page.lines), line_count)

    def test_records_have_stable_shape(self):
        records = self.result.to_records()
        self.assertEqual(len(records), 20)
        self.assertEqual(
            set(records[0]),
            {
                "series",
                "status",
                "candidate_series",
                "x",
                "y",
                "connected_to_next",
                "ambiguity_reason",
                "source",
            },
        )
        ambiguous = next(
            record for record in records if record["status"] == "ambiguous"
        )
        self.assertEqual(
            sorted(ambiguous["candidate_series"]),
            sorted([fixture.SERIES_REVENUE, fixture.SERIES_PROFIT]),
        )

    def test_explicit_ticks_override_detection(self):
        x_ticks = [
            AxisTick(float(year), fixture.x_for_year(year))
            for year in fixture.YEARS
        ]
        y_ticks = [
            AxisTick(
                float(value),
                fixture.PAGE_HEIGHT - fixture.y_for_value(value),
            )
            for value in fixture.Y_TICK_VALUES
        ]
        result = extract_chart(
            self.page, BLUE, x_ticks=x_ticks, y_ticks=y_ticks
        )
        self.assertEqual(
            len(result.series_points[fixture.SERIES_PROFIT]), 8
        )


class TestHelpers(unittest.TestCase):
    def test_linear_scale_round_trip(self):
        scale = LinearScale.from_ticks(
            "x", [AxisTick(0, 10.0), AxisTick(10, 30.0)]
        )
        self.assertAlmostEqual(scale.to_data(20.0), 5.0)
        self.assertAlmostEqual(scale.to_coord(5.0), 20.0)

    def test_linear_scale_rejects_non_collinear_ticks(self):
        with self.assertRaises(ValueError):
            LinearScale.from_ticks(
                "x",
                [AxisTick(0, 0.0), AxisTick(1, 10.0), AxisTick(3, 20.0)],
            )

    def test_dash_signature_normalizes_empty(self):
        self.assertEqual(dash_signature(None), ((), 0.0))
        self.assertEqual(dash_signature(([], 0)), ((), 0.0))
        self.assertEqual(dash_signature(([8, 4], 5)), ((8.0, 4.0), 5.0))

    def test_dash_arrays_match_ignores_phase_only_via_separate_field(self):
        self.assertTrue(dash_arrays_match([8.0, 4.0], [8, 4]))
        self.assertFalse(dash_arrays_match([8.0, 4.0], [8.0, 3.0]))
        self.assertFalse(dash_arrays_match([1.0, 3.0], [8.0, 4.0]))

    def test_colors_match_grayscale_and_rgb(self):
        self.assertTrue(colors_match(0, (0.0,)))
        self.assertTrue(colors_match((0.122, 0.467, 0.705), BLUE))
        self.assertFalse(colors_match((0.75, 0.75, 0.75), BLUE))
        self.assertFalse(colors_match(None, BLUE))
        self.assertFalse(colors_match((0.0, 0.0), BLUE))

    def test_split_subpaths_keeps_breaks_separate(self):
        obj = {
            "path": [
                ("m", (0.0, 0.0)),
                ("l", (1.0, 1.0)),
                ("m", (3.0, 3.0)),
                ("l", (4.0, 4.0)),
            ],
            "pts": [],
        }
        subpaths = split_subpaths(obj)
        self.assertEqual(
            subpaths,
            [[(0.0, 0.0), (1.0, 1.0)], [(3.0, 3.0), (4.0, 4.0)]],
        )

    def test_detect_frame_and_ticks_on_fixture(self):
        with pdfplumber.open(FIXTURE_PATH) as pdf:
            page = pdf.pages[0]
            frame = detect_frame(page)
            self.assertEqual((frame["x0"], frame["x1"]), (90.0, 522.0))
            x_ticks = detect_axis_ticks(page, frame, "x")
            y_ticks = detect_axis_ticks(page, frame, "y")
            self.assertEqual(len(x_ticks), len(fixture.YEARS))
            self.assertEqual(len(y_ticks), len(fixture.Y_TICK_VALUES))

    def test_data_point_record_carries_source(self):
        with pdfplumber.open(FIXTURE_PATH) as pdf:
            page = pdf.pages[0]
            result = extract_chart(page, BLUE)
            point = result.series_points[fixture.SERIES_REVENUE][0]
            self.assertIsInstance(point, DataPoint)
            self.assertTrue(point.source_ref.startswith("page1:"))
