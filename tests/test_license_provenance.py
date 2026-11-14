from datetime import datetime, timezone

import pytest

import pystac
from pystac import (
    Asset,
    Collection,
    Extent,
    Link,
    RelType,
    SpatialExtent,
    TemporalExtent,
)
from pystac.license_provenance import (
    SourceKind,
    collect_license_statements,
)


def make_collection(license: str = "CC-BY-4.0") -> Collection:
    extent = Extent(
        SpatialExtent([[-180.0, -90.0, 180.0, 90.0]]),
        TemporalExtent([[datetime(2020, 1, 1, tzinfo=timezone.utc), None]]),
    )
    return Collection(
        id="research-collection",
        description="A research dataset",
        license=license,
        extent=extent,
    )


def test_collects_collection_license_link_and_asset_statements() -> None:
    collection = make_collection()
    collection.add_link(Link(RelType.LICENSE, "https://example.com/CC-BY-4.0"))
    collection.add_asset(
        "data",
        Asset(
            href="data.zarr",
            roles=["data"],
            extra_fields={"license": "CC-BY-4.0"},
        ),
    )

    report = collect_license_statements(collection)

    kinds = {s.source.kind for s in report.statements}
    assert kinds == {
        SourceKind.COLLECTION_LICENSE,
        SourceKind.LICENSE_LINK,
        SourceKind.ASSET_FIELD,
    }
    link_statement = next(
        s for s in report.statements if s.source.kind == SourceKind.LICENSE_LINK
    )
    assert link_statement.source.href == "https://example.com/CC-BY-4.0"
    asset_statement = next(
        s for s in report.statements if s.source.kind == SourceKind.ASSET_FIELD
    )
    assert asset_statement.source.asset_key == "data"
    assert asset_statement.source.field == "license"
    assert asset_statement.roles == ("data",)


def test_asset_statements_are_scoped_by_declared_roles() -> None:
    collection = make_collection()
    collection.add_asset(
        "thumbnail",
        Asset(
            href="thumb.png",
            roles=["thumbnail"],
            description="Preview only; not for redistribution",
        ),
    )
    collection.add_asset(
        "measurements",
        Asset(href="data.zarr", roles=["data"], extra_fields={"license": "CC0-1.0"}),
    )

    data_report = collect_license_statements(collection, roles=["data"])
    data_texts = [s.text for s in data_report.statements]
    assert "Preview only; not for redistribution" not in data_texts
    assert "CC0-1.0" in data_texts

    thumbnail_report = collect_license_statements(collection, roles=["thumbnail"])
    thumbnail_texts = [s.text for s in thumbnail_report.statements]
    assert "Preview only; not for redistribution" in thumbnail_texts
    assert "CC0-1.0" not in thumbnail_texts


def test_unknown_assets_are_reported_when_license_is_various() -> None:
    collection = make_collection(license="various")
    collection.add_asset(
        "documented", Asset(href="a.tif", extra_fields={"license": "MIT"})
    )
    collection.add_asset("undocumented", Asset(href="b.tif"))

    report = collect_license_statements(collection)

    assert report.unknown_assets == ["undocumented"]


def test_no_unknowns_when_collection_license_covers_assets() -> None:
    collection = make_collection(license="proprietary")
    collection.add_asset("data", Asset(href="data.zarr"))

    report = collect_license_statements(collection)

    assert report.unknown_assets == []


def test_contradictory_statements_are_preserved_as_conflicts() -> None:
    collection = make_collection(license="CC-BY-4.0")
    collection.add_asset(
        "data",
        Asset(
            href="data.zarr", roles=["data"], extra_fields={"license": "CC-BY-NC-4.0"}
        ),
    )

    report = collect_license_statements(collection)
    conflicts = report.conflicts()

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.asset_key == "data"
    assert set(conflict.texts) == {"CC-BY-4.0", "CC-BY-NC-4.0"}
    # both contradictory statements are kept, each with its own source
    assert {s.source.kind for s in conflict.statements} == {
        SourceKind.COLLECTION_LICENSE,
        SourceKind.ASSET_FIELD,
    }


def test_various_license_does_not_conflict_with_asset_licenses() -> None:
    collection = make_collection(license="various")
    collection.add_asset(
        "data", Asset(href="a.tif", extra_fields={"license": "MIT"})
    )

    report = collect_license_statements(collection)

    assert report.conflicts() == []


def test_editing_thumbnail_does_not_change_collected_data_report() -> None:
    collection = make_collection()
    collection.add_asset(
        "thumbnail",
        Asset(href="thumb.png", roles=["thumbnail"], description="Draft preview"),
    )
    collection.add_asset(
        "measurements",
        Asset(href="data.zarr", roles=["data"], extra_fields={"license": "CC0-1.0"}),
    )

    data_report = collect_license_statements(collection, roles=["data"])
    before = data_report.to_dict()

    collection.assets["thumbnail"].description = "Updated preview terms"

    # the already-collected data report is a snapshot and does not change
    assert data_report.to_dict() == before
    assert all(
        s.source.asset_key != "thumbnail" for s in data_report.statements
    )

    # a fresh thumbnail report sees the new text and traces to its own asset
    thumbnail_report = collect_license_statements(collection, roles=["thumbnail"])
    thumbnail_statements = [
        s for s in thumbnail_report.statements if s.source.asset_key == "thumbnail"
    ]
    assert [s.text for s in thumbnail_statements] == ["Updated preview terms"]
    assert all(
        s.source.object_id == "research-collection" for s in thumbnail_statements
    )


def test_summary_contains_only_evidence_backed_statements() -> None:
    collection = make_collection(license="various")
    collection.add_asset(
        "documented", Asset(href="a.tif", extra_fields={"license": "MIT"})
    )
    collection.add_asset("undocumented", Asset(href="b.tif"))

    report = collect_license_statements(collection)
    summary = report.summary()

    assert set(summary) == {"various", "MIT"}
    assert "undocumented" in report.unknown_assets
    assert summary["MIT"] == [
        {
            "kind": "asset_field",
            "object_id": "research-collection",
            "asset_key": "documented",
            "field": "license",
        }
    ]


def test_report_to_dict_is_json_roundtrippable() -> None:
    import json

    collection = make_collection()
    collection.add_asset(
        "data", Asset(href="a.tif", extra_fields={"license": "CC-BY-NC-4.0"})
    )

    report = collect_license_statements(collection)
    d = report.to_dict()

    assert json.loads(json.dumps(d)) == d
    assert d["object_id"] == "research-collection"
    assert d["roles"] is None
    assert len(d["conflicts"]) == 1


def test_exported_from_top_level_package() -> None:
    assert pystac.collect_license_statements is collect_license_statements
    assert pystac.LicenseReport is not None
    assert pystac.LicenseStatement is not None


@pytest.mark.parametrize("role", ["data", "thumbnail"])
def test_collection_wide_statements_apply_to_every_declared_role(role: str) -> None:
    collection = make_collection(license="Apache-2.0")
    collection.add_asset("asset", Asset(href="a.tif", roles=[role]))

    report = collect_license_statements(collection, roles=[role])

    collection_wide = [
        s for s in report.statements if s.source.kind == SourceKind.COLLECTION_LICENSE
    ]
    assert len(collection_wide) == 1
    assert collection_wide[0].applies_to([role])
