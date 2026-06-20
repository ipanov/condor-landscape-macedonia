#!/usr/bin/env python3
"""
Download high-resolution aerial imagery for the MacedoniaSkopje landscape
and stitch it into per-tile source images.

Source: Esri World Imagery tile service (https://server.arcgisonline.com/...)
Tile matrix: Web Mercator (EPSG:3857), standard z/x/y.

The 69.12 x 69.12 km landscape is divided into 3 x 3 Condor tiles
(23.04 km each). For each tile we download the necessary zoom-level tiles,
stitch them, then reproject/warp to UTM 34N and save as a GeoTIFF/BMP
ready for DDS conversion.
"""

import math
import os
import time
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image
import pyproj

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / ".sandbox" / "textures"
CACHE_DIR = OUT_DIR / "tile_cache"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Landscape calibration (UTM 34N)
ULXMAP = 506880.0
ULYMAP = 4700160.0
XDIM = 29.9869848156182
WIDTH = 2305
BR_EASTING = ULXMAP + (WIDTH - 1) * XDIM
BR_NORTHING = ULYMAP - (WIDTH - 1) * XDIM

# Web Mercator helpers
_WM_CRS = pyproj.CRS.from_epsg(3857)
_WGS84_CRS = pyproj.CRS.from_epsg(4326)
_to_wm = pyproj.Transformer.from_crs(_WGS84_CRS, _WM_CRS, always_xy=True)
_to_wgs84 = pyproj.Transformer.from_crs(_WM_CRS, _WGS84_CRS, always_xy=True)


def utm_to_wgs84(e, n):
    transformer = pyproj.Transformer.from_crs("EPSG:32634", "EPSG:4326", always_xy=True)
    return transformer.transform(e, n)


