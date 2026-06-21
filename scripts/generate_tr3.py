#!/usr/bin/env python3
"""
Generate Condor 2 HeightMaps/hCCRR.tr3 patch files from the 30 m UTM DEM.

The patch extraction scheme is verified against Slovenia2:
- Each .tr3 is 193 x 193 uint16 little-endian meters.
- Adjacent patches share one row/column of vertices.
- Patch (c, r) global indices: [c*192 .. c*192+192] horizontally and
  [r*192 .. r*192+192] vertically, with c/r counting from the bottom-right
  (south-east) corner.
- The .tr3 array is stored as the ANTI-TRANSPOSE of the north-up GDAL patch,
  `patch.T[::-1, ::-1]` (the #1 mesh-tear bug — see below / PIPELINES.md §5).

Grid-driven via condor_grid (CONDOR_LANDSCAPE=nm -> NorthMacedonia 40x32 = 1280
patches). Parallel across all CPU cores (multiprocessing). Deterministic: same
DEM -> byte-identical .tr3 set.

Source DEM selection:
  * NorthMacedonia: the BASE (unflattened) 30 m raw — runway flattening is
    DEFERRED until the all-NM airports/.apt exist; re-run flatten + this script +
    -hash afterwards. (--source overrides.)
  * skopje (default): the runway-FLATTENED raw, so tow/winch starts work. The
    unflattened raw is available via --source for non-airport experiments.
"""

import os
import sys
import argparse
import numpy as np
from pathlib import Path
from functools import partial
from multiprocessing import Pool, cpu_count

sys.path.insert(0, str(Path(__file__).resolve().parent))
import condor_grid as g  # noqa: E402

# Landscape settings from the grid.
WIDTH = g.WIDTH
HEIGHT = g.HEIGHT
PATCH_SAMPLES = 193
INTERVAL = 192
PATCHES_X = g.PATCHES_X
PATCHES_Y = g.PATCHES_Y

ROOT = Path(__file__).resolve().parent.parent
DEM_DIR = ROOT / "sources" / "dem"

if g.LANDSCAPE_NAME == "NorthMacedonia":
    # BASE mesh (runway flatten deferred until airports exist).
    SOURCE_RAW = DEM_DIR / f"northmacedonia_dem_30m_{WIDTH}x{HEIGHT}.raw"
    SOURCE_RAW_UNFLAT = SOURCE_RAW
else:
    SOURCE_RAW = DEM_DIR / "macedonia_skopje_dem_30m_2305_flat.raw"
    SOURCE_RAW_UNFLAT = DEM_DIR / "macedonia_skopje_dem_30m_2305.raw"

OUT_DIR = Path(f"C:/Condor2/Landscapes/{g.LANDSCAPE_NAME}/HeightMaps")


def read_source(path: Path) -> np.ndarray:
    data = np.fromfile(path, dtype=np.int16)
    if data.size != WIDTH * HEIGHT:
        raise ValueError(
            f"Expected {WIDTH*HEIGHT} samples ({WIDTH}x{HEIGHT}), got {data.size}")
    arr = data.reshape(HEIGHT, WIDTH)
    # int16 raw may contain nodata as negative; clamp to 0 for Condor uint16.
    arr = np.where(arr < 0, 0, arr)
    return arr.astype(np.uint16)


def extract_patch(src: np.ndarray, c: int, r: int) -> np.ndarray:
    """
    c: column index counting WEST from bottom-right (0 = east-most)
    r: row index counting NORTH from bottom-right (0 = south-most)
    """
    j_start = (PATCHES_X - 1 - c) * INTERVAL  # westward from east edge
    i_start = (PATCHES_Y - 1 - r) * INTERVAL  # northward from south edge
    patch = src[i_start:i_start + PATCH_SAMPLES, j_start:j_start + PATCH_SAMPLES]
    if patch.shape != (PATCH_SAMPLES, PATCH_SAMPLES):
        raise ValueError(f"Patch ({c},{r}) shape {patch.shape}")
    # Condor .tr3 storage is an ANTI-TRANSPOSE of north-up GDAL (verified
    # empirically against Slovenia2 via shared-edge continuity: the correct op
    # makes adjacent-patch boundary vertices bit-exact; identity output diverges
    # by up to ~991 m -> catastrophic mesh tears/voids). In stored .tr3:
    # +row(i) = WEST, +col(j) = NORTH. The DEM slice is north-up (row 0 = north,
    # col 0 = west); apply patch.T[::-1, ::-1]. rot90(x,2) alone does NOT match.
    return patch.T[::-1, ::-1]


def _write_one(cr, src, out_dir):
    c, r = cr
    patch = extract_patch(src, c, r)
    name = f"h{c:02d}{r:02d}.tr3"
    patch.astype(np.uint16).tofile(out_dir / name)
    return name


# Worker-global DEM (loaded once per process, not pickled per task).
_SRC = None
_OUT = None


def _init_worker(source_raw, out_dir):
    global _SRC, _OUT
    _SRC = read_source(Path(source_raw))
    _OUT = Path(out_dir)


def _worker(cr):
    c, r = cr
    patch = extract_patch(_SRC, c, r)
    name = f"h{c:02d}{r:02d}.tr3"
    patch.astype(np.uint16).tofile(_OUT / name)
    return name


def main(source_raw: Path = SOURCE_RAW, out_dir: Path = OUT_DIR, workers: int = 0):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(c, r) for c in range(PATCHES_X) for r in range(PATCHES_Y)]
    n = len(tasks)
    nproc = workers if workers > 0 else cpu_count()
    nproc = min(nproc, n)
    print(f"Generating {n} .tr3 patches ({PATCHES_X}x{PATCHES_Y}) on {nproc} cores")
    print(f"  source: {source_raw}")
    print(f"  out   : {out_dir}")

    if nproc <= 1:
        src = read_source(Path(source_raw))
        for cr in tasks:
            _write_one(cr, src, out_dir)
    else:
        with Pool(processes=nproc,
                  initializer=_init_worker,
                  initargs=(str(source_raw), str(out_dir))) as pool:
            done = 0
            for _ in pool.imap_unordered(_worker, tasks, chunksize=8):
                done += 1
                if done % 128 == 0 or done == n:
                    print(f"  {done}/{n}")
    print(f"Done: {n} .tr3 files in {out_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Generate per-patch .tr3 heightmaps from a 30m raw DEM (parallel)")
    ap.add_argument("--source", default=str(SOURCE_RAW),
                    help="source int16 WIDTHxHEIGHT raw")
    ap.add_argument("--out", default=str(OUT_DIR),
                    help="output HeightMaps dir (default: installed Condor HeightMaps)")
    ap.add_argument("--workers", type=int, default=0,
                    help="worker processes (0 = all CPU cores)")
    args = ap.parse_args()
    main(Path(args.source), Path(args.out), args.workers)
