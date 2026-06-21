#!/usr/bin/env python3
"""INCREMENTAL object placement -- buildings (exact footprints) + landmarks.

FORMAT FIXES (verified against Slovenia2.obj + our MacedoniaSkopje.trn header):
  * .obj record name MUST include the ".c3d" extension (Slovenia2: "C1R.c3d").
  * posZ is the ABSOLUTE terrain altitude at the object (Slovenia2 rec=488 m), not 0
    -- sampled here from the flattened DEM, else buildings sit at sea level (buried).
  * origin = the .trn HEADER easting/northing (575910 / 4631130), not the grid corner
    -- posX = header_E - E, posY = N - header_N (spec 11 + Slovenia2-confirmed).

Buildings: each MK-cadastre footprint extruded to its cadastre height, coloured from
the aerial ortho over its own footprint. Landmarks: Millennium Cross on Vodno (a
custom white cross), so there is at least one recognisable object from altitude.
Local frame x=east, y=north about each centroid; ori=0 (mesh carries orientation).
No GUI, no hash.
"""
import json
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3d

ROOT = Path(__file__).resolve().parent.parent
CAD = ROOT / ".sandbox" / "buildings" / "cadastre_buildings.geojson"
DEM = ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305_flat.raw"
INSTALL = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
OBJDIR = INSTALL / "World" / "Objects"
TEX = INSTALL / "Textures"
OBJFILE = INSTALL / "MacedoniaSkopje.obj"

HDR_E, HDR_N = 575910.0, 4631130.0                 # .trn header easting/northing (.obj origin)
ULX_W, ULY_N = 506880.0, 4700160.0                 # DEM/grid NW corner
SOUTH0 = 4631040.0
PATCH, PXN, NCOL, XDIM, DEMW = 5760.0, 2048, 12, 30.0, 2305

_dem = np.fromfile(DEM, dtype="<u2").reshape(DEMW, DEMW)
_cache: dict = {}


def altitude(E, N):
    col = min(max(int(round((E - ULX_W) / XDIM)), 0), DEMW - 1)
    row = min(max(int(round((ULY_N - N) / XDIM)), 0), DEMW - 1)
    return float(_dem[row, col])


def load_patch(col, row):
    if (col, row) not in _cache:
        p = TEX / f"t{NCOL - 1 - col:02d}{row:02d}.dds"
        arr = (np.asarray(Image.open(p).convert("RGB").resize((PXN, PXN)),
                          dtype=np.float32) / 255.0) if p.exists() else None
        _cache[(col, row)] = arr
    return _cache[(col, row)]


def roof_colour(poly):
    cE, cN = poly.centroid.x, poly.centroid.y
    col, row = int((cE - ULX_W) // PATCH), int((cN - SOUTH0) // PATCH)
    arr = load_patch(col, row)
    if arr is None:
        return (0.60, 0.59, 0.57)
    west, north = ULX_W + col * PATCH, SOUTH0 + (row + 1) * PATCH
    xs = [(x - west) / PATCH * PXN for x, _ in poly.exterior.coords]
    ys = [(north - y) / PATCH * PXN for _, y in poly.exterior.coords]
    x0, x1 = max(0, int(min(xs))), min(PXN, int(max(xs)) + 1)
    y0, y1 = max(0, int(min(ys))), min(PXN, int(max(ys)) + 1)
    if x1 <= x0 or y1 <= y0:
        return (0.60, 0.59, 0.57)
    c = arr[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
    return (float(c[0]), float(c[1]), float(c[2]))


def record(name_c3d, E, N, z, ori=0.0, scale=1.0):
    nm = name_c3d.encode("ascii")
    return (struct.pack("<5f", HDR_E - E, N - HDR_N, z, scale, ori)
            + bytes([len(nm)]) + nm.ljust(131, b"\x00"))


def place_buildings(recs):
    feats = json.load(open(CAD, encoding="utf-8"))["features"]
    n = 0
    for i, f in enumerate(feats):
        g = shape(f["geometry"])
        if g.is_empty:
            continue
        poly = max(g.geoms, key=lambda p: p.area) if g.geom_type == "MultiPolygon" else g
        ring = list(poly.exterior.coords)
        if len(ring) < 4:
            continue
        p = f["properties"]
        h = float(p.get("height") or (int(p.get("num_floors", 1) or 1) * 3 + 3))
        r, gr, b = roof_colour(poly)
        cE, cN = poly.centroid.x, poly.centroid.y
        local = [(x - cE, y - cN) for (x, y) in ring]
        nm = f"b{i:04d}.c3d"
        obj = c3d.make_prism(nm[:-4], local, h, texture="",
                             material=(r, gr, b, 1.0, 1.0, 1.0))
        c3d.write_c3d(c3d.C3DFile(objects=[obj]), OBJDIR / nm)
        recs.append(record(nm, cE, cN, altitude(cE, cN)))
        n += 1
    return n


def place_millennium_cross(recs):
    """White cross on Vodno summit (41.96369 N, 21.40978 E) -- vertical + arms."""
    tx = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
    E, N = tx.transform(21.40978, 41.96369)
    W = c3d.WHITE_MATERIAL
    pillar = c3d.make_box("CrossV", 7.0, 7.0, 0.0, 66.0, texture="", material=W)
    arms = c3d.make_box("CrossH", 30.0, 5.0, 38.0, 48.0, texture="", material=W)
    c3d.write_c3d(c3d.C3DFile(objects=[pillar, arms]), OBJDIR / "MillenniumCross.c3d")
    z = altitude(E, N)
    recs.append(record("MillenniumCross.c3d", E, N, z))
    print(f"  Millennium Cross @ UTM {E:.0f},{N:.0f}  alt {z:.0f} m")


def main():
    OBJDIR.mkdir(parents=True, exist_ok=True)
    for old in list(OBJDIR.glob("blk*.c3d")) + list(OBJDIR.glob("b[0-9]*.c3d")):
        old.unlink()
    print(f"DEM sanity: central Skopje alt={altitude(534900,4649000):.0f} m, "
          f"Vodno alt={altitude(*[float(v) for v in __import__('pyproj').Transformer.from_crs('EPSG:4326','EPSG:32634',always_xy=True).transform(21.40978,41.96369)]):.0f} m")
    recs = []
    nb = place_buildings(recs)
    place_millennium_cross(recs)
    OBJFILE.write_bytes(b"".join(recs))
    print(f"placed {nb} buildings + 1 landmark = {len(recs)} records -> {OBJFILE} "
          f"({OBJFILE.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
