#!/usr/bin/env python3
"""Place SAM3-detected building rooftops via the CALIBRATED source-of-truth transform.

Footprints were detected IN the installed texture (SAM3), so with the verified
condor_grid transform (SE-corner anchor + footprint_to_local + ori=0) they land on
the painted rooftops by construction. Colour from the ortho, height from a footprint-
area heuristic (cadastre-height join is a later refinement). Writes World/Objects + .obj
and a self-validation overlay (decoded records re-drawn on the texture). No GUI/hash.
"""
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import condor_grid as G
import c3d

ROOT = Path(__file__).resolve().parent.parent
SAM = ROOT / ".sandbox" / "sam_buildings_t0703.geojson"
DEM = ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305_flat.raw"
INSTALL = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
OBJDIR = INSTALL / "World" / "Objects"
TEX = INSTALL / "Textures"
OBJFILE = INSTALL / "MacedoniaSkopje.obj"
ULX_W, ULY_N, SOUTH0, PATCH, PXN, NCOL, XDIM, DEMW = 506880.0, 4700160.0, 4631040.0, 5760.0, 2048, 12, 30.0, 2305
_dem = np.fromfile(DEM, dtype="<u2").reshape(DEMW, DEMW)
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


def main():
    OBJDIR.mkdir(parents=True, exist_ok=True)
    for old in OBJDIR.glob("b[0-9]*.c3d"):
        old.unlink()
    feats = json.load(open(SAM, encoding="utf-8"))["features"]
    recs, placed = [], []
    for i, f in enumerate(feats):
        g = shape(f["geometry"])
        poly = max(g.geoms, key=lambda p: p.area) if g.geom_type == "MultiPolygon" else g
        if poly.is_empty or poly.area < 30 or len(poly.exterior.coords) < 4:
            continue
        cE, cN = poly.centroid.x, poly.centroid.y
        h = min(max(math.sqrt(poly.area) * 0.5, 6.0), 28.0)
        local = G.footprint_to_local(list(poly.exterior.coords), (cE, cN))   # true E/N, ori=0
        r, gr, b = roof_colour(poly)
        nm = f"b{i:04d}.c3d"
        obj = c3d.make_prism(nm[:-4], local, h, texture="", material=(r, gr, b, 1.0, 1.0, 1.0))
        c3d.write_c3d(c3d.C3DFile(objects=[obj]), OBJDIR / nm)
        posX, posY = G.obj_record_xy(cE, cN)                                  # CALIBRATED anchor
        recs.append(struct.pack("<5f", posX, posY, altitude(cE, cN), 1.0, 0.0)
                    + bytes([len(nm)]) + nm.encode("ascii").ljust(131, b"\x00"))
        placed.append(poly)
    OBJFILE.write_bytes(b"".join(recs))
    print(f"placed {len(recs)} SAM3 buildings via calibrated transform -> {OBJFILE.stat().st_size} B")

    # self-validation: decode the installed records back and overlay on t0703
    d = OBJFILE.read_bytes()
    col, row = 4, 3
    arr = Image.open(TEX / "t0703.dds").convert("RGB").resize((PXN, PXN))
    dr = ImageDraw.Draw(arr)
    west, north = ULX_W + col * PATCH, SOUTH0 + (row + 1) * PATCH
    for k in range(len(d) // 152):
        b = k * 152
        px, py = struct.unpack_from("<2f", d, b)
        E, N = G.obj_world_xy(px, py)
        x, y = (E - west) / PATCH * PXN, (north - N) / PATCH * PXN
        if 0 <= x < PXN and 0 <= y < PXN:
            dr.ellipse([x - 3, y - 3, x + 3, y + 3], outline=(255, 0, 0), width=1)
    out = ROOT / ".sandbox" / "VALIDATE_sam_placed_on_texture.png"
    arr.crop((600, 500, 1700, 1700)).save(out)
    print(f"validation overlay -> {out}")


if __name__ == "__main__":
    main()
