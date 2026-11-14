import base64
import json

from tinycss2.references import (
    ResourceReference, extract_references, load_manifest,
    resolve_manifest_url)

COMMON = """
@import "sub/extra.css" screen and (min-width: 100px);
@import url('loop.css') layer(theme) supports(display: grid);
.a { background: url(./img/bg.png); }
@media print {
    .b { background-image: url("/assets/logo.svg"); }
    @import "nested.css";
}
@font-face { src: url("fonts/f.woff2") format("woff2"); }
"""
EXTRA = """.x { background: url(deep/x.png); }
@import '../common.css';
"""
LOOP = '.z { color: red; }\n'
NESTED = '.n { background: url(n.png); }\n'


def build_manifest():
    return [
        {
            'url': 'https://s/dir1/lib/common.css',
            'mime_type': 'text/css',
            'text': COMMON,
        },
        {
            'url': 'https://s/dir2/lib/common.css',
            'mime_type': 'text/css',
            'text': COMMON,
        },
        {
            'url': 'https://s/dir1/lib/sub/extra.css',
            'mime_type': 'text/css',
            'text': EXTRA,
        },
        {
            'url': 'https://s/dir2/lib/sub/extra.css',
            'mime_type': 'text/css',
            'text': EXTRA,
        },
        {'url': 'https://s/dir1/lib/loop.css', 'mime_type': 'text/css',
         'text': LOOP},
        {'url': 'https://s/dir2/lib/loop.css', 'mime_type': 'text/css',
         'text': LOOP},
        {'url': 'https://s/dir1/lib/nested.css', 'mime_type': 'text/css',
         'text': NESTED},
        {'url': 'https://s/dir1/lib/img/bg.png', 'mime_type': 'image/png',
         'text': 'PNG'},
        {'url': 'https://s/dir1/lib/deep/x.png', 'mime_type': 'image/png',
         'text': 'PNG'},
    ]


def test_identical_body_resolved_against_each_base():
    refs = extract_references(
        build_manifest(),
        roots=[
            'https://s/dir1/lib/common.css',
            'https://s/dir2/lib/common.css'])
    bg = [ref for ref in refs if ref.raw_url == 'url(./img/bg.png)']
    assert [ref.source for ref in bg] == [
        'https://s/dir1/lib/common.css',
        'https://s/dir2/lib/common.css']
    assert [ref.target for ref in bg] == [
        'https://s/dir1/lib/img/bg.png',
        'https://s/dir2/lib/img/bg.png']
    fonts = [ref for ref in refs if ref.raw_url == 'url("fonts/f.woff2")']
    assert {ref.target for ref in fonts} == {
        'https://s/dir1/lib/fonts/f.woff2',
        'https://s/dir2/lib/fonts/f.woff2'}
    assert all(not ref.cycle for ref in bg)
    assert all(not ref.cycle for ref in fonts)


def test_body_parsed_once_but_relationships_duplicated(monkeypatch):
    import tinycss2.references as references_module

    manifest = build_manifest()
    parsed_bodies = []
    original = references_module.parse_stylesheet

    def spy(css_text, *args, **kwargs):
        parsed_bodies.append(css_text)
        return original(css_text, *args, **kwargs)

    monkeypatch.setattr(references_module, 'parse_stylesheet', spy)
    refs = extract_references(
        manifest,
        roots=[
            'https://s/dir1/lib/common.css',
            'https://s/dir2/lib/common.css'])
    common_refs = [
        ref for ref in refs if ref.source.endswith('/lib/common.css')]
    assert len(common_refs) == 12
    assert parsed_bodies.count(COMMON) == 1
    assert parsed_bodies.count(EXTRA) == 1
    assert parsed_bodies.count(LOOP) == 1


def test_import_conditions_and_raw_spelling_preserved():
    refs = extract_references(
        build_manifest(), roots=['https://s/dir1/lib/common.css'])
    imports = [ref for ref in refs if ref.kind == 'import']
    by_raw = {ref.raw_url: ref for ref in imports}
    assert by_raw['"sub/extra.css"'].condition == (
        'screen and (min-width: 100px)')
    loop_ref = by_raw["url('loop.css')"]
    assert loop_ref.condition == (
        "layer(theme) supports(display: grid)")
    assert loop_ref.raw_url == "url('loop.css')"
    assert loop_ref.target == 'https://s/dir1/lib/loop.css'


