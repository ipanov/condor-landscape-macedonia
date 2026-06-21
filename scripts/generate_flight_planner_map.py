#!/usr/bin/env python3
"""
Generate a Slovenia2-style topographic flight-planner map for the
MacedoniaSkopje Condor 2 landscape.

The script:
  * reads the canonical exactly-30 m DEM (macedonia_skopje_dem_30m_2305.raw,
    int16, 2305x2305, NW pixel-centre 506880/4700160) and resamples it to the
    map master size
  * computes a cool, desaturated hypsometric tint + a SOFT hillshaded relief
    (palette + shading measured from Slovenia2.bmp)
  * overlays OpenStreetMap vector layers in Slovenia2 colours:
      - water polygons (lakes/reservoirs)      RGB (21, 85,149)
      - rivers / streams                        RGB (40,110,170)
      - built-up / settlement areas (yellow)    RGB (243,227, 16)
      - roads by class (orange majors -> dark minors)
      - railways (near-black, thin)             RGB (31, 33, 32)
  * adds a faint UTM / tile grid, town labels, and airport markers
  * builds the map at a higher-resolution master, then LANCZOS-downsamples to
    768x768 (matching the .trn dimensions) while keeping lines >= 2 px at the
    final scale, and writes a 32-bit Windows BMP via Pillow.

Vector data is fetched from the Overpass API and cached in .sandbox/osm/.
Cached files are reused when present so the script can run offline after the
first download.

IMPORTANT (channel order): the final BMP is written with Pillow via
``Image.fromarray(rgb, "RGB").convert("RGBA").save(path, "BMP")``. Pillow handles
channel + row order; do NOT hand-pack BGRA bytes.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.parse
from pathlib import Path

import numpy as np
import pyproj
import requests
from PIL import Image, ImageDraw
from shapely.geometry import LineString, shape
from shapely.ops import transform as shapely_transform
from tqdm import tqdm

# Condor landscape calibration (UTM 34N). Grid-driven via condor_grid:
# CONDOR_LANDSCAPE switches skopje (768x768 .bmp) <-> nm (2560x2048 .bmp). The
# .bmp MUST match the .trn overview dimensions (patches x 64); the script renders
# at a 3x master then LANCZOS-downsamples. BOUNDS_UTM is derived from the grid so
# relief + OSM overlays register pixel-perfectly for either landscape.
import condor_grid as _g
from condor_grid import (
    BR_EASTING,
    BR_NORTHING,
    TILE_SIZE_M,
    TILES_X,
    TILES_Y,
    UTM_CRS,
    WGS84_CRS,
    ULXMAP,
    ULYMAP,
    LANDSCAPE_NAME,
)
from rasterize import rasterize_mask

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_NM = LANDSCAPE_NAME == "NorthMacedonia"

# Canonical exactly-30 m DEM (int16, patches*192+1 per side, NW pixel-centre =
# ULXMAP/ULYMAP, negatives/NoData clamped to 0). Its extent equals BOUNDS_UTM, so
# a straight resize keeps relief registered with the OSM overlays.
#   skopje: macedonia_skopje_dem_30m_2305.raw  (2305x2305)
#   nm    : northmacedonia_dem_30m_7681x6145.raw (7681x6145)
if _NM:
    DEM_PATH = PROJECT_ROOT / "sources" / "dem" / f"northmacedonia_dem_30m_{_g.WIDTH}x{_g.HEIGHT}.raw"
else:
    DEM_PATH = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305.raw"
DEM_W = _g.WIDTH
DEM_H = _g.HEIGHT
DEM_CELL_M = 30.0
# Per-landscape OSM cache (download_osm_nm.py writes .sandbox/osm_nm for NM).
OSM_DIR = PROJECT_ROOT / ".sandbox" / ("osm_nm" if _NM else "osm")
OUT_DIR = PROJECT_ROOT / "output" / "maps"
AIRPORTS_JSON = PROJECT_ROOT / "data" / ("airports_nm.json" if _NM else "airports.json")

# Output sizes = .trn overview (patches x 64). MASTER is an exact 3x multiple per
# axis so the LANCZOS downsample is clean and line-width scaling is predictable
# (a 6 px master line -> 2 px). NM is non-square (2560x2048).
LOW_W = _g.PATCHES_X * 64          # skopje 768 ; nm 2560
LOW_H = _g.PATCHES_Y * 64          # skopje 768 ; nm 2048
DOWNSCALE = 3
MASTER_W = LOW_W * DOWNSCALE        # skopje 2304 ; nm 7680
MASTER_H = LOW_H * DOWNSCALE        # skopje 2304 ; nm 6144

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "condor-landscape/1.0"

# ---------------------------------------------------------------------------
# Colour / style definitions (RGB), measured from Slovenia2.bmp
# ---------------------------------------------------------------------------
# Cool, desaturated green -> khaki -> cream hypsometric ramp. Macedonia/Skopje
# DEM range ~194..2489 m, so the stops span that band. Interpolated linearly.
ELEV_STOPS: list[tuple[float, tuple[int, int, int]]] = [
    (150.0,  (120, 150, 138)),   # valley floor: cool grey-green
    (300.0,  (135, 162, 145)),
    (500.0,  (158, 178, 152)),
    (750.0,  (185, 198, 165)),
    (1050.0, (210, 219, 178)),
    (1400.0, (236, 242, 192)),
    (1800.0, (250, 252, 208)),   # high: pale cream
    (2500.0, (252, 250, 220)),
]

# Overlay colours (Slovenia2-measured)
WATER_COLOR = np.array([21, 85, 149], dtype=np.uint8)     # opaque lake/reservoir blue
RIVER_COLOR = np.array([40, 110, 170], dtype=np.uint8)    # river/stream blue
URBAN_COLOR = np.array([243, 227, 16], dtype=np.uint8)    # settlement yellow
ROAD_MAJOR_COLOR = np.array([222, 88, 44], dtype=np.uint8)      # motorway/trunk/primary orange
ROAD_SECONDARY_COLOR = np.array([232, 150, 60], dtype=np.uint8) # secondary orange
ROAD_MINOR_COLOR = np.array([90, 70, 60], dtype=np.uint8)       # minor dark brown
RAILWAY_COLOR = np.array([31, 33, 32], dtype=np.uint8)         # near-black, thin
GRID_COLOR = np.array([90, 90, 90], dtype=np.uint8)
AIRPORT_COLOR = np.array([220, 40, 40], dtype=np.uint8)

# Hillshade parameters (measured from Slovenia2): soft so colours stay light.
HS_AZIMUTH = 315.0
HS_ALTITUDE = 45.0
HS_Z_FACTOR = 2.0
HS_SHADE_BASE = 0.55         # shade = HS_SHADE_BASE + HS_SHADE_GAIN * hs
HS_SHADE_GAIN = 0.60         # -> multiplier range 0.55 .. 1.15

BOUNDS_UTM = (ULXMAP, BR_NORTHING, BR_EASTING, ULYMAP)


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------
def _periodic_printer(label: str):
    """Return a callable that stops a background progress-reporting thread."""
    start = time.time()
    last_report = start
    done = False

    def _loop():
        nonlocal last_report
        while not done:
            time.sleep(10)
            now = time.time()
            if now - last_report >= 30:
                print(f"[{label}] still working... {now - start:.0f}s elapsed")
                last_report = now

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()

    def _stop():
        nonlocal done
        done = True
        thread.join(timeout=1.0)

    return _stop


# ---------------------------------------------------------------------------
# OSM fetching
# ---------------------------------------------------------------------------
def fetch_overpass(query: str, label: str) -> dict:
    """Query Overpass and return the JSON response."""
    print(f"[OSM] Fetching {label} from Overpass...")
    stop = _periodic_printer(f"Overpass {label}")
    try:
        url = OVERPASS_URL + "?data=" + urllib.parse.quote(query)
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=900,
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"[OSM] {label}: received {len(data.get('elements', []))} elements")
        return data
    finally:
        stop()


def _save_geojson(fc: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    print(f"[OSM] Saved {path} ({len(fc.get('features', []))} features)")


def _load_geojson(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _line_feature_collection(osm_data: dict) -> dict:
    """Convert Overpass `out geom` way elements to LineString FC."""
    features = []
    for el in osm_data.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry")
        if not geom:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in geom]
        if len(coords) < 2:
            continue
        try:
            ls = LineString(coords)
            if ls.is_empty:
                continue
            features.append({
                "type": "Feature",
                "properties": el.get("tags", {}),
                "geometry": {"type": "LineString", "coordinates": coords},
            })
        except Exception:
            continue
    return {"type": "FeatureCollection", "features": features}


def _point_feature_collection(osm_data: dict, tag_key: str = "place") -> dict:
    """Convert Overpass node elements to Point FC."""
    features = []
    for el in osm_data.get("elements", []):
        if el.get("type") != "node":
            continue
        tags = el.get("tags", {})
        if tag_key not in tags:
            continue
        try:
            features.append({
                "type": "Feature",
                "properties": tags,
                "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
            })
        except Exception:
            continue
    return {"type": "FeatureCollection", "features": features}


def _wgs84_bbox() -> tuple[float, float, float, float]:
    """Landscape bbox as (south, west, north, east) in WGS84."""
    transformer = pyproj.Transformer.from_crs(UTM_CRS, WGS84_CRS, always_xy=True)
    sw_lon, sw_lat = transformer.transform(ULXMAP, BR_NORTHING)
    ne_lon, ne_lat = transformer.transform(BR_EASTING, ULYMAP)
    return min(sw_lat, ne_lat), min(sw_lon, ne_lon), max(sw_lat, ne_lat), max(sw_lon, ne_lon)


def _roads_query(south: float, west: float, north: float, east: float) -> str:
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|pedestrian|track|path|cycleway|service|road)$"];
);
out geom;"""


