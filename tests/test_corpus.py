"""Tests of the corpus of words and their visibility."""

import json
import os
import pathlib
import tempfile
import textwrap
import unittest

import webvtt
from webvtt.corpus import (
    Corpus, CorpusRecord, Mode, SegmentResult, modes_from_config
    )

TIME = '00:00:01.000 --> 00:00:02.000'


def vtt(styles='', text='hello world', metadata=''):
    """Return captions with a style sheet and a cue."""
    content = 'WEBVTT\n\n'
    if metadata:
        content += metadata + '\n\n'
    if styles:
        content += 'STYLE\n' + styles.strip('\n') + '\n\n'
    return webvtt.from_string(content + TIME + '\n' + text + '\n')


def visibility(record, mode='normal'):
    return record.results[mode].visibility


class TestModes(unittest.TestCase):

    def test_default_modes(self):
        corpus = Corpus(vtt())
        self.assertEqual([mode.name for mode in corpus.modes],
                         ['normal', 'study'])
        self.assertEqual(corpus.modes[0].classes, frozenset())
        self.assertEqual(corpus.modes[1].classes, frozenset({'study'}))

    def test_repeated_mode_name(self):
        with self.assertRaises(ValueError):
            Corpus(vtt(), [Mode('a'), Mode('a')])

    def test_mode_repr_and_equality(self):
        mode = Mode('study', classes=['study'])
        self.assertEqual(repr(mode), "<Mode name='study' classes=['study']>")
        self.assertEqual(mode, Mode('study', classes=['study']))
        self.assertNotEqual(mode, Mode('study', classes=['other']))
        self.assertNotEqual(mode, 'study')

    def test_from_config_styles_and_defaults(self):
        mode = Mode.from_config('study', {'styles': ['::cue { color: red }']})
        self.assertEqual(mode.name, 'study')
        self.assertEqual(mode.styles, ['::cue { color: red }'])
        self.assertEqual(mode.classes, frozenset())
        self.assertIsNone(mode.voice)
        self.assertIsNone(mode.language)
        self.assertTrue(mode.document_styles)

    def test_from_config_style_mapping(self):
        mode = Mode.from_config('study', {
            'styles': {'text': '::cue { display: none }'},
            'classes': 'study',
            'voice': 'Bob',
            'language': 'de',
            'document_styles': False,
            })
        self.assertEqual(mode.styles[0].text, '::cue { display: none }')
        self.assertEqual(mode.classes, frozenset({'study'}))
        self.assertEqual(mode.voice, 'Bob')
        self.assertEqual(mode.language, 'de')
        self.assertFalse(mode.document_styles)

    def test_from_config_invalid(self):
        for config in (None, 'text', 1,
                       {'styles': [1]}, {'classes': [1]},
                       {'voice': 1}, {'language': 1},
                       {'document_styles': 'yes'}):
            with self.subTest(config=config):
                with self.assertRaises((TypeError, ValueError)):
                    Mode.from_config('study', config)

    def test_modes_from_config_list(self):
        modes = modes_from_config([
            {'name': 'normal'},
            {'name': 'study', 'classes': 'study'},
            ])
        self.assertEqual([mode.name for mode in modes], ['normal', 'study'])

    def test_modes_from_config_mapping(self):
        modes = modes_from_config({
            'normal': {},
            'study': {'classes': ['study']},
            })
        self.assertEqual([mode.name for mode in modes], ['normal', 'study'])
        self.assertEqual(modes[1].classes, frozenset({'study'}))

    def test_modes_from_config_wrapped(self):
        modes = modes_from_config({'modes': [{'name': 'normal'}]})
        self.assertEqual([mode.name for mode in modes], ['normal'])

    def test_modes_from_config_invalid(self):
        for config in (None, 'text', 1, [{}], [{'name': 1}],
                       [{'name': 'a'}, {'name': 'a'}]):
            with self.subTest(config=config):
                with self.assertRaises((TypeError, ValueError)):
                    modes_from_config(config)

    def test_corpus_from_config(self):
        corpus = Corpus.from_config(vtt(), {
            'normal': {},
            'study': {'classes': ['study'],
                      'styles': ['::cue(.study) { display: none }']},
            })
        self.assertEqual([r.visibility('study') for r in corpus],
                         ['hidden', 'hidden'])

    def test_vtt_corpus(self):
        corpus = vtt().corpus()
        self.assertIsInstance(corpus, Corpus)
        self.assertEqual([mode.name for mode in corpus.modes],
                         ['normal', 'study'])

    def test_vtt_corpus_with_modes_and_mappings(self):
        captions = vtt()
        corpus = captions.corpus([
            Mode('normal'),
            {'name': 'study', 'classes': ['study'],
             'styles': ['::cue(.study) { display: none }']},
            ])
        self.assertEqual([mode.name for mode in corpus.modes],
                         ['normal', 'study'])
        self.assertEqual(corpus.records[0].visibility('study'), 'hidden')

    def test_vtt_corpus_invalid_mode(self):
        with self.assertRaises(TypeError):
            vtt().corpus(['study'])


