"""
OSM data helpers:
  - download Overpass data for water / forest features
  - convert Overpass JSON to GeoJSON FeatureCollection
  - load GeoJSON and project features to UTM
"""

import json
import time
import urllib.parse
from pathlib import Path

import requests
import shapely
from shapely.geometry import shape, mapping, Polygon, MultiPolygon, LinearRing, Point

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "condor-landscape/1.0"


def _water_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["natural"="water"];
  way["waterway"="riverbank"];
  way["waterway"="dock"];
  relation["natural"="water"];
  relation["waterway"="riverbank"];
  relation["waterway"="dock"];
);
out geom;"""


def _forest_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["landuse"="forest"];
  way["natural"="wood"];
  relation["landuse"="forest"];
  relation["natural"="wood"];
);
out geom;"""


def query_overpass(kind, south, west, north, east, retries=3):
    """Query Overpass for 'water' or 'forest' features inside a WGS84 bbox."""
    if kind == "water":
        query = _water_query(south, west, north, east)
    elif kind == "forest":
        query = _forest_query(south, west, north, east)
    else:
        raise ValueError(f"Unknown OSM feature kind: {kind}")

    url = OVERPASS_URL + "?data=" + urllib.parse.quote(query)
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=600,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            print(f"Overpass {kind} query failed (attempt {attempt + 1}/{retries}): {exc}")
            if attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
            else:
                raise


def _ring_from_geom(geom):
    """Build a shapely LinearRing from an Overpass geometry list."""
    if not geom:
        return None
    coords = [(pt["lon"], pt["lat"]) for pt in geom]
    if len(coords) < 3:
        return None
    # Overpass closed rings already repeat the first point
    try:
        return LinearRing(coords)
    except Exception:
        return None


def _ring_to_polygon(ring):
    """Convert a single ring to a Polygon (no holes)."""
    try:
        return Polygon(ring)
    except Exception:
        return None


def _build_multipolygon(outer_rings, inner_rings):
    """Build Polygon/MultiPolygon from lists of shapely rings."""
    polys = []
    for outer in outer_rings:
        holes = []
        outer_poly = Polygon(outer)
        # assign holes to the outer ring that contains them
        still_inner = []
        for inner in inner_rings:
            if Point(inner.coords[0]).within(outer_poly):
                holes.append(inner)
            else:
                still_inner.append(inner)
        inner_rings = still_inner
        try:
            polys.append(Polygon(outer, holes=holes))
        except Exception:
            pass

    if not polys:
        return None
    if len(polys) == 1:
        return polys[0]
    return MultiPolygon(polys)


def osm_json_to_geojson(osm_data):
    """Convert Overpass JSON (with `out geom;`) to a GeoJSON FeatureCollection."""
    features = []
    elements = osm_data.get("elements", [])

    # Track way ids that are relation members so we can avoid double counting
    relation_member_ids = set()
    for el in elements:
        if el["type"] == "relation":
            for member in el.get("members", []):
                if member.get("type") == "way":
                    relation_member_ids.add(member.get("ref"))

    # First pass: standalone ways
    for el in elements:
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        geom = el.get("geometry")
        if not geom:
            continue
        ring = _ring_from_geom(geom)
        if ring is None:
            continue
        poly = _ring_to_polygon(ring)
        if poly is None or poly.is_empty:
            continue

        # Skip ways that are already part of a relation we will process separately
        if el.get("id") in relation_member_ids:
            continue

        features.append({
            "type": "Feature",
            "properties": tags,
            "geometry": mapping(poly),
        })

    # Second pass: relations (multipolygons)
    for el in elements:
        if el["type"] != "relation":
            continue
        tags = el.get("tags", {})
        members = el.get("members", [])
        outer_rings = []
        inner_rings = []
        for member in members:
            if member.get("type") != "way":
                continue
            role = member.get("role", "")
            ring = _ring_from_geom(member.get("geometry", []))
            if ring is None:
                continue
            if role == "outer":
                outer_rings.append(ring)
            elif role == "inner":
                inner_rings.append(ring)
            else:
                # Treat unknown roles as outer if they look like a polygon
                outer_rings.append(ring)

        if not outer_rings:
            continue
        mp = _build_multipolygon(outer_rings, inner_rings)
        if mp is None or mp.is_empty:
            continue
        features.append({
            "type": "Feature",
            "properties": tags,
            "geometry": mapping(mp),
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def save_geojson(geojson, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(geojson, f)
    print(f"Saved {path} ({len(geojson['features'])} features)")


def load_geojson(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def project_geojson(geojson, transformer):
    """Return list of shapely geometries projected with the given pyproj Transformer."""
    from shapely.ops import transform as shp_transform

    def _coord_transform(x, y, z=None):
        return transformer.transform(x, y)

    geoms = []
    for feat in geojson.get("features", []):
        geom = shape(feat.get("geometry"))
        if not geom or geom.is_empty:
            continue
        try:
            geom_utm = shp_transform(_coord_transform, geom)
            if geom_utm.is_valid:
                geoms.append(geom_utm)
        except Exception as exc:
            # Skip malformed geometries
            continue
    return geoms
