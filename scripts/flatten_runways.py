#!/usr/bin/env python3
"""
flatten_runways.py

Flatten each runway footprint in the CANONICAL exactly-30m UTM heightmap, with a
graded skirt so the flattened plateau blends into the surrounding terrain instead
of leaving a vertical mesh cliff.

Input  : sources/dem/macedonia_skopje_dem_30m_2305.raw   (int16 LE, 2305x2305,
         NW pixel CENTER 506880/4700160, exactly 30 m/px, EPSG:32634)
Output : sources/dem/macedonia_skopje_dem_30m_2305_flat.raw  (same format)

Runway parameters come from data/airports_aligned.json when present (centers /
true headings validated against the ortho imagery in scripts/align_runways.py),
otherwise from data/airports.json.

Per runway:
  * Flatten an ORIENTED rectangle (rotated to the runway true heading) of size
    (L + 350) x (W + 100) m -- ~175 m flat past each threshold + 50 m lateral
    margin each side -- to the integer target elevation. The generous along-track
    flat zone stops Condor's spline terrain from bending the runway mid-strip, and
    covers the ~170 m threshold-offset ground-start spawn (see generate_apt.py).
  * GRADED SKIRT: over the next SKIRT_M (= 90 m, 3 px at 30 m) beyond the
    rectangle, linearly blend the flat elevation back to the original terrain
    (~1:3 visual slope for a 30 m plateau-to-terrain step). The blend uses the
    signed distance to the oriented rectangle, so corners round naturally.

Only the heightmap is written; .trn / textures / forests are untouched.

Outputs a human-readable summary to tools/flatten_runways_summary.txt.
"""

import json
import math
import sys
from pathlib import Path

import numpy as np
import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent))
from condor_grid import (
    LANDSCAPE_NAME,
    ULXMAP as G_ULXMAP,
    ULYMAP as G_ULYMAP,
    XDIM as G_XDIM,
    WIDTH as G_WIDTH,
    HEIGHT as G_HEIGHT,
)

# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TOOLS_DIR = PROJECT_ROOT / "tools"
DEM_DIR = PROJECT_ROOT / "sources" / "dem"

# Landscape-scoped inputs/outputs.  The NM DEM (7681x6145) is produced by the
# terrain agent; flattening writes the *_flat.raw the forest/tr3 steps consume.
_NM = LANDSCAPE_NAME == "NorthMacedonia"
if _NM:
    AIRPORTS_ALIGNED = DATA_DIR / "airports_nm_aligned.json"
    AIRPORTS_JSON = DATA_DIR / "airports_nm.json"
    INPUT_RAW = DEM_DIR / "northmacedonia_dem_30m_7681x6145.raw"
    OUTPUT_RAW = DEM_DIR / "northmacedonia_dem_30m_7681x6145_flat.raw"
    SUMMARY_TXT = TOOLS_DIR / "flatten_runways_nm_summary.txt"
else:
    AIRPORTS_ALIGNED = DATA_DIR / "airports_aligned.json"
    AIRPORTS_JSON = DATA_DIR / "airports.json"
    INPUT_RAW = DEM_DIR / "macedonia_skopje_dem_30m_2305.raw"
    OUTPUT_RAW = DEM_DIR / "macedonia_skopje_dem_30m_2305_flat.raw"
    SUMMARY_TXT = TOOLS_DIR / "flatten_runways_summary.txt"

# ---------------------------------------------------------------------------
# Heightmap geometry -- canonical exactly-30m raw, derived from condor_grid so
# expansion is a pure reparameterisation.
# ---------------------------------------------------------------------------
NROWS = G_HEIGHT          # skopje 2305 ; nm 6145
NCOLS = G_WIDTH           # skopje 2305 ; nm 7681
XDIM = G_XDIM             # metres per pixel, easting (EXACT 30 m)
YDIM = G_XDIM             # metres per pixel, northing (EXACT 30 m)
ULXMAP = G_ULXMAP         # easting of the CENTRE of the top-left pixel
ULYMAP = G_ULYMAP         # northing of the CENTRE of the top-left pixel

