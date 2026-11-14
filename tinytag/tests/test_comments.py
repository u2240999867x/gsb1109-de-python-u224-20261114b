# SPDX-FileCopyrightText: 2026 tinytag Contributors
# SPDX-License-Identifier: MIT

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=missing-module-docstring
# pyright: reportPrivateUsage=false

from __future__ import annotations

from io import BytesIO
from unittest import TestCase

from tinytag import Comment, CommentView, TinyTag


def _synchsafe(value: int) -> bytes:
    return bytes((
        (value >> 21) & 0x7F,
        (value >> 14) & 0x7F,
        (value >> 7) & 0x7F,
        value & 0x7F,
    ))


def _id3v2(frames: list[tuple[bytes, bytes]], major: int = 3,
           declared_sizes: list[int] | None = None,
           tag_size: int | None = None) -> bytes:
    body_parts = []
    for index, (frame_id, content) in enumerate(frames):
        declared = (
            declared_sizes[index]
            if declared_sizes is not None else len(content))
        body_parts.append(
            frame_id + declared.to_bytes(4, 'big') + b'\x00\x00' + content)
    body = b''.join(body_parts)
    size = len(body) if tag_size is None else tag_size
    return b'ID3\x03\x00\x00' + _synchsafe(size) + body


def _comm(text: bytes, language: bytes = b'fra',
          description: bytes = b'', encoding: int = 0x00) -> bytes:
    return bytes((encoding,)) + language + description + b'\x00' + text


def _id3v1(text: bytes, track: int | None = None,
           genre: int = 255) -> bytes:
    if track is not None:
        assert len(text) <= 28
        comment = text.ljust(28, b'\x00') + b'\x00' + bytes((track,))
    else:
        assert len(text) <= 30
        comment = text.ljust(30, b'\x00')
    return (b'TAG' + b'Title'.ljust(30, b'\x00')
            + b'Artist'.ljust(30, b'\x00')
            + b'Album'.ljust(30, b'\x00')
            + b'1999' + comment + bytes((genre,)))


def _mp3(v1: bytes | None = None, v2: bytes | None = None) -> BytesIO:
    # A single valid MPEG-1 Layer III frame keeps the duration parser happy.
    frame = b'\xff\xfb\x90\x00' + b'\x00' * 413
    return BytesIO((v2 or b'') + frame * 3 + (v1 or b''))


def _tag(v1: bytes | None = None, v2: bytes | None = None) -> TinyTag:
    return TinyTag.get(file_obj=_mp3(v1, v2), duration=False)


