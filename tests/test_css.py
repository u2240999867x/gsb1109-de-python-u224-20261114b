"""Tests of the CSS selectors and declarations of cue styles."""

import unittest

from webvtt import css


class TestParseDeclarations(unittest.TestCase):

    def test_names_are_lowered_and_values_kept(self):
        self.assertEqual(
            css.parse_declarations(' COLOR : Red ; Display : NONE '),
            (css.Declaration('color', 'Red', False),
             css.Declaration('display', 'NONE', False))
            )

    def test_important(self):
        for text in ('display: none !important', 'display: none!important',
                     'display: none ! IMPORTANT'):
            with self.subTest(text=text):
                declaration = css.parse_declarations(text)[0]
                self.assertTrue(declaration.important)
                self.assertEqual(declaration.value, 'none')

    def test_important_value_is_trimmed(self):
        self.assertEqual(
            css.parse_declarations('display: none  !important')[0],
            css.Declaration('display', 'none', True)
            )

    def test_empty_and_nameless(self):
        self.assertEqual(
            css.parse_declarations('; : red; color:; color: blue;;'),
            (css.Declaration('color', 'blue', False),)
            )

    def test_value_with_colon(self):
        self.assertEqual(
            css.parse_declarations('background: url(a:b)')[0].value,
            'url(a:b)'
            )

    def test_no_declarations(self):
        self.assertEqual(css.parse_declarations(''), ())


class TestStripComments(unittest.TestCase):

    def test_comments(self):
        self.assertEqual(css.strip_comments('a /* b */ c'), 'a  c')
        self.assertEqual(css.strip_comments('a /* b'), 'a ')


class TestSplitSelectors(unittest.TestCase):

    def test_commas(self):
        self.assertEqual(
            css.split_selectors('::cue(.a), ::cue(b),'),
            ['::cue(.a)', '::cue(b)']
            )

    def test_escaped_comma(self):
        self.assertEqual(
            css.split_selectors(r'::cue(.a\,b), ::cue(c)'),
            [r'::cue(.a\,b)', '::cue(c)']
            )

    def test_empty(self):
        self.assertEqual(css.split_selectors(' , , '), [])

    def test_linear_on_commas(self):
        # a regex for the selectors rescans the rest of the text from each
        # comma, which is quadratic
        text = ',' * 200_000
        self.assertEqual(css.split_selectors(text), [])


