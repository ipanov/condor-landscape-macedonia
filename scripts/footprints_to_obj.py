#!/usr/bin/env python3
r"""
Deterministic Condor-2 object-placement BRIDGE: building footprints -> .obj + .c3d.

Input : a GeoJSON of building footprint polygons. CRS is auto-detected -- a
        feature collection tagged EPSG:32634 (or with metre-scale coords) is used
        as-is; anything in lon/lat (WGS84) is reprojected to UTM 34N first.
Output: under .sandbox/bridge_out/<source>/ -- one extruded-prism `bNNNN.c3d`
        per footprint plus a single `<source>.obj` of 152-byte placement records.
        NOTHING is written into C:/Condor2.

Per-footprint placement (all VERIFIED, see docs/condor_landscape_spec.md sec 11 and
the in-sim-proven scripts/place_increment.py):
  centroid (E,N)  -> posX = HDR_E - E , posY = N - HDR_N   (HDR = .trn header
                     575910.0 / 4631130.0)
  posZ            -> DEM altitude at the centroid (absolute metres, never 0)
  ori             -> azimuth of the LONG edge of the shapely
                     minimum_rotated_rectangle, in radians
  scale           -> 1.0
  height          -> 'height' property, else num_floors/levels*3+3, else 9 m
  geometry        -> c3d.make_prism on the footprint in local (x=E,y=N) metres
                     relative to the centroid; ori is applied by Condor at
                     placement, so the mesh itself is NOT pre-rotated -- the prism
                     keeps the building's true plan shape and ori only spins the
                     (square-ish) bounding alignment. Colour is the mean of the
                     installed DDS pixels under the footprint (untextured prism).

This module is the single source of truth for the footprint->Condor transform; it
is imported by validate_bridge.py so the validator decodes records through the
exact same constants.
"""
from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3d  # noqa: E402

# --------------------------------------------------------------------------- #
# VERIFIED constants
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent
DEMF = ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305_flat.raw"
INSTALL_TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
OUT_ROOT = ROOT / ".sandbox" / "bridge_out"

HDR_E, HDR_N = 575910.0, 4631130.0            # .trn header origin (posX/posY anchor)
DEM_NWX, DEM_NWY = 506880.0, 4700160.0        # DEM NW corner
DEM_PX, DEM_W = 30.0, 2305                    # 30 m/px, 2305x2305

# Texture patch grid (VERIFIED: true-UTM OSM roads align with painted roads).
TEX_ULX_W = 506880.0                          # west edge of col 0
TEX_SOUTH0 = 4631040.0                        # south edge of row 0
PATCH_M = 5760.0                              # patch side, metres
PATCH_PX = 2048                               # DDS side, pixels
NCOL = 12                                     # filename uses (11-col)

_DEM = np.fromfile(DEMF, dtype="<u2").reshape(DEM_W, DEM_W)
_PATCH_CACHE: dict = {}


# --------------------------------------------------------------------------- #
# DEM altitude + DDS colour sampling
# --------------------------------------------------------------------------- #
def altitude(E: float, N: float) -> float:
    """Absolute terrain altitude (m) at UTM (E,N) by nearest DEM cell."""
    col = min(max(int(round((E - DEM_NWX) / DEM_PX)), 0), DEM_W - 1)
    row = min(max(int(round((DEM_NWY - N) / DEM_PX)), 0), DEM_W - 1)
    return float(_DEM[row, col])


