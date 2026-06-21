#!/usr/bin/env python3
"""INCREMENTAL building placement -- EXACT footprints (validate-as-you-go).

Every MK-cadastre building (central-Skopje proof set) is extruded from its REAL
footprint polygon to its REAL cadastre height and written as its own prism .c3d in
World/Objects/, then referenced once in the landscape MacedoniaSkopje.obj. The mesh
IS the outline, so placement carries no rotation (ori=0, scale=1) -- position +
planform + orientation come straight from the cadastre geometry, which the overlay
(scripts/validate_overlay.py) confirmed lands on the painted rooftops sub-pixel.

Local frame: x=east, y=north, relative to each footprint's centroid; placed at
posx=origin_E-centroidE, posy=centroidN-origin_N (verified .obj convention). Flat
grey concrete for now (precision pass); facade textures/architecture come next.
No GUI, no hash (objects are not hashed).
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3d
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
CAD = ROOT / ".sandbox" / "buildings" / "cadastre_buildings.geojson"
INSTALL = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
OBJDIR = INSTALL / "World" / "Objects"
OBJFILE = INSTALL / "MacedoniaSkopje.obj"
ORIGIN_E, ORIGIN_N = 576000.0, 4631040.0          # SE origin (CCRR col0=east, row0=south)
GREY = (0.58, 0.57, 0.55, 1.0, 1.0, 1.0)           # flat concrete grey, no texture


def main():
    OBJDIR.mkdir(parents=True, exist_ok=True)
    for old in OBJDIR.glob("blk*.c3d"):               # clear the generic-box pass
        old.unlink()
    for old in OBJDIR.glob("b[0-9]*.c3d"):
        old.unlink()

    feats = json.load(open(CAD, encoding="utf-8"))["features"]
    recs = []
    skipped = 0
    for i, f in enumerate(feats):
        g = shape(f["geometry"])
        if g.is_empty:
            skipped += 1
            continue
        poly = max(g.geoms, key=lambda p: p.area) if g.geom_type == "MultiPolygon" else g
        ring = list(poly.exterior.coords)
        if len(ring) < 4:
            skipped += 1
            continue
        p = f["properties"]
        h = float(p.get("height") or (int(p.get("num_floors", 1) or 1) * 3 + 3))
        cE, cN = poly.centroid.x, poly.centroid.y
        local = [(x - cE, y - cN) for (x, y) in ring]   # east-right, north-up, centroid origin
        name = f"b{i:04d}"
        obj = c3d.make_prism(name, local, h, texture="", material=GREY)
        c3d.write_c3d(c3d.C3DFile(objects=[obj]), OBJDIR / f"{name}.c3d")
        posx, posy, posz = ORIGIN_E - cE, cN - ORIGIN_N, 0.0
        rec = (struct.pack("<5f", posx, posy, posz, 1.0, 0.0)
               + bytes([len(name)]) + name.encode("ascii").ljust(131, b"\x00"))
        recs.append(rec)
    OBJFILE.write_bytes(b"".join(recs))
    print(f"extruded {len(recs)} cadastre buildings -> {OBJDIR} (per-building prism .c3d)")
    print(f"  skipped {skipped} (empty/degenerate)")
    print(f"  {OBJFILE} = {OBJFILE.stat().st_size} bytes ({len(recs)} x 152)")
    # round-trip the first written prism as a sanity check
    first = recs and f"b{[i for i,f in enumerate(feats)][0]:04d}"
    sample = sorted(OBJDIR.glob("b*.c3d"))[0]
    rt = c3d.parse_c3d(str(sample))
    o = rt.objects[0]
    print(f"  c3d verify: {sample.name} -> {o.name}, {len(o.vertices)} verts, "
          f"{len(o.indices)} idx, mat={tuple(round(x,2) for x in o.material)}")


if __name__ == "__main__":
    main()
