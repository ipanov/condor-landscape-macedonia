#!/usr/bin/env python3
"""
Generate Condor 2 thermal map (.tdm) directly from the flattened DEM.

Format:
  int32 width
  int32 height
  width x height uint8 values (0 = no thermals, 255 = strongest)

A simple but sensible model:
  - Higher elevations and gentler slopes get stronger thermals.
  - Water / flat valleys get weaker thermals.
  - Values are stretched to the 0-255 range.
"""

import struct
import numpy as np
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_RAW = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_2305_flat.raw"
OUT_TDM = Path("C:/Condor2/Landscapes/MacedoniaSkopje/MacedoniaSkopje.tdm")

# Thermal map dimensions must match the .trn overview (768x768 at 90 m).
# The source DEM is 2305x2305 at 30 m; we resample by a factor of 3.
WIDTH = 768
HEIGHT = 768


def slope(dem: np.ndarray) -> np.ndarray:
    """Compute approximate terrain slope in degrees."""
    dzdx = np.abs(np.gradient(dem, axis=1))
    dzdy = np.abs(np.gradient(dem, axis=0))
    # pixel size ~30 m, convert to degrees
    slope_deg = np.degrees(np.arctan(np.sqrt(dzdx**2 + dzdy**2) / 30.0))
    return slope_deg


def main():
    # Load full-resolution DEM and resample to .trn dimensions
    dem_full = np.fromfile(SOURCE_RAW, dtype=np.int16).reshape(2305, 2305)
    dem_full = np.where(dem_full < 0, 0, dem_full).astype(np.float32)

    # Downsample to 768x768 (matching .trn). 2305 = 768*3 + 1, so we drop the
    # last row/column and average each 3x3 block.
    dem = dem_full[:2304, :2304].reshape(768, 3, 768, 3).mean(axis=(1, 3))

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

    print(f"Wrote {OUT_TDM}")
    print(f"  size: {OUT_TDM.stat().st_size} bytes")
    print(f"  thermal range: {thermal.min()}-{thermal.max()}")


if __name__ == "__main__":
    main()
