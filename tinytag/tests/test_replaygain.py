# SPDX-FileCopyrightText: 2026 tinytag Contributors
# SPDX-License-Identifier: MIT

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=missing-module-docstring

from __future__ import annotations

from math import log10
from unittest import TestCase

from tinytag import (
    STANDARD_REFERENCE_LOUDNESS_DB, TinyTag,
    compute_album_gain, format_report, parse_gain_db, parse_peak,
    track_tags_from_tinytag
)
from tinytag.replaygain import TrackTags


def _headroom(peak):
    return -20.0 * log10(peak)


class TestParsing(TestCase):

    def test_parse_gain_db(self):
        self.assertEqual(parse_gain_db('-3.95 dB'), -3.95)
        self.assertEqual(parse_gain_db('+2.5 dB'), 2.5)
        self.assertEqual(parse_gain_db('-8.26'), -8.26)
        self.assertEqual(parse_gain_db(' -1.0 DB '), -1.0)

    def test_parse_peak(self):
        self.assertEqual(parse_peak('0.999969'), 0.999969)
        self.assertEqual(parse_peak(' 1.50579047 '), 1.50579047)

    def test_track_tags_from_tinytag(self):
        tag = TinyTag()
        tag.title = 'Some Song'
        tag.other['replaygain_track_gain'] = ['-3.95 dB']
        tag.other['replaygain_track_peak'] = ['0.999969']
        tag.other['replaygain_album_gain'] = ['-8.26 dB']
        tag.other['replaygain_album_peak'] = ['1.0000']
        tags = track_tags_from_tinytag(tag)
        self.assertEqual(tags.title, 'Some Song')
        self.assertEqual(tags.track_gain_db, -3.95)
        self.assertEqual(tags.track_peak, 0.999969)
        self.assertEqual(tags.album_gain_db, -8.26)
        self.assertEqual(tags.album_peak, 1.0000)

    def test_track_tags_from_tinytag_missing(self):
        tag = TinyTag()
        tags = track_tags_from_tinytag(tag, title='Fallback')
        self.assertEqual(tags.title, 'Fallback')
        self.assertIsNone(tags.track_gain_db)
        self.assertIsNone(tags.track_peak)
        self.assertIsNone(tags.album_gain_db)
        self.assertIsNone(tags.album_peak)


class TestAlbumGain(TestCase):

    def test_suggested_usable_and_limiting_track(self):
        tracks = [
            TrackTags(title='quiet one', track_gain_db=-8.0,
                      track_peak=0.5, album_gain_db=-2.0),
            TrackTags(title='loud one', track_gain_db=-10.0,
                      track_peak=1.5, album_gain_db=-2.0),
            TrackTags(title='quiet two', track_gain_db=-7.0,
                      track_peak=0.4, album_gain_db=-2.0),
        ]
        report = compute_album_gain(tracks)
        self.assertEqual(report.suggested_album_gains_db, (-2.0,))
        self.assertTrue(report.album_gains_agree)
        self.assertEqual(report.limiting_track, 'loud one')
        self.assertAlmostEqual(
            report.peak_limited_gain_db, _headroom(1.5))
        self.assertEqual(len(report.usable_album_gains_db), 1)
        self.assertAlmostEqual(
            report.usable_album_gains_db[0], _headroom(1.5))
        self.assertTrue(report.clip_free_guaranteed)
        limited = [t for t in report.tracks if t.limits_album_gain]
        self.assertEqual([t.title for t in limited], ['loud one'])

    def test_quiet_tracks_do_not_cancel_loud_peak(self):
        tracks = [
            TrackTags(title='whisper a', track_peak=0.5,
                      album_gain_db=-1.0),
            TrackTags(title='whisper b', track_peak=0.5,
                      album_gain_db=-1.0),
            TrackTags(title='hot take', track_peak=1.2,
                      album_gain_db=-1.0),
        ]
        report = compute_album_gain(tracks)
        # an average peak (~0.73) would leave headroom for -1.0 dB,
        # but the single loudest peak must impose the limit
        self.assertEqual(report.limiting_track, 'hot take')
        self.assertAlmostEqual(
            report.usable_album_gains_db[0], _headroom(1.2))
        self.assertLess(report.usable_album_gains_db[0], -1.0)

    def test_conflicting_album_gains_are_kept(self):
        tracks = [
            TrackTags(title='pressing a', track_peak=0.9,
                      album_gain_db=-6.0),
            TrackTags(title='pressing b', track_peak=0.9,
                      album_gain_db=-8.5),
        ]
        report = compute_album_gain(tracks)
        self.assertFalse(report.album_gains_agree)
        self.assertEqual(report.suggested_album_gains_db, (-6.0, -8.5))
        self.assertEqual(len(report.usable_album_gains_db), 2)
        headroom = _headroom(0.9)
        self.assertAlmostEqual(
            report.usable_album_gains_db[0], min(-6.0, headroom))
        self.assertAlmostEqual(
            report.usable_album_gains_db[1], min(-8.5, headroom))

    def test_missing_peak_lists_suggestions_without_guarantee(self):
        tracks = [
            TrackTags(title='tagged', track_gain_db=-4.0,
                      track_peak=0.9, album_gain_db=-5.0),
            TrackTags(title='untagged', track_gain_db=-3.0,
                      album_gain_db=-5.0),
        ]
        report = compute_album_gain(tracks)
        self.assertEqual(report.suggested_album_gains_db, (-5.0,))
        self.assertIsNone(report.usable_album_gains_db)
        self.assertFalse(report.clip_free_guaranteed)
        text = format_report(report)
        self.assertIn('-5.00 dB', text)
        self.assertIn('clipping cannot be ruled out', text)

    def test_reference_loudness_shifts_gains(self):
        tracks = [
            TrackTags(title='song', track_gain_db=-3.0,
                      track_peak=0.8, album_gain_db=-6.0),
        ]
        report = compute_album_gain(
            tracks, reference_loudness_db=STANDARD_REFERENCE_LOUDNESS_DB - 10)
        self.assertEqual(report.suggested_album_gains_db, (-16.0,))
        self.assertEqual(report.tracks[0].suggested_track_gain_db, -13.0)
        self.assertAlmostEqual(
            report.usable_album_gains_db[0],
            min(-16.0, _headroom(0.8)))

    def test_track_level_usable_gain(self):
        tracks = [
            TrackTags(title='clipping', track_gain_db=1.0,
                      track_peak=1.0, album_gain_db=-2.0),
            TrackTags(title='silent', track_gain_db=5.0,
                      track_peak=0.0, album_gain_db=-2.0),
            TrackTags(title='unknown', track_gain_db=-1.0),
        ]
        report = compute_album_gain(tracks)
        clipping, silent, unknown = report.tracks
        self.assertEqual(clipping.usable_track_gain_db, 0.0)
        self.assertEqual(silent.usable_track_gain_db, 5.0)
        self.assertIsNone(unknown.usable_track_gain_db)

    def test_no_album_gain_tags(self):
        report = compute_album_gain([TrackTags(title='bare')])
        self.assertEqual(report.suggested_album_gains_db, ())
        self.assertTrue(report.album_gains_agree)
        self.assertIsNone(report.limiting_track)
        self.assertIn('<no album gain tags>', format_report(report))

    def test_format_report_names_limiter(self):
        tracks = [
            TrackTags(title='loud one', track_peak=1.5,
                      album_gain_db=-2.0),
        ]
        text = format_report(compute_album_gain(tracks))
        self.assertIn('limited by track: loud one', text)
        self.assertIn('clip-free guarantee: yes', text)
