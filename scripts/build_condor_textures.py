#!/usr/bin/env python3
"""
Build Condor DDS textures from MK 2023 orthophoto tiles.

Strategy: For each of the 9 Condor tiles, directly read the source JPEGs
that overlap the tile's UTM bounds, stitch them in memory, and save as
a BMP for nvcompress. Runs all 9 tiles in parallel.

This avoids the VRT approach which chokes on 732K files.
"""
import os
import sys
import json
import math
import shutil
import subprocess
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

import numpy as np
from PIL import Image
import pyproj

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / ".sandbox" / "textures_mk2023_z11"
WORK_DIR = ROOT / ".sandbox" / "ortho_utm_work"
WORK_DIR.mkdir(parents=True, exist_ok=True)

CONDOR_TEX_DIR = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
NVCOMPRESS = Path("C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe")

# Source tile grid (EPSG:6316)
ORIGIN_X = 7397000.634424793
ORIGIN_Y = 4521901.793180252
TILE_SIZE_PX = 256
RES = 0.28  # meters per pixel
TILE_SIZE_M = TILE_SIZE_PX * RES  # 71.68 m

# Landscape UTM 34N bounds
UTM_E_MIN = 506880.0
UTM_E_MAX = 575970.011
UTM_N_MIN = 4631069.989
UTM_N_MAX = 4700160.0

# Condor tile grid
TILES_X = 3
TILES_Y = 3
TILE_UTM_W = (UTM_E_MAX - UTM_E_MIN) / TILES_X
TILE_UTM_H = (UTM_N_MAX - UTM_N_MIN) / TILES_Y

# Output texture size
TEX_SIZE = 8192

# CRS transformers
_to_6316 = pyproj.Transformer.from_crs("EPSG:32634", "EPSG:6316", always_xy=True)
_to_utm = pyproj.Transformer.from_crs("EPSG:6316", "EPSG:32634", always_xy=True)


def condor_tile_bounds_utm(col, row):
    """Condor tile col/row (SE origin) to UTM bounds."""
    # col 0 = east, row 0 = south
    e_max = UTM_E_MAX - col * TILE_UTM_W
    e_min = e_max - TILE_UTM_W
    n_min = UTM_N_MIN + row * TILE_UTM_H
    n_max = n_min + TILE_UTM_H
    return e_min, n_min, e_max, n_max


def find_tile_path(tx, ty):
    """Find a source tile file in flat or subdir layout."""
    fname = f"z11_x{tx}_y{ty}.jpg"
    # Subdirectory layout
    sub = SRC_DIR / f"x{tx // 256}" / f"y{ty // 256}" / fname
    if sub.exists() and sub.stat().st_size > 100:
        return sub
    # Flat layout
    flat = SRC_DIR / fname
    if flat.exists() and flat.stat().st_size > 100:
        return flat
    return None


