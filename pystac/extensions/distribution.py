"""Mergeable pixel-distribution summaries driven by STAC raster band descriptions.

The :stac-ext:`Raster Extension <raster>` describes how the raw digital numbers
stored in an asset map to physical values (``nodata``, ``scale``, ``offset`` and
``unit``). This module turns small blocks of raw pixels plus those descriptions
into binned distribution summaries that can be merged across assets or epochs.

The workflow for every band is:

1. Identify nodata pixels in the *raw* code space, before any scaling.
2. Transform valid codes with ``value = raw * scale + offset``.
3. Convert the transformed value to the common unit requested by the caller.
4. Count it against the caller-supplied bin edges.

Bands whose unit has no conversion convention to the requested unit are reported
separately instead of being force-merged into a meaningless mean.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Any, Union

from pystac.extensions.raster import Histogram, RasterBand, RasterExtension

__all__ = [
    "BinRule",
    "DistributionSummary",
    "IncompatibleSummaryError",
    "SkippedBand",
    "UnitConversionError",
    "build_distribution",
    "band_from_asset",
    "classify_pixels",
    "merge_summaries",
]

# Temperature units are the only family for which this module ships conversion
# conventions. Bands measured in anything else are listed separately.
_UNIT_ALIASES: dict[str, str] = {
    "celsius": "celsius",
    "°c": "celsius",
    "degree celsius": "celsius",
    "degrees celsius": "celsius",
    "deg c": "celsius",
    "kelvin": "kelvin",
    "k": "kelvin",
    "degree kelvin": "kelvin",
    "degrees kelvin": "kelvin",
    "deg k": "kelvin",
    "fahrenheit": "fahrenheit",
    "°f": "fahrenheit",
    "degree fahrenheit": "fahrenheit",
    "degrees fahrenheit": "fahrenheit",
    "deg f": "fahrenheit",
}

# Known conversions pivot through degrees Celsius.
_TEMPERATURE_UNITS = frozenset({"celsius", "kelvin", "fahrenheit"})


class UnitConversionError(ValueError):
    """Raised when a band unit cannot be converted to the requested unit."""


class IncompatibleSummaryError(ValueError):
    """Raised when summaries with different binning cannot be merged."""


@dataclass(frozen=True)
class BinRule:
    """Common unit and bin edges used to make summaries mergeable.

    Args:
        name: Identifier of the rule; summaries sharing it may be merged.
        unit: Common unit all values are converted to before counting.
        edges: Monotonically increasing bin edges. ``n`` edges define
            ``n - 1`` bins.
        inclusive: Which boundary a value exactly on an edge belongs to.
            ``"left"`` (the default) gives half-open bins
            ``[edges[i], edges[i+1])``; ``"right"`` gives
            ``(edges[i], edges[i+1]]``.
    """

    name: str
    unit: str
    edges: tuple[float, ...]
    inclusive: str = "left"

    def __init__(
        self,
        name: str,
        unit: str,
        edges: Sequence[float],
        inclusive: str = "left",
    ) -> None:
        if inclusive not in ("left", "right"):
            raise ValueError("inclusive must be 'left' or 'right'")
        normalized_edges = tuple(float(edge) for edge in edges)
        if len(normalized_edges) < 2:
            raise ValueError("at least two bin edges are required")
        if any(
            normalized_edges[i] >= normalized_edges[i + 1]
            for i in range(len(normalized_edges) - 1)
        ):
            raise ValueError("bin edges must be strictly increasing")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "unit", _normalize_unit(unit))
        object.__setattr__(self, "edges", normalized_edges)
        object.__setattr__(self, "inclusive", inclusive)

    @property
    def bin_count(self) -> int:
        return len(self.edges) - 1


@dataclass(frozen=True)
class SkippedBand:
    """A band that could not be counted under the requested :class:`BinRule`."""

    band_key: str | None
    unit: str | None
    reason: str


@dataclass
class DistributionSummary:
    """Binned pixel counts for one band or several merged bands.

    Counts always reconcile: ``total == nodata + valid`` and
    ``valid == underflow + sum(buckets) + overflow``.
    """

    rule: BinRule
    unit: str
    source_units: tuple[str, ...]
    band_keys: tuple[str | None, ...]
    total: int
    nodata: int
    underflow: int
    overflow: int
    buckets: list[int]
    zeros: int
    valid_min: float | None = None
    valid_max: float | None = None

    @property
    def valid(self) -> int:
        return self.underflow + sum(self.buckets) + self.overflow

    def to_histogram(self) -> Histogram:
        """Return the in-range buckets as a raster-extension :class:`Histogram`."""
        return Histogram.create(
            count=len(self.buckets),
            min=self.rule.edges[0],
            max=self.rule.edges[-1],
            buckets=list(self.buckets),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation of this summary."""
        return {
            "rule": {
                "name": self.rule.name,
                "unit": self.unit,
                "edges": list(self.rule.edges),
                "inclusive": self.rule.inclusive,
            },
            "unit": self.unit,
            "source_units": list(self.source_units),
            "band_keys": list(self.band_keys),
            "total": self.total,
            "nodata": self.nodata,
            "valid": self.valid,
            "underflow": self.underflow,
            "overflow": self.overflow,
            "buckets": list(self.buckets),
            "zeros": self.zeros,
            "valid_min": self.valid_min,
            "valid_max": self.valid_max,
        }


