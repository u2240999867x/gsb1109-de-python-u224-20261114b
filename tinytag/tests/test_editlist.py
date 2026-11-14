# SPDX-FileCopyrightText: 2014-2026 tinytag Contributors
# SPDX-License-Identifier: MIT

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=missing-module-docstring

from __future__ import annotations

import os.path
import struct

from io import BytesIO
from tempfile import TemporaryDirectory
from unittest import TestCase

from tinytag import (
    SUPPORTED_MEDIA_RATES, TinyTag, EditList, EditRecord, EditTrack,
    parse_edit_list, parse_elst, parse_elst_entries, read_edit_list
)

SAMPLE_FOLDER = os.path.join(os.path.dirname(__file__), 'samples')
MOVIE_TIMESCALE = 1000
AUDIO_TIMESCALE = 48000
VIDEO_TIMESCALE = 90000


def atom(atom_type: bytes, payload: bytes = b'') -> bytes:
    return struct.pack('>I', 8 + len(payload)) + atom_type + payload


def full_atom(atom_type: bytes, payload: bytes = b'') -> bytes:
    return atom(atom_type, b'\x00\x00\x00\x00' + payload)


def mvhd(timescale: int, duration: int) -> bytes:
    return full_atom(
        b'mvhd',
        struct.pack('>IIII', 0, 0, timescale, duration) + b'\x00' * 80)


def tkhd(track_id: int) -> bytes:
    return full_atom(
        b'tkhd',
        struct.pack('>IIIII', 0, 0, track_id, 0, 0) + b'\x00' * 60)


def mdhd(timescale: int, duration: int) -> bytes:
    return full_atom(
        b'mdhd',
        struct.pack('>IIII', 0, 0, timescale, duration) + b'\x00' * 4)


def hdlr(handler_type: bytes) -> bytes:
    return full_atom(
        b'hdlr', struct.pack('>I', 0) + handler_type + b'\x00' * 12)


def elst(entries, version: int = 0) -> bytes:
    payload = struct.pack('>B3xI', version, len(entries))
    for duration, media_time, rate_integer, rate_fraction in entries:
        if version == 0:
            payload += struct.pack(
                '>IiHH', duration, media_time, rate_integer, rate_fraction)
        else:
            payload += struct.pack(
                '>QqHH', duration, media_time, rate_integer, rate_fraction)
    return atom(b'elst', payload)


def stts(sample_count: int, delta: int) -> bytes:
    return full_atom(
        b'stts', struct.pack('>III', 1, sample_count, delta))


def stsd() -> bytes:
    return full_atom(b'stsd', b'\x00\x00\x00\x01')


def trak(track_id: int,
         timescale: int,
         duration: int,
         handler_type: bytes,
         entries,
         sample_delta: int,
         sample_count: int) -> bytes:
    edts = atom(b'edts', elst(entries)) if entries is not None else b''
    minf = atom(b'minf', atom(
        b'stbl', stsd() + stts(sample_count, sample_delta)))
    mdia = atom(
        b'mdia',
        mdhd(timescale, duration) + hdlr(handler_type) + minf)
    return atom(b'trak', tkhd(track_id) + edts + mdia)