def _patch_for(E: float, N: float):
    """(col_from_west, row_from_south) of the texture patch containing (E,N)."""
    return int((E - TEX_ULX_W) // PATCH_M), int((N - TEX_SOUTH0) // PATCH_M)


def _load_patch(col: int, row: int):
    if (col, row) not in _PATCH_CACHE:
        p = INSTALL_TEX / f"t{NCOL - 1 - col:02d}{row:02d}.dds"
        if p.exists():
            arr = np.asarray(Image.open(p).convert("RGB"), dtype=np.float32) / 255.0
            if arr.shape[0] != PATCH_PX:
                arr = np.asarray(
                    Image.open(p).convert("RGB").resize((PATCH_PX, PATCH_PX)),
                    dtype=np.float32) / 255.0
        else:
            arr = None
        _PATCH_CACHE[(col, row)] = arr
    return _PATCH_CACHE[(col, row)]


def _footprint_to_px(ring, col: int, row: int):
    """Footprint UTM ring -> (x,y) float pixel columns/rows in patch (col,row)'s
    north-up DDS (origin top-left = NW; +x east, +y south)."""
    west = TEX_ULX_W + col * PATCH_M
    north = TEX_SOUTH0 + (row + 1) * PATCH_M
    xs = [(x - west) / PATCH_M * PATCH_PX for (x, y) in ring]
    ys = [(north - y) / PATCH_M * PATCH_PX for (x, y) in ring]
    return xs, ys


def roof_colour(ring, cE: float, cN: float):
    """Mean installed-DDS colour under the footprint (fallback: neutral roof)."""
    col, row = _patch_for(cE, cN)
    arr = _load_patch(col, row)
    if arr is None:
        return (0.60, 0.59, 0.57)
    xs, ys = _footprint_to_px(ring, col, row)
    x0, x1 = max(0, int(min(xs))), min(PATCH_PX, int(max(xs)) + 1)
    y0, y1 = max(0, int(min(ys))), min(PATCH_PX, int(max(ys)) + 1)
    if x1 <= x0 or y1 <= y0:
        return (0.60, 0.59, 0.57)
    c = arr[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
    return (float(c[0]), float(c[1]), float(c[2]))


# --------------------------------------------------------------------------- #
# Placement geometry
# --------------------------------------------------------------------------- #
def long_edge_azimuth(poly) -> float:
    """Radian orientation of the LONG edge of the minimum rotated rectangle.

    Returned as a compass-style azimuth measured from +Y(north) toward +X(east),
    i.e. atan2(dE, dN), folded to [0, pi) since a building axis is undirected.
    """
    mrr = poly.minimum_rotated_rectangle
    if mrr.geom_type != "Polygon":
        return 0.0
    cs = list(mrr.exterior.coords)[:4]
    best_len, best_ang = -1.0, 0.0
    for i in range(len(cs)):
        (x0, y0), (x1, y1) = cs[i], cs[(i + 1) % len(cs)]
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy)
        if L > best_len:
            best_len = L
            best_ang = math.atan2(dx, dy)        # dx=east, dy=north
    if best_ang < 0.0:
        best_ang += math.pi
    if best_ang >= math.pi:
        best_ang -= math.pi
    return best_ang


def height_of(props: dict) -> float:
    """Building height (m): explicit 'height', else floors/levels*3 + 3, else 9."""
    def _num(v):
        if v in (None, ""):
            return None
        try:
            return float(str(v).split()[0].replace(",", "."))
        except (ValueError, IndexError):
            return None

    h = _num(props.get("height"))
    if h and h > 0:
        return h
    fl = _num(props.get("num_floors"))
    if fl is None:
        fl = _num(props.get("building:levels"))
    if fl and fl > 0:
        return fl * 3.0 + 3.0
    return 9.0


def record(name: str, E: float, N: float, z: float, ori: float, scale: float = 1.0) -> bytes:
    """152-byte .obj placement record (spec sec 11)."""
    nm = name.encode("ascii")
    if len(nm) > 131:
        raise ValueError(f"name too long: {name!r}")
    return (struct.pack("<5f", HDR_E - E, N - HDR_N, z, scale, ori)
            + bytes([len(nm)]) + nm.ljust(131, b"\x00"))


# --------------------------------------------------------------------------- #
# Source loading (CRS auto-detect + reprojection)
# --------------------------------------------------------------------------- #
_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)


def _crs_is_utm(fc: dict) -> bool:
    """True if the collection is already in UTM-34N (or any metre CRS)."""
    crs = (fc.get("crs") or {}).get("properties", {}).get("name", "")
    if "32634" in str(crs):
        return True
    # Untagged: sniff the first coordinate. lon/lat are |x|<=180, |y|<=90.
    for f in fc.get("features", []):
        g = f.get("geometry")
        if not g:
            continue
        try:
            pt = shape(g).representative_point()
        except Exception:
            continue
        return not (abs(pt.x) <= 180.0 and abs(pt.y) <= 90.0)
    return True


def _iter_polys(fc: dict, reproject: bool):
    """Yield (exterior_ring_UTM, props) for every (multi)polygon, largest part."""
    for f in fc.get("features", []):
        g = f.get("geometry")
        if not g:
            continue
        try:
            geom = shape(g)
        except Exception:
            continue
        if geom.is_empty:
            continue
        if reproject:
            from shapely.ops import transform as shp_transform
            geom = shp_transform(lambda x, y, z=None: _TO_UTM.transform(x, y), geom)
        if not geom.is_valid:
            geom = geom.buffer(0)
            if geom.is_empty:
                continue
        if geom.geom_type == "MultiPolygon":
            geom = max(geom.geoms, key=lambda p: p.area)
        if geom.geom_type != "Polygon":
            continue
        ring = list(geom.exterior.coords)
        if len(ring) < 4:
            continue
        yield geom, ring, dict(f.get("properties") or {})


