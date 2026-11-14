"""Tests of the reading speed of the captions, per language."""

import datetime
import unittest

from webvtt import (
    Caption,
    CountingRule,
    LanguageSpec,
    ReadingConfigError,
    TimeSharing,
    WebVTT,
    count_graphemes,
    count_words,
    format_reading_table,
    merge_pauses,
    reading_table,
    )
from webvtt.errors import MalformedCaptionError
from webvtt.reading import _caption_text_by_language


class TestCounting(unittest.TestCase):

    def test_count_words(self):
        self.assertEqual(count_words('Hello, world!'), 2)
        self.assertEqual(count_words("don't stop U.S.A."), 3)

    def test_count_words_ignores_tokens_without_letter_or_digit(self):
        self.assertEqual(count_words('... -- ***'), 0)
        self.assertEqual(count_words('hi ... bye'), 2)

    def test_count_words_whitespace(self):
        self.assertEqual(count_words('   '), 0)
        self.assertEqual(count_words('one\ntwo\tthree'), 3)

    def test_count_graphemes_combining_mark_is_one_grapheme(self):
        text = 'é'
        self.assertEqual(len(text), 2)
        self.assertEqual(count_graphemes(text), 1)

    def test_count_graphemes_skips_spaces(self):
        self.assertEqual(count_graphemes('a b c'), 3)
        self.assertEqual(count_graphemes('  \n\t'), 0)

    def test_count_graphemes_joining_jamo_continues_syllable(self):
        # lead, medial and final jamo of one Hangul syllable
        self.assertEqual(count_graphemes('각'), 1)
        self.assertEqual(count_graphemes('가'), 1)

    def test_count_graphemes_format_character_continues(self):
        # the zero width joiner extends the previous grapheme
        self.assertEqual(count_graphemes('a‍b'), 2)

    def test_count_graphemes_conjoining_jamo_syllable(self):
        # lead, medial and final jamo, three code points forming one syllable
        syllable = chr(0x1100) + chr(0x1161) + chr(0x11A8)
        self.assertEqual(len(syllable), 3)
        self.assertEqual(count_graphemes(syllable), 1)

    def test_count_graphemes_plain_characters(self):
        self.assertEqual(count_graphemes('hola'), 4)
        self.assertEqual(count_graphemes('你好'), 2)
        self.assertEqual(count_graphemes('😮'), 1)


class TestMergePauses(unittest.TestCase):

    def test_merge_overlapping_and_touching_pauses(self):
        pauses = merge_pauses([(1, 3), (2, 4), (5, 6), (6, 7)])
        self.assertEqual(pauses, [(1000, 4000), (5000, 7000)])

    def test_merge_contained_pause_once(self):
        self.assertEqual(merge_pauses([(1, 10), (2, 3)]), [(1000, 10000)])

    def test_merge_keeps_separate_pauses(self):
        self.assertEqual(
            merge_pauses([(8, 9), (1, 2)]),
            [(1000, 2000), (8000, 9000)]
            )

    def test_merge_ignores_empty_pause(self):
        self.assertEqual(merge_pauses([(2, 2), (3, 1), (1, 4)]),
                         [(1000, 4000)])

    def test_merge_accepts_strings_timestamps_and_timedeltas(self):
        self.assertEqual(
            merge_pauses([
                ('00:00:01.000', '00:00:02.000'),
                (datetime.timedelta(seconds=1.5),
                 datetime.timedelta(seconds=3))
                ]),
            [(1000, 3000)]
            )

    def test_merge_invalid_pause_shape(self):
        with self.assertRaises(ReadingConfigError):
            merge_pauses([(1, 2, 3)])

    def test_merge_invalid_timestamp(self):
        with self.assertRaises(MalformedCaptionError):
            merge_pauses([('not a time', 2)])


class TestLanguageText(unittest.TestCase):

    def test_default_language_for_text_without_tag(self):
        self.assertEqual(
            _caption_text_by_language('Hello world', 'en'),
            {'en': 'Hello world'}
            )

    def test_split_on_language_tags(self):
        self.assertEqual(
            _caption_text_by_language(
                'Hello <lang es>hola</lang> world', 'en'
                ),
            {'en': 'Hello world', 'es': 'hola'}
            )

    def test_language_tag_separators(self):
        self.assertEqual(
            _caption_text_by_language(
                '<lang:fr>bonjour</lang:fr> <lang.de>tschuss</lang.de>',
                'en'
                ),
            {'fr': 'bonjour', 'de': 'tschuss'}
            )

    def test_other_cue_tags_are_removed_but_text_kept(self):
        self.assertEqual(
            _caption_text_by_language(
                '<v.lead Esme>Hi <c.red>there</c></v>', 'en'
                ),
            {'en': 'Hi there'}
            )

    def test_multiple_lines_and_spans(self):
        self.assertEqual(
            _caption_text_by_language(
                'one <lang es>uno</lang>\ntwo <lang es>dos</lang>', 'en'
                ),
            {'en': 'one two', 'es': 'uno dos'}
            )
    def test_closing_tag_without_open_is_ignored(self):
        self.assertEqual(
            _caption_text_by_language('hello</lang>', 'en'),
            {'en': 'hello'}
            )

    def test_language_span_with_only_whitespace_is_dropped(self):
        self.assertEqual(
            _caption_text_by_language(
                'hello <lang es> \n </lang>', 'en'
                ),
            {'en': 'hello'}
            )


