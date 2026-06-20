#!/usr/bin/env python3
"""
Generate 8192x8192 uint8 water-alpha masks for each Condor tile.

Output:
  - .sandbox/textures/water_masks/aCCRR.bmp
    0  = water
    255 = land

The BMP is saved in standard Windows BMP orientation (PIL handles the
bottom-to-top row order automatically) so that the in-memory top-left
origin of the image corresponds to the north-west corner of the tile.
"""

import pyproj
from pathlib import Path

from condor_grid import (
    TILES_X, TILES_Y, TILE_MASK_SIZE,
    tile_bounds_utm, UTM_CRS, WGS84_CRS,
)
from osm_io import load_geojson, project_geojson
from rasterize import rasterize_mask
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OSM_PATH = PROJECT_ROOT / ".sandbox" / "osm" / "water.geojson"
OUT_DIR = PROJECT_ROOT / ".sandbox" / "textures" / "water_masks"


def main():
    if not OSM_PATH.exists():
        raise FileNotFoundError(
            f"{OSM_PATH} not found. Run download_osm_features.py first."
        )

    geojson = load_geojson(OSM_PATH)
    transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)
    geoms = project_geojson(geojson, transformer)
    print(f"Loaded {len(geoms)} projected water geometries")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for tile_row in range(TILES_Y):
        for tile_col in range(TILES_X):
            bounds = tile_bounds_utm(tile_col, tile_row)
            mask = rasterize_mask(
                geoms,
                bounds,
                TILE_MASK_SIZE,
                TILE_MASK_SIZE,
                foreground=0,    # water
                background=255,  # land
            )
            filename = f"a{tile_col:02d}{tile_row:02d}.bmp"
            out_path = OUT_DIR / filename
            Image.fromarray(mask, mode="L").save(out_path)
            print(f"Wrote {out_path} ({mask.shape[1]}x{mask.shape[0]}), "
                  f"water pixels: {int((mask == 0).sum()):,}")


if __name__ == "__main__":
    main()
