#!/usr/bin/env python3
"""DIAGNOSTIC object placement -- isolate why nothing renders, in ONE in-sim test.

  * CONTROL  ZZ_TEST.c3d : a KNOWN-GOOD Slovenia2 building (B-PZ4) retextured into our
    landscape, at Stenkovec, scale 3 (~70 m, unmissable). If this does NOT appear, the
    bug is the .obj mechanism itself (LandscapeEditor compile needed), not our models.
  * Buildings: our cadastre prisms, now TEXTURED (Slovenia2 G10 facade) + ortho colour.
  * Millennium Cross: left UNTEXTURED -> cross-vs-control tells us if a texture is
    MANDATORY for an object to render.
  * Our World/Textures was EMPTY (Slovenia2 has 93); now populated.

Outcomes: nothing -> .obj needs the editor; control only -> our prism c3d is bad;
control+buildings, no cross -> texture mandatory; all -> fixed. Format per spec 11 +
Slovenia2 (name+.c3d, posZ=terrain alt, origin=.trn header). No GUI.
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
DEMF = ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305_flat.raw"
INSTALL = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
SLO = Path("C:/Condor2/Landscapes/Slovenia2")
OBJDIR = INSTALL / "World" / "Objects"
WTEX = INSTALL / "World" / "Textures"
TEX = INSTALL / "Textures"
OBJFILE = INSTALL / "MacedoniaSkopje.obj"
HDR_E, HDR_N = 575910.0, 4631130.0
ULX_W, ULY_N, SOUTH0 = 506880.0, 4700160.0, 4631040.0
PATCH, PXN, NCOL, XDIM, DEMW = 5760.0, 2048, 12, 30.0, 2305
BLDG_TEX = "landscapes/MacedoniaSkopje/world/textures/G10.dds"

_dem = np.fromfile(DEMF, dtype="<u2").reshape(DEMW, DEMW)
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


def copy_tex():
    WTEX.mkdir(parents=True, exist_ok=True)
    for f in ("G10.dds", "G8a.dds"):
        s = SLO / "World" / "Textures" / f
        if s.exists():
            (WTEX / f).write_bytes(s.read_bytes())


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


def place_cross(recs):
    tx = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
    E, N = tx.transform(21.40978, 41.96369)
    W = c3d.WHITE_MATERIAL
    pillar = c3d.make_box("CrossV", 7, 7, 0, 66, texture="", material=W)
    arms = c3d.make_box("CrossH", 30, 5, 38, 48, texture="", material=W)
    c3d.write_c3d(c3d.C3DFile(objects=[pillar, arms]), OBJDIR / "MillenniumCross.c3d")
    recs.append(record("MillenniumCross.c3d", E, N, altitude(E, N)))


def place_airport_tests(recs):
    """Three objects in a row at Stenkovec (inside the ~5 km render range, in the spawn
    view) to ISOLATE our geometry from the pipeline:
      ZZ_CONTROL = Slovenia2 B-PZ4   grey/textured  (proven; the one you already saw)
      ZZ_MYPRISM = our make_prism     RED/untextured (tests the BUILDING geometry)
      ZZ_MYBOX   = our make_box       WHITE/untextured (tests the CROSS geometry)
    If grey shows but red/white don't, our c3d writer is the bug -> compare to B-PZ4."""
    tx = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
    E0, N0 = tx.transform(21.3888, 42.0594)        # Stenkovec LWSN
    z = altitude(E0, N0)
    src = c3d.parse_c3d(str(SLO / "World" / "Objects" / "B-PZ4.c3d"))
    for o in src.objects:
        if o.texture:
            base = o.texture.replace("\\", "/").split("/")[-1]
            (WTEX / base).write_bytes((SLO / "World" / "Textures" / base).read_bytes())
            o.texture = f"landscapes/MacedoniaSkopje/world/textures/{base}"
    c3d.write_c3d(src, OBJDIR / "ZZ_CONTROL.c3d")
    recs.append(record("ZZ_CONTROL.c3d", E0, N0, z, scale=3.0))
    rect = [(-20, -10), (20, -10), (20, 10), (-20, 10)]          # 40x20 m, 30 m tall
    pr = c3d.make_prism("ZZ_MYPRISM", rect, 30.0, texture="", material=(0.9, 0.1, 0.1, 1, 1, 1))
    c3d.write_c3d(c3d.C3DFile(objects=[pr]), OBJDIR / "ZZ_MYPRISM.c3d")
    recs.append(record("ZZ_MYPRISM.c3d", E0 + 90, N0, z))
    bx = c3d.make_box("ZZ_MYBOX", 24, 24, 0, 40, texture="", material=c3d.WHITE_MATERIAL)
    c3d.write_c3d(c3d.C3DFile(objects=[bx]), OBJDIR / "ZZ_MYBOX.c3d")
    recs.append(record("ZZ_MYBOX.c3d", E0 + 180, N0, z))
    # my box TEXTURED, with B-PZ4's exact material (1,1,1,1,0,0.1) -- if only THIS of
    # my four shows, a texture is mandatory and I must texture every building.
    bxt = c3d.make_box("ZZ_MYBOXT", 24, 24, 0, 40,
                       texture="landscapes/MacedoniaSkopje/world/textures/G8a.dds",
                       material=(1.0, 1.0, 1.0, 1.0, 0.0, 0.1))
    c3d.write_c3d(c3d.C3DFile(objects=[bxt]), OBJDIR / "ZZ_MYBOXT.c3d")
    recs.append(record("ZZ_MYBOXT.c3d", E0 + 270, N0, z))
    print(f"  airport tests @ {E0:.0f},{N0:.0f}: ZZ_CONTROL(grey,sl2) ZZ_MYPRISM(red,+90) "
          f"ZZ_MYBOX(white,+180) ZZ_MYBOXT(textured,+270)")


def main():
    OBJDIR.mkdir(parents=True, exist_ok=True)
    for old in (list(OBJDIR.glob("blk*.c3d")) + list(OBJDIR.glob("b[0-9]*.c3d"))
                + list(OBJDIR.glob("ZZ_*.c3d"))):
        old.unlink()
    copy_tex()
    recs = []
    nb = place_buildings(recs)
    place_cross(recs)
    place_airport_tests(recs)
    OBJFILE.write_bytes(b"".join(recs))
    print(f"placed {nb} buildings(textured) + cross(untextured) + 1 control = "
          f"{len(recs)} records -> {OBJFILE.stat().st_size} B")
    print(f"World/Textures now has {len(list(WTEX.glob('*.dds')))} dds")


if __name__ == "__main__":
    main()
