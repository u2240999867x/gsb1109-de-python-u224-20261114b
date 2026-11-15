import functools
import json
import pprint
from pathlib import Path

import pytest
from webencodings import Encoding, lookup

from tinycss2 import (  # isort:skip
    ByteRange, MappingNote, SourceMap, locate_declaration_list_errors,
    locate_stylesheet_declaration_errors, parse_blocks_contents,
    parse_component_value_list, parse_declaration_list,
    parse_one_component_value, parse_one_declaration, parse_one_rule,
    parse_rule_list, parse_stylesheet, parse_stylesheet_bytes, serialize)
from tinycss2.ast import (  # isort:skip
    AtKeywordToken, AtRule, Comment, CurlyBracketsBlock, Declaration,
    DimensionToken, FunctionBlock, HashToken, IdentToken, LiteralToken,
    NumberToken, ParenthesesBlock, ParseError, PercentageToken, QualifiedRule,
    SquareBracketsBlock, StringToken, UnicodeRangeToken, URLToken,
    WhitespaceToken)
from tinycss2.color3 import RGBA  # isort:skip
from tinycss2.color3 import parse_color as parse_color3  # isort:skip
from tinycss2.color4 import Color  # isort:skip
from tinycss2.color4 import parse_color as parse_color4  # isort:skip
from tinycss2.color5 import parse_color as parse_color5  # isort:skip
from tinycss2.nth import parse_nth, serialize_nth  # isort:skip


def generic(func):
    implementations = func()

    @functools.wraps(func)
    def run(value):
        repr(value)  # Test that this does not raise.
        return implementations[type(value)](value)
    return run


@generic
def to_json():
    def numeric(t):
        return [
            t.representation, t.value,
            'integer' if t.int_value is not None else 'number']
    return {
        type(None): lambda _: None,
        str: lambda s: s,
        int: lambda s: s,
        list: lambda li: [to_json(el) for el in li],
        tuple: lambda li: [to_json(el) for el in li],
        Encoding: lambda e: e.name,
        ParseError: lambda e: ['error', e.kind],

        Comment: lambda t: '/* … */',
        WhitespaceToken: lambda t: ' ',
        LiteralToken: lambda t: t.value,
        IdentToken: lambda t: ['ident', t.value],
        AtKeywordToken: lambda t: ['at-keyword', t.value],
        HashToken: lambda t: [
            'hash', t.value, 'id' if t.is_identifier else 'unrestricted'],
        StringToken: lambda t: ['string', t.value],
        URLToken: lambda t: ['url', t.value],
        NumberToken: lambda t: ['number', *numeric(t)],
        PercentageToken: lambda t: ['percentage', *numeric(t)],
        DimensionToken: lambda t: ['dimension', *numeric(t), t.unit],
        UnicodeRangeToken: lambda t: ['unicode-range', t.start, t.end],

        CurlyBracketsBlock: lambda t: ['{}', *to_json(t.content)],
        SquareBracketsBlock: lambda t: ['[]', *to_json(t.content)],
        ParenthesesBlock: lambda t: ['()', *to_json(t.content)],
        FunctionBlock: lambda t: ['function', t.name, *to_json(t.arguments)],

        Declaration: lambda d: ['declaration', d.name, to_json(d.value), d.important],
        AtRule: lambda r: [
            'at-rule', r.at_keyword, to_json(r.prelude), to_json(r.content)],
        QualifiedRule: lambda r: [
            'qualified rule', to_json(r.prelude), to_json(r.content)],

        RGBA: lambda v: [round(c, 6) for c in v],
        Color: lambda v: [
            v.space,
            [round(c, 6) for c in v.params],
            v.function_name,
            [None if arg is None else round(arg, 6) for arg in v.args],
            v.alpha,
        ],
    }


def load_json(filename):
    path = Path(__file__).parent / 'css-parsing-tests' / filename
    json_data = json.loads(path.read_text(encoding='utf-8'))
    return list(zip(json_data[::2], json_data[1::2]))