def test_cycle_back_to_ancestor_is_recorded_and_stops_branch():
    refs = extract_references(
        build_manifest(), roots=['https://s/dir1/lib/common.css'])
    cycles = [ref for ref in refs if ref.cycle]
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.kind == 'import'
    assert cycle.source == 'https://s/dir1/lib/sub/extra.css'
    assert cycle.raw_url == "'../common.css'"
    assert cycle.target == 'https://s/dir1/lib/common.css'
    extra_resources = [
        ref for ref in refs
        if ref.source == 'https://s/dir1/lib/sub/extra.css']
    assert [ref.raw_url for ref in extra_resources] == [
        'url(deep/x.png)', "'../common.css'"]


def test_duplicate_in_other_branch_survives_a_cycle():
    refs = extract_references(
        build_manifest(),
        roots=[
            'https://s/dir1/lib/common.css',
            'https://s/dir2/lib/common.css'])
    extra_imports = [
        ref for ref in refs
        if ref.raw_url == '"sub/extra.css"']
    assert len(extra_imports) == 2
    assert {ref.target for ref in extra_imports} == {
        'https://s/dir1/lib/sub/extra.css',
        'https://s/dir2/lib/sub/extra.css'}
    cycles = [ref for ref in refs if ref.cycle]
    assert len(cycles) == 2
    assert {ref.source for ref in cycles} == {
        'https://s/dir1/lib/sub/extra.css',
        'https://s/dir2/lib/sub/extra.css'}


def test_nested_import_is_reported_but_not_followed():
    refs = extract_references(
        build_manifest(), roots=['https://s/dir1/lib/common.css'])
    nested = [
        ref for ref in refs
        if ref.raw_url == '"nested.css"']
    assert len(nested) == 1
    assert nested[0].source == 'https://s/dir1/lib/common.css'
    assert nested[0].target == 'https://s/dir1/lib/nested.css'
    assert not any(
        ref.source == 'https://s/dir1/lib/nested.css' for ref in refs)


def test_redirects_resolve_to_final_response_url():
    manifest = build_manifest()
    manifest.append({
        'url': 'https://s/old/common.css',
        'status': 301,
        'redirect_url': 'https://s/dir1/lib/common.css',
        'mime_type': 'text/css',
        'text': '',
    })
    assert resolve_manifest_url(
        manifest, 'https://s/old/common.css') == (
        'https://s/dir1/lib/common.css')
    refs = extract_references(
        manifest, roots=['https://s/old/common.css'])
    assert refs[0].source == 'https://s/dir1/lib/common.css'
    assert refs[0].kind == 'import'


def test_har_document_and_mapping_inputs(tmp_path):
    har = {
        'log': {
            'entries': [
                {
                    'request': {'url': 'https://s/a.css'},
                    'response': {
                        'status': 200,
                        'redirectURL': '',
                        'content': {
                            'mimeType': 'text/css',
                            'text': '.a { background: url(x.png); }',
                        },
                    },
                },
            ],
        },
    }
    path = tmp_path / 'manifest.har'
    path.write_text(json.dumps(har), encoding='utf-8')
    loaded = load_manifest(path)
    refs = extract_references(loaded)
    assert refs == [ResourceReference(
        'resource', 'https://s/a.css', 'url(x.png)',
        'https://s/x.png', None, False)]
    mapping_refs = extract_references(
        {'https://s/a.css': '.a { background: url(x.png); }'})
    assert mapping_refs == refs


