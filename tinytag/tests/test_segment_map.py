# SPDX-FileCopyrightText: 2026 tinytag Contributors
# SPDX-License-Identifier: MIT

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=missing-module-docstring

from __future__ import annotations

from dataclasses import replace
from unittest import TestCase

from tinytag.segment_map import (
    DEFAULT_ENCODER_DELAY,
    EncodingMode,
    MPEG1_LAYER3_SAMPLES,
    MPEG2_LAYER3_SAMPLES,
    Relation,
    SampleWindow,
    SliceFile,
    WarehouseAnchor,
    check_coverage,
    map_slices,
    required_frame_count,
    stamp_like_independent_files,
    validate_whole_manifest,
    warehouse_retrieve,
)

SPF = MPEG1_LAYER3_SAMPLES
DELAY = DEFAULT_ENCODER_DELAY


def _segmented_manifest() -> list[SliceFile]:
    """Independent encodes, true lengths 3000/2500/2000 (4/4/3 frames)."""
    true_lengths = (3000, 2500, 2000)
    files: list[SliceFile] = []
    for index, true_length in enumerate(true_lengths):
        frames, padding = required_frame_count(
            true_length, DELAY, SPF)
        files.append(SliceFile(
            name=f'seg{index + 1}.mp3',
            frame_count=frames,
            enc_delay=DELAY,
            end_padding=padding,
            samples_per_frame=SPF,
        ))
    return files


def _whole_stream_manifest() -> list[SliceFile]:
    """10-frame stream (pcm 11520, 10000 real samples) cut 4/3/3."""
    return [
        SliceFile('seg1.mp3', 4, enc_delay=DELAY, end_padding=0),
        SliceFile('seg2.mp3', 3, enc_delay=None, end_padding=0),
        SliceFile('seg3.mp3', 3, enc_delay=None, end_padding=368),
    ]


class FrameMathTests(TestCase):

    def test_required_frame_count_pads_to_frame_grid(self) -> None:
        self.assertEqual(
            required_frame_count(3000, DELAY, SPF), (4, 456))
        self.assertEqual(4 * SPF, 3000 + DELAY + 456)

    def test_required_frame_count_exact_grid_has_no_padding(self) -> None:
        frames, padding = required_frame_count(
            2 * SPF - DELAY, DELAY, SPF)
        self.assertEqual((frames, padding), (2, 0))

    def test_required_frame_count_always_emits_a_frame(self) -> None:
        frames, padding = required_frame_count(0, DELAY, SPF)
        self.assertEqual(frames, 1)
        self.assertEqual(padding, SPF - DELAY)

    def test_sample_window_rejects_bad_extents(self) -> None:
        with self.assertRaises(ValueError):
            SampleWindow(5, 4)
        with self.assertRaises(ValueError):
            SampleWindow(-1, 4)


class RelationTests(TestCase):

    def test_relations_are_structural(self) -> None:
        first = SampleWindow(0, 3000)
        second = SampleWindow(3000, 5500)
        self.assertEqual(first.relation_to(second), Relation.ADJACENT)
        self.assertEqual(second.relation_to(first), Relation.ADJACENT)
        self.assertEqual(
            SampleWindow(0, 10).relation_to(SampleWindow(12, 20)),
            Relation.GAP)
        self.assertEqual(
            SampleWindow(0, 10).relation_to(SampleWindow(8, 20)),
            Relation.OVERLAP)

    def test_identical_edge_values_do_not_prove_overlap(self) -> None:
        manifest = _segmented_manifest()
        manifest[0] = replace(manifest[0], tail_edge_value=42)
        manifest[1] = replace(manifest[1], head_edge_value=42)
        mappings = map_slices(manifest, EncodingMode.SEGMENTED)
        first_window = mappings[0].source_window
        second_window = mappings[1].source_window
        self.assertEqual(
            first_window.relation_to(second_window),
            Relation.ADJACENT)
        plain = map_slices(
            _segmented_manifest(), EncodingMode.SEGMENTED)
        self.assertEqual(
            [m.source_window for m in mappings],
            [m.source_window for m in plain])

    def test_unknown_endpoints_are_indeterminate(self) -> None:
        self.assertEqual(
            SampleWindow(None, 10).relation_to(SampleWindow(0, 10)),
            Relation.INDETERMINATE)