class TestParseSelector(unittest.TestCase):

    def test_cue_root(self):
        selector = css.parse_selector('::cue')
        self.assertTrue(selector.is_root())
        self.assertTrue(selector.supported)
        self.assertFalse(selector.matches_nothing)
        self.assertEqual(selector.specificity, (0, 0, 0))

    def test_type(self):
        selector = css.parse_selector('::cue(B)')
        self.assertEqual(selector.compounds[0].tag, 'b')
        self.assertEqual(selector.specificity, (0, 0, 1))

    def test_classes(self):
        selector = css.parse_selector('::cue(c.loud.red)')
        self.assertEqual(selector.compounds[0].tag, 'c')
        self.assertEqual(selector.compounds[0].classes,
                         frozenset({'loud', 'red'}))
        self.assertEqual(selector.specificity, (0, 2, 1))

    def test_class_escapes(self):
        selector = css.parse_selector(r'::cue(.c\+1)')
        self.assertEqual(selector.compounds[0].classes, frozenset({'c+1'}))

    def test_universal_is_global(self):
        selector = css.parse_selector('::cue(*)')
        self.assertTrue(selector.supported)
        self.assertEqual(selector.specificity, (0, 0, 0))

    def test_voice_attribute(self):
        selector = css.parse_selector('::cue(c[voice="Esme"])')
        attribute = selector.compounds[0].attributes[0]
        self.assertEqual(attribute, css.Attribute('voice', '=', 'Esme', False))
        self.assertEqual(selector.specificity, (0, 1, 1))

    def test_attribute_operators(self):
        for text, operator, value in (
                ('[lang=de]', '=', 'de'),
                ('[lang|=de]', '|=', 'de'),
                ('[lang^=de]', '^=', 'de'),
                ('[lang$=de]', '$=', 'de'),
                ('[lang*=de]', '*=', 'de'),
                ('[lang~=de]', '~=', 'de'),
                ('[lang="de" i]', '=', 'de')):
            with self.subTest(text=text):
                selector = css.parse_selector('::cue(' + text + ')')
                attribute = selector.compounds[0].attributes[0]
                self.assertEqual(attribute.operator, operator)
                self.assertEqual(attribute.value, value)
                self.assertEqual(attribute.insensitive, text.endswith(' i]'))

    def test_lang_pseudo_class(self):
        selector = css.parse_selector('::cue(:lang(de-CH))')
        self.assertEqual(selector.compounds[0].languages, ('de-ch',))
        self.assertEqual(selector.specificity, (0, 1, 0))

    def test_universal_lang_pseudo_class(self):
        selector = css.parse_selector('::cue(c:lang(en))')
        self.assertTrue(selector.supported)

    def test_pseudo_class_is_not_supported(self):
        selector = css.parse_selector('::cue(.a:hover)')
        self.assertFalse(selector.supported)
        self.assertFalse(selector.matches_nothing)
        self.assertEqual(selector.specificity, (0, 2, 0))

    def test_id_matches_nothing(self):
        selector = css.parse_selector('::cue(#id)')
        self.assertTrue(selector.matches_nothing)

    def test_other_attribute_matches_nothing(self):
        selector = css.parse_selector('::cue([foo=bar])')
        self.assertTrue(selector.matches_nothing)

    def test_pseudo_element_matches_nothing(self):
        for text in ('::cue(.a::before)', '::cue(.a::cue-region)'):
            with self.subTest(text=text):
                self.assertTrue(css.parse_selector(text).matches_nothing)

    def test_page_selector_matches_nothing(self):
        for text in ('::cue-region', '.a', 'html::cue'):
            with self.subTest(text=text):
                selector = css.parse_selector(text)
                self.assertFalse(selector.supported)
                self.assertTrue(selector.matches_nothing)

    def test_empty_selector_matches_nothing(self):
        selector = css.parse_selector('::cue()')
        self.assertTrue(selector.matches_nothing)

    def test_combinators(self):
        selector = css.parse_selector('::cue(c.loud > b)')
        self.assertFalse(selector.supported)
        self.assertFalse(selector.matches_nothing)
        self.assertEqual([c.tag for c in selector.compounds], ['c', 'b'])
        self.assertEqual(selector.combinators, ('>',))

    def test_descendant_combinator(self):
        selector = css.parse_selector('::cue(.a .b)')
        self.assertEqual(selector.combinators, (' ',))

    def test_sibling_combinators(self):
        for combinator in ('+', '~'):
            with self.subTest(combinator=combinator):
                selector = css.parse_selector(f'::cue(.a {combinator} .b)')
                self.assertEqual(selector.combinators, (combinator,))

    def test_unreadable(self):
        for text in (r'::cue(.a \)', '::cue(.a[voice)', '::cue(.a >)',
                     '::cue(> .a)', '::cue(.a..b)'):
            with self.subTest(text=text):
                selector = css.parse_selector(text)
                self.assertTrue(selector.unreadable)
                self.assertFalse(selector.matches_nothing)

    def test_selectors_not_matching_fail_in_linear_time(self):
        # each character matched one way, or this backtracks for hours
        for text in ('::cue(.' + 'é' * 200 + ' x)',
                     '::cue(.' + '\\11' * 200 + '!)',
                     '::cue(.' + 'a' * 200 + '\\'):
            with self.subTest(text=text[:12]):
                selector = css.parse_selector(text)
                self.assertFalse(selector.supported)


