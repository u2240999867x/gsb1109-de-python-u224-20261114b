from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

import pystac
from pystac import (
    Catalog,
    CatalogType,
    Collection,
    Inventory,
    Item,
    Link,
    ObjectIdentity,
    ObjectRecord,
    build_inventory,
)
from pystac.inventory import MembershipRecord
from tests.utils import ARBITRARY_BBOX, ARBITRARY_EXTENT, ARBITRARY_GEOM


def _make_item(
    item_id: str,
    collection: str | None = None,
    href: str | None = None,
    properties: dict[str, Any] | None = None,
) -> Item:
    item = Item(
        id=item_id,
        geometry=ARBITRARY_GEOM,
        bbox=ARBITRARY_BBOX,
        datetime=datetime(2024, 1, 1, tzinfo=timezone.utc),
        properties=dict(properties or {}),
        collection=collection,
    )
    if href is not None:
        item.set_self_href(href)
    return item


@pytest.fixture
def supplier_catalog() -> tuple[Catalog, Item, Item, Item]:
    root = Catalog("root", "Root catalog")
    supplier_a = Collection("supplier-a", "Supplier A", ARBITRARY_EXTENT)
    supplier_b = Collection("supplier-b", "Supplier B", ARBITRARY_EXTENT)
    root.add_child(supplier_a)
    root.add_child(supplier_b)

    scene_a = _make_item(
        "scene-1",
        collection="supplier-a",
        href="https://example.com/a/scene-1.json",
        properties={"provider": "a"},
    )
    scene_b = _make_item(
        "scene-1",
        collection="supplier-b",
        href="https://example.com/b/scene-1.json",
        properties={"provider": "b"},
    )
    supplier_a.add_item(scene_a)
    supplier_b.add_item(scene_b)

    shared = _make_item(
        "shared-scene",
        collection="supplier-a",
        href="https://example.com/a/shared.json",
    )
    supplier_a.add_item(shared)
    mirror = Catalog("mirror", "Mirror catalog linking the same image")
    root.add_child(mirror)
    mirror.add_item(shared)

    return root, scene_a, scene_b, shared


def test_same_item_id_from_two_suppliers_is_not_merged(
    supplier_catalog: tuple[Catalog, Item, Item, Item],
) -> None:
    root, scene_a, scene_b, _ = supplier_catalog

    inventory = build_inventory(root)

    assert inventory.member_count == 4
    assert inventory.object_count == 3
    assert inventory.count_difference == 1

    identities = list(inventory.objects)
    identity_a = ObjectIdentity(
        "scene-1", "supplier-a", "https://example.com/a/scene-1.json"
    )
    identity_b = ObjectIdentity(
        "scene-1", "supplier-b", "https://example.com/b/scene-1.json"
    )
    assert identity_a in identities
    assert identity_b in identities
    assert identity_a != identity_b

    record_a = inventory.objects[identity_a]
    record_b = inventory.objects[identity_b]
    assert record_a.source_versions == [scene_a]
    assert record_b.source_versions == [scene_b]
    assert not inventory.conflicting_logical_ids()


def test_same_image_via_two_links_is_one_object_with_two_memberships(
    supplier_catalog: tuple[Catalog, Item, Item, Item],
) -> None:
    root, _, _, shared = supplier_catalog

    inventory = build_inventory(root)
    shared_identity = ObjectIdentity(
        "shared-scene", "supplier-a", "https://example.com/a/shared.json"
    )
    record = inventory.objects[shared_identity]

    memberships = inventory.memberships_for(record)
    assert len(memberships) == 2
    assert {membership.container_id for membership in memberships} == {
        "supplier-a",
        "mirror",
    }
    assert all(
        membership.link_href == shared.get_self_href() for membership in memberships
    )
    assert record.source_versions == [shared]


def test_count_difference_traces_back_to_original_links(
    supplier_catalog: tuple[Catalog, Item, Item, Item],
) -> None:
    root, _, _, _ = supplier_catalog

    inventory = build_inventory(root)
    explanation = inventory.explain_count_difference()

    assert sum(row["extra_memberships"] for row in explanation) == (
        inventory.count_difference
    )
    assert sum(row["membership_count"] for row in explanation) == (
        inventory.member_count
    )

    shared_rows = [row for row in explanation if row["object_id"] == "shared-scene"]
    assert len(shared_rows) == 1
    shared_row = shared_rows[0]
    assert shared_row["extra_memberships"] == 1
    assert set(shared_row["container_ids"]) == {"supplier-a", "mirror"}
    assert shared_row["link_hrefs"] == [
        "https://example.com/a/shared.json",
        "https://example.com/a/shared.json",
    ]

    scene_rows = [row for row in explanation if row["object_id"] == "scene-1"]
    assert all(row["extra_memberships"] == 0 for row in scene_rows)
    assert len(scene_rows) == 2


