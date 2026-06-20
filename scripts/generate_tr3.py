#!/usr/bin/env python3
"""
Generate Condor 2 HeightMaps/hCCRR.tr3 patch files directly from the
flattened 30 m UTM DEM overview.

The patch extraction scheme is verified against Slovenia2:
- Each .tr3 is 193 x 193 uint16 little-endian meters.
- Adjacent patches share one row/column of vertices.
- Patch (c, r) global indices: [c*192 .. c*192+192] horizontally and
  [r*192 .. r*192+192] vertically, with c/r counting from the bottom-right
  (south-east) corner.
- The .tr3 array is stored with the same bottom-right origin as the .trn
  overview, i.e. it is a 180-degree rotation of the raw GDAL top-left patch.
"""

import os
import sys
import argparse
import struct
import numpy as np
from pathlib import Path

# Landscape settings
WIDTH = 2305
HEIGHT = 2305
PATCH_SAMPLES = 193
INTERVAL = 192
PATCHES_X = 12  # (WIDTH - 1) / INTERVAL
PATCHES_Y = 12  # (HEIGHT - 1) / INTERVAL

# Paths
ROOT = Path(__file__).resolve().parent.parent
# Canonical EXACTLY-30m raw (NW pixel-center 506880/4700160). The old
# *_2305_flat.raw was at 29.987 m/px and caused texture-vs-mesh drift.
SOURCE_RAW = ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305.raw"
OUT_DIR = Path("C:/Condor2/Landscapes/MacedoniaSkopje/HeightMaps")


def read_source(path: Path) -> np.ndarray:
    data = np.fromfile(path, dtype=np.int16)
    if data.size != WIDTH * HEIGHT:
        raise ValueError(f"Expected {WIDTH*HEIGHT} samples, got {data.size}")
    arr = data.reshape(HEIGHT, WIDTH)
    # int16 raw may contain nodata as negative; clamp to 0 for Condor uint16
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
    # by up to ~991 m -> the catastrophic mesh tears/voids we saw in-sim).
    # In stored .tr3: +row(i) = WEST, +col(j) = NORTH. The DEM slice above is
    # north-up (row 0 = north, col 0 = west); apply patch.T[::-1, ::-1].
    # NOTE: the spec's "180 degree rotation" wording is incomplete — it omits the
    # transpose. rot90(x, 2) alone does NOT match Condor. The extraction position
    # (j_start/i_start) is already correct; only per-patch orientation was wrong.
    return patch.T[::-1, ::-1]


def write_tr3(path: Path, patch: np.ndarray):
    patch.astype(np.uint16).tofile(path)


def main(source_raw: Path = SOURCE_RAW, out_dir: Path = OUT_DIR):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = read_source(Path(source_raw))

    for c in range(PATCHES_X):
        for r in range(PATCHES_Y):
            patch = extract_patch(src, c, r)
            name = f"h{c:02d}{r:02d}.tr3"
            write_tr3(out_dir / name, patch)

    print(f"Generated {PATCHES_X * PATCHES_Y} .tr3 files in {out_dir}")
    print(f"  source: {source_raw}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate 144 .tr3 patches from a 30m raw DEM")
    ap.add_argument("--source", default=str(SOURCE_RAW),
                    help="source int16 2305x2305 raw (default: canonical 30m raw)")
    ap.add_argument("--out", default=str(OUT_DIR),
                    help="output HeightMaps dir (default: installed Condor HeightMaps)")
    args = ap.parse_args()
    main(Path(args.source), Path(args.out))
