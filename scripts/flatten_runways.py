#!/usr/bin/env python3
"""
flatten_runways.py

Reads data/airports.json and flattens each runway polygon in the
2304x2304 signed 16-bit little-endian raw heightmap.

Projection:
    - WGS-84 lat/lon  ->  UTM zone 34N (EPSG:32634)
    - UTM (E, N)      ->  pixel indices (top-left origin)
        px = (E - ULXMAP) / XDIM
        py = (ULYMAP - N) / YDIM

The heightmap is assumed to use metres directly (orthometric / MSL). No geoid
offset is applied unless evidence of an ellipsoidal DEM is found.

Output:
    sources/dem/macedonia_skopje_dem_utm30m_flat.raw
    tools/flatten_runways_summary.txt
"""

import json
import math
import struct
from pathlib import Path

import numpy as np
import pyproj
from shapely.geometry import Point, Polygon
from shapely.prepared import prep

# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TOOLS_DIR = PROJECT_ROOT / "tools"
DEM_DIR = PROJECT_ROOT / "sources" / "dem"

AIRPORTS_JSON = DATA_DIR / "airports.json"
INPUT_BIL = DEM_DIR / "macedonia_skopje_dem_2305.bil"
OUTPUT_RAW = DEM_DIR / "macedonia_skopje_dem_2305_flat.raw"
SUMMARY_TXT = TOOLS_DIR / "flatten_runways_summary.txt"

# ---------------------------------------------------------------------------
# Heightmap geometry (from sources/dem/macedonia_skopje_dem_2305.hdr)
# 2305 x 2305 samples are required so that 12 x 12 Condor patches can share
# vertices (each patch is 193 x 193 = 192 intervals).
# ---------------------------------------------------------------------------
NROWS = 2305
NCOLS = 2305
NBANDS = 1
XDIM = 29.9869848156182  # metres per pixel, easting
YDIM = 29.9869848156182  # metres per pixel, northing
ULXMAP = 506880.0        # easting of the centre of the top-left pixel
ULYMAP = 4700160.0       # northing of the centre of the top-left pixel
NODATA = -32768

# ---------------------------------------------------------------------------
# Coordinate transforms
# ---------------------------------------------------------------------------
# WGS-84 (EPSG:4326) -> UTM 34N (EPSG:32634)
_utm_crs = pyproj.CRS.from_epsg(32634)
_wgs84_crs = pyproj.CRS.from_epsg(4326)
_transformer = pyproj.Transformer.from_crs(_wgs84_crs, _utm_crs, always_xy=True)


def wgs84_to_utm(lon: float, lat: float) -> tuple[float, float]:
    """Return (easting, northing) in UTM 34N."""
    return _transformer.transform(lon, lat)


def pixel_to_utm(px: float, py: float) -> tuple[float, float]:
    """Return the UTM coordinate of the centre of pixel (px, py)."""
    easting = ULXMAP + px * XDIM
    northing = ULYMAP - py * YDIM
    return easting, northing


def utm_to_pixel(easting: float, northing: float) -> tuple[float, float]:
    """Return floating-point pixel coordinates for a UTM position."""
    px = (easting - ULXMAP) / XDIM
    py = (ULYMAP - northing) / YDIM
    return px, py


def runway_polygon(easting: float, northing: float, length_m: float,
                   width_m: float, true_heading_deg: float) -> Polygon:
    """
    Build a shapely Polygon for a runway rectangle.

    true_heading_deg is the clockwise bearing from true north for the
    low-numbered runway direction (e.g. 165 for runway 16).
    """
    theta = math.radians(true_heading_deg)

    # Unit vector along the runway centreline (E, N components)
    sin_t = math.sin(theta)
    cos_t = math.cos(theta)

    # Half-length and half-width vectors in UTM (E, N) space
    hl = length_m / 2.0
    hw = width_m / 2.0

    # Centreline half vector
    de_c = hl * sin_t
    dn_c = hl * cos_t

    # Perpendicular half vector (rotated +90 deg)
    de_p = hw * cos_t
    dn_p = -hw * sin_t

    corners = [
        (easting - de_c - de_p, northing - dn_c - dn_p),
        (easting + de_c - de_p, northing + dn_c - dn_p),
        (easting + de_c + de_p, northing + dn_c + dn_p),
        (easting - de_c + de_p, northing - dn_c + dn_p),
    ]
    return Polygon(corners)


