#!/usr/bin/env python3
"""
Download building footprints for the Skopje (North Macedonia) landscape bbox and
reproject them to UTM 34N (EPSG:32634), ready for place_buildings.py.

Sources, in priority order
--------------------------
1. Overture Maps "buildings" theme  (primary)
     - License: ODbL / CDLA-Permissive-2.0 (mixed; OSM-derived parts are ODbL).
     - Preferred access: the `overturemaps` Python CLI (pip install overturemaps),
       which streams a bbox slice straight to GeoParquet/GeoJSON from the public
       Azure/AWS GeoParquet release. DuckDB with the spatial+httpfs extensions is
       an equivalent path.
     - Overture buildings carry `height` and `num_floors` (when known), plus the
       full OSM-derived footprint geometry.

2. Microsoft GlobalMLBuildingFootprints  (fill / fallback)
     - License: ODbL (the 1.4B global release; the README also references CDLA-2.0
       for earlier national drops). RegionName for North Macedonia = "FYROMakedonija".
     - Manifest: dataset-links.csv lists per-(RegionName, quadkey) GeoJSONL `.csv.gz`
       URLs. The North-Macedonia quadkeys live under the `120233330*` prefix
       (z9 quadkey 120233330 covers the Skopje basin).
     - ML footprints are machine-traced polygons with NO height/levels attributes.

3. OSM buildings  (always-available local fallback)
     - The repo already has 67k OSM footprints cached at
       .sandbox/osm/buildings.geojson (ODbL). 2,183 of them carry
       `building:levels`, 346 carry `height` -- the only height signal we get.

Output
------
All outputs land in .sandbox/buildings/ as EPSG:32634 GeoJSON (place_buildings.py
consumes whichever is present, preferring the richest source):
    overture_buildings_utm.geojson      (if overturemaps/duckdb available)
    msft_buildings_utm.geojson          (if downloaded)
    osm_buildings_utm.geojson           (always, from the cached OSM geojson)
    buildings_combined_utm.geojson      (deduped union actually used downstream)

This script NEVER writes into C:/Condor2 and NEVER opens a GUI. If a tool is
missing it prints the exact command to install/run it and falls back to OSM.
"""

import csv
import gzip
import io
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import pyproj
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

OUT_DIR = ROOT / ".sandbox" / "buildings"
OSM_BUILDINGS = ROOT / ".sandbox" / "osm" / "buildings.geojson"

# Skopje landscape bbox (WGS84). The landscape footprint (EPSG:32634) spans
# lon 21.08..21.92 / lat 41.83..42.45; we pad west to the task-specified
# 20.96 so anything that could project into the western patches is captured.
BBOX_W, BBOX_S, BBOX_E, BBOX_N = 20.96, 41.83, 21.92, 42.45

# Microsoft GlobalMLBuildingFootprints.
# The anonymous .blob.core.windows.net host now returns HTTP 409 ("Public access
# is not permitted"); the static-website host below is the live one (verified
# HTTP 200, ~7 MB). Manifest columns: Location,QuadKey,Url,Size,UploadDate.
MSFT_MANIFEST = "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
MSFT_REGION = "FYROMakedonija"
# z9 quadkey for the Skopje basin (verified present in the manifest, 13.9 MB).
# We match any quadkey that starts with this prefix in case of deeper tiling.
MSFT_QUADKEY_PREFIX = "120233330"

UTM_EPSG = 32634
WGS84_EPSG = 4326

_TO_UTM = pyproj.Transformer.from_crs(WGS84_EPSG, UTM_EPSG, always_xy=True)


def _to_utm(geom):
    return shp_transform(lambda x, y, z=None: _TO_UTM.transform(x, y), geom)


def _in_bbox_lonlat(lon, lat):
    return BBOX_W <= lon <= BBOX_E and BBOX_S <= lat <= BBOX_N


# ---------------------------------------------------------------------------
# Source 3: OSM (always available -- the guaranteed fallback)
# ---------------------------------------------------------------------------
def build_osm_utm():
    """Reproject the cached OSM building geojson to UTM 34N."""
    if not OSM_BUILDINGS.exists():
        print(f"[osm] cached file missing: {OSM_BUILDINGS}")
        return None, 0
    with open(OSM_BUILDINGS, "r", encoding="utf-8") as f:
        fc = json.load(f)

    out_feats = []
    for feat in fc.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if g.is_empty or not g.is_valid:
            g = g.buffer(0) if not g.is_empty else g
            if g.is_empty or not g.is_valid:
                continue
        props = dict(feat.get("properties", {}))
        props["src"] = "osm"
        out_feats.append({
            "type": "Feature",
            "properties": props,
            "geometry": mapping(_to_utm(g)),
        })

    out = OUT_DIR / "osm_buildings_utm.geojson"
    _write_fc(out, out_feats)
    print(f"[osm] reprojected {len(out_feats)} footprints -> {out.name}")
    return out, len(out_feats)