class SegmentedEncodingTests(TestCase):

    def test_independent_encodes_cover_master_completely(self) -> None:
        manifest = _segmented_manifest()
        mappings = map_slices(manifest, EncodingMode.SEGMENTED)
        windows = [m.source_window for m in mappings]
        self.assertEqual(
            windows,
            [SampleWindow(0, 3000),
             SampleWindow(3000, 5500),
             SampleWindow(5500, 7500)])
        report = check_coverage(mappings, master_true_samples=7500)
        self.assertTrue(report.complete)
        self.assertEqual(report.gaps, ())
        self.assertEqual(report.overlaps, ())
        for mapping in mappings:
            self.assertEqual(mapping.indeterminable_ends, ())

    def test_missing_fill_makes_endpoints_undeterminable(self) -> None:
        manifest = [
            SliceFile('seg1.mp3', 4, enc_delay=None, end_padding=456),
            SliceFile('seg2.mp3', 3, enc_delay=DELAY, end_padding=None),
            SliceFile('seg3.mp3', 3, enc_delay=DELAY, end_padding=880),
        ]
        mappings = map_slices(manifest, EncodingMode.SEGMENTED)
        self.assertIsNone(mappings[0].source_window.end)
        self.assertEqual(
            mappings[0].indeterminable_ends[0][0], 'end')
        for mapping in mappings[1:]:
            self.assertIsNone(mapping.source_window.start)
            self.assertIsNone(mapping.source_window.end)
            end_names = {
                reason[0] for reason in mapping.indeterminable_ends}
            self.assertEqual(end_names, {'start', 'end'})
        report = check_coverage(mappings, master_true_samples=7500)
        self.assertFalse(report.complete)
        self.assertTrue(report.has_indeterminate_ends)


class WholeStreamEncodingTests(TestCase):

    def test_correct_manifest_maps_each_frame_grid_slice(self) -> None:
        mappings = map_slices(
            _whole_stream_manifest(),
            EncodingMode.WHOLE_THEN_SPLIT)
        self.assertEqual(
            [m.source_window for m in mappings],
            [SampleWindow(0, 3456),
             SampleWindow(3456, 6912),
             SampleWindow(6912, 10000)])
        report = check_coverage(mappings, master_true_samples=10000)
        self.assertTrue(report.complete)

    def test_correct_manifest_has_no_internal_fill_flags(self) -> None:
        self.assertEqual(
            validate_whole_manifest(_whole_stream_manifest()), [])

    def test_validator_flags_bogus_internal_delay_stamps(self) -> None:
        stamped = stamp_like_independent_files(
            _whole_stream_manifest(), DELAY)
        issues = validate_whole_manifest(stamped)
        self.assertEqual(len(issues), 2)
        self.assertIn('seg2.mp3', issues[0])
        self.assertIn('seg3.mp3', issues[1])


