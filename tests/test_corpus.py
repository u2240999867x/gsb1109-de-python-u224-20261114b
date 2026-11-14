import pathlib
import time
import unittest

import webvtt
from webvtt import Caption, CueSpan, RubyAnnotation, index_captions
from webvtt.corpus import (
    _decode_entity,
    _iter_decoded,
    _merge_ranges,
    _parse_tag,
    _uncovered_gaps,
    )

PATH_TO_SAMPLES = pathlib.Path(__file__).resolve().parent / 'samples'


def _index(*lines):
    """Index one caption with the given lines of raw cue text."""
    return index_captions([Caption(text=list(lines))])


class TestIndexCaptions(unittest.TestCase):

    def test_empty_captions(self):
        index = index_captions(())
        self.assertEqual(index.cues, [])
        self.assertEqual(index.rubies, [])
        self.assertEqual(index.terms, {})
        self.assertEqual(index.readings, {})
        self.assertEqual(index.speakers, {})
        self.assertEqual(index.language_spans, [])

    def test_accepts_a_generator(self):
        def captions():
            yield Caption(text='<ruby>東京<rt>とうきょう</rt></ruby>')

        index = index_captions(captions())
        self.assertEqual(len(index.cues), 1)
        self.assertEqual(index.find_terms('東京')[0].cue_index, 0)

    def test_one_cue_per_caption_in_order(self):
        captions = [
            Caption('00:00:01.000', '00:00:02.000', 'first',
                    identifier='one'),
            Caption('00:00:03.000', '00:00:04.000', 'second'),
            ]
        index = index_captions(captions)
        self.assertEqual([ref.caption_index for ref in index.cues], [0, 1])
        self.assertEqual(index.cues[0].identifier, 'one')
        self.assertIsNone(index.cues[1].identifier)
        self.assertEqual(index.cues[0].start, '00:00:01.000')
        self.assertEqual(index.cues[0].end, '00:00:02.000')
        self.assertIs(index.cues[1].caption, captions[1])

    def test_sample_file(self):
        vtt = webvtt.read(PATH_TO_SAMPLES / 'japanese_ruby.vtt')
        index = index_captions(vtt.captions)
        self.assertEqual(len(index.cues), 3)
        self.assertEqual(index.cues[0].identifier, 'cue-1')
        self.assertEqual(
            [ruby.base for ruby in index.rubies],
            ['東京', '図書館', '山&aelig;田']
            )
        self.assertEqual(
            [ruby.reading for ruby in index.rubies],
            ['とうきょう', 'としょかん', 'やまだ']
            )


