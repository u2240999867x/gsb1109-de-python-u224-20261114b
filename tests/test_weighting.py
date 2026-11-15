"""Unit tests for tag-weight to theme rollups (no network access)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import pylast
from pylast import (
    SourceReport,
    TagWeight,
    ThemeSplit,
    ThemeWeightRollup,
    UnreturnedRange,
    WeightBasis,
    roll_up_theme_weights,
)


def test_short_normalized_list_is_not_padded_to_one() -> None:
    # Known weights only cover 70% of one play; the missing 30% stays unassigned.
    report = SourceReport(
        source="fm",
        basis=WeightBasis.NORMALIZED,
        known=[TagWeight("rock", 0.45), TagWeight("pop", 0.25)],
        tag_themes={
            "rock": [ThemeSplit("energy", 1.0)],
            "pop": [ThemeSplit("mainstream", 1.0)],
        },
    )

    rollup = roll_up_theme_weights([report])

    assert rollup.contributions == pytest.approx(
        {"energy": 0.45, "mainstream": 0.25}
    )
    share = rollup.unassigned["fm"]
    assert share.bounded is False
    assert share.point == pytest.approx(0.30)
    assert share.low == share.high == pytest.approx(0.30)
    assert rollup.assigned_total == pytest.approx(0.70)
    assert rollup.comparable_total == {"fm": pytest.approx(0.70)}


def test_tag_aliases_are_counted_once() -> None:
    report = SourceReport(
        source="fm",
        basis=WeightBasis.NORMALIZED,
        known=[
            TagWeight("Rock", 0.40),
            TagWeight("rock", 0.10),
            TagWeight(" ROCK ", 0.05),
        ],
        tag_themes={"rock": [ThemeSplit("energy", 1.0)]},
    )

    rollup = roll_up_theme_weights([report])

    assert rollup.contributions == pytest.approx({"energy": 0.55})
    assert set(rollup.contributions) == {"energy"}
    assert rollup.unassigned["fm"].point == pytest.approx(0.45)


def test_theme_mapping_keys_are_alias_insensitive() -> None:
    report = SourceReport(
        source="fm",
        basis=WeightBasis.RELATIVE,
        known=[TagWeight("Hip-Hop", 100.0)],
        tag_themes={" hip-hop ": [ThemeSplit("urban", 1.0)]},
    )

    rollup = roll_up_theme_weights([report])

    assert rollup.contributions == pytest.approx({"urban": 100.0})


def test_one_tag_split_across_multiple_themes() -> None:
    report = SourceReport(
        source="fm",
        basis=WeightBasis.NORMALIZED,
        known=[TagWeight("jazz", 0.60)],
        tag_themes={
            "jazz": [ThemeSplit("late night", 0.6), ThemeSplit("chill", 0.4)]
        },
    )

    rollup = roll_up_theme_weights([report])

    assert rollup.contributions == pytest.approx(
        {"late night": 0.36, "chill": 0.24}
    )
    assert rollup.unassigned["fm"].point == pytest.approx(0.40)


def test_partial_allocation_leaves_the_rest_unassigned() -> None:
    report = SourceReport(
        source="fm",
        basis=WeightBasis.NORMALIZED,
        known=[TagWeight("jazz", 0.60)],
        tag_themes={"jazz": [ThemeSplit("late night", 0.5)]},
    )

    rollup = roll_up_theme_weights([report])

    assert rollup.contributions == pytest.approx({"late night": 0.30})
    assert rollup.unassigned["fm"].point == pytest.approx(0.70)
    assert rollup.assigned_total + rollup.unassigned["fm"].point == pytest.approx(
        1.0
    )


def test_unmapped_known_weight_is_unassigned() -> None:
    report = SourceReport(
        source="fm",
        basis=WeightBasis.NORMALIZED,
        known=[TagWeight("rock", 0.40), TagWeight("metal", 0.10)],
        tag_themes={"rock": [ThemeSplit("energy", 1.0)]},
    )

    rollup = roll_up_theme_weights([report])

    assert rollup.contributions == pytest.approx({"energy": 0.40})
    # 0.10 unmapped metal + 0.50 residual from the truncated list.
    assert rollup.unassigned["fm"].point == pytest.approx(0.60)


def test_unreturned_range_drives_bounded_unassigned_interval() -> None:
    report = SourceReport(
        source="fm",
        basis=WeightBasis.NORMALIZED,
        known=[TagWeight("rock", 0.50)],
        unreturned=UnreturnedRange(0.10, 0.20),
        tag_themes={"rock": [ThemeSplit("energy", 0.5)]},
    )

    rollup = roll_up_theme_weights([report])

    assert rollup.contributions == pytest.approx({"energy": 0.25})
    share = rollup.unassigned["fm"]
    assert share.bounded is True
    assert share.low == pytest.approx(0.10 + 0.25)
    assert share.high == pytest.approx(0.20 + 0.25)
    assert share.point == pytest.approx(0.15 + 0.25)


def test_relative_source_is_excluded_from_comparable_subset() -> None:
    report = SourceReport(
        source="raw",
        basis=WeightBasis.RELATIVE,
        known=[TagWeight("rock", 100.0), TagWeight("pop", 50.0)],
        tag_themes={
            "rock": [ThemeSplit("energy", 1.0)],
            "pop": [ThemeSplit("mainstream", 1.0)],
        },
    )

    rollup = roll_up_theme_weights([report])

    assert rollup.contributions == pytest.approx(
        {"energy": 100.0, "mainstream": 50.0}
    )
    assert dict(rollup.comparable_total) == {}
    assert "raw" in rollup.unassigned
    assert rollup.unassigned["raw"].bounded is False


def test_mixed_bases_only_aggregate_normalized_in_comparable_subset() -> None:
    normalized = SourceReport(
        source="fm",
        basis=WeightBasis.NORMALIZED,
        known=[TagWeight("rock", 0.40)],
        tag_themes={"rock": [ThemeSplit("energy", 1.0)]},
    )
    relative = SourceReport(
        source="raw",
        basis=WeightBasis.RELATIVE,
        known=[TagWeight("rock", 200.0)],
        tag_themes={"rock": [ThemeSplit("energy", 1.0)]},
    )

    rollup = roll_up_theme_weights([normalized, relative])

    assert rollup.contributions == pytest.approx({"energy": 200.40})
    assert set(rollup.comparable_total) == {"fm"}
    assert rollup.comparable_total["fm"] == pytest.approx(0.40)
    assert set(rollup.unassigned) == {"fm", "raw"}


def test_themes_accumulate_across_multiple_sources_and_artists() -> None:
    first = SourceReport(
        source="fm",
        basis=WeightBasis.NORMALIZED,
        known=[TagWeight("rock", 0.40), TagWeight("jazz", 0.20)],
        tag_themes={
            "rock": [ThemeSplit("energy", 1.0)],
            "jazz": [ThemeSplit("chill", 1.0)],
        },
    )
    second = SourceReport(
        source="partner",
        basis=WeightBasis.NORMALIZED,
        known=[TagWeight("rock", 0.30)],
        tag_themes={"rock": [ThemeSplit("energy", 1.0)]},
    )

    rollup = roll_up_theme_weights([first, second])

    assert rollup.contributions == pytest.approx(
        {"energy": 0.70, "chill": 0.20}
    )
    assert rollup.comparable_total == pytest.approx({"fm": 0.60, "partner": 0.30})


def test_normalized_weights_above_one_are_rejected() -> None:
    with pytest.raises(ValueError, match="exceeding 1"):
        SourceReport(
            source="fm",
            basis=WeightBasis.NORMALIZED,
            known=[TagWeight("rock", 0.60), TagWeight("pop", 0.50)],
        )


def test_ratios_above_one_are_rejected() -> None:
    with pytest.raises(ValueError, match="exceeding 1"):
        SourceReport(
            source="fm",
            basis=WeightBasis.RELATIVE,
            known=[TagWeight("rock", 1.0)],
            tag_themes={
                "rock": [ThemeSplit("energy", 0.8), ThemeSplit("other", 0.4)]
            },
        )


def test_mapping_unknown_tag_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown tag"):
        SourceReport(
            source="fm",
            basis=WeightBasis.RELATIVE,
            known=[TagWeight("rock", 1.0)],
            tag_themes={"disco": [ThemeSplit("dance", 1.0)]},
        )


def test_duplicate_canonical_mapping_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate theme mapping"):
        SourceReport(
            source="fm",
            basis=WeightBasis.RELATIVE,
            known=[TagWeight("rock", 1.0)],
            tag_themes={
                "Rock": [ThemeSplit("energy", 1.0)],
                "rock": [ThemeSplit("other", 1.0)],
            },
        )


def test_negative_and_non_finite_weights_are_rejected() -> None:
    with pytest.raises(ValueError):
        SourceReport(
            source="fm",
            basis=WeightBasis.RELATIVE,
            known=[TagWeight("rock", -1.0)],
        )


def test_inverted_unreturned_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="high must be >= low"):
        SourceReport(
            source="fm",
            basis=WeightBasis.NORMALIZED,
            known=[TagWeight("rock", 0.10)],
            unreturned=UnreturnedRange(0.30, 0.10),
        )


def test_duplicate_source_is_rejected() -> None:
    report = SourceReport(
        source="fm",
        basis=WeightBasis.RELATIVE,
        known=[TagWeight("rock", 1.0)],
    )
    rollup = ThemeWeightRollup()
    rollup.add_source(report)

    with pytest.raises(ValueError, match="already added"):
        rollup.add_source(report)


def test_from_top_items_accepts_pylast_top_items() -> None:
    top_items = [
        SimpleNamespace(
            item=SimpleNamespace(get_name=lambda: "rock"), weight=100
        ),
        SimpleNamespace(
            item=SimpleNamespace(get_name=lambda: "jazz"), weight=40
        ),
    ]

    report = SourceReport.from_top_items(
        source="fm",
        basis=WeightBasis.RELATIVE,
        top_items=top_items,
        tag_themes={"rock": [ThemeSplit("energy", 1.0)]},
    )

    rollup = roll_up_theme_weights([report])
    assert report.known == (TagWeight("rock", 100.0), TagWeight("jazz", 40.0))
    assert rollup.contributions == pytest.approx({"energy": 100.0})
    assert rollup.unassigned["fm"].point == pytest.approx(40.0)


def test_builder_and_helper_produce_identical_rollups() -> None:
    reports = [
        SourceReport(
            source="fm",
            basis=WeightBasis.NORMALIZED,
            known=[TagWeight("rock", 0.40)],
            tag_themes={"rock": [ThemeSplit("energy", 1.0)]},
        ),
        SourceReport(
            source="raw",
            basis=WeightBasis.RELATIVE,
            known=[TagWeight("jazz", 8.0)],
            tag_themes={"jazz": [ThemeSplit("chill", 1.0)]},
        ),
    ]

    builder = ThemeWeightRollup()
    for report in reports:
        builder.add_source(report)
    built = builder.build()
    helper = roll_up_theme_weights(reports)

    assert dict(built.contributions) == pytest.approx(dict(helper.contributions))
    assert dict(built.comparable_total) == pytest.approx(
        dict(helper.comparable_total)
    )
    assert built.unassigned.keys() == helper.unassigned.keys()


def test_rollup_contributions_are_immutable() -> None:
    rollup = roll_up_theme_weights(
        [
            SourceReport(
                source="fm",
                basis=WeightBasis.RELATIVE,
                known=[TagWeight("rock", 1.0)],
                tag_themes={"rock": [ThemeSplit("energy", 1.0)]},
            )
        ]
    )

    with pytest.raises(TypeError):
        rollup.contributions["energy"] = 99.0


def test_public_api_is_exported() -> None:
    for name in (
        "WeightBasis",
        "TagWeight",
        "UnreturnedRange",
        "ThemeSplit",
        "SourceReport",
        "UnassignedShare",
        "WeightRollup",
        "ThemeWeightRollup",
        "roll_up_theme_weights",
    ):
        assert hasattr(pylast, name)
