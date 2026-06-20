#!/usr/bin/env python3
"""
Reproject VRT to UTM34 Condor tiles and compress to DDS.
Uses gdalwarp (GDAL) for reprojection and nvcompress (GPU) for DDS.
Runs tiles in parallel to load all hardware.
"""
import subprocess
import shutil
import time
import sys
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
VRT = ROOT / ".sandbox" / "ortho_utm_work" / "ortho_6316.vrt"
WORK = ROOT / ".sandbox" / "ortho_utm_work"
CONDOR = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
GDALWARP = "C:/Program Files/QGIS 4.0.0/bin/gdalwarp.exe"
NVCOMPRESS = "C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe"

# Landscape UTM 34N
E_MIN, E_MAX = 506880.0, 575970.011
N_MIN, N_MAX = 4631069.989, 4700160.0
TILE_W = (E_MAX - E_MIN) / 3
TILE_H = (N_MAX - N_MIN) / 3
TEX = 8192


def tile_bounds(col, row):
    """Condor tile (col=0 east, row=0 south) UTM bounds."""
    e2 = E_MAX - col * TILE_W
    e1 = e2 - TILE_W
    n1 = N_MIN + row * TILE_H
    n2 = n1 + TILE_H
    return e1, n1, e2, n2


def process_tile(col, row):
    name = f"t{col:02d}{row:02d}"
    e1, n1, e2, n2 = tile_bounds(col, row)
    tif = WORK / f"{name}.tif"
    bmp = WORK / f"{name}.bmp"
    dds = WORK / f"{name}.dds"

    # Step 1: gdalwarp VRT -> GeoTIFF (reprojection)
    t0 = time.time()
    print(f"[{name}] gdalwarp EPSG:6316 -> EPSG:32634 ({TEX}x{TEX})...", flush=True)
    cmd = [
        GDALWARP,
        "-s_srs", "EPSG:6316",
        "-t_srs", "EPSG:32634",
        "-te", str(e1), str(n1), str(e2), str(n2),
        "-ts", str(TEX), str(TEX),
        "-r", "bilinear",
        "-ot", "Byte",
        "-of", "GTiff",
        "-co", "COMPRESS=NONE",
        "-co", "TILED=YES",
        "-co", "BLOCKXSIZE=256",
        "-co", "BLOCKYSIZE=256",
        "-overwrite",
        "-multi",
        "-wo", "NUM_THREADS=ALL_CPUS",
        str(VRT), str(tif)
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[{name}] gdalwarp FAILED: {r.stderr[:500]}", flush=True)
        return name, None
    dt = time.time() - t0
    sz = tif.stat().st_size / (1024*1024)
    print(f"[{name}] gdalwarp done in {dt:.0f}s ({sz:.0f} MB)", flush=True)

    # Step 2: Convert to BMP for nvcompress
    t0 = time.time()
    cmd2 = [
        "C:/Program Files/QGIS 4.0.0/bin/gdal_translate.exe",
        "-of", "BMP",
        "-co", "WORLDFILE=NO",
        str(tif), str(bmp)
    ]
    subprocess.run(cmd2, capture_output=True, text=True)
    tif.unlink(missing_ok=True)  # Clean up large TIF
    print(f"[{name}] BMP ready ({bmp.stat().st_size/(1024*1024):.0f} MB)", flush=True)

    # Step 3: nvcompress GPU DDS
    t0 = time.time()
    print(f"[{name}] nvcompress -bc1 -highest (GPU)...", flush=True)
    cmd3 = [
        NVCOMPRESS,
        "-bc1", "-highest",
        "-mipfilter", "kaiser",
        "-color", "-clamp", "-silent",
        str(bmp), str(dds)
    ]
    r = subprocess.run(cmd3, capture_output=True, text=True)
    bmp.unlink(missing_ok=True)  # Clean up BMP
    if r.returncode != 0:
        print(f"[{name}] nvcompress FAILED: {r.stderr[:500]}", flush=True)
        return name, None
    dt = time.time() - t0
    sz = dds.stat().st_size / (1024*1024)
    print(f"[{name}] DDS done in {dt:.0f}s ({sz:.1f} MB)", flush=True)

    # Copy to Condor
    CONDOR.mkdir(parents=True, exist_ok=True)
    dst = CONDOR / f"{name}.dds"
    shutil.copy2(str(dds), str(dst))
    print(f"[{name}] COMPLETE -> {dst}", flush=True)
    return name, str(dst)


def main():
    if not VRT.exists():
        print(f"VRT not found: {VRT}")
        sys.exit(1)

    tiles = [(c, r) for r in range(3) for c in range(3)]
    print(f"Processing {len(tiles)} tiles from VRT ({VRT.stat().st_size/(1024*1024):.0f} MB)")
    print(f"gdalwarp: {GDALWARP}")
    print(f"nvcompress: {NVCOMPRESS}")
    print()

    # gdalwarp is already multithreaded (-multi -wo NUM_THREADS=ALL_CPUS)
    # Run 2 tiles at a time to keep CPU+GPU busy
    results = {}
    with ProcessPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(process_tile, c, r): f"t{c:02d}{r:02d}" for c, r in tiles}
        for fut in as_completed(futs):
            name, path = fut.result()
            results[name] = path

    print("\n" + "=" * 50)
    ok = sum(1 for v in results.values() if v)
    print(f"Results: {ok}/{len(tiles)} tiles OK")
    for n in sorted(results):
        print(f"  {n}: {'OK' if results[n] else 'FAIL'} {results[n] or ''}")


if __name__ == "__main__":
    main()