# Flatten rectangle padding and skirt.
# Condor renders terrain as a SPLINE through the .tr3 grid points, so a plateau
# only as long as the runway still bends mid-strip (Condor forum t=18380, t=20379).
# The flat (zero-slope) core must extend well past each threshold and the graded
# skirt must begin OUTSIDE it. The .apt also spawns the glider ~170 m in from its
# runway end, so generate_apt.py extends the declared length by ~340 m to put the
# start on the real threshold -- the flat core below is sized to cover that.
PAD_LEN_M = 350.0        # total extra length: ~175 m flat past each threshold
PAD_WID_M = 100.0        # total extra width: 50 m flat lateral margin each side
SKIRT_M = 90.0           # graded blend distance beyond the flat rectangle (3 px)

# ---------------------------------------------------------------------------
# Coordinate transform: WGS-84 -> UTM 34N
# ---------------------------------------------------------------------------
_transformer = pyproj.Transformer.from_crs(4326, 32634, always_xy=True)


def wgs84_to_utm(lon: float, lat: float) -> tuple[float, float]:
    return _transformer.transform(lon, lat)


def load_airports() -> tuple[dict, str]:
    """Prefer the imagery-aligned airports file; fall back to the raw json."""
    if AIRPORTS_ALIGNED.exists():
        return json.loads(AIRPORTS_ALIGNED.read_text(encoding="utf-8")), str(AIRPORTS_ALIGNED)
    if AIRPORTS_JSON.exists():
        return json.loads(AIRPORTS_JSON.read_text(encoding="utf-8")), str(AIRPORTS_JSON)
    raise FileNotFoundError("No airports json found (aligned or base).")


def flatten_one(dem: np.ndarray, e_c: float, n_c: float, length_m: float,
                width_m: float, hdg_deg: float, target_i16: int) -> int:
    """
    Flatten one oriented runway rectangle (with graded skirt) into `dem`
    in place. Returns the number of pixels modified (plateau + skirt).

    The rectangle is (length_m + PAD_LEN_M) x (width_m + PAD_WID_M), oriented to
    the true heading. A pixel's signed distance to the rectangle is:
        d = max(|along| - hl, |perp| - hw)   (negative inside; positive outside)
    where (along, perp) are the pixel's offsets from the centre resolved along /
    across the centreline. Inside (d <= 0) -> target elevation. In the skirt
    (0 < d <= SKIRT_M) -> linear blend target<->original. Beyond -> untouched.
    """
    hl = (length_m + PAD_LEN_M) / 2.0
    hw = (width_m + PAD_WID_M) / 2.0
    reach = max(hl, hw) + SKIRT_M  # bounding radius incl. skirt

    # Pixel bounding box around the centre (limit work to a local window).
    px_c = (e_c - ULXMAP) / XDIM
    py_c = (ULYMAP - n_c) / YDIM
    half_px = reach / XDIM + 2
    px0 = max(0, int(math.floor(px_c - half_px)))
    px1 = min(NCOLS - 1, int(math.ceil(px_c + half_px)))
    py0 = max(0, int(math.floor(py_c - half_px)))
    py1 = min(NROWS - 1, int(math.ceil(py_c + half_px)))

    # Local pixel grid -> UTM (pixel centres).
    ys, xs = np.mgrid[py0:py1 + 1, px0:px1 + 1]
    E = ULXMAP + xs * XDIM
    N = ULYMAP - ys * YDIM
    dE = E - e_c
    dN = N - n_c

    th = math.radians(hdg_deg)
    a_e, a_n = math.sin(th), math.cos(th)     # unit vector along centreline
    p_e, p_n = math.cos(th), -math.sin(th)    # unit vector perpendicular (+90)
    along = dE * a_e + dN * a_n
    perp = dE * p_e + dN * p_n

    # Signed distance to the oriented rectangle (metres, negative inside).
    d = np.maximum(np.abs(along) - hl, np.abs(perp) - hw)

    sub = dem[py0:py1 + 1, px0:px1 + 1]
    orig = sub.astype(np.float64)

    inside = d <= 0.0
    skirt = (d > 0.0) & (d <= SKIRT_M)

    new = sub.copy()
    # plateau
    new[inside] = target_i16
    # graded skirt: w=1 at rect edge -> 0 at skirt outer edge
    w = np.clip(1.0 - d / SKIRT_M, 0.0, 1.0)
    blended = np.rint(target_i16 * w + orig * (1.0 - w)).astype(sub.dtype)
    new[skirt] = blended[skirt]

    changed = int(np.sum(new != sub))
    dem[py0:py1 + 1, px0:px1 + 1] = new
    return changed