# ---------------------------------------------------------------------------
# Source 1: Overture Maps (primary)
# ---------------------------------------------------------------------------
def try_overture_cli():
    """Use the `overturemaps` CLI if it is installed."""
    exe = shutil.which("overturemaps")
    if not exe:
        print("[overture] CLI not installed. To enable the PRIMARY source run:")
        print("    pip install overturemaps")
        print("  then:")
        print(f"    overturemaps download --bbox={BBOX_W},{BBOX_S},{BBOX_E},{BBOX_N} \\")
        print("        -f geojson --type=building \\")
        print(f"        -o {OUT_DIR / 'overture_buildings_wgs84.geojson'}")
        return None, 0

    raw = OUT_DIR / "overture_buildings_wgs84.geojson"
    cmd = [
        exe, "download",
        f"--bbox={BBOX_W},{BBOX_S},{BBOX_E},{BBOX_N}",
        "-f", "geojson", "--type=building",
        "-o", str(raw),
    ]
    print(f"[overture] running: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"[overture] CLI failed: {exc}")
        return None, 0
    return _reproject_overture(raw)


def try_overture_duckdb():
    """Use DuckDB (spatial+httpfs) to slice the public GeoParquet release."""
    try:
        import duckdb  # noqa: F401
    except ImportError:
        print("[overture] duckdb not installed. Alternative PRIMARY path:")
        print("    pip install duckdb")
        print("  DuckDB SQL (release 2024-xx; bump to the latest):")
        print("    INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
        print("    COPY (SELECT id, names.primary AS name, height, num_floors,")
        print("                 ST_AsWKB(geometry) AS geometry")
        print("          FROM read_parquet('s3://overturemaps-us-west-2/release/"
              "2024-09-18.0/theme=buildings/type=building/*', filename=true,"
              " hive_partitioning=1)")
        print(f"          WHERE bbox.xmin>{BBOX_W} AND bbox.xmax<{BBOX_E}")
        print(f"            AND bbox.ymin>{BBOX_S} AND bbox.ymax<{BBOX_N})")
        print("    TO 'overture_buildings.geojson' WITH (FORMAT GDAL,"
              " DRIVER 'GeoJSON');")
        return None, 0

    import duckdb
    raw = OUT_DIR / "overture_buildings_wgs84.geojson"
    release = "2024-09-18.0"
    base = (f"s3://overturemaps-us-west-2/release/{release}/"
            "theme=buildings/type=building/*")
    sql = f"""
        INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;
        SET s3_region='us-west-2';
        COPY (
            SELECT id,
                   names.primary AS name,
                   height,
                   num_floors,
                   geometry
            FROM read_parquet('{base}', hive_partitioning=1)
            WHERE bbox.xmin > {BBOX_W} AND bbox.xmax < {BBOX_E}
              AND bbox.ymin > {BBOX_S} AND bbox.ymax < {BBOX_N}
        ) TO '{raw.as_posix()}' WITH (FORMAT GDAL, DRIVER 'GeoJSON');
    """
    print("[overture] querying public GeoParquet via DuckDB (needs network)...")
    try:
        duckdb.sql(sql)
    except Exception as exc:
        print(f"[overture] DuckDB query failed (offline or release moved): {exc}")
        return None, 0
    return _reproject_overture(raw)


def _reproject_overture(raw_path):
    if not raw_path.exists():
        return None, 0
    with open(raw_path, "r", encoding="utf-8") as f:
        fc = json.load(f)
    feats = []
    for feat in fc.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if g.is_empty or not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            continue
        src_props = feat.get("properties", {}) or {}
        props = {
            "src": "overture",
            "name": src_props.get("name"),
            "height": src_props.get("height"),
            "num_floors": src_props.get("num_floors"),
        }
        feats.append({
            "type": "Feature",
            "properties": props,
            "geometry": mapping(_to_utm(g)),
        })
    out = OUT_DIR / "overture_buildings_utm.geojson"
    _write_fc(out, feats)
    print(f"[overture] reprojected {len(feats)} footprints -> {out.name}")
    return out, len(feats)


# ---------------------------------------------------------------------------
# Source 2: Microsoft GlobalMLBuildingFootprints (fill)
# ---------------------------------------------------------------------------
def try_microsoft():
    """Download MS ML footprints for FYROMakedonija quadkeys intersecting bbox."""
    try:
        print(f"[msft] fetching manifest {MSFT_MANIFEST}")
        with urllib.request.urlopen(MSFT_MANIFEST, timeout=60) as resp:
            manifest_text = resp.read().decode("utf-8")
    except Exception as exc:
        print(f"[msft] manifest download failed (offline?): {exc}")
        print("[msft] manual command:")
        print(f"    curl -L -o dataset-links.csv {MSFT_MANIFEST}")
        print(f"    grep {MSFT_REGION} dataset-links.csv   # find quadkey URLs")
        return None, 0

    rows = list(csv.DictReader(io.StringIO(manifest_text)))

    def _region(r):
        # Manifest header is "Location"; older drops used "RegionName".
        return r.get("Location") or r.get("RegionName") or ""

    targets = [
        r for r in rows
        if _region(r) == MSFT_REGION
        and str(r.get("QuadKey", "")).startswith(MSFT_QUADKEY_PREFIX)
    ]
    if not targets:
        # Fall back to all FYROMakedonija quadkeys (then clip by bbox afterwards).
        targets = [r for r in rows if _region(r) == MSFT_REGION]
    print(f"[msft] {len(targets)} manifest rows for {MSFT_REGION} "
          f"(quadkey prefix {MSFT_QUADKEY_PREFIX})")
    if not targets:
        print(f"[msft] region {MSFT_REGION!r} not found; check spelling in manifest")
        return None, 0

    feats = []
    for r in targets:
        url = r.get("Url") or r.get("url")
        qk = r.get("QuadKey")
        if not url:
            continue
        try:
            print(f"[msft] downloading quadkey {qk}")
            with urllib.request.urlopen(url, timeout=120) as resp:
                raw = resp.read()
            # The payload is gzipped line-delimited GeoJSON wrapped in a CSV with a
            # single `geometry` column header.
            text = gzip.decompress(raw).decode("utf-8")
        except Exception as exc:
            print(f"[msft] quadkey {qk} failed: {exc}")
            continue
        feats.extend(_parse_msft_geojsonl(text))

    out = OUT_DIR / "msft_buildings_utm.geojson"
    _write_fc(out, feats)
    print(f"[msft] reprojected {len(feats)} footprints (bbox-clipped) -> {out.name}")
    return out, len(feats)


def _parse_msft_geojsonl(text):
    feats = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.lower() == "geometry":
            continue
        # Each line is either a bare GeoJSON Feature or a CSV cell that *is* the
        # Feature JSON. Try to locate the JSON object.
        start = line.find("{")
        if start < 0:
            continue
        try:
            obj = json.loads(line[start:])
        except Exception:
            continue
        geom = obj.get("geometry", obj if obj.get("type") == "Polygon" else None)
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if g.is_empty:
            continue
        c = g.centroid
        if not _in_bbox_lonlat(c.x, c.y):
            continue
        if not g.is_valid:
            g = g.buffer(0)
            if g.is_empty:
                continue
        feats.append({
            "type": "Feature",
            "properties": {"src": "msft"},
            "geometry": mapping(_to_utm(g)),
        })
    return feats


# ---------------------------------------------------------------------------
# Combine / dedupe
# ---------------------------------------------------------------------------
def combine(layers):
    """Union of all layers, deduping MS/Overture against OSM by centroid proximity.

    OSM + Overture (which carry height) take priority. Microsoft ML footprints are
    only added where no higher-priority footprint already covers that spot
    (centroid within 8 m), so we never double-stack the same building.
    """
    from shapely.strtree import STRtree

    priority = ["overture", "osm", "msft"]
    layers_by_src = {}
    for path, _ in layers:
        if path is None:
            continue
        with open(path, "r", encoding="utf-8") as f:
            fc = json.load(f)
        for feat in fc["features"]:
            src = feat["properties"].get("src", "osm")
            layers_by_src.setdefault(src, []).append(feat)

    kept = []
    kept_geoms = []
    for src in priority:
        feats = layers_by_src.get(src, [])
        if not feats:
            continue
        if not kept_geoms:
            kept.extend(feats)
            kept_geoms.extend(shape(f["geometry"]).centroid for f in feats)
            continue
        tree = STRtree(kept_geoms)
        added = 0
        for feat in feats:
            c = shape(feat["geometry"]).centroid
            idxs = tree.query(c.buffer(8.0))
            if len(idxs) == 0:
                kept.append(feat)
                kept_geoms.append(c)
                added += 1
        print(f"[combine] {src}: added {added}/{len(feats)} "
              f"(rest deduped against higher-priority sources)")

    out = OUT_DIR / "buildings_combined_utm.geojson"
    _write_fc(out, kept)
    print(f"[combine] total {len(kept)} footprints -> {out.name}")
    return out, len(kept)


# ---------------------------------------------------------------------------
def _write_fc(path, feats):
    path.parent.mkdir(parents=True, exist_ok=True)
    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name",
                "properties": {"name": f"urn:ogc:def:crs:EPSG::{UTM_EPSG}"}},
        "features": feats,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Building-footprint download -> {OUT_DIR}")
    print(f"bbox WGS84: W={BBOX_W} S={BBOX_S} E={BBOX_E} N={BBOX_N}\n")

    layers = []

    # 1. Overture (primary) -- CLI first, then DuckDB.
    ov = try_overture_cli()
    if ov[0] is None:
        ov = try_overture_duckdb()
    layers.append(ov)

    # 2. Microsoft (fill).
    layers.append(try_microsoft())

    # 3. OSM (always).
    osm = build_osm_utm()
    layers.append(osm)

    print()
    combined = combine(layers)

    print("\nSummary")
    for name, res in [("overture", layers[0]), ("microsoft", layers[1]),
                      ("osm", layers[2]), ("combined", combined)]:
        n = res[1] if res and res[0] else 0
        print(f"  {name:10s}: {n:7d} footprints")
    print(f"\nDownstream consumer (place_buildings.py) reads:"
          f"\n  {combined[0]}")


if __name__ == "__main__":
    main()
