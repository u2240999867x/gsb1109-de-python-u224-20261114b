from __future__ import annotations

import json
import unittest

from pystac import (
    AxisViolation,
    BboxOverlap,
    CoverageCheckResult,
    SpatialExtent,
    TemporalExtent,
    check_bbox_coverage,
    check_spatial_coverage,
)

TEST_DATETIME_STR = "2020-03-14T16:32:00Z"


class CheckBboxCoverageTest(unittest.TestCase):
    def test_piece_inside_overall_bbox_is_covered(self) -> None:
        overlap = check_bbox_coverage([-100, -50, 100, 50], [-90, -40, 90, 40], index=3)
        self.assertTrue(overlap.covered)
        self.assertEqual(overlap.index, 3)
        self.assertEqual(overlap.dimensions, 2)
        self.assertEqual(overlap.violations, [])
        self.assertFalse(overlap.crosses_antimeridian)

    def test_small_piece_does_not_treat_empty_space_as_observed(self) -> None:
        overlap = check_bbox_coverage([-180, -90, 180, 90], [0, 0, 1, 1])
        self.assertTrue(overlap.covered)
        self.assertEqual(overlap.violations, [])

    def test_piece_outside_overall_bbox_reports_axis(self) -> None:
        overlap = check_bbox_coverage([-100, -50, 100, 50], [101, 0, 110, 10])
        self.assertFalse(overlap.covered)
        self.assertEqual(len(overlap.violations), 1)
        violation = overlap.violations[0]
        self.assertEqual(violation.axis, "x")
        self.assertEqual(violation.bound, "max")
        self.assertEqual(violation.piece_value, 110.0)
        self.assertEqual(violation.overall_value, 100.0)

    def test_latitude_overflow_is_reported(self) -> None:
        overlap = check_bbox_coverage([-100, -50, 100, 50], [0, 51, 10, 60])
        self.assertFalse(overlap.covered)
        self.assertEqual(
            [(v.axis, v.bound) for v in overlap.violations], [("y", "max")]
        )

    def test_tolerance_accepts_values_on_the_bound(self) -> None:
        inside = check_bbox_coverage(
            [-100, -50, 100, 50],
            [100.0 + 1e-12, 0, 100.0 + 1e-12, 1],
            seam_tolerance=1e-9,
        )
        self.assertTrue(inside.covered)

        outside = check_bbox_coverage([-100, -50, 100, 50], [100.5, 0, 101, 1])
        self.assertFalse(outside.covered)

    def test_piece_crossing_antimeridian_is_detected(self) -> None:
        overlap = check_bbox_coverage([170, -50, -170, 50], [175, -10, -175, 10])
        self.assertTrue(overlap.covered)
        self.assertTrue(overlap.crosses_antimeridian)

    def test_piece_on_either_branch_of_wrapping_overall_passes(self) -> None:
        overall = [170, -50, -170, 50]
        east = check_bbox_coverage(overall, [172, 0, 178, 10])
        west = check_bbox_coverage(overall, [-178, 0, -172, 10])
        self.assertTrue(east.covered)
        self.assertTrue(west.covered)
        self.assertFalse(east.crosses_antimeridian)

    def test_piece_in_gap_of_wrapping_overall_fails(self) -> None:
        overlap = check_bbox_coverage([170, -50, -170, 50], [-100, 0, -90, 10])
        self.assertFalse(overlap.covered)
        self.assertTrue(all(v.axis == "x" for v in overlap.violations))

    def test_wrapping_piece_under_full_globe_overall_passes(self) -> None:
        overlap = check_bbox_coverage([-180, -90, 180, 90], [170, 0, -170, 10])
        self.assertTrue(overlap.covered)
        self.assertTrue(overlap.crosses_antimeridian)

    def test_wrapping_piece_under_regional_overall_fails(self) -> None:
        overlap = check_bbox_coverage([-100, -50, 100, 50], [170, 0, -170, 10])
        self.assertFalse(overlap.covered)

    def test_height_overflow_reports_z_even_when_2d_footprint_fits(self) -> None:
        overall = [-100, -50, 0, 100, 50, 1000]
        overlap = check_bbox_coverage(overall, [-90, -40, 0, 90, 40, 1200])
        self.assertFalse(overlap.covered)
        self.assertEqual(
            [(v.axis, v.bound) for v in overlap.violations], [("z", "max")]
        )

    def test_three_d_piece_within_three_d_overall_passes(self) -> None:
        overall = [-100, -50, 0, 100, 50, 1000]
        overlap = check_bbox_coverage(overall, [-90, -40, 10, 90, 40, 900])
        self.assertTrue(overlap.covered)

    def test_extra_height_axis_when_overall_is_2d(self) -> None:
        overlap = check_bbox_coverage([-100, -50, 100, 50], [-90, -40, 90, 40, 0, 100])
        self.assertFalse(overlap.covered)
        self.assertEqual(len(overlap.violations), 1)
        violation = overlap.violations[0]
        self.assertEqual(violation.axis, "z")
        self.assertEqual(violation.bound, "extra")
        self.assertIsNone(violation.overall_value)

    def test_2d_piece_against_3d_overall_records_missing_axis(self) -> None:
        overall = [-100, -50, 0, 100, 50, 1000]
        overlap = check_bbox_coverage(overall, [-90, -40, 90, 40])
        self.assertTrue(overlap.covered)
        self.assertEqual(overlap.missing_axes, ["z"])

    def test_seam_overshoot_is_wrapped_but_large_invalid_longitude_fails(
        self,
    ) -> None:
        wrapped = check_bbox_coverage([-180, -90, 180, 90], [175, 0, 180.5, 10])
        self.assertTrue(wrapped.covered)
        self.assertTrue(wrapped.crosses_antimeridian)

        invalid = check_bbox_coverage([-180, -90, 180, 90], [0, 0, 200, 10])
        self.assertFalse(invalid.covered)

    def test_invalid_bbox_shapes_raise(self) -> None:
        with self.assertRaises(ValueError):
            check_bbox_coverage([-100, -50, 100], [0, 0, 1, 1])
        with self.assertRaises(TypeError):
            check_bbox_coverage([-100, -50, 100, 50], [0, 0, "1", 1])  # type: ignore
        with self.assertRaises(TypeError):
            check_bbox_coverage([-100, -50, 100, 50], 42)  # type: ignore


