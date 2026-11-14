"""Tests of the incremental update of the derived data."""

import unittest

from hypothesis import given, settings, strategies as st

from webvtt import (
    Caption, DerivedData, SearchHit, UpdateReport, WebVTT, WordTiming
    )

VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
<v Jane>Hello <00:00:02.000>world

NOTE opening

00:00:03.000 --> 00:00:05.000
Hello again

00:00:05.000 --> 00:00:07.000
repeat

00:00:07.000 --> 00:00:09.000
repeat
"""


def document(text=VTT):
    """Return a WebVTT document from a string."""
    return WebVTT.from_string(text)


def document_from_lines(lines):
    """
    Build a document from lines of text.

    The cues are one second apart and have no cue tag.
    """
    blocks = ['WEBVTT', '']
    second = 0
    for line in lines:
        blocks.extend([
            f'00:00:{second:02d}.000 --> 00:00:{second + 1:02d}.000',
            line,
            ''
            ])
        second += 2
    return WebVTT.from_string('\n'.join(blocks))


class TestBuild(unittest.TestCase):
    """Tests of building the derived data as a whole."""

    def test_search_index_words_and_positions(self):
        data = DerivedData.build(document())
        self.assertEqual(
            data.search_index['hello'],
            [SearchHit(0, 0, 5), SearchHit(1, 0, 5)]
            )
        self.assertEqual(
            data.search_index['world'], [SearchHit(0, 6, 11)]
            )

    def test_search_is_case_insensitive_by_default(self):
        data = DerivedData.build(document())
        self.assertEqual(
            [hit.caption_index for hit in data.search('HELLO')], [0, 1]
            )
        self.assertNotIn('Hello', data.search_index)
        self.assertIn('hello', data.search_index)

    def test_search_is_case_sensitive_when_asked(self):
        data = DerivedData.build(document(), case_sensitive=True)
        self.assertEqual(
            [hit.caption_index for hit in data.search('Hello')], [0, 1]
            )
        self.assertEqual(data.search('hello'), [])
        self.assertIn('Hello', data.search_index)

    def test_search_combines_words_with_and(self):
        data = DerivedData.build(document())
        self.assertEqual(
            [hit.caption_index for hit in data.search('hello world')],
            [0, 0]
            )
        self.assertEqual(data.search('hello missing'), [])
        self.assertEqual(data.search(''), [])

    def test_search_keeps_letter_words_and_drops_digits(self):
        doc = document_from_lines(['café 42 naïve'])
        data = DerivedData.build(doc)
        self.assertEqual(
            sorted(data.search_index), ['café', 'naïve']
            )

    def test_positions_count_line_breaks_not_tags(self):
        caption = Caption('00:00:01.000', '00:00:03.000',
                          ['first line', '<00:00:02.000>second'])
        data = DerivedData.build([caption])
        self.assertEqual(
            data.search_index['second'], [SearchHit(0, 11, 17)]
            )

    def test_word_timings_interpolate_between_outer_times(self):
        data = DerivedData.build(document())
        self.assertEqual(
            data.word_timings[0],
            [WordTiming('Hello', 1000, 1833, 0),
             WordTiming('world', 2000, 3000, 0)]
            )

    def test_word_timings_without_inline_tags(self):
        caption = Caption('00:00:01.000', '00:00:03.000', 'Hello world')
        data = DerivedData.build([caption])
        self.assertEqual(
            data.word_timings[0],
            [WordTiming('Hello', 1000, 1909, 0),
             WordTiming('world', 2091, 3000, 0)]
            )

    def test_word_timings_handle_out_of_order_inline_tags(self):
        caption = Caption(
            '00:00:05.000', '00:00:06.000',
            'word <00:00:01.000>late'
            )
        data = DerivedData.build([caption])
        # the second boundary is earlier, so the interpolation is negative
        self.assertEqual(data.word_timings[0][0].start, 5000)
        self.assertEqual(data.word_timings[0][1].start, 1000)

    def test_inline_timestamp_has_to_be_valid(self):
        caption = Caption(
            '00:00:01.000', '00:00:03.000',
            'word <99:99.000><c.foo>after</c>'
            )
        data = DerivedData.build([caption])
        self.assertEqual(
            [timing.word for timing in data.word_timings[0]],
            ['word', 'after']
            )

    def test_chapters_come_from_caption_comments(self):
        data = DerivedData.build(document())
        self.assertEqual(len(data.chapters), 1)
        chapter = data.chapters[0]
        self.assertEqual(chapter.title, 'opening')
        self.assertEqual(chapter.caption_index, 1)
        self.assertEqual(chapter.start, 3000)

    def test_empty_document(self):
        data = DerivedData.build(document_from_lines([]))
        self.assertEqual(data.search_index, {})
        self.assertEqual(data.word_timings, [])
        self.assertEqual(data.chapters, [])
        self.assertEqual(data.search('anything'), [])

    def test_search_index_is_a_copy(self):
        data = DerivedData.build(document())
        index = data.search_index
        index.clear()
        self.assertIn('hello', data.search_index)


class TestUpdate(unittest.TestCase):
    """Tests of the incremental update."""

    def _updated(self, mutate):
        doc = document()
        data = DerivedData.build(doc)
        new_doc = document()
        mutate(new_doc)
        report = data.update(new_doc)
        return data, report, new_doc

    def _assert_parity(self, data, new_doc):
        full = DerivedData.build(
            new_doc, case_sensitive=data.case_sensitive
            )
        self.assertEqual(data.search_index, full.search_index)
        self.assertEqual(data.word_timings, full.word_timings)
        self.assertEqual(data.chapters, full.chapters)

    def test_outer_time_change_only_reports_timing(self):
        data, report, new_doc = self._updated(
            lambda doc: setattr(doc.captions[1], 'start', '00:00:04.000')
            )
        self.assertEqual(len(report.changes), 1)
        change = report.changes[0]
        self.assertTrue(change.timing)
        self.assertFalse(change.body)
        self.assertFalse(change.inline)
        self.assertFalse(change.speaker)
        self.assertEqual(report.rebuild_ranges, [])
        self._assert_parity(data, new_doc)

    def test_body_change_reports_body_and_rebuilds(self):
        data, report, new_doc = self._updated(
            lambda doc: setattr(doc.captions[1], 'text', 'changed words')
            )
        change = next(
            change for change in report.changes
            if change.old_index == 1 and change.new_index is not None
            )
        self.assertTrue(change.body)
        self.assertFalse(change.inline)
        self.assertIn((1, 2), report.rebuild_ranges)
        self._assert_parity(data, new_doc)

    def test_inline_timestamp_change_reports_inline(self):
        data, report, new_doc = self._updated(
            lambda doc: setattr(
                doc.captions[0],
                'lines',
                ['<v Jane>Hello <00:00:02.500>world']
                )
            )
        change = report.changes[0]
        self.assertTrue(change.inline)
        self.assertFalse(change.body)
        self.assertFalse(change.speaker)
        self.assertEqual(report.rebuild_ranges, [(0, 1)])
        self._assert_parity(data, new_doc)

    def test_speaker_change_reports_speaker(self):
        data, report, new_doc = self._updated(
            lambda doc: setattr(
                doc.captions[0], 'lines',
                ['<v John>Hello <00:00:02.000>world']
                )
            )
        change = report.changes[0]
        self.assertTrue(change.speaker)
        self.assertFalse(change.body)
        self.assertEqual(report.rebuild_ranges, [(0, 1)])
        self._assert_parity(data, new_doc)

    def test_insert_and_delete_changes(self):
        data, report, new_doc = self._updated(
            lambda doc: doc.captions.insert(
                0, Caption('00:00:00.000', '00:00:01.000', 'new cue')
                )
            )
        inserted = report.changes[0]
        self.assertTrue(inserted.inserted)
        self.assertFalse(inserted.deleted)
        self.assertEqual(inserted.new_index, 0)
        self._assert_parity(data, new_doc)

        data, report, new_doc = self._updated(
            lambda doc: doc.captions.pop(2)
            )
        deleted = next(change for change in report.changes if change.deleted)
        self.assertTrue(deleted.deleted)
        self.assertFalse(deleted.inserted)
        self._assert_parity(data, new_doc)

    def test_paired_change_is_neither_inserted_nor_deleted(self):
        _, report, _ = self._updated(
            lambda doc: setattr(doc.captions[0], 'start', '00:00:02.000')
            )
        self.assertFalse(report.changes[0].inserted)
        self.assertFalse(report.changes[0].deleted)

    def test_unchanged_cue_is_not_reported(self):
        _, report, _ = self._updated(
            lambda doc: setattr(doc.captions[0], 'start', '00:00:02.000')
            )
        self.assertEqual(
            [change.old_index for change in report.changes], [0]
            )

    def test_shifting_every_cue_keeps_the_search_index(self):
        data, report, new_doc = self._updated(
            lambda doc: [caption.shift(1) for caption in doc.captions]
            )
        self.assertTrue(all(change.timing for change in report.changes))
        self.assertEqual(report.rebuild_ranges, [])
        self._assert_parity(data, new_doc)

    def test_moving_a_cue_remaps_the_indices(self):
        def move(doc):
            doc.captions.insert(0, doc.captions.pop(1))

        data, report, new_doc = self._updated(move)
        self._assert_parity(data, new_doc)
        timing_indices = {
            timing.caption_index
            for timings in data.word_timings
            for timing in timings
            }
        self.assertEqual(timing_indices, set(range(len(new_doc.captions))))

    def test_repeated_text_without_neighbour_is_ambiguous(self):
        old = document_from_lines(['repeat', 'repeat'])
        data = DerivedData.build(old)
        new_doc = document_from_lines(['repeat'])
        report = data.update(new_doc)
        self.assertTrue(report.ambiguous)
        self.assertEqual(report.rebuild_ranges, [(0, 1)])
        ambiguous = next(
            change for change in report.changes if change.ambiguous
            )
        self.assertEqual(ambiguous.old_index, 0)
        self.assertEqual(ambiguous.new_index, 0)
        self._assert_parity(data, new_doc)

    def test_repeated_text_pinned_by_neighbours_is_unique(self):
        old = document()
        data = DerivedData.build(old)
        new_doc = document()
        new_doc.captions.pop(3)
        report = data.update(new_doc)
        self.assertFalse(report.ambiguous)
        self._assert_parity(data, new_doc)

    def test_identifier_pins_repeated_text(self):
        old = document_from_lines(['repeat', 'repeat'])
        for index, caption in enumerate(old.captions):
            caption.identifier = f'id{index}'
        data = DerivedData.build(old)
        new_doc = document_from_lines(['repeat'])
        new_doc.captions[0].identifier = 'id1'
        report = data.update(new_doc)
        self.assertFalse(report.ambiguous)
        self.assertEqual(report.rebuild_ranges, [])
        self._assert_parity(data, new_doc)

    def test_duplicate_identifier_falls_back_to_text(self):
        old = document_from_lines(['same', 'same'])
        for caption in old.captions:
            caption.identifier = 'dup'
        data = DerivedData.build(old)
        new_doc = document_from_lines(['same'])
        report = data.update(new_doc)
        self.assertTrue(report.ambiguous)
        self._assert_parity(data, new_doc)

    def test_ranges_are_merged(self):
        old = document()
        data = DerivedData.build(old)
        new_doc = document()
        new_doc.captions[0].text = 'one change'
        new_doc.captions[1].text = 'another change'
        report = data.update(new_doc)
        self.assertIn((0, 2), report.rebuild_ranges)
        self._assert_parity(data, new_doc)

    def test_chapter_is_rewritten_when_caption_moves(self):
        old = document()
        data = DerivedData.build(old)
        new_doc = document()
        new_doc.captions.insert(0, new_doc.captions.pop(1))
        data.update(new_doc)
        chapter = data.chapters[0]
        moved = next(
            caption for caption in new_doc.captions
            if caption.comments == ['opening']
            )
        self.assertEqual(
            chapter.caption_index, new_doc.captions.index(moved)
            )
        self.assertEqual(chapter.start, moved.start_in_milliseconds)

    def test_chapter_title_change_is_detected(self):
        old = document()
        data = DerivedData.build(old)
        new_doc = document()
        new_doc.captions[1].comments = ['new title']
        report = data.update(new_doc)
        self.assertEqual(data.chapters[0].title, 'new title')
        self._assert_parity(data, new_doc)
        change = next(
            change for change in report.changes
            if change.old_index == 1
            )
        self.assertTrue(change.comment)
        self.assertFalse(change.body)

    def test_successive_updates_keep_parity(self):
        doc = document()
        data = DerivedData.build(doc)
        new_doc = document()
        new_doc.captions[0].start = '00:00:02.000'
        data.update(new_doc)
        new_doc.captions.insert(
            2, Caption('00:00:04.000', '00:00:05.000', 'inserted here')
            )
        report = data.update(new_doc)
        self._assert_parity(data, new_doc)
        new_doc.captions[1].text = 'edited again'
        report = data.update(new_doc)
        self._assert_parity(data, new_doc)
        self.assertIsInstance(report, UpdateReport)

    def test_update_accepts_a_sequence_of_captions(self):
        doc = document()
        data = DerivedData.build(list(doc.captions))
        new_doc = document()
        new_doc.captions[0].start = '00:00:02.000'
        report = data.update(list(new_doc.captions))
        self._assert_parity(data, new_doc)
        self.assertTrue(report.changes[0].timing)

    def test_case_sensitive_update_matches_full_build(self):
        doc = document()
        data = DerivedData.build(doc, case_sensitive=True)
        new_doc = document()
        new_doc.captions[1].text = 'HELLO changed'
        data.update(new_doc)
        self._assert_parity(data, new_doc)
        self.assertTrue(data.case_sensitive)


# documents made of a small, repeated vocabulary so the diff has to follow
# cues with repeated text, plus identifiers, tags and comments
_TEXT = st.sampled_from([
    'repeat', 'repeat', 'other line', 'Hello world',
    '<v Jane>Hello <00:00:02.000>world', 'last cue'
    ])
_IDENTIFIER = st.none() | st.sampled_from(['id-1', 'id-2', None])


def _caption(second, text_strategy=_TEXT):
    """Return a strategy of captions a fixed distance apart."""
    return st.builds(
        Caption,
        st.just(second * 2000 / 1000),
        st.just((second * 2000 + 1000) / 1000),
        st.lists(text_strategy, min_size=1, max_size=2),
        identifier=_IDENTIFIER
        )


@st.composite
def _documents(draw):
    """Return two documents, the second one edited with random operations."""
    size = draw(st.integers(min_value=0, max_value=6))
    captions = draw(st.lists(
        st.one_of([_caption(index) for index in range(size)]),
        min_size=size, max_size=size
        )) if size else []
    edited = [caption.copy() for caption in captions]

    operations = draw(st.integers(min_value=0, max_value=4))
    for _ in range(operations):
        if not edited:
            edited.append(Caption(0, 1, 'fresh cue'))
            continue
        choice = draw(st.integers(min_value=0, max_value=4))
        index = draw(st.integers(min_value=0, max_value=len(edited) - 1))
        if choice == 0:
            edited.pop(index)
        elif choice == 1:
            edited.insert(index, Caption(index, index + 1, 'fresh cue'))
        elif choice == 2:
            edited[index].shift(draw(st.integers(min_value=-2, max_value=2)))
        elif choice == 3:
            edited[index].text = draw(st.sampled_from(
                ['new wording', 'repeat', 'Hello <00:00:03.000>world']
                ))
        else:
            edited[index].comments = [
                draw(st.sampled_from(['chapter A', 'chapter B']))
                ]
    return captions, edited


class TestParity(unittest.TestCase):
    """Property tests that the incremental update equals a full build."""

    @given(_documents())
    @settings(deadline=None, max_examples=200)
    def test_incremental_matches_full_generation(self, documents):
        old_captions, new_captions = documents
        for case_sensitive in (False, True):
            data = DerivedData.build(
                old_captions, case_sensitive=case_sensitive
                )
            data.update(new_captions)
            full = DerivedData.build(
                new_captions, case_sensitive=case_sensitive
                )
            self.assertEqual(data.search_index, full.search_index)
            self.assertEqual(data.word_timings, full.word_timings)
            self.assertEqual(data.chapters, full.chapters)
            for query in ('repeat', 'Hello world', 'fresh cue', ''):
                self.assertEqual(
                    data.search(query), full.search(query)
                    )


if __name__ == '__main__':
    unittest.main()
