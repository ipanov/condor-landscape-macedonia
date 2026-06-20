#!/usr/bin/env python3
"""
Download OpenStreetMap water and forest/wood features for the MacedoniaSkopje
landscape bounding box and save them as GeoJSON in .sandbox/osm/.
"""

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


def main():
    south, west, north, east = landscape_wgs84_bbox()
    print(f"Landscape WGS84 bbox: south={south:.6f}, west={west:.6f}, north={north:.6f}, east={east:.6f}")

    for kind in ("water", "forest"):
        print(f"Downloading OSM {kind} features...")
        osm_data = query_overpass(kind, south, west, north, east)
        geojson = osm_json_to_geojson(osm_data)
        out_path = OUT_DIR / f"{kind}.geojson"
        save_geojson(geojson, out_path)


if __name__ == "__main__":
    main()
