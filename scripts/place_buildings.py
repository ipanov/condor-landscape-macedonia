#!/usr/bin/env python3
"""
Building PLACEMENT step for the MacedoniaSkopje (Skopje, North Macedonia) landscape.

This consumes the already-downloaded, merged building footprints
(``.sandbox/buildings/buildings_combined_utm.geojson`` - Overture+Microsoft+OSM,
EPSG:32634, produced by ``download_buildings.py``) and emits a Condor 2 object
*placement* file: a flat binary sequence of 152-byte records, one per placed
building, each referencing a building model ``.c3d`` (produced separately by
``scripts/c3d.py``).

It does NOT download anything, does NOT open a GUI, and does NOT write into the
Condor install. The sample ``.obj`` and the stats land in ``.sandbox/buildings/``.

Per building we derive
----------------------
* **position**  - UTM centroid of the footprint (EPSG:32634).
* **orientation** - bearing of the *longest edge* of the footprint, in radians,
  so a placed box model lines up with the building's long axis.
* **height** - ``building:levels`` x 3 m, else an explicit ``height`` tag, else
  a 3 m default (single storey). Carried in the stats for the c3d/merge step; a
  single placeholder c3d is uniform-scaled in Z only by ``scale`` (Condor's .obj
  has ONE uniform scale field, so height is best baked per-c3d - see docs).
* **footprint area** - shapely polygon area (m^2), used for significance ranking.

Significance filter
-------------------
Footprints with area < ``MIN_AREA_M2`` (default 40 m^2 - sheds, garages, map
noise) are dropped. The survivors are ranked by area, then height, so a
``--limit`` cut keeps the most visually prominent structures first.

Condor .obj record (152 bytes, little-endian, no file header)
-------------------------------------------------------------
Verified against ``C:/Condor2/Landscapes/Slovenia2/Slovenia2.obj``
(1,139,392 / 152 = 7,496 records) and ``flxhu/condor2`` ``condor_obj_file_tool.py``:

    off  type      field
    0    float32   posX   = origin_E - easting
    4    float32   posY   = northing - origin_N
    8    float32   posZ   = terrain altitude (m ASL) at the centroid
    12   float32   scale  = uniform scale (1.0 = model's native size)
    16   float32   ori    = orientation, radians
    20   uint8     nameLen
    21   char[131] name   = c3d filename, null-padded  (20+1+131 = 152)

Origin is the landscape's SOUTH-EAST corner, per the task: E=576000, N=4631040.
(Note: this is the object-placement origin used by Condor's .obj; it is offset by
one patch-aligned step from the .trn header BR. We use exactly the task value.)

Usage
-----
    python scripts/place_buildings.py                 # full run -> sample .obj + stats
    python scripts/place_buildings.py --limit 20000   # keep top-N significant
    python scripts/place_buildings.py --min-area 60   # stricter significance cut
    python scripts/place_buildings.py --c3d-name building.c3d
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from condor_grid import (  # noqa: E402
    PATCHES_X,
    PATCHES_Y,
    PATCH_SIZE_M,
    patch_bounds_utm,
)
from forest_utils import load_dem, utm_to_dem_index  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IN_GEOJSON = ROOT / ".sandbox" / "buildings" / "buildings_combined_utm.geojson"
OUT_DIR = ROOT / ".sandbox" / "buildings"
OUT_OBJ = OUT_DIR / "MacedoniaSkopje.obj"
OUT_STATS = OUT_DIR / "building_stats.json"
OUT_GROUPING = OUT_DIR / "building_patch_groups.json"

# Canonical 30 m, runway-flattened DEM (same one forest maps / mesh use) for posZ.
DEM_PATH = ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305_flat.raw"

# Object-placement origin = landscape SE corner (task-specified).
ORIGIN_E = 576000.0
ORIGIN_N = 4631040.0

# Landscape bbox for clipping (task-specified; matches 12x12 x 5760 m from the NW
# ULX/ULY 506880/4700160 down to the SE origin).
BBOX_MIN_E = 506880.0
BBOX_MAX_E = 576000.0
BBOX_MIN_N = 4631040.0
BBOX_MAX_N = 4700160.0

# Significance threshold and height model.
MIN_AREA_M2 = 40.0
METRES_PER_LEVEL = 3.0
DEFAULT_HEIGHT_M = 3.0

RECORD_SIZE = 152
NAME_FIELD = 131  # bytes after the uint8 nameLen


# ---------------------------------------------------------------------------
# Height / orientation helpers
# ---------------------------------------------------------------------------
def _parse_float(val):
    """Best-effort float from a tag that may be '12', '12 m', '12.5', etc."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower().replace("m", "").replace(",", ".").strip()
    try:
        return float(s.split()[0]) if s else None
    except (ValueError, IndexError):
        return None