class CheckSpatialCoverageTest(unittest.TestCase):
    def test_aggregates_counts(self) -> None:
        result = check_spatial_coverage(
            [-100, -50, 100, 50],
            [[-90, -40, 90, 40], [101, 0, 110, 10]],
        )
        self.assertIsInstance(result, CoverageCheckResult)
        self.assertEqual(result.total_pieces, 2)
        self.assertEqual(result.covered_pieces, 1)
        self.assertEqual(result.not_covered_pieces, 1)
        self.assertFalse(result.all_covered)
        self.assertEqual(len(result.overlaps), 2)
        self.assertEqual([o.index for o in result.overlaps], [0, 1])

    def test_all_covered_when_every_piece_fits(self) -> None:
        result = check_spatial_coverage(
            [-100, -50, 100, 50],
            [[-90, -40, 0, 10], [0, -10, 90, 40]],
        )
        self.assertTrue(result.all_covered)
        self.assertEqual(result.not_covered_pieces, 0)

    def test_empty_piece_list_is_fully_covered(self) -> None:
        result = check_spatial_coverage([-100, -50, 100, 50], [])
        self.assertTrue(result.all_covered)
        self.assertEqual(result.total_pieces, 0)
        self.assertEqual(result.samples, [])

    def test_seam_pair_passes_together_and_is_annotated(self) -> None:
        result = check_spatial_coverage(
            [170, -50, -170, 50],
            [[175, -10, 180, 10], [-180, -10, -175, 10]],
        )
        self.assertTrue(result.all_covered)
        first, second = result.overlaps
        self.assertTrue(first.seam_joined)
        self.assertTrue(second.seam_joined)
        self.assertEqual(first.seam_partner, 1)
        self.assertEqual(second.seam_partner, 0)

    def test_branch_pieces_pass_without_seam_annotation(self) -> None:
        result = check_spatial_coverage(
            [170, -50, -170, 50],
            [[172, 0, 178, 10], [-178, 0, -172, 10]],
        )
        self.assertTrue(result.all_covered)
        self.assertFalse(any(o.seam_joined for o in result.overlaps))

    def test_seam_pair_with_non_overlapping_latitude_is_not_annotated(
        self,
    ) -> None:
        result = check_spatial_coverage(
            [170, -50, -170, 50],
            [[175, 30, 180, 40], [-180, -40, -175, -30]],
        )
        self.assertTrue(result.all_covered)
        self.assertFalse(any(o.seam_joined for o in result.overlaps))

    def test_seam_pair_does_not_hide_latitude_violation(self) -> None:
        result = check_spatial_coverage(
            [170, -50, -170, 50],
            [[175, 60, 180, 70], [-180, -10, -175, 10]],
        )
        self.assertFalse(result.all_covered)
        bad = result.overlaps[0]
        self.assertFalse(bad.seam_joined)
        self.assertEqual([(v.axis, v.bound) for v in bad.violations], [("y", "max")])

    def test_antimeridian_count(self) -> None:
        result = check_spatial_coverage(
            [170, -50, -170, 50],
            [[175, -10, -175, 10], [172, 0, 178, 10]],
        )
        self.assertEqual(result.antimeridian_pieces, 1)

    def test_extra_height_axis_count(self) -> None:
        result = check_spatial_coverage(
            [-100, -50, 100, 50],
            [[-90, -40, 90, 40, 0, 100], [0, 0, 1, 1]],
        )
        self.assertEqual(result.extra_height_axis_pieces, 1)

    def test_sample_keeps_not_covered_pieces_first(self) -> None:
        pieces = [[-90, -40, 90, 40]] * 5 + [[101, 0, 110, 10]]
        result = check_spatial_coverage([-100, -50, 100, 50], pieces, sample_size=3)
        self.assertEqual(len(result.samples), 3)
        self.assertEqual(result.samples[0].index, 5)
        self.assertFalse(result.samples[0].covered)
        self.assertTrue(all(o.covered for o in result.samples[1:]))

    def test_sample_size_zero_returns_no_samples(self) -> None:
        result = check_spatial_coverage(
            [-100, -50, 100, 50], [[0, 0, 1, 1]], sample_size=0
        )
        self.assertEqual(result.samples, [])

    def test_sample_returns_everything_when_below_limit(self) -> None:
        result = check_spatial_coverage(
            [-100, -50, 100, 50], [[0, 0, 1, 1]], sample_size=10
        )
        self.assertEqual(len(result.samples), 1)

    def test_result_is_json_serializable(self) -> None:
        result = check_spatial_coverage(
            [-100, -50, 100, 50],
            [[-90, -40, 90, 40], [101, 0, 110, 10, 0, 100]],
        )
        encoded = json.dumps(result.to_dict())
        decoded = json.loads(encoded)
        self.assertEqual(decoded["overall_bbox"], [-100, -50, 100, 50])
        self.assertEqual(decoded["not_covered_pieces"], 1)
        self.assertIn("violations", decoded["overlaps"][1])
        self.assertIn("message", decoded["overlaps"][1]["violations"][0])

    def test_invalid_piece_bbox_reports_index(self) -> None:
        with self.assertRaises(ValueError) as context:
            check_spatial_coverage([-100, -50, 100, 50], [[0, 0, 1, 1], [0, 0, 1]])
        self.assertIn("piece_bboxes[1]", str(context.exception))

    def test_piece_bboxes_must_be_a_sequence(self) -> None:
        with self.assertRaises(TypeError):
            check_spatial_coverage(
                [-100, -50, 100, 50],
                42,  # type: ignore
            )