def _normalize_unit(unit: str | None) -> str:
    if unit is None:
        raise UnitConversionError("band declares no unit")
    normalized = _UNIT_ALIASES.get(unit.strip().lower())
    if normalized is None:
        raise UnitConversionError(f"no conversion convention for unit {unit!r}")
    return normalized


def _to_celsius(value: float, unit: str) -> float:
    if unit == "celsius":
        return value
    if unit == "kelvin":
        return value - 273.15
    return (value - 32.0) * 5.0 / 9.0


def _from_celsius(value: float, unit: str) -> float:
    if unit == "celsius":
        return value
    if unit == "kelvin":
        return value + 273.15
    return value * 9.0 / 5.0 + 32.0


def _convert_temperature(value: float, source_unit: str, target_unit: str) -> float:
    if source_unit == target_unit:
        return value
    if source_unit not in _TEMPERATURE_UNITS or target_unit not in _TEMPERATURE_UNITS:
        raise UnitConversionError(
            f"cannot convert {source_unit!r} to {target_unit!r}"
        )
    return _from_celsius(_to_celsius(value, source_unit), target_unit)


def _band_properties(band: RasterBand | dict[str, Any]) -> dict[str, Any]:
    if isinstance(band, RasterBand):
        return band.properties
    if isinstance(band, dict):
        return band
    raise TypeError("band must be a RasterBand or a raster:bands dict")


def band_from_asset(asset: Any, index: int = 0) -> RasterBand:
    """Extract a :class:`RasterBand` from a STAC asset's ``raster:bands``."""
    bands = RasterExtension.ext(asset).bands
    if bands is None or not bands:
        raise ValueError("asset has no raster:bands entries")
    try:
        return bands[index]
    except IndexError as exc:
        raise IndexError(
            f"asset only has {len(bands)} band(s), index {index} is out of range"
        ) from exc


def _is_nodata(raw: float, nodata: Any) -> bool:
    if isinstance(nodata, str):
        token = nodata.strip().lower()
        if token == "nan":
            return math.isnan(raw)
        if token == "inf":
            return math.isinf(raw) and raw > 0
        if token == "-inf":
            return math.isinf(raw) and raw < 0
        return False
    if nodata is None:
        return False
    if math.isnan(raw):
        return False
    return raw == nodata


def _flatten_pixels(pixels: Iterable[Any]) -> list[float]:
    flattened: list[float] = []
    stack: list[Any] = [list(pixels)]
    while stack:
        item = stack.pop()
        if isinstance(item, Real) and not isinstance(item, bool):
            flattened.append(float(item))
        elif isinstance(item, Iterable):
            stack.extend(item)
        else:
            raise TypeError(f"pixel values must be numeric, got {type(item)!r}")
    return flattened


