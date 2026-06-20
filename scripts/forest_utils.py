#!/usr/bin/env python3
"""
Helpers for the improved MacedoniaSkopje forest map.
"""

import json
import math
from pathlib import Path

import numpy as np
import pyproj
from scipy import ndimage
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

from condor_grid import (
    ULXMAP,
    ULYMAP,
    XDIM,
    WIDTH,
    HEIGHT,
    PATCHES_X,
    PATCHES_Y,
    PATCH_MASK_SIZE,
    patch_bounds_utm,
    UTM_CRS,
    WGS84_CRS,
)


# -----------------------------------------------------------------------------
# DEM helpers
# -----------------------------------------------------------------------------

def load_dem(path: Path):
    """Load the flattened 2305x2305 int16 DEM (top-left origin)."""
    dem = np.fromfile(path, dtype=np.int16).reshape(HEIGHT, WIDTH)
    return dem


def utm_to_dem_index(e, n):
    """Return (row, col) float indices into the top-left DEM for a UTM point."""
    col = (e - ULXMAP) / XDIM
    row = (ULYMAP - n) / XDIM
    return row, col


def patch_elevations(dem, patch_col, patch_row, size=PATCH_MASK_SIZE):
    """Return a ``size x size`` array of elevations for a Condor patch."""
    bounds = patch_bounds_utm(patch_col, patch_row)
    min_e, min_n, max_e, max_n = bounds

    # Pixel centres in UTM
    xs = np.linspace(min_e, max_e, size, endpoint=False) + (max_e - min_e) / (2 * size)
    ys = np.linspace(max_n, min_n, size, endpoint=False) - (max_n - min_n) / (2 * size)
    # ys goes from north to south
    ee, nn = np.meshgrid(xs, ys)

    rows, cols = utm_to_dem_index(ee, nn)
    # map_coordinates wants (row, col)
    coords = np.stack([rows, cols], axis=0)
    return ndimage.map_coordinates(dem, coords, order=1, mode="nearest")


def patch_aspect(dem, patch_col, patch_row, size=PATCH_MASK_SIZE):
    """Return aspect in degrees (0=N, 90=E, 180=S, 270=W) for the patch."""
    # gradient on the full DEM; rows increase southwards, cols eastwards.
    dz_dy, dz_dx = np.gradient(dem.astype(np.float32), XDIM)
    aspect = np.degrees(np.arctan2(-dz_dx, dz_dy))
    aspect = np.where(aspect < 0, 90.0 - aspect, 360.0 - aspect + 90.0)
    aspect = np.mod(aspect, 360.0)

    bounds = patch_bounds_utm(patch_col, patch_row)
    min_e, min_n, max_e, max_n = bounds
    xs = np.linspace(min_e, max_e, size, endpoint=False) + (max_e - min_e) / (2 * size)
    ys = np.linspace(max_n, min_n, size, endpoint=False) - (max_n - min_n) / (2 * size)
    ee, nn = np.meshgrid(xs, ys)
    rows, cols = utm_to_dem_index(ee, nn)
    coords = np.stack([rows, cols], axis=0)
    return ndimage.map_coordinates(aspect, coords, order=1, mode="nearest")


# -----------------------------------------------------------------------------
# OSM helpers
# -----------------------------------------------------------------------------

def load_geojson_features(path: Path, transformer=None):
    """Load a GeoJSON and return a list of (shapely_geometry, properties)."""
    with open(path, "r", encoding="utf-8") as f:
        geojson = json.load(f)

    geoms = []
    for feat in geojson.get("features", []):
        geom = shape(feat.get("geometry"))
        if geom is None or geom.is_empty:
            continue
        if transformer is not None:
            try:
                geom = shp_transform(lambda x, y, z=None: transformer.transform(x, y), geom)
            except Exception:
                continue
            if not geom.is_valid:
                continue
        geoms.append((geom, feat.get("properties", {})))
    return geoms


# -----------------------------------------------------------------------------
# Road / railway buffering
# -----------------------------------------------------------------------------

# Road widths in metres (full carriageway, not per lane).
# Wider buffers ensure tree clearance, especially for mountain roads
# where OSM centre-lines may be offset from the actual road edge.
_ROAD_WIDTHS = {
    "motorway": 28.0,
    "motorway_link": 14.0,
    "trunk": 22.0,
    "trunk_link": 12.0,
    "primary": 16.0,
    "primary_link": 10.0,
    "secondary": 12.0,
    "secondary_link": 8.0,
    "tertiary": 10.0,
    "tertiary_link": 8.0,
    "unclassified": 8.0,
    "residential": 8.0,
    "living_street": 7.0,
    "pedestrian": 6.0,
    "service": 6.0,
    "track": 5.0,
    "path": 3.0,
    "cycleway": 3.0,
    "road": 7.0,
}

# Waterway buffer widths in metres (half-width from centre-line).
_WATERWAY_WIDTHS = {
    "river": 20.0,
    "canal": 12.0,
    "stream": 4.0,
    "drain": 3.0,
    "ditch": 2.0,
}


def _parse_width(tags):
    w = tags.get("width")
    if w is None:
        return None
    try:
        return float(str(w).replace(" m", "").replace("m", ""))
    except Exception:
        return None


def buffer_roads(features):
    """Take an iterable of (geom, props) road features and return buffered polygons.

    Buffer radius = half the carriageway width + a 3 m tree-clearance margin.
    For LineString geometries the buffer is applied directly.
    For Polygon geometries (closed-way area roads) a small fixed buffer is added.
    """
    out = []
    for geom, props in features:
        if geom.is_empty:
            continue
        highway = props.get("highway", "road")
        width = _parse_width(props)
        if width is None:
            width = _ROAD_WIDTHS.get(highway, 7.0)
        # buffer radius = half width + 3 m safety margin for tree clearance
        try:
            out.append(geom.buffer(width * 0.5 + 3.0))
        except Exception:
            continue
    return out


def buffer_railways(features, radius=10.0):
    """Buffer railway geometries (typically LineStrings) by *radius* metres."""
    out = []
    for geom, props in features:
        if geom.is_empty:
            continue
        try:
            out.append(geom.buffer(radius))
        except Exception:
            continue
    return out


def buffer_buildings(features, radius=5.0):
    """Add a clearance buffer around building footprints."""
    out = []
    for geom, props in features:
        if geom.is_empty:
            continue
        try:
            out.append(geom.buffer(radius))
        except Exception:
            continue
    return out


def buffer_waterways(features):
    """Buffer waterway LineStrings by type-appropriate widths."""
    out = []
    for geom, props in features:
        if geom.is_empty:
            continue
        ww_type = props.get("waterway", "stream")
        width = _WATERWAY_WIDTHS.get(ww_type, 4.0)
        try:
            out.append(geom.buffer(width))
        except Exception:
            continue
    return out