class TestParseStyles(unittest.TestCase):

    def test_rules(self):
        rules = css.parse_styles(
            '::cue(.a) { color: red; display: none } '
            '::cue(b), ::cue(i) { visibility: hidden; }'
            )
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0].order, 1)
        self.assertEqual(rules[0].declarations,
                         (css.Declaration('color', 'red', False),
                          css.Declaration('display', 'none', False)))
        self.assertEqual(
            [selector.raw for selector in rules[1].selectors],
            ['::cue(b)', '::cue(i)']
            )

    def test_comments_and_empty_rules(self):
        self.assertEqual(
            css.parse_styles('/* ::cue(.a) { display: none } */'
                             '::cue(.b) { } ::cue(.c) { color: red }'),
            [css.Rule((css.parse_selector('::cue(.c)'),),
                      (css.Declaration('color', 'red', False),), 1)]
            )

    def test_no_styles(self):
        self.assertEqual(css.parse_styles(''), [])

    def test_linear_on_braces(self):
        # a regex for the braces is quadratic on text without braces
        self.assertEqual(css.parse_styles('{' * 200_000), [])


class _Element:
    """An element to match selectors against."""

    def __init__(self, tag=None, classes=(), voice=None, language=None,
                 parent=None, previous=()):
        self.tag = tag
        self.classes = frozenset(classes)
        self.voice = voice
        self.language = language
        self.parent = parent
        self.previous = list(previous)


class TestMatches(unittest.TestCase):

    def match(self, selector, element):
        return css.matches(css.parse_selector(selector), element)

    def test_root(self):
        root = _Element()
        self.assertTrue(self.match('::cue', root))
        self.assertFalse(self.match('::cue', _Element('b', parent=root)))
        self.assertTrue(self.match('::cue(*)', _Element('b', parent=root)))

    def test_type_class(self):
        element = _Element('c', ['loud', 'red'])
        self.assertTrue(self.match('::cue(c.loud)', element))
        self.assertFalse(self.match('::cue(c.loud.blue)', element))
        self.assertFalse(self.match('::cue(b)', element))

    def test_voice_and_language(self):
        element = _Element('v', voice='Bob', language='de-CH')
        self.assertTrue(self.match('::cue([voice="Bob"])', element))
        self.assertFalse(self.match('::cue([voice="Alice"])', element))
        self.assertTrue(self.match('::cue([voice])', element))
        self.assertTrue(self.match('::cue(:lang(de))', element))
        self.assertFalse(self.match('::cue(:lang(en))', element))
        self.assertTrue(self.match('::cue([lang|=de])', element))

    def test_attribute_operators(self):
        element = _Element('v', voice='Bob Smith')
        for text, expected in (
                ('[voice="Bob Smith"]', True),
                ('[voice="bob smith" i]', True),
                ('[voice^="Bob"]', True),
                ('[voice$="Smith"]', True),
                ('[voice*="ob Sm"]', True),
                ('[voice~="Smith"]', True),
                ('[voice~="Smi"]', False),
                ('[voice="Bob"]', False)):
            with self.subTest(text=text):
                self.assertEqual(
                    self.match('::cue(' + text + ')', element), expected
                    )

    def test_ancestors(self):
        root = _Element()
        outer = _Element('c', ['outer'], parent=root)
        middle = _Element('i', [], parent=outer)
        deep = _Element('b', ['deep'], parent=middle)
        self.assertTrue(self.match('::cue(.outer .deep)', deep))
        self.assertFalse(self.match('::cue(c > .deep)', deep))
        self.assertTrue(self.match('::cue(c .deep)', deep))
        self.assertFalse(self.match('::cue(.deep > c)', deep))
        self.assertTrue(self.match('::cue(c b)', deep))
        self.assertFalse(self.match('::cue(b > c)', deep))
        # the type of the immediate parent, not of an ancestor
        self.assertTrue(self.match('::cue(c > i)', middle))

    def test_siblings(self):
        root = _Element()
        first = _Element('b', ['first'], parent=root)
        second = _Element('i', ['second'], parent=root, previous=[first])
        third = _Element('u', ['third'], parent=root,
                         previous=[first, second])
        self.assertTrue(self.match('::cue(.first + .second)', second))
        self.assertFalse(self.match('::cue(.first + .third)', third))
        self.assertTrue(self.match('::cue(.first ~ .third)', third))
        self.assertFalse(self.match('::cue(.second + .first)', first))

    def test_unknown_pseudo_class(self):
        self.assertIsNone(self.match('::cue(.a:hover)', _Element('c', ['a'])))

    def test_matches_nothing(self):
        self.assertFalse(self.match('::cue(#id)', _Element('c')))
        self.assertFalse(self.match('.a', _Element('c', ['a'])))

    def test_unreadable(self):
        self.assertIsNone(self.match(r'::cue(.a \)', _Element('c')))