def json_test(filename=None):
    def decorator(function):
        filename_ = filename or function.__name__.split('_', 1)[-1] + '.json'

        @pytest.mark.parametrize(('css', 'expected'), load_json(filename_))
        def test(css, expected):
            value = to_json(function(css))
            if value != expected:  # pragma: no cover
                pprint.pprint(value)
                assert value == expected
        return test
    return decorator


SKIP = {'skip_comments': True, 'skip_whitespace': True}


@json_test()
def test_component_value_list(input):
    return parse_component_value_list(input, skip_comments=True)


@json_test()
def test_one_component_value(input):
    return parse_one_component_value(input, skip_comments=True)


@json_test()
def test_declaration_list(input):
    return parse_declaration_list(input, **SKIP)


@json_test()
def test_blocks_contents(input):
    return parse_blocks_contents(input, **SKIP)


@json_test()
def test_one_declaration(input):
    return parse_one_declaration(input, skip_comments=True)


@json_test()
def test_stylesheet(input):
    return parse_stylesheet(input, **SKIP)


@json_test()
def test_rule_list(input):
    return parse_rule_list(input, **SKIP)


@json_test()
def test_one_rule(input):
    return parse_one_rule(input, skip_comments=True)


@json_test(filename='An+B.json')
def test_nth(input):
    return parse_nth(input)


@pytest.mark.parametrize(('a', 'b', 'expected'), [
    (0, 0, '0'), (0, 1, '1'), (0, -2, '-2'), (1, 0, 'n'), (1, 3, 'n+3'), (1, -3, 'n-3'),
    (-1, 0, '-n'), (-1, 3, '-n+3'), (-1, -3, '-n-3'), (2, 0, '2n'), (2, 1, '2n+1'),
    (2, -4, '2n-4'), (-2, 0, '-2n'), (-2, 1, '-2n+1'), (-2, -4, '-2n-4'),
])
def test_serialize_nth(a, b, expected):
    assert serialize_nth(a, b) == expected
    assert parse_nth(expected) == (a, b)


@json_test(filename='An+B.json')
def test_round_trip_nth(input):
    if numbers := parse_nth(input):
        return parse_nth(serialize_nth(*numbers))


@pytest.mark.parametrize('invalid', ['+', '+/**/', 'n+', 'n +', '-n-', '2n +'])
def test_nth_invalid_does_not_crash(invalid):
    # Truncated/invalid An+B fragments must return None per parse_nth's
    # documented contract, not raise StopIteration or AttributeError.
    assert parse_nth(invalid) is None


def test_nth_leading_plus_whitespace_still_invalid():
    # The fix for the above must not make '+ n' (whitespace after a leading
    # '+') accidentally valid: only '+n' is a valid nth expression.
    assert parse_nth('+n') == (1, 0)
    assert parse_nth('+ n') is None


def _number(value):
    if value is None:
        return 'none'
    value = round(value + 0.0000001, 6)
    return str(int(value) if value.is_integer() else value)


def _build_color(color):
    if color is None:
        return
    (*coordinates, alpha) = color
    result = f'color({color.space}'
    for coordinate in coordinates:
        result += f' {_number(coordinate)}'
    if alpha != 1:
        result += f' / {_number(alpha)}'
    result += ')'
    return result


def test_color_currentcolor_3():
    for value in ('currentcolor', 'currentColor', 'CURRENTCOLOR'):
        assert parse_color3(value) == 'currentColor'


def test_color_currentcolor_4():
    for value in ('currentcolor', 'currentColor', 'CURRENTCOLOR'):
        assert parse_color4(value) == 'currentcolor'


def test_color_currentcolor_5():
    for value in ('currentcolor', 'currentColor', 'CURRENTCOLOR'):
        assert parse_color5(value) == 'currentcolor'


@json_test()
def test_color_function_4(input):
    return _build_color(parse_color4(input))


@json_test(filename='color_function_4.json')
def test_color_function_4_with_5(input):
    return _build_color(parse_color5(input))