def _railways_query(south: float, west: float, north: float, east: float) -> str:
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["railway"~"^(rail|light_rail|narrow_gauge|tram|subway)$"];
);
out geom;"""


def _waterways_query(south: float, west: float, north: float, east: float) -> str:
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["waterway"~"^(river|stream|canal)$"];
);
out geom;"""


def _settlements_query(south: float, west: float, north: float, east: float) -> str:
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["landuse"~"^(residential|commercial|industrial|retail)$"];
  relation["landuse"~"^(residential|commercial|industrial|retail)$"];
);
out geom;"""


def _places_query(south: float, west: float, north: float, east: float) -> str:
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  node["place"~"^(city|town|village|hamlet)$"];
);
out geom;"""


# ---------------------------------------------------------------------------
# Vector loading / projection
# ---------------------------------------------------------------------------
def project_features(fc: dict):
    """Project a GeoJSON FeatureCollection from WGS84 to UTM34."""
    transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)

    def _trans(x, y, z=None):
        return transformer.transform(x, y)

    geoms = []
    props = []
    for feat in fc.get("features", []):
        try:
            geom = shapely_transform(_trans, shape(feat.get("geometry")))
            if geom and not geom.is_empty and geom.is_valid:
                geoms.append(geom)
                props.append(feat.get("properties", {}))
        except Exception:
            continue
    return geoms, props