class TestReadingTable(unittest.TestCase):

    def setUp(self):
        self.en = LanguageSpec('en', CountingRule.WORDS, 3.0)
        self.es = LanguageSpec('es', CountingRule.GRAPHEMES, 2.0)

    def test_words_and_graphemes_independent(self):
        caption = Caption(1, 5, 'Hello <lang es>hola</lang>')
        rows = reading_table([caption], [self.en, self.es])

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.duration_seconds, 4.0)
        self.assertEqual(row.readable_seconds, 4.0)
        self.assertFalse(row.unreadable)
        self.assertEqual(
            [(load.language, load.amount, load.counting)
             for load in row.languages],
            [('en', 1, CountingRule.WORDS),
             ('es', 4, CountingRule.GRAPHEMES)]
            )
        for load in row.languages:
            self.assertEqual(load.readable_seconds, 4.0)
            self.assertFalse(load.unreadable)

    def test_excess_and_speed(self):
        spec = LanguageSpec('en', CountingRule.WORDS, 0.1)
        caption = Caption(0, 4, 'ten words in this caption right here now')
        row = reading_table([caption], [spec])[0]
        load = row.languages[0]
        self.assertEqual(load.amount, 8)
        self.assertEqual(load.required_seconds, 80.0)
        self.assertEqual(load.excess_seconds, 76.0)
        self.assertEqual(load.speed, 2.0)

    def test_no_excess_when_text_fits(self):
        caption = Caption(0, 10, 'hi')
        load = reading_table([caption], [self.en])[0].languages[0]
        self.assertEqual(load.excess_seconds, 0.0)
        self.assertEqual(load.speed, 0.1)

    def test_pauses_are_subtracted_and_merged_once(self):
        caption = Caption(0, 10, 'hello')
        rows = reading_table(
            [caption], [self.en],
            pauses=[(2, 4), (3, 5)]
            )
        row = rows[0]
        # the two pauses merge into (2, 5), so 7 seconds remain
        self.assertEqual(row.readable_seconds, 7.0)
        windows = [
            (start.in_milliseconds(), end.in_milliseconds())
            for start, end in row.readable_windows
            ]
        self.assertEqual(windows, [(0, 2000), (5000, 10000)])
        self.assertEqual(row.languages[0].readable_seconds, 7.0)

    def test_pause_outside_caption_does_not_reduce_time(self):
        caption = Caption(0, 4, 'hello')
        row = reading_table(
            [caption], [self.en], pauses=[(10, 20)]
            )[0]
        self.assertEqual(row.readable_seconds, 4.0)

    def test_fully_unreadable_caption(self):
        caption = Caption(1, 5, 'hello')
        row = reading_table(
            [caption], [self.en], pauses=[(0, 10)]
            )[0]
        self.assertTrue(row.unreadable)
        self.assertEqual(row.readable_windows, ())
        self.assertEqual(row.readable_seconds, 0.0)
        load = row.languages[0]
        self.assertTrue(load.unreadable)
        self.assertIsNone(load.speed)
        self.assertEqual(
            load.excess_seconds, load.required_seconds
            )

    def test_shared_time_split_by_weights(self):
        en = LanguageSpec(
            'en', CountingRule.WORDS, 3.0,
            sharing=TimeSharing.SHARED, weight=2
            )
        es = LanguageSpec(
            'es', CountingRule.GRAPHEMES, 2.0,
            sharing=TimeSharing.SHARED, weight=1
            )
        caption = Caption(0, 10, 'Hello <lang es>hola</lang>')
        row = reading_table([caption], [en, es])[0]
        loads = {load.language: load for load in row.languages}
        self.assertEqual(loads['en'].readable_seconds, 6.667)
        self.assertEqual(loads['es'].readable_seconds, 3.333)

    def test_shared_time_allocations_add_up(self):
        specs = [
            LanguageSpec(
                code, CountingRule.WORDS, 3.0,
                sharing=TimeSharing.SHARED
                )
            for code in ('en', 'es', 'fr')
            ]
        caption = Caption(
            0, 1,
            '<lang en>a</lang><lang es>b</lang><lang fr>c</lang>'
            )
        row = reading_table([caption], specs)[0]
        self.assertEqual(
            sum(load.readable_seconds for load in row.languages),
            1.0
            )

    def test_shared_time_when_unreadable(self):
        specs = [
            LanguageSpec(
                code, CountingRule.WORDS, 3.0,
                sharing=TimeSharing.SHARED
                )
            for code in ('en', 'es')
            ]
        caption = Caption(0, 4, '<lang en>a</lang><lang es>b</lang>')
        row = reading_table(
            [caption], specs, pauses=[(0, 10)]
            )[0]
        self.assertTrue(row.unreadable)
        for load in row.languages:
            self.assertEqual(load.readable_seconds, 0.0)
            self.assertTrue(load.unreadable)

    def test_caption_without_text(self):
        row = reading_table([Caption(0, 2)], [self.en])[0]
        self.assertEqual(row.languages, ())
        self.assertEqual(row.readable_seconds, 2.0)

    def test_zero_duration_caption(self):
        row = reading_table([Caption(2, 2, 'hello')], [self.en])[0]
        self.assertEqual(row.duration_seconds, 0.0)
        self.assertTrue(row.unreadable)

    def test_empty_captions(self):
        self.assertEqual(reading_table([], [self.en]), [])

    def test_integration_with_parsed_vtt(self):
        vtt = WebVTT.from_string(
            'WEBVTT\n\n'
            '00:00:01.000 --> 00:00:05.000\n'
            'Hello <lang es>hola</lang>\n'
            )
        row = reading_table(vtt.captions, [self.en, self.es])[0]
        self.assertEqual(row.index, 0)
        self.assertEqual(str(row.start), '00:00:01.000')
        self.assertEqual(
            [load.amount for load in row.languages], [1, 4]
            )