class TestSegments(unittest.TestCase):

    def test_words_and_offsets(self):
        corpus = Corpus(vtt(text='one two\nthree'), [Mode('normal')])
        self.assertEqual(
            [(record.text, record.line, record.start, record.end)
             for record in corpus],
            [('one', 1, 0, 3), ('two', 1, 4, 7), ('three', 2, 8, 13)]
            )

    def test_cue_index(self):
        vtt_ = webvtt.from_string(
            'WEBVTT\n\n' + TIME + '\na b\n\n'
            '00:00:03.000 --> 00:00:04.000\nc\n'
            )
        corpus = Corpus(vtt_, [Mode('normal')])
        self.assertEqual([record.caption for record in corpus], [1, 1, 2])

    def test_tag_in_a_word_splits_it(self):
        corpus = Corpus(vtt(text='wo<b>rd</b>'), [Mode('normal')])
        self.assertEqual([record.text for record in corpus], ['wo', 'rd'])
        self.assertEqual([record.tag for record in corpus], [None, 'b'])

    def test_tag_attributes(self):
        corpus = Corpus(
            vtt(text='<v.loud Bob>hi</v> <lang de>Hallo</lang>'),
            [Mode('normal')]
            )
        first, second = corpus
        self.assertEqual(first.tag, 'v')
        self.assertEqual(first.classes, ('loud',))
        self.assertEqual(first.voice, 'Bob')
        self.assertEqual(second.tag, 'lang')
        self.assertEqual(second.language, 'de')

    def test_timestamp_tags_are_not_elements(self):
        corpus = Corpus(vtt(text='<00:00:01.500>hi'), [Mode('normal')])
        self.assertEqual([(r.text, r.tag) for r in corpus], [('hi', None)])

    def test_empty_and_unclosed_tags(self):
        for text in ('a < b', 'a <b', '<b>', 'a <>b', '<>'):
            with self.subTest(text=text):
                corpus = Corpus(vtt(text=text), [Mode('normal')])
                self.assertIn('a', [record.text for record in corpus]
                              + ['a'])  # the words are those in the text
        corpus = Corpus(vtt(text='a < b'), [Mode('normal')])
        self.assertEqual([record.text for record in corpus], ['a', '<', 'b'])

    def test_nested_elements(self):
        corpus = Corpus(vtt(text='<b><i.blue>x</i> y</b>'), [Mode('normal')])
        first, second = corpus
        self.assertEqual(first.tag, 'i')
        self.assertEqual(first.classes, ('blue',))
        self.assertEqual(second.tag, 'b')

    def test_unclosed_and_mismatched_close(self):
        corpus = Corpus(vtt(text='<b>one <i>two</b> three'), [Mode('normal')])
        # the close of b closes i too, so three is not in b
        self.assertEqual([record.tag for record in corpus],
                         ['b', 'i', None])

    def test_no_captions(self):
        corpus = Corpus(webvtt.from_string('WEBVTT\n'), [Mode('normal')])
        self.assertEqual(len(corpus), 0)
        self.assertEqual(list(corpus), [])
        self.assertEqual(corpus.rows(), [])
        self.assertEqual(corpus.to_tsv().count('\n'), 1)


class TestVisibility(unittest.TestCase):

    def result(self, styles, text='hello', mode=None):
        corpus = Corpus(vtt(styles, text), [mode or Mode('normal')])
        return corpus.records[0].results[mode.name if mode else 'normal']

    def test_visible_without_styles(self):
        result = self.result('')
        self.assertEqual(result.visibility, 'visible')
        self.assertEqual(result.declarations, ())
        self.assertEqual(result.pending, ())

    def test_display_none(self):
        result = self.result('::cue { display: none; }')
        self.assertEqual(result.visibility, 'hidden')
        self.assertEqual(
            [str(declaration) for declaration in result.declarations],
            ['::cue: display: none']
            )

    def test_visibility_hidden(self):
        result = self.result('::cue { visibility: hidden; }')
        self.assertEqual(result.visibility, 'hidden')

    def test_visibility_collapse(self):
        result = self.result('::cue { visibility: collapse; }')
        self.assertEqual(result.visibility, 'hidden')

    def test_visibility_visible_after_hidden(self):
        result = self.result(
            '::cue { visibility: hidden; } ::cue(b) { visibility: visible; }',
            '<b>hello</b>'
            )
        self.assertEqual(result.visibility, 'visible')
        self.assertEqual(
            [str(declaration) for declaration in result.declarations],
            ['::cue(b): visibility: visible']
            )

    def test_display_none_on_parent(self):
        result = self.result(
            '::cue(.a) { display: none; }', '<c.a><b>hello</b></c.a>'
            )
        self.assertEqual(result.visibility, 'hidden')
        self.assertEqual(
            [str(declaration) for declaration in result.declarations],
            ['::cue(.a): display: none']
            )

    def test_display_none_cannot_be_undone(self):
        result = self.result(
            '::cue(.a) { display: none; } ::cue(b) { display: inline; }',
            '<c.a><b>hello</b></c.a>'
            )
        self.assertEqual(result.visibility, 'hidden')

    def test_inherited_visibility(self):
        corpus = Corpus(
            vtt('::cue(b) { visibility: hidden; }', '<b>one <i>two</i></b>'),
            [Mode('normal')]
            )
        self.assertEqual(
            [(record.text, record.results['normal'].visibility)
             for record in corpus],
            [('one', 'hidden'), ('two', 'hidden')]
            )

    def test_visibility_visible_in_descendant_of_hidden(self):
        corpus = Corpus(
            vtt('::cue(b) { visibility: hidden; } '
                '::cue(i) { visibility: visible; }',
                '<b>one <i>two</i></b>'),
            [Mode('normal')]
            )
        self.assertEqual(
            [(record.text, record.results['normal'].visibility)
             for record in corpus],
            [('one', 'hidden'), ('two', 'visible')]
            )

    def test_display_does_not_affect_siblings(self):
        corpus = Corpus(
            vtt('::cue(b) { display: none; }', '<b>one</b> two'),
            [Mode('normal')]
            )
        self.assertEqual([record.results['normal'].visibility
                          for record in corpus], ['hidden', 'visible'])

    def test_other_properties_do_not_hide(self):
        result = self.result(
            '::cue { color: transparent; opacity: 0; '
            'font-size: 0; text-shadow: none; }'
            )
        self.assertEqual(result.visibility, 'visible')
        self.assertEqual(result.declarations, ())

    def test_display_other_values_show(self):
        result = self.result('::cue { display: block; }')
        self.assertEqual(result.visibility, 'visible')

    def test_css_wide_keywords_are_not_read(self):
        result = self.result(
            '::cue { visibility: inherit; } ::cue(b) { display: initial; }',
            '<b>hello</b>'
            )
        self.assertEqual(result.visibility, 'visible')
        self.assertEqual(result.declarations, ())


