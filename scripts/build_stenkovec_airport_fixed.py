#!/usr/bin/env python3
r"""
Build a CORRECT Stenkovec (LWSN) airport ground+object model into
``.sandbox/airports_fixed/`` (NOT installed). Fixes the "DISASTER" report:
grass runway at the wrong heading, crossing the painted runway, z-fighting.

ROOT CAUSE (verified)
---------------------
The installed ``StenkovecG.c3d`` modelled the grass quad **pre-rotated to the
runway azimuth (120.55 deg) in the LOCAL c3d frame**. But Condor's airport
convention -- proven against Slovenia2 PTUJ / AJDOVSCINA / SLOVENJ GRADEC /
LESCE-BLED, whose ``.apt`` Directions are 109/273/143/134 deg yet whose G-file
runway strips all sit within 0-3 deg of the LOCAL +Y axis -- is:

    The G-file runway strip is modelled ALONG LOCAL +Y (north). Condor then
    rotates the whole airport by the ``.apt`` Direction.

So the pre-rotated quad was being rotated a SECOND time by the .apt dir (121 deg)
=> it rendered at ~241 deg, crossing the real painted runway, and being coplanar
(Z=0) with the ortho's painted strip it z-fought ("blinking green").

THE FIX
-------
Model the strip along +Y exactly like the Macedonian Dolneni reference
(E:/Condor2/Landscapes/Northern_Greece/Airports/DolneniG.c3d). The installed
``.apt`` Direction = 121 deg (chart 120.55; the painted-runway azimuth MEASURED
from the ortho t0704.dds via Canny edge-fit of the two long parallel airfield
edges = ~120.7 deg, i.e. the .apt heading is already correct) rotates it onto the
painted runway. No double rotation, no crossing.

  StenkovecG.c3d : 'Grass3D' strip + 'Asphaltpaint' centreline, along +Y, Z=0,
                   Dolneni materials/winding. Sized to the real 1200 x 50 m strip.
  StenkovecO.c3d : 'Pole' (8 m decagon mast) + 'Windsack1' (triangle, circumradius
                   0.04 m -> ~4 m animated windsock), replicating DolneniO.c3d
                   structure/material exactly, placed 65 m to the +X side of the
                   centre (Dolneni offset), i.e. beside the runway after rotation.

Z-FIGHT NOTE: the airport has NO underlying ground polygon (GUIDE p.40 #7); the
painted runway base IS the ortho texture. Slovenia2/Dolneni keep grass+paint at
Z~=0 because they are engine DECALS, not a second coplanar mesh -- so Z=0 here
does NOT z-fight (the previous blinking came from the *crossing wrong-heading*
quad, not from the Z value). We keep Z=0 to match every verified reference.

Outputs are round-tripped through scripts/c3d.py after writing. No GUI, no hash,
nothing installed.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3d
from c3d import Vertex as V, C3DObject, C3DFile, write_c3d, parse_c3d

OUT = Path(__file__).resolve().parent.parent / ".sandbox" / "airports_fixed"

# --- Real Stenkovec runway 12/30 (data/airports.json): grass, 1200 x 50 m ------
RUNWAY_LEN = 1200.0
RUNWAY_WIDTH = 50.0

# --- Dolneni (Macedonian grass strip) materials, copied verbatim ---------------
GRASS_TEX = "Textures/Herbe_Foncee.dds"            # ignored in-game; kept for parity
GRASS_MAT = (0.78985381, 0.81333333, 0.69404447, 1.0, 0.16898538, 0.2)
PAINT_MAT = (1.0, 1.0, 1.0, 1.0, 0.1, 0.2)
POLE_MAT = (0.208, 0.208, 0.208, 1.0, 0.0, 0.1)    # DolneniO Pole/Windsack material


def grass_strip() -> C3DObject:
    """Grass decal strip, centred at origin, long axis along +Y (NORTH).

    Double-sided (both +Z and -Z faces) exactly like DolneniG 'Grass3D'. Condor
    swaps in its built-in seamless grass; the .apt Direction rotates this onto the
    painted runway."""
    hw, hl = RUNWAY_WIDTH / 2.0, RUNWAY_LEN / 2.0      # 25 x 600 m
    sx, sy = RUNWAY_WIDTH / 10.0, RUNWAY_LEN / 10.0    # UV tiles ~1 per 10 m
    # corners CCW seen from above: SW, SE, NE, NW  (x=E, y=N)
    corners = [(-hw, -hl, 0.0, 0.0), (hw, -hl, sx, 0.0),
               (hw, hl, sx, sy), (-hw, hl, 0.0, sy)]
    verts = [V(x, y, 0.0, 0.0, 0.0, 1.0, u, v) for (x, y, u, v) in corners]      # top (+Z)
    verts += [V(x, y, 0.0, 0.0, 0.0, -1.0, u, v) for (x, y, u, v) in corners]    # bottom (-Z)
    idx = [0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6]
    return C3DObject(name="Grass3D", texture=GRASS_TEX, material=GRASS_MAT,
                     vertices=verts, indices=idx)


def centreline_paint() -> C3DObject:
    """A thin white centreline 'Asphaltpaint' strip along +Y, Z=0 (decal).

    Marks the runway axis so the painted runway has a crisp centre after rotation.
    1 m wide, runs the runway length minus 60 m of displaced-threshold margin."""
    hw, hl = 0.6, (RUNWAY_LEN / 2.0 - 30.0)
    corners = [(-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl)]
    verts = [V(x, y, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0) for (x, y) in corners]
    idx = [0, 1, 2, 0, 2, 3]
    return C3DObject(name="Asphaltpaint", texture="", material=PAINT_MAT,
                     vertices=verts, indices=idx)


def pole(cx: float, cy: float, r: float = 0.05, z0: float = 0.0, z1: float = 8.0,
         n: int = 10) -> C3DObject:
    """8 m decagon mast at (cx, cy), matching DolneniO 'Pole' (decagon, 0..8 m)."""
    verts, idx = [], []
    ring = [(cx + r * math.cos(2 * math.pi * i / n),
             cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        nx, ny = math.cos(2 * math.pi * (i + 0.5) / n), math.sin(2 * math.pi * (i + 0.5) / n)
        base = len(verts)
        verts += [V(a[0], a[1], z0, nx, ny, 0, 0, 0), V(b[0], b[1], z0, nx, ny, 0, 0, 0),
                  V(b[0], b[1], z1, nx, ny, 0, 0, 0), V(a[0], a[1], z1, nx, ny, 0, 0, 0)]
        idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    cap = len(verts)
    verts.append(V(cx, cy, z1, 0, 0, 1, 0, 0))
    r0 = len(verts)
    for (x, y) in ring:
        verts.append(V(x, y, z1, 0, 0, 1, 0, 0))
    for i in range(n):
        idx += [cap, r0 + i, r0 + (i + 1) % n]
    return C3DObject(name="Pole", texture="", material=POLE_MAT, vertices=verts, indices=idx)


def windsack(cx: float, cy: float, z: float = 8.02, rad: float = 0.04) -> C3DObject:
    """'Windsack1' equilateral-ish triangle on the mast top; circumradius ``rad``
    metres -> windsock length ~ rad*100 m (4 cm -> 4 m). Single triangle, zero
    normals, material = Dolneni's -- Condor turns the NAMED object into the live
    animated windsock (GUIDE p.41 / AERO p.79). Winding matches DolneniO."""
    # three points on a circle of radius `rad` about (cx,cy); centroid = attach pt
    pts = [(cx,                                     cy + rad),
           (cx + rad * math.cos(math.radians(30)),  cy - rad * math.sin(math.radians(30))),
           (cx - rad * math.cos(math.radians(30)),  cy - rad * math.sin(math.radians(30)))]
    verts = [V(p[0], p[1], z, 0.0, 0.0, 1.0, 0.0, 0.0) for p in pts]
    return C3DObject(name="Windsack1", texture="", material=POLE_MAT,
                     vertices=verts, indices=[0, 1, 2])


def emit(name: str, cf: C3DFile) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    blob = write_c3d(cf, path)
    rt = parse_c3d(blob)
    ok = write_c3d(rt) == blob
    nv = sum(len(o.vertices) for o in rt.objects)
    print(f"  wrote {path}  ({len(blob)} B, {len(rt.objects)} objs, {nv} verts)  "
          f"round-trip={'OK' if ok else 'FAIL'}")
    for o in rt.objects:
        print(f"     - {o.name:13} v={len(o.vertices):3} i={len(o.indices):3} mat={tuple(round(x,2) for x in o.material)}")
    if not ok:
        raise SystemExit("round-trip FAILED")


def main() -> None:
    print("Building FIXED Stenkovec G/O (along +Y; .apt dir=121 rotates onto painted runway):")
    emit("StenkovecG.c3d", C3DFile(objects=[grass_strip(), centreline_paint()]))
    # windsock 65 m to +X side of centre (matches Dolneni's +65 m offset); after the
    # .apt 121 deg rotation this lands ~65 m to the NE of the runway, clear of the strip.
    ox, oy = 65.0, -2.0
    emit("StenkovecO.c3d", C3DFile(objects=[pole(ox, oy), windsack(ox, oy)]))
    print("done.")


if __name__ == "__main__":
    main()