def _load_or_fetch_lines(path: Path, query_fn, bbox, label: str):
    """Load a cached LineString GeoJSON or fetch+cache it from Overpass."""
    south, west, north, east = bbox
    if path.exists():
        print(f"[OSM] Using cached {label}: {path}")
        return _load_geojson(path)
    try:
        data = fetch_overpass(query_fn(south, west, north, east), label)
        fc = _line_feature_collection(data)
        _save_geojson(fc, path)
        return fc
    except Exception as exc:
        print(f"[OSM] WARNING: could not fetch {label}: {exc}")
        return {"type": "FeatureCollection", "features": []}


def load_osm_layers() -> dict:
    """Load or fetch all OSM vector layers needed by the map."""
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    bbox = _wgs84_bbox()
    south, west, north, east = bbox
    print(f"[OSM] Landscape WGS84 bbox: {south:.6f},{west:.6f} -> {north:.6f},{east:.6f}")

    layers: dict[str, tuple[list, list]] = {}

    # Water polygons -- usually already cached by download_osm_features.py
    water_path = OSM_DIR / "water.geojson"
    if water_path.exists():
        print(f"[OSM] Using cached water polygons: {water_path}")
        layers["water"] = project_features(_load_geojson(water_path))
    else:
        print("[OSM] Water cache not found; download it with download_osm_features.py")
        layers["water"] = ([], [])

    # Road lines
    layers["roads"] = project_features(
        _load_or_fetch_lines(OSM_DIR / "roads_lines.geojson", _roads_query, bbox, "roads")
    )

    # Railway centre-lines (thin near-black). NB: the cached railways.geojson is
    # POLYGON corridors built by the polygon importer and is unsuitable for thin
    # lines, so we keep railway *lines* in a separate file.
    layers["railways"] = project_features(
        _load_or_fetch_lines(OSM_DIR / "railways_lines.geojson", _railways_query, bbox, "railways")
    )

    # Waterway lines
    layers["waterways"] = project_features(
        _load_or_fetch_lines(OSM_DIR / "waterways.geojson", _waterways_query, bbox, "waterways")
    )

    # Settlement / built-up polygons
    settlements_path = OSM_DIR / "settlements.geojson"
    if settlements_path.exists():
        print(f"[OSM] Using cached settlement polygons: {settlements_path}")
        fc = _load_geojson(settlements_path)
    else:
        try:
            data = fetch_overpass(_settlements_query(south, west, north, east), "settlements")
            from osm_io import osm_json_to_geojson
            fc = osm_json_to_geojson(data)
            _save_geojson(fc, settlements_path)
        except Exception as exc:
            print(f"[OSM] WARNING: could not fetch settlements: {exc}")
            fc = {"type": "FeatureCollection", "features": []}
    layers["settlements"] = project_features(fc)

    # Place nodes (towns/villages) -- markers + labels
    places_path = OSM_DIR / "places.geojson"
    if places_path.exists():
        fc = _load_geojson(places_path)
    else:
        try:
            data = fetch_overpass(_places_query(south, west, north, east), "places")
            fc = _point_feature_collection(data)
            _save_geojson(fc, places_path)
        except Exception as exc:
            print(f"[OSM] WARNING: could not fetch places: {exc}")
            fc = {"type": "FeatureCollection", "features": []}
    layers["places"] = project_features(fc)

    return layers