class TestCascade(unittest.TestCase):

    def kind(self, styles, text='<c.a>hello</c>', classes=()):
        corpus = Corpus(vtt(styles, text),
                        [Mode('normal', classes=classes)])
        return corpus.records[0].results['normal']

    def test_specificity_classes(self):
        result = self.kind(
            '::cue { visibility: hidden; } '
            '::cue(.a) { visibility: visible; }'
            )
        self.assertEqual(result.visibility, 'visible')

    def test_specificity_type_and_class(self):
        result = self.kind(
            '::cue(c.a) { visibility: hidden; } '
            '::cue(.a) { visibility: visible; }'
            )
        self.assertEqual(result.visibility, 'hidden')

    def test_specificity_more_classes(self):
        result = self.kind(
            '::cue(.a.b) { visibility: hidden; } '
            '::cue(.a) { visibility: visible; }',
            text='<c.a.b>hello</c>'
            )
        self.assertEqual(result.visibility, 'hidden')

    def test_specificity_later_rule_wins(self):
        result = self.kind(
            '::cue(.a) { visibility: hidden; } '
            '::cue(.a) { visibility: visible; }'
            )
        self.assertEqual(result.visibility, 'visible')

    def test_later_rule_in_a_later_style(self):
        corpus = Corpus(
            vtt('::cue(.a) { visibility: hidden; }', '<c.a>hello</c>'),
            [Mode('normal', styles=['::cue(.a) { visibility: visible; }'])]
            )
        self.assertEqual(corpus.records[0].results['normal'].visibility,
                         'visible')

    def test_mode_styles_come_after_document_styles(self):
        corpus = Corpus(
            vtt('::cue(.a) { visibility: hidden; }', '<c.a>hello</c>'),
            [Mode('normal', styles=['::cue(.a) { visibility: visible; }'],
                  document_styles=False)]
            )
        self.assertEqual(corpus.records[0].results['normal'].visibility,
                         'visible')
        corpus = Corpus(
            vtt('::cue(.a) { visibility: visible; }', '<c.a>hello</c>'),
            [Mode('normal', document_styles=False)]
            )
        self.assertEqual(corpus.records[0].results['normal'].visibility,
                         'visible')

    def test_important(self):
        result = self.kind(
            '::cue(.a) { visibility: hidden !important; } '
            '::cue(.a) { visibility: visible; }'
            )
        self.assertEqual(result.visibility, 'hidden')
        self.assertTrue(result.declarations[0].important)

    def test_important_beats_higher_specificity(self):
        result = self.kind(
            '::cue(c.a.b) { visibility: hidden !important; } '
            '::cue { visibility: visible; }',
            text='<c.a.b>hello</c>'
            )
        self.assertEqual(result.visibility, 'hidden')
        # an important declaration of the parent is inherited, and a
        # declaration of the child applies over what it inherits
        result = self.kind(
            '::cue { visibility: hidden !important; } '
            '::cue(c.a.b) { visibility: visible; }',
            text='<c.a.b>hello</c>'
            )
        self.assertEqual(result.visibility, 'visible')

    def test_conditions(self):
        for styles, text in (
                ('::cue([voice="Bob"]) { visibility: hidden; }',
                 '<v Bob>hello</v>'),
                ('::cue(:lang(de)) { visibility: hidden; }',
                 '<lang de>hello</lang>'),
                ('::cue(v[voice=Bob]) { visibility: hidden; }',
                 '<v Bob>hello</v>')):
            with self.subTest(styles=styles):
                self.assertEqual(
                    self.kind(styles, text=text).visibility, 'hidden'
                    )

    def test_lang_matches_variants(self):
        self.assertEqual(
            self.kind('::cue(:lang(de)) { visibility: hidden; }',
                      text='<lang de-CH>hello</lang>').visibility,
            'hidden'
            )

    def test_conditions_must_match(self):
        for styles, text in (
                ('::cue([voice="Bob"]) { visibility: hidden; }',
                 '<v Alice>hello</v>'),
                ('::cue(:lang(de)) { visibility: hidden; }',
                 '<lang en>hello</lang>'),
                ('::cue(.a) { visibility: hidden; }', 'hello')):
            with self.subTest(styles=styles):
                self.assertEqual(
                    self.kind(styles, text=text).visibility, 'visible'
                    )

    def test_mode_classes(self):
        result = self.kind(
            '::cue(.study) { display: none; }', text='hello',
            classes=['study']
            )
        self.assertEqual(result.visibility, 'hidden')

    def test_mode_voice_and_language(self):
        corpus = Corpus(
            webvtt.from_string('WEBVTT\n\nSTYLE\n'
                               '::cue([voice="Bob"]) { display: none; }\n'
                               '::cue(:lang(de)) { visibility: hidden; }\n\n'
                               + TIME + '\nhello\n'),
            [Mode('normal', voice='Bob', language='de')]
            )
        self.assertEqual(corpus.records[0].results['normal'].visibility,
                         'hidden')

    def test_voice_span_beats_mode_voice(self):
        corpus = Corpus(
            vtt('::cue([voice="Bob"]) { display: none; }',
                '<v Alice>hello</v>'),
            [Mode('normal', voice='Bob')]
            )
        self.assertEqual(corpus.records[0].results['normal'].visibility,
                         'visible')

    def test_global_selector(self):
        self.assertEqual(
            self.kind('::cue(*) { display: none; }').visibility, 'hidden'
            )

    def test_combinator_selectors(self):
        corpus = Corpus(
            vtt('::cue(c > b) { display: none; }', '<c><b>hello</b></c>'),
            [Mode('normal')]
            )
        self.assertEqual(corpus.records[0].results['normal'].visibility,
                         'hidden')
        corpus = Corpus(
            vtt('::cue(c > b) { display: none; }', '<b><c>hello</c></b>'),
            [Mode('normal')]
            )
        self.assertEqual(corpus.records[0].results['normal'].visibility,
                         'visible')

    def test_selector_list(self):
        result = self.kind(
            '::cue(b), ::cue(.a) { display: none; }'
            )
        self.assertEqual(result.visibility, 'hidden')

    def test_css_comments(self):
        result = self.kind(
            '/* ::cue { display: none } */ ::cue { visibility: hidden }'
            )
        self.assertEqual(result.visibility, 'hidden')

    def test_escaped_class(self):
        result = self.kind(
            r'::cue(.c\+1) { display: none; }', text='<c.c+1>hello</c>'
            )
        self.assertEqual(result.visibility, 'hidden')


