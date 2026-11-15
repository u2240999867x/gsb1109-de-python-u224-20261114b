from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from pystac.utils import datetime_to_str, str_to_datetime

if TYPE_CHECKING:
    from pystac.asset import Asset
    from pystac.item import Item


class TimeOrigin(str, Enum):
    """Where a temporal value came from.

    ``DECLARED`` values are written on the asset itself (e.g. a ``datetime``,
    ``start_datetime`` or ``end_datetime`` key in the asset object). ``INHERITED``
    values are taken from the owning item because the asset does not declare its
    own value for the field.
    """

    DECLARED = "declared"
    INHERITED = "inherited"


@dataclass(frozen=True)
class TimeValue:
    """A resolved temporal value together with its :class:`TimeOrigin`."""

    value: datetime
    origin: TimeOrigin


@dataclass(frozen=True)
class TimeConflict:
    """A contradiction detected while resolving an asset's temporal metadata.

    The ``code`` is stable and suitable for machine filtering; ``message`` is a
    human-readable explanation. Conflicting bounds are never reordered, so a
    conflict means no valid interval can be derived from the affected fields.
    """

    code: str
    message: str


# A window is represented as ``(lower, upper)``; either bound may be ``None``
# when unbounded.
_Window = tuple[datetime | None, datetime | None]


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _declared_value(asset: Asset, key: str) -> datetime | None:
    raw = asset.extra_fields.get(key)
    if raw is None:
        return None
    return str_to_datetime(raw)


def _owner_item(asset: Asset) -> Item | None:
    owner = asset.owner
    if owner is not None and hasattr(owner, "properties") and hasattr(
        owner, "datetime"
    ):
        return cast("Item", owner)
    return None


def _inherited_value(item: Item | None, key: str) -> datetime | None:
    if item is None:
        return None
    if key == "datetime":
        return item.datetime
    raw = item.properties.get(key)
    if raw is None:
        return None
    return str_to_datetime(raw)


def _resolve(
    asset: Asset, item: Item | None, key: str
) -> TimeValue | None:
    declared = _declared_value(asset, key)
    if declared is not None:
        return TimeValue(declared, TimeOrigin.DECLARED)
    inherited = _inherited_value(item, key)
    if inherited is not None:
        return TimeValue(inherited, TimeOrigin.INHERITED)
    return None


def _max_present(values: list[datetime]) -> datetime | None:
    return max(values) if values else None


def _min_present(values: list[datetime]) -> datetime | None:
    return min(values) if values else None


