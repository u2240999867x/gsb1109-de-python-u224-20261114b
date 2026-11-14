"""Tests for scripts/compare_asset_hrefs.py."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from pystac import Asset, Catalog, Item

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "compare_asset_hrefs.py"
)
_SPEC = importlib.util.spec_from_file_location("compare_asset_hrefs", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_module = importlib.util.module_from_spec(_SPEC)
sys.modules["compare_asset_hrefs"] = _module
_SPEC.loader.exec_module(_module)
comparer: Any = _module

_DATETIME = datetime(2020, 1, 1, tzinfo=timezone.utc)
_GEOM = {"type": "Point", "coordinates": [0.0, 0.0]}
_BBOX = [0.0, 0.0, 0.0, 0.0]


def _make_item(item_id: str, self_href: str, assets: Dict[str, str]) -> Item:
    item = Item(item_id, _GEOM, _BBOX, _DATETIME, {})
    item.set_self_href(self_href)
    for key, href in assets.items():
        item.add_asset(key, Asset(href=href))
    return item


def _make_catalog(label: str, items: List[Item]) -> Catalog:
    catalog = Catalog(id=f"catalog-{label}", description=label)
    for item in items:
        catalog.add_item(item)
    return catalog


def test_resolve_relative_url_href_preserves_escaping_and_query() -> None:
    resolved = comparer.resolve_asset_href(
        "../../../data/im%20a.tif?token=ab%3D1#frag",
        "https://old.example.com/catalog/col/items/i/i.json",
    )
    assert resolved == (
        "https://old.example.com/catalog/data/im%20a.tif?token=ab%3D1#frag"
    )


def test_resolve_relative_local_href_preserves_query() -> None:
    resolved = comparer.resolve_asset_href(
        "../../data/im g.tif?token=q",
        "/srv/old/catalog/col/items/i/i.json",
    )
    assert resolved == "/srv/old/catalog/col/data/im g.tif?token=q"


def test_resolve_absolute_and_protocol_relative_hrefs() -> None:
    absolute = "https://other.example.com/a%20b.tif?z=%2Fx"
    assert comparer.resolve_asset_href(absolute, "https://h/i.json") == absolute

    resolved = comparer.resolve_asset_href(
        "//cdn.example.com/a.tif?x=1", "https://h/i.json"
    )
    assert resolved == "https://cdn.example.com/a.tif?x=1"


def test_resolve_relative_href_requires_item_href() -> None:
    with pytest.raises(ValueError, match="Item self HREF"):
        comparer.resolve_asset_href("../a.tif", None)


def test_parse_root_mapping_validation() -> None:
    mapping = comparer.parse_root_mapping(
        "https://old.example.com/v1=https://cdn.example.com/m"
    )
    assert mapping.old_path == "/v1"
    assert mapping.new_path == "/m"
    with pytest.raises(Exception, match="OLD_ROOT=NEW_ROOT"):
        comparer.parse_root_mapping("no-equals-sign")


def test_compare_statuses_for_paired_assets() -> None:
    old_items = [
        _make_item(
            "i1",
            "https://old.example.com/v1/items/i1/i1.json",
            {
                "visual": "https://old.example.com/v1/data/i1/visual.tif",
                "kept": "https://cdn.example.com/static/ref.tif?x=1",
                "gone": "https://old.example.com/v1/data/i1/gone.tif",
            },
        ),
        _make_item(
            "i3",
            "https://old.example.com/v1/items/i3/i3.json",
            {"band": "https://old.example.com/v1/data/i3/b.tif"},
        ),
        _make_item(
            "old-only",
            "https://old.example.com/v1/items/old-only/x.json",
            {"data": "https://old.example.com/v1/data/old-only/x.tif"},
        ),
    ]
    new_items = [
        _make_item(
            "i1",
            "https://cdn.example.com/catalog/items/i1/i1.json",
            {
                "visual": "https://cdn.example.com/mirror/data/i1/visual.tif",
                "kept": "https://cdn.example.com/static/ref.tif?x=1",
                "added": "https://cdn.example.com/mirror/data/i1/added.tif",
            },
        ),
        _make_item(
            "i3",
            "https://cdn.example.com/catalog/items/i3/i3.json",
            {"band": "https://cdn.example.com/elsewhere/b.tif"},
        ),
    ]
    mappings = [
        comparer.parse_root_mapping(
            "https://old.example.com/v1=https://cdn.example.com/mirror"
        )
    ]

    old_records = comparer.collect_assets(_make_catalog("old", old_items), "old")
    new_records = comparer.collect_assets(_make_catalog("new", new_items), "new")
    rows, summary = comparer.compare_catalogs(
        old_records, new_records, mappings, "old", "new"
    )
    by_key = {(row.item_id, row.asset_key): row for row in rows}

    assert by_key[("i1", "visual")].status == comparer.STATUS_MIRROR_MOVED
    assert by_key[("i1", "visual")].matched_old_root == ("https://old.example.com/v1")
    assert by_key[("i1", "kept")].status == comparer.STATUS_UNCHANGED
    assert by_key[("i1", "gone")].status == comparer.STATUS_ONLY_OLD
    assert by_key[("i1", "added")].status == comparer.STATUS_ONLY_NEW
    assert by_key[("i3", "band")].status == comparer.STATUS_CHANGED
    assert by_key[("old-only", "data")].status == comparer.STATUS_ONLY_OLD

    counts = summary["status_counts"]
    assert counts == {
        "changed": 1,
        "mirror-moved": 1,
        "only-new": 1,
        "only-old": 2,
        "unchanged": 1,
    }
    assert summary["item_counts"] == {"old": 3, "new": 2}
    assert summary["asset_counts"] == {"old": 5, "new": 4}
    roots = summary["source_roots"]
    assert set(roots["old"]) == {
        "https://old.example.com",
        "https://cdn.example.com",
    }
    assert roots["new"] == ["https://cdn.example.com"]
    assert summary["merged_root_groups"] == []


def test_merged_roots_flag_ambiguous_assets_without_guessing() -> None:
    mappings = [
        comparer.parse_root_mapping(
            "https://old-a.example.com/root=https://cdn.example.com/merged"
        ),
        comparer.parse_root_mapping(
            "https://old-b.example.com/root=https://cdn.example.com/merged"
        ),
    ]
    old_items = [
        _make_item(
            "scene-1",
            "https://old-a.example.com/root/items/scene-1.json",
            {"data": "https://old-a.example.com/root/scene/x.tif?token=A"},
        ),
        _make_item(
            "scene-1-b",
            "https://old-b.example.com/root/items/scene-1-b.json",
            {"data": "https://old-b.example.com/root/scene/x.tif?token=B"},
        ),
        _make_item(
            "scene-1",
            "https://old-a.example.com/root/items/scene-1-dup.json",
            {"unique": "https://old-a.example.com/root/scene/u.tif"},
        ),
    ]
    new_items = [
        _make_item(
            "scene-1",
            "https://cdn.example.com/merged/items/scene-1.json",
            {
                "data": "https://cdn.example.com/merged/scene/x.tif?token=N",
                "unique": "https://cdn.example.com/merged/scene/u.tif",
            },
        )
    ]

    old_records = comparer.collect_assets(_make_catalog("old", old_items), "old")
    new_records = comparer.collect_assets(_make_catalog("new", new_items), "new")
    rows, summary = comparer.compare_catalogs(
        old_records, new_records, mappings, "old", "new"
    )
    by_key = {(row.item_id, row.asset_key): row for row in rows}

    ambiguous_row = by_key[("scene-1", "data")]
    assert ambiguous_row.status == comparer.STATUS_AMBIGUOUS
    candidate_roots = {
        candidate["old_root"] for candidate in ambiguous_row.ambiguous_candidates
    }
    assert candidate_roots == {
        "https://old-a.example.com/root",
        "https://old-b.example.com/root",
    }
    observed = {
        href
        for candidate in ambiguous_row.ambiguous_candidates
        for href in candidate["observed_old_hrefs"]
    }
    assert observed == {
        "https://old-a.example.com/root/scene/x.tif?token=A",
        "https://old-b.example.com/root/scene/x.tif?token=B",
    }
    assert all(
        candidate["exact_href_match"] is False
        for candidate in ambiguous_row.ambiguous_candidates
    )
    assert by_key[("scene-1", "unique")].status == comparer.STATUS_MIRROR_MOVED

    ambiguous_summary = summary["ambiguous_assets"]
    assert len(ambiguous_summary) == 1
    assert ambiguous_summary[0]["new_resolved_href"] == (
        "https://cdn.example.com/merged/scene/x.tif?token=N"
    )
    assert summary["merged_root_groups"] == [
        {
            "new_root": "https://cdn.example.com/merged",
            "old_roots": [
                "https://old-a.example.com/root",
                "https://old-b.example.com/root",
            ],
        }
    ]


def test_find_ambiguities_exact_match_and_path_only_match() -> None:
    mappings = [
        comparer.parse_root_mapping(
            "https://old-a.example.com/root=https://cdn.example.com/merged"
        ),
        comparer.parse_root_mapping(
            "https://old-b.example.com/root=https://cdn.example.com/merged"
        ),
    ]

    def _record(item_id: str, href: str) -> Any:
        return comparer.AssetRecord(
            catalog_label="old",
            item_id=item_id,
            item_href=f"https://x/{item_id}.json",
            asset_key="data",
            raw_href=href,
            resolved_href=href,
        )

    exact_href = "https://old-a.example.com/root/scene/x.tif?token=same"
    old_records_list = [
        _record("a", exact_href),
        _record("b", "https://old-b.example.com/root/scene/x.tif?token=same"),
        _record("aq", "https://old-a.example.com/root/scene/q.tif?token=old-q"),
        _record("c", "https://old-b.example.com/root/scene/q.tif?token=different"),
    ]
    old_by_href: Dict[str, List[Any]] = {}
    old_by_path: Dict[Any, List[Any]] = {}
    for record in old_records_list:
        old_by_href.setdefault(record.resolved_href, []).append(record)
        old_by_path.setdefault(comparer._path_key(record.resolved_href), []).append(
            record
        )

    exact_new = [
        comparer.AssetRecord(
            "new",
            "new",
            "https://n/new.json",
            "data",
            exact_href.replace("old-a.example.com/root", "cdn.example.com/merged"),
            "https://cdn.example.com/merged/scene/x.tif?token=same",
        )
    ]
    exact = comparer.find_ambiguities(exact_new, old_by_href, old_by_path, mappings)
    assert len(exact) == 1
    assert all(
        candidate["exact_href_match"] is True
        for candidate in exact[exact_new[0].resolved_href]
    )

    query_differs_new = [
        comparer.AssetRecord(
            "new",
            "new",
            "https://n/new.json",
            "data",
            "https://cdn.example.com/merged/scene/q.tif?token=new",
            "https://cdn.example.com/merged/scene/q.tif?token=new",
        )
    ]
    path_only = comparer.find_ambiguities(
        query_differs_new, old_by_href, old_by_path, mappings
    )
    assert list(path_only) == ["https://cdn.example.com/merged/scene/q.tif?token=new"]
    path_candidates = path_only[query_differs_new[0].resolved_href]
    assert len(path_candidates) == 2
    assert all(candidate["exact_href_match"] is False for candidate in path_candidates)
    observed = {
        href
        for candidate in path_candidates
        for href in candidate["observed_old_hrefs"]
    }
    assert observed == {
        "https://old-a.example.com/root/scene/q.tif?token=old-q",
        "https://old-b.example.com/root/scene/q.tif?token=different",
    }


def _item_dict(item_id: str, assets: Dict[str, str]) -> Dict[str, Any]:
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "geometry": _GEOM,
        "bbox": _BBOX,
        "properties": {"datetime": _DATETIME.isoformat()},
        "links": [],
        "assets": {key: {"href": href} for key, href in assets.items()},
    }


def _catalog_dict(catalog_id: str, item_hrefs: List[str]) -> Dict[str, Any]:
    return {
        "type": "Catalog",
        "stac_version": "1.0.0",
        "id": catalog_id,
        "description": catalog_id,
        "links": [
            {
                "rel": "item",
                "href": href,
                "type": "application/geo+json",
            }
            for href in item_hrefs
        ],
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_catalog_pair(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    merged_root = new_root / "merged"

    old_item_rel = Path("col/items/i1/i1.json")
    _write_json(
        old_root / old_item_rel,
        _item_dict(
            "i1",
            {
                "visual": "../../../data/im%20a.tif?token=ab%3D1",
                "merged": "/srv/a/root/scene/x.tif?token=A",
            },
        ),
    )
    _write_json(
        old_root / "col/items/i1-extra/i1-extra.json",
        _item_dict(
            "i1-extra",
            {"merged": "/srv/b/root/scene/x.tif?token=B"},
        ),
    )
    _write_json(
        old_root / "catalog.json",
        _catalog_dict(
            "old-catalog",
            [
                "col/items/i1/i1.json",
                "col/items/i1-extra/i1-extra.json",
            ],
        ),
    )

    _write_json(
        new_root / "col/items/i1/i1.json",
        _item_dict(
            "i1",
            {
                "visual": "../../../data/im%20a.tif?token=ab%3D1",
                "merged": f"{merged_root}/scene/x.tif?token=N",
            },
        ),
    )
    _write_json(
        new_root / "catalog.json",
        _catalog_dict("new-catalog", ["col/items/i1/i1.json"]),
    )
    return old_root / "catalog.json", new_root / "catalog.json", old_root, merged_root


def test_cli_writes_reports_and_flags_ambiguity(tmp_path: Path) -> None:
    old_catalog_path, new_catalog_path, old_root, merged_root = _write_catalog_pair(
        tmp_path
    )
    new_root = tmp_path / "new"
    csv_path = tmp_path / "report.csv"
    json_path = tmp_path / "report.json"
    argv = [
        str(old_catalog_path),
        str(new_catalog_path),
        "--mirror",
        f"{old_root}={new_root}",
        "--mirror",
        f"/srv/a/root={merged_root}",
        "--mirror",
        f"/srv/b/root={merged_root}",
        "--csv",
        str(csv_path),
        "--json",
        str(json_path),
    ]

    exit_code = comparer.main(argv)
    assert exit_code == 0
    assert csv_path.exists()
    assert json_path.exists()

    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0].keys()) == comparer.CSV_FIELDS
    by_key = {(row["item_id"], row["asset_key"]): row for row in rows}

    visual = by_key[("i1", "visual")]
    assert visual["old_asset_href_raw"] == "../../../data/im%20a.tif?token=ab%3D1"
    assert visual["old_resolved_href"] == (f"{old_root}/data/im%20a.tif?token=ab%3D1")
    assert visual["new_resolved_href"] == (f"{new_root}/data/im%20a.tif?token=ab%3D1")
    assert visual["status"] == comparer.STATUS_MIRROR_MOVED

    merged = by_key[("i1", "merged")]
    assert merged["status"] == comparer.STATUS_AMBIGUOUS
    candidates = json.loads(merged["ambiguous_candidates"])
    assert len(candidates) == 2
    assert by_key[("i1-extra", "merged")]["status"] == comparer.STATUS_ONLY_OLD

    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["summary"]["ambiguous_assets"][0]["new_resolved_href"] == (
        f"{merged_root}/scene/x.tif?token=N"
    )

    fail_code = comparer.main(argv + ["--fail-on-ambiguous"])
    assert fail_code == 3
