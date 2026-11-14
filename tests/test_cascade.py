from tinycss2 import parse_stylesheet
from tinycss2.cascade import (
    AnonymousLayer,
    Cascade,
    CascadeEntry,
    CascadeOrigin,
    cascade,
)
from tinycss2.serializer import serialize

UA = CascadeOrigin.USER_AGENT
USER = CascadeOrigin.USER
AUTHOR = CascadeOrigin.AUTHOR


def matched(rule, selector):
    # Every rule in these fixtures matches the price element.
    return specificity(rule)


def specificity(rule):
    # Minimal selector specificity: #id -> a, .class/[attr]/:pseudo -> b,
    # type idents -> c. Enough for the simple selectors in these fixtures.
    a = b = c = 0
    after_colon = False
    for token in rule.prelude:
        if token.type == 'hash':
            a += 1
            after_colon = False
        elif token.type == 'literal':
            if token.value in ('.', '['):
                b += 1
            after_colon = token.value == ':'
        elif token.type == 'ident':
            if after_colon:
                b += 1
            else:
                c += 1
            after_colon = False
    return (a, b, c)


def collect(css, origin=AUTHOR, specificity=matched, property='color'):
    cascade_set = Cascade()
    cascade_set.add_rules(
        parse_stylesheet(css, skip_comments=True, skip_whitespace=True),
        origin, specificity, property=property)
    return cascade_set


def test_specificity_wins_inside_one_layer():
    css = '''
      .price { color: red }
      div .price { color: blue }
    '''
    result = collect(css)
    assert result.winning_value() == 'blue'

    css = '''
      div .price { color: blue }
      .price { color: red }
    '''
    result = collect(css)
    assert result.winning_value() == 'blue'


def test_source_order_breaks_ties():
    css = '''
      .price { color: red }
      .price { color: green }
    '''
    result = collect(css)
    winner = result.winner()
    assert winner.value == 'green'
    assert winner.order > result.entries()[0].order


def test_layer_order_follows_first_mention_not_rule_position():
    # The @layer statement orders "reset" before "theme" even though the
    # theme rule appears first in the source.
    css = '''
      @layer reset, theme;
      @layer theme { .price { color: green } }
      @layer reset { .price { color: red } }
    '''
    result = collect(css)
    assert result.layers == ('reset', 'theme')
    assert result.winning_value() == 'green'


def test_moving_unrelated_rules_inside_a_layer_keeps_winning_layer():
    def build(reset_order):
        rules = []
        if reset_order == 'width-first':
            rules.append(
                '@layer reset { .price { width: 10px } '
                '.price { color: red } }')
        else:
            rules.append(
                '@layer reset { .price { color: red } '
                '.price { width: 10px } }')
        rules.append('@layer theme { .price { color: green } }')
        return collect('\n'.join(rules))

    first = build('width-first').winner()
    second = build('color-first').winner()
    assert first.layer == second.layer == 'theme'
    assert first.value == second.value == 'green'


def test_unlayered_normal_rules_beat_layered_ones():
    css = '''
      @layer theme { .price { color: green } }
      .price { color: red }
    '''
    result = collect(css)
    assert result.winner().layer is None
    assert result.winning_value() == 'red'


def test_important_declarations_beat_more_specific_normal_ones():
    css = '''
      body div .price { color: green }
      .price { color: red !important }
    '''
    result = collect(css)
    winner = result.winner()
    assert winner.value == 'red'
    assert winner.important is True


def test_important_reverses_layer_order():
    css = '''
      @layer a, b;
      @layer b { .price { color: green !important } }
      @layer a { .price { color: red !important } }
    '''
    result = collect(css)
    winner = result.winner()
    assert winner.value == 'red'
    assert winner.layer == 'a'
    # Without importance, the later layer "b" would have won.
    normal = collect(css.replace('!important', ''))
    assert normal.winning_value() == 'green'


def test_layered_important_beats_unlayered_important():
    css = '''
      @layer a { .price { color: green !important } }
      .price { color: red !important }
    '''
    result = collect(css)
    winner = result.winner()
    assert winner.value == 'green'
    assert winner.layer == 'a'


def test_anonymous_layers_are_distinct_and_ordered_by_appearance():
    css = '''
      @layer { .price { color: red } }
      @layer { .price { color: green } }
    '''
    result = collect(css)
    entries = result.entries()
    layers = [entry.layer for entry in entries]
    assert all(isinstance(layer, AnonymousLayer) for layer in layers)
    assert layers[0] is not layers[1]
    assert result.winning_value() == 'green'

    important_css = '''
      @layer { .price { color: red !important } }
      @layer { .price { color: green !important } }
    '''
    important = collect(important_css)
    assert important.winner().important is True
    assert important.winning_value() == 'red'


def test_nested_layer_names_are_qualified():
    css = '''
      @layer outer {
        @layer inner { .price { color: red } }
      }
      @layer "outer";
    '''
    result = collect(css)
    assert 'outer.inner' in result.layers
    assert result.winner().layer == 'outer.inner'


def test_conditional_group_rules_inherit_the_current_layer():
    css = '''
      @layer theme {
        @media screen { .price { color: green } }
      }
      .price { color: red }
    '''
    result = collect(css)
    # Normal unlayered declarations still beat layered ones.
    assert result.winner().layer is None
    assert result.winning_value() == 'red'
    themed = result.entries()[0]
    assert themed.layer == 'theme'
    assert themed.value == 'green'

    important_css = css.replace('color: green', 'color: green !important')
    important = collect(important_css)
    # The layered important declaration inherits the layer of @media's
    # enclosing @layer and beats the unlayered normal one.
    assert important.winner().layer == 'theme'
    assert important.winning_value() == 'green'


