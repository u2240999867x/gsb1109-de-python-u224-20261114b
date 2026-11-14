# SPDX-FileCopyrightText: 2026 tinytag Contributors
# SPDX-License-Identifier: MIT

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=missing-module-docstring
# pyright: reportPrivateUsage=false

from __future__ import annotations

import os

from datetime import date
from fractions import Fraction
from pathlib import Path
from struct import pack
from tempfile import TemporaryDirectory
from unittest import TestCase

from tinytag import TinyTag
from tinytag.bwf import (
    Reel, ReelLedger, build_ledger, candidate_ledgers,
)


def _riff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    padding = b'\x00' if len(payload) % 2 else b''
    return chunk_id + pack('<I', len(payload)) + payload + padding


def make_bwf_wav(path: Path,
                 samplerate: int,
                 num_samples: int,
                 time_reference: int,
                 origination_date: bytes = b'2026-11-13',
                 origination_time: bytes = b'12:00:00',
                 description: bytes = b'',
                 originator: bytes = b'GSB Radio',
                 originator_reference: bytes = b'REF-0001',
                 version: int = 1,
                 umid: bytes = b'',
                 loudness: tuple[int, ...] | None = None,
                 coding_history: bytes = b'',
                 fact_samples: int | None = None) -> Path:
    channels = 1
    bitdepth = 16
    block_align = channels * bitdepth // 8
    byte_rate = samplerate * block_align
    fmt_payload = pack(
        '<HHIIHH', 1, channels, samplerate, byte_rate,
        block_align, bitdepth)
    bext_payload = b''.join((
        description.ljust(256, b'\x00'),
        originator.ljust(32, b'\x00'),
        originator_reference.ljust(32, b'\x00'),
        origination_date.ljust(10, b'\x00'),
        origination_time.ljust(8, b'\x00'),
        pack('<Q', time_reference),
        pack('<H', version),
        umid.ljust(64, b'\x00'),
    ))
    if loudness is not None:
        bext_payload += pack('<5h', *loudness)
    bext_payload += coding_history
    chunks = _riff_chunk(b'fmt ', fmt_payload)
    chunks += _riff_chunk(b'bext', bext_payload)
    if fact_samples is not None:
        chunks += _riff_chunk(b'fact', pack('<I', fact_samples))
    chunks += _riff_chunk(
        b'data', b'\x00' * (num_samples * block_align))
    riff = b'RIFF' + pack('<I', len(b'WAVE') + len(chunks)) + b'WAVE'
    riff += chunks
    path.write_bytes(riff)
    return path


