"""Tests for fixed-width report row splitting with redacted (masked) fields."""

# 3rd party
import pytest

# local
from wcwidth.report_split import (
    Alignment,
    FieldSpec,
    InfeasibleRowError,
    MaskedField,
    ReportSpec,
    ReportSplitError,
    certain_split,
    possible_splits,
    split_report_row,
)


def _names_spec():
    return ReportSpec((
        FieldSpec('name'),
        FieldSpec('amount', Alignment.RIGHT, is_integer=True),
    ))


def test_single_masked_field_enumerates_every_split():
    spec = _names_spec()
    row = '**  42'
    splits = possible_splits(row, spec, (MaskedField('name', 2, 4),))

    assert len(splits) == 3
    boundaries = [(split.widths['name'], split.widths['amount']) for split in splits]
    assert boundaries == [(2, 4), (3, 3), (4, 2)]
    for split in splits:
        assert sum(split.widths.values()) == len(row)
        assert ''.join(split.values.values()) == row
        assert split.starts['amount'] == split.widths['name']
        assert split.values['amount'].lstrip(' ').isdigit()


def test_forced_column_survives_ambiguity_elsewhere():
    # Leading spaces make the right-aligned masked name reach two end
    # positions, but only one survives the fixed fields: its width is forced
    # ("交账") even though the other two columns stay ambiguous.
    spec = ReportSpec((
        FieldSpec('name', Alignment.RIGHT),
        FieldSpec('code'),
        FieldSpec('amount', Alignment.RIGHT, is_integer=True),
    ))
    row = '  **AB 42'
    splits, certain = split_report_row(row, spec, (MaskedField('name', 1, 4),))

    assert len(splits) == 2
    assert certain == {'name': 4, 'code': None, 'amount': None}
    assert {split.widths['code'] for split in splits} == {2, 3}
    for split in splits:
        assert split.values['name'] == '  **'
        assert split.values['code'].rstrip(' ') == 'AB'


def test_multiple_masked_segments_constrain_each_other():
    # Two mask runs pin a one-digit integer between them without any padding
    # freedom, so the joint constraints leave a single interpretation.
    spec = ReportSpec((
        FieldSpec('first'),
        FieldSpec('middle', is_integer=True),
        FieldSpec('second'),
    ))
    row = '**7**'
    masked = (MaskedField('first', 1, 4), MaskedField('second', 1, 4))
    splits = possible_splits(row, spec, masked)

    assert len(splits) == 1
    split = splits[0]
    assert split.widths == {'first': 2, 'middle': 1, 'second': 2}
    assert split.values == {'first': '**', 'middle': '7', 'second': '**'}

    # Demanding a run of at least three masks cannot be satisfied by either
    # observed two-symbol run, so the whole row becomes infeasible.
    tight = (MaskedField('first', 3, 4), MaskedField('second', 3, 4))
    with pytest.raises(InfeasibleRowError):
        possible_splits(row, spec, tight)


def test_right_aligned_masked_field():
    spec = ReportSpec((
        FieldSpec('name', Alignment.RIGHT),
        FieldSpec('amount', Alignment.RIGHT, is_integer=True),
    ))
    row = ' **42'
    splits = possible_splits(row, spec, (MaskedField('name', 2, 4),))

    assert len(splits) == 1
    split = splits[0]
    assert split.widths == {'name': 3, 'amount': 2}
    assert split.values == {'name': ' **', 'amount': '42'}


def test_integer_field_rejects_non_digit_body():
    spec = _names_spec()
    with pytest.raises(InfeasibleRowError):
        possible_splits('** AB', spec, (MaskedField('name', 2, 4),))


def test_mask_symbol_cannot_occur_in_fixed_field():
    spec = ReportSpec((FieldSpec('code'), FieldSpec('amount', Alignment.RIGHT)))
    with pytest.raises(InfeasibleRowError):
        possible_splits('A* 42', spec, (MaskedField('amount', 1, 3),))


def test_infeasible_when_side_table_range_excludes_run():
    spec = _names_spec()
    with pytest.raises(InfeasibleRowError):
        possible_splits('** 42', spec, (MaskedField('name', 3, 5),))


def test_certain_split_marks_forced_and_unknown_widths():
    spec = _names_spec()
    splits = possible_splits('**42', spec, (MaskedField('name', 2, 2),))
    certain = certain_split(splits)

    assert len(splits) == 1
    assert certain == {'name': 2, 'amount': 2}


def test_side_table_bounds_the_mask_run_not_the_column_width():
    # The run is two symbols; a fixed range of 2 still allows the trailing
    # padding space to be claimed by either column.
    spec = _names_spec()
    splits, certain = split_report_row('** 42', spec, (MaskedField('name', 2, 2),))

    assert len(splits) == 2
    assert certain == {'name': None, 'amount': None}


def test_certain_split_rejects_empty_input():
    with pytest.raises(ReportSplitError):
        certain_split(())


@pytest.mark.parametrize(
    ('kwargs', 'masked'),
    [
        ({'fields': ()}, ()),
        ({'fields': (FieldSpec('name'),), 'mask_symbol': '**'},
         (MaskedField('name', 1, 2),)),
        ({'fields': (FieldSpec('name'),), 'mask_symbol': ' '},
         (MaskedField('name', 1, 2),)),
        ({'fields': (FieldSpec('name'), FieldSpec('name'))},
         (MaskedField('name', 1, 2),)),
    ],
)
def test_spec_validation_errors(kwargs, masked):
    with pytest.raises(ReportSplitError):
        possible_splits('**', ReportSpec(**kwargs), masked)


def test_masked_field_validation_errors():
    spec = _names_spec()
    with pytest.raises(ReportSplitError):
        possible_splits('** 42', spec, (MaskedField('missing', 1, 2),))
    with pytest.raises(ReportSplitError):
        possible_splits('** 42', spec, (MaskedField('name', 0, 2),))
    with pytest.raises(ReportSplitError):
        possible_splits('** 42', spec, (MaskedField('name', 3, 2),))
    with pytest.raises(ReportSplitError):
        possible_splits(
            '** 42', spec,
            (MaskedField('name', 1, 2), MaskedField('name', 1, 2)))


def test_row_must_be_single_nonempty_line():
    spec = _names_spec()
    masked = (MaskedField('name', 1, 2),)
    with pytest.raises(ReportSplitError):
        possible_splits('', spec, masked)
    with pytest.raises(ReportSplitError):
        possible_splits('**\n', spec, masked)


def test_splits_are_deterministic_and_fully_tile_the_row():
    spec = ReportSpec((
        FieldSpec('name'),
        FieldSpec('city'),
        FieldSpec('amount', Alignment.RIGHT, is_integer=True),
    ))
    row = '***PARIS  100'
    masked = (MaskedField('name', 1, 5),)
    splits = possible_splits(row, spec, masked)

    assert splits == possible_splits(row, spec, masked)
    assert len(splits) == 3
    for split in splits:
        assert ''.join(split.values[field.name] for field in spec.fields) == row
        offset = 0
        for field in spec.fields:
            assert split.starts[field.name] == offset
            offset += split.widths[field.name]
        assert offset == len(row)
        assert split.values['city'].rstrip(' ') == 'PARIS'
        assert split.values['amount'].lstrip(' ').isdigit()
