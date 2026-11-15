from __future__ import annotations

from datetime import datetime, timezone

import pytest

import pystac
from pystac import Asset, Item
from pystac.temporal import TimeOrigin, get_asset_time


def _utc(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 14, hour, minute, second, tzinfo=timezone.utc)


def _flight_item() -> Item:
    # The item covers a full half-hour flight block.
    item = Item(
        id="flight",
        geometry=None,
        bbox=None,
        datetime=None,
        properties={
            "start_datetime": "2026-11-14T10:00:00Z",
            "end_datetime": "2026-11-14T10:30:00Z",
        },
    )
    # A two-second clip inside the block, declaring its own range.
    item.add_asset(
        "clip",
        Asset(
            "https://example.com/clip.mp4",
            extra_fields={
                "start_datetime": "2026-11-14T10:10:00Z",
                "end_datetime": "2026-11-14T10:10:02Z",
            },
        ),
    )
    # A thumbnail with no temporal metadata of its own.
    item.add_asset("thumbnail", Asset("https://example.com/thumb.png"))
    # An asset whose own start lands after the item's inherited end.
    item.add_asset(
        "late",
        Asset(
            "https://example.com/late.mp4",
            extra_fields={"start_datetime": "2026-11-14T10:31:00Z"},
        ),
    )
    # An asset declaring a single instant but no range.
    item.add_asset(
        "snapshot",
        Asset(
            "https://example.com/snapshot.jpg",
            extra_fields={"datetime": "2026-11-14T10:10:05Z"},
        ),
    )
    # Paired start/end without a datetime field.
    item.add_asset(
        "pair",
        Asset(
            "https://example.com/pair.tif",
            extra_fields={
                "start_datetime": "2026-11-14T10:05:00Z",
                "end_datetime": "2026-11-14T10:06:00Z",
            },
        ),
    )
    # Reversed, contradictory bounds declared on the asset itself.
    item.add_asset(
        "reversed",
        Asset(
            "https://example.com/reversed.tif",
            extra_fields={
                "start_datetime": "2026-11-14T10:20:00Z",
                "end_datetime": "2026-11-14T10:19:00Z",
            },
        ),
    )
    return item


def test_declared_fields_win_and_inheritance_is_recorded() -> None:
    item = _flight_item()

    clip = get_asset_time(item.assets["clip"])
    assert clip.start is not None
    assert clip.end is not None
    assert clip.start.origin is TimeOrigin.DECLARED
    assert clip.end.origin is TimeOrigin.DECLARED
    assert clip.point is None
    assert clip.origins == {
        "start_datetime": TimeOrigin.DECLARED,
        "end_datetime": TimeOrigin.DECLARED,
    }

    thumbnail = get_asset_time(item.assets["thumbnail"])
    assert thumbnail.start is not None
    assert thumbnail.end is not None
    assert thumbnail.start.origin is TimeOrigin.INHERITED
    assert thumbnail.end.origin is TimeOrigin.INHERITED
    assert thumbnail.start.value == _utc(10, 0)
    assert thumbnail.end.value == _utc(10, 30)
    assert thumbnail.origins == {
        "start_datetime": TimeOrigin.INHERITED,
        "end_datetime": TimeOrigin.INHERITED,
    }


def test_asset_time_property_matches_helper() -> None:
    item = _flight_item()
    assert item.assets["clip"].time.asset is item.assets["clip"]
    assert item.get_asset_times()["clip"].interval == get_asset_time(
        item.assets["clip"]
    ).interval


def test_interval_uses_actual_asset_time_not_item_coverage() -> None:
    item = _flight_item()
    clip = get_asset_time(item.assets["clip"])
    assert clip.interval == (_utc(10, 10, 0), _utc(10, 10, 2))


def test_paired_start_end_retained_without_datetime() -> None:
    item = _flight_item()
    pair = get_asset_time(item.assets["pair"])
    assert pair.point is None
    assert pair.interval == (_utc(10, 5), _utc(10, 6))
    assert not pair.has_conflict