# ---------------------------------------------------------------------------
# DEM / relief
# ---------------------------------------------------------------------------
def load_dem(out_w: int, out_h: int) -> np.ndarray:
    """Read the canonical 30 m int16 DEM and resample to ``out_w``x``out_h``.

    The DEM's geographic extent equals BOUNDS_UTM exactly (NW pixel-centre =
    ULXMAP/ULYMAP, patches*192+1 px, 30 m), so a straight resize keeps relief
    registered with the OSM overlays. Negatives/NoData are clamped to 0 (per the
    DEM header). Both axes are resampled independently so non-square (NM) maps
    stay registered.
    """
    print(f"[DEM] Reading {DEM_PATH.name} ({DEM_W}x{DEM_H} int16) and resampling to {out_w}x{out_h}...")
    raw = np.fromfile(DEM_PATH, dtype="<i2")
    if raw.size != DEM_W * DEM_H:
        raise ValueError(
            f"DEM {DEM_PATH} has {raw.size} samples, expected {DEM_W*DEM_H} "
            f"({DEM_W}x{DEM_H})"
        )
    dem = raw.astype(np.float32).reshape(DEM_H, DEM_W)
    dem = np.where(dem < 0, 0.0, dem)

    if (out_w, out_h) != (DEM_W, DEM_H):
        # Bilinear resample via Pillow F-mode (float32) for a smooth relief.
        # PIL.resize takes (width, height).
        dem_img = Image.fromarray(dem, mode="F").resize((out_w, out_h), Image.Resampling.BILINEAR)
        dem = np.asarray(dem_img, dtype=np.float32)
    print(f"[DEM] Elevation range: {dem.min():.0f} - {dem.max():.0f} m")
    return dem


