"""Tests for the alignment of a transcript with captions."""

import dataclasses
import datetime
import unittest

from webvtt import Caption
from webvtt.align import (
    Alignment,
    Anchor,
    TranscriptWord,
    _equivalence_groups,
    _forced_pairs,
    _temporal_match,
    align_transcript,
    caption_tokens,
    )
from webvtt.errors import MalformedCaptionError
from webvtt.models import Timestamp


def word(text, start, end):
    """Return a transcript word with times in milliseconds."""
    return TranscriptWord(text, start, end)


def caption(start, end, text):
    """Return a caption with times in seconds."""
    return Caption(start / 1000, end / 1000, text)


class TestTranscriptWord(unittest.TestCase):

    def test_instantiation(self):
        transcript_word = TranscriptWord('roger', 1000, 1200)
        self.assertEqual(transcript_word.text, 'roger')
        self.assertEqual(transcript_word.start, 1000)
        self.assertEqual(transcript_word.end, 1200)

    def test_is_frozen(self):
        transcript_word = TranscriptWord('roger', 1000, 1200)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            transcript_word.start = 500

    def test_rejects_float_times(self):
        with self.assertRaises(TypeError):
            TranscriptWord('roger', 1.0, 1200)

    def test_rejects_negative_start(self):
        with self.assertRaises(ValueError):
            TranscriptWord('roger', -1, 1200)

    def test_rejects_end_before_start(self):
        with self.assertRaises(ValueError):
            TranscriptWord('roger', 1200, 1000)

    def test_zero_length_word(self):
        transcript_word = TranscriptWord('roger', 1000, 1000)
        self.assertEqual(transcript_word.start, transcript_word.end)

    def test_from_times_with_strings(self):
        transcript_word = TranscriptWord.from_times(
            'roger', '00:00:01.000', '00:00:01.200'
            )
        self.assertEqual(transcript_word,
                         TranscriptWord('roger', 1000, 1200))

    def test_from_times_with_seconds_and_timedelta(self):
        transcript_word = TranscriptWord.from_times(
            'roger', 1.0, datetime.timedelta(milliseconds=1200)
            )
        self.assertEqual((transcript_word.start, transcript_word.end),
                         (1000, 1200))

    def test_from_times_with_timestamp(self):
        transcript_word = TranscriptWord.from_times(
            'roger',
            Timestamp(0, 0, 1, 0),
            Timestamp(0, 0, 1, 200)
            )
        self.assertEqual((transcript_word.start, transcript_word.end),
                         (1000, 1200))

    def test_from_times_rejects_invalid_time(self):
        with self.assertRaises(MalformedCaptionError):
            TranscriptWord.from_times('roger', 'not a time', 1200)


class TestCaptionTokens(unittest.TestCase):

    def test_splits_and_normalizes_words(self):
        self.assertEqual(
            caption_tokens(caption(0, 1000, 'Hello, world!')),
            ('hello', 'world')
            )

    def test_strips_cue_tags_and_voice_spans(self):
        cue = Caption(0, 1, '<v Speaker>收到</v> 收到')
        self.assertEqual(caption_tokens(cue), ('收到', '收到'))

    def test_keeps_cjk_letters(self):
        self.assertEqual(
            caption_tokens(caption(0, 1000, '好的，没问题。')),
            ('好的', '没问题')
            )

    def test_drops_punctuation_only_text(self):
        self.assertEqual(caption_tokens(caption(0, 1000, '...')), ())