@json_test()
def test_color_functions_5(input):
    if input.startswith('light-dark'):
        result = []
        result.append(_build_color(parse_color5(input, ('light',))))
        result.append(_build_color(parse_color5(input, ('dark',))))
    else:
        result = _build_color(parse_color5(input))
    return result


@json_test()
def test_color_hexadecimal_3(input):
    if not (color := parse_color3(input)):
        return None
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_hexadecimal_3.json')
def test_color_hexadecimal_3_with_4(input):
    if not (color := parse_color4(input)):
        return None
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_hexadecimal_4(input):
    if not (color := parse_color4(input)):
        return None
    assert color.space == 'srgb'
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_hexadecimal_4.json')
def test_color_hexadecimal_4_with_5(input):
    if not (color := parse_color5(input)):
        return None
    assert color.space == 'srgb'
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_hexadecimal_3.json')
def test_color_hexadecimal_3_with_5(input):
    if not (color := parse_color5(input)):
        return None
    assert color.space == 'srgb'
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_hsl_3(input):
    if not (color := parse_color3(input)):
        return None
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_hsl_3.json')
def test_color_hsl_3_with_4(input):
    if not (color := parse_color4(input)):
        return None
    assert color.space == 'hsl'
    (*coordinates, alpha) = color.to('srgb')
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_hsl_3.json')
def test_color_hsl_3_with_5(input):
    if not (color := parse_color5(input)):
        return None
    assert color.space == 'hsl'
    (*coordinates, alpha) = color.to('srgb')
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_hsl_4(input):
    if not (color := parse_color4(input)):
        return None
    assert color.space == 'hsl'
    (*coordinates, alpha) = color.to('srgb')
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_hsl_4.json')
def test_color_hsl_4_with_5(input):
    if not (color := parse_color5(input)):
        return None
    assert color.space == 'hsl'
    (*coordinates, alpha) = color.to('srgb')
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_hwb_4(input):
    if not (color := parse_color4(input)):
        return None
    assert color.space == 'hwb'
    (*coordinates, alpha) = color.to('srgb')
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_hwb_4.json')
def test_color_hwb_4_with_5(input):
    if not (color := parse_color5(input)):
        return None
    assert color.space == 'hwb'
    (*coordinates, alpha) = color.to('srgb')
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_keywords_3(input):
    if not (color := parse_color3(input)):
        return None
    elif isinstance(color, str):
        return color
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_keywords_3.json')
def test_color_keywords_3_with_4(input):
    if not (color := parse_color4(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'srgb'
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_keywords_3.json')
def test_color_keywords_3_with_5(input):
    if not (color := parse_color5(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'srgb'
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_keywords_4(input):
    if not (color := parse_color4(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'srgb'
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_keywords_4.json')
def test_color_keywords_4_with_5(input):
    if not (color := parse_color5(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'srgb'
    (*coordinates, alpha) = color
    result = f'rgb{"a" if alpha != 1 else ""}('
    result += f'{", ".join(_number(coordinate * 255) for coordinate in coordinates)}'
    if alpha != 1:
        result += f', {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_lab_4(input):
    if not (color := parse_color4(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'lab'
    (*coordinates, alpha) = color
    result = f'{color.space}('
    result += f'{" ".join(_number(coordinate) for coordinate in coordinates)}'
    if alpha != 1:
        result += f' / {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_lab_4.json')
def test_color_lab_4_with_5(input):
    if not (color := parse_color5(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'lab'
    (*coordinates, alpha) = color
    result = f'{color.space}('
    result += f'{" ".join(_number(coordinate) for coordinate in coordinates)}'
    if alpha != 1:
        result += f' / {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_oklab_4(input):
    if not (color := parse_color4(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'oklab'
    (*coordinates, alpha) = color
    result = f'{color.space}('
    result += f'{" ".join(_number(coordinate) for coordinate in coordinates)}'
    if alpha != 1:
        result += f' / {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_oklab_4.json')
def test_color_oklab_4_with_5(input):
    if not (color := parse_color5(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'oklab'
    (*coordinates, alpha) = color
    result = f'{color.space}('
    result += f'{" ".join(_number(coordinate) for coordinate in coordinates)}'
    if alpha != 1:
        result += f' / {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_lch_4(input):
    if not (color := parse_color4(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'lch'
    (*coordinates, alpha) = color
    result = f'{color.space}('
    result += f'{" ".join(_number(coordinate) for coordinate in coordinates)}'
    if alpha != 1:
        result += f' / {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_lch_4.json')
def test_color_lch_4_with_5(input):
    if not (color := parse_color5(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'lch'
    (*coordinates, alpha) = color
    result = f'{color.space}('
    result += f'{" ".join(_number(coordinate) for coordinate in coordinates)}'
    if alpha != 1:
        result += f' / {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_color_oklch_4(input):
    if not (color := parse_color4(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'oklch'
    (*coordinates, alpha) = color
    result = f'{color.space}('
    result += f'{" ".join(_number(coordinate) for coordinate in coordinates)}'
    if alpha != 1:
        result += f' / {_number(alpha)}'
    result += ')'
    return result


@json_test(filename='color_oklch_4.json')
def test_color_oklch_4_with_5(input):
    if not (color := parse_color5(input)):
        return None
    elif isinstance(color, str):
        return color
    assert color.space == 'oklch'
    (*coordinates, alpha) = color
    result = f'{color.space}('
    result += f'{" ".join(_number(coordinate) for coordinate in coordinates)}'
    if alpha != 1:
        result += f' / {_number(alpha)}'
    result += ')'
    return result


@json_test()
def test_stylesheet_bytes(kwargs):
    kwargs['css_bytes'] = kwargs['css_bytes'].encode('latin1')
    kwargs.pop('comment', None)
    if kwargs.get('environment_encoding'):
        kwargs['environment_encoding'] = lookup(kwargs['environment_encoding'])
    kwargs.update(SKIP)
    return parse_stylesheet_bytes(**kwargs)


@json_test(filename='component_value_list.json')
def test_serialization(css):
    parsed = parse_component_value_list(css, skip_comments=True)
    return parse_component_value_list(serialize(parsed), skip_comments=True)


def test_skip():
    source = '''
    /* foo */
    @media print {
        #foo {
            width: /* bar*/4px;
            color: green;
        }
    }
    '''
    no_ws = parse_stylesheet(source, skip_whitespace=True)
    no_comment = parse_stylesheet(source, skip_comments=True)
    default = parse_component_value_list(source)
    assert serialize(no_ws) != source
    assert serialize(no_comment) != source
    assert serialize(default) == source


def test_comment_eof():
    source = '/* foo '
    parsed = parse_component_value_list(source)
    assert serialize(parsed) == '/* foo */'


def test_parse_declaration_value_color():
    source = 'color:#369'
    declaration = parse_one_declaration(source)
    (value_token,) = declaration.value
    assert parse_color3(value_token) == (.2, .4, .6, 1)
    assert parse_color4(value_token) == (.2, .4, .6, 1)
    assert declaration.serialize() == source


def test_serialize_rules():
    source = '@import "a.css"; foo#bar.baz { color: red } /**/ @media print{}'
    rules = parse_rule_list(source)
    assert serialize(rules) == source


def test_serialize_declarations():
    source = 'color: #123; /**/ @top-left {} width:7px !important;'
    rules = parse_blocks_contents(source)
    assert serialize(rules) == source


def test_serialize_rules_with_functions():
    source = '''
        foo#bar.baz {
            background: url();
            color: rgb(0, 0, 0);
            width: calc(calc());
            height: calc(calc(calc()));
        }
    '''
    rules = parse_rule_list(source)
    assert serialize(rules) == source


@pytest.mark.parametrize('source', ['1\\65 2', '34\\45 56a'])
def test_serialize_e_in_unit(source):
    rules = parse_component_value_list(source)
    assert serialize(rules) == source.replace('\\45', '\\65')


def test_backslash_delim():
    source = '\\\nfoo'
    tokens = parse_component_value_list(source)
    assert [t.type for t in tokens] == ['literal', 'whitespace', 'ident']
    assert tokens[0].value == '\\'
    del tokens[1]
    assert [t.type for t in tokens] == ['literal', 'ident']
    assert serialize(tokens) == source


def test_escape_in_at_rule():
    at_rule, = parse_rule_list('@\udca9')
    assert at_rule.type == 'at-rule'
    assert at_rule.at_keyword == '\udca9'


def test_escape_in_ident():
    declaration = parse_one_declaration('background:\udca9')
    assert declaration.type == 'declaration'
    value, = declaration.value
    assert value.value == '\udca9'


def test_escape_in_dimension_token():
    dimension, = parse_component_value_list('0\\dddf')
    assert dimension.type == 'dimension'
    assert dimension.int_value == 0
    assert dimension.unit == '\udddf'


def test_escape_in_function_name():
    function, = parse_component_value_list('\\dddf()')
    assert function.type == 'function'
    assert function.name == '\udddf'


def _only_block(tokens):
    return next(token for token in tokens if token.type == '{} block')


def test_source_map_basic_offsets():
    raw = b'a { color: red }'
    source_map = SourceMap.from_bytes(raw)
    assert source_map.encoding.name == 'utf-8'
    assert source_map.bom_length == 0
    byte_range, notes = source_map.locate_range(4, 14)
    assert byte_range == ByteRange(4, 14)
    assert notes == ()
    assert raw[byte_range.start:byte_range.end] == b'color: red'
    with pytest.raises(ValueError):
        source_map.locate_range(5, 5)


def test_source_map_multibyte_and_bom():
    text = 'x { bad z: 1; ok: épx }\n'
    raw = b'\xff\xfe' + text.encode('utf-16-le')
    source_map = SourceMap.from_bytes(raw)
    assert source_map.encoding.name == 'utf-16le'
    assert source_map.bom_length == 2
    result, = locate_stylesheet_declaration_errors(source_map)
    assert raw[result.error_byte_range.start:result.error_byte_range.end] == (
        'bad z: 1'.encode('utf-16-le'))
    assert raw[result.next_byte_range.start:result.next_byte_range.end] == (
        'ok: épx'.encode('utf-16-le'))
    assert result.error_byte_range.start == 2 + len(
        'x { '.encode('utf-16-le'))
    assert result.error_byte_range.end - result.error_byte_range.start == (
        len('bad z: 1') * 2)
    assert result.next_declaration.name == 'ok'


def test_source_map_decode_replacement_and_crlf():
    raw = (
        b'a {\r\n'
        b'  bad decl(;):\r\n 1;\r\n'
        b'  good: \x81px;\r\n'
        b'  tail: 2px\r\n'
        b'}')
    source_map = SourceMap.from_bytes(
        raw, protocol_encoding='windows-1252')
    assert source_map.encoding.name == 'windows-1252'
    result, = locate_stylesheet_declaration_errors(source_map)
    assert raw[result.error_byte_range.start:result.error_byte_range.end] == (
        b'bad decl(;):\r\n 1')
    assert raw[result.next_byte_range.start:result.next_byte_range.end] == (
        b'good: \x81px')
    assert source_map.normalized_text[
        result.error_text_range[0]:result.error_text_range[1]] == (
        'bad decl(;):\n 1')
    note_keys = {(note.kind, note.detail) for note in result.notes}
    assert ('newline-normalization', 'CRLF') in note_keys
    assert ('decode-replacement', 'windows-1252') in note_keys
    replacement_note = next(
        note for note in result.notes
        if note.kind == 'decode-replacement')
    assert raw[replacement_note.byte_range.start:
               replacement_note.byte_range.end] == b'\x81'


def test_source_map_nul_and_bare_cr_without_recovery():
    raw = b'a { bad\x00 z: 1; also bad: 2\r 3'
    source_map = SourceMap.from_bytes(raw)
    results = locate_stylesheet_declaration_errors(source_map)
    assert len(results) == 2
    first_notes = {(note.kind, note.detail) for note in results[0].notes}
    second_notes = {(note.kind, note.detail) for note in results[1].notes}
    assert ('nul-replacement', 'NUL') in first_notes
    assert ('newline-normalization', 'CR') in second_notes
    for result in results:
        assert result.next_declaration is None
        assert result.next_text_range is None
        assert result.next_byte_range is None
    assert raw[results[0].error_byte_range.start:
               results[0].error_byte_range.end] == b'bad\x00 z: 1'
    assert raw[results[1].error_byte_range.start:
               results[1].error_byte_range.end] == b'also bad: 2\r 3'


def test_source_map_parenthesized_semicolon_does_not_split():
    raw = b'p { bad z: calc(1; 2); color: red }'
    source_map = SourceMap.from_bytes(raw)
    block = _only_block(
        parse_component_value_list(source_map.normalized_text))
    result, = locate_declaration_list_errors(
        source_map, block.content, len(source_map.normalized_text) - 1)
    assert raw[result.error_byte_range.start:result.error_byte_range.end] == (
        b'bad z: calc(1; 2)')
    assert raw[result.next_byte_range.start:result.next_byte_range.end] == (
        b'color: red')
    assert result.next_declaration.name == 'color'


def test_source_map_at_rules_are_not_recovery_points():
    raw = b'a { bad z: 1; @media print {}; color: red }'
    result, = locate_stylesheet_declaration_errors(
        SourceMap.from_bytes(raw))
    assert result.next_declaration.name == 'color'
    assert raw[result.next_byte_range.start:
               result.next_byte_range.end] == b'color: red'


def test_source_map_multiple_rejections_keep_following_intact():
    raw = b'a { one z: 1; two z: 2; color: red }'
    results = locate_stylesheet_declaration_errors(
        SourceMap.from_bytes(raw))
    assert len(results) == 2
    for result in results:
        assert result.next_declaration.name == 'color'
        assert raw[result.next_byte_range.start:
                   result.next_byte_range.end] == b'color: red'
    assert raw[results[0].error_byte_range.start:
               results[0].error_byte_range.end] == b'one z: 1'
    assert raw[results[1].error_byte_range.start:
               results[1].error_byte_range.end] == b'two z: 2'


def test_source_map_nested_blocks_and_form_feed():
    raw = b'@media screen {\r\f a { bad z: 1; color: red } }'
    result, = locate_stylesheet_declaration_errors(
        SourceMap.from_bytes(raw))
    assert raw[result.error_byte_range.start:result.error_byte_range.end] == (
        b'bad z: 1')
    assert raw[result.next_byte_range.start:result.next_byte_range.end] == (
        b'color: red')


def test_source_map_no_errors():
    assert locate_stylesheet_declaration_errors(
        SourceMap.from_bytes(b'a { color: red }')) == []


def test_source_map_truncated_multibyte_replacement():
    raw = b'a { bad z: \xe2\x82; color: red }'
    result, = locate_stylesheet_declaration_errors(
        SourceMap.from_bytes(raw))
    note = next(
        note for note in result.notes
        if note.kind == 'decode-replacement')
    assert note.byte_range == ByteRange(11, 13)
    assert raw[note.byte_range.start:note.byte_range.end] == b'\xe2\x82'


def test_source_map_literal_replacement_character_is_not_decode_note():
    raw = 'a { bad z: �; color: red }'.encode('utf-8')
    result, = locate_stylesheet_declaration_errors(
        SourceMap.from_bytes(raw))
    assert all(
        note.kind != 'decode-replacement' for note in result.notes)


def test_source_map_mapping_note_equality():
    note = MappingNote('newline-normalization', 'CRLF', ByteRange(2, 4))
    assert note == MappingNote(
        'newline-normalization', 'CRLF', ByteRange(2, 4))
    assert note != MappingNote(
        'newline-normalization', 'CR', ByteRange(2, 4))