class TestBwfParsing(TestCase):

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.tempdir = Path(self._tempdir.name)

    def test_bext_core_fields(self) -> None:
        path = make_bwf_wav(
            self.tempdir / 'reel.wav',
            samplerate=48000, num_samples=48000,
            time_reference=3600 * 48000,
            description=b'Evening news',
            fact_samples=48000)
        tag = TinyTag.get(os.fspath(path))
        self.assertEqual(tag.bwf_time_reference, 3600 * 48000)
        self.assertEqual(tag.bwf_origination_date, date(2026, 11, 13))
        self.assertEqual(tag.bwf_num_samples, 48000)
        self.assertEqual(tag.samplerate, 48000)
        self.assertEqual(tag.comment, 'Evening news')
        self.assertEqual(tag.other['bwf_originator'], ['GSB Radio'])
        self.assertEqual(
            tag.other['bwf_originator_reference'], ['REF-0001'])
        self.assertEqual(tag.other['bwf_origination_time'], ['12:00:00'])
        self.assertEqual(tag.other['bwf_version'], ['1'])
        flat = tag.as_dict()
        self.assertEqual(flat['bwf_time_reference'], 3600 * 48000)
        self.assertEqual(flat['bwf_num_samples'], 48000)
        self.assertEqual(flat['bwf_origination_date'], ['2026-11-13'])

    def test_bext_num_samples_from_data_without_fact(self) -> None:
        path = make_bwf_wav(
            self.tempdir / 'reel.wav',
            samplerate=44100, num_samples=22050,
            time_reference=0)
        tag = TinyTag.get(os.fspath(path))
        self.assertEqual(tag.bwf_num_samples, 22050)

    def test_bext_v2_umid_and_loudness(self) -> None:
        umid = b'\x01' * 32
        path = make_bwf_wav(
            self.tempdir / 'reel.wav',
            samplerate=48000, num_samples=4800, time_reference=0,
            version=2, umid=umid,
            loudness=(-23, 7, -1, -20, -22))
        tag = TinyTag.get(os.fspath(path))
        self.assertEqual(
            tag.other['bwf_umid'], [umid.hex().upper()])
        self.assertEqual(tag.other['bwf_loudness_value'], ['-23'])
        self.assertEqual(tag.other['bwf_loudness_range'], ['7'])
        self.assertEqual(tag.other['bwf_max_true_peak_level'], ['-1'])
        self.assertEqual(
            tag.other['bwf_max_momentary_loudness'], ['-20'])
        self.assertEqual(
            tag.other['bwf_max_short_term_loudness'], ['-22'])

    def test_truncated_bext_is_ignored(self) -> None:
        path = self.tempdir / 'truncated.wav'
        fmt_payload = pack(
            '<HHIIHH', 1, 1, 48000, 96000, 2, 16)
        chunks = _riff_chunk(b'fmt ', fmt_payload)
        chunks += _riff_chunk(b'bext', b'\x00' * 64)
        chunks += _riff_chunk(b'data', b'\x00' * 96000)
        riff = b'RIFF' + pack('<I', len(b'WAVE') + len(chunks)) + b'WAVE'
        path.write_bytes(riff + chunks)
        tag = TinyTag.get(os.fspath(path))
        self.assertIsNone(tag.bwf_time_reference)
        self.assertIsNone(tag.bwf_origination_date)
        self.assertIsNone(tag.bwf_num_samples)

    def test_bext_not_parsed_without_tags(self) -> None:
        path = make_bwf_wav(
            self.tempdir / 'reel.wav',
            samplerate=48000, num_samples=4800, time_reference=123)
        tag = TinyTag.get(os.fspath(path), tags=False)
        self.assertIsNone(tag.bwf_time_reference)
        self.assertIsNone(tag.bwf_num_samples)