class TestRubyAnnotations(unittest.TestCase):

    def test_reading_points_back_to_its_base_range(self):
        index = _index('<ruby>東京<rt>とうきょう</rt></ruby>')
        ruby = index.rubies[0]
        ref = index.cue_for(ruby)
        self.assertIsInstance(ruby, RubyAnnotation)
        self.assertEqual(ruby.base, '東京')
        self.assertEqual(ruby.reading, 'とうきょう')
        self.assertEqual(
            ref.text[ruby.base_range[0]:ruby.base_range[1]], ruby.base
            )
        self.assertEqual(
            ref.text[ruby.reading_range[0]:ruby.reading_range[1]],
            ruby.reading
            )
        self.assertEqual(ruby.cue_index, 0)
        self.assertEqual(ruby.line, 0)

    def test_raw_ranges_slice_raw_text(self):
        caption = Caption(text='a<ruby>東京<rt>とうきょう</rt></ruby>b')
        index = index_captions([caption])
        ruby = index.rubies[0]
        self.assertEqual(
            caption.raw_text[
                ruby.base_raw_range[0]:ruby.base_raw_range[1]
                ],
            '東京'
            )
        self.assertEqual(
            caption.raw_text[
                ruby.reading_raw_range[0]:ruby.reading_raw_range[1]
                ],
            'とうきょう'
            )

    def test_offsets_with_length_changing_entities(self):
        caption = Caption(text='<ruby>A&amp;B<rt>x&#65;y</rt></ruby>')
        index = index_captions([caption])
        ruby = index.rubies[0]
        ref = index.cue_for(ruby)
        # the decoded offsets index the decoded cue text
        self.assertEqual(ruby.base, 'A&B')
        self.assertEqual(ruby.reading, 'xAy')
        self.assertEqual(
            ref.text[ruby.base_range[0]:ruby.base_range[1]], 'A&B'
            )
        self.assertEqual(
            ref.text[ruby.reading_range[0]:ruby.reading_range[1]],
            'xAy'
            )
        # the raw offsets keep the references as written
        self.assertEqual(
            caption.raw_text[
                ruby.base_raw_range[0]:ruby.base_raw_range[1]
                ],
            'A&amp;B'
            )
        self.assertEqual(
            caption.raw_text[
                ruby.reading_raw_range[0]:ruby.reading_raw_range[1]
                ],
            'x&#65;y'
            )
        self.assertIn('A&B', index.terms)
        self.assertIn('xAy', index.readings)

    def test_group_reading_is_not_split(self):
        index = _index('<ruby>図書館<rt>としょかん</rt></ruby>')
        self.assertEqual(len(index.rubies), 1)
        ruby = index.rubies[0]
        self.assertEqual(ruby.base, '図書館')
        self.assertEqual(ruby.base_range, (0, 3))
        self.assertEqual(ruby.reading, 'としょかん')
        self.assertEqual(ruby.reading_range, (3, 8))

    def test_per_character_annotations_are_separate(self):
        index = _index(
            '<ruby>日<rt>ひ</rt></ruby><ruby>本<rt>もと</rt></ruby>'
            )
        self.assertEqual(
            [(ruby.base, ruby.reading) for ruby in index.rubies],
            [('日', 'ひ'), ('本', 'もと')]
            )

    def test_nested_ruby(self):
        index = _index(
            '<ruby>外<rt>そと<ruby>内<rt>うち</rt></ruby></rt></ruby>'
            )
        bases = [ruby.base for ruby in index.rubies]
        self.assertIn('内', bases)
        self.assertIn('外', bases)
        inner = next(ruby for ruby in index.rubies if ruby.base == '内')
        self.assertEqual(inner.reading, 'うち')

    def test_ruby_without_reading_is_ignored(self):
        index = _index('<ruby>欠落</ruby>')
        self.assertEqual(index.rubies, [])
        # the base stays an ordinary term
        self.assertEqual(index.find_terms('欠落')[0].text, '欠落')

    def test_ruby_without_base_is_ignored(self):
        index = _index('<ruby><rt>よみ</rt></ruby>')
        self.assertEqual(index.rubies, [])
        self.assertEqual(index.find_readings('よみ'), [])

    def test_empty_ruby_is_ignored(self):
        index = _index('<ruby></ruby>')
        self.assertEqual(index.rubies, [])

    def test_stray_closing_ruby(self):
        index = _index('text</ruby>more')
        self.assertEqual(index.rubies, [])
        # the tag adds no separator, so the text is one word run
        self.assertIn('textmore', index.terms)

    def test_unclosed_ruby_is_closed_at_end_of_cue(self):
        index = _index('<ruby>閉じない<rt>とじない')
        self.assertEqual(len(index.rubies), 1)
        ruby = index.rubies[0]
        self.assertEqual(ruby.base, '閉じない')
        self.assertEqual(ruby.reading, 'とじない')

    def test_rt_outside_ruby_is_ignored(self):
        index = _index('plain<rt>よみ</rt>text')
        self.assertEqual(index.rubies, [])
        self.assertEqual(index.find_readings('よみ'), [])

    def test_rp_text_is_neither_base_nor_reading(self):
        index = _index(
            '<ruby>東京<rp>（</rp><rt>とうきょう</rt><rp>）</rp></ruby>'
            )
        ruby = index.rubies[0]
        self.assertEqual(ruby.base, '東京')
        self.assertEqual(ruby.reading, 'とうきょう')
        self.assertNotIn('（', index.cue_for(ruby).text[
            ruby.base_range[0]:ruby.base_range[1]
            ])

    def test_stray_closing_rp(self):
        index = _index('<ruby>東京<rt>とうきょう</rt></rp></ruby>')
        self.assertEqual(len(index.rubies), 1)
        self.assertEqual(index.rubies[0].reading, 'とうきょう')

    def test_rb_base_tag(self):
        index = _index('<ruby><rb>東京</rb><rt>とうきょう</rt></ruby>')
        self.assertEqual(index.rubies[0].base, '東京')

    def test_cross_line_ruby(self):
        index = _index(
            '<ruby><rb>山田</rb><rp>（</rp><rt>',
            'やまだ</rt><rp>）</rp></ruby>'
            )
        ruby = index.rubies[0]
        self.assertEqual(ruby.base, '山田')
        self.assertEqual(ruby.reading, 'やまだ')
        self.assertEqual(ruby.line, 0)
        ref = index.cue_for(ruby)
        self.assertIn('\n', ref.text)
        self.assertEqual(
            ref.text[ruby.base_range[0]:ruby.base_range[1]], '山田'
            )
        self.assertEqual(
            ref.text[ruby.reading_range[0]:ruby.reading_range[1]],
            'やまだ'
            )
        reading_span = index.find_readings('やまだ')[0]
        self.assertEqual(reading_span.line, 1)

    def test_ruby_inherits_language(self):
        index = _index(
            '<lang ja><ruby>東京<rt>とうきょう</rt></ruby></lang>'
            )
        self.assertEqual(index.rubies[0].lang, 'ja')
        self.assertEqual(index.find_terms('東京')[0].lang, 'ja')