def test_contradictory_content_under_same_logical_id_keeps_versions() -> None:
    root = Catalog("root", "Root catalog")
    collection = Collection("coll", "A collection", ARBITRARY_EXTENT)
    root.add_child(collection)

    version_one = _make_item(
        "scene-1",
        collection="coll",
        href="https://example.com/v1/scene-1.json",
        properties={"revision": 1},
    )
    version_two = _make_item(
        "scene-1",
        collection="coll",
        href="https://example.com/v2/scene-1.json",
        properties={"revision": 2},
    )
    collection.add_item(version_one)
    collection.add_item(version_two)

    inventory = build_inventory(root)

    assert inventory.member_count == 2
    assert inventory.object_count == 2
    groups = inventory.logical_objects()
    assert len(groups["coll/scene-1"]) == 2
    assert set(inventory.conflicting_logical_ids()) == {"coll/scene-1"}

    observed = {
        record.href: record.source_versions[0] for record in groups["coll/scene-1"]
    }
    assert observed == {
        "https://example.com/v1/scene-1.json": version_one,
        "https://example.com/v2/scene-1.json": version_two,
    }


def test_object_record_retains_changed_content_at_same_identity() -> None:
    item = _make_item(
        "scene-1",
        collection="coll",
        href="https://example.com/scene-1.json",
        properties={"revision": 1},
    )
    identity = ObjectIdentity(item.id, item.collection_id, item.get_self_href())
    record = ObjectRecord(identity)

    assert record.observe(item) is True
    assert record.observe(item) is False
    item.properties["revision"] = 2
    assert record.observe(item) is True
    assert record.has_conflicting_versions
    assert len(record.source_versions) == 2


def test_identity_requires_all_evidence_to_agree() -> None:
    base = ObjectIdentity("scene-1", "coll", "https://example.com/scene-1.json")

    assert base == ObjectIdentity("scene-1", "coll", "https://example.com/scene-1.json")
    assert base != ObjectIdentity(
        "scene-1", "other-coll", "https://example.com/scene-1.json"
    )
    assert base != ObjectIdentity(
        "scene-1", "coll", "https://example.com/elsewhere/scene-1.json"
    )
    assert base != ObjectIdentity("scene-2", "coll", "https://example.com/scene-1.json")
    assert base.logical_id == "coll/scene-1"
    assert ObjectIdentity("scene-1", None, None).logical_id == "scene-1"


def test_membership_and_object_tables_serialize() -> None:
    root = Catalog("root", "Root catalog")
    item = _make_item(
        "scene-1",
        href="https://example.com/scene-1.json",
    )
    root.add_item(item)

    payload = build_inventory(root).to_dict()

    assert payload["summary"] == {
        "member_count": 1,
        "object_count": 1,
        "count_difference": 0,
    }
    assert payload["object_table"] == [
        {
            "logical_id": "scene-1",
            "object_id": "scene-1",
            "collection_id": None,
            "href": "https://example.com/scene-1.json",
            "version_count": 1,
            "source_hrefs": ["https://example.com/scene-1.json"],
        }
    ]
    assert payload["membership_table"] == [
        {
            "container_id": "root",
            "logical_id": "scene-1",
            "object_id": "scene-1",
            "collection_id": None,
            "href": "https://example.com/scene-1.json",
        }
    ]
    assert payload["conflicting_logical_ids"] == []


def test_inventory_resolves_relative_links_from_saved_catalog(
    tmp_path: Any,
) -> None:
    root = Catalog("root", "Root catalog")
    collection = Collection("coll", "A collection", ARBITRARY_EXTENT)
    root.add_child(collection)
    collection.add_item(
        _make_item("scene-1", collection="coll", properties={"provider": "a"})
    )
    collection.add_item(
        _make_item("scene-2", collection="coll", properties={"provider": "b"})
    )

    root.normalize_and_save(str(tmp_path), catalog_type=CatalogType.SELF_CONTAINED)
    reloaded = Catalog.from_file(str(tmp_path / "catalog.json"))
    inventory = build_inventory(reloaded)

    assert inventory.member_count == 2
    assert inventory.object_count == 2
    membership: MembershipRecord = inventory.memberships[0]
    assert membership.link_href is not None
    assert membership.link_href.startswith(str(tmp_path))
    assert membership.link_href.endswith(".json")


def test_inventory_walk_handles_catalog_loops() -> None:
    root = Catalog("root", "Root catalog")
    first = Catalog("first", "First subcatalog")
    second = Catalog("second", "Second subcatalog")
    root.add_child(first)
    first.add_child(second)
    second.add_link(Link.child(first))

    item = _make_item("scene-1", href="https://example.com/scene-1.json")
    first.add_item(item)

    inventory = build_inventory(root)

    assert inventory.member_count == 1
    assert inventory.object_count == 1
    assert isinstance(inventory, Inventory)
    assert pystac.build_inventory is build_inventory