def flatten_runways() -> dict:
    """Load the heightmap and flatten every runway. Return summary stats."""

    if not AIRPORTS_JSON.exists():
        raise FileNotFoundError(f"Airports file not found: {AIRPORTS_JSON}")
    if not INPUT_BIL.exists():
        raise FileNotFoundError(f"Input DEM not found: {INPUT_BIL}")

    with open(AIRPORTS_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    airports = data.get("airports", [])

    # Load signed 16-bit little-endian BIL heightmap
    dem = np.fromfile(INPUT_BIL, dtype=np.int16).reshape(NROWS, NCOLS)
    original = dem.copy()

    summary = {
        "input_file": str(INPUT_BIL),
        "output_file": str(OUTPUT_RAW),
        "input_shape": dem.shape,
        "input_dtype": str(dem.dtype),
        "airports": []
    }

    for airport in airports:
        icao = airport["icao"]
        elevation_m = airport["elevation_m"]
        elevation_i16 = int(round(elevation_m))

        airport_summary = {
            "icao": icao,
            "name": airport.get("name", ""),
            "elevation_m": elevation_m,
            "runways": []
        }

        for rwy in airport.get("runways", []):
            designation = rwy["designation"]
            length_m = rwy["length_m"]
            width_m = rwy["width_m"]
            heading = rwy["true_heading"]
            center_lat = rwy["center_lat"]
            center_lon = rwy["center_lon"]

            easting, northing = wgs84_to_utm(center_lon, center_lat)
            poly = runway_polygon(easting, northing, length_m, width_m, heading)
            prepared_poly = prep(poly)

            # Bounding box in pixel indices (inclusive)
            min_e, min_n, max_e, max_n = poly.bounds
            px_min_f, py_min_f = utm_to_pixel(min_e, max_n)  # top-left
            px_max_f, py_max_f = utm_to_pixel(max_e, min_n)  # bottom-right

            px_min = max(0, int(math.floor(px_min_f)))
            px_max = min(NCOLS - 1, int(math.ceil(px_max_f)))
            py_min = max(0, int(math.floor(py_min_f)))
            py_max = min(NROWS - 1, int(math.ceil(py_max_f)))

            flattened = 0
            for py in range(py_min, py_max + 1):
                for px in range(px_min, px_max + 1):
                    # Pixel centre in UTM
                    e, n = pixel_to_utm(px + 0.5, py + 0.5)
                    if prepared_poly.contains(Point(e, n)):
                        # Only overwrite real elevation pixels, leave NODATA as-is
                        if dem[py, px] != NODATA:
                            dem[py, px] = elevation_i16
                            flattened += 1

            airport_summary["runways"].append({
                "designation": designation,
                "length_m": length_m,
                "width_m": width_m,
                "surface": rwy.get("surface", ""),
                "true_heading": heading,
                "flattened_pixels": flattened,
                "center_utm_easting": round(easting, 3),
                "center_utm_northing": round(northing, 3)
            })

        summary["airports"].append(airport_summary)

    # Write flattened heightmap in the same raw format
    dem.astype(np.int16).tofile(OUTPUT_RAW)

    # Verify output size
    expected_bytes = NROWS * NCOLS * NBANDS * 2
    output_size = OUTPUT_RAW.stat().st_size
    summary["expected_bytes"] = expected_bytes
    summary["output_bytes"] = output_size
    summary["size_ok"] = output_size == expected_bytes

    # Overall changed pixel count
    changed = int(np.sum(original != dem))
    summary["total_changed_pixels"] = changed

    return summary


def write_summary(summary: dict) -> None:
    """Write a human-readable summary of the flattening run."""
    lines = []
    lines.append("Runway flattening summary")
    lines.append("=" * 50)
    lines.append(f"Input:  {summary['input_file']}")
    lines.append(f"Output: {summary['output_file']}")
    lines.append(f"Heightmap shape: {summary['input_shape']}")
    lines.append(f"Expected output bytes: {summary['expected_bytes']}")
    lines.append(f"Actual output bytes:   {summary['output_bytes']}")
    lines.append(f"Size matches: {summary['size_ok']}")
    lines.append(f"Total pixels changed: {summary['total_changed_pixels']}")
    lines.append("")

    for ap in summary["airports"]:
        lines.append(f"Airport: {ap['icao']} - {ap['name']}")
        lines.append(f"  Elevation (m): {ap['elevation_m']}")
        for rwy in ap["runways"]:
            lines.append(
                f"  Runway {rwy['designation']}: "
                f"{rwy['length_m']} m x {rwy['width_m']} m, "
                f"surface={rwy['surface']}, true_heading={rwy['true_heading']:.2f} deg, "
                f"flattened_pixels={rwy['flattened_pixels']}"
            )
            lines.append(
                f"    UTM centre: E={rwy['center_utm_easting']} N={rwy['center_utm_northing']}"
            )
        lines.append("")

    SUMMARY_TXT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    summary = flatten_runways()
    write_summary(summary)

    print(f"Wrote flattened heightmap to: {summary['output_file']}")
    print(f"Output size: {summary['output_bytes']} bytes "
          f"(expected {summary['expected_bytes']}) - OK={summary['size_ok']}")
    print(f"Total pixels flattened: {summary['total_changed_pixels']}")
    print(f"Summary written to: {SUMMARY_TXT}")


if __name__ == "__main__":
    main()