class TestTermsAndReadings(unittest.TestCase):

    def test_terms_and_readings_are_indexed_separately(self):
        index = _index('<ruby>東京<rt>とうきょう</rt></ruby>')
        self.assertIn('東京', index.terms)
        self.assertNotIn('東京', index.readings)
        self.assertIn('とうきょう', index.readings)
        self.assertNotIn('とうきょう', index.terms)

    def test_reading_does_not_leak_into_adjacent_term(self):
        index = _index(
            '<ruby>東京<rt>とうきょう</rt></ruby>は'
            '<ruby>日本<rt>にほん</rt></ruby>の'
            )
        self.assertEqual(set(index.terms), {'の', '日本', '東京', 'は'})
        self.assertEqual(
            set(index.readings), {'とうきょう', 'にほん'}
            )

    def test_ordinary_text_outside_ruby_is_indexed(self):
        index = _index('hello <ruby>東京<rt>とうきょう</rt></ruby> world')
        self.assertIn('hello', index.terms)
        self.assertIn('world', index.terms)
        self.assertIn('東京', index.terms)

    def test_same_term_in_more_than_one_cue(self):
        captions = [
            Caption(text='<ruby>東京<rt>とうきょう</rt></ruby>'),
            Caption(text='東京 駅'),
            ]
        index = index_captions(captions)
        spans = index.find_terms('東京')
        self.assertEqual([span.cue_index for span in spans], [0, 1])

    def test_find_methods_return_copies(self):
        index = _index('<ruby>東京<rt>とうきょう</rt></ruby>')
        terms = index.find_terms('東京')
        terms.append(CueSpan('x', 0, 1, 0, 0))
        self.assertEqual(len(index.find_terms('東京')), 1)
        self.assertEqual(index.find_terms('absent'), [])
        self.assertEqual(index.find_readings('absent'), [])

    def test_spans_keep_their_line(self):
        index = _index(
            'first line',
            '<ruby>東京<rt>とうきょう</rt></ruby> second'
            )
        self.assertEqual(index.find_terms('first')[0].line, 0)
        self.assertEqual(index.find_terms('東京')[0].line, 1)
        self.assertEqual(index.find_terms('second')[0].line, 1)