def flatten_runways() -> dict:
    if not INPUT_RAW.exists():
        raise FileNotFoundError(f"Input DEM not found: {INPUT_RAW}")

    data, src_json = load_airports()

    dem = np.fromfile(INPUT_RAW, dtype=np.int16)
    if dem.size != NROWS * NCOLS:
        raise ValueError(f"Expected {NROWS*NCOLS} samples, got {dem.size}")
    dem = dem.reshape(NROWS, NCOLS)
    original = dem.copy()

    summary = {
        "input_file": str(INPUT_RAW),
        "output_file": str(OUTPUT_RAW),
        "airports_source": src_json,
        "input_shape": list(dem.shape),
        "input_dtype": str(dem.dtype),
        "pad_len_m": PAD_LEN_M,
        "pad_wid_m": PAD_WID_M,
        "skirt_m": SKIRT_M,
        "airports": [],
    }

    for ap in data.get("airports", []):
        icao = ap["icao"]
        elev_i16 = int(round(ap["elevation_m"]))
        ap_sum = {"icao": icao, "name": ap.get("name", ""),
                  "elevation_m": ap["elevation_m"], "runways": []}

        for rwy in ap.get("runways", []):
            L = rwy["length_m"]
            Wm = rwy["width_m"]
            hdg = rwy["true_heading"]
            e_c, n_c = wgs84_to_utm(rwy["center_lon"], rwy["center_lat"])
            changed = flatten_one(dem, e_c, n_c, L, Wm, hdg, elev_i16)
            ap_sum["runways"].append({
                "designation": rwy["designation"],
                "length_m": L, "width_m": Wm,
                "surface": rwy.get("surface", ""),
                "true_heading": hdg,
                "aligned_from_imagery": bool(rwy.get("_aligned_from_imagery", False)),
                "center_utm_e": round(e_c, 1),
                "center_utm_n": round(n_c, 1),
                "target_elev_i16": elev_i16,
                "changed_pixels": changed,
            })
        summary["airports"].append(ap_sum)

    dem.astype(np.int16).tofile(OUTPUT_RAW)

    expected = NROWS * NCOLS * 2
    out_sz = OUTPUT_RAW.stat().st_size
    summary["expected_bytes"] = expected
    summary["output_bytes"] = out_sz
    summary["size_ok"] = out_sz == expected
    summary["total_changed_pixels"] = int(np.sum(original != dem))
    return summary


def write_summary(s: dict) -> None:
    L = []
    L.append("Runway flattening summary (exactly-30m raw, graded skirt)")
    L.append("=" * 58)
    L.append(f"Input:           {s['input_file']}")
    L.append(f"Output:          {s['output_file']}")
    L.append(f"Airports source: {s['airports_source']}")
    L.append(f"Shape:           {s['input_shape']}  dtype {s['input_dtype']}")
    L.append(f"Pad L/W:         +{s['pad_len_m']} / +{s['pad_wid_m']} m   "
             f"Skirt: {s['skirt_m']} m")
    L.append(f"Output bytes:    {s['output_bytes']} (expected {s['expected_bytes']}) "
             f"OK={s['size_ok']}")
    L.append(f"Total changed:   {s['total_changed_pixels']} px")
    L.append("")
    for ap in s["airports"]:
        L.append(f"{ap['icao']} - {ap['name']}  (elev {ap['elevation_m']} m)")
        for r in ap["runways"]:
            tag = " [aligned-to-imagery]" if r["aligned_from_imagery"] else ""
            L.append(f"  {r['designation']}: {r['length_m']}x{r['width_m']} m  "
                     f"hdg {r['true_heading']:.2f}  elev {r['target_elev_i16']} m  "
                     f"changed {r['changed_pixels']} px{tag}")
            L.append(f"    UTM centre E={r['center_utm_e']} N={r['center_utm_n']}")
        L.append("")
    SUMMARY_TXT.write_text("\n".join(L), encoding="utf-8")


def _oriented_rect(e_c, n_c, length_m, width_m, hdg_deg, pad_len=0.0, pad_wid=0.0):
    """Return the 4 UTM corners of an oriented runway rectangle (E,N tuples)."""
    hl = (length_m + pad_len) / 2.0
    hw = (width_m + pad_wid) / 2.0
    th = math.radians(hdg_deg)
    a_e, a_n = math.sin(th), math.cos(th)     # along centreline
    p_e, p_n = math.cos(th), -math.sin(th)    # perpendicular
    corners = []
    for sl, sw in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        e = e_c + sl * hl * a_e + sw * hw * p_e
        n = n_c + sl * hl * a_n + sw * hw * p_n
        corners.append((e, n))
    corners.append(corners[0])
    return corners