def test_declared_start_after_inherited_end_is_conflict() -> None:
    item = _flight_item()
    late = get_asset_time(item.assets["late"])
    assert late.has_conflict
    assert len(late.conflicts) == 1
    conflict = late.conflicts[0]
    assert conflict.code == "declared_start_after_inherited_end"
    assert late.start is not None
    assert late.end is not None
    assert late.start.origin is TimeOrigin.DECLARED
    assert late.end.origin is TimeOrigin.INHERITED
    # No interval is fabricated and the bounds are not swapped.
    assert late.interval is None
    assert late.start.value == _utc(10, 31)
    assert late.end.value == _utc(10, 30)


def test_reversed_declared_bounds_are_not_swapped() -> None:
    item = _flight_item()
    reversed_asset = get_asset_time(item.assets["reversed"])
    assert reversed_asset.has_conflict
    assert reversed_asset.conflicts[0].code == "start_after_end"
    assert reversed_asset.interval is None
    assert reversed_asset.start is not None
    assert reversed_asset.end is not None
    assert reversed_asset.start.value == _utc(10, 20)
    assert reversed_asset.end.value == _utc(10, 19)


def test_definite_hit_requires_asset_declared_time() -> None:
    item = _flight_item()

    # Inside the two-second clip: definite, because the clip declared the range.
    result = item.search_assets_at(_utc(10, 10, 1))
    assert set(result.definite) == {"clip"}
    assert "clip" not in result.possible

    # The thumbnail only inherits the item's half-hour range, so it can never be
    # a definite hit.
    assert "thumbnail" not in result.definite
    assert "thumbnail" in result.possible


def test_short_asset_not_returned_for_unrelated_instant() -> None:
    item = _flight_item()
    result = item.search_assets_at(_utc(10, 15))
    assert "clip" not in result.definite
    assert "clip" not in result.possible
    assert "thumbnail" in result.possible


def test_declared_point_is_definite_only_at_that_instant() -> None:
    item = _flight_item()

    hit = item.search_assets_at(_utc(10, 10, 5))
    assert set(hit.definite) == {"snapshot"}

    miss = item.search_assets_at(_utc(10, 10, 6))
    assert "snapshot" not in miss.definite
    assert "snapshot" not in miss.possible


def test_inherited_range_is_possible_but_never_definite() -> None:
    item = _flight_item()
    result = item.search_assets_at(_utc(10, 0))
    assert result.definite == {}
    assert "thumbnail" in result.possible
    result_at_end = item.search_assets_at(_utc(10, 30))
    assert "thumbnail" in result_at_end.possible


def test_asset_without_any_time_is_always_possible_never_definite() -> None:
    # An ownerless asset with no temporal fields carries no time information at
    # all, so every instant remains possible but none can be definite.
    asset = Asset("https://example.com/data.bin")
    resolved = get_asset_time(asset)
    assert resolved.point is None
    assert resolved.start is None
    assert resolved.end is None
    assert not resolved.definitely_contains(_utc(23, 59))
    assert resolved.possibly_contains(_utc(23, 59))
    assert resolved.possibly_contains(_utc(0, 0))


def test_conflicting_asset_falls_back_to_its_own_claim_for_possible() -> None:
    item = _flight_item()
    # At the asset's own declared start the instant is still possible, but never
    # definite because the declared/inherited bounds contradict each other.
    at_start = item.search_assets_at(_utc(10, 31))
    assert "late" not in at_start.definite
    assert "late" in at_start.possible

    # Before the declared start the asset itself rules the instant out.
    before = item.search_assets_at(_utc(10, 29))
    assert "late" not in before.definite
    assert "late" not in before.possible


def test_declared_point_is_not_widened_by_inherited_range() -> None:
    item = Item(
        id="point",
        geometry=None,
        bbox=None,
        datetime=None,
        properties={
            "start_datetime": "2026-11-14T10:00:00Z",
            "end_datetime": "2026-11-14T10:30:00Z",
        },
    )
    item.add_asset(
        "shot",
        Asset(
            "https://example.com/shot.jpg",
            extra_fields={"datetime": "2026-11-14T10:10:05Z"},
        ),
    )
    shot = get_asset_time(item.assets["shot"])
    # The asset's own point claim is definite at that instant...
    assert shot.definitely_contains(_utc(10, 10, 5))
    # ...and neither the point nor the inherited half-hour range make any other
    # instant possible.
    assert not shot.definitely_contains(_utc(10, 10, 6))
    assert not shot.possibly_contains(_utc(10, 10, 6))
    assert not shot.possibly_contains(_utc(10, 20))
    assert shot.possibly_contains(_utc(10, 10, 5))


