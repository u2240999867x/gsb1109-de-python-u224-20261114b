from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from pystac.asset import Asset
from pystac.rel_type import RelType
from pystac.stac_object import STACObject

DEFAULT_ASSET_FIELDS: tuple[str, ...] = ("license", "description")
"""Asset fields inspected, in order, for asset-level license / usage statements."""

VARIOUS_LICENSE = "various"
"""Collection ``license`` value that defers licensing to the member assets."""


class SourceKind(str, Enum):
    """The kind of place a license statement was found."""

    COLLECTION_LICENSE = "collection_license"
    """The ``license`` field of the collection itself."""

    LICENSE_LINK = "license_link"
    """A link with ``rel=\"license\"`` on the collection."""

    ASSET_FIELD = "asset_field"
    """A field carried by an individual asset (e.g. its own ``license``)."""


@dataclass(frozen=True)
class StatementSource:
    """A reference to the exact place a license statement was found.

    All attributes are immutable scalars captured at collection time, so a
    source stays valid (and unchanged) even if the originating object is
    edited later.
    """

    kind: SourceKind
    """The kind of source the statement came from."""

    object_id: str | None
    """The ``id`` of the STAC object the statement was read from."""

    asset_key: str | None = None
    """The key of the asset the statement was read from, if any."""

    field: str | None = None
    """The field the statement was read from, if any."""

    href: str | None = None
    """The link href the statement was read from, if any."""

    def to_dict(self) -> dict[str, Any]:
        """Returns this source as a dictionary."""
        d: dict[str, Any] = {"kind": self.kind.value, "object_id": self.object_id}
        if self.asset_key is not None:
            d["asset_key"] = self.asset_key
        if self.field is not None:
            d["field"] = self.field
        if self.href is not None:
            d["href"] = self.href
        return d


@dataclass(frozen=True)
class LicenseStatement:
    """A single license or usage statement, captured verbatim from its source.

    Statements are snapshots: the text is copied at collection time, so later
    edits to the underlying collection or assets do not mutate a statement
    that has already been collected.
    """

    text: str
    """The verbatim text of the statement (an SPDX id, a link href or title,
    or an asset's own usage note)."""

    source: StatementSource
    """Where the statement was found."""

    roles: tuple[str, ...] = ()
    """The asset roles this statement applies to. An empty tuple means the
    statement applies to the whole collection, regardless of role."""

    def applies_to(self, roles: Iterable[str]) -> bool:
        """Checks whether this statement applies to any of the given roles.

        Collection-wide statements (no roles) apply to everything.
        """
        if not self.roles:
            return True
        return any(role in self.roles for role in roles)

    def to_dict(self) -> dict[str, Any]:
        """Returns this statement as a dictionary."""
        return {
            "text": self.text,
            "source": self.source.to_dict(),
            "roles": list(self.roles),
        }


@dataclass(frozen=True)
class LicenseConflict:
    """A set of contradictory license statements covering the same asset.

    Conflicting statements are preserved, not resolved: every statement keeps
    its own source so a human reviewer can adjudicate.
    """

    asset_key: str | None
    """The asset the conflicting statements cover, or ``None`` for
    collection-wide conflicts."""

    statements: tuple[LicenseStatement, ...]
    """The contradictory statements, in the order they were collected."""

    @property
    def texts(self) -> tuple[str, ...]:
        """The distinct statement texts involved in the conflict."""
        return tuple(dict.fromkeys(s.text for s in self.statements))

    def to_dict(self) -> dict[str, Any]:
        """Returns this conflict as a dictionary."""
        return {
            "asset_key": self.asset_key,
            "texts": list(self.texts),
            "statements": [s.to_dict() for s in self.statements],
        }


