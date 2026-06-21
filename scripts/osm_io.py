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
from shapely.geometry import (
    shape, mapping, Polygon, MultiPolygon, LinearRing, Point, LineString,
)

# Primary + mirror Overpass endpoints. Big tiled North-Macedonia runs hammer one
# server, so rotate mirrors on failure to spread load and dodge rate-limits.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "condor-landscape/1.0"


def _water_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["natural"="water"];
  way["waterway"="riverbank"];
  way["waterway"="dock"];
  way["landuse"="reservoir"];
  relation["natural"="water"];
  relation["waterway"="riverbank"];
  relation["waterway"="dock"];
  relation["landuse"="reservoir"];
);
out geom;"""


def _forest_query(south, west, north, east):
    # Keep ALL tags (out geom) so leaf_type / leaf_cycle survive for species.
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["landuse"="forest"];
  way["natural"="wood"];
  relation["landuse"="forest"];
  relation["natural"="wood"];
);
out geom;"""


def _waterways_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["waterway"~"^(river|stream|canal|drain|ditch)$"];
);
out geom;"""


def _roads_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["highway"~"^(motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|unclassified|residential|living_street|pedestrian|track|path|cycleway|service|road)$"];
);
out geom;"""


def _railways_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["railway"~"^(rail|light_rail|narrow_gauge|tram|subway)$"];
);
out geom;"""


def _buildings_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["building"];
  relation["building"];
);
out geom;"""


def _runways_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["aeroway"~"^(runway|taxiway|apron)$"];
  relation["aeroway"~"^(runway|taxiway|apron)$"];
);
out geom;"""


def _settlements_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["landuse"~"^(residential|commercial|industrial|retail)$"];
  relation["landuse"~"^(residential|commercial|industrial|retail)$"];
);
out geom;"""


def _aerodromes_query(south, west, north, east):
    # aeroway=aerodrome polygons + nodes, so we can discover airfields and read
    # name/icao/ele tags for the .apt builder.
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  node["aeroway"="aerodrome"];
  way["aeroway"="aerodrome"];
  relation["aeroway"="aerodrome"];
);
out geom;"""


_QUERY_BUILDERS = {
    "water": _water_query,
    "forest": _forest_query,
    "waterways": _waterways_query,
    "roads": _roads_query,
    "railways": _railways_query,
    "buildings": _buildings_query,
    "runways": _runways_query,
    "settlements": _settlements_query,
    "aerodromes": _aerodromes_query,
}


def build_query(kind, south, west, north, east):
    """Return the Overpass QL string for a known *kind* over a WGS84 bbox."""
    builder = _QUERY_BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"Unknown OSM feature kind: {kind}")
    return builder(south, west, north, east)


def query_overpass(kind, south, west, north, east, retries=6, endpoint_offset=0):
    """Query Overpass for a known *kind* of feature inside a WGS84 bbox.

    Rotates across :data:`OVERPASS_MIRRORS` on failure so large tiled
    North-Macedonia runs survive a single server rate-limiting/timing out.
    ``endpoint_offset`` staggers which mirror is tried first, so concurrent
    layer fetchers do not all hammer the same server on the first request.
    A 429 backs off longer (the server is explicitly rate-limiting).
    """
    query = build_query(kind, south, west, north, east)
    last_exc = None
    for attempt in range(retries):
        endpoint = OVERPASS_MIRRORS[(attempt + endpoint_offset) % len(OVERPASS_MIRRORS)]
        url = endpoint + "?data=" + urllib.parse.quote(query)
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=600)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            host = endpoint.split("//")[1].split("/")[0]
            is_429 = "429" in str(exc)
            print(f"Overpass {kind} @ {host} failed "
                  f"(attempt {attempt + 1}/{retries}{' [429]' if is_429 else ''}): {exc}")
            if attempt < retries - 1:
                # Most tiles fetch in <1s; a 429 just means "use another mirror /
                # wait briefly", not a multi-minute outage.  Keep backoff modest
                # so a transient throttle does not stall the whole pool, but grow
                # it a little each retry. 429 waits slightly longer than a timeout.
                time.sleep(min(30, (5 if is_429 else 3) * (attempt + 1)))
    raise last_exc


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


def osm_json_to_geojson_lines(osm_data):
    """Convert Overpass JSON ways to a GeoJSON FeatureCollection of LineStrings.

    Used for road/railway/waterway centre-lines (the forest pipeline buffers
    these line layers by class width).  Closed ways are kept as lines too -- the
    buffer treats them the same.
    """
    features = []
    for el in osm_data.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry")
        if not geom or len(geom) < 2:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in geom]
        try:
            line = LineString(coords)
        except Exception:
            continue
        if line.is_empty:
            continue
        features.append({
            "type": "Feature",
            "properties": el.get("tags", {}),
            "geometry": mapping(line),
        })
    return {"type": "FeatureCollection", "features": features}


def osm_json_to_points(osm_data):
    """Return a list of dicts ``{lat, lon, tags}`` for node + way/relation
    aerodromes (way/relation centroids), for airport discovery."""
    pts = []
    for el in osm_data.get("elements", []):
        tags = el.get("tags", {})
        if el.get("type") == "node":
            if "lat" in el and "lon" in el:
                pts.append({"lat": el["lat"], "lon": el["lon"], "tags": tags})
        elif el.get("type") in ("way", "relation"):
            geom = el.get("geometry")
            if geom:
                lats = [p["lat"] for p in geom if "lat" in p]
                lons = [p["lon"] for p in geom if "lon" in p]
                if lats and lons:
                    pts.append({"lat": sum(lats) / len(lats),
                                "lon": sum(lons) / len(lons), "tags": tags})
            elif "center" in el:
                c = el["center"]
                pts.append({"lat": c["lat"], "lon": c["lon"], "tags": tags})
    return pts


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