def test_naive_instant_is_treated_as_utc() -> None:
    item = _flight_item()
    naive = datetime(2026, 11, 14, 10, 10, 1)
    result = item.search_assets_at(naive)
    assert set(result.definite) == {"clip"}


def test_to_dict_export_for_cleaning() -> None:
    item = _flight_item()
    exported = get_asset_time(item.assets["clip"]).to_dict()
    assert exported == {
        "href": "https://example.com/clip.mp4",
        "datetime": None,
        "start_datetime": "2026-11-14T10:10:00Z",
        "end_datetime": "2026-11-14T10:10:02Z",
        "origins": {
            "start_datetime": "declared",
            "end_datetime": "declared",
        },
        "interval": [
            "2026-11-14T10:10:00Z",
            "2026-11-14T10:10:02Z",
        ],
        "conflicts": [],
    }

    conflicted = get_asset_time(item.assets["late"]).to_dict()
    assert conflicted["interval"] is None
    assert conflicted["origins"] == {
        "start_datetime": "declared",
        "end_datetime": "inherited",
    }
    assert conflicted["conflicts"][0]["code"] == (
        "declared_start_after_inherited_end"
    )


def test_asset_without_owner_only_uses_declared_time() -> None:
    asset = Asset(
        "https://example.com/orphan.tif",
        extra_fields={"datetime": "2026-11-14T10:10:05Z"},
    )
    resolved = get_asset_time(asset)
    assert resolved.point is not None
    assert resolved.point.origin is TimeOrigin.DECLARED
    assert resolved.start is None
    assert resolved.end is None
    assert resolved.definitely_contains(_utc(10, 10, 5))
    assert not resolved.possibly_contains(_utc(10, 10, 6))


def test_inherited_datetime_point_on_otherwise_untimed_asset() -> None:
    item = Item(
        id="point-item",
        geometry=None,
        bbox=None,
        datetime=_utc(10, 10, 5),
        properties={},
    )
    item.add_asset("thumb", Asset("https://example.com/thumb.png"))
    thumb = get_asset_time(item.assets["thumb"])
    assert thumb.point is not None
    assert thumb.point.origin is TimeOrigin.INHERITED
    assert not thumb.definitely_contains(_utc(10, 10, 5))
    assert thumb.possibly_contains(_utc(10, 10, 5))
    assert not thumb.possibly_contains(_utc(10, 10, 6))


def test_public_api_exports() -> None:
    for name in (
        "AssetTime",
        "AssetSearchResult",
        "TimeConflict",
        "TimeOrigin",
        "TimeValue",
        "get_asset_time",
        "get_asset_times",
        "search_assets",
    ):
        assert hasattr(pystac, name)


@pytest.mark.parametrize(
    "asset_key",
    ["clip", "thumbnail", "late", "snapshot", "pair", "reversed"],
)
def test_every_asset_resolves(asset_key: str) -> None:
    item = _flight_item()
    resolved = item.get_asset_times()[asset_key]
    assert resolved.asset is item.assets[asset_key]


def test_naive_and_aware_datetimes_do_not_raise_on_conflict() -> None:
    item = Item(
        id="mixed-tz",
        geometry=None,
        bbox=None,
        datetime=None,
        properties={
            "start_datetime": "2026-11-14T10:00:00Z",
            "end_datetime": "2026-11-14T10:30:00Z",
        },
    )
    # Naive (timezone-less) declared start after the aware inherited end.
    item.add_asset(
        "naive-late",
        Asset(
            "https://example.com/naive-late.mp4",
            extra_fields={"start_datetime": "2026-11-14T10:31:00"},
        ),
    )
    resolved = get_asset_time(item.assets["naive-late"])
    assert resolved.has_conflict
    assert resolved.conflicts[0].code == "declared_start_after_inherited_end"
    assert resolved.interval is None
