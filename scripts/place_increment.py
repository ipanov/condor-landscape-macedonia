#!/usr/bin/env python3
"""Object placement: Stenkovec airport objects (the priority) + Skopje autogen.

Airport (you spawn here, so these are visible immediately AND prove our geometry
renders at close range):
  AP_windsock  : pole + orange sock
  AP_hangar    : box walls (textured) + gable roof
  AP_tug       : simple tow-plane near the 12 threshold
Millennium Cross: GOLD (not white), on Vodno.
Buildings: cadastre prisms, untextured, ortho-sampled colour (no texture-scale issue).

.obj format per spec 11 + Slovenia2 (name+.c3d, posZ=terrain altitude, origin=.trn
header 575910/4631130). Objects render within ~5 km of the camera. No GUI/hash.
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
HANGAR_TEX = "landscapes/MacedoniaSkopje/world/textures/G8a.dds"
TX = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
_dem = np.fromfile(DEMF, dtype="<u2").reshape(DEMW, DEMW)
_cache: dict = {}
V = c3d.Vertex


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


# ---- small custom meshes (double-sided where winding is uncertain) ------------ #
def _quad(v, idx, pts, n, two=True):
    b = len(v)
    for (x, y, z) in pts:
        v.append(V(x, y, z, n[0], n[1], n[2], 0.0, 0.0))
    idx += [b, b + 1, b + 2, b, b + 2, b + 3]
    if two:
        idx += [b, b + 2, b + 1, b, b + 3, b + 2]


def _tri(v, idx, pts, n, two=True):
    b = len(v)
    for (x, y, z) in pts:
        v.append(V(x, y, z, n[0], n[1], n[2], 0.0, 0.0))
    idx += [b, b + 1, b + 2]
    if two:
        idx += [b, b + 2, b + 1]


def gable_roof(name, L, W, wh, rh, mat):
    hl, hw = L / 2, W / 2
    v, idx = [], []
    _quad(v, idx, [(-hl, -hw, wh), (hl, -hw, wh), (hl, 0, rh), (-hl, 0, rh)], (0, -0.6, 0.8))
    _quad(v, idx, [(-hl, hw, wh), (-hl, 0, rh), (hl, 0, rh), (hl, hw, wh)], (0, 0.6, 0.8))
    _tri(v, idx, [(-hl, -hw, wh), (-hl, 0, rh), (-hl, hw, wh)], (-1, 0, 0))
    _tri(v, idx, [(hl, -hw, wh), (hl, hw, wh), (hl, 0, rh)], (1, 0, 0))
    return c3d.C3DObject(name=name, texture="", material=mat, vertices=v, indices=idx)


def windsock(mat_pole, mat_sock):
    pole = c3d.make_box("wsk_pole", 0.5, 0.5, 0.0, 7.0, material=mat_pole)
    v, idx = [], []
    h = 7.0
    mouth = [(0, -0.6, h - 0.6), (0, 0.6, h - 0.6), (0, 0.6, h + 0.6), (0, -0.6, h + 0.6)]
    for (x, y, z) in mouth:
        v.append(V(x, y, z, 0, 0, 0, 0, 0))
    t = len(v)
    v.append(V(3.0, 0, h, 0, 0, 0, 0, 0))            # tail downwind (+X)
    for i in range(4):
        a, b = i, (i + 1) % 4
        idx += [a, b, t, a, t, b]
    sock = c3d.C3DObject(name="wsk_sock", texture="", material=mat_sock, vertices=v, indices=idx)
    return [pole, sock]


def tug(body, trim):
    fus = c3d.make_box("tug_fus", 1.1, 7.0, 0.5, 2.3, material=body)
    wing = c3d.make_box("tug_wing", 10.0, 1.6, 1.7, 2.0, material=body)
    for vv in wing.vertices:
        vv.py += 0.6
    htail = c3d.make_box("tug_htail", 3.4, 1.2, 1.5, 1.8, material=body)
    vtail = c3d.make_box("tug_vtail", 0.3, 1.5, 2.0, 3.4, material=trim)
    for vv in htail.vertices + vtail.vertices:
        vv.py -= 3.0
    return [fus, wing, htail, vtail]


def copy_tex():
    WTEX.mkdir(parents=True, exist_ok=True)
    s = SLO / "World" / "Textures" / "G8a.dds"
    if s.exists():
        (WTEX / "G8a.dds").write_bytes(s.read_bytes())


# ---- placement ---------------------------------------------------------------- #
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
        obj = c3d.make_prism(nm[:-4], local, h, texture="", material=(r, gr, b, 1.0, 1.0, 1.0))
        c3d.write_c3d(c3d.C3DFile(objects=[obj]), OBJDIR / nm)
        recs.append(record(nm, cE, cN, altitude(cE, cN)))
        n += 1
    return n


def place_cross(recs):
    E, N = TX.transform(21.40978, 41.96369)
    GOLD = (0.83, 0.68, 0.22, 1.0, 1.0, 1.0)
    pillar = c3d.make_box("CrossV", 6, 6, 0, 66, texture="", material=GOLD)
    arms = c3d.make_box("CrossH", 28, 4.5, 38, 48, texture="", material=GOLD)
    c3d.write_c3d(c3d.C3DFile(objects=[pillar, arms]), OBJDIR / "MillenniumCross.c3d")
    recs.append(record("MillenniumCross.c3d", E, N, altitude(E, N)))


def place_airport_objects(recs):
    E0, N0 = TX.transform(21.38875, 42.059444)        # Stenkovec ARP = runway midpoint
    z = altitude(E0, N0)
    ne, al = (0.512, 0.859), (0.859, -0.512)           # NE-perp + along-runway unit vectors

    def at(perp, along=0.0):
        return (E0 + ne[0] * perp + al[0] * along, N0 + ne[1] * perp + al[1] * along)

    GREY = (0.72, 0.72, 0.74, 1, 1, 1)
    WHITE = (0.95, 0.95, 0.95, 1, 1, 1)
    ORANGE = (0.95, 0.45, 0.10, 1, 1, 1)
    BROWN = (0.45, 0.27, 0.20, 1, 1, 1)
    BLUE = (0.20, 0.35, 0.70, 1, 1, 1)

    px, py = at(40, 0)
    c3d.write_c3d(c3d.C3DFile(objects=windsock(GREY, ORANGE)), OBJDIR / "AP_windsock.c3d")
    recs.append(record("AP_windsock.c3d", px, py, altitude(px, py)))

    hx, hy = at(95, -40)
    walls = c3d.make_box("hg_w", 26, 18, 0, 6, texture=HANGAR_TEX,
                         material=(1.0, 1.0, 1.0, 1.0, 0.0, 0.1))
    roof = gable_roof("hg_r", 26, 18, 6, 9.5, BROWN)
    c3d.write_c3d(c3d.C3DFile(objects=[walls, roof]), OBJDIR / "AP_hangar.c3d")
    recs.append(record("AP_hangar.c3d", hx, hy, altitude(hx, hy)))

    gx, gy = at(-22, -470)
    c3d.write_c3d(c3d.C3DFile(objects=tug(WHITE, BLUE)), OBJDIR / "AP_tug.c3d")
    recs.append(record("AP_tug.c3d", gx, gy, altitude(gx, gy)))
    print(f"  airport: windsock@{px:.0f},{py:.0f}  hangar@{hx:.0f},{hy:.0f}  tug@{gx:.0f},{gy:.0f}")


def main():
    OBJDIR.mkdir(parents=True, exist_ok=True)
    for pat in ("blk*.c3d", "b[0-9]*.c3d", "ZZ_*.c3d", "AP_*.c3d", "MillenniumCross.c3d"):
        for old in OBJDIR.glob(pat):
            old.unlink()
    copy_tex()
    recs = []
    nb = place_buildings(recs)
    place_cross(recs)
    place_airport_objects(recs)
    OBJFILE.write_bytes(b"".join(recs))
    print(f"placed {nb} buildings + cross(gold) + airport objects = {len(recs)} records "
          f"-> {OBJFILE.stat().st_size} B")


if __name__ == "__main__":
    main()