class TestEquivalenceGroups(unittest.TestCase):

    def test_direct_equivalence_is_undirected(self):
        groups = _equivalence_groups({'OK': 'okay'})
        self.assertEqual(groups['ok'], groups['okay'])
        self.assertEqual(groups['ok'], frozenset({'ok', 'okay'}))

    def test_several_equivalents_and_transitive_groups(self):
        groups = _equivalence_groups(
            {'OK': ['okay', 'okey'], 'okey-dokey': 'okey'}
            )
        self.assertEqual(
            groups['ok'],
            frozenset({'ok', 'okay', 'okey', 'dokey'})
            )

    def test_normalizes_case_and_punctuation(self):
        groups = _equivalence_groups({'OK.': 'okay!'})
        self.assertIn('ok', groups)
        self.assertIn('okay', groups)

    def test_unrelated_words_are_not_grouped(self):
        groups = _equivalence_groups({'OK': 'okay'})
        self.assertNotIn('roger', groups)

    def test_empty_input(self):
        self.assertEqual(_equivalence_groups(None), {})
        self.assertEqual(_equivalence_groups({}), {})

    def test_entry_without_words_is_ignored(self):
        self.assertEqual(_equivalence_groups({'...': 'okay'}), {})

    def test_rejects_non_string_key(self):
        with self.assertRaises(TypeError):
            _equivalence_groups({1: 'okay'})

    def test_rejects_invalid_value(self):
        with self.assertRaises(TypeError):
            _equivalence_groups({'OK': 1})
        with self.assertRaises(TypeError):
            _equivalence_groups({'OK': [1]})


class TestTemporalMatch(unittest.TestCase):

    def test_overlapping_intervals(self):
        transcript_word = word('roger', 1000, 1500)
        self.assertTrue(
            _temporal_match(transcript_word, 1200, 2000, 0)
            )

    def test_gap_within_tolerance(self):
        transcript_word = word('roger', 5000, 5100)
        self.assertTrue(
            _temporal_match(transcript_word, 5200, 5300, 100)
            )

    def test_gap_at_the_tolerance_boundary(self):
        transcript_word = word('roger', 5000, 5100)
        self.assertTrue(
            _temporal_match(transcript_word, 5200, 5300, 100)
            )
        self.assertFalse(
            _temporal_match(transcript_word, 5200, 5300, 99)
            )


class TestCheckAnchors(unittest.TestCase):

    def test_accepts_anchor_objects_and_tuples(self):
        result = align_transcript(
            [word('a', 0, 100), word('b', 200, 300)],
            [caption(0, 100, 'a'), caption(200, 300, 'b')],
            anchors=[Anchor(0, 0), (1, 1)]
            )
        self.assertEqual(
            [(item.word_index, item.caption_index) for item in result],
            [(0, 0), (1, 1)]
            )

    def test_anchors_may_share_a_caption(self):
        result = align_transcript(
            [word('a', 0, 100), word('b', 100, 200)],
            [caption(0, 300, 'a b')],
            anchors=[(0, 0), (1, 0)]
            )
        self.assertEqual(
            [item.caption_index for item in result], [0, 0]
            )
        self.assertTrue(all(item.anchor for item in result))

    def test_rejects_bad_anchor_type(self):
        with self.assertRaises(TypeError):
            align_transcript(
                [word('a', 0, 100)], [caption(0, 100, 'a')],
                anchors=[(0,)]
                )

    def test_rejects_index_out_of_range(self):
        words = [word('a', 0, 100)]
        captions = [caption(0, 100, 'a')]
        with self.assertRaises(ValueError):
            align_transcript(words, captions, anchors=[(1, 0)])
        with self.assertRaises(ValueError):
            align_transcript(words, captions, anchors=[(0, 1)])

    def test_rejects_repeated_word(self):
        with self.assertRaises(ValueError):
            align_transcript(
                [word('a', 0, 100), word('b', 100, 200)],
                [caption(0, 100, 'a'), caption(100, 200, 'b')],
                anchors=[(0, 0), (0, 1)]
                )

    def test_rejects_caption_index_going_backwards(self):
        with self.assertRaises(ValueError):
            align_transcript(
                [word('a', 0, 100), word('b', 100, 200)],
                [caption(0, 100, 'a'), caption(100, 200, 'b')],
                anchors=[(0, 1), (1, 0)]
                )

    def test_anchors_are_sorted_before_use(self):
        result = align_transcript(
            [word('a', 0, 100), word('b', 200, 300)],
            [caption(0, 100, 'a'), caption(200, 300, 'b')],
            anchors=[(1, 1), (0, 0)]
            )
        self.assertEqual(
            [item.word_index for item in result], [0, 1]
            )