def classify_pixels(
    band: RasterBand | dict[str, Any],
    pixels: Iterable[Any],
    rule: BinRule,
    band_key: str | None = None,
) -> DistributionSummary:
    """Classify a block of raw pixels under a band description and bin rule.

    Nodata is matched against the raw digital numbers first; surviving codes are
    transformed with ``raw * scale + offset`` and converted to ``rule.unit``
    before binning.
    """
    properties = _band_properties(band)
    source_unit = _normalize_unit(properties.get("unit"))

    scale_value = properties.get("scale")
    offset_value = properties.get("offset")
    scale = 1.0 if scale_value is None else float(scale_value)
    offset = 0.0 if offset_value is None else float(offset_value)
    nodata = properties.get("nodata")

    raw_values = _flatten_pixels(pixels)
    buckets = [0] * rule.bin_count
    nodata_count = 0
    underflow = 0
    overflow = 0
    zeros = 0
    valid_min: float | None = None
    valid_max: float | None = None

    for raw in raw_values:
        if math.isnan(raw) or _is_nodata(raw, nodata):
            nodata_count += 1
            continue
        value = raw * scale + offset
        value = _convert_temperature(value, source_unit, rule.unit)

        if value == 0.0:
            zeros += 1
        if valid_min is None or value < valid_min:
            valid_min = value
        if valid_max is None or value > valid_max:
            valid_max = value

        if value < rule.edges[0]:
            underflow += 1
            continue
        if value > rule.edges[-1]:
            overflow += 1
            continue
        if rule.inclusive == "left":
            index = bisect.bisect_right(rule.edges, value) - 1
        else:
            index = bisect.bisect_left(rule.edges, value) - 1
        if index < 0:
            underflow += 1
        elif index >= rule.bin_count:
            overflow += 1
        else:
            buckets[index] += 1

    summary = DistributionSummary(
        rule=rule,
        unit=rule.unit,
        source_units=(source_unit,),
        band_keys=(band_key,),
        total=len(raw_values),
        nodata=nodata_count,
        underflow=underflow,
        overflow=overflow,
        buckets=buckets,
        zeros=zeros,
        valid_min=valid_min,
        valid_max=valid_max,
    )
    _assert_totals(summary)
    return summary


def _assert_totals(summary: DistributionSummary) -> None:
    if summary.nodata + summary.valid != summary.total:
        raise AssertionError(
            f"counts do not reconcile: {summary.nodata} nodata + "
            f"{summary.valid} valid != {summary.total} total"
        )


def merge_summaries(summaries: Sequence[DistributionSummary]) -> DistributionSummary:
    """Merge summaries produced under the same :class:`BinRule`.

    Raises:
        IncompatibleSummaryError: If the summaries use different bin edges,
            inclusivity, rule names or target units.
    """
    if not summaries:
        raise ValueError("at least one summary is required to merge")

    first = summaries[0]
    for summary in summaries[1:]:
        if (
            summary.rule.name != first.rule.name
            or summary.rule.edges != first.rule.edges
            or summary.rule.inclusive != first.rule.inclusive
            or summary.unit != first.unit
        ):
            raise IncompatibleSummaryError(
                "summaries must share rule name, unit, edges and inclusivity"
            )

    merged = DistributionSummary(
        rule=first.rule,
        unit=first.unit,
        source_units=tuple(
            unit
            for summary in summaries
            for unit in summary.source_units
        ),
        band_keys=tuple(
            key for summary in summaries for key in summary.band_keys
        ),
        total=sum(summary.total for summary in summaries),
        nodata=sum(summary.nodata for summary in summaries),
        underflow=sum(summary.underflow for summary in summaries),
        overflow=sum(summary.overflow for summary in summaries),
        buckets=[
            sum(summary.buckets[i] for summary in summaries)
            for i in range(first.rule.bin_count)
        ],
        zeros=sum(summary.zeros for summary in summaries),
        valid_min=_min_opt(summary.valid_min for summary in summaries),
        valid_max=_max_opt(summary.valid_max for summary in summaries),
    )
    _assert_totals(merged)
    return merged


def _min_opt(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _max_opt(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def build_distribution(
    band_descriptions: Sequence[
        tuple[Union[RasterBand, dict[str, Any]], Iterable[Any]]
        | tuple[str, Union[RasterBand, dict[str, Any]], Iterable[Any]]
    ],
    rule: BinRule,
) -> tuple[DistributionSummary | None, list[SkippedBand]]:
    """Classify several bands under one rule and merge everything mergeable.

    Each record is either ``(band, pixels)`` or ``(band_key, band, pixels)``.
    Bands whose unit has no conversion convention to ``rule.unit`` are returned
    as :class:`SkippedBand` entries rather than forced into the merged summary.
    """
    summaries: list[DistributionSummary] = []
    skipped: list[SkippedBand] = []

    for record in band_descriptions:
        if len(record) == 3:
            band_key, band, pixels = record
        elif len(record) == 2:
            band_key = None
            band, pixels = record
        else:
            raise ValueError(
                "each record must be (band, pixels) or (band_key, band, pixels)"
            )
        properties = _band_properties(band)
        try:
            summary = classify_pixels(band, pixels, rule, band_key)
        except UnitConversionError as exc:
            skipped.append(
                SkippedBand(
                    band_key=band_key,
                    unit=properties.get("unit"),
                    reason=str(exc),
                )
            )
            continue
        summaries.append(summary)

    merged = merge_summaries(summaries) if summaries else None
    return merged, skipped
