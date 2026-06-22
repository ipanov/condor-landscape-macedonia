"""
ms_footprints.py — Microsoft GlobalMLBuildingFootprints loader for North Macedonia.

Fetches ML-detected building polygons (footprints, optional height, confidence)
from the Microsoft Global Building Footprints dataset
(https://github.com/microsoft/GlobalMLBuildingFootprints), reprojects them to
UTM 34N (EPSG:32634), and returns them as dicts with shapely geometry.

PRIMARY method: quadkey-based GeoJSONL tiles from the public blob index
  (https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv).
  North Macedonia is stored under Location = "FYROMakedonija" at zoom-9 quadkeys.
  Tiles are cached under .sandbox/ms_buildings/<location>/<quadkey>.geojsonl.gz.

FALLBACK method: Microsoft Planetary Computer STAC + GeoParquet delta tables.
  Requires planetary-computer + pystac-client + geopandas (all pip-installable).

API
---
fetch_buildings(lat, lon, radius_m=500) -> list[dict]
fetch_buildings_bbox(min_lon, min_lat, max_lon, max_lat) -> list[dict]
Each dict = {'geom': shapely.Polygon in EPSG:32634,
             'height': float | None,        # metres; None if MS reports -1
             'confidence': float | None}    # 0-1; None if MS reports -1

CLI
---
python scripts/ms_footprints.py <lat> <lon> [radius_m]

    Prints each building near <lat,lon> (centroid UTM, min-rotated-rect L x W,
    long-axis azimuth, height) and saves .sandbox/ms_buildings/<slug>.geojson.

Example:
    python scripts/ms_footprints.py 42.0594 21.3888 500
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import sys
from pathlib import Path
from typing import Generator

import pyproj
import requests
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIR = _REPO_ROOT / ".sandbox" / "ms_buildings"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Dataset-links index URL (blob storage mirror, stable)
_DATASET_LINKS_URL = (
    "https://minedbuildings.z5.web.core.windows.net"
    "/global-buildings/dataset-links.csv"
)
_DATASET_LINKS_CACHE = _CACHE_DIR / "dataset-links.csv"

# Coordinate systems
_WGS84 = pyproj.CRS.from_epsg(4326)
_UTM34N = pyproj.CRS.from_epsg(32634)
_TO_UTM = pyproj.Transformer.from_crs(_WGS84, _UTM34N, always_xy=True)
_FROM_UTM = pyproj.Transformer.from_crs(_UTM34N, _WGS84, always_xy=True)

# ---------------------------------------------------------------------------
# Dataset-links index helpers
# ---------------------------------------------------------------------------

def _fetch_dataset_links(force: bool = False) -> list[dict]:
    """Download (or load from cache) the dataset-links.csv index.

    Returns a list of row dicts with keys: Location, QuadKey, Url, Size, UploadDate.
    Cache lifetime is 7 days (re-download when stale).
    """
    import time
    if not force and _DATASET_LINKS_CACHE.exists():
        age_days = (time.time() - _DATASET_LINKS_CACHE.stat().st_mtime) / 86400
        if age_days < 7:
            text = _DATASET_LINKS_CACHE.read_text(encoding="utf-8")
            return list(csv.DictReader(io.StringIO(text)))

    resp = requests.get(_DATASET_LINKS_URL, timeout=60)
    resp.raise_for_status()
    text = resp.text
    _DATASET_LINKS_CACHE.write_text(text, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _rows_for_locations(location_names: list[str]) -> list[dict]:
    """Return all index rows whose Location matches any name in location_names."""
    rows = _fetch_dataset_links()
    loc_set = {n.lower() for n in location_names}
    return [r for r in rows if r.get("Location", "").lower() in loc_set]


# ---------------------------------------------------------------------------
# Quadkey helpers (uses mercantile)
# ---------------------------------------------------------------------------

def _quadkeys_covering_bbox(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float,
    zoom: int = 9,
) -> set[str]:
    """Return the set of zoom-9 quadkeys that overlap the given WGS-84 bbox."""
    import mercantile  # installed by requirements; pip install mercantile
    tiles = list(mercantile.tiles(min_lon, min_lat, max_lon, max_lat, zooms=zoom))
    return {mercantile.quadkey(t) for t in tiles}


# ---------------------------------------------------------------------------
# Tile download + parse
# ---------------------------------------------------------------------------

_LOCATION_NAMES = [
    # All known spellings of North Macedonia in the MS index
    "fyromakedonija",
    "northmacedonia",
    "macedonia",
]


def _tile_cache_path(url: str, location: str, quadkey: str) -> Path:
    loc_dir = _CACHE_DIR / location
    loc_dir.mkdir(parents=True, exist_ok=True)
    # Use just the quadkey as the filename (the URL part number is irrelevant)
    return loc_dir / f"{quadkey}.geojsonl.gz"


def _download_tile(url: str, cache_path: Path) -> bytes:
    """Download a tile .gz if not already cached; return raw gzipped bytes."""
    if cache_path.exists():
        return cache_path.read_bytes()
    resp = requests.get(url, timeout=180, stream=True)
    resp.raise_for_status()
    data = resp.content
    cache_path.write_bytes(data)
    return data


def _iter_features(gz_bytes: bytes) -> Generator[dict, None, None]:
    """Decompress a .gz tile and yield parsed GeoJSON feature dicts."""
    text = gzip.decompress(gz_bytes).decode("utf-8")
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _feature_to_dict(feat: dict) -> dict | None:
    """Convert one MS GeoJSON feature to our output dict (geometry in UTM34N).

    Returns None if the geometry is invalid or not a Polygon/MultiPolygon.
    """
    try:
        geom_wgs = shape(feat["geometry"])
        if geom_wgs.is_empty or not geom_wgs.is_valid:
            return None
    except Exception:
        return None

    # Reproject to UTM 34N
    geom_utm = shp_transform(
        lambda x, y, z=None: _TO_UTM.transform(x, y),
        geom_wgs,
    )

    props = feat.get("properties") or {}
    height_raw = props.get("height", -1)
    conf_raw = props.get("confidence", -1)

    return {
        "geom": geom_utm,
        "height": float(height_raw) if height_raw is not None and float(height_raw) > 0 else None,
        "confidence": float(conf_raw) if conf_raw is not None and float(conf_raw) > 0 else None,
    }


# ---------------------------------------------------------------------------
# Primary method: quadkey tiles
# ---------------------------------------------------------------------------

def _fetch_primary(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> list[dict] | None:
    """Primary: quadkey GeoJSONL tiles.

    Returns list of building dicts, or None if the method fails.
    """
    try:
        import mercantile  # noqa: F401  (just confirm it's available)
    except ImportError:
        print("[ms_footprints] mercantile not installed; skipping primary method.", file=sys.stderr)
        return None

    try:
        # 1. Find all index rows for North Macedonia
        mk_rows = _rows_for_locations(_LOCATION_NAMES)
        if not mk_rows:
            print("[ms_footprints] No rows found for North Macedonia in dataset-links.csv.", file=sys.stderr)
            return None

        # Build a lookup: quadkey -> row
        qk_to_row = {r["QuadKey"]: r for r in mk_rows}

        # 2. Find which zoom-9 quadkeys the bbox needs
        needed_qks = _quadkeys_covering_bbox(min_lon, min_lat, max_lon, max_lat, zoom=9)

        # 3. Download & parse matching tiles
        results: list[dict] = []
        for qk in needed_qks:
            row = qk_to_row.get(qk)
            if row is None:
                # Quadkey not in the dataset (no buildings / different region)
                continue
            location = row["Location"]
            url = row["Url"]
            cache_path = _tile_cache_path(url, location, qk)
            print(f"[ms_footprints] Tile {qk} ({row['Size']}) ...", file=sys.stderr)
            gz_bytes = _download_tile(url, cache_path)

            for feat in _iter_features(gz_bytes):
                d = _feature_to_dict(feat)
                if d is None:
                    continue
                # Spatial filter: centroid must be inside the bbox
                c = d["geom"].centroid
                cx, cy = _FROM_UTM.transform(c.x, c.y)  # back to WGS84
                if min_lon <= cx <= max_lon and min_lat <= cy <= max_lat:
                    results.append(d)

        return results

    except Exception as exc:
        print(f"[ms_footprints] Primary method error: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Fallback method: Planetary Computer STAC + GeoParquet
# ---------------------------------------------------------------------------

def _fetch_fallback(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> list[dict]:
    """Fallback: Microsoft Planetary Computer STAC.

    pip install planetary-computer pystac-client geopandas
    """
    try:
        import planetary_computer as pc
        import pystac_client
        import geopandas as gpd
    except ImportError as exc:
        raise RuntimeError(
            f"Fallback requires planetary-computer + pystac-client + geopandas: {exc}"
        )

    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=pc.sign_inplace,
    )

    aoi = {
        "type": "Polygon",
        "coordinates": [[
            [min_lon, min_lat],
            [max_lon, min_lat],
            [max_lon, max_lat],
            [min_lon, max_lat],
            [min_lon, min_lat],
        ]],
    }

    search = catalog.search(
        collections=["ms-buildings"],
        intersects=aoi,
        max_items=100,
    )
    items = list(search.items())
    if not items:
        return []

    results: list[dict] = []
    for item in items:
        for asset_key, asset in item.assets.items():
            if asset.media_type and "parquet" in asset.media_type:
                try:
                    gdf = gpd.read_parquet(asset.href, storage_options={"account_name": "minedbuildings"})
                    gdf_clip = gdf.cx[min_lon:max_lon, min_lat:max_lat]
                    for _, row in gdf_clip.iterrows():
                        geom_wgs = row.geometry
                        geom_utm = shp_transform(
                            lambda x, y, z=None: _TO_UTM.transform(x, y),
                            geom_wgs,
                        )
                        h = row.get("height", -1)
                        c = row.get("confidence", -1)
                        results.append({
                            "geom": geom_utm,
                            "height": float(h) if h and float(h) > 0 else None,
                            "confidence": float(c) if c and float(c) > 0 else None,
                        })
                except Exception as e:
                    print(f"[ms_footprints] Parquet asset failed: {e}", file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_buildings_bbox(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> list[dict]:
    """Fetch MS buildings within a WGS-84 bbox.

    Returns list of dicts:
      {'geom': Polygon (EPSG:32634), 'height': float|None, 'confidence': float|None}

    Tries the quadkey primary method first; falls back to Planetary Computer.
    """
    result = _fetch_primary(min_lon, min_lat, max_lon, max_lat)
    if result is not None:
        return result
    print("[ms_footprints] Primary failed, trying Planetary Computer fallback...", file=sys.stderr)
    return _fetch_fallback(min_lon, min_lat, max_lon, max_lat)


def fetch_buildings(lat: float, lon: float, radius_m: float = 500.0) -> list[dict]:
    """Fetch MS buildings within radius_m metres of (lat, lon).

    Converts radius to a bbox in WGS-84 (slightly over-queries, then
    distance-filters in UTM so the result is a true circle).

    Returns list of dicts (same as fetch_buildings_bbox).
    """
    # Convert radius to degrees (approximate; fine for <= 10 km)
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))

    min_lon = lon - dlon
    max_lon = lon + dlon
    min_lat = lat - dlat
    max_lat = lat + dlat

    candidates = fetch_buildings_bbox(min_lon, min_lat, max_lon, max_lat)

    # Distance-filter by centroid in UTM
    cx, cy = _TO_UTM.transform(lon, lat)
    results = []
    for d in candidates:
        c = d["geom"].centroid
        dist = math.sqrt((c.x - cx) ** 2 + (c.y - cy) ** 2)
        if dist <= radius_m:
            d["_dist_m"] = dist  # internal convenience; not part of the spec
            results.append(d)
    results.sort(key=lambda d: d.get("_dist_m", 0))
    return results


# ---------------------------------------------------------------------------
# Geometry helpers for the CLI
# ---------------------------------------------------------------------------

def _min_rotated_rect_dims(geom_utm) -> tuple[float, float, float]:
    """Return (length_m, width_m, long_axis_azimuth_deg) from the MRR."""
    mrr = geom_utm.minimum_rotated_rectangle
    coords = list(mrr.exterior.coords)
    dx1 = coords[1][0] - coords[0][0]
    dy1 = coords[1][1] - coords[0][1]
    dx2 = coords[2][0] - coords[1][0]
    dy2 = coords[2][1] - coords[1][1]
    s1 = math.sqrt(dx1 ** 2 + dy1 ** 2)
    s2 = math.sqrt(dx2 ** 2 + dy2 ** 2)
    if s1 >= s2:
        L, W = s1, s2
        az = math.degrees(math.atan2(dx1, dy1)) % 360
    else:
        L, W = s2, s1
        az = math.degrees(math.atan2(dx2, dy2)) % 360
    return L, W, az


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cli(lat: float, lon: float, radius_m: float, save_slug: str) -> None:
    print(f"Fetching MS buildings within {radius_m:.0f} m of ({lat}, {lon}) ...", file=sys.stderr)
    buildings = fetch_buildings(lat, lon, radius_m)

    cx, cy = _TO_UTM.transform(lon, lat)
    print(f"\nCenter UTM34N: E={cx:.1f} N={cy:.1f}")
    print(f"MS buildings found: {len(buildings)}\n")

    features_out = []
    for i, b in enumerate(buildings, 1):
        geom = b["geom"]
        c = geom.centroid
        dist = b.get("_dist_m", math.sqrt((c.x - cx) ** 2 + (c.y - cy) ** 2))
        L, W, az = _min_rotated_rect_dims(geom)
        h_str = f"{b['height']:.1f} m" if b["height"] is not None else "no height (-1)"
        conf_str = f"{b['confidence']:.3f}" if b["confidence"] is not None else "-1"
        print(
            f"  #{i:3d}  dist={dist:6.0f}m  centroid E={c.x:.1f} N={c.y:.1f}"
            f"  MRR={L:.1f}x{W:.1f}m  az={az:.0f}deg"
            f"  height={h_str}  conf={conf_str}"
        )
        # Accumulate GeoJSON feature (WGS84 for easy viewing in GIS)
        geom_wgs = shp_transform(lambda x, y, z=None: _FROM_UTM.transform(x, y), geom)
        features_out.append({
            "type": "Feature",
            "properties": {
                "dist_m": round(dist, 1),
                "L_m": round(L, 1),
                "W_m": round(W, 1),
                "azimuth_deg": round(az, 1),
                "height_m": b["height"],
                "confidence": b["confidence"],
            },
            "geometry": mapping(geom_wgs),
        })

    # OSM comparison
    _compare_osm(lat, lon, radius_m, cx, cy)

    # Save GeoJSON
    out_path = _CACHE_DIR / f"{save_slug}.geojson"
    fc = {"type": "FeatureCollection", "features": features_out}
    out_path.write_text(json.dumps(fc, indent=2), encoding="utf-8")
    print(f"\nSaved {len(features_out)} features -> {out_path}")


def _compare_osm(lat: float, lon: float, radius_m: float, cx: float, cy: float) -> None:
    """Load cached OSM buildings and compare count + sizes with the MS result."""
    osm_path = _REPO_ROOT / ".sandbox" / "osm" / "buildings.geojson"
    if not osm_path.exists():
        print("\n[OSM comparison] No cached .sandbox/osm/buildings.geojson found.")
        return

    with open(osm_path, encoding="utf-8") as f:
        data = json.load(f)

    osm_nearby = []
    for feat in data.get("features", []):
        try:
            geom_wgs = shape(feat["geometry"])
        except Exception:
            continue
        c = geom_wgs.centroid
        ex, ny = _TO_UTM.transform(c.x, c.y)
        dist = math.sqrt((ex - cx) ** 2 + (ny - cy) ** 2)
        if dist <= radius_m:
            utm_coords = [_TO_UTM.transform(p[0], p[1]) for p in geom_wgs.exterior.coords]
            geom_utm = shape({"type": "Polygon", "coordinates": [utm_coords]})
            L, W, az = _min_rotated_rect_dims(geom_utm)
            props = feat.get("properties", {})
            h = props.get("height") or props.get("building:height")
            osm_nearby.append({
                "dist": dist, "L": L, "W": W, "area": geom_utm.area,
                "height": float(h) if h else None,
                "btype": props.get("building", "yes"),
            })
    osm_nearby.sort(key=lambda x: x["dist"])

    print(f"\n--- OSM comparison (within {radius_m:.0f} m) ---")
    print(f"OSM buildings: {len(osm_nearby)}")
    osm_with_height = [b for b in osm_nearby if b["height"] is not None]
    print(f"OSM buildings with height: {len(osm_with_height)}")
    for b in osm_nearby:
        h_str = f"{b['height']:.1f} m" if b["height"] is not None else "no height"
        print(f"  dist={b['dist']:.0f}m  MRR={b['L']:.1f}x{b['W']:.1f}m  type={b['btype']}  height={h_str}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python ms_footprints.py <lat> <lon> [radius_m]")
        print("Example: python ms_footprints.py 42.0594 21.3888 500")
        sys.exit(1)

    _lat = float(sys.argv[1])
    _lon = float(sys.argv[2])
    _radius = float(sys.argv[3]) if len(sys.argv) > 3 else 500.0

    # Derive a safe slug for the output file name
    _slug = f"lat{_lat:.4f}_lon{_lon:.4f}_r{_radius:.0f}".replace(".", "p").replace("-", "m")
    # Special-case the Stenkovec CLI example
    if abs(_lat - 42.0594) < 0.001 and abs(_lon - 21.3888) < 0.001:
        _slug = "stenkovec"

    _cli(_lat, _lon, _radius, _slug)