class TestSpeakersAndLanguages(unittest.TestCase):

    def test_voice_span_speaker(self):
        index = _index('<v 田中>こんにちは</v>')
        spans = index.find_speaker('田中')
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].text, 'こんにちは')
        self.assertEqual(index.find_speaker('absent'), [])

    def test_voice_span_with_classes(self):
        index = _index('<v.quiet.slow 田中>こんにちは</v>')
        self.assertEqual(list(index.speakers), ['田中'])

    def test_more_than_one_voice_span(self):
        index = _index(
            '<v 田中>one</v> and <v 佐藤>two</v>'
            )
        self.assertEqual(
            index.find_speaker('田中')[0].text, 'one'
            )
        self.assertEqual(
            index.find_speaker('佐藤')[0].text, 'two'
            )

    def test_voice_span_across_lines(self):
        index = _index('<v 田中>line one', 'line two</v>')
        span = index.find_speaker('田中')[0]
        self.assertEqual(span.text, 'line one\nline two')
        self.assertEqual(span.line, 0)

    def test_invalid_voice_tag_is_not_a_speaker(self):
        index = _index('<v no closing angle')
        self.assertEqual(index.speakers, {})

    def test_voice_tag_without_a_name_is_not_a_speaker(self):
        index = _index('<v>no name</v>')
        self.assertEqual(index.speakers, {})

    def test_stray_closing_voice_tag(self):
        index = _index('text</v>more')
        self.assertEqual(index.speakers, {})

    def test_unclosed_voice_span(self):
        index = _index('<v 田中>こんにちは')
        self.assertEqual(index.find_speaker('田中')[0].text, 'こんにちは')

    def test_empty_voice_span_is_skipped(self):
        index = _index('<v 田中></v>after')
        self.assertEqual(index.speakers, {})
        self.assertIn('after', index.terms)

    def test_language_span(self):
        index = _index('<lang ja>日本語</lang>')
        span = index.language_spans[0]
        self.assertEqual(span.text, '日本語')
        self.assertEqual(span.lang, 'ja')
        self.assertEqual(span.line, 0)

    def test_language_span_with_classes(self):
        index = _index('<lang.kanji ja>日本語</lang>')
        self.assertEqual(index.language_spans[0].lang, 'ja')

    def test_language_span_across_lines(self):
        index = _index('<lang en>good', 'morning</lang>')
        span = index.language_spans[0]
        self.assertEqual(span.text, 'good\nmorning')
        self.assertEqual(span.line, 0)

    def test_lang_tag_without_language_is_ignored(self):
        index = _index('<lang>日本語</lang>')
        self.assertEqual(index.language_spans, [])

    def test_stray_closing_lang_tag(self):
        index = _index('text</lang>more')
        self.assertEqual(index.language_spans, [])

    def test_unclosed_language_span(self):
        index = _index('<lang ja>日本語')
        self.assertEqual(index.language_spans[0].text, '日本語')

    def test_empty_language_span_is_skipped(self):
        index = _index('<lang ja></lang>after')
        self.assertEqual(index.language_spans, [])


class TestCharacterReferences(unittest.TestCase):

    def test_named_entities_are_decoded_in_the_cue_text(self):
        index = _index(
            'a &amp; b &lt; c &gt; d'
            )
        ref = index.cues[0]
        self.assertEqual(ref.text, 'a & b < c > d')

    def test_numeric_entities_are_decoded(self):
        index = _index('&#65; &#x42;')
        self.assertEqual(index.cues[0].text, 'A B')
        self.assertIn('A', index.terms)
        self.assertIn('B', index.terms)

    def test_unknown_named_entity_is_kept_literal(self):
        index = _index('山&aelig;田')
        self.assertEqual(index.cues[0].text, '山&aelig;田')

    def test_reference_without_semicolon_is_literal(self):
        index = _index('a &amp b')
        self.assertEqual(index.cues[0].text, 'a &amp b')

    def test_out_of_range_numeric_reference_is_literal(self):
        index = _index('<ruby>x<rt>&#x110000;</rt></ruby>')
        self.assertEqual(index.rubies[0].reading, '&#x110000;')

    def test_surrogate_reference_becomes_replacement_character(self):
        index = _index('<ruby>x<rt>&#xD800;</rt></ruby>')
        self.assertEqual(index.rubies[0].reading, '�')

    def test_decode_entity(self):
        self.assertEqual(_decode_entity('&amp;'), '&')
        self.assertEqual(_decode_entity('&lt;'), '<')
        self.assertEqual(_decode_entity('&gt;'), '>')
        self.assertIsNone(_decode_entity('&quot;'))
        self.assertEqual(_decode_entity('&#65;'), 'A')
        self.assertEqual(_decode_entity('&#x42;'), 'B')
        self.assertEqual(_decode_entity('&#xD800;'), '�')
        self.assertIsNone(_decode_entity('&#x110000;'))
        self.assertIsNone(_decode_entity('&bogus;'))

    def test_iter_decoded(self):
        self.assertEqual(
            list(_iter_decoded('a&amp;b')),
            [('a', 0, 1), ('&', 1, 6), ('b', 6, 7)]
            )
        self.assertEqual(list(_iter_decoded('plain')), [('plain', 0, 5)])
        self.assertEqual(list(_iter_decoded('&#65;')), [('A', 0, 5)])