def make_mp4(audio_entries,
             video_entries=None,
             movie_timescale: int = MOVIE_TIMESCALE) -> bytes:
    duration = sum(entry[0] for entry in (audio_entries or []))
    tracks = []
    if video_entries is not None:
        tracks.append(trak(
            1, VIDEO_TIMESCALE, duration * VIDEO_TIMESCALE // 1000,
            b'vide', video_entries, 3000, duration // 1000 * 30))
    tracks.append(trak(
        2, AUDIO_TIMESCALE, duration * AUDIO_TIMESCALE // 1000,
        b'soun', audio_entries, 1024, duration * AUDIO_TIMESCALE // 1000
        // 1024))
    moov = atom(b'moov', mvhd(movie_timescale, duration) + b''.join(tracks))
    return (atom(b'ftyp', b'M4A \x00\x00\x02\x00M4A isomiso2')
            + atom(b'mdat', b'\x00' * 64) + moov)


SHOW_ENTRIES = [
    (2000, -1, 1, 0),                 # opening silence
    (16000, 0, 1, 0),                 # first showing: media 0..16 s
    (9500, 4 * AUDIO_TIMESCALE, 1, 0)  # replay from 4 s at 18 s
]
SHOW_VIDEO_ENTRIES = [
    (27500, -1, 1, 0),
    (16000, 0, 1, 0),
    (9500, 4 * VIDEO_TIMESCALE, 1, 0),
]


class TestEditList(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.show = make_mp4(SHOW_ENTRIES, SHOW_VIDEO_ENTRIES)
        cls.single = make_mp4([(524, 0, 1, 0)])
        cls.classical = os.path.join(SAMPLE_FOLDER, 'classical.m4a')

    def get_show(self) -> TinyTag:
        return TinyTag.get(file_obj=BytesIO(self.show))

    def test_expanded_show_table(self) -> None:
        edit_list = self.get_show().edit_list
        assert edit_list is not None
        self.assertEqual(len(edit_list), 3)
        self.assertEqual(edit_list.movie_timescale, MOVIE_TIMESCALE)
        silence, first, replay = edit_list
        self.assertEqual(silence.presentation_range, (0.0, 2.0))
        self.assertEqual(silence.media_range, (None, None))
        self.assertTrue(silence.empty)
        self.assertEqual(silence.media_sample_count, None)
        self.assertEqual(first.presentation_range, (2.0, 18.0))
        self.assertEqual(first.media_range, (0.0, 16.0))
        self.assertEqual(first.sample_range, (0, 749))
        self.assertEqual(replay.presentation_range, (18.0, 27.5))
        self.assertEqual(replay.media_range, (4.0, 13.5))
        self.assertEqual(replay.sample_range, (187, 632))
        self.assertEqual(replay.media_sample_count, 446)

    def test_movie_and_track_units_are_kept(self) -> None:
        edit_list = self.get_show().edit_list
        assert edit_list is not None
        first = edit_list[1]
        self.assertEqual(first.movie_timescale, MOVIE_TIMESCALE)
        self.assertEqual(first.media_timescale, AUDIO_TIMESCALE)
        self.assertEqual(first.segment_duration, 16000)
        self.assertEqual(first.media_time, 0)
        self.assertEqual(first.presentation_range_ticks, (2000, 18000))
        self.assertEqual(first.media_range_ticks, (0, 768000.0))
        self.assertAlmostEqual(first.media_end_ticks, 768000.0)

    def test_table_records_are_flat_dicts(self) -> None:
        edit_list = self.get_show().edit_list
        assert edit_list is not None
        table = edit_list.to_table()
        self.assertEqual(len(table), 3)
        self.assertTrue(all(isinstance(row, dict) for row in table))
        self.assertEqual(
            table[0]['presentation_start'], 0.0)
        self.assertEqual(table[0]['empty'], True)
        self.assertEqual(table[2]['media_start'], 4.0)
        self.assertEqual(table[2]['media_sample_start'], 187)
        self.assertEqual(table[2]['media_rate'], 1.0)
        self.assertEqual(table[2]['supported'], True)
        self.assertEqual(table[2]['unexplained'], False)

    def test_presentation_lookup_and_opening_silence(self) -> None:
        edit_list = self.get_show().edit_list
        assert edit_list is not None
        self.assertEqual(
            [record.index for record in edit_list.records_at(1.0)], [0])
        self.assertTrue(edit_list.silence_at(1.0))
        self.assertIsNone(edit_list.media_position(1.0))
        self.assertIsNone(edit_list.media_sample(1.0))
        self.assertFalse(edit_list.silence_at(3.0))
        self.assertAlmostEqual(edit_list.media_position(3.0), 1.0)
        self.assertIsNotNone(edit_list.media_sample(3.0))

    def test_source_range_finds_both_showings(self) -> None:
        edit_list = self.get_show().edit_list
        assert edit_list is not None
        matches = edit_list.records_for_media(4.0, 13.5)
        self.assertEqual([record.index for record in matches], [1, 2])
        self.assertEqual(
            edit_list.presentations_for_media(4.0, 13.5),
            [(2.0, 18.0), (18.0, 27.5)])
        self.assertEqual(
            edit_list.media_to_presentation(4.0, 13.5),
            [(2.0, 18.0), (18.0, 27.5)])
        # The replay is a subset of the first showing, so both match.
        self.assertEqual(len(edit_list.records_for_media(4.0, 8.0)), 2)
        # A range after the replay's source window matches only the first.
        self.assertEqual(
            [record.index for record in
             edit_list.records_for_media(14.0, 15.0)],
            [1])

    def test_source_sample_reverse_lookup(self) -> None:
        edit_list = self.get_show().edit_list
        assert edit_list is not None
        self.assertEqual(
            [record.index for record in
             edit_list.records_for_media_samples(200, 300)],
            [1, 2])
        self.assertEqual(
            [record.index for record in
             edit_list.records_for_media_samples(740, 749)],
            [1])

    def test_video_track_keeps_its_own_timescale(self) -> None:
        edit_list = self.get_show().edit_list
        assert edit_list is not None
        self.assertEqual(len(edit_list.tracks), 2)
        video = edit_list.tracks[0]
        self.assertEqual(video.handler_type, 'vide')
        self.assertEqual(video.media_type, 'video')
        self.assertEqual(video.media_timescale, VIDEO_TIMESCALE)
        self.assertEqual(len(video), 3)
        self.assertEqual(video[1].media_range_ticks, (0, 1440000.0))
        audio = edit_list.tracks[1]
        self.assertEqual(audio.media_timescale, AUDIO_TIMESCALE)
        self.assertEqual(edit_list.primary_track, audio)

    def test_unsupported_rate_is_recorded_not_hidden(self) -> None:
        data = make_mp4([(2000, -1, 1, 0), (10000, 0, 2, 0)])
        edit_list = TinyTag.get(file_obj=BytesIO(data)).edit_list
        assert edit_list is not None
        slow = edit_list[1]
        self.assertEqual(slow.media_rate, 2.0)
        self.assertFalse(slow.rate_supported)
        self.assertFalse(slow.supported)
        self.assertIn('unsupported media rate', slow.reason or '')
        self.assertFalse(edit_list.supported)
        self.assertEqual(len(edit_list.unsupported_records), 1)
        self.assertTrue(edit_list[0].supported)

    def test_dwell_rate_is_supported(self) -> None:
        data = make_mp4([(2000, -1, 1, 0), (5000, 0, 0, 0)])
        edit_list = TinyTag.get(file_obj=BytesIO(data)).edit_list
        assert edit_list is not None
        hold = edit_list[1]
        self.assertTrue(hold.rate_supported)
        self.assertIn(hold.media_rate, SUPPORTED_MEDIA_RATES)
        self.assertEqual(hold.media_range, (0.0, 1024 / AUDIO_TIMESCALE))

    def test_unexplained_edits_keep_a_reason(self) -> None:
        edit_list = TinyTag.get(file_obj=BytesIO(self.single)).edit_list
        assert edit_list is not None
        truncated = elst([(524, 0, 1, 0), (1000, 0, 1, 0)])
        # Drop part of the second entry, leaving a truncated atom.
        parsed = parse_elst(
            truncated[8:-5], movie_timescale=MOVIE_TIMESCALE,
            media_timescale=AUDIO_TIMESCALE)
        self.assertEqual(len(parsed), 2)
        self.assertFalse(parsed[0].unexplained)
        self.assertTrue(parsed[1].unexplained)
        self.assertFalse(parsed[1].supported)
        self.assertIn('truncated edit list', parsed[1].reason or '')

        unknown = parse_elst(
            b'\x02' + b'\x00' * 3 + struct.pack('>I', 0),
            movie_timescale=MOVIE_TIMESCALE, media_timescale=AUDIO_TIMESCALE)
        self.assertTrue(unknown[0].unexplained)
        self.assertIn('unsupported edit list version',
                      unknown[0].reason or '')

    def test_version_one_entries(self) -> None:
        payload = elst([(524, 0, 1, 0)], version=1)
        parsed = parse_elst(
            payload[8:], movie_timescale=MOVIE_TIMESCALE,
            media_timescale=AUDIO_TIMESCALE)
        self.assertEqual(parsed[0].segment_duration, 524)
        self.assertEqual(parsed[0].media_time, 0)
        self.assertEqual(parsed[0].presentation_range, (0.0, 0.524))

    def test_missing_media_timescale_is_explained(self) -> None:
        edit_list = parse_edit_list(
            elst([(1000, 0, 1, 0)])[8:],
            movie_timescale=MOVIE_TIMESCALE, media_timescale=0)
        self.assertTrue(edit_list[0].unexplained)
        self.assertIn('media timescale is missing',
                      edit_list[0].reason or '')

    def test_empty_edit_entry_parser(self) -> None:
        entries = parse_elst_entries(elst(
            [(2000, -1, 1, 0), (16000, 0, 1, 0)])[8:])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['media_time'], -1)
        self.assertEqual(entries[1]['media_rate'], 1.0)
        self.assertNotIn('unexplained', entries[0])

    def test_edit_list_without_elst_is_none(self) -> None:
        data = make_mp4(None, video_entries=None)
        tag = TinyTag.get(file_obj=BytesIO(data))
        self.assertIsNone(tag.edit_list)
        self.assertEqual(tag.edit_list_table, [])

    def test_out_of_range_media_time_is_explained(self) -> None:
        data = make_mp4([(2000, 20 * AUDIO_TIMESCALE, 1, 0)])
        edit_list = TinyTag.get(file_obj=BytesIO(data)).edit_list
        assert edit_list is not None
        self.assertTrue(edit_list[0].unexplained)
        self.assertIn('past the track duration', edit_list[0].reason or '')

    def test_existing_fixture_still_parses(self) -> None:
        tag = TinyTag.get(self.classical)
        assert tag.edit_list is not None
        self.assertEqual(tag.edit_list[0].presentation_range, (0.0, 0.524))
        self.assertEqual(tag.edit_list[0].media_range, (0.0, 0.524))
        self.assertEqual(tag.edit_list[0].media_sample_count, 23)
        self.assertTrue(tag.edit_list[0].supported)

    def test_read_edit_list_from_file_object(self) -> None:
        edit_list = read_edit_list(BytesIO(self.show))
        assert isinstance(edit_list, EditList)
        self.assertEqual(len(edit_list), 3)
        self.assertIsInstance(edit_list[0], EditRecord)
        self.assertIsInstance(edit_list.tracks[0], EditTrack)

    def test_index_and_track_lookup(self) -> None:
        edit_list = self.get_show().edit_list
        assert edit_list is not None
        self.assertEqual(edit_list.record_for_index(2).media_start, 4.0)
        self.assertIsNone(edit_list.record_for_index(9))
        self.assertEqual(edit_list.track(2).handler_type, 'soun')
        self.assertEqual(edit_list.track(1).handler_type, 'vide')
        self.assertEqual(edit_list.media_index_at(19.0), 2)
        self.assertIsNone(edit_list.media_index_at(30.0))

    def test_duration_summaries(self) -> None:
        edit_list = self.get_show().edit_list
        assert edit_list is not None
        self.assertAlmostEqual(edit_list.duration, 27.5)
        self.assertAlmostEqual(edit_list.media_duration, 25.5)
        self.assertAlmostEqual(edit_list.silent_duration, 2.0)
        self.assertEqual(edit_list.coverage, (0.0, 27.5))
        self.assertEqual(edit_list.validate(), [])
        self.assertEqual(edit_list.primary_track, edit_list.primary)
        self.assertEqual(edit_list.track_count, 2)

    def test_table_helper_accepts_paths_and_atoms(self) -> None:
        from tinytag import edit_list_table, expand_edit_list
        table = edit_list_table(self.classical)
        self.assertEqual(table[0]['presentation_range']
                         if 'presentation_range' in table[0]
                         else (table[0]['presentation_start'],
                               table[0]['presentation_end']),
                         (0.0, 0.524))
        atom = elst([(2000, -1, 1, 0)])
        expanded = expand_edit_list(atom)
        assert expanded is not None
        self.assertTrue(expanded[0].empty)
        self.assertEqual(expand_edit_list(b'not an mp4'), None)

    def test_record_mapping_access(self) -> None:
        edit_list = read_edit_list(BytesIO(self.show))
        assert edit_list is not None
        record = edit_list[2]
        self.assertEqual(record['media_start'], 4.0)
        self.assertEqual(record.get('empty'), False)
        self.assertIn('presentation_end', record.keys())
        self.assertEqual(dict(record.items())['media_rate'], 1.0)
        self.assertEqual(record.as_dict(), record)

    def test_fixture_files_on_disk(self) -> None:
        fixtures = {
            'show.mp4': (SHOW_ENTRIES, SHOW_VIDEO_ENTRIES),
            'silence_only.mp4': ([(2000, -1, 1, 0)], None),
            'unsupported_rate.mp4': ([(2000, -1, 1, 0),
                                      (10000, 0, 2, 0)], None),
        }
        with TemporaryDirectory() as folder:
            for name, (audio, video) in fixtures.items():
                with open(os.path.join(folder, name), 'wb') as file_handle:
                    file_handle.write(make_mp4(audio, video))
            tag = TinyTag.get(os.path.join(folder, 'show.mp4'))
            assert tag.edit_list is not None
            self.assertEqual(len(tag.edit_list), 3)
            self.assertEqual(len(tag.edit_list.tracks), 2)
            silence = TinyTag.get(os.path.join(folder, 'silence_only.mp4'))
            assert silence.edit_list is not None
            self.assertTrue(silence.edit_list[0].empty)
            self.assertIsNone(silence.edit_list.media_position(0.1))
            slowed = TinyTag.get(os.path.join(folder, 'unsupported_rate.mp4'))
            assert slowed.edit_list is not None
            self.assertFalse(slowed.edit_list[1].supported)
            self.assertIn('unsupported media rate',
                          slowed.edit_list[1].reason or '')
