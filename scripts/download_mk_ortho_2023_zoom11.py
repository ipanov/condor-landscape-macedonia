#!/usr/bin/env python3
"""Async parallel downloader for Macedonian 2023 color orthophoto at zoom 11.

Zoom 11 = 0.28 m/px, tile size 256x256, covers ~71.68 m per tile.
Run multiple instances for different tile ranges to saturate bandwidth.
Files are stored in subdirectories to avoid huge flat directories.
"""
import os
import sys
import json
import math
import time
import asyncio
import aiohttp
from pathlib import Path
from urllib.parse import urlencode
from tqdm import tqdm

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".sandbox" / "textures_mk2023_z11"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# EPSG:6316 MSCS6316 grid definition
ORIGIN_X = 7397000.634424793
ORIGIN_Y = 4521901.793180252
RESOLUTIONS = [560, 280, 140, 70, 35, 17.92, 8.96, 4.48, 2.24, 1.12, 0.56, 0.28, 0.14]
ZOOM = 11
RES = RESOLUTIONS[ZOOM]
TILE_SIZE_PX = 256
TILE_SIZE_M = TILE_SIZE_PX * RES

# Landscape bounds in UTM34 (from DEM header)
UTM_E_MIN = 506880.0
UTM_E_MAX = 575970.011
UTM_N_MIN = 4631069.989
UTM_N_MAX = 4700160.0

# Convert UTM34 bounds to EPSG:6316
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

TILE_X_MIN = int(math.floor((X_MIN - ORIGIN_X) / TILE_SIZE_M))
TILE_X_MAX = int(math.floor((X_MAX - ORIGIN_X) / TILE_SIZE_M)) + 1
TILE_Y_MIN = int(math.floor((Y_MIN - ORIGIN_Y) / TILE_SIZE_M))
TILE_Y_MAX = int(math.floor((Y_MAX - ORIGIN_Y) / TILE_SIZE_M)) + 1

BASE_URL = "https://e-uslugi.katastar.gov.mk/geo/proxy/gwc/wms"
HEADERS = {"Referer": "https://e-uslugi.katastar.gov.mk/"}


def tile_path(tx, ty):
    """Return subdirectory path based on tile x/y to avoid huge flat dirs."""
    return OUT_DIR / f"x{tx // 256}" / f"y{ty // 256}" / f"z{ZOOM}_x{tx}_y{ty}.jpg"


def old_tile_path(tx, ty):
    """Legacy flat path for already-downloaded tiles."""
    return OUT_DIR / f"z{ZOOM}_x{tx}_y{ty}.jpg"


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


def build_existing_set():
    """Build a fast lookup set of already-downloaded tile filenames (flat + subdirs)."""
    existing = set()
    # Legacy flat files
    with os.scandir(OUT_DIR) as it:
        for entry in it:
            if entry.is_file() and entry.name.endswith(".jpg"):
                st = entry.stat()
                if st.st_size > 100:
                    existing.add(entry.name)
    # New subdirectory files
    for sub in OUT_DIR.iterdir():
        if not (sub.is_dir() and sub.name.startswith("x")):
            continue
        for sub2 in sub.iterdir():
            if not (sub2.is_dir() and sub2.name.startswith("y")):
                continue
            for f in sub2.iterdir():
                if f.suffix == ".jpg" and f.stat().st_size > 100:
                    existing.add(f.name)
    return existing


async def download_tile(session, tx, ty, sem, stats, existing):
    out_path = tile_path(tx, ty)
    fname = out_path.name
    if fname in existing:
        stats["cached"] += 1
        return "cached"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    err_path = out_path.with_suffix(".err")
    url = tile_url(tx, ty)
    async with sem:
        for attempt in range(5):
            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200:
                        content_type = r.headers.get("Content-Type", "")
                        data = await r.read()
                        if content_type.startswith("image") and len(data) > 100:
                            out_path.write_bytes(data)
                            stats["ok"] += 1
                            return "ok"
                        else:
                            if attempt == 0:
                                err_path.write_text(f"{r.status}\n{content_type}\n{data[:500].decode('utf-8', errors='ignore')}")
                    else:
                        if attempt == 0:
                            txt = await r.text()
                            err_path.write_text(f"{r.status}\n{txt[:500]}")
                    await asyncio.sleep(0.3 * (attempt + 1))
            except Exception as e:
                await asyncio.sleep(0.3 * (attempt + 1))
    stats["failed"] += 1
    return "failed"


def load_existing_set(index_path):
    """Load existing tile set from a pre-built text index."""
    existing = set()
    with open(index_path, "r") as f:
        for line in f:
            name = line.strip()
            if name:
                existing.add(name)
    return existing


async def main():
    tx_min = int(sys.argv[1]) if len(sys.argv) > 1 else TILE_X_MIN
    tx_max = int(sys.argv[2]) if len(sys.argv) > 2 else TILE_X_MAX
    ty_min = int(sys.argv[3]) if len(sys.argv) > 3 else TILE_Y_MIN
    ty_max = int(sys.argv[4]) if len(sys.argv) > 4 else TILE_Y_MAX
    concurrency = int(sys.argv[5]) if len(sys.argv) > 5 else 256
    cache_index = sys.argv[6] if len(sys.argv) > 6 else None

    total = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)
    print(f"Zoom {ZOOM} ({RES} m/px) region X:{tx_min}..{tx_max} Y:{ty_min}..{ty_max}", flush=True)
    print(f"Tiles: {total}, concurrency: {concurrency}", flush=True)

    if cache_index and Path(cache_index).exists():
        print(f"Loading cache index from {cache_index}...", flush=True)
        existing = load_existing_set(cache_index)
    else:
        print("Building cache index...", flush=True)
        existing = build_existing_set()
    print(f"Cache index: {len(existing)} tiles already present", flush=True)

    meta_path = OUT_DIR / "metadata.json"
    if not meta_path.exists():
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
        meta_path.write_text(json.dumps(meta, indent=2))

    stats = {"ok": 0, "failed": 0, "cached": 0}
    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(
        limit=concurrency,
        limit_per_host=concurrency,
        ttl_dns_cache=300,
        use_dns_cache=True,
        keepalive_timeout=30,
    )

    tiles = [(x, y) for y in range(ty_min, ty_max + 1) for x in range(tx_min, tx_max + 1)]

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [asyncio.create_task(download_tile(session, x, y, sem, stats, existing)) for x, y in tiles]
        pbar = tqdm(total=total, desc=f"Z11 {tx_min}-{tx_max},{ty_min}-{ty_max}")
        for coro in asyncio.as_completed(tasks):
            await coro
            pbar.update(1)
        pbar.close()

    print(f"Region done. ok={stats['ok']} cached={stats['cached']} failed={stats['failed']} / {total}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