class TestReelLedger(TestCase):

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.tempdir = Path(self._tempdir.name)
        self.day0 = date(2026, 11, 13)
        self.day1 = date(2026, 11, 14)

    def reel(self, name: str, samplerate: int, num_samples: int,
             time_reference: int, day: date | None = date(2026, 11, 13)
             ) -> Reel:
        path = make_bwf_wav(
            self.tempdir / name, samplerate, num_samples,
            time_reference,
            origination_date=(day.isoformat().encode('ascii')
                              if day is not None else b'\x00' * 10))
        tag = TinyTag.get(os.fspath(path))
        return Reel.from_tag(tag)

    def test_exact_fraction_relations_across_sample_rates(self) -> None:
        reels = [
            self.reel('a.wav', 48000, 48000, 0),
            self.reel('b.wav', 44100, 22050, 44100),
            self.reel('c.wav', 32000, 8000, 64000),
            self.reel('d.wav', 48000, 48000, 105600),
        ]
        ledger = build_ledger(reels)
        self.assertIsInstance(ledger, ReelLedger)
        kinds = [relation.kind for relation in ledger.relations]
        self.assertEqual(kinds, ['contiguous', 'gap', 'overlap'])
        contiguous, gap, overlap = ledger.relations
        self.assertEqual(contiguous.delta, Fraction(0))
        self.assertEqual(gap.delta, Fraction(1, 2))
        self.assertEqual(gap.gap, Fraction(1, 2))
        self.assertEqual(overlap.delta, Fraction(-1, 20))
        self.assertEqual(overlap.overlap, Fraction(1, 20))
        self.assertEqual(
            [entry.day for entry in ledger.entries],
            [self.day0] * 4)
        day_start = self.day0.toordinal() * 86400
        self.assertEqual(
            ledger.entries[0].start, Fraction(day_start))
        self.assertEqual(
            ledger.entries[1].start, Fraction(day_start + 1))

    def test_smaller_counter_same_day_is_overlap_not_new_day(self) -> None:
        reels = [
            self.reel('first.wav', 48000, 48000, 80000),
            self.reel('rerecorded.wav', 48000, 24000, 10000),
        ]
        ledger = build_ledger(reels)
        relation = ledger.relations[0]
        self.assertEqual(relation.kind, 'overlap')
        self.assertEqual(relation.delta, Fraction(-59, 24))
        self.assertEqual(
            {entry.day for entry in ledger.entries}, {self.day0})

    def test_midnight_rollover_uses_collection_dates(self) -> None:
        reels = [
            self.reel('late.wav', 48000, 96000, 86399 * 48000,
                      day=self.day0),
            self.reel('early.wav', 48000, 48000, 0, day=self.day1),
        ]
        ledger = build_ledger(reels)
        self.assertEqual(
            [entry.day for entry in ledger.entries],
            [self.day0, self.day1])
        relation = ledger.relations[0]
        self.assertEqual(relation.kind, 'overlap')
        self.assertEqual(relation.overlap, Fraction(1))

    def test_build_ledger_rejects_undated_reel(self) -> None:
        reels = [
            self.reel('a.wav', 48000, 4800, 0),
            self.reel('b.wav', 48000, 4800, 0, day=None),
        ]
        with self.assertRaises(ValueError):
            build_ledger(reels)

    def test_build_ledger_rejects_non_chronological_dates(self) -> None:
        reels = [
            self.reel('a.wav', 48000, 4800, 0, day=self.day1),
            self.reel('b.wav', 48000, 4800, 0, day=self.day0),
        ]
        with self.assertRaises(ValueError):
            build_ledger(reels)

    def test_reel_day_override(self) -> None:
        reel = self.reel('a.wav', 48000, 4800, 0, day=None)
        overridden = Reel.from_tag(
            TinyTag.get(os.fspath(self.tempdir / 'a.wav')),
            day=self.day1)
        self.assertIsNone(reel.day)
        self.assertEqual(overridden.day, self.day1)

    def test_candidate_ledgers_inside_window(self) -> None:
        reels = [
            self.reel('undated.wav', 48000, 4800, 0, day=None),
            self.reel('dated.wav', 48000, 4800, 4800, day=self.day1),
        ]
        ledgers = candidate_ledgers(reels, self.day0, self.day1)
        day_sequences = [
            tuple(entry.day for entry in ledger.entries)
            for ledger in ledgers
        ]
        self.assertEqual(
            sorted(day_sequences),
            [(self.day0, self.day1), (self.day1, self.day1)])
        for ledger in ledgers:
            for relation in ledger.relations:
                self.assertIsInstance(relation.delta, Fraction)

    def test_candidate_ledgers_all_undated(self) -> None:
        reels = [self.reel('only.wav', 48000, 4800, 0, day=None)]
        day2 = date(2026, 11, 15)
        ledgers = candidate_ledgers(reels, self.day0, day2)
        self.assertEqual(
            [ledger.entries[0].day for ledger in ledgers],
            [self.day0, self.day1, day2])

    def test_candidate_ledgers_known_day_outside_window(self) -> None:
        reels = [
            self.reel('dated.wav', 48000, 4800, 0, day=self.day1)]
        with self.assertRaises(ValueError):
            candidate_ledgers(
                reels, date(2026, 11, 10), date(2026, 11, 12))

    def test_candidate_ledgers_requires_window(self) -> None:
        reels = [self.reel('a.wav', 48000, 4800, 0, day=None)]
        with self.assertRaises(ValueError):
            candidate_ledgers(
                reels, None, self.day1)  # type: ignore[arg-type]

    def test_from_tag_rejects_incomplete_tag(self) -> None:
        path = self.tempdir / 'plain.wav'
        fmt_payload = pack(
            '<HHIIHH', 1, 1, 48000, 96000, 2, 16)
        chunks = _riff_chunk(b'fmt ', fmt_payload)
        chunks += _riff_chunk(b'data', b'\x00' * 96000)
        riff = b'RIFF' + pack('<I', len(b'WAVE') + len(chunks)) + b'WAVE'
        path.write_bytes(riff + chunks)
        with self.assertRaises(ValueError):
            Reel.from_tag(TinyTag.get(os.fspath(path)))
