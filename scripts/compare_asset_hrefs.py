#!/usr/bin/env python3
"""Compare asset HREFs between two versions of a STAC catalog.

The two catalog versions are walked independently. Every asset HREF is
resolved against the self HREF of the Item that carries it, so relative
HREFs are interpreted from each catalog's own layout. Resolution never
quotes, unquotes or normalizes percent-escaped path segments, and query
strings and fragments are preserved verbatim (including for local paths).

Items are paired by ID and assets within an Item by asset key. When files
moved together with their directory, ``--mirror OLD_ROOT=NEW_ROOT``
mappings translate the old absolute HREF into the expected new one. If two
old roots merge onto a single new root, assets whose new HREF could have
been replaced by more than one old asset are reported as *ambiguous* with
every candidate listed instead of guessing which file replaced which.

Outputs a CSV row per asset (paired or one-sided) and/or a JSON report with
counts, source roots, merged-root groups and the ambiguous assets.

Example:
    python scripts/compare_asset_hrefs.py \
        /srv/old/catalog.json /srv/new/catalog.json \
        --mirror https://old.example.com/a=https://cdn.example.com/m \
        --mirror https://old.example.com/b=https://cdn.example.com/m \
        --csv report.csv --json report.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import posixpath
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlunparse

import pystac
from pystac.utils import make_posix_style, safe_urlparse

STATUS_UNCHANGED = "unchanged"
STATUS_MIRROR_MOVED = "mirror-moved"
STATUS_CHANGED = "changed"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_ONLY_OLD = "only-old"
STATUS_ONLY_NEW = "only-new"

CSV_FIELDS = [
    "status",
    "item_id",
    "asset_key",
    "source_catalog",
    "old_item_href",
    "new_item_href",
    "old_asset_href_raw",
    "new_asset_href_raw",
    "old_resolved_href",
    "new_resolved_href",
    "matched_old_root",
    "new_root",
    "ambiguous_candidates",
    "note",
]


@dataclass(frozen=True)
class RootMapping:
    """A mirrored directory root, parsed into URL/path components."""

    old_scheme: str
    old_netloc: str
    old_path: str
    new_scheme: str
    new_netloc: str
    new_path: str
    raw_old: str
    raw_new: str

    @property
    def new_root_key(self) -> Tuple[str, str, str]:
        return (self.new_scheme, self.new_netloc, self.new_path.rstrip("/"))


@dataclass
class AssetRecord:
    """One asset as observed in one catalog version."""

    catalog_label: str
    item_id: str
    item_href: Optional[str]
    asset_key: str
    raw_href: str
    resolved_href: str


@dataclass
class ComparisonRow:
    """The paired or one-sided result for one asset."""

    status: str
    item_id: str
    asset_key: str
    source_catalog: str
    old_item_href: Optional[str] = None
    new_item_href: Optional[str] = None
    old_asset_href_raw: Optional[str] = None
    new_asset_href_raw: Optional[str] = None
    old_resolved_href: Optional[str] = None
    new_resolved_href: Optional[str] = None
    matched_old_root: Optional[str] = None
    new_root: Optional[str] = None
    ambiguous_candidates: List[Dict[str, object]] = field(default_factory=list)
    note: str = ""

    def as_csv_dict(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for key in CSV_FIELDS:
            value = getattr(self, key)
            if key == "ambiguous_candidates":
                result[key] = json.dumps(value, ensure_ascii=False)
            elif value is None:
                result[key] = ""
            else:
                result[key] = str(value)
        return result


def _is_absolute(parsed: object) -> bool:
    path = getattr(parsed, "path", "")
    scheme = getattr(parsed, "scheme", "")
    return bool(scheme) or (bool(path) and os.path.isabs(path))


def _rebuild(
    scheme: str,
    netloc: str,
    path: str,
    params: str,
    query: str,
    fragment: str,
) -> str:
    if not scheme and not netloc:
        # Local path: keep the bare path and append every byte of the
        # params/query/fragment so nothing is dropped on disk paths.
        result = path
        if params:
            result += f";{params}"
        if query:
            result += f"?{query}"
        if fragment:
            result += f"#{fragment}"
        return result
    return urlunparse((scheme, netloc, path, params, query, fragment))


def resolve_asset_href(asset_href: str, item_href: Optional[str]) -> str:
    """Resolve an asset HREF against its Item's self HREF.

    Relative HREFs are joined to the Item's directory using POSIX path
    semantics for both URLs and local paths. Percent escaping is left
    untouched and the asset's params, query string and fragment are carried
    over, including for local file paths.
    """

    normalized = make_posix_style(asset_href)
    asset = safe_urlparse(normalized)

    if item_href is None:
        if _is_absolute(asset) or asset.netloc:
            return normalized
        raise ValueError(
            f"Cannot resolve relative asset HREF {asset_href!r} without an "
            "Item self HREF"
        )

    base = safe_urlparse(make_posix_style(item_href))
    if asset.netloc:
        # Protocol-relative HREF: borrow the Item scheme, keep the rest.
        return _rebuild(
            base.scheme,
            asset.netloc,
            asset.path,
            asset.params,
            asset.query,
            asset.fragment,
        )

    if _is_absolute(asset):
        return normalized

    base_dir = posixpath.dirname(base.path)
    resolved_path = (
        posixpath.normpath(posixpath.join(base_dir, asset.path))
        if asset.path
        else base_dir
    )
    return _rebuild(
        base.scheme,
        base.netloc,
        resolved_path,
        asset.params,
        asset.query,
        asset.fragment,
    )


def _path_key(href: str) -> Tuple[str, str, str, str]:
    """Identity of an HREF excluding its query string and fragment."""

    parsed = safe_urlparse(href)
    return (parsed.scheme, parsed.netloc, parsed.path, parsed.params)


def parse_root_mapping(spec: str) -> RootMapping:
    """Parse an ``OLD_ROOT=NEW_ROOT`` mirror specification."""

    if "=" not in spec:
        raise argparse.ArgumentTypeError(
            f"Mirror mapping {spec!r} must look like OLD_ROOT=NEW_ROOT"
        )
    raw_old, raw_new = spec.split("=", 1)
    if not raw_old or not raw_new:
        raise argparse.ArgumentTypeError(
            f"Mirror mapping {spec!r} has an empty old or new root"
        )
    old = safe_urlparse(make_posix_style(raw_old))
    new = safe_urlparse(make_posix_style(raw_new))
    return RootMapping(
        old_scheme=old.scheme,
        old_netloc=old.netloc,
        old_path=old.path,
        new_scheme=new.scheme,
        new_netloc=new.netloc,
        new_path=new.path,
        raw_old=raw_old,
        raw_new=raw_new,
    )


def _path_under(path: str, prefix: str) -> bool:
    root = prefix.rstrip("/")
    if not root:
        return True
    if root == "/":
        return path.startswith("/")
    return path == root or path.startswith(root + "/")


def match_root(href: str, mappings: Sequence[RootMapping]) -> Optional[RootMapping]:
    """Return the longest old root that contains ``href``."""

    parsed = safe_urlparse(href)
    best: Optional[RootMapping] = None
    for mapping in mappings:
        if (
            parsed.scheme == mapping.old_scheme
            and parsed.netloc == mapping.old_netloc
            and _path_under(parsed.path, mapping.old_path)
        ):
            if best is None or len(mapping.old_path) > len(best.old_path):
                best = mapping
    return best


def translate_href(href: str, mapping: RootMapping) -> str:
    """Rewrite the root of ``href`` according to ``mapping``.

    The path suffix, params, query and fragment are preserved byte for byte.
    """

    parsed = safe_urlparse(href)
    cut = mapping.old_path.rstrip("/")
    suffix = parsed.path[len(cut) :] if cut else parsed.path
    new_path = mapping.new_path.rstrip("/") + suffix
    if mapping.new_scheme and mapping.new_netloc and not new_path.startswith("/"):
        new_path = "/" + new_path
    return urlunparse(
        (
            mapping.new_scheme,
            mapping.new_netloc,
            new_path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def _iter_items(catalog: pystac.Catalog) -> Iterable[pystac.Item]:
    if isinstance(catalog, pystac.Item):
        yield catalog
        return
    for _, _, items in catalog.walk():
        yield from items


def collect_assets(catalog: pystac.Catalog, label: str) -> Dict[str, List[AssetRecord]]:
    """Walk one catalog and return records grouped by Item ID."""

    records: Dict[str, List[AssetRecord]] = {}
    for item in _iter_items(catalog):
        item_href = item.self_href
        item_records = records.setdefault(item.id, [])
        for key, asset in item.assets.items():
            item_records.append(
                AssetRecord(
                    catalog_label=label,
                    item_id=item.id,
                    item_href=item_href,
                    asset_key=key,
                    raw_href=asset.href,
                    resolved_href=resolve_asset_href(asset.href, item_href),
                )
            )
    return records


def iter_item_documents(
    root_href: str, stac_io: Optional["pystac.StacIO"] = None
) -> Iterable[Tuple[Dict[str, object], str]]:
    """Yield ``(item_document, item_self_href)`` pairs from raw JSON.

    Catalog and Collection documents are followed through their ``child``
    and ``item`` links. Reading the raw documents (instead of the pystac
    object graph) keeps the original asset HREF bytes, including query
    strings on relative local paths that pystac rewrites when an Item gets
    its self HREF.
    """

    io = stac_io or pystac.StacIO.default()
    queue: List[str] = [root_href]
    seen: set[str] = set()
    while queue:
        href = queue.pop(0)
        if href in seen:
            continue
        seen.add(href)
        document = io.read_json(href)
        assert isinstance(document, dict)
        doc_type = str(document.get("type", "")).lower()
        if doc_type == "feature":
            yield document, href
            continue
        if doc_type == "featurecollection":
            features = document.get("features", [])
            if isinstance(features, list):
                for feature in features:
                    if isinstance(feature, dict):
                        yield feature, href
            continue
        for link in document.get("links", []):
            if not isinstance(link, dict):
                continue
            if link.get("rel") not in ("child", "item"):
                continue
            target = link.get("href")
            if not target or not isinstance(target, str):
                continue
            queue.append(resolve_asset_href(target, href))


def collect_assets_from_href(
    root_href: str, label: str, stac_io: Optional["pystac.StacIO"] = None
) -> Dict[str, List[AssetRecord]]:
    """Walk raw catalog JSON rooted at ``root_href`` and collect records."""

    records: Dict[str, List[AssetRecord]] = {}
    for document, item_href in iter_item_documents(root_href, stac_io):
        item_id = str(document.get("id", ""))
        assets = document.get("assets", {})
        item_records = records.setdefault(item_id, [])
        if not isinstance(assets, dict):
            continue
        for key, asset in assets.items():
            if not isinstance(asset, dict) or "href" not in asset:
                continue
            raw_href = str(asset["href"])
            item_records.append(
                AssetRecord(
                    catalog_label=label,
                    item_id=item_id,
                    item_href=item_href,
                    asset_key=str(key),
                    raw_href=raw_href,
                    resolved_href=resolve_asset_href(raw_href, item_href),
                )
            )
    return records


def _index_by_key(
    records: Dict[str, List[AssetRecord]],
) -> Dict[str, Dict[str, AssetRecord]]:
    return {
        item_id: {record.asset_key: record for record in item_records}
        for item_id, item_records in records.items()
    }


def _source_roots(records: Sequence[AssetRecord]) -> List[str]:
    roots = set()
    for record in records:
        parsed = safe_urlparse(record.resolved_href)
        if parsed.scheme:
            roots.add(f"{parsed.scheme}://{parsed.netloc}")
            continue
        parts = parsed.path.split("/")
        head = parts[1] if parsed.path.startswith("/") and len(parts) > 1 else ""
        roots.add(f"local-path:{head or '/'}")
    return sorted(roots)


def _merged_root_groups(mappings: Sequence[RootMapping]) -> List[List[RootMapping]]:
    groups: Dict[Tuple[str, str, str], List[RootMapping]] = {}
    for mapping in mappings:
        groups.setdefault(mapping.new_root_key, []).append(mapping)
    return [group for group in groups.values() if len(group) >= 2]


def find_ambiguities(
    new_records: Sequence[AssetRecord],
    old_by_href: Dict[str, List[AssetRecord]],
    old_by_path: Dict[Tuple[str, str, str, str], List[AssetRecord]],
    mappings: Sequence[RootMapping],
) -> Dict[str, List[Dict[str, object]]]:
    """Find new HREFs reachable from two or more merged old roots.

    A merged root is a new root that several old roots map to. For every new
    asset under such a root, the candidate old HREF under each merged old
    root is rebuilt and checked against the assets that actually exist.
    Candidates match on full HREF, or on path alone when only the query
    string or fragment differs. Two or more existing candidates mean the
    replacement cannot be decided.
    """

    merged_groups = _merged_root_groups(mappings)
    ambiguities: Dict[str, List[Dict[str, object]]] = {}
    for record in new_records:
        parsed = safe_urlparse(record.resolved_href)
        for group in merged_groups:
            target_scheme, target_netloc, target_path = group[0].new_root_key
            if (
                parsed.scheme != target_scheme
                or parsed.netloc != target_netloc
                or not _path_under(parsed.path, target_path)
            ):
                continue
            cut = target_path.rstrip("/")
            suffix = parsed.path[len(cut) :] if cut else parsed.path
            candidates: List[Dict[str, object]] = []
            for mapping in group:
                candidate_path = mapping.old_path.rstrip("/") + suffix
                candidate = urlunparse(
                    (
                        mapping.old_scheme,
                        mapping.old_netloc,
                        candidate_path,
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                )
                sources = old_by_href.get(candidate)
                exact = True
                if sources is None:
                    sources = old_by_path.get(
                        (
                            mapping.old_scheme,
                            mapping.old_netloc,
                            candidate_path,
                            parsed.params,
                        )
                    )
                    exact = False
                if sources is None:
                    continue
                candidates.append(
                    {
                        "old_root": mapping.raw_old,
                        "candidate_old_href": candidate,
                        "exact_href_match": exact,
                        "observed_old_hrefs": sorted(
                            source.resolved_href for source in sources
                        ),
                        "sources": [
                            {
                                "item_id": source.item_id,
                                "asset_key": source.asset_key,
                                "item_href": source.item_href,
                            }
                            for source in sources
                        ],
                    }
                )
            if len(candidates) >= 2:
                ambiguities[record.resolved_href] = candidates
    return ambiguities


def compare_catalogs(
    old_records: Dict[str, List[AssetRecord]],
    new_records: Dict[str, List[AssetRecord]],
    mappings: Sequence[RootMapping],
    old_label: str,
    new_label: str,
) -> Tuple[List[ComparisonRow], Dict[str, object]]:
    """Build the full comparison, including merged-root ambiguities."""

    old_by_key = _index_by_key(old_records)
    new_by_key = _index_by_key(new_records)
    old_flat = [record for group in old_records.values() for record in group]
    new_flat = [record for group in new_records.values() for record in group]
    old_by_href: Dict[str, List[AssetRecord]] = {}
    old_by_path: Dict[Tuple[str, str, str, str], List[AssetRecord]] = {}
    for record in old_flat:
        old_by_href.setdefault(record.resolved_href, []).append(record)
        old_by_path.setdefault(_path_key(record.resolved_href), []).append(record)

    ambiguities = find_ambiguities(new_flat, old_by_href, old_by_path, mappings)

    rows: List[ComparisonRow] = []
    for item_id in sorted(set(old_by_key) | set(new_by_key)):
        old_assets = old_by_key.get(item_id, {})
        new_assets = new_by_key.get(item_id, {})
        for key in sorted(set(old_assets) | set(new_assets)):
            rows.append(
                _compare_asset(
                    item_id,
                    key,
                    old_assets.get(key),
                    new_assets.get(key),
                    mappings,
                    ambiguities,
                    old_label,
                    new_label,
                )
            )

    summary = _build_summary(
        rows, old_flat, new_flat, mappings, ambiguities, old_label, new_label
    )
    return rows, summary


def _compare_asset(
    item_id: str,
    key: str,
    old_record: Optional[AssetRecord],
    new_record: Optional[AssetRecord],
    mappings: Sequence[RootMapping],
    ambiguities: Dict[str, List[Dict[str, object]]],
    old_label: str,
    new_label: str,
) -> ComparisonRow:
    if old_record is None:
        assert new_record is not None
        return ComparisonRow(
            status=STATUS_ONLY_NEW,
            item_id=item_id,
            asset_key=key,
            source_catalog=new_label,
            new_item_href=new_record.item_href,
            new_asset_href_raw=new_record.raw_href,
            new_resolved_href=new_record.resolved_href,
            note="Asset only exists in the new catalog",
        )
    if new_record is None:
        return ComparisonRow(
            status=STATUS_ONLY_OLD,
            item_id=item_id,
            asset_key=key,
            source_catalog=old_label,
            old_item_href=old_record.item_href,
            old_asset_href_raw=old_record.raw_href,
            old_resolved_href=old_record.resolved_href,
            note="Asset only exists in the old catalog",
        )

    row = ComparisonRow(
        status=STATUS_CHANGED,
        item_id=item_id,
        asset_key=key,
        source_catalog=f"{old_label} -> {new_label}",
        old_item_href=old_record.item_href,
        new_item_href=new_record.item_href,
        old_asset_href_raw=old_record.raw_href,
        new_asset_href_raw=new_record.raw_href,
        old_resolved_href=old_record.resolved_href,
        new_resolved_href=new_record.resolved_href,
    )

    candidates = ambiguities.get(new_record.resolved_href)
    if candidates is not None:
        row.status = STATUS_AMBIGUOUS
        row.ambiguous_candidates = candidates
        row.note = (
            "Multiple old assets resolve to this new HREF through merged "
            "roots; no replacement was assumed"
        )
        return row

    if old_record.resolved_href == new_record.resolved_href:
        row.status = STATUS_UNCHANGED
        row.note = "Resolved HREF is identical in both catalogs"
        return row

    mapping = match_root(old_record.resolved_href, mappings)
    if mapping is not None:
        translated = translate_href(old_record.resolved_href, mapping)
        row.matched_old_root = mapping.raw_old
        row.new_root = mapping.raw_new
        if translated == new_record.resolved_href:
            row.status = STATUS_MIRROR_MOVED
            row.note = "Asset moved together with its mirrored directory root"
            return row
        if _path_key(translated) == _path_key(new_record.resolved_href):
            row.status = STATUS_MIRROR_MOVED
            row.note = (
                "Asset moved with its mirrored directory root, but the query "
                "string or fragment differs between versions"
            )
            return row

    row.note = "Resolved HREFs differ and no mirror mapping explains the change"
    return row


def _build_summary(
    rows: Sequence[ComparisonRow],
    old_flat: Sequence[AssetRecord],
    new_flat: Sequence[AssetRecord],
    mappings: Sequence[RootMapping],
    ambiguities: Dict[str, List[Dict[str, object]]],
    old_label: str,
    new_label: str,
) -> Dict[str, object]:
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1

    merged = [
        {
            "new_root": group[0].raw_new,
            "old_roots": [mapping.raw_old for mapping in group],
        }
        for group in _merged_root_groups(mappings)
    ]
    return {
        "old_catalog": old_label,
        "new_catalog": new_label,
        "item_counts": {
            "old": len({record.item_id for record in old_flat}),
            "new": len({record.item_id for record in new_flat}),
        },
        "asset_counts": {"old": len(old_flat), "new": len(new_flat)},
        "status_counts": dict(sorted(counts.items())),
        "source_roots": {
            old_label: _source_roots(old_flat),
            new_label: _source_roots(new_flat),
        },
        "mirror_mappings": [
            {"old_root": mapping.raw_old, "new_root": mapping.raw_new}
            for mapping in mappings
        ],
        "merged_root_groups": merged,
        "ambiguous_assets": [
            {"new_resolved_href": href, "candidates": candidates}
            for href, candidates in sorted(ambiguities.items())
        ],
    }


def write_csv(rows: Sequence[ComparisonRow], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_csv_dict())


def write_json_report(
    rows: Sequence[ComparisonRow], summary: Dict[str, object], path: str
) -> None:
    payload = {
        "summary": summary,
        "assets": [
            {field_name: getattr(row, field_name) for field_name in CSV_FIELDS}
            for row in rows
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare asset HREFs between two STAC catalog versions, resolving "
            "relative HREFs from each Item's own address."
        )
    )
    parser.add_argument("old_catalog", help="Path or URL to the old catalog.json")
    parser.add_argument("new_catalog", help="Path or URL to the new catalog.json")
    parser.add_argument(
        "--mirror",
        action="append",
        default=[],
        metavar="OLD_ROOT=NEW_ROOT",
        help=(
            "Mirrored directory root mapping; may be given multiple times. "
            "Two old roots mapping to one new root triggers ambiguity checks."
        ),
    )
    parser.add_argument("--label-old", default=None, help="Label for the old catalog")
    parser.add_argument("--label-new", default=None, help="Label for the new catalog")
    parser.add_argument("--csv", dest="csv_path", help="Write the per-asset CSV here")
    parser.add_argument(
        "--json", dest="json_path", help="Write the full JSON report here"
    )
    parser.add_argument(
        "--fail-on-ambiguous",
        action="store_true",
        help="Exit with code 3 when merged-root ambiguities are found",
    )
    return parser


def _print_summary(
    summary: Dict[str, object],
    csv_path: Optional[str],
    json_path: Optional[str],
) -> None:
    print(f"Old catalog: {summary['old_catalog']}")
    print(f"New catalog: {summary['new_catalog']}")
    print("Status counts:")
    status_counts = summary["status_counts"]
    assert isinstance(status_counts, dict)
    for status, count in status_counts.items():
        print(f"  {status}: {count}")
    ambiguous = summary["ambiguous_assets"]
    assert isinstance(ambiguous, list)
    if ambiguous:
        print(f"WARNING: {len(ambiguous)} ambiguous new asset HREF(s):")
        for entry in ambiguous:
            assert isinstance(entry, dict)
            print(f"  - {entry['new_resolved_href']}")
            for candidate in entry["candidates"]:
                assert isinstance(candidate, dict)
                print(
                    "      candidate: "
                    f"{candidate['candidate_old_href']} (from {candidate['old_root']})"
                )
    if csv_path:
        print(f"CSV report: {csv_path}")
    if json_path:
        print(f"JSON report: {json_path}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    mappings = [parse_root_mapping(spec) for spec in args.mirror]
    old_label = args.label_old or args.old_catalog
    new_label = args.label_new or args.new_catalog

    old_records = collect_assets_from_href(args.old_catalog, old_label)
    new_records = collect_assets_from_href(args.new_catalog, new_label)

    rows, summary = compare_catalogs(
        old_records, new_records, mappings, old_label, new_label
    )

    if args.csv_path:
        write_csv(rows, args.csv_path)
    if args.json_path:
        write_json_report(rows, summary, args.json_path)

    _print_summary(summary, csv_path=args.csv_path, json_path=args.json_path)

    if args.fail_on_ambiguous and summary["ambiguous_assets"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