def building_height(props: dict) -> float:
    """levels x 3 m, else explicit height tag, else single-storey default."""
    levels = _parse_float(props.get("building:levels")) or _parse_float(
        props.get("num_floors")
    )
    if levels and levels > 0:
        return max(levels * METRES_PER_LEVEL, DEFAULT_HEIGHT_M)
    h = _parse_float(props.get("height"))
    if h and h > 0:
        return h
    return DEFAULT_HEIGHT_M


def longest_edge_bearing(poly) -> float:
    """Orientation (radians, 0..pi) of the polygon's longest exterior edge.

    Condor's ``ori`` is a heading in radians; a box model's footprint is
    symmetric under a half turn, so 0..pi is sufficient and avoids 180-deg
    ambiguity. Uses the oriented bounding box's long axis, which is robust to
    the digitisation start-point of the ring.
    """
    try:
        mrr = poly.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)
    except Exception:
        coords = list(poly.exterior.coords)
    best_len = -1.0
    best_ang = 0.0
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        dx, dy = x1 - x0, y1 - y0
        d = dx * dx + dy * dy
        if d > best_len:
            best_len = d
            best_ang = math.atan2(dy, dx)
    # Normalise to [0, pi).
    a = best_ang % math.pi
    if a < 0:
        a += math.pi
    return a