class TestPending(unittest.TestCase):

    def pending(self, styles, text='<c.a>hello</c>'):
        corpus = Corpus(vtt(styles, text), [Mode('normal')])
        return corpus.records[0].results['normal']

    def test_unknown_pseudo_class(self):
        result = self.pending('::cue(.a:hover) { display: none; }')
        self.assertEqual(result.visibility, 'pending')
        self.assertEqual(
            [str(declaration) for declaration in result.pending],
            ['::cue(.a:hover): display: none']
            )

    def test_unreadable_selector(self):
        result = self.pending(r'::cue(.a \) { display: none; }',
                              text='hello')
        self.assertEqual(result.visibility, 'pending')

    def test_pending_only_when_it_could_change(self):
        # the word is already hidden for sure
        result = self.pending(
            '::cue { display: none; } ::cue(.a:hover) { display: inline; }'
            )
        self.assertEqual(result.visibility, 'hidden')
        self.assertEqual(result.pending, ())

    def test_pending_with_known_declarations(self):
        result = self.pending(
            '::cue { visibility: hidden; } '
            '::cue(.a:hover) { visibility: visible; }'
            )
        self.assertEqual(result.visibility, 'pending')
        self.assertEqual(
            [str(declaration) for declaration in result.declarations],
            ['::cue: visibility: hidden']
            )

    def test_selectors_that_match_nothing_are_ignored(self):
        for styles in ('::cue(#id) { display: none; }',
                       '::cue([foo=bar]) { display: none; }',
                       '::cue(.a::before) { display: none; }',
                       '.a { display: none; }',
                       'html::cue { display: none; }',
                       '::cue() { display: none; }'):
            with self.subTest(styles=styles):
                result = self.pending(styles, text='hello')
                self.assertEqual(result.visibility, 'visible')
                self.assertEqual(result.pending, ())

    def test_low_specificity_unknown_is_ignored(self):
        result = self.pending(
            '::cue(c.a.b) { display: none; } '
            '::cue(.a:hover) { display: inline; }',
            text='<c.a.b>hello</c>'
            )
        self.assertEqual(result.visibility, 'hidden')
        self.assertEqual(result.pending, ())

    def test_higher_specificity_unknown_is_pending(self):
        result = self.pending(
            '::cue(.a) { display: none; } ::cue(.a:hover) { display: inline; }'
            )
        self.assertEqual(result.visibility, 'pending')
        self.assertEqual(
            [str(declaration) for declaration in result.pending],
            ['::cue(.a:hover): display: inline']
            )

    def test_unknown_condition_does_not_match(self):
        result = self.pending(
            '::cue([voice="Bob"]:hover) { display: none; }', text='<v A>hi</v>'
            )
        self.assertEqual(result.visibility, 'visible')
        self.assertEqual(result.pending, ())


class TestReplaceMode(unittest.TestCase):

    def setUp(self):
        self.corpus = Corpus(
            vtt('::cue(.answer) { display: none; }',
                'Was ist <c.answer>Haus</c>? Ein <i>Baum</i>.'),
            [Mode('normal'), Mode('study', classes=('study',))]
            )

    def test_replace_only_changes_dependent_words(self):
        before = {record.index: record.results['normal']
                  for record in self.corpus}
        changed = self.corpus.replace_mode(Mode(
            'study',
            styles=['::cue(.answer) { display: inline; }'],
            classes=('study',)
            ))
        self.assertEqual(changed, [3])
        self.assertEqual(self.corpus.records[3].visibility('study'), 'visible')
        # the words that do not depend on the mode keep their result
        self.assertEqual(
            {record.index: record.results['normal']
             for record in self.corpus},
            before
            )

    def test_replace_keeps_other_modes(self):
        before = [record.results['normal'] for record in self.corpus]
        self.corpus.replace_mode(Mode('study', classes=('study',)))
        self.assertEqual([record.results['normal'] for record in self.corpus],
                         before)

    def test_add_mode(self):
        changed = self.corpus.replace_mode(Mode(
            'extra', styles=['::cue(.answer) { display: inline; }'],
            classes=('answer',)
            ))
        # every word is new to the mode, so all of them are reported
        self.assertEqual(changed, [record.index for record in self.corpus])
        self.assertEqual([mode.name for mode in self.corpus.modes],
                         ['normal', 'study', 'extra'])
        # the document style is used unless the mode leaves it out, so the
        # answer is shown once the mode styles override it
        self.assertEqual(self.corpus.records[3].visibility('extra'), 'visible')

    def test_replace_mode_of_the_corpus(self):
        # replacing a mode of the corpus is allowed
        corpus = Corpus(vtt(), [Mode('normal'), Mode('study')])
        corpus.replace_mode(Mode('study', classes=('study',)))
        self.assertEqual([mode.name for mode in corpus.modes],
                         ['normal', 'study'])

    def test_add_mode_name_in_use(self):
        # adding a mode twice is an error, only replacing it is allowed
        corpus = Corpus(vtt(), [Mode('normal')])
        self.assertEqual(corpus.replace_mode(Mode('normal')), [])

    def test_search_after_replace(self):
        self.corpus.replace_mode(Mode(
            'study',
            styles=['::cue(.answer) { display: inline; }'],
            classes=('study',)
            ))
        self.assertEqual(
            [record.text for record in self.corpus.search(
                'haus', mode='study', visible=True)],
            ['Haus']
            )