def colorize_elevation(dem: np.ndarray) -> np.ndarray:
    """Apply the cool hypsometric colour table to the DEM (RGB uint8)."""
    stops = ELEV_STOPS
    zs = np.array([s[0] for s in stops], dtype=np.float32)
    cols = np.array([s[1] for s in stops], dtype=np.float32)
    img = np.zeros((*dem.shape, 3), dtype=np.float32)
    z = dem.astype(np.float32)

    img[z <= zs[0]] = cols[0]
    for i in range(len(stops) - 1):
        mask = (z > zs[i]) & (z <= zs[i + 1])
        if not np.any(mask):
            continue
        t = (z[mask] - zs[i]) / (zs[i + 1] - zs[i])
        img[mask] = (1.0 - t)[:, None] * cols[i] + t[:, None] * cols[i + 1]
    img[z > zs[-1]] = cols[-1]
    return np.clip(img, 0, 255).astype(np.uint8)


def hillshade(dem: np.ndarray, cellsize: float) -> np.ndarray:
    """Soft analytical hillshade multiplier in 0.55 .. 1.15 (light, not dark)."""
    dy, dx = np.gradient(dem * HS_Z_FACTOR, cellsize, cellsize)
    slope = np.pi / 2.0 - np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    az = math.radians(HS_AZIMUTH)
    alt = math.radians(HS_ALTITUDE)
    hs = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    hs = np.clip(hs, 0.0, 1.0)
    return HS_SHADE_BASE + HS_SHADE_GAIN * hs


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def _utm_to_px(e: float, n: float, width: int, height: int) -> tuple[float, float]:
    min_e, min_n, max_e, max_n = BOUNDS_UTM
    sx = width / (max_e - min_e)
    sy = height / (max_n - min_n)
    return (e - min_e) * sx, (max_n - n) * sy


def _mask_to_layer(mask: np.ndarray, color: np.ndarray, alpha: float) -> Image.Image:
    """Turn a uint8 mask into an RGBA PIL layer."""
    rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
    rgba[mask > 0, :3] = color
    rgba[mask > 0, 3] = int(alpha * 255)
    return Image.fromarray(rgba, mode="RGBA")


def _blend(base: np.ndarray, layer: Image.Image) -> np.ndarray:
    """Alpha-composite an RGBA PIL layer over an RGB numpy image."""
    layer_np = np.array(layer.convert("RGBA"))
    alpha = layer_np[:, :, 3:4].astype(np.float32) / 255.0
    blended = base.astype(np.float32) * (1.0 - alpha) + layer_np[:, :, :3].astype(np.float32) * alpha
    return np.clip(blended, 0, 255).astype(np.uint8)


def rasterize_polygon_layer(geoms, bounds, width, height, color, alpha, desc):
    """Rasterize polygon geometries into a coloured RGBA layer."""
    if not geoms:
        return Image.new("RGBA", (width, height), (0, 0, 0, 0))
    print(f"[Render] Rasterizing {desc} ({len(geoms)} geometries)...")
    stop = _periodic_printer(f"rasterize {desc}")
    try:
        mask = rasterize_mask(geoms, bounds, width, height, foreground=255, background=0)
    finally:
        stop()
    return _mask_to_layer(mask, color, alpha)


def draw_line_layer(geoms, props, width, height, width_fn, color_fn, desc):
    """Draw line geometries onto an RGBA PIL layer."""
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not geoms:
        return layer
    draw = ImageDraw.Draw(layer)
    items = list(zip(geoms, props))
    for geom, prop in tqdm(items, desc=desc, mininterval=5):
        if geom.is_empty:
            continue
        sequences = []
        if geom.geom_type == "LineString":
            sequences.append(list(geom.coords))
        elif geom.geom_type == "MultiLineString":
            for part in geom.geoms:
                sequences.append(list(part.coords))
        else:
            continue

        color = color_fn(prop)
        w_px = width_fn(prop)
        for seq in sequences:
            pts = [_utm_to_px(x, y, width, height) for x, y, *_ in seq]
            if len(pts) < 2:
                continue
            draw.line(pts, fill=(int(color[0]), int(color[1]), int(color[2]), 255),
                      width=w_px, joint="curve")
    return layer