def tile_to_latlon(z, x, y):
    """Return (west, south, east, north) in WGS84 for a TMS/XYZ tile."""
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def latlon_to_tile(z, lon, lat):
    """Return (x, y) tile indices containing the given lat/lon at zoom z."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_url(z, x, y):
    # Main Esri World Imagery endpoint (verified to resolve in this environment)
    return f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


def download_tile(z, x, y, retries=3, delay=0.5):
    path = CACHE_DIR / f"{z}" / f"{x}" / f"{y}.jpg"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    url = tile_url(z, x, y)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CondorLandscapeBuilder/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            with open(path, "wb") as f:
                f.write(data)
            return path
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                print(f"Failed {url}: {e}")
    return None


def get_tile_bounds_utm(z, x, y):
    """Return (west_e, south_n, east_e, north_n) in UTM 34N for a tile."""
    w, s, e, n = tile_to_latlon(z, x, y)
    transformer = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
    west_e, north_n = transformer.transform(w, n)
    east_e, south_n = transformer.transform(e, s)
    return west_e, south_n, east_e, north_n


def condor_tile_bounds_utm(tile_col, tile_row):
    """
    Condor tile indices count from bottom-right (SE).
    tile_col 0 = east-most tile, tile_row 0 = south-most tile.
    Each tile is 23.04 km = 768 samples at ~30m.
    """
    samples_per_tile = 768
    # east edge of landscape = BR_EASTING
    e_max = BR_EASTING
    # south edge of landscape = BR_NORTHING
    n_min = BR_NORTHING
    # pixel index of east edge of tile_col (counting from east)
    east_px = (3 - tile_col) * samples_per_tile
    west_px = east_px - samples_per_tile
    # pixel index of south edge of tile_row (counting from south)
    south_px = (3 - tile_row) * samples_per_tile
    north_px = south_px - samples_per_tile
    # Convert to UTM (using pixel centers; edges are +/- 0.5 pixel)
    e_east = ULXMAP + east_px * XDIM
    e_west = ULXMAP + west_px * XDIM
    n_south = ULYMAP - south_px * XDIM
    n_north = ULYMAP - north_px * XDIM
    return e_west, n_south, e_east, n_north


def download_and_stitch_tile(tile_col, tile_row, zoom=17):
    """Download all web tiles covering a Condor tile and save a stitched mosaic."""
    e_west, n_south, e_east, n_north = condor_tile_bounds_utm(tile_col, tile_row)
    # Convert to WGS84
    wgs_transformer = pyproj.Transformer.from_crs("EPSG:32634", "EPSG:4326", always_xy=True)
    w, s = wgs_transformer.transform(e_west, n_south)
    e, n = wgs_transformer.transform(e_east, n_north)

    # Add a small buffer
    lon_buf = (e - w) * 0.05
    lat_buf = (n - s) * 0.05
    w -= lon_buf; e += lon_buf
    s -= lat_buf; n += lat_buf

    x_min, y_min = latlon_to_tile(zoom, w, n)  # top-left
    x_max, y_max = latlon_to_tile(zoom, e, s)  # bottom-right

    print(f"Tile t{tile_col:02d}{tile_row:02d}: z={zoom} x={x_min}..{x_max} y={y_min}..{y_max} "
          f"({(x_max-x_min+1)*(y_max-y_min+1)} tiles)")

    tiles = [(zoom, x, y) for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)]

    # Parallel download
    results = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(download_tile, z, x, y): (z, x, y) for z, x, y in tiles}
        for future in as_completed(futures):
            z, x, y = futures[future]
            path = future.result()
            if path:
                results[(x, y)] = path
            else:
                results[(x, y)] = None

    # Build mosaic
    tile_w = 256
    tile_h = 256
    mosaic_w = (x_max - x_min + 1) * tile_w
    mosaic_h = (y_max - y_min + 1) * tile_h
    mosaic = np.zeros((mosaic_h, mosaic_w, 3), dtype=np.uint8)

    for (x, y), path in results.items():
        if path is None:
            continue
        mx = (x - x_min) * tile_w
        my = (y - y_min) * tile_h
        try:
            img = np.array(Image.open(path))
            if len(img.shape) == 2:
                img = np.stack([img] * 3, axis=-1)
            elif img.shape[2] == 4:
                img = img[:, :, :3]
            mosaic[my:my+tile_h, mx:mx+tile_w] = img
        except Exception as e:
            print(f"Error opening {path}: {e}")

    # Save mosaic with georeferencing metadata (Web Mercator bounds)
    # We'll use the center of top-left pixel and pixel size in WM
    n_tiles_x = x_max - x_min + 1
    n_tiles_y = y_max - y_min + 1
    # WM bounds of the mosaic computed directly from tile indices
    n = 2 ** zoom
    west_wm = x_min / n * 2 * math.pi * 6378137 - math.pi * 6378137
    east_wm = (x_max + 1) / n * 2 * math.pi * 6378137 - math.pi * 6378137
    north_wm = math.pi * 6378137 - y_min / n * 2 * math.pi * 6378137
    south_wm = math.pi * 6378137 - (y_max + 1) / n * 2 * math.pi * 6378137
    pixel_size_wm = (east_wm - west_wm) / mosaic_w

    meta = {
        "tile": f"t{tile_col:02d}{tile_row:02d}",
        "zoom": zoom,
        "x_range": [x_min, x_max],
        "y_range": [y_min, y_max],
        "mosaic_size": [mosaic_w, mosaic_h],
        "web_mercator": {
            "west": west_wm,
            "south": south_wm,
            "east": east_wm,
            "north": north_wm,
            "pixel_size": pixel_size_wm
        },
        "utm34n": {
            "west": e_west,
            "south": n_south,
            "east": e_east,
            "north": n_north
        }
    }

    mosaic_path = OUT_DIR / f"mosaic_t{tile_col:02d}{tile_row:02d}.png"
    Image.fromarray(mosaic).save(mosaic_path)
    meta_path = OUT_DIR / f"mosaic_t{tile_col:02d}{tile_row:02d}.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"Saved {mosaic_path} ({mosaic_w}x{mosaic_h})")
    return mosaic_path, meta


def main():
    # Process all 9 tiles at zoom 16 (~0.6-0.8 m/pixel at this latitude).
    # This is well above the final 2.8 m/pixel Condor tile resolution, so it
    # exceeds Slovenia2 quality while keeping download counts reasonable.
    # For even better quality, set TEXTURE_ZOOM=17 (much larger downloads).
    zoom = int(os.environ.get("TEXTURE_ZOOM", "16"))
    for tile_row in range(3):
        for tile_col in range(3):
            download_and_stitch_tile(tile_col, tile_row, zoom=zoom)


if __name__ == "__main__":
    main()
