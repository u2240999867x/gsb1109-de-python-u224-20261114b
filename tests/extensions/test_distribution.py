import pytest

import pystac
from pystac.extensions.distribution import (
    BinRule,
    IncompatibleSummaryError,
    UnitConversionError,
    band_from_asset,
    build_distribution,
    classify_pixels,
    merge_summaries,
)
from pystac.extensions.raster import RasterBand, RasterExtension

# Common rule requested by the caller: everything is expressed in degrees
# Celsius and counted on the same edges so epochs can be merged.
EDGES = (-20.0, -10.0, 0.0, 10.0, 20.0, 30.0)
RULE = BinRule("temp-celsius", "celsius", EDGES)

# Epoch 1: temperature already stored in degrees Celsius, identity scaling.
CELSIUS_BAND = {
    "unit": "celsius",
    "scale": 1,
    "offset": 0,
    "nodata": -9999,
    "data_type": "int16",
}

# Epoch 2: integer codes, negative scale and offset. A larger raw code means a
# colder temperature, so the raw extrema swap physical roles after scaling.
KELVIN_BAND = {
    "unit": "kelvin",
    "scale": -0.1,
    "offset": 303.15,
    "nodata": -9999,
    "data_type": "int16",
}

# Epoch 1 block: nodata, an underflow, values on several edges, a valid zero,
# and an overflow (30 lands on the last edge and is excluded on the left).
CELSIUS_PIXELS = [-9999, -25, -15, -10, 0, 5, 10, 25, 30, 35]

# Epoch 2 block, chosen so scaled/converted values land on the same edges:
# raw 500 -> -20 C, raw 400 -> -10 C, raw 300 -> 0 C, raw 200 -> 10 C,
# raw 100 -> 20 C, raw 0 -> 30 C; raw 550 underflows and raw -50 overflows.
KELVIN_PIXELS = [-9999, 550, 500, 400, 300, 200, 100, 50, 0, -50]


def test_celsius_band_classification_and_totals() -> None:
    summary = classify_pixels(CELSIUS_BAND, CELSIUS_PIXELS, RULE, "epoch-1")

    assert summary.total == 10
    assert summary.nodata == 1
    assert summary.valid == 9
    assert summary.nodata + summary.valid == summary.total
    assert summary.underflow == 1
    assert summary.overflow == 2
    assert summary.zeros == 1
    assert summary.buckets == [1, 1, 2, 1, 1]
    assert summary.valid_min == -25.0
    assert summary.valid_max == 35.0
    assert summary.band_keys == ("epoch-1",)
    assert summary.source_units == ("celsius",)


def test_negative_scale_swaps_raw_extrema() -> None:
    band = RasterBand(KELVIN_BAND)
    summary = classify_pixels(band, KELVIN_PIXELS, RULE, "epoch-2")

    valid_raw = [raw for raw in KELVIN_PIXELS if raw != -9999]
    raw_min, raw_max = min(valid_raw), max(valid_raw)
    # The largest raw code is the coldest pixel and vice versa.
    assert raw_min * -0.1 + 303.15 > raw_max * -0.1 + 303.15
    assert summary.valid_min == -25.0
    assert summary.valid_max == 35.0

    assert summary.total == 10
    assert summary.nodata == 1
    assert summary.underflow == 1
    assert summary.overflow == 2
    assert summary.zeros == 1
    assert summary.buckets == [1, 1, 1, 1, 2]
    assert summary.source_units == ("kelvin",)


def test_nodata_is_detected_in_raw_code_space() -> None:
    # The nodata code -9999 must never be scaled; with the negative scale it
    # would otherwise look like a plausible hot pixel (~1203 C).
    summary = classify_pixels(KELVIN_BAND, [-9999, -9999, 300], RULE)
    assert summary.nodata == 2
    assert summary.valid == 1
    assert summary.buckets == [0, 0, 1, 0, 0]
    assert summary.zeros == 1


def test_string_nodata_nan() -> None:
    band = {"unit": "celsius", "nodata": "nan", "data_type": "float32"}
    summary = classify_pixels(
        band, [float("nan"), float("nan"), 0.0, 5.0], RULE
    )
    assert summary.nodata == 2
    assert summary.valid == 2
    assert summary.zeros == 1


def test_edge_assignment_respects_inclusive_side() -> None:
    left_rule = BinRule("edge", "celsius", EDGES, inclusive="left")
    right_rule = BinRule("edge", "celsius", EDGES, inclusive="right")
    pixels = [-10]

    left_summary = classify_pixels(CELSIUS_BAND, pixels, left_rule)
    right_summary = classify_pixels(CELSIUS_BAND, pixels, right_rule)

    # Left-inclusive: -10 opens the [-10, 0) bin; right-inclusive it closes
    # the (-20, -10] bin.
    assert left_summary.buckets == [0, 1, 0, 0, 0]
    assert right_summary.buckets == [1, 0, 0, 0, 0]

    # First and last edges stay excluded/included consistently at the tails.
    right_tail = classify_pixels(CELSIUS_BAND, [-20, 30], right_rule)
    assert right_tail.underflow == 1
    assert right_tail.overflow == 0
    assert right_tail.buckets[-1] == 1
    left_tail = classify_pixels(CELSIUS_BAND, [-20, 30], left_rule)
    assert left_tail.buckets[0] == 1
    assert left_tail.overflow == 1