def export_footprints() -> dict:
    """Emit runway flatten footprints (no DEM needed) for the terrain agent.

    Writes:
      data/<landscape>_runway_footprints.geojson  -- oriented flatten rectangles
        (the L+PAD_LEN x W+PAD_WID plateau) in UTM 34N (EPSG:32634), each tagged
        with icao / designation / target_elev_m / heading, plus a SKIRT_M field.
      tools/flatten_runways_<...>_footprints.json  -- machine-readable manifest.

    The terrain agent flattens each polygon to ``target_elev_m`` (with the same
    graded skirt this script applies) and re-hashes.
    """
    data, src_json = load_airports()
    feats = []
    manifest = {"landscape": LANDSCAPE_NAME, "crs": "EPSG:32634",
                "pad_len_m": PAD_LEN_M, "pad_wid_m": PAD_WID_M, "skirt_m": SKIRT_M,
                "airports_source": src_json, "runways": []}
    for ap in data.get("airports", []):
        elev_i16 = int(round(ap["elevation_m"]))
        for rwy in ap.get("runways", []):
            e_c, n_c = wgs84_to_utm(rwy["center_lon"], rwy["center_lat"])
            ring = _oriented_rect(e_c, n_c, rwy["length_m"], rwy["width_m"],
                                  rwy["true_heading"], PAD_LEN_M, PAD_WID_M)
            props = {
                "icao": ap["icao"], "name": ap.get("name", ""),
                "designation": rwy["designation"],
                "target_elev_m": elev_i16,
                "true_heading": rwy["true_heading"],
                "length_m": rwy["length_m"], "width_m": rwy["width_m"],
                "pad_len_m": PAD_LEN_M, "pad_wid_m": PAD_WID_M, "skirt_m": SKIRT_M,
                "center_utm_e": round(e_c, 1), "center_utm_n": round(n_c, 1),
            }
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "Polygon",
                                       "coordinates": [[list(c) for c in ring]]}})
            manifest["runways"].append(props)
    gj = {"type": "FeatureCollection",
          "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32634"}},
          "features": feats}
    out_gj = DATA_DIR / f"{LANDSCAPE_NAME.lower()}_runway_footprints.geojson"
    out_mf = TOOLS_DIR / f"flatten_runways_{LANDSCAPE_NAME.lower()}_footprints.json"
    out_gj.write_text(json.dumps(gj, indent=2), encoding="utf-8")
    out_mf.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {len(feats)} runway footprints:\n  {out_gj}\n  {out_mf}")
    for r in manifest["runways"]:
        print(f"  {r['icao']:6s} {r['designation']:6s} elev {r['target_elev_m']:>4} m  "
              f"hdg {r['true_heading']:.1f}  ({r['length_m']}x{r['width_m']} m)")
    return manifest


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--footprints", action="store_true",
                    help="only export runway flatten footprints (no DEM needed); "
                         "for the terrain agent to flatten + re-hash")
    args = ap.parse_args()

    if args.footprints or not INPUT_RAW.exists():
        if not INPUT_RAW.exists() and not args.footprints:
            print(f"[flatten] input DEM {INPUT_RAW.name} not present yet -- "
                  "exporting footprints only (terrain agent will flatten).")
        export_footprints()
        if not INPUT_RAW.exists():
            return

    s = flatten_runways()
    write_summary(s)
    print(f"Input : {s['input_file']}")
    print(f"Output: {s['output_file']}  ({s['output_bytes']} bytes, "
          f"OK={s['size_ok']})")
    print(f"Airports: {s['airports_source']}")
    print(f"Total pixels changed: {s['total_changed_pixels']}")
    for ap in s["airports"]:
        for r in ap["runways"]:
            print(f"  {ap['icao']} {r['designation']}: {r['changed_pixels']} px "
                  f"-> {r['target_elev_i16']} m"
                  f"{'  [aligned]' if r['aligned_from_imagery'] else ''}")
    print(f"Summary: {SUMMARY_TXT}")


if __name__ == "__main__":
    main()