class TestCommentView(TestCase):

    def test_id3v1_track_not_merged_into_text(self) -> None:
        tag = _tag(_id3v1(b'Old comment'.ljust(28, b'\x00'), track=7))
        candidate = tag.comment_candidates[0]
        self.assertEqual(candidate.text, 'Old comment')
        self.assertEqual(candidate.track, 7)
        self.assertEqual(tag.comment, 'Old comment')
        self.assertEqual(tag.track, 7)
        self.assertFalse(candidate.truncated)

    def test_short_comment_is_not_truncated(self) -> None:
        tag = _tag(_id3v1(b'Hi', track=None))
        candidate = tag.comment_candidates[0]
        assert candidate.offset is not None
        self.assertEqual(candidate.text, 'Hi')
        self.assertFalse(candidate.truncated)
        self.assertEqual(candidate.evidence['terminator'],
                         candidate.offset + 2)
        self.assertEqual(candidate.evidence['field_full'], False)

    def test_full_comment_area_is_not_truncated(self) -> None:
        full = b'0123456789abcdefghijklmnopqrst'
        tag = _tag(_id3v1(full, track=None))
        candidate = tag.comment_candidates[0]
        assert candidate.offset is not None
        self.assertEqual(candidate.text, full.decode())
        self.assertFalse(candidate.truncated)
        self.assertTrue(candidate.evidence['field_full'])
        self.assertEqual(
            candidate.evidence['comment_field'],
            [candidate.offset, candidate.offset + 30])

    def test_track_byte_not_merged_without_v11(self) -> None:
        # ID3v1.0 uses the full 30 bytes, so a printable byte after the NUL
        # must also be offered as a v1.0 reading instead of being dropped.
        text = b'Old comment fillsthirtytwo..'[:28]
        tag = _tag(_id3v1(text, track=65))
        candidate = tag.comment_candidates[0]
        v10 = next(
            reading for reading in candidate.readings
            if reading['version'] == 'id3v1.0')
        self.assertEqual(v10['text'], text.decode() + '\x00A')
        self.assertEqual(v10['track'], None)
        self.assertEqual(candidate.text, text.decode())
        self.assertNotIn('A', candidate.text)
        self.assertTrue(candidate.ambiguous)

    def test_track_is_kept_out_of_comment_text(self) -> None:
        tag = _tag(_id3v1(b'Old comment', track=7))
        candidate = tag.comment_candidates[0]
        assert candidate.offset is not None
        self.assertEqual(candidate.track, 7)
        self.assertNotIn('\x07', candidate.text)
        self.assertEqual(
            candidate.evidence['track_bytes'],
            [candidate.offset + 28, candidate.offset + 30])

    def test_ambiguous_v10_v11_report_both_readings(self) -> None:
        full = b'0123456789abcdefghijklmnopqr'
        # 28 characters plus two NUL bytes: either a v1.1 tag with track 0
        # or v1.0 padding.  The bytes alone cannot distinguish the versions.
        tag = _tag(_id3v1(full, track=None))
        candidate = tag.comment_candidates[0]
        self.assertTrue(candidate.ambiguous)
        self.assertEqual(candidate.version, 'unknown')
        self.assertEqual(len(candidate.readings), 2)
        self.assertEqual(
            [reading['version'] for reading in candidate.readings],
            ['id3v1.1', 'id3v1.0'])
        view = tag.comment_view()
        self.assertFalse(view.ambiguous)
        self.assertTrue(view.version_ambiguous)
        self.assertEqual(view.text, full.decode())
        self.assertEqual(len(view.readings), 2)
        for reading in view.readings:
            self.assertTrue(reading['bytes'])
        self.assertEqual(view.readings[0]['text'], full.decode())
        self.assertEqual(view.readings[1]['text'], full.decode())

    def test_prefers_requested_language_and_keeps_all(self) -> None:
        tag = _tag(v2=_id3v2([
            (b'COMM', _comm(b'French note', b'fra')),
            (b'COMM', _comm(b'English note', b'eng')),
        ]))
        all_texts = [c.text for c in tag.comment_candidates]
        self.assertEqual(all_texts, ['French note', 'English note'])
        view = tag.comment_view(language='eng')
        self.assertEqual(view.text, 'English note')
        self.assertEqual(
            [c.text for c in view.candidates],
            ['French note', 'English note'])
        self.assertTrue(view.matched)
        # ISO 639-1 and legacy bibliographic codes both resolve.
        self.assertEqual(tag.comment_view(language='fr').text, 'French note')
        self.assertEqual(tag.comment_view(language='fre').text, 'French note')

    def test_preferred_source_wins_without_merging(self) -> None:
        v1 = _id3v1(b'Archived comment', track=3)
        tag = _tag(v1=v1, v2=_id3v2([
            (b'COMM', _comm(b'Nouvelle note', b'fra')),
        ]))
        self.assertEqual(
            [c.source for c in tag.comment_candidates],
            ['id3v2.3', 'id3v1'])
        view = tag.comment_view(source='id3v1')
        self.assertEqual(view.text, 'Archived comment')
        self.assertEqual(len(view.candidates), 2)
        self.assertEqual(view.candidates[0].text, 'Nouvelle note')

    def test_v1_and_v2_disagreement_is_not_resolved_by_order(self) -> None:
        v1 = _id3v1(b'Old comment', track=None)
        tag = _tag(v1=v1, v2=_id3v2([
            (b'COMM', _comm(b'New comment', b'fra')),
        ]))
        view = tag.comment_view()
        self.assertIsNone(view.text)
        self.assertTrue(view.ambiguous)
        self.assertEqual(
            sorted(c.text for c in view.alternatives),
            ['New comment', 'Old comment'])
        self.assertEqual(
            sorted(reading['text'] for reading in view.readings),
            ['New comment', 'Old comment'])
        self.assertEqual(tag.comment, 'New comment')

    def test_description_preference_keeps_technical_frame(self) -> None:
        tag = _tag(v2=_id3v2([
            (b'COMM', _comm(b'plain', b'eng')),
            (b'COMM', _comm(b' 0000044E', b'eng', b'iTunNORM')),
        ]))
        self.assertEqual(tag.comment, 'plain')
        view = tag.comment_view(description='iTunNORM')
        self.assertEqual(view.text, ' 0000044E')
        self.assertEqual(len(view.candidates), 2)

    def test_unknown_preference_is_reported_not_silently_substituted(
        self
    ) -> None:
        tag = _tag(v2=_id3v2([
            (b'COMM', _comm(b'Bonjour', b'fra')),
        ]))
        view = tag.comment_view(language='deu')
        self.assertFalse(view.matched)
        # An unmatched preference never looks like an exact match.
        self.assertIsNone(view.text)
        self.assertEqual(
            [c.text for c in view.candidates], ['Bonjour'])

    def test_empty_view_reports_unmatched_preference(self) -> None:
        tag = _tag()
        self.assertFalse(tag.comment_view(language='fra').matched)
        self.assertTrue(tag.comment_view().matched)
        self.assertEqual(tag.comment_candidates, [])

    def test_raw_bytes_are_preserved(self) -> None:
        tag = _tag(v1=_id3v1(b'Old comment', track=5),
                   v2=_id3v2([(b'COMM', _comm(b'Note', b'eng'))]))
        for candidate in tag.comment_candidates:
            self.assertTrue(candidate.raw)
            self.assertEqual(candidate.size, len(candidate.raw))
            byte_range = candidate.byte_range
            assert byte_range is not None
            start, end = byte_range
            self.assertEqual(end - start, candidate.size)
            self.assertTrue(
                candidate.source.startswith('id3v1')
                or candidate.evidence['frame_id'] == 'COMM')

    def test_view_is_serializable(self) -> None:
        tag = _tag(v1=_id3v1(b'Old', track=2))
        view = tag.comment_view()
        data = view.as_dict()
        self.assertEqual(data['text'], 'Old')
        self.assertFalse(data['ambiguous'])
        candidates = data['candidates']
        readings = data['readings']
        assert isinstance(candidates, list)
        assert isinstance(readings, list)
        self.assertEqual(candidates[0]['text'], 'Old')
        self.assertEqual(readings[0]['track'], 2)
        self.assertIsInstance(data['readings'], list)

    def test_comment_classes_are_public(self) -> None:
        self.assertIsInstance(Comment('x', 'id3v1'), Comment)
        self.assertIsInstance(
            CommentView.select([]), CommentView)

    def test_truncated_comm_frame_reports_bytes(self) -> None:
        # The frame declares 40 bytes, but the file ends after 17 of them.
        content = _comm(b'Cut off', b'fra')
        data = _id3v2([(b'COMM', content)], declared_sizes=[40],
                      tag_size=60)
        tag = TinyTag.get(file_obj=BytesIO(data), duration=False)
        candidate = tag.comment_candidates[0]
        assert candidate.offset is not None
        self.assertTrue(candidate.truncated)
        self.assertEqual(candidate.evidence['declared_size'], 40)
        self.assertEqual(candidate.evidence['available_size'], len(content))
        truncation = candidate.evidence['truncation_bytes']
        assert isinstance(truncation, list)
        self.assertEqual(truncation[1] - candidate.offset, 40)

    def test_short_comm_frame_keeps_evidence(self) -> None:
        tag = _tag(v2=_id3v2([(b'COMM', b'\x00fr')]))
        self.assertFalse(tag.comment_candidates[0].text)
        self.assertEqual(tag.comment_view().text, None)
