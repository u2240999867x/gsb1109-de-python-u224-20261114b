import json
from datetime import datetime

from pystac import Item
from pystac.provenance import (
    CyclicRelationship,
    ProvenanceReport,
    SourceReference,
    UnresolvedReference,
    trace_provenance,
)
from tests.utils import ARBITRARY_BBOX, ARBITRARY_GEOM


def make_item(item_id: str) -> Item:
    return Item(
        id=item_id,
        geometry=ARBITRARY_GEOM,
        bbox=ARBITRARY_BBOX,
        datetime=datetime.now(),
        properties={},
    )


def test_mosaic_with_three_original_scenes() -> None:
    mosaic = make_item("mosaic")
    originals = [make_item(f"original-{idx}") for idx in range(3)]
    mosaic.add_derived_from(*originals)

    report = trace_provenance(mosaic)

    assert report.product_id == "mosaic"
    assert report.root_source_count == 3
    assert report.total_reference_count == 3
    assert {root.id for root in report.roots} == {
        "original-0",
        "original-1",
        "original-2",
    }
    assert all(root.reference_count == 1 for root in report.roots)
    assert report.unresolved == []
    assert report.cycles == []


def test_two_roads_merging_into_same_base_map() -> None:
    mosaic = make_item("mosaic")
    originals = [make_item(f"original-{idx}") for idx in range(3)]
    road_a = make_item("road-a")
    road_b = make_item("road-b")
    base_map = make_item("base-map")
    base_map.set_self_href("https://example.com/base-map.json")

    mosaic.add_derived_from(*originals, road_a, road_b)
    road_a.add_derived_from(base_map)
    road_b.add_derived_from(base_map)

    report = trace_provenance(mosaic)
    roots_by_id = {root.id: root for root in report.roots}

    assert report.root_source_count == 4
    assert report.total_reference_count == 5
    assert roots_by_id["base-map"].reference_count == 2
    assert roots_by_id["base-map"].href == "https://example.com/base-map.json"
    for idx in range(3):
        assert roots_by_id[f"original-{idx}"].reference_count == 1
    # Derived products themselves are never listed as roots.
    assert "road-a" not in roots_by_id
    assert "road-b" not in roots_by_id
    assert "mosaic" not in roots_by_id


def test_derived_intermediate_is_not_counted_as_original() -> None:
    product = make_item("product")
    intermediate = make_item("intermediate")
    root = make_item("root")
    product.add_derived_from(intermediate)
    intermediate.add_derived_from(root)

    report = trace_provenance(product)

    assert report.root_source_count == 1
    assert report.roots == [
        SourceReference(id="root", href=None, reference_count=1)
    ]


def test_unresolvable_external_catalog_marked_pending() -> None:
    product = make_item("product")
    confirmed_root = make_item("confirmed-root")
    product.add_derived_from(confirmed_root)
    product.add_derived_from("external-catalog/missing-scene.json")

    report = trace_provenance(product)

    assert report.root_source_count == 1
    assert report.roots[0].id == "confirmed-root"
    assert report.unresolved == [
        UnresolvedReference(
            source_id="product",
            target_href="external-catalog/missing-scene.json",
        )
    ]
    # The unresolved target must not be mistaken for a root source.
    assert all(root.id != "missing-scene" for root in report.roots)


def test_cycle_between_two_generations_is_flagged() -> None:
    generation_one = make_item("gen-1")
    generation_two = make_item("gen-2")
    confirmed_root = make_item("confirmed-root")

    generation_two.add_derived_from(generation_one)
    generation_one.add_derived_from(generation_two)
    generation_one.add_derived_from(confirmed_root)

    report = trace_provenance(generation_two)

    assert report.cycles == [
        CyclicRelationship(
            source_id="gen-2",
            target_id="gen-1",
            target_href=None,
        ),
        CyclicRelationship(
            source_id="gen-1",
            target_id="gen-2",
            target_href=None,
        ),
    ]
    # Independently confirmed roots are still reported as usual.
    assert report.roots == [
        SourceReference(id="confirmed-root", href=None, reference_count=1)
    ]
    assert report.unresolved == []


def test_cycle_is_reported_once_but_reached_from_multiple_roads() -> None:
    start = make_item("start")
    road_a = make_item("road-a")
    road_b = make_item("road-b")
    gen_one = make_item("gen-1")
    gen_two = make_item("gen-2")

    start.add_derived_from(road_a, road_b)
    road_a.add_derived_from(gen_two)
    road_b.add_derived_from(gen_two)
    gen_two.add_derived_from(gen_one)
    gen_one.add_derived_from(gen_two)

    report = trace_provenance(start)

    cycle_pairs = [(edge.source_id, edge.target_id) for edge in report.cycles]
    assert cycle_pairs == [("gen-2", "gen-1"), ("gen-1", "gen-2")]
    assert report.roots == []
    assert report.unresolved == []


def test_product_without_derived_from_links() -> None:
    standalone = make_item("standalone")

    report = trace_provenance(standalone)

    assert isinstance(report, ProvenanceReport)
    assert report.root_source_count == 0
    assert report.total_reference_count == 0
    assert report.roots == []
    assert report.unresolved == []
    assert report.cycles == []


def test_report_to_dict_is_json_compatible() -> None:
    product = make_item("product")
    root = make_item("root")
    root.set_self_href("https://example.com/root.json")
    product.add_derived_from(root)

    report_dict = trace_provenance(product).to_dict()

    assert report_dict == {
        "product_id": "product",
        "root_source_count": 1,
        "total_reference_count": 1,
        "roots": [
            {
                "id": "root",
                "href": "https://example.com/root.json",
                "reference_count": 1,
            }
        ],
        "unresolved": [],
        "cycles": [],
    }
    json.dumps(report_dict)