class TestReadingConfig(unittest.TestCase):

    def test_rate_and_weight_must_be_positive_numbers(self):
        for value in (0, -1, float('nan'), float('inf'), '2', True):
            with self.subTest(value=value):
                with self.assertRaises(ReadingConfigError):
                    LanguageSpec('en', CountingRule.WORDS, value)
            with self.subTest(weight=value):
                with self.assertRaises(ReadingConfigError):
                    LanguageSpec(
                        'en', CountingRule.WORDS, 1.0, weight=value
                        )

    def test_no_language_rule(self):
        with self.assertRaises(ReadingConfigError):
            reading_table([Caption(0, 1, 'hi')], [])

    def test_duplicate_language_rules(self):
        with self.assertRaises(ReadingConfigError):
            reading_table(
                [Caption(0, 1, 'hi')],
                [LanguageSpec('en', CountingRule.WORDS, 1.0),
                 LanguageSpec('en', CountingRule.GRAPHEMES, 1.0)]
                )

    def test_rule_must_be_a_language_spec(self):
        with self.assertRaises(ReadingConfigError):
            reading_table([Caption(0, 1, 'hi')], ['en'])

    def test_unknown_language_in_caption(self):
        with self.assertRaises(ReadingConfigError):
            reading_table(
                [Caption(0, 1, '<lang fr>bonjour</lang>')],
                [LanguageSpec('en', CountingRule.WORDS, 1.0)]
                )

    def test_mixed_time_strategies(self):
        specs = [
            LanguageSpec('en', CountingRule.WORDS, 1.0),
            LanguageSpec(
                'es', CountingRule.GRAPHEMES, 1.0,
                sharing=TimeSharing.SHARED
                )
            ]
        with self.assertRaises(ReadingConfigError):
            reading_table(
                [Caption(0, 1, 'hi <lang es>hola</lang>')], specs
                )


class TestFormatTable(unittest.TestCase):

    def test_table_has_one_column_group_per_language(self):
        en = LanguageSpec('en', CountingRule.WORDS, 3.0)
        es = LanguageSpec('es', CountingRule.GRAPHEMES, 2.0)
        rows = reading_table(
            [Caption(0, 4, 'Hello <lang es>hola</lang>'),
             Caption(4, 8, 'Goodbye')],
            [en, es]
            )
        table = format_reading_table(rows)
        header, first, second = table.split('\n')
        self.assertIn('en words', header)
        self.assertIn('es graphemes', header)
        self.assertIn('Unreadable', header)
        self.assertTrue(first.startswith('1 | 00:00:00.000 | 00:00:04.000'))
        self.assertIn(' | no | ', first)
        # Spanish is absent from the second caption: its columns are dashes
        self.assertTrue(second.endswith('| - | - | -'))

    def test_table_marks_unreadable_caption(self):
        rows = reading_table(
            [Caption(1, 5, 'hello')],
            [LanguageSpec('en', CountingRule.WORDS, 3.0)],
            pauses=[(0, 10)]
            )
        line = format_reading_table(rows).split('\n')[1]
        self.assertIn('| yes |', line)

    def test_table_decimals(self):
        rows = reading_table(
            [Caption(0, 4, 'hello')],
            [LanguageSpec('en', CountingRule.WORDS, 3.0)]
            )
        columns = format_reading_table(
            rows, seconds_decimals=1
            ).split('\n')[1].split(' | ')
        # the readable and excess columns use the requested decimals
        self.assertEqual(columns[-2:], ['4.0', '0.0'])


if __name__ == '__main__':
    unittest.main()
