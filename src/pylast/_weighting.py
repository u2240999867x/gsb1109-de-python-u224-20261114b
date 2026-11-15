#
# pylast -
#     A Python interface to Last.fm and Libre.fm
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Roll artist top-tag weights up into theme contributions.

Artist top tags are *relative* weights and providers often return only the
first few tags, so the known weights must never be force-normalised to 100%.
Each :class:`SourceReport` therefore declares its weight basis (normalized
fractions versus raw relative weights), the known tag weights and, optionally,
an interval covering the weight of the tags that were not returned.

Tags may be mapped to several themes at once with explicit allocation ratios.
Tag aliases are merged on a case-insensitive canonical key so a single play
weight is never counted twice. Sources without a normalized conversion basis
only contribute to the comparable subset, while their theme contributions are
still reported separately.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from types import MappingProxyType

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from typing import Any

DEFAULT_EPSILON = 1e-9


class WeightBasis(enum.Enum):
    """How a source's known tag weights must be interpreted."""

    NORMALIZED = "normalized"
    """Fractions of one play, summing to at most one across all tags."""

    RELATIVE = "relative"
    """Raw relative weights with no common one-play conversion."""


@dataclass(frozen=True, slots=True)
class TagWeight:
    """A tag name and the weight the provider assigned to it."""

    tag: str
    weight: float


@dataclass(frozen=True, slots=True)
class UnreturnedRange:
    """Inclusive interval for the weight of tags the provider did not return."""

    low: float
    high: float


@dataclass(frozen=True, slots=True)
class ThemeSplit:
    """Allocation of a tag to a theme, weighted by ``ratio`` (0 < ratio <= 1)."""

    theme: str
    ratio: float


@dataclass(frozen=True, slots=True)
class UnassignedShare:
    """Play weight from one source that is not attributed to any theme.

    When ``bounded`` is true, ``low``/``high`` are a provider-bounded
    interval and ``point`` is its midpoint. Otherwise the three values are
    the same point estimate without a guaranteed interval.
    """

    source: str
    low: float
    point: float
    high: float
    bounded: bool


@dataclass(frozen=True, slots=True)
class SourceReport:
    """One provider's tag weights plus its theme mapping for one version.

    Only one ``tag_themes`` mapping version belongs to a report. Build a
    separate report for another version rather than merging two mappings,
    otherwise the same play weight would be allocated twice.
    """

    source: str
    basis: WeightBasis
    known: Sequence[TagWeight]
    unreturned: UnreturnedRange | None = None
    tag_themes: Mapping[str, Sequence[ThemeSplit]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "known", tuple(self.known))
        mapping: dict[str, tuple[ThemeSplit, ...]] = {}
        for tag, splits in self.tag_themes.items():
            key = _canonical(tag)
            if key in mapping:
                raise ValueError(
                    f"duplicate theme mapping for tag {tag!r} after canonicalization"
                )
            mapping[key] = tuple(splits)
        object.__setattr__(self, "tag_themes", MappingProxyType(mapping))
        self._validate()

    @classmethod
    def from_top_items(
        cls,
        source: str,
        basis: WeightBasis,
        top_items: Iterable[Any],
        unreturned: UnreturnedRange | None = None,
        tag_themes: Mapping[str, Sequence[ThemeSplit]] | None = None,
    ) -> SourceReport:
        """Build a report from pylast ``TopItem`` objects (duck-typed).

        Each item needs a ``weight`` attribute and an ``item`` exposing
        ``get_name()``, mirroring ``Artist.get_top_tags()`` output.
        """

        known = tuple(
            TagWeight(top_item.item.get_name(), float(top_item.weight))
            for top_item in top_items
        )
        return cls(source, basis, known, unreturned, tag_themes or {})

    def _validate(self, epsilon: float = DEFAULT_EPSILON) -> None:
        if not self.source:
            raise ValueError("source must be a non-empty name")
        if not isinstance(self.basis, WeightBasis):
            raise TypeError("basis must be a WeightBasis member")

        known_keys: set[str] = set()
        for tag_weight in self.known:
            if not tag_weight.tag or not tag_weight.tag.strip():
                raise ValueError("tag names must be non-empty")
            if not math.isfinite(tag_weight.weight) or tag_weight.weight < 0:
                raise ValueError(f"weight for {tag_weight.tag!r} must be >= 0")
            known_keys.add(_canonical(tag_weight.tag))

        if self.basis is WeightBasis.NORMALIZED:
            known_total = math.fsum(tw.weight for tw in self.known)
            if known_total > 1.0 + epsilon:
                raise ValueError(
                    f"normalized known weights sum to {known_total}, exceeding 1"
                )

        if self.unreturned is not None:
            low = self.unreturned.low
            high = self.unreturned.high
            if not math.isfinite(low) or not math.isfinite(high) or low < 0:
                raise ValueError("unreturned range bounds must be finite and >= 0")
            if high < low:
                raise ValueError("unreturned high must be >= low")

        for tag, splits in self.tag_themes.items():
            if tag not in known_keys:
                raise ValueError(f"theme mapping references unknown tag {tag!r}")
            if not splits:
                raise ValueError(f"theme mapping for {tag!r} must not be empty")
            ratio_total = 0.0
            for split in splits:
                if not split.theme:
                    raise ValueError("theme names must be non-empty")
                if not math.isfinite(split.ratio) or not 0.0 < split.ratio <= 1.0:
                    raise ValueError(
                        f"ratio for theme {split.theme!r} must be in (0, 1]"
                    )
                ratio_total += split.ratio
            if ratio_total > 1.0 + epsilon:
                raise ValueError(
                    f"theme ratios for {tag!r} sum to {ratio_total}, exceeding 1"
                )