class AssetTime:
    """The temporal information that actually applies to a single asset.

    The three STAC temporal fields (``datetime``, ``start_datetime`` and
    ``end_datetime``) are resolved independently: a value declared on the
    :class:`~pystac.Asset` wins, otherwise the owning :class:`~pystac.Item`'s
    value is inherited. Each resolved value carries its :class:`TimeOrigin` so
    downstream cleaning can tell asset declarations apart from item inheritance.

    Use :meth:`definitely_contains` and :meth:`possibly_contains` (or
    :func:`search_assets`) to distinguish instants the asset is known to cover
    from instants that cannot be ruled out.
    """

    def __init__(
        self,
        asset: Asset,
        point: TimeValue | None,
        start: TimeValue | None,
        end: TimeValue | None,
        conflicts: tuple[TimeConflict, ...],
    ) -> None:
        self.asset = asset
        self.point = point
        self.start = start
        self.end = end
        self.conflicts = conflicts

        self._definite_window = self._compute_definite_window()
        self._possible_window = self._compute_possible_window()

    @property
    def datetime(self) -> TimeValue | None:
        """The resolved ``datetime`` field, if any."""
        return self.point

    @property
    def has_conflict(self) -> bool:
        """Whether any contradictory temporal fields were detected."""
        return bool(self.conflicts)

    @property
    def origins(self) -> dict[str, TimeOrigin]:
        """Mapping of every resolved field name to its origin."""
        result: dict[str, TimeOrigin] = {}
        if self.point is not None:
            result["datetime"] = self.point.origin
        if self.start is not None:
            result["start_datetime"] = self.start.origin
        if self.end is not None:
            result["end_datetime"] = self.end.origin
        return result

    @property
    def interval(self) -> tuple[datetime, datetime] | None:
        """The usable closed interval ``(start, end)``.

        Returns ``None`` when either bound is missing or the bounds contradict
        each other (start after end). The bounds are never swapped to fabricate
        an interval; the contradiction is reported in :attr:`conflicts` instead.
        A paired ``start_datetime``/``end_datetime`` is enough on its own, a
        ``datetime`` value is not required.
        """
        if self.start is None or self.end is None:
            return None
        start = _as_utc(self.start.value)
        end = _as_utc(self.end.value)
        if start > end:
            return None
        return start, end

    def _declared_window(self) -> _Window:
        """Bounds implied by fields declared on the asset itself."""
        lowers: list[datetime] = []
        uppers: list[datetime] = []
        if self.point is not None and self.point.origin is TimeOrigin.DECLARED:
            lowers.append(_as_utc(self.point.value))
            uppers.append(_as_utc(self.point.value))
        if self.start is not None and self.start.origin is TimeOrigin.DECLARED:
            lowers.append(_as_utc(self.start.value))
        if self.end is not None and self.end.origin is TimeOrigin.DECLARED:
            uppers.append(_as_utc(self.end.value))
        return _max_present(lowers), _min_present(uppers)

    def _compute_definite_window(self) -> _Window | None:
        lower, upper = self._declared_window()
        if lower is None or upper is None or lower > upper:
            return None
        return lower, upper

    def _compute_possible_window(self) -> _Window:
        declared_lower, declared_upper = self._declared_window()
        has_declared = any(
            value is not None and value.origin is TimeOrigin.DECLARED
            for value in (self.point, self.start, self.end)
        )

        if has_declared:
            # An asset that declares its own time is bounded by its claims; only
            # range bounds inherited from the item tighten those claims. An
            # inherited nominal datetime (a point) must not shrink a range the
            # asset explicitly declares.
            lower = declared_lower
            upper = declared_upper
            if (
                self.start is not None
                and self.start.origin is TimeOrigin.INHERITED
            ):
                inherited_start = _as_utc(self.start.value)
                lower = (
                    inherited_start if lower is None else max(lower, inherited_start)
                )
            if self.end is not None and self.end.origin is TimeOrigin.INHERITED:
                inherited_end = _as_utc(self.end.value)
                upper = inherited_end if upper is None else min(upper, inherited_end)
            if lower is not None and upper is not None and lower > upper:
                # Declared and inherited range bounds contradict each other;
                # trust the asset's own claims rather than reordering anything.
                return declared_lower, declared_upper
            return lower, upper

        # Nothing declared on the asset: the item's metadata is the only estimate.
        lowers: list[datetime] = []
        uppers: list[datetime] = []
        for value in (self.point, self.start):
            if value is not None:
                lowers.append(_as_utc(value.value))
        for value in (self.point, self.end):
            if value is not None:
                uppers.append(_as_utc(value.value))
        return _max_present(lowers), _min_present(uppers)

    def definitely_contains(self, instant: datetime) -> bool:
        """Whether the asset is known to cover ``instant``.

        Only facts declared on the asset itself count as certain: an asset-level
        point equal to ``instant`` or a closed asset-declared interval containing
        it. Inherited item metadata can never produce a definite hit.
        """
        window = self._definite_window
        if window is None:
            return False
        lower, upper = window
        target = _as_utc(instant)
        return (lower is None or target >= lower) and (
            upper is None or target <= upper
        )

    def possibly_contains(self, instant: datetime) -> bool:
        """Whether coverage at ``instant`` cannot be ruled out.

        Combines asset-declared and item-inherited constraints. Assets without
        any temporal information are considered possible at every instant.
        Definite coverage implies possible coverage.
        """
        lower, upper = self._possible_window
        target = _as_utc(instant)
        return (lower is None or target >= lower) and (
            upper is None or target <= upper
        )

    def to_dict(self) -> dict[str, Any]:
        """Export the resolved temporal information as a JSON-friendly dict.

        ``interval`` is only populated when a valid, non-contradictory
        start/end pair is available, making it suitable for cleaning pipelines
        that need the asset's actually usable time rather than the owning item's
        broad coverage.
        """
        interval = self.interval
        return {
            "href": self.asset.href,
            "datetime": (
                datetime_to_str(self.point.value) if self.point is not None else None
            ),
            "start_datetime": (
                datetime_to_str(self.start.value) if self.start is not None else None
            ),
            "end_datetime": (
                datetime_to_str(self.end.value) if self.end is not None else None
            ),
            "origins": {key: origin.value for key, origin in self.origins.items()},
            "interval": (
                [datetime_to_str(interval[0]), datetime_to_str(interval[1])]
                if interval is not None
                else None
            ),
            "conflicts": [
                {"code": conflict.code, "message": conflict.message}
                for conflict in self.conflicts
            ],
        }

    def __repr__(self) -> str:
        return f"<AssetTime href={self.asset.href} conflicts={len(self.conflicts)}>"


