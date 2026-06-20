#!/usr/bin/env python3
"""
Generate a Slovenia2-style topographic flight-planner map for the
MacedoniaSkopje Condor 2 landscape.

The script:
  * resamples the 30 m DEM to the target map resolution
  * computes a hypsometric tint + hillshaded relief
  * overlays OpenStreetMap vector layers:
      - water polygons (lakes/reservoirs)
      - rivers and streams
      - built-up / settlement areas
      - major and minor roads
  * adds a subtle UTM / tile grid and airport markers
  * saves 24-bit BMP output at 4096x4096 (high-res) and a 32-bit BGRA Windows
    BMP at 768x768 (Condor, matching the .trn dimensions)

Vector data is fetched from the Overpass API and cached in .sandbox/osm/.
Cached files are reused when present so the script can run offline after the
first download.
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
import rasterio
import requests
from PIL import Image, ImageDraw
from rasterio.crs import CRS
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject
from shapely.geometry import LineString, MultiLineString, Point, shape
from shapely.ops import transform as shapely_transform
from tqdm import tqdm

# Condor landscape calibration (UTM 34N)
from condor_grid import (
    BR_EASTING,
    BR_NORTHING,
    TILE_SIZE_M,
    TILES_X,
    TILES_Y,
    UTM_CRS,
    WGS84_CRS,
    WIDTH,
    HEIGHT,
    XDIM,
    ULXMAP,
    ULYMAP,
)
from rasterize import rasterize_mask

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEM_PATH = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_utm30m.bil"
OSM_DIR = PROJECT_ROOT / ".sandbox" / "osm"
OUT_DIR = PROJECT_ROOT / "output" / "maps"
AIRPORTS_JSON = PROJECT_ROOT / "data" / "airports.json"

# Output sizes
HIGH_SIZE = 4096
LOW_SIZE = 768

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "condor-landscape/1.0"

# ---------------------------------------------------------------------------
# Colour / style definitions (RGB)
# ---------------------------------------------------------------------------
ELEV_STOPS: list[tuple[float, tuple[int, int, int]]] = [
    (0.0, (110, 150, 90)),      # low plains: muted green
    (250.0, (165, 195, 125)),   # farmland/foothills
    (550.0, (215, 215, 165)),   # rolling hills
    (950.0, (200, 165, 115)),   # lower mountains
    (1400.0, (165, 125, 85)),   # mountains
    (2000.0, (190, 185, 180)),  # high peaks grey
    (2700.0, (250, 250, 250)),  # snow/rock white
]

WATER_COLOR = np.array([120, 170, 220], dtype=np.uint8)
RIVER_COLOR = np.array([80, 130, 200], dtype=np.uint8)
URBAN_COLOR = np.array([230, 220, 80], dtype=np.uint8)    # yellow (matching Slovenia2)
ROAD_MAJOR_COLOR = np.array([220, 50, 30], dtype=np.uint8)    # red (motorway/trunk/primary)
ROAD_SECONDARY_COLOR = np.array([230, 140, 40], dtype=np.uint8) # orange (secondary)
ROAD_MINOR_COLOR = np.array([180, 140, 100], dtype=np.uint8)  # light brown (tertiary and below)
RAILWAY_COLOR = np.array([30, 30, 30], dtype=np.uint8)        # black (matching Slovenia2 railways)
GRID_COLOR = np.array([190, 190, 190], dtype=np.uint8)
AIRPORT_COLOR = np.array([255, 0, 0], dtype=np.uint8)

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
    """Convert Overpass `out geom` road/waterway elements to LineString FC."""
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


def load_osm_layers() -> dict:
    """Load or fetch all OSM vector layers needed by the map."""
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    south, west, north, east = _wgs84_bbox()
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
    roads_path = OSM_DIR / "roads_lines.geojson"
    if roads_path.exists():
        print(f"[OSM] Using cached road lines: {roads_path}")
        fc = _load_geojson(roads_path)
    else:
        try:
            data = fetch_overpass(_roads_query(south, west, north, east), "roads")
            fc = _line_feature_collection(data)
            _save_geojson(fc, roads_path)
        except Exception as exc:
            print(f"[OSM] WARNING: could not fetch roads: {exc}")
            fc = {"type": "FeatureCollection", "features": []}
    layers["roads"] = project_features(fc)

    # Waterway lines
    waterways_path = OSM_DIR / "waterways.geojson"
    if waterways_path.exists():
        print(f"[OSM] Using cached waterways: {waterways_path}")
        fc = _load_geojson(waterways_path)
    else:
        try:
            data = fetch_overpass(_waterways_query(south, west, north, east), "waterways")
            fc = _line_feature_collection(data)
            _save_geojson(fc, waterways_path)
        except Exception as exc:
            print(f"[OSM] WARNING: could not fetch waterways: {exc}")
            fc = {"type": "FeatureCollection", "features": []}
    layers["waterways"] = project_features(fc)

    # Settlement / built-up polygons
    settlements_path = OSM_DIR / "settlements.geojson"
    if settlements_path.exists():
        print(f"[OSM] Using cached settlement polygons: {settlements_path}")
        fc = _load_geojson(settlements_path)
    else:
        try:
            data = fetch_overpass(_settlements_query(south, west, north, east), "settlements")
            # Re-use the project's polygon builder for multipolygon relations
            from osm_io import osm_json_to_geojson
            fc = osm_json_to_geojson(data)
            _save_geojson(fc, settlements_path)
        except Exception as exc:
            print(f"[OSM] WARNING: could not fetch settlements: {exc}")
            fc = {"type": "FeatureCollection", "features": []}
    layers["settlements"] = project_features(fc)

    # Place nodes (towns/villages) -- optional markers
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
def load_dem(width: int, height: int) -> np.ndarray:
    """Resample the 30 m DEM to the requested map size."""
    print(f"[DEM] Resampling DEM to {width}x{height}...")
    min_e, min_n, max_e, max_n = BOUNDS_UTM
    dst_transform = (
        Affine.translation(min_e, max_n)
        * Affine.scale((max_e - min_e) / width, -(max_n - min_n) / height)
    )

    with rasterio.open(DEM_PATH) as src:
        dst = np.empty((height, width), dtype=np.float32)
        reproject(
            rasterio.band(src, 1),
            dst,
            src_transform=src.transform,
            src_crs=UTM_CRS,
            dst_transform=dst_transform,
            dst_crs=UTM_CRS,
            resampling=Resampling.bilinear,
            src_nodata=src.nodata,
            dst_nodata=-32768.0,
        )
    dem = np.where(dst <= -32000, np.nan, dst)
    # Fill small no-data holes with the minimum valid elevation
    fill_val = float(np.nanmin(dem)) if np.any(np.isfinite(dem)) else 0.0
    dem = np.nan_to_num(dem, nan=fill_val)
    print(f"[DEM] Elevation range: {dem.min():.0f} - {dem.max():.0f} m")
    return dem


def colorize_elevation(dem: np.ndarray) -> np.ndarray:
    """Apply a hypsometric colour table to the DEM."""
    stops = ELEV_STOPS
    zs = np.array([s[0] for s in stops], dtype=np.float32)
    cols = np.array([s[1] for s in stops], dtype=np.float32)
    img = np.zeros((*dem.shape, 3), dtype=np.float32)
    z = dem.astype(np.float32)

    # below first stop
    img[z < zs[0]] = cols[0]
    # between stops
    for i in range(len(stops) - 1):
        mask = (z >= zs[i]) & (z < zs[i + 1])
        if not np.any(mask):
            continue
        t = (z[mask] - zs[i]) / (zs[i + 1] - zs[i])
        img[mask] = (1.0 - t)[:, None] * cols[i] + t[:, None] * cols[i + 1]
    # above last stop
    img[z >= zs[-1]] = cols[-1]
    return np.clip(img, 0, 255).astype(np.uint8)


def hillshade(dem: np.ndarray, cellsize: float, azimuth: float = 315, altitude: float = 45) -> np.ndarray:
    """Simple analytical hillshade multiplier (0..1)."""
    dx, dy = np.gradient(dem, cellsize, cellsize)
    slope = np.pi / 2.0 - np.arctan(np.sqrt(dx * dx + dy * dy))
    aspect = np.arctan2(-dx, dy)
    az = math.radians(azimuth)
    alt = math.radians(altitude)
    shaded = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    shaded = (shaded * 0.5 + 0.5)
    return np.clip(shaded, 0.35, 1.35)


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
        # collect coordinate sequences
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
            draw.line(pts, fill=(*color, 255), width=w_px)
    return layer


def draw_grid_layer(width: int, height: int) -> Image.Image:
    """Draw Condor tile boundaries and a faint UTM 10 km grid."""
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    min_e, min_n, max_e, max_n = BOUNDS_UTM

    # Condor tile grid (3x3)
    tile_color = (*GRID_COLOR, 140)
    for col in range(TILES_X + 1):
        e = BR_EASTING - col * TILE_SIZE_M
        if min_e <= e <= max_e:
            x, _ = _utm_to_px(e, max_n, width, height)
            draw.line([(x, 0), (x, height)], fill=tile_color, width=2)
    for row in range(TILES_Y + 1):
        n = BR_NORTHING + row * TILE_SIZE_M
        if min_n <= n <= max_n:
            _, y = _utm_to_px(min_e, n, width, height)
            draw.line([(0, y), (width, y)], fill=tile_color, width=2)

    # UTM 10 km grid (very faint)
    utm_color = (*GRID_COLOR, 50)
    e = math.ceil(min_e / 10000.0) * 10000.0
    while e <= max_e:
        x, _ = _utm_to_px(e, max_n, width, height)
        draw.line([(x, 0), (x, height)], fill=utm_color, width=1)
        e += 10000.0
    n = math.ceil(min_n / 10000.0) * 10000.0
    while n <= max_n:
        _, y = _utm_to_px(min_e, n, width, height)
        draw.line([(0, y), (width, y)], fill=utm_color, width=1)
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

    for ap in data.get("airports", []):
        try:
            e, n = transformer.transform(ap["lon"], ap["lat"])
            x, y = _utm_to_px(e, n, width, height)
            r = 7
            draw.ellipse(
                [(x - r, y - r), (x + r, y + r)],
                fill=(*AIRPORT_COLOR, 200),
                outline=(0, 0, 0, 255),
            )
        except Exception:
            continue
    return layer


def draw_place_markers(geoms, props, width, height) -> Image.Image:
    """Draw city/town dots with name labels (matching Slovenia2 style)."""
    from PIL import ImageFont
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not geoms:
        return layer
    draw = ImageDraw.Draw(layer)

    # Load fonts for labels
    font_city = None
    font_town = None
    font_village = None
    try:
        from pathlib import Path
        bold = Path("C:/Windows/Fonts/arialbd.ttf")
        regular = Path("C:/Windows/Fonts/arial.ttf")
        if bold.exists():
            font_city = ImageFont.truetype(str(bold), max(14, width // 250))
            font_town = ImageFont.truetype(str(bold), max(11, width // 340))
        if regular.exists():
            font_village = ImageFont.truetype(str(regular), max(9, width // 420))
    except Exception:
        pass

    for geom, prop in zip(geoms, props):
        if geom.geom_type != "Point":
            continue
        x, y = _utm_to_px(geom.x, geom.y, width, height)
        place = prop.get("place", "")
        name = prop.get("name", prop.get("name:en", ""))

        if place == "city":
            r = 5
            dot_color = (40, 40, 40, 220)
            font = font_city
            text_color = (30, 30, 30, 240)
        elif place == "town":
            r = 4
            dot_color = (60, 60, 60, 200)
            font = font_town
            text_color = (40, 40, 40, 220)
        elif place == "village":
            r = 2
            dot_color = (80, 80, 80, 160)
            font = font_village
            text_color = (60, 60, 60, 180)
        else:
            r = 2
            dot_color = (100, 80, 60, 140)
            font = font_village
            text_color = (60, 60, 60, 150)

        # Draw dot
        draw.ellipse(
            [(x - r, y - r), (x + r, y + r)],
            fill=dot_color,
            outline=(0, 0, 0, 200),
        )

        # Draw label with shadow
        if name and font and place in ("city", "town", "village"):
            tx = x + r + 4
            ty = y - r - 2
            # Shadow
            draw.text((tx + 1, ty + 1), name, font=font, fill=(255, 255, 255, 140))
            # Text
            draw.text((tx, ty), name, font=font, fill=text_color)

    return layer


# ---------------------------------------------------------------------------
# Road / river styling functions
# ---------------------------------------------------------------------------
def road_width(prop: dict, scale: float) -> int:
    cls = prop.get("highway", "")
    base = {
        "motorway": 4,
        "trunk": 4,
        "primary": 3,
        "secondary": 3,
        "tertiary": 2,
        "unclassified": 2,
        "residential": 2,
        "living_street": 2,
        "service": 2,
        "track": 2,
        "path": 1,
        "cycleway": 1,
        "pedestrian": 2,
        "road": 2,
    }.get(cls, 2)
    return max(1, int(round(base * scale)))


def road_color(prop: dict) -> np.ndarray:
    cls = prop.get("highway", "")
    if cls in ("motorway", "motorway_link", "trunk", "trunk_link"):
        return ROAD_MAJOR_COLOR  # red for major roads
    if cls in ("primary", "primary_link"):
        return ROAD_MAJOR_COLOR
    if cls in ("secondary", "secondary_link"):
        return ROAD_SECONDARY_COLOR
    return ROAD_MINOR_COLOR


def river_width(prop: dict, scale: float) -> int:
    cls = prop.get("waterway", "")
    base = {"river": 3, "canal": 3, "stream": 2}.get(cls, 2)
    return max(1, int(round(base * scale)))


# ---------------------------------------------------------------------------
# Main map generation
# ---------------------------------------------------------------------------
def generate_map(size: int, layers: dict) -> Image.Image:
    width = height = size
    scale = size / HIGH_SIZE
    bounds = BOUNDS_UTM
    print(f"\n[Map] Generating {width}x{height} map (scale={scale:.3f})...")

    # Relief
    dem = load_dem(width, height)
    relief = colorize_elevation(dem)
    cellsize = (bounds[2] - bounds[0]) / width
    shade = hillshade(dem, cellsize)
    base = (relief.astype(np.float32) * shade[..., None]).clip(0, 255).astype(np.uint8)

    # Layers (composite order matters)
    print("[Render] Compositing layers...")
    base = _blend(base, rasterize_polygon_layer(layers["water"][0], bounds, width, height,
                                                WATER_COLOR, 0.92, "water"))
    base = _blend(base, rasterize_polygon_layer(layers["settlements"][0], bounds, width, height,
                                                URBAN_COLOR, 0.55, "settlements"))
    base = _blend(base, draw_line_layer(layers["waterways"][0], layers["waterways"][1],
                                        width, height,
                                        lambda p: river_width(p, scale),
                                        lambda p: RIVER_COLOR, "rivers/streams"))
    base = _blend(base, draw_line_layer(layers["roads"][0], layers["roads"][1],
                                        width, height,
                                        lambda p: road_width(p, scale),
                                        road_color, "roads"))
    base = _blend(base, draw_grid_layer(width, height))
    base = _blend(base, draw_place_markers(layers["places"][0], layers["places"][1], width, height))
    base = _blend(base, draw_airports(width, height))

    return Image.fromarray(base, mode="RGB")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print("Generating Slovenia2-style topographic flight planner map")
    print(f"Bounds UTM34: {BOUNDS_UTM}")
    print("=" * 60)

    layers = load_osm_layers()

    # High-res master
    img_high = generate_map(HIGH_SIZE, layers)
    high_path = OUT_DIR / "MacedoniaSkopje_4096.bmp"
    img_high.save(high_path, format="BMP")
    print(f"[Out] Saved high-res map: {high_path} ({high_path.stat().st_size / (1024*1024):.1f} MB)")

    # Condor-sized downsample (must match .trn dimensions)
    img_low = img_high.resize((LOW_SIZE, LOW_SIZE), Image.Resampling.LANCZOS)
    low_path = OUT_DIR / "MacedoniaSkopje.bmp"
    img_low.convert("RGBA").save(low_path, format="BMP")
    print(f"[Out] Saved Condor map: {low_path} ({low_path.stat().st_size / (1024*1024):.1f} MB)")

    print("\nDone.")


if __name__ == "__main__":
    main()
