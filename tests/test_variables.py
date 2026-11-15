from tinycss2 import parse_one_declaration
from tinycss2.variables import (
    CSS_WIDE_KEYWORDS,
    Declaration,
    FallbackUsage,
    Resolution,
    Resolver,
    cascade_custom_properties,
    resolve_declaration,
    resolve_resource,
    resolve_variables,
)


def test_simple_substitution():
    resolution = resolve_variables(
        'var(--brand)', {'--brand': 'url(brand.png)'},
        base_url='https://shop.test/css/product.css')
    assert not resolution.invalid
    assert resolution.text == 'url(brand.png)'
    assert resolution.url == 'https://shop.test/css/brand.png'
    assert resolution.urls == ('https://shop.test/css/brand.png',)
    assert resolution.used_variables == ('--brand',)
    assert resolution.used_fallbacks == ()


def test_fallback_used_only_when_needed():
    resolution = resolve_variables(
        'var(--missing, url(fallback.png))',
        base_url='https://shop.test/css/product.css')
    assert not resolution.invalid
    assert resolution.text == 'url(fallback.png)'
    assert resolution.url == 'https://shop.test/css/fallback.png'
    assert resolution.used_variables == ('--missing',)
    (fallback,) = resolution.used_fallbacks
    assert fallback.variable == '--missing'
    assert fallback.text == 'url(fallback.png)'
    assert fallback.resolved_text == 'url(fallback.png)'


def test_unused_fallback_url_is_not_collected():
    resolution = resolve_variables(
        'var(--brand, url(unavailable.png))',
        {'--brand': 'url(brand.png)'},
        base_url='https://shop.test/css/product.css')
    assert resolution.text == 'url(brand.png)'
    assert resolution.urls == ('https://shop.test/css/brand.png',)
    assert resolution.used_fallbacks == ()


def test_losing_declaration_url_is_not_collected():
    declarations = [
        Declaration(
            'background-image', 'url(grey-brand.png)',
            'https://shop.test/css/grey.css'),
        Declaration(
            'background-image', 'var(--brand)',
            'https://shop.test/css/product.css'),
    ]
    resolution = resolve_resource(
        declarations,
        {'--brand': Declaration('--brand', 'url(brand.png)',
                                'https://shop.test/css/product.css')},
        name='background-image')
    assert resolution.used_variables == ('--brand',)
    assert resolution.url == 'https://shop.test/css/brand.png'
    assert resolution.urls == ('https://shop.test/css/brand.png',)
    assert resolution.text == 'url(brand.png)'


def test_fallback_url_uses_stylesheet_that_defines_it():
    custom_properties = [Declaration(
        '--hero', 'var(--brand, url(assets/hero.png))',
        'https://cdn.test/theme/theme.css')]
    resolution = resolve_variables(
        'var(--hero)', custom_properties,
        base_url='https://shop.test/product/page.css')
    assert resolution.text == 'url(assets/hero.png)'
    assert resolution.url == 'https://cdn.test/theme/assets/hero.png'
    (fallback,) = resolution.used_fallbacks
    assert fallback.variable == '--brand'
    assert fallback.base_url == 'https://cdn.test/theme/theme.css'


def test_variable_value_uses_its_own_stylesheet_base_url():
    custom_properties = [
        Declaration(
            '--brand', 'url(img/brand.svg)',
            'https://cdn.test/theme/tokens.css'),
        Declaration(
            '--hero', 'var(--brand)',
            'https://cdn.test/theme/hero.css'),
    ]
    resolution = resolve_variables(
        'var(--hero)', custom_properties,
        base_url='https://shop.test/product/page.css')
    assert resolution.text == 'url(img/brand.svg)'
    assert resolution.url == 'https://cdn.test/theme/img/brand.svg'
    assert resolution.used_variables == ('--hero', '--brand')


def test_self_reference_cycle_is_invalid():
    resolution = resolve_variables('var(--a)', {'--a': 'var(--a)'})
    assert resolution.invalid
    assert resolution.value is None
    assert resolution.text is None
    assert resolution.urls == ()
    assert 'cycle' in resolution.reason


def test_mutual_reference_cycle_is_invalid():
    resolution = resolve_variables(
        'var(--a)', {'--a': 'var(--b)', '--b': 'var(--a)'})
    assert resolution.invalid
    assert resolution.value is None
    assert resolution.used_variables == ('--a',)


def test_cycle_through_unused_fallback_is_invalid():
    resolution = resolve_variables(
        'var(--a)', {'--a': 'var(--b, var(--a))', '--b': 'url(ok.png)'})
    assert resolution.invalid
    assert resolution.url is None


