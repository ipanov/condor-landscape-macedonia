"""
Rasterize shapely geometries into uint8 masks using Pillow.
"""

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import box
from shapely.strtree import STRtree
from shapely.prepared import prep


def _coords_to_pixels(coords, bounds, width, height):
    """Convert a sequence of (x, y) UTM coords to top-left pixel coordinates."""
    min_x, min_y, max_x, max_y = bounds
    sx = width / (max_x - min_x)
    sy = height / (max_y - min_y)
    pts = []
    for x, y in coords:
        px = (x - min_x) * sx
        py = (max_y - y) * sy
        pts.append((px, py))
    return pts


def _draw_polygon(draw, polygon, bounds, width, height, fill):
    """Draw a shapely Polygon (with holes) onto a PIL ImageDraw object."""
    if polygon.is_empty:
        return
    exterior = list(polygon.exterior.coords)
    if len(exterior) < 3:
        return
    pts = _coords_to_pixels(exterior, bounds, width, height)
    draw.polygon(pts, fill=fill)
    for interior in polygon.interiors:
        coords = list(interior.coords)
        if len(coords) < 3:
            continue
        # Holes are drawn with the inverse fill (background)
        # The caller passes the background value for holes.
        pass


def _iter_polygons(geom):
    """Yield individual shapely Polygons from a Polygon or MultiPolygon."""
    from shapely.geometry import MultiPolygon, GeometryCollection
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type == "MultiPolygon":
        for p in geom.geoms:
            yield p
    elif geom.geom_type == "GeometryCollection":
        for part in geom.geoms:
            if part.geom_type == "Polygon":
                yield part
            elif part.geom_type == "MultiPolygon":
                for p in part.geoms:
                    yield p


def rasterize_mask(geoms, bounds, width, height, foreground=1, background=0,
                   tree=None, prepared_box=None):
    """
    Rasterize a list of shapely geometries into a top-left oriented uint8 mask.

    Parameters
    ----------
    geoms : list of shapely geometries (projected to the same CRS as bounds)
    bounds : (min_x, min_y, max_x, max_y)
    width, height : output raster dimensions
    foreground : uint8 value to write inside geometries
    background : uint8 value outside geometries
    tree : optional pre-built shapely STRtree over ``geoms``
    prepared_box : optional prepared shapely polygon for ``bounds``

    Returns
    -------
    numpy.ndarray of shape (height, width), dtype uint8
    """
    img = Image.new("L", (width, height), background)
    draw = ImageDraw.Draw(img)

    tile_box = box(*bounds)
    if prepared_box is None:
        prepared_box = prep(tile_box)

    # Spatial index for fast lookup
    if tree is None:
        tree = STRtree(geoms)
    candidates = tree.query(tile_box)

    for idx in candidates:
        geom = geoms[idx]
        if not prepared_box.intersects(geom):
            continue
        clipped = geom.intersection(tile_box)
        for poly in _iter_polygons(clipped):
            if poly.is_empty:
                continue
            # Exterior -> foreground
            pts = _coords_to_pixels(poly.exterior.coords, bounds, width, height)
            if len(pts) >= 3:
                draw.polygon(pts, fill=foreground)
            # Holes -> background
            for interior in poly.interiors:
                pts = _coords_to_pixels(interior.coords, bounds, width, height)
                if len(pts) >= 3:
                    draw.polygon(pts, fill=background)

    return np.array(img, dtype=np.uint8)