def draw_grid_layer(width: int, height: int) -> Image.Image:
    """Draw faint Condor tile boundaries and a fainter UTM 10 km grid."""
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    min_e, min_n, max_e, max_n = BOUNDS_UTM

    line_w = max(1, DOWNSCALE - 1)  # ~1 px at final 768 scale

    # Condor tile grid (3x3) -- faint
    tile_color = (int(GRID_COLOR[0]), int(GRID_COLOR[1]), int(GRID_COLOR[2]), 60)
    for col in range(TILES_X + 1):
        e = BR_EASTING - col * TILE_SIZE_M
        if min_e <= e <= max_e:
            x, _ = _utm_to_px(e, max_n, width, height)
            draw.line([(x, 0), (x, height)], fill=tile_color, width=line_w)
    for row in range(TILES_Y + 1):
        n = BR_NORTHING + row * TILE_SIZE_M
        if min_n <= n <= max_n:
            _, y = _utm_to_px(min_e, n, width, height)
            draw.line([(0, y), (width, y)], fill=tile_color, width=line_w)

    # UTM 10 km grid (very faint)
    utm_color = (int(GRID_COLOR[0]), int(GRID_COLOR[1]), int(GRID_COLOR[2]), 35)
    e = math.ceil(min_e / 10000.0) * 10000.0
    while e <= max_e:
        x, _ = _utm_to_px(e, max_n, width, height)
        draw.line([(x, 0), (x, height)], fill=utm_color, width=max(1, DOWNSCALE - 1))
        e += 10000.0
    n = math.ceil(min_n / 10000.0) * 10000.0
    while n <= max_n:
        _, y = _utm_to_px(min_e, n, width, height)
        draw.line([(0, y), (width, y)], fill=utm_color, width=max(1, DOWNSCALE - 1))
        n += 10000.0

    return layer