def process_condor_tile(col, row):
    """Build one Condor tile texture by stitching source tiles."""
    tile_name = f"t{col:02d}{row:02d}"
    print(f"[{tile_name}] Starting...", flush=True)

    # Get UTM bounds for this Condor tile
    e_min, n_min, e_max, n_max = condor_tile_bounds_utm(col, row)
    print(f"[{tile_name}] UTM bounds: E {e_min:.0f}-{e_max:.0f}, N {n_min:.0f}-{n_max:.0f}", flush=True)

    # Convert corners to EPSG:6316 to find source tile range
    corners_utm = [(e_min, n_min), (e_min, n_max), (e_max, n_min), (e_max, n_max)]
    xs_6316, ys_6316 = [], []
    for e, n in corners_utm:
        x, y = _to_6316.transform(e, n)
        xs_6316.append(x)
        ys_6316.append(y)

    x_min_6316 = min(xs_6316)
    x_max_6316 = max(xs_6316)
    y_min_6316 = min(ys_6316)
    y_max_6316 = max(ys_6316)

    # Add buffer
    buf = TILE_SIZE_M * 2
    x_min_6316 -= buf
    x_max_6316 += buf
    y_min_6316 -= buf
    y_max_6316 += buf

    # Source tile index range
    tx_min = int(math.floor((x_min_6316 - ORIGIN_X) / TILE_SIZE_M))
    tx_max = int(math.ceil((x_max_6316 - ORIGIN_X) / TILE_SIZE_M))
    ty_min = int(math.floor((y_min_6316 - ORIGIN_Y) / TILE_SIZE_M))
    ty_max = int(math.ceil((y_max_6316 - ORIGIN_Y) / TILE_SIZE_M))

    n_tiles_x = tx_max - tx_min + 1
    n_tiles_y = ty_max - ty_min + 1
    print(f"[{tile_name}] Source tiles: {n_tiles_x}x{n_tiles_y} = {n_tiles_x * n_tiles_y}", flush=True)

    # Build intermediate mosaic in EPSG:6316 space
    mosaic_w = n_tiles_x * TILE_SIZE_PX
    mosaic_h = n_tiles_y * TILE_SIZE_PX

    # Mosaic bounds in EPSG:6316
    mos_x_min = ORIGIN_X + tx_min * TILE_SIZE_M
    mos_y_min = ORIGIN_Y + ty_min * TILE_SIZE_M
    mos_x_max = mos_x_min + n_tiles_x * TILE_SIZE_M
    mos_y_max = mos_y_min + n_tiles_y * TILE_SIZE_M

    # Allocate mosaic
    mosaic = np.zeros((mosaic_h, mosaic_w, 3), dtype=np.uint8)

    loaded = 0
    missing = 0
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            path = find_tile_path(tx, ty)
            if path is None:
                missing += 1
                continue
            try:
                img = np.array(Image.open(path))
                if img.ndim == 2:
                    img = np.stack([img] * 3, axis=-1)
                elif img.shape[2] == 4:
                    img = img[:, :, :3]
                # Position in mosaic (y-axis: tile ty=ty_min is at bottom of mosaic in EPSG:6316)
                # But image rows go top-to-bottom, and EPSG:6316 Y increases northward
                # So tile ty_max is at the top of the mosaic image
                px = (tx - tx_min) * TILE_SIZE_PX
                py = (ty_max - ty) * TILE_SIZE_PX  # flip Y
                h, w = img.shape[:2]
                mosaic[py:py + h, px:px + w] = img[:h, :w]
                loaded += 1
            except Exception as e:
                missing += 1

    pct = loaded / max(loaded + missing, 1) * 100
    print(f"[{tile_name}] Loaded {loaded} tiles, missing {missing} ({pct:.0f}% coverage)", flush=True)

    # Now resample from EPSG:6316 to UTM 34N at TEX_SIZE x TEX_SIZE
    # For each output pixel, compute its UTM coordinate, transform to EPSG:6316,
    # then sample from the mosaic
    print(f"[{tile_name}] Reprojecting to UTM 34N at {TEX_SIZE}x{TEX_SIZE}...", flush=True)

    # Output pixel grid in UTM
    out_es = np.linspace(e_min, e_max, TEX_SIZE, endpoint=False) + (e_max - e_min) / (2 * TEX_SIZE)
    out_ns = np.linspace(n_max, n_min, TEX_SIZE, endpoint=False) - (n_max - n_min) / (2 * TEX_SIZE)
    ee, nn = np.meshgrid(out_es, out_ns)

    # Transform to EPSG:6316
    xx, yy = _to_6316.transform(ee.ravel(), nn.ravel())
    xx = np.array(xx).reshape(TEX_SIZE, TEX_SIZE)
    yy = np.array(yy).reshape(TEX_SIZE, TEX_SIZE)

    # Convert to mosaic pixel coordinates
    px_x = ((xx - mos_x_min) / (mos_x_max - mos_x_min) * mosaic_w).astype(np.float32)
    px_y = ((mos_y_max - yy) / (mos_y_max - mos_y_min) * mosaic_h).astype(np.float32)  # flip Y

    # Nearest-neighbor sampling (fast)
    px_x = np.clip(px_x.astype(np.int32), 0, mosaic_w - 1)
    px_y = np.clip(px_y.astype(np.int32), 0, mosaic_h - 1)

    output = mosaic[px_y, px_x]

    # Save as BMP
    bmp_path = WORK_DIR / f"{tile_name}.bmp"
    Image.fromarray(output).save(str(bmp_path), "BMP")
    bmp_size = bmp_path.stat().st_size / (1024 * 1024)
    print(f"[{tile_name}] Saved BMP: {bmp_path} ({bmp_size:.0f} MB)", flush=True)

    # Compress to DDS
    dds_path = WORK_DIR / f"{tile_name}.dds"
    print(f"[{tile_name}] Compressing to DDS DXT1 (GPU)...", flush=True)
    cmd = [
        str(NVCOMPRESS),
        "-bc1", "-highest",
        "-mipfilter", "kaiser",
        "-color", "-clamp", "-silent",
        str(bmp_path), str(dds_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[{tile_name}] nvcompress FAILED: {result.stderr}", flush=True)
        return tile_name, None

    dds_size = dds_path.stat().st_size / (1024 * 1024)
    print(f"[{tile_name}] DDS: {dds_path} ({dds_size:.1f} MB)", flush=True)

    # Copy to Condor
    dst = CONDOR_TEX_DIR / f"{tile_name}.dds"
    shutil.copy2(str(dds_path), str(dst))
    print(f"[{tile_name}] DONE — copied to {dst}", flush=True)

    # Clean up BMP to save disk
    bmp_path.unlink(missing_ok=True)

    return tile_name, str(dst)


def main():
    CONDOR_TEX_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Processing {TILES_X * TILES_Y} Condor tiles in parallel...")
    print(f"Source: {SRC_DIR}")
    print(f"Output: {CONDOR_TEX_DIR}")
    print(f"Workers: {min(cpu_count(), 4)}")
    print(f"Target: {TEX_SIZE}x{TEX_SIZE} DDS DXT1")
    print()

    tiles = [(col, row) for row in range(TILES_Y) for col in range(TILES_X)]

    # Process tiles — limit parallelism due to memory (each tile needs ~2-4 GB)
    results = {}
    with ProcessPoolExecutor(max_workers=min(cpu_count(), 3)) as ex:
        futures = {ex.submit(process_condor_tile, c, r): (c, r) for c, r in tiles}
        for fut in as_completed(futures):
            col, row = futures[fut]
            try:
                tile_name, dds_path = fut.result()
                results[tile_name] = dds_path
                print(f"\n>>> {tile_name} complete: {dds_path}\n", flush=True)
            except Exception as e:
                print(f"\n>>> t{col:02d}{row:02d} FAILED: {e}\n", flush=True)

    print("\n" + "=" * 60)
    print("RESULTS:")
    for tn, dp in sorted(results.items()):
        status = "OK" if dp else "FAILED"
        print(f"  {tn}: {status} {dp or ''}")
    print(f"\nTotal: {sum(1 for v in results.values() if v)}/{len(tiles)} tiles")


if __name__ == "__main__":
    main()