class TestEffectiveVisibility(unittest.TestCase):

    def test_display(self):
        self.assertEqual(css.effective_visibility('display', 'none'), 'none')
        self.assertEqual(css.effective_visibility('display', 'block'), 'shown')
        self.assertEqual(css.effective_visibility('display', 'NONE'), 'none')

    def test_visibility(self):
        self.assertEqual(
            css.effective_visibility('visibility', 'hidden'), 'hidden'
            )
        self.assertEqual(
            css.effective_visibility('visibility', 'collapse'), 'hidden'
            )
        self.assertEqual(
            css.effective_visibility('visibility', 'visible'), 'visible'
            )

    def test_other_values_and_properties(self):
        for name, value in (
                ('visibility', 'inherit'), ('visibility', 'unset'),
                ('color', 'red'), ('display', 'inherit')):
            with self.subTest(name=name, value=value):
                self.assertIsNone(css.effective_visibility(name, value))


class TestMalformedSelectors(unittest.TestCase):
    """Malformed selectors are unreadable and match nothing in a corpus."""

    CASES = (
        r'::cue(\)', '::cue(.)', '::cue(..)', '::cue(.a#)', '::cue(#)',
        '::cue([)', '::cue([voice)', '::cue([=x])', '::cue([])',
        '::cue(:)', '::cue(::)', '::cue(.a:)', '::cue(.a::)',
        '::cue(c!)', '::cue(.a >)', '::cue(> .a)', '::cue(.a > > .b)',
        '::cue(.a[voice=])', '::cue(.a[voice="x" z])',
        '::cue(.a[voice=\\)])', '::cue((a))', '::cue(.a))',
        )

    def test_unreadable(self):
        for text in self.CASES:
            with self.subTest(text=text):
                selector = css.parse_selector(text)
                self.assertTrue(selector.unreadable)
                self.assertFalse(selector.matches_nothing)
                self.assertIsNone(css.matches(selector, _Element('c')))

    def test_escapes_and_ids(self):
        self.assertTrue(css.parse_selector(r'::cue(\))').unreadable)
        self.assertTrue(css.parse_selector('::cue(#)').unreadable)
        self.assertTrue(css.parse_selector('::cue(#a#)').unreadable)

    def test_skip_function_with_unclosed_parenthesis(self):
        selector = css.parse_selector('::cue(.a:not(x)')
        # the argument is not closed, so the selector cannot be read
        self.assertTrue(selector.unreadable)

    def test_skip_function_with_argument(self):
        selector = css.parse_selector('::cue(.a:not(.b))')
        self.assertTrue(selector.supported is False)
        self.assertTrue(selector.unreadable is False)


class TestElementMatchingEdgeCases(unittest.TestCase):

    def test_compound_with_pseudo_element_never_matches(self):
        selector = css.parse_selector('::cue(.a::before)')
        self.assertTrue(selector.matches_nothing)

    def test_attribute_without_value_on_missing_attribute(self):
        order = _Element('i')
        self.assertFalse(
            css.matches(css.parse_selector('::cue([voice])'), order)
            )

    def test_language_attribute_is_case_insensitive(self):
        element = _Element('v', language='DE-ch')
        self.assertTrue(
            css.matches(css.parse_selector('::cue([lang="de-CH"])'), element)
            )

    def test_prefix_and_suffix_never_match_empty(self):
        element = _Element('v', voice='Bob')
        for text in ('[voice^=""]', '[voice$=""]', '[voice*=""]'):
            with self.subTest(text=text):
                self.assertFalse(
                    css.matches(css.parse_selector('::cue(' + text + ')'),
                                element)
                    )

    def test_extended_visibility_values(self):
        self.assertIsNone(css.effective_visibility('visibility', 'collapse!'))
        self.assertIsNone(css.effective_visibility('visibility', 'revert'))

    def test_display_visibility_other_values(self):
        self.assertEqual(
            css.effective_visibility('display', 'inline-block'), 'shown'
            )
        self.assertIsNone(css.effective_visibility('opacity', '0'))