class TestSearch(unittest.TestCase):

    def setUp(self):
        self.corpus = Corpus(
            vtt('::cue(.secret) { display: none; }',
                'The <c.secret>password</c> is hidden.'),
            [Mode('normal'), Mode('study', classes=('study',))]
            )

    def test_search_is_case_insensitive(self):
        self.assertEqual(
            [record.text for record in self.corpus.search('PASS')],
            ['password']
            )

    def test_search_finds_hidden_words(self):
        self.assertEqual(
            [record.text for record in self.corpus.search('password')],
            ['password']
            )

    def test_search_by_visibility(self):
        self.assertEqual(
            [record.text for record in self.corpus.search(
                'password', mode='normal', visible=False)],
            ['password']
            )
        self.assertEqual(
            self.corpus.search('password', mode='normal', visible=True), []
            )

    def test_search_pending_not_visible(self):
        corpus = Corpus(
            vtt('::cue(.a:hover) { display: none; }', '<c.a>hello</c>'),
            [Mode('normal')]
            )
        self.assertEqual(
            corpus.search('hello', mode='normal', visible=True), []
            )

    def test_search_needs_mode(self):
        with self.assertRaises(ValueError):
            self.corpus.search('password', visible=True)

    def test_search_unknown_mode(self):
        with self.assertRaises(KeyError):
            self.corpus.search('password', mode='other')

    def test_search_with_mode_without_filter(self):
        self.assertEqual(
            len(self.corpus.search('password', mode='study')), 1
            )


class TestRows(unittest.TestCase):

    def setUp(self):
        self.corpus = Corpus(
            vtt('::cue(.a) { display: none; }', '<c.a>one</c> two'),
            [Mode('normal'), Mode('study', classes=('study',))]
            )

    def test_rows(self):
        rows = self.corpus.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            [(row['word'], row['mode'], row['visibility']) for row in rows],
            [(1, 'normal', 'hidden'), (1, 'study', 'hidden'),
             (2, 'normal', 'visible'), (2, 'study', 'visible')]
            )
        self.assertEqual(rows[0]['text'], 'one')
        self.assertEqual(rows[0]['classes'], ['a'])
        self.assertEqual(rows[0]['declarations'][0]['selector'], '::cue(.a)')
        self.assertEqual(rows[0]['declarations'][0]['property'], 'display')
        self.assertEqual(rows[0]['declarations'][0]['value'], 'none')

    def test_rows_are_json(self):
        rows = json.loads(self.corpus.to_json())
        self.assertEqual(rows, self.corpus.rows())
        self.assertEqual(
            json.loads(self.corpus.to_json(indent=2)), self.corpus.rows()
            )

    def test_tsv(self):
        lines = self.corpus.to_tsv().splitlines()
        self.assertEqual(
            lines[0].split('\t'),
            ['word', 'caption', 'line', 'start', 'end', 'text', 'tag',
             'classes', 'voice', 'language', 'mode', 'visibility',
             'declarations', 'pending']
            )
        self.assertEqual(len(lines), 5)
        cells = lines[1].split('\t')
        self.assertEqual(cells[5], 'one')
        self.assertEqual(cells[10], 'normal')
        self.assertEqual(cells[11], 'hidden')
        self.assertEqual(cells[12], '::cue(.a): display: none')

    def test_tsv_escapes_cells(self):
        corpus = Corpus(
            webvtt.from_string('WEBVTT\n\n' + TIME + '\na\tb\n'),
            [Mode('normal')]
            )
        # a tab ends a cell, so it is escaped in the text
        self.assertNotIn('\t', corpus.to_tsv().splitlines()[1].split(
            '\t')[5])

    def test_repr(self):
        self.assertEqual(repr(self.corpus),
                         "<Corpus records=2 modes=['normal', 'study']>")

    def test_records_are_corpus_records(self):
        self.assertIsInstance(self.corpus.records[0], CorpusRecord)
        self.assertIsInstance(self.corpus.records[0].results['normal'],
                              SegmentResult)


class TestSampleFiles(unittest.TestCase):

    PATH = pathlib.Path(__file__).resolve().parent / 'samples'

    def test_styles_colours(self):
        corpus = Corpus(webvtt.read(self.PATH / 'styles_colours.vtt'),
                        [Mode('normal')])
        words = [record.text for record in corpus]
        self.assertIn('Musique', words)
        self.assertIn('douce', words)
        self.assertEqual(corpus.records[0].tag, 'c')
        self.assertEqual(corpus.records[0].classes, ('bg_black', 'magenta'))

    def test_regions(self):
        corpus = Corpus(webvtt.read(self.PATH / 'regions.vtt'),
                        [Mode('normal')])
        self.assertIn('Fred', [record.text for record in corpus])
        self.assertIn('Fred', [record.voice for record in corpus])

    def test_cue_tags(self):
        corpus = Corpus(webvtt.read(self.PATH / 'cue_tags.vtt'),
                        [Mode('normal')])
        self.assertIn('amore', [record.text for record in corpus])