class SameManifestTwoAnnotationsTests(TestCase):

    def test_segmented_annotation_covers_its_master(self) -> None:
        mappings = warehouse_retrieve(_segmented_manifest())
        report = check_coverage(mappings, master_true_samples=7500)
        self.assertTrue(report.complete)

    def test_whole_stream_loses_samples_at_every_splice_on_grid(self) -> None:
        stamped = stamp_like_independent_files(
            _whole_stream_manifest(), DELAY)
        mappings = warehouse_retrieve(
            stamped, stream_encoder_delay=DELAY,
            anchor=WarehouseAnchor.FRAME_GRID)
        self.assertEqual(
            [m.source_window for m in mappings],
            [SampleWindow(0, 3456),
             SampleWindow(4608, 6912),
             SampleWindow(8064, 10000)])
        report = check_coverage(mappings, master_true_samples=10000)
        self.assertFalse(report.complete)
        self.assertEqual(
            report.gaps[0], SampleWindow(3456, 4608))
        self.assertEqual(report.gaps[0].sample_count, DELAY)
        self.assertEqual(
            sum(window.sample_count for window in report.gaps),
            2 * DELAY)
        self.assertEqual(report.overlaps, ())

    def test_cumulative_anchor_drifts_and_runs_short(self) -> None:
        stamped = stamp_like_independent_files(
            _whole_stream_manifest(), DELAY)
        mappings = warehouse_retrieve(stamped)
        self.assertEqual(
            [m.source_window for m in mappings],
            [SampleWindow(0, 3456),
             SampleWindow(3456, 5760),
             SampleWindow(5760, 7696)])
        report = check_coverage(mappings, master_true_samples=10000)
        self.assertFalse(report.complete)
        self.assertEqual(report.gaps, (SampleWindow(7696, 10000),))
        self.assertEqual(report.gaps[0].sample_count, 2 * DELAY)

    def test_frame_grid_anchor_requires_stream_delay(self) -> None:
        stamped = stamp_like_independent_files(
            _whole_stream_manifest(), DELAY)
        mappings = warehouse_retrieve(
            stamped, stream_encoder_delay=None,
            anchor=WarehouseAnchor.FRAME_GRID)
        for mapping in mappings:
            self.assertFalse(mapping.source_window.is_fully_known)
            self.assertTrue(mapping.indeterminable_ends)

    def test_structural_mapping_ignores_edge_sample_values(self) -> None:
        stamped = stamp_like_independent_files(
            _whole_stream_manifest(), DELAY)
        with_values = [
            replace(slice_file, head_edge_value=7, tail_edge_value=7)
            for slice_file in stamped]
        self.assertEqual(
            [m.source_window for m in warehouse_retrieve(with_values)],
            [m.source_window for m in warehouse_retrieve(stamped)])


class CoverageTests(TestCase):

    def test_reports_overlap(self) -> None:
        files = _segmented_manifest()
        mappings = map_slices(files, EncodingMode.SEGMENTED)
        shifted = [
            replace(mapping, source_window=SampleWindow(
                max(0, mapping.source_window.start - 100),
                mapping.source_window.end))
            for mapping in mappings]
        report = check_coverage(shifted, master_true_samples=7500)
        self.assertFalse(report.complete)
        self.assertEqual(len(report.overlaps), 2)

    def test_reports_head_and_tail_gap(self) -> None:
        files = _segmented_manifest()
        mappings = map_slices(files, EncodingMode.SEGMENTED)
        trimmed = [
            replace(mappings[0], source_window=SampleWindow(10, 3000)),
            mappings[1],
            replace(mappings[2], source_window=SampleWindow(5500, 7400)),
        ]
        report = check_coverage(trimmed, master_true_samples=7500)
        self.assertEqual(
            report.gaps,
            (SampleWindow(0, 10), SampleWindow(7400, 7500)))

    def test_unknown_tail_does_not_invent_a_gap(self) -> None:
        blind = [
            replace(_whole_stream_manifest()[2], end_padding=None),
        ]
        mappings = map_slices(
            _whole_stream_manifest()[:2] + blind,
            EncodingMode.WHOLE_THEN_SPLIT)
        report = check_coverage(mappings, master_true_samples=10000)
        self.assertTrue(report.has_indeterminate_ends)
        self.assertEqual(report.gaps, ())
        self.assertFalse(report.complete)


class Mpeg2Tests(TestCase):

    def test_mpeg2_layer3_uses_576_samples_per_frame(self) -> None:
        mpeg2_delay = MPEG2_LAYER3_SAMPLES
        frames, padding = required_frame_count(
            1000, mpeg2_delay, MPEG2_LAYER3_SAMPLES)
        self.assertEqual(frames, 3)
        self.assertEqual(padding, 3 * 576 - 1000 - mpeg2_delay)
        files = [
            SliceFile('a.mp3', frames, enc_delay=mpeg2_delay,
                      end_padding=padding,
                      samples_per_frame=MPEG2_LAYER3_SAMPLES),
        ]
        mappings = map_slices(files, EncodingMode.SEGMENTED)
        self.assertEqual(
            mappings[0].source_window, SampleWindow(0, 1000))
