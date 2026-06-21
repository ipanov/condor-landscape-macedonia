#!/usr/bin/env python3
"""
Generate Condor 2 .trn terrain overview from the 30 m raw heightmap.

CRITICAL: The .trn is a LOW-RESOLUTION OVERVIEW at 90m per pixel.
The full 30m resolution is in the .tr3 patch files (193x193 each).
The .trn grid = patches * 64 pixels. For 12x12 patches = 768x768;
for 40x32 patches (full North Macedonia) = 2560x2048.

Header layout (36 bytes, little-endian):
  int32  width          (patches_x * 64)
  int32  height         (patches_y * 64)
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

Grid-driven via condor_grid (CONDOR_LANDSCAPE=nm -> NorthMacedonia 40x32). The
.trn BR pixel-CENTRE is computed on the 90 m overview grid (patches*64 @ 90 m),
which is NOT the 30 m DEM SE corner — they differ because a 2305@30m grid and a
768@90m grid don't share an SE corner. Objects anchor off THIS header (see
condor_grid.obj_anchor_from_trn).
"""

import sys
import struct
import numpy as np
from pathlib import Path
from scipy.ndimage import zoom

sys.path.insert(0, str(Path(__file__).resolve().parent))
import condor_grid as g  # noqa: E402

# Full-resolution DEM settings (30m) from the grid.
DEM_WIDTH = g.WIDTH
DEM_HEIGHT = g.HEIGHT
ULXMAP = g.ULXMAP
ULYMAP = g.ULYMAP

# TRN overview settings (90m, 64 px per patch).
PX_PER_PATCH = 64
TRN_WIDTH = g.PATCHES_X * PX_PER_PATCH
TRN_HEIGHT = g.PATCHES_Y * PX_PER_PATCH
PIXEL_SIZE = 90.0  # meters — MUST be 90.0

UTM_ZONE = 34
HEMISPHERE = ord('N')

# BR pixel-CENTRE for the (patches*64) grid at 90 m (the .trn header origin).
BR_EASTING = ULXMAP + (TRN_WIDTH - 1) * PIXEL_SIZE
BR_NORTHING = ULYMAP - (TRN_HEIGHT - 1) * PIXEL_SIZE

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Canonical EXACTLY-30 m raw, same source the .tr3 patches use so overview and
# mesh share one grid. NorthMacedonia uses the 7681x6145 raw; skopje the 2305.
if g.LANDSCAPE_NAME == "NorthMacedonia":
    SOURCE_RAW = PROJECT_ROOT / "sources" / "dem" / f"northmacedonia_dem_30m_{g.WIDTH}x{g.HEIGHT}.raw"
else:
    SOURCE_RAW = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305.raw"

OUT_TRN = Path(f"C:/Condor2/Landscapes/{g.LANDSCAPE_NAME}/{g.LANDSCAPE_NAME}.trn")


def main():
    # Load full-resolution DEM (int16 LE, GDAL top-left order: row 0 = north).
    dem = np.fromfile(SOURCE_RAW, dtype=np.int16)
    if dem.size != DEM_HEIGHT * DEM_WIDTH:
        raise SystemExit(
            f"DEM {SOURCE_RAW} has {dem.size} samples, expected "
            f"{DEM_HEIGHT*DEM_WIDTH} ({DEM_WIDTH}x{DEM_HEIGHT})")
    dem = dem.reshape(DEM_HEIGHT, DEM_WIDTH)
    dem = np.where(dem < 0, 0, dem).astype(np.float32)

    # Resample full DEM -> (patches*64) at 90 m. Anti-aliased downsample with a
    # per-axis scale (NM is non-square), bilinear (order=1).
    zy = TRN_HEIGHT / DEM_HEIGHT
    zx = TRN_WIDTH / DEM_WIDTH
    dem_90m = zoom(dem, (zy, zx), order=1)[:TRN_HEIGHT, :TRN_WIDTH]
    dem_90m = dem_90m.clip(0, 65535).astype(np.uint16)
    print(f"Resampled: {DEM_WIDTH}x{DEM_HEIGHT} -> {dem_90m.shape[1]}x{dem_90m.shape[0]}")

    # Build header.
    header = struct.pack('<ii', TRN_WIDTH, TRN_HEIGHT)
    header += struct.pack('<fff', PIXEL_SIZE, -PIXEL_SIZE, PIXEL_SIZE)
    header += struct.pack('<ff', float(BR_EASTING), float(BR_NORTHING))
    header += struct.pack('<HH', UTM_ZONE, 0)
    header += struct.pack('<HH', HEMISPHERE, 0)

    # Flip vertically: store south-to-north.
    data = np.flipud(dem_90m)

    OUT_TRN.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_TRN, 'wb') as f:
        f.write(header)
        f.write(data.tobytes())

    print(f"Wrote {OUT_TRN}")
    print(f"  size: {OUT_TRN.stat().st_size} bytes (expected {36 + TRN_WIDTH * TRN_HEIGHT * 2})")
    print(f"  Grid: {TRN_WIDTH}x{TRN_HEIGHT} at {PIXEL_SIZE}m")
    print(f"  Header floats: ({PIXEL_SIZE}, {-PIXEL_SIZE}, {PIXEL_SIZE})")
    print(f"  BR pixel-centre: E={BR_EASTING:.1f}, N={BR_NORTHING:.1f}  zone {UTM_ZONE}N")
    print(f"  Extent: {(TRN_WIDTH-1)*PIXEL_SIZE/1000:.1f} x {(TRN_HEIGHT-1)*PIXEL_SIZE/1000:.1f} km")


if __name__ == "__main__":
    main()