def test_relative_and_absolute_url_forms():
    manifest = [{
        'url': 'https://s/dir/a.css',
        'mime_type': 'text/css',
        'text': (
            '@import url(https://cdn.test/ext.css);\n'
            '.a { background: url(data:image/png;base64,AAAA); }\n'
            '.b { background: url("//cdn.test/protocol.png"); }\n'),
    }, {
        'url': 'https://cdn.test/ext.css',
        'mime_type': 'text/css',
        'text': '.c { background: url(ext.png); }',
    }]
    refs = extract_references(manifest)
    by_raw = {ref.raw_url: ref for ref in refs}
    assert by_raw['url(https://cdn.test/ext.css)'].target == (
        'https://cdn.test/ext.css')
    assert by_raw['url(data:image/png;base64,AAAA)'].target == (
        'data:image/png;base64,AAAA')
    assert by_raw['url("//cdn.test/protocol.png")'].target == (
        'https://cdn.test/protocol.png')
    assert by_raw['url(ext.png)'].source == 'https://cdn.test/ext.css'


def test_resource_redirect_targets_final_response():
    manifest = [{
        'url': 'https://s/a.css',
        'mime_type': 'text/css',
        'text': '.a { background: url(old.png); }',
    }, {
        'url': 'https://s/old.png',
        'status': 302,
        'redirect_url': 'https://s/new.png',
        'text': '',
    }, {
        'url': 'https://s/new.png',
        'mime_type': 'image/png',
        'text': 'PNG',
    }]
    refs = extract_references(manifest)
    assert len(refs) == 1
    assert refs[0].kind == 'resource'
    assert refs[0].raw_url == 'url(old.png)'
    assert refs[0].target == 'https://s/new.png'
    assert not refs[0].cycle


def test_self_import_is_a_cycle_and_keeps_sibling_relationships():
    body = (
        '@import "self.css";\n'
        '@import "sibling.css";\n'
        '.a { background: url(a.png); }\n')
    manifest = [{
        'url': 'https://s/self.css',
        'mime_type': 'text/css',
        'text': body,
    }, {
        'url': 'https://s/sibling.css',
        'mime_type': 'text/css',
        'text': '.b { background: url(b.png); }\n',
    }]
    refs = extract_references(manifest, roots=['https://s/self.css'])
    by_target = {ref.target: ref for ref in refs}
    self_import = by_target['https://s/self.css']
    sibling_import = by_target['https://s/sibling.css']
    assert self_import.kind == 'import'
    assert self_import.cycle
    assert sibling_import.kind == 'import'
    assert not sibling_import.cycle
    assert by_target['https://s/a.png'].source == 'https://s/self.css'
    sibling_resource = by_target['https://s/b.png']
    assert sibling_resource.source == 'https://s/sibling.css'


def test_cycle_in_one_branch_does_not_deduplicate_other_branch():
    shared = '@import "back.css";\n.a { background: url(shared.png); }\n'
    manifest = [
        {'url': 'https://s/branch1/back.css', 'mime_type': 'text/css',
         'text': shared},
        {'url': 'https://s/branch1/main.css', 'mime_type': 'text/css',
         'text': '@import "back.css";'},
        {'url': 'https://s/branch2/back.css', 'mime_type': 'text/css',
         'text': shared},
        {'url': 'https://s/branch2/main.css', 'mime_type': 'text/css',
         'text': '@import "back.css";'},
    ]
    refs = extract_references(
        manifest,
        roots=['https://s/branch1/main.css', 'https://s/branch2/main.css'])
    cycles = [ref for ref in refs if ref.cycle]
    assert len(cycles) == 2
    assert {ref.source for ref in cycles} == {
        'https://s/branch1/back.css',
        'https://s/branch2/back.css'}
    shared_png = [ref for ref in refs if ref.raw_url == 'url(shared.png)']
    assert {ref.target for ref in shared_png} == {
        'https://s/branch1/shared.png',
        'https://s/branch2/shared.png'}


def test_har_base64_content_is_decoded():
    css = '.a { background: url(x.png); }'
    har = {
        'log': {
            'entries': [{
                'request': {'url': 'https://s/a.css'},
                'response': {
                    'status': 200,
                    'content': {
                        'mimeType': 'text/css',
                        'encoding': 'base64',
                        'text': base64.b64encode(css.encode()).decode(),
                    },
                },
            }],
        },
    }
    refs = extract_references(har)
    assert len(refs) == 1
    assert refs[0].target == 'https://s/x.png'
