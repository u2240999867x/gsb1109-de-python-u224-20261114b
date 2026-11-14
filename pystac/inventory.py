"""Inventory tables for the objects and membership relationships in a catalog.

Two suppliers can publish Items that share an Item ID (e.g. both use
``"scene-1"``), and a single image can be reachable through more than one link
inside the same catalog. This module separates two ways of counting:

* memberships, i.e. the ``item`` links a catalog declares (one row per link),
  and
* objects, i.e. the distinct Items those links point at (one row per identity).

Identity is decided from evidence rather than the Item ID alone:

* the Item ID declared in the document,
* the declared collection ID, and
* the resolvable absolute HREF of the link target.

Observations are only merged into one object row when their evidence agrees.
When contradictory content is published under the same logical ID, every
source version is retained instead of being collapsed into a single object.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pystac
from pystac.utils import is_absolute_href, make_absolute_href

if TYPE_CHECKING:
    from pystac.catalog import Catalog
    from pystac.item import Item
    from pystac.link import Link


def _content_signature(item: Item) -> str:
    """Stable signature of the document content of an Item."""
    return json.dumps(
        item.to_dict(include_self_link=False),
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )


def _resolve_href(link: Link, item: Item) -> str | None:
    """Resolve the absolute HREF of an item link.

    Resolved links report the target's self HREF, unresolved links are resolved
    against the link owner, and objects without any HREF return ``None``.
    """
    href = link.get_absolute_href()
    if href is None:
        href = item.get_self_href()
    elif not is_absolute_href(href):
        owner_href = link.owner.get_self_href() if link.owner is not None else None
        if owner_href is not None:
            href = make_absolute_href(href, owner_href)
    return href


@dataclass(frozen=True)
class ObjectIdentity:
    """Evidence-based identity of a STAC object.

    Two observations share an identity only when their Item ID, declared
    collection ID and resolvable HREF all agree. A missing collection
    declaration or HREF is represented as ``None`` and only matches another
    missing value; an unknown piece of evidence never confirms a merge.

    Args:
        object_id : The Item ID declared in the document.
        collection_id : The collection ID declared on the Item, if any.
        href : The resolvable absolute HREF of the object, if any.
    """

    object_id: str
    collection_id: str | None
    href: str | None

    @property
    def logical_id(self) -> str:
        """The logical identifier shared by conflicting source versions.

        Combines the declared collection ID with the Item ID when a collection
        is declared, otherwise uses the Item ID alone.
        """
        if self.collection_id is not None:
            return f"{self.collection_id}/{self.object_id}"
        return self.object_id


@dataclass
class ObjectRecord:
    """One row of the object table.

    A record gathers the source versions that share an :class:`ObjectIdentity`.
    In practice there is a single version, but contradictory documents published
    at the same logical identifier are all retained.

    Args:
        identity : The evidence-based identity of the object.
        source_versions : The distinct Item documents observed for this
            identity, in first-seen order.
    """

    identity: ObjectIdentity
    source_versions: list[Item] = field(default_factory=list)
    _signatures: set[str] = field(default_factory=set, repr=False)

    @property
    def object_id(self) -> str:
        return self.identity.object_id

    @property
    def collection_id(self) -> str | None:
        return self.identity.collection_id

    @property
    def href(self) -> str | None:
        return self.identity.href

    @property
    def logical_id(self) -> str:
        return self.identity.logical_id

    @property
    def has_conflicting_versions(self) -> bool:
        """True if more than one distinct document shares this identity."""
        return len(self.source_versions) > 1

    def observe(self, item: Item) -> bool:
        """Record an observation, retaining contradictory content.

        Returns:
            bool: True if this is a new source version, False if the content
            was already observed.
        """
        signature = _content_signature(item)
        if signature in self._signatures:
            return False
        self._signatures.add(signature)
        self.source_versions.append(item)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "object_id": self.object_id,
            "collection_id": self.collection_id,
            "href": self.href,
            "version_count": len(self.source_versions),
            "source_hrefs": [
                version.get_self_href() for version in self.source_versions
            ],
        }


@dataclass(frozen=True)
class MembershipRecord:
    """One row of the membership table.

    A membership is a single ``item`` link from a container (catalog or
    collection) to an Item. The same object can appear in several membership
    rows when it is reachable through several links.

    Args:
        container_id : The ID of the catalog or collection owning the link.
        link_href : The HREF carried by the original link, resolved to an
            absolute HREF when possible.
        identity : The identity of the referenced object.
    """

    container_id: str
    link_href: str | None
    identity: ObjectIdentity

    @property
    def logical_id(self) -> str:
        return self.identity.logical_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "container_id": self.container_id,
            "logical_id": self.logical_id,
            "object_id": self.identity.object_id,
            "collection_id": self.identity.collection_id,
            "href": self.link_href,
        }


class Inventory:
    """The object table and membership table of a catalog tree.

    Build one with :func:`build_inventory`. Membership counts follow the links
    declared by catalogs, while object counts follow the distinct identities
    those links resolve to. The difference between the two counts can be
    traced back to the original links with :meth:`explain_count_difference`.
    """

    def __init__(self) -> None:
        self.objects: dict[ObjectIdentity, ObjectRecord] = {}
        self.memberships: list[MembershipRecord] = []

    @property
    def object_count(self) -> int:
        """Number of distinct object identities."""
        return len(self.objects)

    @property
    def member_count(self) -> int:
        """Number of item-link memberships declared by the catalogs."""
        return len(self.memberships)

    @property
    def count_difference(self) -> int:
        """Memberships minus objects.

        Every extra link to an already-counted object adds one to this value,
        so the difference is fully explained by
        :meth:`explain_count_difference`.
        """
        return self.member_count - self.object_count

    def logical_objects(self) -> dict[str, list[ObjectRecord]]:
        """Group object records by their logical ID.

        A logical ID with more than one record holds contradictory source
        versions that were deliberately not merged.
        """
        groups: dict[str, list[ObjectRecord]] = {}
        for record in self.objects.values():
            groups.setdefault(record.logical_id, []).append(record)
        return groups

    def conflicting_logical_ids(self) -> dict[str, list[ObjectRecord]]:
        """Logical IDs published with more than one source version."""
        return {
            logical_id: records
            for logical_id, records in self.logical_objects().items()
            if len(records) > 1
            or any(record.has_conflicting_versions for record in records)
        }

    def memberships_for(self, record: ObjectRecord) -> list[MembershipRecord]:
        """All membership links that point at the given object record."""
        return [
            membership
            for membership in self.memberships
            if membership.identity == record.identity
        ]

    def explain_count_difference(self) -> list[dict[str, Any]]:
        """Trace the member/object count difference back to original links.

        Returns one row per object with the membership links that reference it
        and the number of extra (duplicate) memberships. The sum of
        ``extra_memberships`` across the rows equals
        :attr:`count_difference`.
        """
        rows: list[dict[str, Any]] = []
        for record in self.objects.values():
            memberships = self.memberships_for(record)
            rows.append(
                {
                    "logical_id": record.logical_id,
                    "object_id": record.object_id,
                    "collection_id": record.collection_id,
                    "href": record.href,
                    "membership_count": len(memberships),
                    "extra_memberships": len(memberships) - 1,
                    "container_ids": [
                        membership.container_id for membership in memberships
                    ],
                    "link_hrefs": [membership.link_href for membership in memberships],
                }
            )
        return rows

    def to_dict(self) -> dict[str, Any]:
        """Serialize both tables and the summary counts."""
        return {
            "summary": {
                "member_count": self.member_count,
                "object_count": self.object_count,
                "count_difference": self.count_difference,
            },
            "object_table": [record.to_dict() for record in self.objects.values()],
            "membership_table": [
                membership.to_dict() for membership in self.memberships
            ],
            "conflicting_logical_ids": sorted(self.conflicting_logical_ids()),
        }

    def _record_membership(self, container: Catalog, link: Link, item: Item) -> None:
        href = _resolve_href(link, item)
        identity = ObjectIdentity(
            object_id=item.id,
            collection_id=item.collection_id,
            href=href,
        )
        record = self.objects.get(identity)
        if record is None:
            record = ObjectRecord(identity)
            self.objects[identity] = record
        record.observe(item)
        self.memberships.append(
            MembershipRecord(
                container_id=container.id,
                link_href=href,
                identity=identity,
            )
        )


def build_inventory(catalog: Catalog) -> Inventory:
    """Build an :class:`Inventory` for the tree rooted at ``catalog``.

    The walk follows ``child`` and ``item`` links, resolving them through the
    root catalog's object cache so that links to the same HREF observe the same
    object. Loops and shared children in the catalog graph are handled without
    visiting a node or link more than once.

    Args:
        catalog : The root catalog or collection to inventory.

    Returns:
        Inventory: The object table, membership table and counting helpers for
        the catalog tree.
    """
    inventory = Inventory()
    root = catalog.get_root() or catalog

    visited_nodes: set[int] = set()
    visited_links: set[int] = set()
    stack: list[Catalog] = [catalog]

    while stack:
        node = stack.pop()
        if id(node) in visited_nodes:
            continue
        visited_nodes.add(id(node))

        for link in node.get_item_links():
            if id(link) in visited_links:
                continue
            visited_links.add(id(link))
            link.resolve_stac_object(root=root)
            target = link.target
            if isinstance(target, pystac.Item):
                inventory._record_membership(node, link, target)

        for child in node.get_children():
            if isinstance(child, pystac.Catalog):
                stack.append(child)

    return inventory
