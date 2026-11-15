"""Find the minimum-byte set of STAC assets covering required bands plus
grid-compatible quality masks.

Rules implemented:
- Coverage: every required band must be present in the eo:bands of at least
  one selected data asset (multi-band files cover several bands at once).
- Grid constraint: for every required band, at least one selected mask must
  share the exact grid (proj:epsg + proj:shape + proj:transform) of a
  selected asset covering that band. A smaller mask on a different grid is
  not acceptable.
- Pay once: assets pointing to the same provable object (same file:checksum,
  falling back to same href) are charged only once, however many asset
  entries / paths reference them.
"""

import itertools
import json
import sys

MASK_ROLES = {"data-mask", "cloud", "cloud-shadow", "snow-ice", "water-mask"}


def load_item(path):
    with open(path) as f:
        return json.load(f)


def asset_grid(item, asset):
    return (
        asset.get("proj:epsg", item.get("properties", {}).get("proj:epsg")),
        tuple(asset.get("proj:shape", [])),
        tuple(asset.get("proj:transform", [])),
    )


def object_key(asset):
    # Same provable object => pay once.
    return asset.get("file:checksum") or asset["href"]


def is_mask(asset):
    return bool(MASK_ROLES & set(asset.get("roles", []))) and not asset.get("eo:bands")


def band_names(asset):
    return {b.get("common_name") or b.get("name") for b in asset.get("eo:bands", [])}


def solve(item, required_bands):
    assets = item["assets"]
    data_assets = {k: a for k, a in assets.items() if a.get("eo:bands")}
    mask_assets = {k: a for k, a in assets.items() if is_mask(a)}
    keys = list(assets)

    best = None
    for r in range(1, len(keys) + 1):
        for combo in itertools.combinations(keys, r):
            sel = {k: assets[k] for k in combo}
            sel_data = {k: a for k, a in sel.items() if k in data_assets}
            sel_masks = {k: a for k, a in sel.items() if k in mask_assets}
            if not sel_masks:
                continue
            ok = True
            for band in required_bands:
                covers = [a for a in sel_data.values() if band in band_names(a)]
                if not covers:
                    ok = False
                    break
                grids = {asset_grid(item, a) for a in covers}
                if not any(asset_grid(item, m) in grids for m in sel_masks.values()):
                    ok = False
                    break
            if not ok:
                continue
            seen = {}
            for k in combo:
                seen.setdefault(object_key(assets[k]), k)
            cost = sum(assets[k]["file:size"] for k in seen.values())
            if best is None or cost < best[0]:
                best = (cost, combo)
    return best


def report(item, required_bands, cost, combo):
    assets = item["assets"]
    sel = {k: assets[k] for k in combo}
    print(f"required bands : {', '.join(required_bands)}")
    print(f"selected assets: {', '.join(combo)}")
    seen = {}
    for k in combo:
        key = object_key(assets[k])
        if key in seen:
            print(f"  {k}: same object as {seen[key]} (checksum/href match) -> 0 extra bytes")
        else:
            seen[key] = k
            print(f"  {k}: {assets[k]['file:size']:,} bytes")
    print(f"total download : {cost:,} bytes")
    print()
    for band in required_bands:
        covers = [k for k, a in sel.items() if band in band_names(a)]
        grids = {asset_grid(item, sel[c]) for c in covers}
        masks = [k for k, a in sel.items() if is_mask(a) and asset_grid(item, a) in grids]
        print(f"  band {band:<6} <- {', '.join(covers)} ; mask <- {', '.join(masks)}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "solution/item.json"
    required = sys.argv[2:] or ["red", "green", "blue"]
    item = load_item(path)
    best = solve(item, required)
    if best is None:
        print("no feasible selection")
        sys.exit(1)
    report(item, required, *best)

    # Show why the cheaper-but-incompatible mask cannot win.
    masks = {k: a for k, a in item["assets"].items() if is_mask(a)}
    print()
    print("mask grid check:")
    for k, m in masks.items():
        grid = asset_grid(item, m)
        print(f"  {k}: {m['file:size']:>12,} bytes  grid={grid[1]} @{grid[2][0]}m")


if __name__ == "__main__":
    main()