def draw_airports(width: int, height: int) -> Image.Image:
    """Draw airport location markers from data/airports.json."""
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not AIRPORTS_JSON.exists():
        return layer
    draw = ImageDraw.Draw(layer)
    transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)
    with open(AIRPORTS_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    r = max(5, 3 * DOWNSCALE)
    for ap in data.get("airports", []):
        try:
            e, n = transformer.transform(ap["lon"], ap["lat"])
            x, y = _utm_to_px(e, n, width, height)
            draw.ellipse(
                [(x - r, y - r), (x + r, y + r)],
                fill=(int(AIRPORT_COLOR[0]), int(AIRPORT_COLOR[1]), int(AIRPORT_COLOR[2]), 220),
                outline=(0, 0, 0, 255),
                width=max(1, DOWNSCALE),
            )
        except Exception:
            continue
    return layer


def draw_place_markers(geoms, props, width, height) -> Image.Image:
    """Draw city/town dots with name labels (dark text, light halo)."""
    from PIL import ImageFont
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not geoms:
        return layer
    draw = ImageDraw.Draw(layer)

    font_city = font_town = font_village = None
    try:
        bold = Path("C:/Windows/Fonts/arialbd.ttf")
        regular = Path("C:/Windows/Fonts/arial.ttf")
        if bold.exists():
            font_city = ImageFont.truetype(str(bold), max(14, width // 90))
            font_town = ImageFont.truetype(str(bold), max(11, width // 130))
        if regular.exists():
            font_village = ImageFont.truetype(str(regular), max(9, width // 170))
    except Exception:
        pass

    def _halo_text(tx, ty, name, font, text_color):
        # Light halo (8-neighbour) for readability over relief, then dark text.
        halo = (250, 250, 250, 200)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    draw.text((tx + dx, ty + dy), name, font=font, fill=halo)
        draw.text((tx, ty), name, font=font, fill=text_color)

    for geom, prop in zip(geoms, props):
        if geom.geom_type != "Point":
            continue
        x, y = _utm_to_px(geom.x, geom.y, width, height)
        place = prop.get("place", "")
        name = prop.get("name", prop.get("name:en", ""))

        if place == "city":
            r = max(4, 2 * DOWNSCALE)
            dot_color = (40, 40, 40, 230)
            font = font_city
            text_color = (25, 25, 25, 245)
        elif place == "town":
            r = max(3, DOWNSCALE + 1)
            dot_color = (60, 60, 60, 210)
            font = font_town
            text_color = (35, 35, 35, 230)
        elif place == "village":
            r = max(2, DOWNSCALE)
            dot_color = (80, 80, 80, 170)
            font = font_village
            text_color = (55, 55, 55, 200)
        else:
            continue  # skip hamlets/other to avoid clutter

        draw.ellipse(
            [(x - r, y - r), (x + r, y + r)],
            fill=dot_color,
            outline=(0, 0, 0, 210),
        )

        if name and font and place in ("city", "town", "village"):
            tx = x + r + 3
            ty = y - r - 2
            _halo_text(tx, ty, name, font, text_color)

    return layer


# ---------------------------------------------------------------------------
# Road / river / railway styling functions
# ---------------------------------------------------------------------------
def road_width(prop: dict, scale: float) -> int:
    cls = prop.get("highway", "")
    base = {
        "motorway": 4, "trunk": 4, "primary": 3,
        "secondary": 3, "tertiary": 2, "unclassified": 2,
        "residential": 2, "living_street": 2, "service": 2,
        "track": 2, "path": 1, "cycleway": 1,
        "pedestrian": 2, "road": 2,
    }.get(cls, 2)
    return max(2, int(round(base * scale)))


def road_color(prop: dict) -> np.ndarray:
    cls = prop.get("highway", "")
    if cls in ("motorway", "motorway_link", "trunk", "trunk_link",
               "primary", "primary_link"):
        return ROAD_MAJOR_COLOR
    if cls in ("secondary", "secondary_link"):
        return ROAD_SECONDARY_COLOR
    return ROAD_MINOR_COLOR


def river_width(prop: dict, scale: float) -> int:
    cls = prop.get("waterway", "")
    base = {"river": 3, "canal": 3, "stream": 2}.get(cls, 2)
    return max(2, int(round(base * scale)))


def railway_width(prop: dict, scale: float) -> int:
    # Thin near-black lines.
    return max(2, int(round(2 * scale)))


# ---------------------------------------------------------------------------
# Main map generation
# ---------------------------------------------------------------------------
def generate_master(width: int, height: int, layers: dict) -> Image.Image:
    """Render the full map at the master resolution (RGB)."""
    scale = width / MASTER_W   # 1.0 at master; keeps line-width scaling correct
    bounds = BOUNDS_UTM
    print(f"\n[Map] Generating {width}x{height} master (scale={scale:.3f})...")

    # Relief + soft hillshade
    dem = load_dem(width, height)
    relief = colorize_elevation(dem)
    cellsize = (bounds[2] - bounds[0]) / width
    shade = hillshade(dem, cellsize)
    base = (relief.astype(np.float32) * shade[..., None]).clip(0, 255).astype(np.uint8)

    # Composite order:
    #   relief+hillshade -> water -> rivers -> settlements -> minor roads ->
    #   secondary -> major roads -> railways -> grid -> labels -> airports
    print("[Render] Compositing layers...")

    # Water polygons (opaque)
    base = _blend(base, rasterize_polygon_layer(
        layers["water"][0], bounds, width, height, WATER_COLOR, 1.0, "water"))

    # Rivers / streams
    base = _blend(base, draw_line_layer(
        layers["waterways"][0], layers["waterways"][1], width, height,
        lambda p: river_width(p, scale), lambda p: RIVER_COLOR, "rivers/streams"))

    # Settlements (yellow, semi-opaque)
    base = _blend(base, rasterize_polygon_layer(
        layers["settlements"][0], bounds, width, height, URBAN_COLOR, 0.72, "settlements"))

    # Roads, drawn minor -> secondary -> major so majors stay on top.
    road_geoms, road_props = layers["roads"]
    minor_idx, sec_idx, major_idx = [], [], []
    for i, p in enumerate(road_props):
        cls = p.get("highway", "")
        if cls in ("motorway", "motorway_link", "trunk", "trunk_link",
                   "primary", "primary_link"):
            major_idx.append(i)
        elif cls in ("secondary", "secondary_link"):
            sec_idx.append(i)
        else:
            minor_idx.append(i)

    def _subset(idxs):
        return [road_geoms[i] for i in idxs], [road_props[i] for i in idxs]

    g, pr = _subset(minor_idx)
    base = _blend(base, draw_line_layer(g, pr, width, height,
                  lambda p: road_width(p, scale), road_color, "minor roads"))
    g, pr = _subset(sec_idx)
    base = _blend(base, draw_line_layer(g, pr, width, height,
                  lambda p: road_width(p, scale), road_color, "secondary roads"))
    g, pr = _subset(major_idx)
    base = _blend(base, draw_line_layer(g, pr, width, height,
                  lambda p: road_width(p, scale), road_color, "major roads"))

    # Railways (near-black, thin) -- above roads
    base = _blend(base, draw_line_layer(
        layers["railways"][0], layers["railways"][1], width, height,
        lambda p: railway_width(p, scale), lambda p: RAILWAY_COLOR, "railways"))

    # Faint grid
    base = _blend(base, draw_grid_layer(width, height))

    # Town labels
    base = _blend(base, draw_place_markers(layers["places"][0], layers["places"][1], width, height))

    # Airport markers (top)
    base = _blend(base, draw_airports(width, height))

    return Image.fromarray(base, mode="RGB")


def main():
    import shutil

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print(f"Generating Slovenia2-style topographic flight planner map: {LANDSCAPE_NAME}")
    print(f"Bounds UTM34: {BOUNDS_UTM}")
    print(f"Master {MASTER_W}x{MASTER_H} -> {LOW_W}x{LOW_H} (x{DOWNSCALE})")
    print("=" * 60)

    layers = load_osm_layers()

    # High-res master
    img_master = generate_master(MASTER_W, MASTER_H, layers)
    master_path = OUT_DIR / f"{LANDSCAPE_NAME}_{MASTER_W}x{MASTER_H}.bmp"
    img_master.save(master_path, format="BMP")
    print(f"[Out] Saved master map: {master_path} "
          f"({master_path.stat().st_size / (1024*1024):.1f} MB)")

    # Condor-sized downsample (must match .trn dimensions, 32-bit).
    img_low = img_master.resize((LOW_W, LOW_H), Image.Resampling.LANCZOS)
    low_path = OUT_DIR / f"{LANDSCAPE_NAME}.bmp"
    # RGB -> RGBA -> BMP: Pillow writes a 32-bit bottom-up Windows BMP with the
    # correct channel order. Do NOT hand-pack BGRA bytes.
    img_low.convert("RGBA").save(low_path, format="BMP")
    print(f"[Out] Saved Condor map: {low_path} "
          f"({low_path.stat().st_size:,} bytes)")

    # Install into the Condor landscape, backing up the existing file first.
    installed = Path(f"C:/Condor2/Landscapes/{LANDSCAPE_NAME}/{LANDSCAPE_NAME}.bmp")
    if installed.parent.exists():
        if installed.exists():
            backup = installed.with_suffix(".bmp.bak")
            if not backup.exists():
                shutil.copy2(installed, backup)
                print(f"[Install] Backed up existing -> {backup}")
            else:
                print(f"[Install] Backup already exists -> {backup} (left as-is)")
        shutil.copy2(low_path, installed)
        print(f"[Install] Installed -> {installed} ({installed.stat().st_size:,} bytes)")
    else:
        print(f"[Install] WARNING: {installed.parent} not found; skipped install")

    print("\nDone.")


if __name__ == "__main__":
    main()
