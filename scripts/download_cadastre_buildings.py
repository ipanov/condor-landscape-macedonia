#!/usr/bin/env python3
"""
Scrape NORTH MACEDONIAN CADASTRE building footprints (with floors + use code) for
the MacedoniaSkopje landscape and reproject them to UTM 34N (EPSG:32634).

WHY THIS EXISTS
---------------
``place_buildings.py`` falls back to a flat 3 m height for every building that has
no ``building:levels`` / ``height`` tag. OSM carries levels for only ~3 % of the
67 k Skopje footprints, so almost everything became a 3 m box -- unacceptable. The
Agency for Real Estate Cadastre (Агенција за катастар на недвижности, АКН) holds a
*per-building* number-of-floors attribute. This script pulls it straight from the
same public backend that already serves this project's orthophoto.

DISCOVERED SERVICE  (no auth, no captcha -- the public e-uslugi map viewer uses it)
----------------------------------------------------------------------------------
* Portal           : https://e-uslugi.katastar.gov.mk  (React SPA, АКН)
* GeoServer (WFS)  : https://e-uslugi.katastar.gov.mk/geo/proxy/wfs_geoserver_vector
    - reverse-proxies an internal GeoServer (``geoserver_vector``); WFS 2.0.0,
      OWS-standard KVP, ``outputFormat=application/json`` supported.
* Building layer   : ``Public:RM_OBJECTS``  (viewer label "Објекти" / Objects,
                     shown at map zoom 10-12). Two equivalent twins also exist:
                     ``PARCELI_KO:OBJEKTI`` and ``OBJEKTI:OBJEKTI_WMS``.
* Native CRS       : EPSG:6316 (MGI 1901 / Balkans zone 7, the MK national grid).
                     WFS BBOX axis order is **E,N** (minx=Easting, miny=Northing).
* Attribute schema (DescribeFeatureType ``Public:RM_OBJECTS``):
    ID_PARC   int     parcel id (FK to KIS:PARCELI parcel)
    BLDN      short   NUMBER OF FLOORS / storeys above ground  <-- the height signal
    CODE_US   int     building USE code (internal AKN codelist; 17 dominant)
    PCLASS    short   class
    AREA      double  footprint area (m^2)
    CHANGE_YEAR short year of last survey change
    GR_PARCEL string  human cadastral parcel label (e.g. "2660/2")
    OBJ_TYPE  double  object type: 1 = real building, 0 = "land under building"/aux
    ID_PO     short   ; GR_COD_CC, GR_COD_DP doubles (county/dept group codes)
    GEOMETRY  MultiSurface polygon (EPSG:6316)

SERVER ETIQUETTE  (LEARNED THE HARD WAY -- do not change without re-testing)
---------------------------------------------------------------------------
The proxy has a ~50 s hard timeout and the SDE-backed layer is SLOW and FRAGILE:
* A single tight-bbox query (<=~300 m) returns in 5-15 s.
* Firing several in quick succession makes the gateway cascade into 502/504 for
  ~1 min; it only recovers when left ALONE.
=> This scraper is deliberately **strictly sequential, one request at a time**,
   with a base inter-request delay and exponential backoff + long cooldown on any
   gateway error. No threads, no asyncio. Tiles that still cap out are bisected.
   It is fully RESUMABLE (a per-tile ledger), so a long polite run can be stopped
   and continued. This respects the public server while still getting all data.

OUTPUT
------
``.sandbox/buildings/cadastre_buildings.geojson``  -- EPSG:32634 FeatureCollection.
Each feature's properties are normalised so ``place_buildings.py`` consumes them
with no change (it already reads ``num_floors`` then ``height``):
    num_floors : int   = BLDN (floors)          -> drives levels x 3 m
    height     : float = BLDN*3 + 3 (roof)      -> explicit fallback height
    use_code   : int   = CODE_US
    obj_type   : int   = OBJ_TYPE
    area_m2, change_year, parcel, id_parc, src="cadastre"
A sidecar ``cadastre_buildings_stats.json`` carries counts + attribute histograms.

USAGE
-----
    python scripts/download_cadastre_buildings.py --region central   # proof subset
    python scripts/download_cadastre_buildings.py --region full      # whole bbox
    python scripts/download_cadastre_buildings.py --region full --resume
    python scripts/download_cadastre_buildings.py --tile 250 --delay 1.5
Flags: --max-tiles N (stop early), --layer, --tile (m), --delay (s), --no-reproject.

NEVER writes into C:/Condor2 and NEVER opens a GUI.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import pyproj
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".sandbox" / "buildings"
LEDGER_DIR = OUT_DIR / "cadastre_tiles"          # per-tile ledger + raw cache

WFS_URL = "https://e-uslugi.katastar.gov.mk/geo/proxy/wfs_geoserver_vector"
DEFAULT_LAYER = "Public:RM_OBJECTS"
REFERER = "https://e-uslugi.katastar.gov.mk/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) condor-landscape/1.0 "
              "(MacedoniaSkopje terrain; polite sequential cadastre fetch)")

SRC_EPSG = 6316          # cadastre native (MGI Balkans 7)
UTM_EPSG = 32634         # landscape working CRS
WGS84_EPSG = 4326

# Landscape bbox in UTM 34N (task-specified, matches condor_grid / place_buildings).
UTM_W, UTM_S, UTM_E, UTM_N = 506880.0, 4631040.0, 576000.0, 4700160.0

# Per-feature count cap we ask GeoServer for. If a tile returns >= this, we treat
# it as "truncated" and bisect so we never silently drop buildings.
COUNT_CAP = 4000

# Politeness / robustness knobs (overridable via CLI).
BASE_DELAY_S = 1.5       # wait between successful tile requests
GATEWAY_COOLDOWN_S = 60  # extra sleep after a 502/503/504 burst
MAX_RETRIES = 6          # per tile, before giving up (it'll be retried next run)
REQ_TIMEOUT_S = 120

# Height model: floors * 3 m + a 3 m roof/parapet allowance.
METRES_PER_FLOOR = 3.0
ROOF_M = 3.0

_TO_UTM = pyproj.Transformer.from_crs(SRC_EPSG, UTM_EPSG, always_xy=True)
_WGS_TO_SRC = pyproj.Transformer.from_crs(WGS84_EPSG, SRC_EPSG, always_xy=True)
_UTM_TO_SRC = pyproj.Transformer.from_crs(UTM_EPSG, SRC_EPSG, always_xy=True)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def to_utm(geom):
    return shp_transform(lambda x, y, z=None: _TO_UTM.transform(x, y), geom)


def utm_bbox_to_src(w, s, e, n):
    """Project the UTM landscape bbox to EPSG:6316, padding to the bounding rect."""
    xs, ys = [], []
    for ee, nn in ((w, s), (w, n), (e, s), (e, n)):
        x, y = _UTM_TO_SRC.transform(ee, nn)
        xs.append(x)
        ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def lonlat_bbox_to_src(w, s, e, n):
    xs, ys = [], []
    for lo, la in ((w, s), (w, n), (e, s), (e, n)):
        x, y = _WGS_TO_SRC.transform(lo, la)
        xs.append(x)
        ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Single WFS tile fetch (the ONLY network call; strictly one at a time)
# ---------------------------------------------------------------------------
def fetch_tile(layer, bbox_src, count=COUNT_CAP):
    """GET one bbox of features as GeoJSON.

    bbox_src = (minx, miny, maxx, maxy) in EPSG:6316, axis E,N.
    Returns (features:list, truncated:bool). Raises RuntimeError on hard failure
    so the caller can decide to retry / cool down.
    """
    minx, miny, maxx, maxy = bbox_src
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer,
        "srsName": "EPSG:6316",
        "count": str(count),
        "outputFormat": "application/json",
        # WFS 2.0 KVP BBOX with explicit CRS; axis order for 6316 here is E,N.
        "bbox": f"{minx:.2f},{miny:.2f},{maxx:.2f},{maxy:.2f},EPSG:6316",
    }
    url = WFS_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Referer": REFERER, "User-Agent": USER_AGENT,
        "Accept": "application/json,*/*",
    })
    with urllib.request.urlopen(req, timeout=REQ_TIMEOUT_S) as resp:
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read()
    if "json" not in ctype.lower():
        # GeoServer returns an XML ServiceException on bad params; surface it.
        raise RuntimeError(f"non-JSON response ({ctype}): {raw[:200]!r}")
    data = json.loads(raw)
    feats = data.get("features", [])
    truncated = len(feats) >= count
    return feats, truncated


def fetch_tile_resilient(layer, bbox_src, delay, count=COUNT_CAP):
    """fetch_tile with retries, exponential backoff, and gateway cooldown.

    Returns (features, truncated). On exhaustion returns (None, False) so the
    tile stays un-done in the ledger and is retried on the next run.
    """
    backoff = delay
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            feats, truncated = fetch_tile(layer, bbox_src, count=count)
            return feats, truncated
        except urllib.error.HTTPError as ex:
            code = ex.code
            transient = code in (429, 500, 502, 503, 504)
            msg = f"HTTP {code}"
        except urllib.error.URLError as ex:
            transient = True
            msg = f"URLError {ex.reason}"
        except (TimeoutError, RuntimeError, json.JSONDecodeError) as ex:
            transient = True
            msg = f"{type(ex).__name__}: {str(ex)[:80]}"
        if not transient or attempt == MAX_RETRIES:
            print(f"      ! give up on tile ({msg}) after {attempt} tries "
                  f"-- will retry next run", flush=True)
            return None, False
        cool = GATEWAY_COOLDOWN_S if ("HTTP 50" in msg or "HTTP 429" in msg) else 0
        wait = backoff + cool
        print(f"      . {msg}; backoff {wait:.0f}s (attempt {attempt}/{MAX_RETRIES})",
              flush=True)
        time.sleep(wait)
        backoff = min(backoff * 2, 30)
    return None, False


# ---------------------------------------------------------------------------
# Tiling with bisection on truncation
# ---------------------------------------------------------------------------
def make_grid(bbox_src, tile_m):
    minx, miny, maxx, maxy = bbox_src
    xs, x = [], minx
    while x < maxx:
        xs.append(x)
        x += tile_m
    ys, y = [], miny
    while y < maxy:
        ys.append(y)
        y += tile_m
    tiles = []
    for ix in xs:
        for iy in ys:
            tiles.append((ix, iy, min(ix + tile_m, maxx), min(iy + tile_m, maxy)))
    return tiles


def tile_key(b):
    return f"{b[0]:.0f}_{b[1]:.0f}_{b[2]:.0f}_{b[3]:.0f}"


# ---------------------------------------------------------------------------
# Normalisation -> place_buildings-friendly properties
# ---------------------------------------------------------------------------
def normalise(props):
    """Map raw RM_OBJECTS attributes to friendly keys.

    IMPORTANT about BLDN: in this dataset BLDN is the FLOOR NUMBER of a cadastral
    *unit* (кат), not the building's storey count. A high-rise is catalogued as
    many small co-located unit-polygons on one parcel with BLDN = 1,2,3,...,N. So
    a per-record BLDN must NOT be used directly as a building height. We keep the
    raw value in ``unit_floor`` and let the post-pass derive each footprint's true
    height = MAX(BLDN) over its parcel (see derive_parcel_heights). ``num_floors``/
    ``height`` are filled by that post-pass so place_buildings.py reads them.
    """
    bldn = props.get("BLDN")
    unit_floor = int(bldn) if isinstance(bldn, (int, float)) and bldn else None
    return {
        "src": "cadastre",
        "id_parc": props.get("ID_PARC"),
        "parcel": props.get("GR_PARCEL"),
        "use_code": props.get("CODE_US"),
        "obj_type": (int(props["OBJ_TYPE"])
                     if isinstance(props.get("OBJ_TYPE"), (int, float)) else None),
        "area_m2": props.get("AREA"),
        "change_year": props.get("CHANGE_YEAR"),
        "unit_floor": unit_floor,        # raw BLDN (per-unit floor index)
        # num_floors / height are assigned per-parcel in write_outputs().
    }


def derive_parcel_heights(features):
    """Assign per-building num_floors/height from MAX(BLDN) over each parcel.

    Cadastral high-rises are stored as stacked per-floor unit polygons; the tallest
    BLDN on a parcel is the building's storey count. Single-storey houses have one
    BLDN=1 record, so this reduces to the obvious answer for them.
    Mutates each feature's ``properties`` in place.
    """
    def _raw_floor(p):
        # New caches store the raw per-unit BLDN in unit_floor; older caches put
        # it in num_floors. Either way it's the per-unit floor index.
        v = p.get("unit_floor")
        if v is None:
            v = p.get("num_floors")
        return v or 0

    by_parcel = {}
    for rec in features.values():
        p = rec["properties"]
        key = p.get("id_parc") or p.get("parcel")
        uf = _raw_floor(p)
        # Preserve the raw per-unit floor in unit_floor before we overwrite
        # num_floors with the parcel max (keeps old caches self-consistent).
        p["unit_floor"] = uf or None
        if key is not None:
            by_parcel[key] = max(by_parcel.get(key, 0), uf)
    for rec in features.values():
        p = rec["properties"]
        key = p.get("id_parc") or p.get("parcel")
        floors = by_parcel.get(key) or _raw_floor(p) or None
        if floors and floors > 0:
            p["num_floors"] = int(floors)
            p["height"] = round(floors * METRES_PER_FLOOR + ROOF_M, 1)
        else:
            p["num_floors"] = None
    return by_parcel


# ---------------------------------------------------------------------------
# Main scrape loop
# ---------------------------------------------------------------------------
def run(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)

    # Resolve target bbox (EPSG:6316).
    if args.region == "central":
        # ~6 x 6 km around central Skopje (Centar/Aerodrom/Karpos) -- the proof set.
        bbox_src = lonlat_bbox_to_src(21.38, 41.97, 21.48, 42.02)
        label = "central Skopje (~6x6 km proof subset)"
    else:
        bbox_src = utm_bbox_to_src(UTM_W, UTM_S, UTM_E, UTM_N)
        label = "full landscape bbox (69 x 69 km)"

    if args.bbox_src:  # manual override "minx,miny,maxx,maxy" in EPSG:6316
        bbox_src = tuple(float(v) for v in args.bbox_src.split(","))
        label = f"manual 6316 bbox {bbox_src}"

    tile_m = args.tile

    # --write-only: skip all network I/O, just (re)build the final GeoJSON+stats
    # from whatever the cache already holds (handy after the schema/height logic
    # changes, or to finalise a partially-scraped resumable run).
    if args.write_only:
        cache_path = LEDGER_DIR / f"features_{args.region}_{tile_m:.0f}.geojsonl"
        if not cache_path.exists():
            sys.exit(f"--write-only: no cache at {cache_path}")
        features = {}
        with open(cache_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    features[obj["id"]] = obj
        print(f"--write-only: loaded {len(features)} cached features from "
              f"{cache_path.name}")
        write_outputs(features, args)
        return

    tiles = make_grid(bbox_src, tile_m)
    print(f"Cadastre building scrape  [{args.layer}]")
    print(f"  region : {label}")
    print(f"  bbox6316: {bbox_src[0]:.0f},{bbox_src[1]:.0f} .. "
          f"{bbox_src[2]:.0f},{bbox_src[3]:.0f}")
    print(f"  tiles  : {len(tiles)} @ {tile_m:.0f} m  | base delay {args.delay:.1f}s")
    if args.max_tiles:
        print(f"  max-tiles: {args.max_tiles} (early stop for a quick sample)")
    print(f"  ledger : {LEDGER_DIR}")

    ledger_path = LEDGER_DIR / f"ledger_{args.region}_{tile_m:.0f}.json"
    ledger = {}
    if args.resume and ledger_path.exists():
        ledger = json.loads(ledger_path.read_text())
        done = sum(1 for v in ledger.values() if v.get("status") == "done")
        print(f"  resume : {done}/{len(ledger)} tiles already done")

    # gml:id -> normalised feature (dedupe across overlapping/bisected tiles).
    features = {}
    # Pre-load any features already captured in the ledger's raw cache.
    cache_path = LEDGER_DIR / f"features_{args.region}_{tile_m:.0f}.geojsonl"
    if args.resume and cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                features[obj["id"]] = obj
        print(f"  cache  : {len(features)} features preloaded")

    cache_f = open(cache_path, "a", encoding="utf-8")

    processed = 0
    new_feats = 0
    t0 = time.time()

    def handle_tile(b, depth=0):
        """Fetch one tile; bisect on truncation. Returns count of new features."""
        nonlocal new_feats
        key = tile_key(b)
        if args.resume and ledger.get(key, {}).get("status") == "done":
            return 0
        feats, truncated = fetch_tile_resilient(args.layer, b, args.delay,
                                                 count=COUNT_CAP)
        if feats is None:
            ledger[key] = {"status": "error", "bbox": b}
            return 0
        if truncated and depth < 4:
            # Split into quadrants and recurse (server capped this tile).
            print(f"      ~ tile {key} truncated ({len(feats)}); bisecting", flush=True)
            ledger[key] = {"status": "split", "bbox": b}
            mx = (b[0] + b[2]) / 2.0
            my = (b[1] + b[3]) / 2.0
            quads = [(b[0], b[1], mx, my), (mx, b[1], b[2], my),
                     (b[0], my, mx, b[3]), (mx, my, b[2], b[3])]
            added = 0
            for q in quads:
                time.sleep(args.delay)
                added += handle_tile(q, depth + 1)
            return added
        added = 0
        for feat in feats:
            fid = feat.get("id") or feat.get("properties", {}).get("ID_PARC")
            if fid is None or fid in features:
                continue
            geom = feat.get("geometry")
            if not geom:
                continue
            rec = {"id": fid,
                   "properties": normalise(feat.get("properties", {})),
                   "geometry": geom}              # geometry stays EPSG:6316 here
            features[fid] = rec
            cache_f.write(json.dumps(rec) + "\n")
            added += 1
            new_feats += 1
        ledger[key] = {"status": "done", "bbox": b, "n": len(feats)}
        return added

    try:
        for i, b in enumerate(tiles, 1):
            if args.max_tiles and processed >= args.max_tiles:
                print(f"  reached --max-tiles {args.max_tiles}; stopping early")
                break
            key = tile_key(b)
            if args.resume and ledger.get(key, {}).get("status") == "done":
                continue
            added = handle_tile(b)
            processed += 1
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed else 0
            print(f"  [{i}/{len(tiles)}] tile {key}: +{added} feats "
                  f"(total {len(features)}) | {rate:.2f} tiles/s", flush=True)
            cache_f.flush()
            ledger_path.write_text(json.dumps(ledger))
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\n  interrupted -- progress saved; rerun with --resume to continue")
    finally:
        cache_f.close()
        ledger_path.write_text(json.dumps(ledger))

    print(f"\n  collected {len(features)} unique features "
          f"({new_feats} new this run) in {time.time()-t0:.0f}s")

    write_outputs(features, args)


def write_outputs(features, args):
    """Derive per-parcel heights, reproject to UTM 34N, write GeoJSON + stats."""
    out_geojson = OUT_DIR / "cadastre_buildings.geojson"
    out_stats = OUT_DIR / "cadastre_buildings_stats.json"

    # Fill num_floors/height from MAX(BLDN) per parcel BEFORE writing.
    parcel_max = derive_parcel_heights(features)
    if parcel_max:
        import statistics
        pmf = [v for v in parcel_max.values() if v]
        print(f"  parcels: {len(parcel_max)} | per-parcel max floors "
              f"median={statistics.median(pmf) if pmf else 0} "
              f"max={max(pmf) if pmf else 0}")

    floors = Counter()
    unit_floors = Counter()
    use = Counter()
    objt = Counter()
    yr = Counter()
    n_with_floors = 0
    out_feats = []
    for rec in features.values():
        props = rec["properties"]
        geom = rec["geometry"]
        if args.no_reproject:
            g_out = geom
        else:
            try:
                g = shape(geom)
            except Exception:
                continue
            if g.is_empty:
                continue
            if not g.is_valid:
                g = g.buffer(0)
                if g.is_empty:
                    continue
            g_out = mapping(to_utm(g))
        out_feats.append({"type": "Feature", "properties": props, "geometry": g_out})
        f = props.get("num_floors")
        floors[f] += 1
        if f:
            n_with_floors += 1
        unit_floors[props.get("unit_floor")] += 1
        use[props.get("use_code")] += 1
        objt[props.get("obj_type")] += 1
        yr[props.get("change_year")] += 1

    crs_epsg = SRC_EPSG if args.no_reproject else UTM_EPSG
    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name",
                "properties": {"name": f"urn:ogc:def:crs:EPSG::{crs_epsg}"}},
        "features": out_feats,
    }
    with open(out_geojson, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    n = len(out_feats)
    print(f"  wrote {n} buildings -> {out_geojson} (EPSG:{crs_epsg})")

    stats = {
        "source": "AKN cadastre WFS Public:RM_OBJECTS",
        "wfs_url": WFS_URL,
        "layer": args.layer,
        "native_crs": f"EPSG:{SRC_EPSG}",
        "output_crs": f"EPSG:{crs_epsg}",
        "n_footprints": n,
        "n_parcels": len(parcel_max),
        "n_with_floors": n_with_floors,
        "pct_with_floors": round(100.0 * n_with_floors / n, 1) if n else 0.0,
        "note_bldn": ("BLDN = per-unit floor index (кат), NOT building storeys. "
                      "num_floors/height are MAX(BLDN) per parcel (id_parc)."),
        "building_floors_histogram": dict(sorted(
            (str(k), v) for k, v in floors.items())),
        "raw_unit_floor_histogram": dict(sorted(
            (str(k), v) for k, v in unit_floors.items() if k is not None)),
        "use_code_histogram": dict(sorted((str(k), v) for k, v in use.items()
                                          if k is not None)),
        "obj_type_histogram": dict(sorted((str(k), v) for k, v in objt.items()
                                          if k is not None)),
        "change_year_histogram": dict(sorted((str(k), v) for k, v in yr.items()
                                             if k is not None)),
    }
    with open(out_stats, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    print(f"  stats -> {out_stats}")
    print(f"  with-floors: {n_with_floors}/{n} "
          f"({stats['pct_with_floors']}%)  | top use codes: "
          f"{list(stats['use_code_histogram'].items())[:5]}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", choices=["central", "full"], default="central",
                    help="'central' = ~6x6 km Skopje proof set (default); "
                         "'full' = whole 69x69 km landscape bbox")
    ap.add_argument("--layer", default=DEFAULT_LAYER,
                    help=f"WFS building layer (default {DEFAULT_LAYER})")
    ap.add_argument("--tile", type=float, default=250.0,
                    help="tile edge in metres (EPSG:6316). Smaller = safer vs the "
                         "fragile proxy. Default 250.")
    ap.add_argument("--delay", type=float, default=BASE_DELAY_S,
                    help=f"base seconds between requests (default {BASE_DELAY_S})")
    ap.add_argument("--max-tiles", type=int, default=0,
                    help="process at most N top-level tiles then stop (quick sample)")
    ap.add_argument("--resume", action="store_true",
                    help="skip tiles already marked done in the ledger")
    ap.add_argument("--bbox-src", default=None,
                    help="manual EPSG:6316 bbox 'minx,miny,maxx,maxy' (overrides region)")
    ap.add_argument("--no-reproject", action="store_true",
                    help="keep geometry in EPSG:6316 (debug; default reprojects to 32634)")
    ap.add_argument("--write-only", action="store_true",
                    help="don't scrape; rebuild the GeoJSON+stats from the tile cache")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