def test_non_style_at_rules_are_skipped():
    css = '''
      @keyframes spin { to { color: red } }
      @font-face { font-family: x; color: red }
      .price { color: green }
    '''
    result = collect(css)
    assert len(result.entries()) == 1
    assert result.winning_value() == 'green'


def test_origin_order_for_normal_declarations():
    cascade_set = Cascade()
    cascade_set.add('red', origin=UA, property='color', order=0)
    cascade_set.add('green', origin=USER, property='color', order=1)
    cascade_set.add('blue', origin=AUTHOR, property='color', order=2)
    assert cascade_set.winning_value() == 'blue'


def test_origin_order_is_reversed_for_important_declarations():
    cascade_set = Cascade()
    cascade_set.add(
        'blue', origin=AUTHOR, important=True, property='color', order=0)
    cascade_set.add(
        'green', origin=USER, important=True, property='color', order=1)
    cascade_set.add(
        'red', origin=UA, important=True, property='color', order=2)
    winner = cascade_set.winner()
    assert winner.value == 'red'
    assert winner.origin is UA


def test_important_beats_normal_regardless_of_origin():
    cascade_set = Cascade()
    cascade_set.add('blue', origin=AUTHOR, property='color', order=0)
    cascade_set.add(
        'red', origin=UA, important=True, property='color', order=1)
    assert cascade_set.winning_value() == 'red'


def test_non_matching_rules_can_be_skipped_with_callable_specificity():
    css = '''
      .other { color: red }
      .price { color: green }
    '''

    def specificity(rule, selector_text):
        return None if selector_text == '.other' else (0, 1, 0)

    cascade_set = Cascade()
    cascade_set.add_rules(
        parse_stylesheet(css, skip_comments=True, skip_whitespace=True),
        AUTHOR, specificity)
    assert len(cascade_set.entries()) == 1
    assert cascade_set.winning_value() == 'green'


def test_winner_keeps_its_original_source_range():
    css = (
        '@layer a, theme;\n'
        '@layer a {\n'
        '  .price {\n'
        '    color: red;\n'
        '  }\n'
        '}\n'
        '@layer theme {\n'
        '  .price {\n'
        '    color: green;\n'
        '  }\n'
        '}\n')
    result = collect(css)
    winner = result.winner()
    assert winner.value == 'green'
    assert winner.property == 'color'
    assert winner.selector == '.price'
    assert winner.source_line == 9
    assert winner.declaration.lower_name == 'color'
    serialized = serialize(winner.declaration.value).strip()
    assert serialized == 'green'


def test_inline_style_is_just_an_unlayered_author_entry():
    cascade_set = Cascade()
    cascade_set.add(
        'red', origin=AUTHOR, specificity=(1, 0, 0),
        property='color', selector=None)
    cascade_set.add(
        'green', origin=AUTHOR, specificity=(0, 1, 0),
        property='color', selector='.price')
    assert cascade_set.winner().selector is None
    assert cascade_set.winning_value() == 'red'


def test_functional_cascade_api_with_layer_order():
    entries = [
        CascadeEntry(
            value='red', origin=AUTHOR, specificity=(0, 1, 0), order=0,
            layer='reset', property='color'),
        CascadeEntry(
            value='green', origin=AUTHOR, specificity=(0, 1, 0), order=1,
            layer='theme', property='color'),
    ]
    winner = cascade(entries, layer_order=('reset', 'theme'))
    assert winner.value == 'green'
    assert winner.layer == 'theme'


def test_property_filtering_and_empty_cascade():
    cascade_set = Cascade()
    cascade_set.add('red', origin=AUTHOR, property='color', order=0)
    cascade_set.add('10px', origin=AUTHOR, property='width', order=1)
    assert cascade_set.winner('width').value == '10px'
    assert cascade_set.winner('margin') is None
    assert cascade_set.winning_value('margin') is None
    assert Cascade().winner() is None


def test_layer_declared_after_values_keeps_first_mention_semantics():
    cascade_set = Cascade()
    cascade_set.add(
        'red', origin=AUTHOR, layer='late', property='color', order=0)
    cascade_set.declare_layers(('early',))
    cascade_set.add(
        'green', origin=AUTHOR, layer='early', property='color', order=1)
    # "late" was registered first, so it has the lower priority for normal
    # declarations and "early" wins.
    assert cascade_set.layers == ('late', 'early')
    assert cascade_set.winning_value() == 'green'


def test_fully_sorted_order_covers_all_cascade_criteria():
    css = '''
      @layer a, b;
      @layer a { .price { color: layer-a } }
      @layer b { .price { color: layer-b } }
      .price { color: unlayered }
    '''
    result = collect(css)
    assert [entry.value for entry in result.entries()] == [
        'layer-a', 'layer-b', 'unlayered']


def test_entries_are_not_mutated_by_resolution():
    cascade_set = collect('.price { color: red } .price { color: green }')
    first_pass = [entry.value for entry in cascade_set.entries()]
    second_pass = [entry.value for entry in cascade_set.entries()]
    assert first_pass == second_pass == ['red', 'green']
    assert cascade_set.winning_value() == 'green'