@dataclass(frozen=True, slots=True)
class WeightRollup:
    """Result of rolling one or more :class:`SourceReport` objects up."""

    contributions: Mapping[str, float]
    unassigned: Mapping[str, UnassignedShare]
    comparable_total: Mapping[str, float]

    @property
    def assigned_total(self) -> float:
        """Total play weight attributed to themes across every source."""

        return math.fsum(self.contributions.values())


class ThemeWeightRollup:
    """Accumulates source reports into theme contributions and unassigned shares."""

    def __init__(self) -> None:
        self._contributions: dict[str, float] = {}
        self._unassigned: dict[str, UnassignedShare] = {}
        self._comparable: dict[str, float] = {}

    def add_source(self, report: SourceReport) -> None:
        if report.source in self._unassigned:
            raise ValueError(f"source {report.source!r} was already added")

        combined = self._combine_aliases(report)
        known_total = math.fsum(combined.values())
        unassigned_known = self._allocate_themes(report, combined)
        share = self._unassigned_share(report, unassigned_known, known_total)

        self._unassigned[report.source] = share
        if report.basis is WeightBasis.NORMALIZED:
            self._comparable[report.source] = known_total

    def build(self) -> WeightRollup:
        return WeightRollup(
            contributions=MappingProxyType(dict(self._contributions)),
            unassigned=MappingProxyType(dict(self._unassigned)),
            comparable_total=MappingProxyType(dict(self._comparable)),
        )

    def _combine_aliases(self, report: SourceReport) -> dict[str, float]:
        combined: dict[str, float] = {}
        for tag_weight in report.known:
            key = _canonical(tag_weight.tag)
            combined[key] = combined.get(key, 0.0) + tag_weight.weight
        return combined

    def _allocate_themes(
        self, report: SourceReport, combined: Mapping[str, float]
    ) -> float:
        unassigned_known = 0.0
        for key, weight in combined.items():
            splits = report.tag_themes.get(key)
            if not splits:
                unassigned_known += weight
                continue

            ratio_total = math.fsum(split.ratio for split in splits)
            for split in splits:
                allocated = weight * split.ratio
                self._contributions[split.theme] = (
                    self._contributions.get(split.theme, 0.0) + allocated
                )
            unassigned_known += weight * (1.0 - ratio_total)
        return unassigned_known

    def _unassigned_share(
        self, report: SourceReport, unassigned_known: float, known_total: float
    ) -> UnassignedShare:
        unreturned = report.unreturned

        if unreturned is not None:
            low = unreturned.low + unassigned_known
            high = unreturned.high + unassigned_known
            point = (unreturned.low + unreturned.high) / 2.0 + unassigned_known
            return UnassignedShare(report.source, low, point, high, bounded=True)

        if report.basis is WeightBasis.NORMALIZED:
            residual = max(0.0, 1.0 - known_total) + unassigned_known
        else:
            residual = unassigned_known
        return UnassignedShare(
            report.source, residual, residual, residual, bounded=False
        )


def roll_up_theme_weights(reports: Iterable[SourceReport]) -> WeightRollup:
    """Roll several source reports up in a single call."""

    rollup = ThemeWeightRollup()
    for report in reports:
        rollup.add_source(report)
    return rollup.build()


def _canonical(tag: str) -> str:
    """Case- and whitespace-insensitive key used to merge tag aliases."""

    return tag.casefold().strip()