class TestTraceBack(unittest.TestCase):

    def test_cue_for_a_term_reading_and_ruby(self):
        captions = [
            Caption('00:00:01.000', '00:00:02.000', 'one'),
            Caption('00:00:03.000', '00:00:04.000',
                    '<ruby>東京<rt>とうきょう</rt></ruby>'),
            ]
        index = index_captions(captions)
        term = index.find_terms('one')[0]
        reading = index.find_readings('とうきょう')[0]
        ruby = index.rubies[0]
        self.assertEqual(index.cue_for(term).caption_index, 0)
        self.assertEqual(index.cue_for(reading).caption_index, 1)
        self.assertEqual(index.cue_for(ruby).start, '00:00:03.000')
        self.assertIs(index.cue_for(ruby).caption, captions[1])

    def test_speaker_and_language_hits_trace_back(self):
        index = _index(
            '<v 田中><lang ja>こんにちは</lang></v>'
            )
        speaker_span = index.find_speaker('田中')[0]
        lang_span = index.language_spans[0]
        self.assertEqual(index.cue_for(speaker_span).caption_index, 0)
        self.assertEqual(index.cue_for(lang_span).caption_index, 0)


class TestHelpers(unittest.TestCase):

    def test_parse_tag(self):
        self.assertEqual(_parse_tag('v.quiet.slow 田中'),
                         ('v', False, '田中'))
        self.assertEqual(_parse_tag('/v'), ('v', True, ''))
        self.assertEqual(_parse_tag('lang ja'), ('lang', False, 'ja'))
        self.assertEqual(_parse_tag('RT'), ('rt', False, ''))
        self.assertEqual(_parse_tag('ruby'), ('ruby', False, ''))

    def test_merge_ranges(self):
        self.assertEqual(_merge_ranges([]), [])
        self.assertEqual(_merge_ranges([(2, 4), (0, 2)]), [(0, 4)])
        self.assertEqual(
            _merge_ranges([(0, 5), (3, 4), (6, 8)]),
            [(0, 5), (6, 8)]
            )
        self.assertEqual(_merge_ranges([(0, 10), (2, 3)]), [(0, 10)])

    def test_uncovered_gaps(self):
        self.assertEqual(_uncovered_gaps([(2, 4)], 6),
                         [(0, 2), (4, 6)])
        self.assertEqual(_uncovered_gaps([(0, 6)], 6), [])
        self.assertEqual(_uncovered_gaps([], 3), [(0, 3)])


class TestLinearTime(unittest.TestCase):
    """The indexing stays linear in the length of the cue text."""

    LENGTH = 100_000
    LIMIT = 5

    def test_many_ruby_annotations(self):
        line = '<ruby>漢<rt>かん</rt></ruby>' * (self.LENGTH // 5)
        caption = Caption(text=line)
        start = time.perf_counter()
        index = index_captions([caption])
        self.assertLess(time.perf_counter() - start, self.LIMIT)
        self.assertEqual(len(index.rubies), self.LENGTH // 5)

    def test_long_plain_text(self):
        caption = Caption(text=' word ' * self.LENGTH)
        start = time.perf_counter()
        index = index_captions([caption])
        self.assertLess(time.perf_counter() - start, self.LIMIT)
        self.assertIn('word', index.terms)