def test_valid_zero_is_counted_separately_from_nodata() -> None:
    # Nodata code and a genuine physical zero coexist in one block.
    summary = classify_pixels(
        KELVIN_BAND, [-9999, 300, 300, 400], RULE
    )
    assert summary.nodata == 1
    assert summary.zeros == 2
    assert summary.total == 4
    assert summary.nodata + summary.valid == summary.total


def test_merged_epochs_reconcile() -> None:
    first = classify_pixels(CELSIUS_BAND, CELSIUS_PIXELS, RULE, "epoch-1")
    second = classify_pixels(KELVIN_BAND, KELVIN_PIXELS, RULE, "epoch-2")
    merged = merge_summaries([first, second])

    assert merged.total == 20
    assert merged.nodata == 2
    assert merged.valid == 18
    assert merged.underflow == 2
    assert merged.overflow == 4
    assert merged.zeros == 2
    assert merged.buckets == [2, 2, 3, 2, 3]
    assert merged.band_keys == ("epoch-1", "epoch-2")
    assert merged.source_units == ("celsius", "kelvin")
    assert merged.valid_min == -25.0
    assert merged.valid_max == 35.0

    histogram = merged.to_histogram()
    assert histogram.count == 5
    assert histogram.min == EDGES[0]
    assert histogram.max == EDGES[-1]
    assert histogram.buckets == merged.buckets

    payload = merged.to_dict()
    assert payload["total"] == 20
    assert "mean" not in payload


def test_merge_rejects_incompatible_rules() -> None:
    first = classify_pixels(CELSIUS_BAND, CELSIUS_PIXELS, RULE)
    other_rule = BinRule("temp-celsius", "celsius", (-10.0, 0.0, 10.0))
    second = classify_pixels(CELSIUS_BAND, CELSIUS_PIXELS, other_rule)
    with pytest.raises(IncompatibleSummaryError):
        merge_summaries([first, second])


def test_band_without_convention_is_listed_separately() -> None:
    radiance_band = {
        "unit": "watt/steradian/square_meter/micrometer",
        "scale": 0.001,
        "nodata": -9999,
        "data_type": "int16",
    }
    unitless_band = {"scale": 1, "nodata": 0, "data_type": "uint8"}

    merged, skipped = build_distribution(
        [
            ("epoch-1", CELSIUS_BAND, CELSIUS_PIXELS),
            ("radiance", radiance_band, [1, 2, 3]),
            ("unitless", unitless_band, [0, 1]),
        ],
        RULE,
    )

    assert merged is not None
    assert merged.total == 10
    assert [item.band_key for item in skipped] == ["radiance", "unitless"]
    assert all(item.reason for item in skipped)
    assert {item.unit for item in skipped} == {
        "watt/steradian/square_meter/micrometer",
        None,
    }


def test_build_distribution_returns_none_when_all_skipped() -> None:
    radiance_band = {"unit": "kilometer", "scale": 0.01, "nodata": -9999}
    merged, skipped = build_distribution(
        [("distance", radiance_band, [10, 20])], RULE
    )
    assert merged is None
    assert len(skipped) == 1
    with pytest.raises(UnitConversionError):
        classify_pixels(radiance_band, [10, 20], RULE)


def test_nested_pixel_block_is_flattened() -> None:
    block = [[-9999, -15], [0, [10, 35]]]
    summary = classify_pixels(CELSIUS_BAND, block, RULE)
    assert summary.total == 5
    assert summary.nodata == 1
    assert summary.valid == 4
    assert summary.zeros == 1


def test_band_from_asset_uses_stac_description() -> None:
    asset = pystac.Asset(
        href="https://example.com/epoch-2.tif",
        extra_fields={"raster:bands": [KELVIN_BAND]},
    )
    band = band_from_asset(asset)
    assert isinstance(band, RasterBand)
    assert band.scale == -0.1
    assert band.nodata == -9999

    summary = classify_pixels(band, KELVIN_PIXELS, RULE)
    assert summary.buckets == [1, 1, 1, 1, 2]

    with pytest.raises(IndexError):
        band_from_asset(asset, index=1)


def test_asset_roundtrip_keeps_raster_band_description() -> None:
    asset = pystac.Asset(href="https://example.com/tif")
    RasterExtension.ext(asset).bands = [RasterBand.create(**KELVIN_BAND)]
    recovered = band_from_asset(asset)
    assert recovered.offset == 303.15
    assert recovered.unit == "kelvin"


def test_invalid_bin_rules_are_rejected() -> None:
    with pytest.raises(ValueError):
        BinRule("bad", "celsius", [0.0])
    with pytest.raises(ValueError):
        BinRule("bad", "celsius", [0.0, 0.0, 1.0])
    with pytest.raises(ValueError):
        BinRule("bad", "celsius", EDGES, inclusive="middle")
    with pytest.raises(UnitConversionError):
        BinRule("bad", "furlong", EDGES)