def test_fallback_can_break_a_cycle():
    resolution = resolve_variables(
        'var(--a, url(fallback.png))',
        {'--a': 'var(--b)', '--b': 'var(--a)'},
        base_url='https://shop.test/css/page.css')
    assert not resolution.invalid
    assert resolution.url == 'https://shop.test/css/fallback.png'


def test_missing_variable_without_fallback_is_invalid():
    resolution = resolve_variables('var(--missing)', base_url='https://x.test/')
    assert resolution.invalid
    assert resolution.value is None
    assert 'missing' in resolution.reason


def test_css_wide_keyword_custom_property_uses_fallback():
    resolution = resolve_variables(
        'var(--x, blue)', {'--x': 'inherit'}, base_url='https://x.test/')
    assert resolution.text == 'blue'
    assert resolution.used_variables == ('--x',)
    assert [fallback.text for fallback in resolution.used_fallbacks] == ['blue']


def test_empty_fallback_is_valid():
    resolution = resolve_variables('var(--x,)', {'--x': 'initial'})
    assert not resolution.invalid
    assert resolution.text == ''


def test_nested_var_in_function_and_block():
    resolution = resolve_variables(
        'calc(var(--gap) + 2px)',
        {'--gap': '4px'}, base_url='https://x.test/')
    assert resolution.text == 'calc(4px + 2px)'
    resolution = resolve_variables(
        'image-set(var(--image) 1x)', {'--image': '"img/a.png"'},
        base_url='https://x.test/css/')
    assert resolution.text == 'image-set("img/a.png" 1x)'
    assert resolution.url == 'https://x.test/css/img/a.png'


def test_rgba_arguments_are_substituted():
    resolution = resolve_variables(
        'rgb(var(--channels))', {'--channels': '0 0 0'},
        base_url='https://x.test/')
    assert resolution.text == 'rgb(0 0 0)'


def test_multiple_urls_are_all_collected():
    resolution = resolve_variables(
        'url(a.png), url(b.png)', base_url='https://x.test/css/'
    )
    assert resolution.urls == (
        'https://x.test/css/a.png', 'https://x.test/css/b.png')


def test_cascade_uses_source_order_and_importance():
    declarations = [
        Declaration('--brand', 'url(first.png)', 'https://x.test/a.css'),
        Declaration('--brand', 'url(second.png)', 'https://x.test/b.css'),
        Declaration(
            '--brand', 'url(important.png)', 'https://x.test/c.css', True),
    ]
    winners = cascade_custom_properties(declarations)
    assert winners['--brand'].base_url == 'https://x.test/c.css'
    assert winners['--brand'].important


def test_explicit_priority_wins_over_source_order():
    declarations = [
        Declaration('background', 'url(a.png)', priority=1),
        Declaration('background', 'url(b.png)', priority=5),
        Declaration('background', 'url(c.png)', priority=3),
    ]
    resolution = resolve_resource(declarations, name='background')
    assert resolution.text == 'url(b.png)'


def test_resolve_property_without_name():
    resolution = resolve_resource([
        'background: url(a.png)',
        'background: var(--brand)',
    ], {'--brand': 'url(brand.png)'}, base_url='https://x.test/css/')
    assert resolution.url == 'https://x.test/css/brand.png'


def test_no_matching_declaration():
    resolution = resolve_resource(
        [Declaration('color', 'red')], name='background-image')
    assert not resolution.invalid
    assert resolution.value is None
    assert resolution.url is None


def test_ast_declaration_input():
    declarations = [parse_one_declaration('background-image: var(--brand)')]
    resolution = resolve_resource(
        declarations, {'--brand': 'url(brand.png)'},
        base_url='https://x.test/css/')
    assert resolution.text == 'url(brand.png)'
    assert resolution.declaration.name == 'background-image'


def test_resolver_reuses_custom_properties():
    resolver = Resolver({'--brand': 'url(brand.png)'}, 'https://x.test/css/')
    first = resolver.resolve_value('var(--brand)')
    second = resolver.resolve_value('var(--brand)')
    assert first.url == 'https://x.test/css/brand.png'
    assert second.url == 'https://x.test/css/brand.png'


def test_declaration_and_resolution_types():
    resolution = resolve_declaration(
        Declaration('background-image', 'url(a.png)'))
    assert isinstance(resolution, Resolution)
    assert isinstance(resolution.declaration, Declaration)
    fallback = FallbackUsage('--x', (), None)
    assert fallback.variable == '--x'
    assert 'inherit' in CSS_WIDE_KEYWORDS