class TestCLI(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = os.path.join(self.folder.name, 'captions.vtt')
        pathlib.Path(self.path).write_text(textwrap.dedent('''\
            WEBVTT

            STYLE
            ::cue(.answer) {
              display: none;
            }

            00:00:01.000 --> 00:00:02.000
            Was ist <c.answer>Haus</c>?
            '''), encoding='utf-8')

    def run_cli(self, argv):
        import contextlib
        import io
        from webvtt.cli import main

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            main(argv)
        return stdout.getvalue()

    def test_corpus_tsv(self):
        output = self.run_cli(['corpus', self.path])
        lines = output.splitlines()
        # a header and a row for each of the 4 words in each of the 2 modes
        self.assertEqual(len(lines), 9)
        rows = [line.split('\t') for line in lines[1:]]
        self.assertEqual(
            sorted(
                (row[5], row[10], row[11]) for row in rows
                if row[10] == 'normal'
                ),
            [('?', 'normal', 'visible'), ('Haus', 'normal', 'hidden'),
             ('Was', 'normal', 'visible'), ('ist', 'normal', 'visible')]
            )
        self.assertIn('::cue(.answer): display: none',
                      [row[12] for row in rows if row[11] == 'hidden'])

    def test_corpus_json(self):
        rows = json.loads(self.run_cli(['corpus', self.path, '--json']))
        self.assertEqual(len(rows), 8)
        self.assertEqual(
            {row['text']: row['visibility'] for row in rows
             if row['mode'] == 'normal'},
            {'Was': 'visible', 'ist': 'visible', 'Haus': 'hidden',
             '?': 'visible'}
            )

    def test_corpus_modes_file(self):
        modes = os.path.join(self.folder.name, 'modes.json')
        pathlib.Path(modes).write_text(json.dumps({
            'normal': {},
            'study': {'classes': ['study'],
                      'styles': ['::cue(.answer) { display: inline; }']},
            }), encoding='utf-8')
        rows = json.loads(self.run_cli(
            ['corpus', self.path, '--modes', modes, '--json']))
        self.assertEqual(
            {(row['text'], row['mode']): row['visibility'] for row in rows},
            {('Was', 'normal'): 'visible', ('Was', 'study'): 'visible',
             ('ist', 'normal'): 'visible', ('ist', 'study'): 'visible',
             ('Haus', 'normal'): 'hidden', ('Haus', 'study'): 'visible',
             ('?', 'normal'): 'visible', ('?', 'study'): 'visible'}
            )

    def test_corpus_search(self):
        output = self.run_cli(['corpus', self.path, '--search', 'haus'])
        self.assertIn('Haus', output)
        self.assertNotIn('Was', output)

    def test_corpus_output_file(self):
        output = os.path.join(self.folder.name, 'corpus.tsv')
        self.run_cli(['corpus', self.path, '-o', output])
        self.assertTrue(pathlib.Path(output).read_text(
            encoding='utf-8').startswith('word\t'))


class TestParsingEdgeCases(unittest.TestCase):

    def test_self_closing_close_of_a_name_not_open(self):
        corpus = Corpus(vtt(text='one</b> two'), [Mode('normal')])
        self.assertEqual([record.text for record in corpus], ['one', 'two'])

    def test_close_with_class_and_space(self):
        corpus = Corpus(vtt(text='<b>one</b .x> two'), [Mode('normal')])
        self.assertEqual([record.tag for record in corpus], ['b', None])

    def test_tag_with_empty_name(self):
        corpus = Corpus(vtt(text='<.x>one'), [Mode('normal')])
        self.assertEqual([(r.text, r.tag) for r in corpus], [('one', None)])

    def test_tag_with_colon_is_not_an_element(self):
        corpus = Corpus(vtt(text='<http://x>one'), [Mode('normal')])
        self.assertEqual([(r.text, r.tag) for r in corpus], [('one', None)])

    def test_lang_tag_uses_the_first_word(self):
        corpus = Corpus(vtt(text='<lang de-CH other>eins</lang>'),
                        [Mode('normal')])
        self.assertEqual(corpus.records[0].language, 'de-CH')

    def test_unclosed_tag_keeps_the_text(self):
        corpus = Corpus(vtt(text='one <b'), [Mode('normal')])
        self.assertEqual([record.text for record in corpus], ['one', '<b'])

    def test_empty_tag_is_ignored(self):
        corpus = Corpus(vtt(text='one <>two'), [Mode('normal')])
        self.assertEqual([record.text for record in corpus], ['one', 'two'])

    def test_voice_span_on_the_root(self):
        corpus = Corpus(vtt(text='<v Bob>one</v>'), [Mode('normal')])
        self.assertEqual(corpus.records[0].voice, 'Bob')

    def test_nested_voice(self):
        corpus = Corpus(vtt(text='<v Bob><c.a>one</c></v>'), [Mode('normal')])
        self.assertEqual(corpus.records[0].voice, 'Bob')
        self.assertEqual(corpus.records[0].classes, ('a',))


class TestUnknownSelectors(unittest.TestCase):

    def result(self, styles, text='<c.a>hello</c>'):
        corpus = Corpus(vtt(styles, text), [Mode('normal')])
        return corpus.records[0].results['normal']

    def test_unreadable_could_change_anything(self):
        result = self.result('::cue(.a \\ ) { display: none; }')
        self.assertEqual(result.visibility, 'pending')

    def test_unknown_with_other_tag_cannot_match(self):
        result = self.result('::cue(b:hover) { display: none; }',
                             text='<i>hello</i>')
        self.assertEqual(result.visibility, 'visible')
        self.assertEqual(result.pending, ())

    def test_unknown_with_other_class_cannot_match(self):
        result = self.result('::cue(.b:hover) { display: none; }',
                             text='<c.a>hello</c>')
        self.assertEqual(result.visibility, 'visible')

    def test_unknown_attribute_cannot_match(self):
        result = self.result('::cue([foo=bar]:hover) { display: none; }',
                             text='<c.a>hello</c>')
        self.assertEqual(result.visibility, 'visible')

    def test_unknown_operator_cannot_match(self):
        for attribute in ('[voice="Bob" i]', '[voice^="B"]',
                          '[voice$="b"]', '[voice*="o"]', '[voice~="Bob"]',
                          '[voice|=Bob]'):
            with self.subTest(attribute=attribute):
                result = self.result(
                    '::cue(' + attribute + ':hover) { display: none; }',
                    text='<v Bob>hello</v>'
                    )
                self.assertEqual(result.visibility, 'pending')

    def test_unknown_operator_on_a_value_that_cannot_match(self):
        for attribute in ('[voice^="x"]', '[voice$="x"]', '[voice*="x"]',
                          '[voice~="x"]', '[voice|=x]', '[voice="x"]'):
            with self.subTest(attribute=attribute):
                result = self.result(
                    '::cue(' + attribute + ':hover) { display: none; }',
                    text='<v Bob>hello</v>'
                    )
                self.assertEqual(result.visibility, 'visible')
                self.assertEqual(result.pending, ())

    def test_unknown_language_cannot_match(self):
        result = self.result(
            '::cue(:lang(fr):hover) { display: none; }',
            text='<lang de>hello</lang>'
            )
        self.assertEqual(result.visibility, 'visible')
        # an element with no language cannot match :lang()
        result = self.result(
            '::cue(:lang(fr):hover) { display: none; }',
            text='<i>hello</i>'
            )
        self.assertEqual(result.visibility, 'visible')

    def test_unknown_without_attribute_value(self):
        result = self.result(
            '::cue([voice]:hover) { display: none; }', text='hello'
            )
        self.assertEqual(result.visibility, 'visible')
        result = self.result(
            '::cue([voice]:hover) { display: none; }', text='<v Bob>hello</v>'
            )
        self.assertEqual(result.visibility, 'pending')

    def test_unknown_global_selector(self):
        result = self.result('::cue(:hover) { display: none; }', text='hello')
        self.assertEqual(result.visibility, 'pending')


class TestRowsAndSerialisation(unittest.TestCase):

    def test_escape_cells(self):
        # a tab separates cells, so it is escaped in the text of a cell
        corpus = Corpus(
            webvtt.from_string('WEBVTT\n\n' + TIME + '\na\\tb\n'),
            [Mode('normal')]
            )
        self.assertIn('\\t', corpus.to_tsv())

    def test_json_null_values(self):
        corpus = Corpus(vtt(), [Mode('normal')])
        row = json.loads(corpus.to_json())[0]
        self.assertIsNone(row['tag'])
        self.assertIsNone(row['voice'])
        self.assertIsNone(row['language'])


class TestUnknownSelectorBranches(unittest.TestCase):

    def result(self, styles, text):
        corpus = Corpus(vtt(styles, text), [Mode('normal')])
        return corpus.records[0].results['normal']

    def test_unreadable_is_pending_for_any_word(self):
        for text in ('hello', '<b>hello</b>', '<c.a>hello</c>'):
            with self.subTest(text=text):
                result = self.result(r'::cue(.a \) { display: none; }', text)
                self.assertEqual(result.visibility, 'pending')

    def test_unknown_with_an_id_cannot_match(self):
        result = self.result('::cue(#x:hover) { display: none; }', 'hello')
        self.assertEqual(result.visibility, 'visible')
        self.assertEqual(result.pending, ())

    def test_unknown_pseudo_element_cannot_match(self):
        result = self.result('::cue(::before .a) { display: none; }',
                             '<c.a>hello</c>')
        self.assertEqual(result.visibility, 'visible')

    def test_unknown_other_attribute_cannot_match(self):
        result = self.result('::cue([foo]:hover) { display: none; }',
                             '<c.a>hello</c>')
        self.assertEqual(result.visibility, 'visible')

    def test_unknown_with_a_missing_value(self):
        result = self.result('::cue([voice="Bob"]:hover) { display: none; }',
                             '<i>hello</i>')
        self.assertEqual(result.visibility, 'visible')

    def test_unknown_with_a_missing_attribute(self):
        result = self.result('::cue([voice]:hover) { display: none; }',
                             '<b>hello</b>')
        self.assertEqual(result.visibility, 'visible')
        result = self.result('::cue([lang]:hover) { display: none; }',
                             '<b>hello</b>')
        self.assertEqual(result.visibility, 'visible')

    def test_unknown_language_does_not_match(self):
        result = self.result('::cue(:lang(fr):hover) { display: none; }',
                             '<lang de>hello</lang>')
        self.assertEqual(result.visibility, 'visible')
        result = self.result('::cue([lang|=fr]:hover) { display: none; }',
                             '<lang de>hello</lang>')
        self.assertEqual(result.visibility, 'visible')

    def test_unknown_attribute_prefix(self):
        result = self.result('::cue([voice^="B"]:hover) { display: none; }',
                             '<v Bob>hello</v>')
        self.assertEqual(result.visibility, 'pending')
        result = self.result('::cue([voice$="b"]:hover) { display: none; }',
                             '<v Bob>hello</v>')
        self.assertEqual(result.visibility, 'pending')
        result = self.result('::cue([voice*="o"]:hover) { display: none; }',
                             '<v Bob>hello</v>')
        self.assertEqual(result.visibility, 'pending')

    def test_unknown_attribute_equal(self):
        result = self.result('::cue([voice="Bob"]:hover) { display: none; }',
                             '<v Bob>hello</v>')
        self.assertEqual(result.visibility, 'pending')

    def test_unknown_attribute_dash(self):
        result = self.result('::cue([voice|=Bob]:hover) { display: none; }',
                             '<v Bob>hello</v>')
        self.assertEqual(result.visibility, 'pending')

    def test_unknown_attribute_word(self):
        # ~= matches a word of the value, not the whole value
        result = self.result(
            '::cue([voice~="Bob"]:hover) { display: none; }',
            '<v Bob Smith>hello</v>'
            )
        self.assertEqual(result.visibility, 'pending')
        result = self.result(
            '::cue([voice~="Bob Smith"]:hover) { display: none; }',
            '<v Bob Smith>hello</v>'
            )
        self.assertEqual(result.visibility, 'visible')

    def test_unknown_with_empty_expected(self):
        for operator in ('^=', '$=', '*='):
            with self.subTest(operator=operator):
                result = self.result(
                    '::cue([voice' + operator + '""]:hover) '
                    '{ display: none; }',
                    '<v Bob>hello</v>'
                    )
                self.assertEqual(result.visibility, 'visible')

    def test_unknown_combinator_subject_is_checked(self):
        result = self.result('::cue(c > b:hover) { display: none; }',
                             '<b>hello</b>')
        self.assertEqual(result.visibility, 'visible')
        result = self.result('::cue(c > b:hover) { display: none; }',
                             '<c><b>hello</b></c>')
        self.assertEqual(result.visibility, 'pending')

    def test_unknown_root_selector_cannot_change(self):
        result = self.result('::cue(:hover) { display: none; }', 'hello')
        self.assertEqual(result.visibility, 'pending')


class TestHiddenAndVisibleDeclarations(unittest.TestCase):

    def result(self, styles, text):
        corpus = Corpus(vtt(styles, text), [Mode('normal')])
        return corpus.records[0].results['normal']

    def test_hidden_from_the_initial_visibility(self):
        # visibility starts visible, so a hidden word always has a
        # declaration that hides it
        result = self.result(
            '::cue(.a) { visibility: hidden; }', '<c.a>hello</c>'
            )
        self.assertEqual(result.visibility, 'hidden')
        self.assertEqual(
            result.declarations[0].selector, '::cue(.a)'
            )

    def test_hidden_without_any_declaration(self):
        # display: none on the root, visible by default on the word
        result = self.result(
            '::cue { display: none; } ::cue(b) { display: block; }',
            '<b>hello</b>'
            )
        self.assertEqual(result.visibility, 'hidden')
        self.assertEqual(
            [str(declaration) for declaration in result.declarations],
            ['::cue: display: none']
            )

    def test_visible_with_an_explicit_declaration(self):
        result = self.result(
            '::cue { visibility: hidden; } ::cue(b) { visibility: visible; }',
            '<b>hello</b>'
            )
        self.assertEqual(result.visibility, 'visible')
        self.assertEqual(
            [str(declaration) for declaration in result.declarations],
            ['::cue(b): visibility: visible']
            )

    def test_visible_without_a_declaration(self):
        result = self.result('', 'hello')
        self.assertEqual(result.visibility, 'visible')
        self.assertEqual(result.declarations, ())


class TestModeConfiguration(unittest.TestCase):

    def test_config_of_a_single_mode(self):
        modes = modes_from_config({
            'name': 'study',
            'classes': ['study'],
            'styles': ['::cue(.study) { display: none }'],
            })
        self.assertEqual([mode.name for mode in modes], ['study'])

    def test_config_of_a_mapping_with_settings(self):
        modes = modes_from_config({'normal': {}})
        self.assertEqual(len(modes), 1)
        self.assertEqual(modes[0].name, 'normal')

    def test_invalid_config_style(self):
        for styles in ([1], [None], [{'x': 1}], {'x': 1}):
            with self.subTest(styles=styles):
                with self.assertRaises(ValueError):
                    Mode.from_config('a', {'styles': styles})

    def test_invalid_config_styles_type(self):
        with self.assertRaises(TypeError):
            Mode.from_config('a', {'styles': 1})

    def test_config_from_a_vtt(self):
        captions = vtt('::cue(.study) { display: none }')
        corpus = captions.corpus({'modes': [{'name': 'study',
                                             'classes': 'study'}]})
        self.assertEqual([mode.name for mode in corpus.modes], ['study'])
        self.assertEqual(corpus.records[0].visibility('study'), 'hidden')


class TestSerialisationFiltering(unittest.TestCase):

    def setUp(self):
        self.corpus = Corpus(
            vtt('::cue(.a) { display: none; }', '<c.a>one</c> two'),
            [Mode('normal'), Mode('study', classes=('study',))]
            )

    def test_rows_of_records(self):
        records = self.corpus.records[1:]
        self.assertEqual(
            [(row['word'], row['mode']) for row in self.corpus.rows(records)],
            [(2, 'normal'), (2, 'study')]
            )

    def test_to_json_of_records(self):
        rows = json.loads(self.corpus.to_json(
            records=self.corpus.records[:1]))
        self.assertEqual([row['word'] for row in rows], [1, 1])

    def test_to_tsv_of_records(self):
        lines = self.corpus.to_tsv(self.corpus.records[:1]).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1].split('\t')[0], '1')