class SpatialExtentCoverageTest(unittest.TestCase):
    def _extent(self, bboxes: list) -> SpatialExtent:
        return SpatialExtent(bboxes=bboxes)

    def test_first_bbox_is_treated_as_overall(self) -> None:
        extent = self._extent(
            [
                [-100, -50, 100, 50],
                [-90, -40, 90, 40],
                [101, 0, 110, 10],
            ]
        )
        result = extent.check_coverage()
        self.assertEqual(result.overall_bbox, [-100, -50, 100, 50])
        self.assertEqual(result.total_pieces, 2)
        self.assertEqual(result.not_covered_pieces, 1)

    def test_single_bbox_has_no_pieces(self) -> None:
        extent = self._extent([[-100, -50, 100, 50]])
        result = extent.check_coverage()
        self.assertTrue(result.all_covered)
        self.assertEqual(result.total_pieces, 0)

    def test_empty_bboxes_raise(self) -> None:
        extent = SpatialExtent.__new__(SpatialExtent)
        extent.bboxes = []
        extent.extra_fields = {}
        with self.assertRaises(ValueError):
            extent.check_coverage()

    def test_three_d_collection_extent_check(self) -> None:
        extent = self._extent(
            [
                [-100, -50, 0, 100, 50, 1000],
                [-90, -40, 0, 90, 40, 1200],
            ]
        )
        result = extent.check_coverage()
        self.assertFalse(result.all_covered)
        violation = result.overlaps[0].violations[0]
        self.assertIsInstance(violation, AxisViolation)
        self.assertEqual((violation.axis, violation.bound), ("z", "max"))

    def test_round_trip_through_collection_dict(self) -> None:
        import pystac

        extent = pystac.Extent(
            spatial=SpatialExtent([[-100, -50, 100, 50], [-90, -40, 90, 40]]),
            temporal=TemporalExtent.from_dict(
                {"interval": [[TEST_DATETIME_STR, None]]}
            ),
        )
        collection = pystac.Collection(
            id="coverage-collection",
            description="coverage test",
            extent=extent,
        )
        rebuilt = pystac.Collection.from_dict(collection.to_dict())
        result = rebuilt.extent.spatial.check_coverage()
        self.assertTrue(result.all_covered)

    def test_overlap_dataclass_to_dict(self) -> None:
        overlap = BboxOverlap(
            index=0,
            bbox=[0, 0, 1, 1],
            dimensions=2,
            covered=True,
            crosses_antimeridian=False,
        )
        encoded = overlap.to_dict()
        self.assertEqual(encoded["index"], 0)
        self.assertEqual(encoded["bbox"], [0, 0, 1, 1])
        self.assertTrue(encoded["covered"])
        self.assertEqual(encoded["violations"], [])


if __name__ == "__main__":
    unittest.main()
