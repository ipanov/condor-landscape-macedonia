#!/usr/bin/env python3
"""Parallel downloader for Macedonian 2023 color orthophoto (APP_DATA:ORTOFOTO_2023).

Uses the GWC WMS endpoint with aligned EPSG:6316 MSCS6316 grid tiles.
Zoom 10 = 0.56 m/px, tile size 256x256, covers ~143.36 m per tile.
"""
import os
import sys
import time
import json
import math
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".sandbox" / "textures_mk2023"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# EPSG:6316 MSCS6316 grid definition
ORIGIN_X = 7397000.634424793
ORIGIN_Y = 4521901.793180252
RESOLUTIONS = [560, 280, 140, 70, 35, 17.92, 8.96, 4.48, 2.24, 1.12, 0.56, 0.28, 0.14]
ZOOM = 10
RES = RESOLUTIONS[ZOOM]
TILE_SIZE_PX = 256
TILE_SIZE_M = TILE_SIZE_PX * RES

# Landscape bounds in UTM34 (from DEM header)
UTM_E_MIN = 506880.0
UTM_E_MAX = 575970.011
UTM_N_MIN = 4631069.989
UTM_N_MAX = 4700160.0

# Convert UTM34 bounds to EPSG:6316
try:
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:32634", "EPSG:6316", always_xy=True)
    corners = [(UTM_E_MIN, UTM_N_MIN), (UTM_E_MIN, UTM_N_MAX),
               (UTM_E_MAX, UTM_N_MIN), (UTM_E_MAX, UTM_N_MAX)]
    xs, ys = [], []
    for e, n in corners:
        x, y = t.transform(e, n)
        xs.append(x)
        ys.append(y)
    X_MIN = min(xs)
    X_MAX = max(xs)
    Y_MIN = min(ys)
    Y_MAX = max(ys)
except Exception as e:
    print(f"pyproj error: {e}")
    sys.exit(1)

TILE_X_MIN = int(math.floor((X_MIN - ORIGIN_X) / TILE_SIZE_M))
TILE_X_MAX = int(math.floor((X_MAX - ORIGIN_X) / TILE_SIZE_M)) + 1
TILE_Y_MIN = int(math.floor((Y_MIN - ORIGIN_Y) / TILE_SIZE_M))
TILE_Y_MAX = int(math.floor((Y_MAX - ORIGIN_Y) / TILE_SIZE_M)) + 1

BASE_URL = "https://e-uslugi.katastar.gov.mk/geo/proxy/gwc/wms"
HEADERS = {"Referer": "https://e-uslugi.katastar.gov.mk/"}
MAX_WORKERS = 64


def tile_url(tx, ty):
    minx = ORIGIN_X + tx * TILE_SIZE_M
    miny = ORIGIN_Y + ty * TILE_SIZE_M
    maxx = minx + TILE_SIZE_M
    maxy = miny + TILE_SIZE_M
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": "APP_DATA:ORTOFOTO_2023",
        "STYLES": "",
        "FORMAT": "image/jpeg",
        "TILED": "true",
        "RESIZE": "resize",
        "GRIDSET": "MSCS6316",
        "SRS": "EPSG:6316",
        "BBOX": f"{minx},{miny},{maxx},{maxy}",
        "WIDTH": TILE_SIZE_PX,
        "HEIGHT": TILE_SIZE_PX,
    }
    return f"{BASE_URL}?{urlencode(params)}"


def download_tile(args):
    tx, ty = args
    out_path = OUT_DIR / f"z{ZOOM}_x{tx}_y{ty}.jpg"
    if out_path.exists() and out_path.stat().st_size > 0:
        return (tx, ty, "cached", out_path.stat().st_size)
    url = tile_url(tx, ty)
    for attempt in range(5):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image"):
                out_path.write_bytes(r.content)
                return (tx, ty, "ok", len(r.content))
            else:
                # Save error for debugging on first failure
                if attempt == 0 and r.status_code != 200:
                    err_path = OUT_DIR / f"z{ZOOM}_x{tx}_y{ty}.err"
                    err_path.write_text(f"{r.status_code}\n{r.text[:500]}")
                time.sleep(0.5 * (attempt + 1))
        except Exception as e:
            time.sleep(0.5 * (attempt + 1))
    return (tx, ty, "failed", 0)


def main():
    total = (TILE_X_MAX - TILE_X_MIN + 1) * (TILE_Y_MAX - TILE_Y_MIN + 1)
    print(f"Zoom {ZOOM} ({RES} m/px)")
    print(f"Tile range X: {TILE_X_MIN}..{TILE_X_MAX}, Y: {TILE_Y_MIN}..{TILE_Y_MAX}")
    print(f"Total tiles: {total}")
    print(f"Output: {OUT_DIR}")

    tiles = [(x, y) for y in range(TILE_Y_MIN, TILE_Y_MAX + 1)
             for x in range(TILE_X_MIN, TILE_X_MAX + 1)]

    # Save metadata
    meta = {
        "zoom": ZOOM,
        "resolution_m": RES,
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "tile_size_px": TILE_SIZE_PX,
        "tile_size_m": TILE_SIZE_M,
        "tile_x_min": TILE_X_MIN,
        "tile_x_max": TILE_X_MAX,
        "tile_y_min": TILE_Y_MIN,
        "tile_y_max": TILE_Y_MAX,
        "landscape_epsg6316": {"x_min": X_MIN, "x_max": X_MAX, "y_min": Y_MIN, "y_max": Y_MAX},
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(meta, indent=2))

    ok = failed = cached = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(download_tile, t): t for t in tiles}
        for i, fut in enumerate(as_completed(futures)):
            tx, ty, status, size = fut.result()
            if status == "ok":
                ok += 1
            elif status == "cached":
                cached += 1
            else:
                failed += 1
            if (i + 1) % 500 == 0:
                elapsed = time.time() - start
                rate = (i + 1) / elapsed
                print(f"Progress {i+1}/{total} ({100*(i+1)/total:.1f}%) | ok={ok} cached={cached} failed={failed} | {rate:.1f} tiles/s")

    elapsed = time.time() - start
    print(f"Done in {elapsed:.1f}s. ok={ok} cached={cached} failed={failed}")


if __name__ == "__main__":
    main()
