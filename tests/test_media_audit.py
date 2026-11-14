import pytest

from media_audit import (
    ALWAYS,
    NEVER,
    SOMETIMES,
    UNKNOWN,
    DeviceProfile,
    audit,
    main,
)


def statuses(css, profile):
    return [r.status for r in audit(css, profile)]


def reasons_of(css, profile, index=0):
    return audit(css, profile)[index].reasons


PROFILE = DeviceProfile(min_width=360, max_width=1440)


def test_must_use_when_width_covers_all_devices():
    css = '@media (min-width: 300px) { a { color: red } }'
    assert statuses(css, PROFILE) == [ALWAYS]


def test_excluded_when_width_has_no_intersection():
    css = '@media (min-width: 2000px) { a { color: red } }'
    assert statuses(css, PROFILE) == [NEVER]
    assert any('无交集' in r for r in reasons_of(css, PROFILE))


def test_possible_when_width_partially_overlaps():
    css = '@media (max-width: 800px) { a { color: red } }'
    assert statuses(css, PROFILE) == [SOMETIMES]


def test_open_bound_excludes_single_width_device():
    profile = DeviceProfile(min_width=600, max_width=600)
    assert statuses('@media (width > 600px) {}', profile) == [NEVER]


def test_closed_bound_includes_single_width_device():
    profile = DeviceProfile(min_width=600, max_width=600)
    assert statuses('@media (width >= 600px) {}', profile) == [ALWAYS]


def test_closed_range_syntax():
    css = '@media (400px <= width <= 800px) {}'
    assert statuses(css, PROFILE) == [SOMETIMES]
    narrow = DeviceProfile(min_width=500, max_width=700)
    assert statuses(css, narrow) == [ALWAYS]
    wide = DeviceProfile(min_width=900, max_width=1000)
    assert statuses(css, wide) == [NEVER]


def test_open_range_syntax_boundary():
    profile = DeviceProfile(min_width=400, max_width=800)
    assert statuses('@media (400px < width < 800px) {}', profile) == [SOMETIMES]
    inner = DeviceProfile(min_width=401, max_width=799)
    assert statuses('@media (400px < width < 800px) {}', inner) == [ALWAYS]


def test_comma_branches_are_alternatives():
    css = '@media (min-width: 2000px), (max-width: 500px) {}'
    assert statuses(css, PROFILE) == [SOMETIMES]


def test_all_branches_excluded_means_excluded():
    css = '@media (min-width: 2000px), (max-width: 100px) {}'
    assert statuses(css, PROFILE) == [NEVER]


def test_one_always_true_branch_means_must_use():
    css = '@media (min-width: 2000px), (min-width: 100px) {}'
    assert statuses(css, PROFILE) == [ALWAYS]


def test_nested_layers_must_both_hold():
    css = '''
    @media (min-width: 300px) {
      @media (max-width: 500px) { a { color: red } }
    }
    '''
    assert statuses(css, PROFILE) == [ALWAYS, SOMETIMES]


def test_nested_exclusion_comes_from_inner_layer():
    css = '''
    @media (min-width: 300px) {
      @media (min-width: 5000px) { a { color: red } }
    }
    '''
    resources = audit(css, PROFILE)
    assert [r.status for r in resources] == [ALWAYS, NEVER]
    assert any('第2层' in r for r in resources[1].reasons)


def test_unknown_feature_keeps_uncertainty():
    css = '@media (hover: hover) {}'
    assert statuses(css, PROFILE) == [UNKNOWN]


def test_unknown_feature_with_impossible_width_is_excluded():
    css = '@media (hover: hover) and (min-width: 2000px) {}'
    assert statuses(css, PROFILE) == [NEVER]


def test_unknown_feature_in_one_branch_only():
    css = '@media (hover: hover), (min-width: 2000px) {}'
    assert statuses(css, PROFILE) == [UNKNOWN]


def test_prefers_color_scheme():
    dark_only = DeviceProfile(
        min_width=360, max_width=1440, color_schemes=('dark',))
    css = '@media (prefers-color-scheme: dark) {}'
    assert statuses(css, dark_only) == [ALWAYS]
    assert statuses(css, PROFILE) == [SOMETIMES]
    assert statuses('@media (prefers-color-scheme: light) {}', dark_only) == [
        NEVER]


def test_not_negates_known_condition():
    assert statuses('@media not (min-width: 2000px) {}', PROFILE) == [ALWAYS]
    assert statuses('@media not (min-width: 100px) {}', PROFILE) == [NEVER]


def test_media_type_mismatch_excludes():
    assert statuses('@media print {}', PROFILE) == [NEVER]
    assert statuses('@media screen and (min-width: 100px) {}', PROFILE) == [
        ALWAYS]


def test_unconditional_rules_are_must_use():
    css = 'a { color: red }'
    resources = audit(css, PROFILE)
    assert [r.status for r in resources] == [ALWAYS]
    assert resources[0].verdict == '必用'


def test_verdict_labels():
    css = '''
    @media (min-width: 100px) { a {} }
    @media (min-width: 9000px) { b {} }
    @media (max-width: 800px) { c {} }
    '''
    assert [r.verdict for r in audit(css, PROFILE)] == [
        '必用', '可排除', '可能用']


def test_non_px_length_keeps_uncertainty():
    css = '@media (min-width: 100em) {}'
    assert statuses(css, PROFILE) == [UNKNOWN]


def test_cli_json(tmp_path, capsys):
    css_file = tmp_path / 'styles.css'
    css_file.write_text(
        '@media (min-width: 2000px) { a { color: red } }', encoding='utf-8')
    exit_code = main([
        str(css_file), '--min-width', '360', '--max-width', '1440',
        '--color-scheme', 'light', '--json'])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert '可排除' in output
    assert '无交集' in output


def test_cli_rejects_inverted_width_range(tmp_path):
    css_file = tmp_path / 'styles.css'
    css_file.write_text('a {}', encoding='utf-8')
    with pytest.raises(SystemExit):
        main([str(css_file), '--min-width', '1000', '--max-width', '500'])
