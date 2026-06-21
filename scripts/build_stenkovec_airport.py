#!/usr/bin/env python3
"""Build Stenkovec's missing airport O model = Pole + Windsack1, replicating the
Dolneni (Macedonian grass strip) reference in Northern_Greece exactly:
  - 'Pole'      : an 8 m decagon mast, dark-grey material (0.21,0.21,0.21,1,0,0.1)
  - 'Windsack1' : a 3-vertex triangle at the mast top -- Condor turns the object NAMED
                  'Windsack1' into the live animated windsock (GUIDE p.41 / AERO p.79).
Having a real <Name>O.c3d (+ the G.c3d + flattened strip) is what makes Condor treat
Stenkovec as a real runway -> windsock shows AND the aerotow tug spawns. No GUI/hash.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3d

AD = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Airports")
MAT = (0.21, 0.21, 0.21, 1.0, 0.0, 0.1)        # Dolneni windsock material, verbatim
V = c3d.Vertex
RW_HDG = 120.55                                 # UTM azimuth of the painted runway


def cylinder(name, cx, cy, r, z0, z1, n=10, mat=MAT):
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
    cap = len(verts)                            # top cap fan
    verts.append(V(cx, cy, z1, 0, 0, 1, 0, 0))
    r0 = len(verts)
    for (x, y) in ring:
        verts.append(V(x, y, z1, 0, 0, 1, 0, 0))
    for i in range(n):
        idx += [cap, r0 + i, r0 + (i + 1) % n]
    return c3d.C3DObject(name=name, texture="", material=mat, vertices=verts, indices=idx)


def build_g():
    """Runway ground model: a single 'Grass' quad ALIGNED to the runway azimuth (the
    old one ran N-S). Condor ignores the texture and renders its built-in grass; the
    runway markings come from the ortho. Sized to the flattened strip (1540 x 60 m)."""
    h = math.radians(RW_HDG)
    al = (math.sin(h), math.cos(h))                 # along runway
    pe = (math.sin(h - math.pi / 2), math.cos(h - math.pi / 2))  # perpendicular
    L, W = 770.0, 30.0

    def corner(s, t):
        return (al[0] * s + pe[0] * t, al[1] * s + pe[1] * t)

    c = [corner(-L, -W), corner(L, -W), corner(L, W), corner(-L, W)]
    verts = [V(x, y, 0.0, 0, 0, 1, 0, 0) for (x, y) in c]
    return c3d.C3DObject(name="Grass", texture="", material=(1, 1, 1, 1, 1, 1),
                         vertices=verts, indices=[0, 1, 2, 0, 2, 3])


def main():
    AD.mkdir(parents=True, exist_ok=True)
    gblob = c3d.write_c3d(c3d.C3DFile(objects=[build_g()]), AD / "StenkovecG.c3d")
    print(f"wrote {AD/'StenkovecG.c3d'} ({len(gblob)} B) -- Grass quad aligned to {RW_HDG} deg")
    # place the windsock ~40 m perpendicular (NE side) of the runway midpoint
    perp = math.radians(RW_HDG - 90.0)
    ox, oy = 40.0 * math.sin(perp), 40.0 * math.cos(perp)
    pole = cylinder("Pole", ox, oy, 0.12, 0.0, 8.0)
    # Windsack1: small triangle at the mast top; circumradius ~0.04 m -> ~4 m windsock
    tri = [V(ox - 0.03, oy + 0.02, 8.02, 0, 0, 1, 0, 0),
           V(ox + 0.04, oy + 0.02, 8.02, 0, 0, 1, 0, 0),
           V(ox + 0.00, oy - 0.04, 8.02, 0, 0, 1, 0, 0)]
    windsack = c3d.C3DObject(name="Windsack1", texture="", material=MAT,
                             vertices=tri, indices=[0, 1, 2])
    AD.mkdir(parents=True, exist_ok=True)
    blob = c3d.write_c3d(c3d.C3DFile(objects=[pole, windsack]), AD / "StenkovecO.c3d")
    rt = c3d.parse_c3d(blob)
    print(f"wrote {AD/'StenkovecO.c3d'} ({len(blob)} B) objects={[o.name for o in rt.objects]} "
          f"windsock@local {ox:.1f},{oy:.1f}")


if __name__ == "__main__":
    main()