class TestSplitterBranches(unittest.TestCase):
    """Branches of the complex selector splitter."""

    def split(self, argument):
        return css._split_complex(argument)

    def test_stray_bracket(self):
        self.assertIsNone(self.split('.a]'))
        self.assertIsNone(self.split('.a['))
        self.assertIsNone(self.split('(.a))'))
        self.assertIsNone(self.split('.a('))

    def test_trailing_combinator(self):
        self.assertIsNone(self.split('.a >'))
        self.assertIsNone(self.split('.a +'))
        self.assertIsNone(self.split('.a ~'))
        self.assertIsNone(self.split('> .a'))
        self.assertIsNone(self.split('.a > > .b'))
        self.assertIsNone(self.split('.a > > .b'))
        self.assertEqual(self.split('.a . > .b'),
                         (['.a', '.', '.b'], [' ', '>']))

    def test_adjacent_compounds_without_separator(self):
        # .a.b is one compound, not two, and is split as one
        self.assertEqual(self.split('.a.b'), (['.a.b'], []))

    def test_trailing_space_is_fine(self):
        self.assertEqual(self.split('.a '), (['.a'], []))
        self.assertEqual(self.split(' .a'), (['.a'], []))

    def test_string_with_a_space_in_brackets(self):
        self.assertEqual(
            self.split('.a[voice="b c"] > i'),
            (['.a[voice="b c"]', 'i'], ['>'])
            )

    def test_backslash_at_the_end(self):
        self.assertIsNone(self.split('.a\\'))

    def test_next_compound_without_a_separator(self):
        self.assertIsNone(self.split('\\. \\'))

    def test_empty_and_whitespace(self):
        self.assertIsNone(self.split(''))
        self.assertIsNone(self.split('  '))


class TestCompoundBranches(unittest.TestCase):

    def test_empty_compound(self):
        self.assertIsNone(css._parse_compound(''))

    def test_unclosed_bracket(self):
        self.assertIsNone(css._parse_compound('[voice'))

    def test_unknown_attribute(self):
        compound = css._parse_compound('[foo=bar]')
        self.assertTrue(compound.impossible)

    def test_unknown_character(self):
        self.assertIsNone(css._parse_compound('.a!'))

    def test_class_escape(self):
        self.assertEqual(
            css._parse_compound(r'.c\+\32').classes, frozenset({'c+2'})
            )

    def test_unclosed_attribute_bracket(self):
        self.assertIsNone(css._closing_bracket('[voice="x"', 0))

    def test_quoted_bracket(self):
        self.assertEqual(css._closing_bracket('[voice="]"]', 0), 10)
        self.assertEqual(css._closing_bracket("[voice=']']", 0), 10)

    def test_attribute_without_operator(self):
        self.assertEqual(
            css._parse_attribute('voice'),
            css.Attribute('voice', None, None, False)
            )

    def test_attribute_with_escaped_quotes(self):
        self.assertEqual(
            css._parse_attribute(r'voice="a\"b"').value, 'a"b'
            )

    def test_unreadable_attribute(self):
        for content in ('', '=x', '"x"', 'voice "x"', 'voice=x y'):
            with self.subTest(content=content):
                self.assertIsNone(css._parse_attribute(content))

    def test_pseudo_element(self):
        parsed = css._parse_pseudo('::before', 0)
        self.assertEqual(parsed, (True, None, 8))

    def test_pseudo_class(self):
        parsed = css._parse_pseudo(':hover', 0)
        self.assertEqual(parsed, (False, None, 6))

    def test_lang_pseudo_class(self):
        parsed = css._parse_pseudo(':lang(de)', 0)
        self.assertEqual(parsed, (False, 'de', 9))

    def test_unreadable_pseudo(self):
        self.assertIsNone(css._parse_pseudo(':', 0))
        self.assertIsNone(css._parse_pseudo('::', 0))

    def test_skip_function_with_unclosed_argument(self):
        self.assertEqual(css._skip_function(':not(', 4), 4)
        self.assertEqual(css._skip_function(':not(x', 4), 4)
        self.assertEqual(css._skip_function(':not(x)', 4), 7)
        self.assertEqual(css._skip_function(':not(x(y))', 4), 10)
        self.assertEqual(css._skip_function(':notx', 4), 4)