# --------------------------------------------------------------------------- #
# Main bridge
# --------------------------------------------------------------------------- #
def build(src_geojson: Path, source_tag: str, *, limit: int | None = None,
          bbox_utm: tuple | None = None) -> dict:
    """Convert one GeoJSON to .c3d prisms + a .obj. Returns a stats dict.

    bbox_utm = (E0, N0, E1, N1) optionally restricts placement to footprints whose
    centroid falls inside that UTM box (used to confine the huge landscape-wide OSM
    set to the same Skopje patch where cadastre lives, so the two sources are
    compared on identical ground).
    """
    fc = json.load(open(src_geojson, encoding="utf-8"))
    reproject = not _crs_is_utm(fc)

    out_dir = OUT_ROOT / source_tag
    obj_dir = out_dir / "Objects"
    obj_dir.mkdir(parents=True, exist_ok=True)
    for old in obj_dir.glob("b*.c3d"):
        old.unlink()

    recs: list[bytes] = []
    placements: list[dict] = []          # for the validator (no re-parse needed)
    n = 0
    for geom, ring, props in _iter_polys(fc, reproject):
        if limit is not None and n >= limit:
            break
        cE, cN = geom.centroid.x, geom.centroid.y
        if bbox_utm is not None:
            e0, nn0, e1, nn1 = bbox_utm
            if not (e0 <= cE <= e1 and nn0 <= cN <= nn1):
                continue
        z = altitude(cE, cN)
        ori = long_edge_azimuth(geom)
        h = height_of(props)
        r, g, b = roof_colour(ring, cE, cN)
        local = [(x - cE, y - cN) for (x, y) in ring]
        nm = f"b{n:04d}.c3d"
        try:
            obj = c3d.make_prism(nm[:-4], local, h, texture="",
                                 material=(r, g, b, 1.0, 1.0, 1.0))
        except ValueError:
            continue
        c3d.write_c3d(c3d.C3DFile(objects=[obj]), obj_dir / nm)
        recs.append(record(nm, cE, cN, z, ori))
        placements.append({"name": nm, "E": cE, "N": cN, "z": z, "ori": ori,
                           "ring": ring})
        n += 1

    obj_path = out_dir / f"{source_tag}.obj"
    obj_path.write_bytes(b"".join(recs))

    # Sidecar JSON with the placements (UTM rings) so the validator decodes the
    # exact records this run produced.
    (out_dir / f"{source_tag}_placements.json").write_text(
        json.dumps({"source": source_tag,
                    "reprojected_from_wgs84": reproject,
                    "count": n,
                    "obj_bytes": obj_path.stat().st_size,
                    "placements": placements}),
        encoding="utf-8")

    stats = {"source": source_tag, "input": str(src_geojson),
             "reprojected_from_wgs84": reproject, "placed": n,
             "obj": str(obj_path), "obj_bytes": obj_path.stat().st_size,
             "c3d_dir": str(obj_dir)}
    print(f"[{source_tag}] placed {n} buildings  reproj={reproject}  "
          f"-> {obj_path.name} ({stats['obj_bytes']} B), {n} .c3d in {obj_dir}")
    return stats


def main():
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    cad = ROOT / ".sandbox" / "buildings" / "cadastre_buildings.geojson"
    osm = ROOT / ".sandbox" / "osm" / "buildings.geojson"

    # Cadastre (887 footprints) lives entirely inside one texture patch in central
    # Skopje: patch (col=4,row=3) = t0703.dds, covering E[529920..535680],
    # N[4648320..4654080]. OSM is 67k footprints landscape-wide; restricting OSM to
    # that SAME patch (a) compares the two sources on identical ground -- the only
    # valid way to judge relative alignment -- and (b) keeps the .c3d count sane.
    SKOPJE_PATCH = (TEX_ULX_W + 4 * PATCH_M, TEX_SOUTH0 + 3 * PATCH_M,
                    TEX_ULX_W + 5 * PATCH_M, TEX_SOUTH0 + 4 * PATCH_M)

    results = []
    if cad.exists():
        results.append(build(cad, "cadastre"))
    else:
        print(f"[cadastre] missing: {cad}")
    if osm.exists():
        results.append(build(osm, "osm", bbox_utm=SKOPJE_PATCH))
    else:
        print(f"[osm] missing: {osm}")

    (OUT_ROOT / "bridge_stats.json").write_text(json.dumps(results, indent=2),
                                                encoding="utf-8")
    print(f"\nwrote {OUT_ROOT / 'bridge_stats.json'}")


if __name__ == "__main__":
    main()
