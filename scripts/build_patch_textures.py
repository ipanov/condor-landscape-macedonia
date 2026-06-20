#!/usr/bin/env python3
"""
Build 144 Condor patch-level DDS textures from the MK 2023 ortho VRT.

Each patch gets a 2048x2048 DXT3 DDS file (matching Slovenia2 format).
Uses gdalwarp for reprojection and nvcompress (GPU) for compression.

Patch naming: tCCRR.dds where CC=column (0=east), RR=row (0=south).
"""
import os
import sys
import subprocess
import shutil
import time
import struct
import numpy as np
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Import the grid from condor_grid so texture registration matches the mesh
# (.trn/.tr3). condor_grid now uses EXACTLY XDIM=30.0 — the old hardcoded
# 29.9869848156182 drifted the texture grid ~30 m from the mesh at the SE corner.
from condor_grid import (
    ULXMAP, ULYMAP, XDIM, WIDTH, HEIGHT, BR_EASTING, BR_NORTHING,
    PATCH_SIZE_M, PATCHES_X, PATCHES_Y, patch_bounds_utm,
)

ROOT = Path(__file__).resolve().parent.parent
VRT = ROOT / ".sandbox" / "ortho_utm_work" / "ortho_6316.vrt"
WORK = ROOT / ".sandbox" / "patch_textures"
CONDOR_TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
GDALWARP = "C:/Program Files/QGIS 4.0.0/bin/gdalwarp.exe"
GDAL_TRANSLATE = "C:/Program Files/QGIS 4.0.0/bin/gdal_translate.exe"
NVCOMPRESS = "C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe"

TEX_SIZE = 2048  # pixels per patch texture (matches Slovenia2)


def process_patch(col, row):
    """Generate one patch DDS texture."""
    name = f"t{col:02d}{row:02d}"
    e_min, n_min, e_max, n_max = patch_bounds_utm(col, row)

    tif_path = WORK / f"{name}.tif"
    png_path = WORK / f"{name}.png"
    dds_path = WORK / f"{name}.dds"

    # Step 1: gdalwarp VRT -> GeoTIFF at 2048x2048
    cmd_warp = [
        GDALWARP,
        "-s_srs", "EPSG:6316",
        "-t_srs", "EPSG:32634",
        "-te", str(e_min), str(n_min), str(e_max), str(n_max),
        "-ts", str(TEX_SIZE), str(TEX_SIZE),
        "-r", "bilinear",
        "-ot", "Byte",
        "-of", "GTiff",
        "-co", "COMPRESS=NONE",
        "-overwrite",
        "-wo", "NUM_THREADS=2",
        str(VRT), str(tif_path)
    ]
    r = subprocess.run(cmd_warp, capture_output=True, text=True)
    if r.returncode != 0:
        return name, f"gdalwarp failed: {r.stderr[:200]}"

    # Step 2: Convert to PNG (nvcompress reads PNG well)
    cmd_tr = [
        GDAL_TRANSLATE,
        "-of", "PNG",
        "-co", "WORLDFILE=NO",
        str(tif_path), str(png_path)
    ]
    subprocess.run(cmd_tr, capture_output=True, text=True)
    tif_path.unlink(missing_ok=True)

    if not png_path.exists() or png_path.stat().st_size < 1000:
        return name, "gdal_translate failed or empty output"

    # Step 3: nvcompress to DXT1 DDS (GPU accelerated)
    # Using DXT1 (bc1) like Slovenia2 uses for non-water patches
    # DXT3 (bc2) for patches with water — we'll use bc1 for now, upgrade later
    cmd_nv = [
        NVCOMPRESS,
        "-bc1",        # DXT1 compression
        "-highest",    # best quality
        "-mipfilter", "kaiser",
        "-color",
        "-clamp",
        "-silent",
        str(png_path), str(dds_path)
    ]
    r = subprocess.run(cmd_nv, capture_output=True, text=True)
    png_path.unlink(missing_ok=True)

    if r.returncode != 0 or not dds_path.exists():
        return name, f"nvcompress failed: {r.stderr[:200]}"

    # Step 4: Copy to Condor
    dst = CONDOR_TEX / f"{name}.dds"
    shutil.copy2(str(dds_path), str(dst))

    sz = dst.stat().st_size
    return name, f"OK ({sz/1024:.0f} KB)"


def create_empty_dds():
    """Create a proper 2048x2048 empty.dds (black, DXT1)."""
    print("Creating empty.dds (2048x2048 black DXT1)...")
    # Create black PNG
    from PIL import Image
    black = Image.new("RGB", (TEX_SIZE, TEX_SIZE), (0, 0, 0))
    black_path = WORK / "empty.png"
    black.save(str(black_path))

    empty_dds = WORK / "empty.dds"
    cmd = [
        NVCOMPRESS, "-bc1", "-highest", "-silent",
        str(black_path), str(empty_dds)
    ]
    subprocess.run(cmd, capture_output=True)
    black_path.unlink(missing_ok=True)

    if empty_dds.exists():
        shutil.copy2(str(empty_dds), str(CONDOR_TEX / "empty.dds"))
        print(f"  empty.dds: {empty_dds.stat().st_size} bytes")
    else:
        print("  FAILED to create empty.dds")


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    CONDOR_TEX.mkdir(parents=True, exist_ok=True)

    if not VRT.exists():
        print(f"ERROR: VRT not found at {VRT}")
        print("Run process_mk_ortho.py first to build the VRT.")
        sys.exit(1)

    print(f"Building {PATCHES_X * PATCHES_Y} patch textures at {TEX_SIZE}x{TEX_SIZE}")
    print(f"VRT: {VRT}")
    print(f"Output: {CONDOR_TEX}")
    print(f"gdalwarp: {GDALWARP}")
    print(f"nvcompress: {NVCOMPRESS}")
    print()

    # Create empty.dds first
    create_empty_dds()

    # Process all 144 patches in parallel
    # Limit workers to avoid overwhelming GPU with nvcompress
    patches = [(c, r) for r in range(PATCHES_Y) for c in range(PATCHES_X)]
    max_workers = min(os.cpu_count() or 4, 6)
    print(f"\nProcessing {len(patches)} patches with {max_workers} parallel workers...")

    results = {}
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(process_patch, c, r): f"t{c:02d}{r:02d}" for c, r in patches}
        done = 0
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                result_name, status = fut.result()
                results[result_name] = status
                done += 1
                if done % 12 == 0 or done == len(patches):
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (len(patches) - done) / rate if rate > 0 else 0
                    print(f"  [{done}/{len(patches)}] {result_name}: {status} "
                          f"({elapsed:.0f}s elapsed, ETA {eta:.0f}s)", flush=True)
            except Exception as e:
                results[name] = f"EXCEPTION: {e}"
                done += 1

    elapsed = time.time() - t0
    ok = sum(1 for v in results.values() if v.startswith("OK"))
    failed = len(results) - ok

    print(f"\n{'='*60}")
    print(f"RESULTS: {ok}/{len(patches)} OK, {failed} failed in {elapsed:.0f}s")
    if failed > 0:
        print("\nFailed patches:")
        for n in sorted(results):
            if not results[n].startswith("OK"):
                print(f"  {n}: {results[n]}")

    # Verify output
    print(f"\nCondor textures: {len(list(CONDOR_TEX.glob('t*.dds')))} DDS files")
    print(f"Empty.dds: {(CONDOR_TEX / 'empty.dds').stat().st_size} bytes")


if __name__ == "__main__":
    main()
