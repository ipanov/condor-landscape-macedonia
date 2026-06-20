#!/usr/bin/env python3
"""
Generate Condor 2 .trn terrain overview from the flattened raw heightmap.

CRITICAL: The .trn is a LOW-RESOLUTION OVERVIEW at 90m per pixel.
The full 30m resolution is in the .tr3 patch files (193x193 each).
The .trn grid = patches * 64 pixels. For 12x12 patches = 768x768.

Header layout (36 bytes, little-endian):
  int32  width          (768 for 12x12 patches)
  int32  height         (768 for 12x12 patches)
  float  pixel_size_x   (90.0 — ALWAYS 90.0, do NOT change)
  float  pixel_size_y   (-90.0 — ALWAYS -90.0, do NOT change)
  float  pixel_size_z   (90.0 — ALWAYS 90.0, do NOT change)
  float  br_easting     (UTM easting of bottom-right pixel center)
  float  br_northing    (UTM northing of bottom-right pixel center)
  uint16 utm_zone       (34 for Macedonia)
  uint16 pad            (0)
  uint16 hemisphere     ('N' = 78)
  uint16 pad            (0)

Elevation data: uint16 meters, rows stored south-to-north (flipped from GDAL).
"""

import struct
import numpy as np
from pathlib import Path
from scipy.ndimage import zoom

# Full-resolution DEM settings (30m)
DEM_WIDTH = 2305
DEM_HEIGHT = 2305
ULXMAP = 506880.0
ULYMAP = 4700160.0

# TRN overview settings (90m, 64 px per patch)
PATCHES_X = 12
PATCHES_Y = 12
PX_PER_PATCH = 64
TRN_WIDTH = PATCHES_X * PX_PER_PATCH   # 768
TRN_HEIGHT = PATCHES_Y * PX_PER_PATCH  # 768
PIXEL_SIZE = 90.0  # meters — MUST be 90.0

UTM_ZONE = 34
HEMISPHERE = ord('N')

# BR corner for the 768x768 grid at 90m
BR_EASTING = ULXMAP + (TRN_WIDTH - 1) * PIXEL_SIZE
BR_NORTHING = ULYMAP - (TRN_HEIGHT - 1) * PIXEL_SIZE

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Canonical EXACTLY-30m raw (NW pixel-center 506880/4700160), same source the
# .tr3 patches use, so overview and mesh share one grid.
SOURCE_RAW = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305.raw"
OUT_TRN = Path("C:/Condor2/Landscapes/MacedoniaSkopje/MacedoniaSkopje.trn")


def main():
    # Load full-resolution DEM
    dem = np.fromfile(SOURCE_RAW, dtype=np.int16).reshape(DEM_HEIGHT, DEM_WIDTH)
    dem = np.where(dem < 0, 0, dem).astype(np.float32)

    # Resample from 2305x2305 (30m) to 768x768 (90m)
    scale = TRN_WIDTH / DEM_WIDTH
    dem_90m = zoom(dem, scale, order=1)[:TRN_HEIGHT, :TRN_WIDTH]
    dem_90m = dem_90m.clip(0, 65535).astype(np.uint16)
    print(f"Resampled: {DEM_WIDTH}x{DEM_HEIGHT} -> {dem_90m.shape[1]}x{dem_90m.shape[0]}")

    # Build header
    header = struct.pack('<ii', TRN_WIDTH, TRN_HEIGHT)
    header += struct.pack('<fff', PIXEL_SIZE, -PIXEL_SIZE, PIXEL_SIZE)
    header += struct.pack('<ff', float(BR_EASTING), float(BR_NORTHING))
    header += struct.pack('<HH', UTM_ZONE, 0)
    header += struct.pack('<HH', HEMISPHERE, 0)

    # Flip vertically: store south-to-north
    data = np.flipud(dem_90m)

    OUT_TRN.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_TRN, 'wb') as f:
        f.write(header)
        f.write(data.tobytes())

    print(f"Wrote {OUT_TRN}")
    print(f"  size: {OUT_TRN.stat().st_size} bytes (expected {36 + TRN_WIDTH * TRN_HEIGHT * 2})")
    print(f"  Grid: {TRN_WIDTH}x{TRN_HEIGHT} at {PIXEL_SIZE}m")
    print(f"  BR: E={BR_EASTING:.1f}, N={BR_NORTHING:.1f}")
    print(f"  Extent: {(TRN_WIDTH-1)*PIXEL_SIZE/1000:.1f} x {(TRN_HEIGHT-1)*PIXEL_SIZE/1000:.1f} km")


if __name__ == "__main__":
    main()