class TestAlignTranscript(unittest.TestCase):

    def test_unique_matches_are_automatic(self):
        words = [
            word('hello', 0, 300),
            word('world', 500, 800),
            ]
        captions = [
            caption(0, 400, 'hello'),
            caption(500, 900, 'world'),
            ]
        result = align_transcript(words, captions)
        self.assertEqual(
            [(item.word_index, item.caption_index, item.automatic)
             for item in result],
            [(0, 0, True), (1, 1, True)]
            )
        self.assertEqual(result[0].time_offset, 0)
        self.assertEqual(result[1].time_offset, 0)

    def test_time_offset_is_caption_start_minus_word_start(self):
        result = align_transcript(
            [word('hello', 200, 500)],
            [caption(700, 1000, 'hello')]
            )
        self.assertEqual(result[0].time_offset, 500)

    def test_three_repeated_replies_leave_the_middle_ambiguous(self):
        words = [
            word('收到', 1000, 1200),
            word('收到', 1500, 1700),
            word('收到', 2000, 2200),
            word('好的', 3000, 3200),
            ]
        captions = [
            caption(1000, 1800, '收到 收到'),
            caption(2000, 2300, '收到'),
            caption(3000, 3300, '好的'),
            ]
        result = align_transcript(
            words, captions, anchors=[(0, 0), (2, 1)]
            )
        self.assertEqual(
            [(item.word_index, item.caption_index) for item in result],
            [(0, 0), (2, 1), (3, 2)]
            )
        self.assertTrue(result[0].anchor)
        self.assertTrue(result[1].anchor)
        self.assertTrue(result[2].automatic)

    def test_without_anchors_repeated_replies_are_not_guessed(self):
        words = [
            word('收到', 1000, 1200),
            word('收到', 1500, 1700),
            word('收到', 2000, 2200),
            word('好的', 3000, 3200),
            ]
        captions = [
            caption(1000, 1800, '收到 收到'),
            caption(2000, 2300, '收到'),
            caption(3000, 3300, '好的'),
            ]
        self.assertEqual(
            [(item.word_index, item.caption_index)
             for item in align_transcript(words, captions)],
            [(3, 2)]
            )

    def test_temporal_evidence_forces_the_middle_reply(self):
        words = [
            word('收到', 1000, 1200),
            word('收到', 1900, 1950),
            word('收到', 2050, 2200),
            ]
        captions = [
            caption(1000, 1800, '收到 收到'),
            caption(2000, 2300, '收到'),
            ]
        result = align_transcript(
            words,
            captions,
            tolerance=0.05,
            anchors=[(0, 0), (2, 1)]
            )
        self.assertEqual(
            [(item.word_index, item.caption_index) for item in result],
            [(0, 0), (1, 1), (2, 1)]
            )
        middle = next(item for item in result if item.word_index == 1)
        self.assertTrue(middle.automatic)

    def test_several_words_keep_their_contribution_in_one_cue(self):
        words = [
            word('over', 0, 200),
            word('and', 200, 400),
            word('out', 400, 600),
            ]
        captions = [caption(0, 700, 'over and out')]
        result = align_transcript(words, captions)
        self.assertEqual(
            [(item.word_index, item.caption_index) for item in result],
            [(0, 0), (1, 0), (2, 0)]
            )

    def test_equivalence_table_matches_different_wordings(self):
        result = align_transcript(
            [word('okay', 3000, 3200)],
            [caption(3000, 3300, 'OK')],
            equivalents={'okay': 'OK'}
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(
            (result[0].word_index, result[0].caption_index), (0, 0)
            )

    def test_matching_ignores_case_and_punctuation(self):
        result = align_transcript(
            [word('Roger', 0, 200)],
            [caption(0, 300, 'roger!')]
            )
        self.assertEqual(len(result), 1)

    def test_words_outside_tolerance_are_not_linked(self):
        self.assertEqual(
            align_transcript(
                [word('roger', 0, 200)],
                [caption(5000, 5300, 'roger')],
                tolerance=0.1
                ),
            []
            )

    def test_zero_tolerance_keeps_only_overlapping_times(self):
        result = align_transcript(
            [word('roger', 1000, 1500), word('out', 3000, 3200)],
            [caption(1200, 2000, 'roger'), caption(3000, 3300, 'out')],
            tolerance=0
            )
        self.assertEqual(
            [(item.word_index, item.caption_index) for item in result],
            [(0, 0), (1, 1)]
            )

    def test_anchors_are_always_kept(self):
        result = align_transcript(
            [word('roger', 0, 200)],
            [caption(9000, 9300, 'different')],
            anchors=[(0, 0)]
            )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].anchor)
        self.assertFalse(result[0].automatic)

    def test_anchor_does_not_use_line_numbers_or_identifiers(self):
        words = [word('roger', 0, 200), word('out', 500, 700)]
        first = caption(0, 300, 'roger')
        second = caption(500, 800, 'out')
        first.identifier = 'cue 42'
        second.identifier = 'cue 7'
        result = align_transcript(words, [first, second])
        self.assertEqual(
            [(item.word_index, item.caption_index) for item in result],
            [(0, 0), (1, 1)]
            )

    def test_result_is_ordered_by_word_index(self):
        result = align_transcript(
            [word('a', 0, 100),
             word('b', 200, 300),
             word('c', 400, 500)],
            [caption(0, 100, 'a'),
             caption(200, 300, 'b'),
             caption(400, 500, 'c')],
            anchors=[(2, 2), (0, 0)]
            )
        self.assertEqual(
            [item.word_index for item in result], [0, 1, 2]
            )

    def test_empty_inputs(self):
        self.assertEqual(align_transcript([], []), [])
        self.assertEqual(
            align_transcript([word('roger', 0, 100)], []), []
            )
        self.assertEqual(
            align_transcript([], [caption(0, 100, 'roger')]), []
            )

    def test_words_without_a_match_are_left_out(self):
        self.assertEqual(
            align_transcript(
                [word('roger', 0, 100)],
                [caption(0, 100, 'wilco')]
                ),
            []
            )

    def test_timedelta_tolerance(self):
        result = align_transcript(
            [word('roger', 0, 200)],
            [caption(600, 900, 'roger')],
            tolerance=datetime.timedelta(milliseconds=500)
            )
        self.assertEqual(len(result), 1)

    def test_rejects_invalid_tolerance(self):
        words = [word('roger', 0, 100)]
        captions = [caption(0, 100, 'roger')]
        with self.assertRaises(ValueError):
            align_transcript(words, captions, tolerance=-1)
        with self.assertRaises(ValueError):
            align_transcript(words, captions, tolerance=float('nan'))
        with self.assertRaises(TypeError):
            align_transcript(words, captions, tolerance='one second')
        with self.assertRaises(TypeError):
            align_transcript(words, captions, tolerance=True)

    def test_rejects_invalid_input_types(self):
        captions = [caption(0, 100, 'roger')]
        with self.assertRaises(TypeError):
            align_transcript([('roger', 0, 100)], captions)
        with self.assertRaises(TypeError):
            align_transcript([word('roger', 0, 100)], [('roger',)])

    def test_alignment_fields(self):
        alignment = Alignment(
            word_index=1,
            caption_index=2,
            anchor=True,
            automatic=False,
            time_offset=-50
            )
        self.assertEqual(
            (alignment.word_index, alignment.caption_index,
             alignment.anchor, alignment.automatic,
             alignment.time_offset),
            (1, 2, True, False, -50)
            )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            alignment.anchor = False


class TestForcedPairs(unittest.TestCase):

    def test_no_compatible_pair(self):
        self.assertEqual(_forced_pairs([[False, False]]), [])

    def test_empty_matrix(self):
        self.assertEqual(_forced_pairs([]), [])
        self.assertEqual(_forced_pairs([[]]), [])

    def test_ambiguous_word_is_not_forced(self):
        self.assertEqual(
            _forced_pairs([[True, True]]), []
            )

    def test_unique_word_is_forced_while_caption_is_shared(self):
        self.assertEqual(
            _forced_pairs([[True, False], [True, False]]),
            [(0, 0), (1, 0)]
            )


if __name__ == '__main__':
    unittest.main()