class TestMatchingBranches(unittest.TestCase):

    def test_any_none(self):
        self.assertIsNone(css._any([None]))

    def test_and_none(self):
        self.assertIsNone(css._and(None, True))
        self.assertFalse(css._and(False, None))

    def test_compound_with_id(self):
        compound = css._parse_compound('#a')
        self.assertFalse(css._match_compound(compound, _Element('c')))

    def test_compound_with_pseudo_element(self):
        compound = css._parse_compound('c::before')
        self.assertFalse(css._match_compound(compound, _Element('c')))

    def test_compound_with_unknown_attribute(self):
        compound = css._parse_compound('[foo=bar]')
        self.assertFalse(css._match_compound(compound, _Element('c')))

    def test_attribute_without_value_on_a_missing_attribute(self):
        compound = css._parse_compound('[voice]')
        self.assertFalse(css._match_compound(compound, _Element('c')))

    def test_any_with_a_false_and_unknown(self):
        element = _Element('c', ['a'], parent=_Element('b', ['x']),
                           previous=[_Element('i', ['y'])])
        self.assertIsNone(
            css.matches(css.parse_selector('::cue(.x .a:hover)'), element)
            )

    def test_sibling_selector_with_unknown(self):
        root = _Element('b')
        first = _Element('i', ['a'], parent=root)
        second = _Element('u', ['b'], parent=root, previous=[first])
        self.assertIsNone(
            css.matches(css.parse_selector('::cue(.a:not(x) + .b)'), second)
            )

    def test_empty_expected_never_matches(self):
        element = _Element('v', voice='Bob')
        for operator in ('^=', '$=', '*='):
            with self.subTest(operator=operator):
                self.assertFalse(
                    css.matches(
                        css.parse_selector(
                            '::cue([voice' + operator + '""])'
                            ),
                        element
                        )
                    )


class TestMoreCoverage(unittest.TestCase):

    def test_invalid_code_points(self):
        for text in (r'\0 a', r'\d800 b', r'\110000 c'):
            with self.subTest(text=text):
                self.assertIn('�', css.unescape(text))

    def test_rule_without_selectors(self):
        self.assertEqual(css.parse_styles('{ color: red; }'), [])
        self.assertEqual(css.parse_styles(' , { color: red; }'), [])

    def test_two_escapes_without_a_separator(self):
        # escapes are characters of the same compound
        self.assertEqual(
            css._split_complex(r'\.\\'), ([r'\.\\'], [])
            )

    def test_combinator_after_a_combinator(self):
        self.assertIsNone(css._split_complex('.a > + .b'))
        self.assertIsNone(css._split_complex('.a > ~ .b'))

    def test_combinator_with_a_trailing_space(self):
        self.assertIsNone(css._split_complex('.a > '))

    def test_escape_after_a_compound_without_a_separator(self):
        self.assertEqual(
            css._split_complex(r'.a\\'), ([r'.a\\'], [])
            )

    def test_escape_at_the_start_of_a_compound_after_a_space(self):
        self.assertEqual(
            css._split_complex(r'.a \.b'), (['.a', r'\.b'], [' '])
            )
