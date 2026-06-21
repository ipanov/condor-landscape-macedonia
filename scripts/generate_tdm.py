#!/usr/bin/env python3
"""
Generate Condor 2 thermal map (.tdm) directly from the (flattened) DEM.

Format:
  int32 width
  int32 height
  width x height uint8 values (0 = no thermals, 255 = strongest)

A simple but sensible model:
  - Higher elevations and gentler slopes get stronger thermals.
  - Water / flat valleys get weaker thermals.
  - Values are stretched to the 0-255 range.

Grid-driven via condor_grid (CONDOR_LANDSCAPE switches skopje<->nm). The .tdm MUST
match the .trn overview dimensions (patches x 64): skopje 768x768, NM 2560x2048.
The source 30 m DEM is patches*192+1 per side (skopje 2305, NM 7681x6145); we
resample it down to the overview grid. Deterministic: same DEM -> identical .tdm.
"""

import sys
import struct
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import condor_grid as g  # noqa: E402  (honours CONDOR_LANDSCAPE)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEM_DIR = PROJECT_ROOT / "sources" / "dem"

# Full 30 m DEM dimensions from the grid (patches*192+1).
DEM_WIDTH = g.WIDTH
DEM_HEIGHT = g.HEIGHT

# Thermal map dimensions MUST match the .trn overview (90 m, patches x 64).
WIDTH = g.PATCHES_X * 64                 # skopje 768 ; nm 2560
HEIGHT = g.PATCHES_Y * 64                # skopje 768 ; nm 2048

# Source DEM + slope cellsize, per landscape.
#   skopje: LEGACY path preserved BYTE-FOR-BYTE -- the installed/verified .tdm was
#           built from macedonia_skopje_dem_2305_flat.raw with a 30 m slope divisor
#           (a pre-canonical choice). Changing either re-hashes the file, so the
#           skopje regression (default env must reproduce the installed .tdm)
#           pins SOURCE_RAW + SLOPE_CELL_M to those exact legacy values.
#   nm    : canonical runway-FLATTENED 30 m DEM (falls back to base if not built),
#           with a physically-correct 90 m slope cellsize on the overview grid.
if g.LANDSCAPE_NAME == "NorthMacedonia":
    _CANDIDATES = [
        DEM_DIR / f"northmacedonia_dem_30m_{DEM_WIDTH}x{DEM_HEIGHT}_flat.raw",
        DEM_DIR / f"northmacedonia_dem_30m_{DEM_WIDTH}x{DEM_HEIGHT}.raw",
    ]
    SLOPE_CELL_M = 90.0
else:
    _CANDIDATES = [
        DEM_DIR / "macedonia_skopje_dem_2305_flat.raw",   # legacy (byte-identity)
    ]
    SLOPE_CELL_M = 30.0
SOURCE_RAW = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

OUT_TDM = Path(f"C:/Condor2/Landscapes/{g.LANDSCAPE_NAME}/{g.LANDSCAPE_NAME}.tdm")


def slope(dem: np.ndarray) -> np.ndarray:
    """Compute approximate terrain slope in degrees on the overview grid."""
    dzdx = np.abs(np.gradient(dem, axis=1))
    dzdy = np.abs(np.gradient(dem, axis=0))
    slope_deg = np.degrees(np.arctan(np.sqrt(dzdx**2 + dzdy**2) / SLOPE_CELL_M))
    return slope_deg


def _downsample(dem_full: np.ndarray) -> np.ndarray:
    """Average-pool the full 30 m DEM down to the WIDTHxHEIGHT overview grid.

    The DEM is patches*192+1 per side; dropping the final row/column gives an
    exact patches*192 = (patches x 64) x 3 grid, so each overview pixel is the
    mean of a 3x3 block of 30 m samples (deterministic, anti-aliased).
    """
    h3 = HEIGHT * 3
    w3 = WIDTH * 3
    cropped = dem_full[:h3, :w3]
    return cropped.reshape(HEIGHT, 3, WIDTH, 3).mean(axis=(1, 3))


def main():
    # Load full-resolution DEM (int16 LE, GDAL top-left order) and resample.
    dem_full = np.fromfile(SOURCE_RAW, dtype=np.int16)
    if dem_full.size != DEM_WIDTH * DEM_HEIGHT:
        raise SystemExit(
            f"DEM {SOURCE_RAW} has {dem_full.size} samples, expected "
            f"{DEM_WIDTH*DEM_HEIGHT} ({DEM_WIDTH}x{DEM_HEIGHT})")
    dem_full = np.where(dem_full < 0, 0, dem_full).reshape(DEM_HEIGHT, DEM_WIDTH)
    dem_full = dem_full.astype(np.float32)

    dem = _downsample(dem_full)

    s = slope(dem)

    # Thermal strength: high elevation, low slope
    elev_norm = (dem - dem.min()) / (dem.max() - dem.min() + 1e-6)
    slope_norm = s / (s.max() + 1e-6)
    thermal = 0.6 * elev_norm + 0.4 * (1.0 - slope_norm)

    # Reduce thermals over very flat low terrain (valley bottoms likely rivers/fields)
    low_flat = (dem < 300) & (s < 2.0)
    thermal[low_flat] *= 0.5

    # Scale to 0-255
    thermal = (thermal * 255).clip(0, 255).astype(np.uint8)

    OUT_TDM.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_TDM, "wb") as f:
        f.write(struct.pack("<ii", WIDTH, HEIGHT))
        f.write(thermal.tobytes())

    print(f"Landscape: {g.LANDSCAPE_NAME}  ({WIDTH}x{HEIGHT} overview)")
    print(f"Source DEM: {SOURCE_RAW.name}")
    print(f"Wrote {OUT_TDM}")
    print(f"  size: {OUT_TDM.stat().st_size} bytes (expected {8 + WIDTH*HEIGHT})")
    print(f"  thermal range: {thermal.min()}-{thermal.max()}")


if __name__ == "__main__":
    main()
