"""
Common Condor 2 grid definitions for the MacedoniaSkopje landscape.

Condor uses a bottom-right (south-east) origin for tile/patch naming:
  - tile/patch column 0 is the east-most column
  - tile/patch row 0 is the south-most row
"""

from pathlib import Path
import pyproj

# Landscape calibration (UTM 34N, EPSG:32634)
ULXMAP = 506880.0
ULYMAP = 4700160.0
# EXACTLY 30 m. The DEM was previously 29.9869848156182 m/px, which drifted the
# texture grid ~30 m from the mesh grid at the SE corner. The mesh (.trn/.tr3)
# is built on exact 30 m, so textures must use 30 m too. NOTE: textures already
# installed predate this fix (built on 29.987 m) — rebuild them via
# build_patch_textures.py to get pixel-perfect mesh/texture registration.
XDIM = 30.0
WIDTH = 2305
HEIGHT = 2305

UTM_CRS = pyproj.CRS.from_epsg(32634)
WGS84_CRS = pyproj.CRS.from_epsg(4326)

# Landscape extents in UTM
BR_EASTING = ULXMAP + (WIDTH - 1) * XDIM
BR_NORTHING = ULYMAP - (HEIGHT - 1) * XDIM

# Tile / patch geometry
TILE_SIZE_M = 23040.0  # 23.04 km
PATCH_SIZE_M = 5760.0  # 5.76 km
TILES_X = 3
TILES_Y = 3
PATCHES_X = 12
PATCHES_Y = 12

# Texture / mask resolutions
TILE_MASK_SIZE = 8192
PATCH_MASK_SIZE = 512


def project_to_utm(geoms):
    """Project a list of WGS84 shapely geometries to UTM 34N."""
    transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)

    def _transform(x, y, z=None):
        return transformer.transform(x, y)

    from shapely.ops import transform as shp_transform
    return [shp_transform(_transform, g) for g in geoms if g and g.is_valid]


def tile_bounds_utm(tile_col, tile_row):
    """Return (min_e, min_n, max_e, max_n) for a Condor tile."""
    e_max = BR_EASTING - tile_col * TILE_SIZE_M
    e_min = e_max - TILE_SIZE_M
    n_min = BR_NORTHING + tile_row * TILE_SIZE_M
    n_max = n_min + TILE_SIZE_M
    return e_min, n_min, e_max, n_max


def patch_bounds_utm(patch_col, patch_row):
    """Return (min_e, min_n, max_e, max_n) for a Condor patch."""
    e_max = BR_EASTING - patch_col * PATCH_SIZE_M
    e_min = e_max - PATCH_SIZE_M
    n_min = BR_NORTHING + patch_row * PATCH_SIZE_M
    n_max = n_min + PATCH_SIZE_M
    return e_min, n_min, e_max, n_max


def utm_to_pixel(e, n, bounds, width, height):
    """Map UTM coordinate to top-left oriented pixel coordinate."""
    min_e, min_n, max_e, max_n = bounds
    px = (e - min_e) / (max_e - min_e) * width
    py = (max_n - n) / (max_n - min_n) * height
    return px, py
