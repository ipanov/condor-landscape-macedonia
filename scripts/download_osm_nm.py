#!/usr/bin/env python3
"""
Tiled OpenStreetMap downloader for the **full North Macedonia** Condor landscape.

The NM bbox is ~250x170 km -- far too large for a single Overpass query (it would
time out / exceed the memory guard).  This script tiles the landscape WGS84 bbox
into a grid of small sub-bboxes, queries every layer per tile (rotating across
Overpass mirrors), then merges + de-duplicates by OSM element id and writes one
cached GeoJSON per layer into ``.sandbox/osm_nm/`` (or ``.sandbox/osm/`` for the
Skopje pilot).

Layers produced (consumed by generate_forest_maps.py / bake_water.py /
generate_apt.py / generate_flight_planner_map.py):

  water.geojson         natural=water / riverbank / reservoir polygons (lakes etc.)
  forest.geojson        landuse=forest / natural=wood polygons (with leaf_type tags)
  waterways.geojson     river/stream/canal/drain/ditch LINES
  roads.geojson         highway polygons (closed ways only -- legacy)
  roads_lines.geojson   highway centre LINES (what the forest pipeline buffers)
  railways.geojson      railway polygons (legacy) + railways_lines.geojson LINES
  buildings.geojson     building polygons
  runways.geojson       aeroway=runway/taxiway/apron polygons
  settlements.geojson   landuse=residential/commercial/industrial/retail polygons
  aerodromes.json       aeroway=aerodrome points (name/icao/ele tags) for airports

Run (NM):   CONDOR_LANDSCAPE=nm python scripts/download_osm_nm.py
Run (test): CONDOR_LANDSCAPE=nm python scripts/download_osm_nm.py --tiles 0,0

It is resumable: each (layer, tile) raw response is cached under
``.sandbox/osm_nm/_tiles/`` so a re-run only fetches what is missing.  Layers can
be fetched concurrently with ``--workers`` (default 4) to saturate the network.
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent))
from condor_grid import (
    LANDSCAPE_NAME,
    ULXMAP,
    ULYMAP,
    PATCHES_X,
    PATCHES_Y,
    PATCH_SIZE_M,
    UTM_CRS,
    WGS84_CRS,
)
from osm_io import (
    query_overpass,
    osm_json_to_geojson,
    osm_json_to_geojson_lines,
    osm_json_to_points,
    save_geojson,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OSM_SUBDIR = "osm_nm" if LANDSCAPE_NAME == "NorthMacedonia" else "osm"
OUT_DIR = PROJECT_ROOT / ".sandbox" / _OSM_SUBDIR
TILE_CACHE = OUT_DIR / "_tiles"

# Tile size for the DENSE layers (buildings/roads/forest).  Measured: a ~0.93 deg
# chunk returns 32k buildings + 20k roads in ~3 s, so the earlier 0.30 deg grid
# (60 tiny queries/layer) was massively over-fragmented and triggered Overpass
# rate-limiting (429/504).  Use ~0.85 deg -> a 4x3 = 12-tile grid: far fewer
# queries, each still well within the 300 s server timeout.
TILE_DEG = 0.85

# Which converter each layer uses.  POLY -> polygons, LINE -> linestrings,
# POINT -> aerodrome point list.
POLY, LINE, POINT = "poly", "line", "point"
LAYERS = {
    "water":       ("water",       POLY),
    "forest":      ("forest",      POLY),
    "buildings":   ("buildings",   POLY),
    "runways":     ("runways",     POLY),
    "settlements": ("settlements", POLY),
    "waterways":   ("waterways",   LINE),
    "roads_lines": ("roads",       LINE),
    "railways_lines": ("railways", LINE),
    "aerodromes":  ("aerodromes",  POINT),
}

# Kinds DENSE enough that a single full-bbox Overpass query would time out / blow
# the memory guard, so they MUST be tiled.  Everything else (water, railways,
# waterways, runways, settlements, aerodromes) fetches over the whole NM bbox in
# ONE query -- verified: each returns in <80 s -- which avoids the per-tile 429
# storm that throttles 60-query-per-layer fetching.
DENSE_KINDS = {"buildings", "roads", "forest"}


def landscape_wgs84_bbox():
    """(south, west, north, east) of the landscape grid in WGS84."""
    e0, e1 = ULXMAP, ULXMAP + PATCHES_X * PATCH_SIZE_M
    n0, n1 = ULYMAP - PATCHES_Y * PATCH_SIZE_M, ULYMAP
    t = pyproj.Transformer.from_crs(UTM_CRS, WGS84_CRS, always_xy=True)
    lons, lats = [], []
    for e in (e0, e1):
        for n in (n0, n1):
            lo, la = t.transform(e, n)
            lons.append(lo)
            lats.append(la)
    return min(lats), min(lons), max(lats), max(lons)


def tile_grid(south, west, north, east):
    """Yield (ti, tj, s, w, n, e) sub-bboxes covering the extent."""
    import math
    nlon = max(1, math.ceil((east - west) / TILE_DEG))
    nlat = max(1, math.ceil((north - south) / TILE_DEG))
    dlon = (east - west) / nlon
    dlat = (north - south) / nlat
    for tj in range(nlat):
        for ti in range(nlon):
            w = west + ti * dlon
            e = west + (ti + 1) * dlon
            s = south + tj * dlat
            n = south + (tj + 1) * dlat
            yield ti, tj, s, w, n, e


def _tile_cache_path(kind, ti, tj):
    """Cache path for a tile; ti<0 denotes the whole-bbox (single-query) fetch."""
    if ti < 0:
        return TILE_CACHE / f"{kind}_whole.json"
    return TILE_CACHE / f"{kind}_t{ti:02d}_{tj:02d}.json"


def _fetch_tile_raw(kind, ti, tj, s, w, n, e, force=False, endpoint_offset=0):
    """Fetch one (kind, tile) Overpass response, cached as raw JSON.

    ``ti < 0`` fetches the whole bbox in a single query (used for sparse kinds).
    """
    cache = _tile_cache_path(kind, ti, tj)
    if cache.exists() and not force and cache.stat().st_size > 2:
        return json.loads(cache.read_text(encoding="utf-8"))
    data = query_overpass(kind, s, w, n, e, endpoint_offset=endpoint_offset)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data), encoding="utf-8")
    n_el = len(data.get("elements", []))
    label = "whole-bbox" if ti < 0 else f"tile ({ti},{tj})"
    print(f"    {kind} {label}: {n_el} elements")
    return data


def assemble_layer(layer_name, query_kind, conv, tiles):
    """Merge an already-cached layer across all tiles, de-duped by element id.

    Reads only the tile cache (no network) -- the actual fetching is done by the
    flattened (layer, tile) task pool in :func:`main`.  Tiles that are missing
    from the cache are skipped (a warning is printed) so a partial run still
    produces usable layers.
    """
    merged_elements = {}
    points = []
    missing = 0
    # Sparse kinds are cached as a single whole-bbox file; dense kinds per tile.
    if query_kind not in DENSE_KINDS:
        src_tiles = [(-1, -1, None, None, None, None)]
    else:
        src_tiles = [(t[0], t[1], None, None, None, None) for t in tiles]
    for (ti, tj, *_rest) in src_tiles:
        cache = _tile_cache_path(query_kind, ti, tj)
        if not (cache.exists() and cache.stat().st_size > 2):
            missing += 1
            continue
        data = json.loads(cache.read_text(encoding="utf-8"))
        if conv == POINT:
            points.extend(osm_json_to_points(data))
            continue
        for el in data.get("elements", []):
            key = (el.get("type"), el.get("id"))
            if key not in merged_elements:
                merged_elements[key] = el

    if conv == POINT:
        seen, uniq = set(), []
        for p in points:
            k = (round(p["lat"], 4), round(p["lon"], 4))
            if k in seen:
                continue
            seen.add(k)
            uniq.append(p)
        out = OUT_DIR / f"{layer_name}.json"
        out.write_text(json.dumps({"aerodromes": uniq}, indent=2), encoding="utf-8")
        print(f"  {layer_name:16s} -> {len(uniq)} aerodromes"
              f"{f'  ({missing} tiles missing)' if missing else ''}")
        return layer_name, len(uniq), missing

    combined = {"elements": list(merged_elements.values())}
    fc = osm_json_to_geojson_lines(combined) if conv == LINE else osm_json_to_geojson(combined)
    save_geojson(fc, OUT_DIR / f"{layer_name}.geojson")
    print(f"  {layer_name:16s} -> {len(fc['features'])} features"
          f"{f'  ({missing} tiles missing)' if missing else ''}")
    return layer_name, len(fc["features"]), missing


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="ignore tile cache")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel layer fetchers (default 4)")
    ap.add_argument("--only", default=None,
                    help="comma list of output layers to fetch (default all)")
    ap.add_argument("--tiles", default=None,
                    help="restrict to a single 'ti,tj' tile (debug)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TILE_CACHE.mkdir(parents=True, exist_ok=True)

    s, w, n, e = landscape_wgs84_bbox()
    print(f"Landscape: {LANDSCAPE_NAME}  bbox S,W,N,E = "
          f"{s:.5f},{w:.5f},{n:.5f},{e:.5f}")
    tiles = list(tile_grid(s, w, n, e))
    if args.tiles:
        ti0, tj0 = (int(x) for x in args.tiles.split(","))
        tiles = [t for t in tiles if t[0] == ti0 and t[1] == tj0]
    print(f"Tile grid: {len({t[0] for t in tiles})}x{len({t[1] for t in tiles})} "
          f"= {len(tiles)} tiles @ {TILE_DEG} deg")
    print(f"Output dir: {OUT_DIR}")

    layers = LAYERS
    if args.only:
        want = set(args.only.split(","))
        layers = {k: v for k, v in LAYERS.items() if k in want}

    # Flatten work into (query_kind, tile) tasks so fetching is parallel across
    # BOTH layers and tiles -- fast tiles complete immediately and the heavy
    # building/road tiles never block the rest (the old per-layer pool stalled
    # every fast layer behind buildings).  Distinct query kinds only (roads_lines
    # + railways_lines reuse the 'roads'/'railways' kind, so each kind is fetched
    # once and shared).
    kinds = []
    for name, (kind, conv) in layers.items():
        if kind not in kinds:
            kinds.append(kind)
    full_bbox = (s, w, n, e)
    tasks = []
    for ki, kind in enumerate(kinds):
        if kind in DENSE_KINDS:
            for (ti, tj, ts, tw, tn, te) in tiles:
                tasks.append((kind, ki, ti, tj, ts, tw, tn, te))
        else:
            # sparse -> ONE whole-bbox query (ti=-1) instead of 60 tiles
            tasks.append((kind, ki, -1, -1, *full_bbox))
    n_dense = sum(1 for k in kinds if k in DENSE_KINDS)
    n_sparse = len(kinds) - n_dense
    print(f"Fetching {n_sparse} sparse kinds (1 whole-bbox query each) + "
          f"{n_dense} dense kinds x {len(tiles)} tiles = {len(tasks)} tasks "
          f"on {args.workers} workers")

    def _task(t):
        kind, ki, ti, tj, s, w, n, e = t
        try:
            _fetch_tile_raw(kind, ti, tj, s, w, n, e, force=args.force,
                            endpoint_offset=ki)
            return kind, ti, tj, True
        except Exception as exc:
            print(f"    !! {kind} tile ({ti},{tj}) FAILED: {exc}")
            return kind, ti, tj, False

    t0 = time.time()
    n_ok = n_fail = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        # as_completed (not map) so progress reports as tasks finish, regardless
        # of order -- a slow buildings tile never blocks the counter.
        futs = [ex.submit(_task, t) for t in tasks]
        for fut in as_completed(futs):
            kind, ti, tj, ok = fut.result()
            if ok:
                n_ok += 1
            else:
                n_fail += 1
            done = n_ok + n_fail
            if done % 15 == 0 or done == len(tasks):
                rate = done / max(1e-6, time.time() - t0)
                print(f"  {done}/{len(tasks)} tasks  ({rate:.1f}/s, "
                      f"{n_fail} failed, ETA {(len(tasks)-done)/max(1e-6,rate):.0f}s)")

    print(f"\nFetched {n_ok} ok / {n_fail} failed in {time.time() - t0:.0f}s. "
          "Assembling layers from cache...")
    results = {}
    for name, (kind, conv) in layers.items():
        try:
            _, count, missing = assemble_layer(name, kind, conv, tiles)
            results[name] = (count, missing)
        except Exception as exc:
            print(f"!! assemble {name} crashed: {exc}")
            results[name] = (-1, -1)

    print(f"\n=== OSM download summary ({time.time() - t0:.0f}s) ===")
    for name in layers:
        cnt, miss = results.get(name, ("?", "?"))
        print(f"  {name:16s} {cnt} features"
              f"{f'  [{miss} tiles missing]' if miss else ''}")


if __name__ == "__main__":
    main()
