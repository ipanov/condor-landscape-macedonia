#!/usr/bin/env python3
"""
Download OpenStreetMap features that must be tree-free in the MacedoniaSkopje
landscape and save them as GeoJSON in .sandbox/osm/.

Layers:
  - water          (already downloaded by download_osm_features.py, kept here
                    for convenience)
  - roads          (highways, buffered by inferred width)
  - railways       (rail lines, buffered)
  - buildings      (polygons)
  - runways        (aeroway=runway / taxiway / apron)
"""

import json
import pyproj
from pathlib import Path

from condor_grid import ULXMAP, ULYMAP, WIDTH, HEIGHT, XDIM, UTM_CRS, WGS84_CRS
from osm_io import query_overpass, osm_json_to_geojson, save_geojson

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / ".sandbox" / "osm"


def landscape_wgs84_bbox():
    """Return (south, west, north, east) for the landscape extent."""
    br_easting = ULXMAP + (WIDTH - 1) * XDIM
    br_northing = ULYMAP - (HEIGHT - 1) * XDIM

    transformer = pyproj.Transformer.from_crs(UTM_CRS, WGS84_CRS, always_xy=True)
    sw_lon, sw_lat = transformer.transform(ULXMAP, br_northing)
    ne_lon, ne_lat = transformer.transform(br_easting, ULYMAP)

    south = min(sw_lat, ne_lat)
    north = max(sw_lat, ne_lat)
    west = min(sw_lon, ne_lon)
    east = max(sw_lon, ne_lon)
    return south, west, north, east


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


def _road_query(south, west, north, east):
    # All roads that should be tree-free.  Minor tracks/paths are included
    # because Condor should not plant trees on them either.
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|pedestrian|track|path|cycleway|service|road)$"];
);
out geom;"""


def _railway_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["railway"];
);
out geom;"""


def _building_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["building"];
  relation["building"];
);
out geom;"""


def _runway_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["aeroway"~"^(runway|taxiway|apron)$"];
  relation["aeroway"~"^(runway|taxiway|apron)$"];
);
out geom;"""


def _kind_query(kind, south, west, north, east):
    if kind == "water":
        return _water_query(south, west, north, east)
    if kind == "roads":
        return _road_query(south, west, north, east)
    if kind == "railways":
        return _railway_query(south, west, north, east)
    if kind == "buildings":
        return _building_query(south, west, north, east)
    if kind == "runways":
        return _runway_query(south, west, north, east)
    raise ValueError(kind)


def download(kind):
    south, west, north, east = landscape_wgs84_bbox()
    print(f"Downloading OSM {kind} features...")
    osm_data = query_overpass_custom(kind, south, west, north, east)
    geojson = osm_json_to_geojson(osm_data)
    out_path = OUT_DIR / f"{kind}.geojson"
    save_geojson(geojson, out_path)
    return out_path


def query_overpass_custom(kind, south, west, north, east, retries=3):
    """Custom query dispatcher that bypasses osm_io's built-in kind handling."""
    import time
    import urllib.parse
    import requests

    query = _kind_query(kind, south, west, north, east)
    url = "https://overpass-api.de/api/interpreter?data=" + urllib.parse.quote(query)
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "condor-landscape/1.0"},
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


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    south, west, north, east = landscape_wgs84_bbox()
    print(f"Landscape WGS84 bbox: south={south:.6f}, west={west:.6f}, north={north:.6f}, east={east:.6f}")

    # Re-download water so all exclusion layers live in one place.
    for kind in ("water", "roads", "railways", "buildings", "runways"):
        download(kind)


if __name__ == "__main__":
    main()
