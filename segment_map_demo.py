#!/usr/bin/env python3
"""Walk the recording-studio slice scenario end to end.

Run from the repository root:

    python segment_map_demo.py
"""

from __future__ import annotations

from dataclasses import replace

from tinytag.segment_map import (
    DEFAULT_ENCODER_DELAY,
    EncodingMode,
    Relation,
    SliceFile,
    WarehouseAnchor,
    check_coverage,
    map_slices,
    required_frame_count,
    stamp_like_independent_files,
    validate_whole_manifest,
    warehouse_retrieve,
)

SPF = 1152
DELAY = DEFAULT_ENCODER_DELAY


def show(title: str, mappings, master_samples: int) -> None:
    print(f'== {title} ==')
    for mapping in mappings:
        window = mapping.source_window
        unknown = mapping.indeterminable_ends
        unknown_text = (
            '  undeterminable: '
            + ', '.join(f'{end} ({why})' for end, why in unknown)
            if unknown else '')
        print(
            f'  {mapping.name}: frames={mapping.frame_count} '
            f'pcm={mapping.pcm_sample_count} '
            f'window=[{window.start}, {window.end})'
            f'{unknown_text}')
    report = check_coverage(mappings, master_true_samples=master_samples)
    print(f'  complete coverage: {report.complete}')
    for gap in report.gaps:
        print(f'  GAP  [{gap.start}, {gap.end}) '
              f'({gap.sample_count} samples)')
    for overlap in report.overlaps:
        print(f'  OVERLAP [{overlap.start}, {overlap.end}) '
              f'({overlap.sample_count} samples)')
    print()


def main() -> None:
    # One master of 10000 samples, encoded as a 10-frame MPEG-1 LIII
    # stream (11520 decoder samples = 10000 real + 1152 delay + 368 pad)
    # and cut on frame boundaries into three slices: 4 / 3 / 3 frames.
    master_samples = 10000
    whole_manifest = [
        SliceFile('part1.mp3', 4, enc_delay=DELAY, end_padding=0),
        SliceFile('part2.mp3', 3, enc_delay=None, end_padding=0),
        SliceFile('part3.mp3', 3, enc_delay=None, end_padding=368),
    ]
    print('Same three-slice manifest, annotated both ways.\n')

    correct = map_slices(
        whole_manifest, EncodingMode.WHOLE_THEN_SPLIT)
    show('A. encode whole, then split (correct back-references)',
         correct, master_samples)

    stamped = stamp_like_independent_files(whole_manifest, DELAY)
    print('Manifest problems after per-file delay stamping:')
    for issue in validate_whole_manifest(stamped):
        print(f'  - {issue}')
    print()
    show('B. warehouse deducts fill per file (frame-grid anchor)',
         warehouse_retrieve(
             stamped, stream_encoder_delay=DELAY,
             anchor=WarehouseAnchor.FRAME_GRID),
         master_samples)
    show('C. same deduction, slices laid end to end (cumulative)',
         warehouse_retrieve(stamped), master_samples)

    # The other encoder: three independent encodes of 3000/2500/2000
    # samples. Each file legitimately owns its own delay and padding.
    segmented_manifest = []
    for index, true_length in enumerate((3000, 2500, 2000), start=1):
        frames, padding = required_frame_count(
            true_length, DELAY, SPF)
        segmented_manifest.append(SliceFile(
            f'part{index}.mp3', frames, enc_delay=DELAY,
            end_padding=padding))
    show('D. each segment encoded independently',
         map_slices(segmented_manifest, EncodingMode.SEGMENTED), 7500)

    # Missing fill records: endpoints must stay undetermined. Here
    # the stream delay is known but the final tail padding is absent.
    blind = [
        whole_manifest[0],
        whole_manifest[1],
        replace(whole_manifest[2], end_padding=None),
    ]
    show('E. fill information missing (tail padding unknown)',
         map_slices(blind, EncodingMode.WHOLE_THEN_SPLIT),
         master_samples)

    # Equal edge sample values are not overlap evidence.
    first = correct[0].source_window
    second = correct[1].source_window
    print('== F. identical edge sample values ==')
    print(f'  part1 tail value = 42, part2 head value = 42')
    print(f'  structural relation: {first.relation_to(second)} '
          f'(not {Relation.OVERLAP})')


if __name__ == '__main__':
    main()
