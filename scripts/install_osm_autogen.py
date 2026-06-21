#!/usr/bin/env python3
"""Install a CORRECT, bounded OSM autogen for central Skopje via the verified <3 m
method (OSM footprints, agent-measured 2.81 m on the painted roofs; anchor 575910).

Each significant OSM building (area >= MIN_AREA) is extruded from its real footprint
(world-aligned local coords, ori=0 -- the mesh carries the orientation, the approach
that already rendered correctly) at a height from building:levels, coloured from the
installed ortho. Bounded to the central-Skopje patches + a min-area filter so we stay
well under the per-tile object budget. No GUI, no hash. This is the autogen redone the
RIGHT way -- OSM, not the parcel-boundary cadastre.
"""
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3d

ROOT = Path(__file__).resolve().parent.parent
OSM = ROOT / ".sandbox" / "osm" / "buildings.geojson"
INSTALL = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
OBJDIR = INSTALL / "World" / "Objects"
TEX = INSTALL / "Textures"
OBJFILE = INSTALL / "MacedoniaSkopje.obj"
HDR_E, HDR_N = 576000.0, 4631040.0   # PATCH-GRID SE corner (what the TEXTURES were built on,
                                      # condor_grid.patch_bounds_utm) -- NOT the .trn header
                                      # 575910/4631130. The 90 m gap = the in-sim offset.
ULX_W, ULY_N, SOUTH0 = 506880.0, 4700160.0, 4631040.0
PATCH, PXN, NCOL, XDIM, DEMW = 5760.0, 2048, 12, 30.0, 2305
MIN_AREA = 150.0                                  # m^2 -- significant buildings only
CAP = 3000                                        # keep <= real-landscape object budget
# central-Skopje window (UTM 34N): the city + the cadastre proof area
E0, N0, E1, N1 = 528000.0, 4645000.0, 545000.0, 4656000.0
LATLON = (41.94, 21.36, 42.05, 21.53)             # coarse pre-filter (S,W,N,E)

_tx = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
_dem = np.fromfile(ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305_flat.raw",
                   dtype="<u2").reshape(DEMW, DEMW)
_cache: dict = {}


def altitude(E, N):
    c = min(max(int(round((E - ULX_W) / XDIM)), 0), DEMW - 1)
    r = min(max(int(round((ULY_N - N) / XDIM)), 0), DEMW - 1)
    return float(_dem[r, c])


def load_patch(col, row):
    if (col, row) not in _cache:
        p = TEX / f"t{NCOL - 1 - col:02d}{row:02d}.dds"
        _cache[(col, row)] = ((np.asarray(Image.open(p).convert("RGB").resize((PXN, PXN)),
                                          dtype=np.float32) / 255.0) if p.exists() else None)
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


def record(name, E, N, z, ori=0.0, scale=1.0):
    nm = name.encode("ascii")
    return (struct.pack("<5f", HDR_E - E, N - HDR_N, z, scale, ori)
            + bytes([len(nm)]) + nm.ljust(131, b"\x00"))


def levels_height(props):
    lv = props.get("building:levels") or props.get("levels")
    try:
        return float(lv) * 3.0 + 3.0
    except (TypeError, ValueError):
        return 9.0


def main():
    OBJDIR.mkdir(parents=True, exist_ok=True)
    for old in OBJDIR.glob("b[0-9]*.c3d"):
        old.unlink()
    feats = json.load(open(OSM, encoding="utf-8"))["features"]
    s, w, n_, e = LATLON
    cands = []
    for f in feats:
        geom = f.get("geometry")
        if not geom or geom["type"] not in ("Polygon", "MultiPolygon"):
            continue
        g = shape(geom)
        c = g.centroid
        if not (w <= c.x <= e and s <= c.y <= n_):       # coarse lat/lon pre-filter
            continue
        gu = shp_transform(_tx.transform, g)
        poly = max(gu.geoms, key=lambda p: p.area) if gu.geom_type == "MultiPolygon" else gu
        if poly.is_empty or poly.area < MIN_AREA:
            continue
        cen = poly.centroid
        if not (E0 <= cen.x <= E1 and N0 <= cen.y <= N1):
            continue
        if len(poly.exterior.coords) >= 4:
            cands.append((poly.area, poly, f.get("properties", {})))
    # real Condor landscapes hold ~5-7k objects for the WHOLE map by reusing ~150
    # models; we keep exact per-building footprints, so cap to the CAP largest (most
    # visible from altitude) to stay inside the object/per-tile budget.
    cands.sort(key=lambda t: -t[0])
    cands = cands[:CAP]
    recs = []
    for i, (_area, poly, props) in enumerate(cands):
        cE, cN = poly.centroid.x, poly.centroid.y
        r, gr, b = roof_colour(poly)
        local = [(x - cE, y - cN) for (x, y) in poly.exterior.coords]
        nm = f"b{i:05d}.c3d"
        obj = c3d.make_prism(nm[:-4], local, levels_height(props),
                             texture="", material=(r, gr, b, 1.0, 1.0, 1.0))
        c3d.write_c3d(c3d.C3DFile(objects=[obj]), OBJDIR / nm)
        recs.append(record(nm, cE, cN, altitude(cE, cN)))
    OBJFILE.write_bytes(b"".join(recs))
    print(f"installed {len(recs)} OSM buildings (top {CAP} by area, >= {MIN_AREA:.0f} m^2, "
          f"central Skopje) -> {OBJFILE} ({OBJFILE.stat().st_size} B)")


if __name__ == "__main__":
    main()