def _build_conflicts(
    start: TimeValue | None, end: TimeValue | None
) -> tuple[TimeConflict, ...]:
    conflicts: list[TimeConflict] = []
    if (
        start is not None
        and end is not None
        and _as_utc(start.value) > _as_utc(end.value)
    ):
        if (
            start.origin is TimeOrigin.DECLARED
            and end.origin is TimeOrigin.INHERITED
        ):
            code = "declared_start_after_inherited_end"
            message = (
                f"Asset-declared start_datetime {datetime_to_str(start.value)} is "
                f"later than the end_datetime {datetime_to_str(end.value)} inherited "
                "from the owning item; bounds were not reordered."
            )
        elif (
            start.origin is TimeOrigin.INHERITED
            and end.origin is TimeOrigin.DECLARED
        ):
            code = "inherited_start_after_declared_end"
            message = (
                f"Start_datetime {datetime_to_str(start.value)} inherited from the "
                f"owning item is later than the asset-declared end_datetime "
                f"{datetime_to_str(end.value)}; bounds were not reordered."
            )
        else:
            code = "start_after_end"
            message = (
                f"start_datetime {datetime_to_str(start.value)} is later than "
                f"end_datetime {datetime_to_str(end.value)}; bounds were not "
                "reordered."
            )
        conflicts.append(TimeConflict(code=code, message=message))
    return tuple(conflicts)


def get_asset_time(asset: Asset) -> AssetTime:
    """Resolve the temporal information that applies to one asset.

    Each of ``datetime``, ``start_datetime`` and ``end_datetime`` is read from
    the asset first and falls back to the owning item when undeclared. Paired
    start/end values are retained even when ``datetime`` is missing, and
    contradictory bounds are reported as conflicts instead of being swapped.
    """
    item = _owner_item(asset)
    point = _resolve(asset, item, "datetime")
    start = _resolve(asset, item, "start_datetime")
    end = _resolve(asset, item, "end_datetime")
    conflicts = _build_conflicts(start, end)
    return AssetTime(asset, point, start, end, conflicts)


def get_asset_times(item: Item) -> dict[str, AssetTime]:
    """Resolve temporal information for every asset of an item, keyed by asset key."""
    return {key: get_asset_time(asset) for key, asset in item.assets.items()}


@dataclass(frozen=True)
class AssetSearchResult:
    """Outcome of :func:`search_assets` at one instant.

    ``definite`` holds assets known to cover the instant and ``possible`` holds
    the remaining assets whose coverage cannot be ruled out (including assets
    with no temporal information at all). The two dictionaries are disjoint.
    """

    instant: datetime
    definite: dict[str, AssetTime]
    possible: dict[str, AssetTime]


def search_assets(item: Item, instant: datetime) -> AssetSearchResult:
    """Split an item's assets into definite and possible hits at ``instant``.

    A hit is definite only when the asset itself declares a point equal to the
    instant or a closed interval containing it. Everything that merely inherits
    temporal metadata from the item, or carries no temporal metadata, lands in
    the possible group instead.
    """
    definite: dict[str, AssetTime] = {}
    possible: dict[str, AssetTime] = {}
    for key, asset_time in get_asset_times(item).items():
        if asset_time.definitely_contains(instant):
            definite[key] = asset_time
        elif asset_time.possibly_contains(instant):
            possible[key] = asset_time
    return AssetSearchResult(instant=instant, definite=definite, possible=possible)