# ---------------------------------------------------------------------------
# Patch indexing (Condor CCRR: col 0 = EAST, row 0 = SOUTH)
# ---------------------------------------------------------------------------
def patch_of(e: float, n: float):
    """Return (col, row) Condor patch index for a UTM point, or None if outside."""
    col = int((ORIGIN_E - e) // PATCH_SIZE_M)
    row = int((n - ORIGIN_N) // PATCH_SIZE_M)
    if 0 <= col < PATCHES_X and 0 <= row < PATCHES_Y:
        return col, row
    return None


# ---------------------------------------------------------------------------
# Record encoding
# ---------------------------------------------------------------------------
def encode_record(posx: float, posy: float, posz: float, scale: float,
                  ori: float, name: str) -> bytes:
    name_b = name.encode("ascii", errors="ignore")[:NAME_FIELD]
    rec = struct.pack("<5f", posx, posy, posz, scale, ori)
    rec += struct.pack("<B", len(name_b))
    rec += name_b.ljust(NAME_FIELD, b"\x00")
    assert len(rec) == RECORD_SIZE, len(rec)
    return rec


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-area", type=float, default=MIN_AREA_M2,
                    help=f"drop footprints smaller than this (m^2, default {MIN_AREA_M2})")
    ap.add_argument("--limit", type=int, default=0,
                    help="keep only the top-N most significant (0 = all)")
    ap.add_argument("--c3d-name", default="building.c3d",
                    help="placeholder building model referenced by every record")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="uniform scale written to every record (default 1.0)")
    ap.add_argument("--in", dest="in_path", default=str(IN_GEOJSON),
                    help="input combined UTM geojson")
    ap.add_argument("--emit-grouping", action="store_true",
                    help="also write per-patch building grouping json (merge plan)")
    args = ap.parse_args()

    in_path = Path(args.in_path)
    if not in_path.exists():
        sys.exit(f"ERROR: input geojson not found: {in_path}\n"
                 f"Run scripts/download_buildings.py first (download is a separate step).")

    print(f"[place] reading {in_path.name} ({in_path.stat().st_size/1e6:.1f} MB) ...")
    with open(in_path, "r", encoding="utf-8") as f:
        fc = json.load(f)
    feats = fc.get("features", [])
    print(f"[place] {len(feats)} raw footprints")

    # DEM for ground elevation (posZ). If missing, fall back to 0 and warn.
    dem = None
    if DEM_PATH.exists():
        dem = load_dem(DEM_PATH)
        print(f"[place] DEM loaded for posZ: {DEM_PATH.name} {dem.shape}")
    else:
        print(f"[place] WARNING: DEM {DEM_PATH.name} missing -> posZ=0 for all "
              f"records (Condor will still drape objects on terrain at load).")

    # ----- Pass 1: parse, clip, measure, filter on area -----------------------
    rows_out = []  # dicts: e,n,area,height,ori,col,row
    n_outside = 0
    n_small = 0
    n_bad = 0
    src_counter = Counter()
    for feat in feats:
        geom = feat.get("geometry")
        if not geom:
            n_bad += 1
            continue
        try:
            g = shape(geom)
        except Exception:
            n_bad += 1
            continue
        if g.is_empty:
            n_bad += 1
            continue
        if not g.is_valid:
            g = g.buffer(0)
            if g.is_empty or not g.is_valid:
                n_bad += 1
                continue
        c = g.centroid
        e, n = c.x, c.y
        if not (BBOX_MIN_E <= e <= BBOX_MAX_E and BBOX_MIN_N <= n <= BBOX_MAX_N):
            n_outside += 1
            continue
        area = g.area
        if area < args.min_area:
            n_small += 1
            continue
        pr = patch_of(e, n)
        if pr is None:
            n_outside += 1
            continue
        props = feat.get("properties", {}) or {}
        src_counter[props.get("src", "?")] += 1
        rows_out.append({
            "e": e, "n": n,
            "area": area,
            "height": building_height(props),
            "ori": longest_edge_bearing(g),
            "col": pr[0], "row": pr[1],
        })

    print(f"[place] clipped/measured: kept {len(rows_out)} | "
          f"outside-bbox {n_outside} | <{args.min_area:.0f}m^2 {n_small} | invalid {n_bad}")
    print(f"[place] kept by source: {dict(src_counter)}")
    if not rows_out:
        sys.exit("ERROR: no buildings survived clipping/filtering.")

    # ----- Significance ranking: area desc, then height desc ------------------
    rows_out.sort(key=lambda r: (r["area"], r["height"]), reverse=True)
    n_significant = len(rows_out)
    if args.limit and args.limit < len(rows_out):
        rows_out = rows_out[: args.limit]
        print(f"[place] --limit {args.limit}: keeping top {len(rows_out)} of "
              f"{n_significant} significant")

    # ----- posZ from DEM ------------------------------------------------------
    if dem is not None:
        es = np.array([r["e"] for r in rows_out])
        ns = np.array([r["n"] for r in rows_out])
        rr, cc = utm_to_dem_index(es, ns)
        rr = np.clip(np.rint(rr).astype(int), 0, dem.shape[0] - 1)
        cc = np.clip(np.rint(cc).astype(int), 0, dem.shape[1] - 1)
        zs = dem[rr, cc].astype(float)
    else:
        zs = np.zeros(len(rows_out))

    # ----- Write .obj placement records --------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = args.c3d_name
    with open(OUT_OBJ, "wb") as f:
        for r, z in zip(rows_out, zs):
            posx = ORIGIN_E - r["e"]
            posy = r["n"] - ORIGIN_N
            f.write(encode_record(posx, posy, float(z), args.scale, r["ori"], name))
    n_written = len(rows_out)
    print(f"[place] wrote {n_written} records ({n_written*RECORD_SIZE} bytes) "
          f"-> {OUT_OBJ}  (placeholder model '{name}')")

    # ----- Stats --------------------------------------------------------------
    hist = Counter((r["col"], r["row"]) for r in rows_out)
    per_patch = {}
    for (col, row), cnt in hist.items():
        per_patch.setdefault(f"{col:02d}{row:02d}", cnt)
    counts = np.array(list(hist.values())) if hist else np.array([0])
    areas = np.array([r["area"] for r in rows_out])
    heights = np.array([r["height"] for r in rows_out])

    def _pct(a, p):
        return float(np.percentile(a, p)) if len(a) else 0.0

    # Patches with at least one building, and the worst-case draw load.
    occupied = len(hist)
    max_patch = max(hist.items(), key=lambda kv: kv[1]) if hist else ((0, 0), 0)

    stats = {
        "input": in_path.name,
        "origin_E": ORIGIN_E,
        "origin_N": ORIGIN_N,
        "bbox": [BBOX_MIN_E, BBOX_MIN_N, BBOX_MAX_E, BBOX_MAX_N],
        "min_area_m2": args.min_area,
        "c3d_name": name,
        "scale": args.scale,
        "counts": {
            "raw_footprints": len(feats),
            "outside_bbox": n_outside,
            "below_min_area": n_small,
            "invalid": n_bad,
            "significant": n_significant,
            "written": n_written,
        },
        "by_source_kept": dict(src_counter),
        "per_patch_histogram": {
            "patches_total": PATCHES_X * PATCHES_Y,
            "patches_occupied": occupied,
            "patches_empty": PATCHES_X * PATCHES_Y - occupied,
            "buildings_per_patch_min": int(counts.min()),
            "buildings_per_patch_max": int(counts.max()),
            "buildings_per_patch_mean": float(counts.mean()),
            "buildings_per_patch_median": float(np.median(counts)),
            "busiest_patch": {"col": max_patch[0][0], "row": max_patch[0][1],
                              "ccrr": f"{max_patch[0][0]:02d}{max_patch[0][1]:02d}",
                              "count": max_patch[1]},
            "per_patch": dict(sorted(per_patch.items())),
        },
        "area_distribution_m2": {
            "min": float(areas.min()), "p25": _pct(areas, 25),
            "median": _pct(areas, 50), "p75": _pct(areas, 75),
            "p95": _pct(areas, 95), "max": float(areas.max()),
            "mean": float(areas.mean()),
        },
        "height_distribution_m": {
            "min": float(heights.min()), "median": _pct(heights, 50),
            "p95": _pct(heights, 95), "max": float(heights.max()),
            "default_3m_share": float((heights == DEFAULT_HEIGHT_M).mean()),
        },
        "merge_strategy": {
            "ideal": "ONE merged .c3d per 5760 m patch (1 draw call / patch).",
            "draw_calls_now": ("Every record references the SAME placeholder c3d, "
                               "so Condor batches by model; this is acceptable for "
                               "a single shared box. For per-patch geometry merging "
                               "(distinct rooflines), group records by (col,row) and "
                               "bake one c3d per occupied patch."),
            "occupied_patches": occupied,
            "max_buildings_in_one_patch": int(counts.max()),
            "recommended": (
                "Phase A: ship the single placeholder building.c3d referenced by all "
                "records (this file) for a quick in-sim sanity pass. "
                "Phase B: replace with per-patch merged c3d named e.g. bldg_CCRR.c3d "
                "and rewrite each record's name accordingly (see "
                "building_patch_groups.json)."),
        },
    }
    with open(OUT_STATS, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    print(f"[place] stats -> {OUT_STATS}")

    # ----- Optional per-patch grouping (merge plan) ---------------------------
    if args.emit_grouping:
        groups = {}
        for i, r in enumerate(rows_out):
            key = f"{r['col']:02d}{r['row']:02d}"
            groups.setdefault(key, []).append({
                "e": round(r["e"], 2), "n": round(r["n"], 2),
                "area": round(r["area"], 1), "height": round(r["height"], 1),
                "ori": round(r["ori"], 4),
            })
        with open(OUT_GROUPING, "w", encoding="utf-8") as f:
            json.dump({"c3d_name_template": "bldg_CCRR.c3d", "patches": groups},
                      f, indent=2)
        print(f"[place] per-patch grouping ({len(groups)} patches) -> {OUT_GROUPING}")

    # ----- Console summary ----------------------------------------------------
    print("\n=== SUMMARY ===")
    print(f"  buildings written : {n_written}")
    print(f"  significant total : {n_significant} (area >= {args.min_area:.0f} m^2)")
    print(f"  patches occupied  : {occupied}/{PATCHES_X*PATCHES_Y}")
    print(f"  per-patch min/med/max: {int(counts.min())} / "
          f"{np.median(counts):.0f} / {int(counts.max())}")
    print(f"  busiest patch     : CCRR {max_patch[0][0]:02d}{max_patch[0][1]:02d} "
          f"= {max_patch[1]} buildings")
    print(f"  area  m^2 med/p95/max: {_pct(areas,50):.0f} / "
          f"{_pct(areas,95):.0f} / {areas.max():.0f}")
    print(f"  height m  med/p95/max: {_pct(heights,50):.0f} / "
          f"{_pct(heights,95):.0f} / {heights.max():.0f}")
    print(f"  default 3 m share : {(heights==DEFAULT_HEIGHT_M).mean()*100:.0f}%")
    print(f"\n  .obj  -> {OUT_OBJ}")
    print(f"  stats -> {OUT_STATS}")
    print("  (NOT installed to C:/Condor2 - this is a sandbox sample.)")


if __name__ == "__main__":
    main()