@dataclass
class LicenseReport:
    """The result of collecting license statements for a declared scope.

    A report is a point-in-time snapshot. It owns copies of everything it
    references, so mutating the collection afterwards (for example editing a
    thumbnail's usage note) never changes an already-collected report.
    """

    object_id: str | None
    """The ``id`` of the STAC object the statements were collected from."""

    roles: tuple[str, ...] | None
    """The caller-declared roles used to scope the collection, or ``None``
    if no role filter was declared."""

    statements: list[LicenseStatement] = field(default_factory=list)
    """All evidence-backed statements applicable to the declared scope."""

    unknown_assets: list[str] = field(default_factory=list)
    """Keys of in-scope assets for which no license evidence was found."""

    def conflicts(self) -> list[LicenseConflict]:
        """Computes the contradictory statement groups in this report.

        Two statements conflict when they cover the same asset with different
        license texts. Link statements are pointers to license documents, not
        license texts, and a collection license of ``"various"`` defers to the
        assets, so neither takes part in a conflict.
        """
        collection_wide = [
            s
            for s in self.statements
            if s.source.kind == SourceKind.COLLECTION_LICENSE
            and s.text != VARIOUS_LICENSE
        ]
        by_asset: dict[str, list[LicenseStatement]] = {}
        for statement in self.statements:
            if statement.source.kind == SourceKind.ASSET_FIELD:
                assert statement.source.asset_key is not None
                by_asset.setdefault(statement.source.asset_key, []).append(statement)

        conflicts: list[LicenseConflict] = []
        for key in sorted(by_asset):
            statements = tuple(collection_wide) + tuple(by_asset[key])
            if len({s.text for s in statements}) > 1:
                conflicts.append(
                    LicenseConflict(asset_key=key, statements=statements)
                )
        return conflicts

    def summary(self) -> dict[str, list[dict[str, Any]]]:
        """Summarizes the distinct statement texts and their sources.

        Only evidence-backed statements are summarized; unknown assets are
        reported separately via :attr:`unknown_assets` and never appear here.
        """
        result: dict[str, list[dict[str, Any]]] = {}
        for statement in self.statements:
            sources = result.setdefault(statement.text, [])
            source_dict = statement.source.to_dict()
            if source_dict not in sources:
                sources.append(source_dict)
        return result

    def to_dict(self) -> dict[str, Any]:
        """Returns this report as a dictionary, suitable for export (e.g. JSON)
        for human review."""
        return {
            "object_id": self.object_id,
            "roles": list(self.roles) if self.roles is not None else None,
            "statements": [s.to_dict() for s in self.statements],
            "unknown_assets": list(self.unknown_assets),
            "conflicts": [c.to_dict() for c in self.conflicts()],
        }


def _asset_field_value(asset: Asset, field_name: str) -> Any:
    if field_name in asset.extra_fields:
        return asset.extra_fields[field_name]
    return getattr(asset, field_name, None)


def _asset_roles(asset: Asset) -> tuple[str, ...]:
    return tuple(asset.roles) if asset.roles else ()


def collect_license_statements(
    obj: STACObject,
    roles: Iterable[str] | None = None,
    asset_fields: tuple[str, ...] = DEFAULT_ASSET_FIELDS,
) -> LicenseReport:
    """Collects license statements from a STAC object for human review.

    Statements are gathered from three kinds of sources:

    - the object's own ``license`` field (collection-wide scope),
    - its links with ``rel=\"license\"`` (collection-wide scope),
    - license / usage fields carried by individual assets (scoped to that
      asset's roles, so e.g. a thumbnail's usage note only applies to
      assets with the ``thumbnail`` role).

    Nothing is merged or resolved: multiple applicable statements, unknown
    assets, and contradictory statements are all preserved in the returned
    :class:`LicenseReport`, each statement tracing back to its own source.

    Args:
        obj: The STAC object (typically a :class:`~pystac.Collection`) to
            collect statements from.
        roles: The caller-declared asset roles to collect for (e.g.
            ``["data"]``). Asset-level statements are only included when the
            asset's roles intersect this declaration; collection-wide
            statements are always included. If ``None``, all assets are in
            scope.
        asset_fields: The asset fields inspected for asset-level statements,
            in order. Defaults to :data:`DEFAULT_ASSET_FIELDS`.

    Returns:
        LicenseReport: A snapshot of the applicable statements, unknown
        assets, and any contradictions.
    """
    declared_roles = tuple(roles) if roles is not None else None
    object_id = getattr(obj, "id", None)
    report = LicenseReport(object_id=object_id, roles=declared_roles)

    collection_license_texts: list[str] = []
    license_text = getattr(obj, "license", None)
    if isinstance(license_text, str) and license_text:
        collection_license_texts.append(license_text)
        report.statements.append(
            LicenseStatement(
                text=license_text,
                source=StatementSource(
                    kind=SourceKind.COLLECTION_LICENSE,
                    object_id=object_id,
                    field="license",
                ),
            )
        )

    for link in obj.get_links(RelType.LICENSE):
        href = link.get_href()
        text = link.title if link.title else href
        if not text:
            continue
        report.statements.append(
            LicenseStatement(
                text=text,
                source=StatementSource(
                    kind=SourceKind.LICENSE_LINK,
                    object_id=object_id,
                    href=href,
                ),
            )
        )

    assets: dict[str, Asset] = getattr(obj, "assets", {})
    for key, asset in assets.items():
        role_set = _asset_roles(asset)
        if declared_roles is not None and not any(
            role in declared_roles for role in role_set
        ):
            continue
        asset_has_evidence = False
        for field_name in asset_fields:
            value = _asset_field_value(asset, field_name)
            if isinstance(value, str) and value:
                asset_has_evidence = True
                report.statements.append(
                    LicenseStatement(
                        text=value,
                        source=StatementSource(
                            kind=SourceKind.ASSET_FIELD,
                            object_id=object_id,
                            asset_key=key,
                            field=field_name,
                        ),
                        roles=role_set,
                    )
                )
        covered_by_collection = any(
            text != VARIOUS_LICENSE for text in collection_license_texts
        )
        if not asset_has_evidence and not covered_by_collection:
            report.unknown_assets.append(key)

    return report
