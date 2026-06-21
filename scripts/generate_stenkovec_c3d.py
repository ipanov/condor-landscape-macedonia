#!/usr/bin/env python3
r"""
Generate TEST Condor 2 ``.c3d`` objects for Stenkovec (LWSN) into
``.sandbox/airports_test/`` -- NOT installed into the Condor landscape.

  StenkovecG.c3d  -- runway plane: a flat grass quad (1200 x 50 m) centred at the
                     airport origin, plus a thin asphalt centre strip, white
                     material, on the ground plane (Z=0). Built with the same
                     object structure as Slovenia2 ``*G.c3d`` (Grass / Asphalt).
  StenkovecO.c3d  -- a windsock: a 'Windsack1' double-sided flag triangle (flat-
                     shaded, like the Slovenia2 reference) on top of a slim
                     'WindsockPole' box, offset to the side of the runway.

Every file is re-parsed and round-tripped after writing, and the writer/reader
are the verified ``scripts/c3d.py``. Run:  python scripts/generate_stenkovec_c3d.py
"""

from __future__ import annotations

from pathlib import Path

import c3d
from c3d import C3DFile, parse_c3d, write_c3d, make_quad, make_triangle, make_box

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / ".sandbox" / "airports_test"

# Stenkovec runway 12/30, grass, 1200 x 50 m (data/airports.json).
RUNWAY_LEN = 1200.0
RUNWAY_WIDTH = 50.0
WHITE = c3d.WHITE_MATERIAL          # (1,1,1,1,1,1)


def build_g() -> C3DFile:
    """Runway plane. Local airport frame: X=east, Y=north, Z=up, origin at centre.

    The strip is laid along +Y (the .apt record carries the true heading, so the
    mesh itself is the canonical north-aligned strip, exactly like the Slovenia2
    G files which store an arbitrarily-oriented local plane)."""
    half_len = RUNWAY_LEN / 2.0          # 600 m
    half_w = RUNWAY_WIDTH / 2.0          # 25 m

    # Grass base: the full runway footprint. UV tiles ~ once per 10 m so the
    # grass texture repeats sensibly (matches the look of the reference Grass).
    grass = make_quad(
        "Grass", half_w, half_len,
        texture="Textures/Herbe_Foncee.dds",   # dark grass, like Northern_Greece DolneniG (a MK grass strip)
        material=WHITE,
        z=0.0,
        uv_scale=(RUNWAY_WIDTH / 10.0, RUNWAY_LEN / 10.0),
    )
    # No asphalt strip: Stenkovec is a GRASS airfield, so a paved centre line looks
    # wrong (the ortho already shows the real grass field). Just the grass rolling
    # plane that the glider sits on and that makes the airport a "real" runway.
    return C3DFile(objects=[grass])


def build_o() -> C3DFile:
    """Windsock: flag triangle + pole, set 40 m east of the runway centreline."""
    px = RUNWAY_WIDTH / 2.0 + 15.0       # 40 m east of centreline
    py = 0.0
    pole_h = 6.0                         # 6 m pole

    pole = make_box(
        "WindsockPole", 0.30, 0.30, 0.0, pole_h,
        texture="",                      # flat-shaded grey pole
        material=(0.6, 0.6, 0.6, 1.0, 1.0, 1.0),
    )
    # the box is centred on the origin in X/Y; translate it to the windsock spot
    for v in pole.vertices:
        v.px += px
        v.py += py

    # flag: a small orange triangle at the top of the pole, double-sided and
    # flat-shaded (zero normals/UVs) exactly like Slovenia2 'Windsack1'.
    z = pole_h
    flag = make_triangle(
        "Windsack1",
        (px,        py,        z),
        (px + 2.5,  py + 0.7,  z - 0.6),
        (px + 2.5,  py - 0.7,  z - 0.6),
        texture="",
        material=(0.85, 0.45, 0.10, 1.0, 0.0, 1.0),   # orange, alpha 1
        double_sided=True,
    )
    return C3DFile(objects=[pole, flag])


def _emit(name: str, c3d_file: C3DFile) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    blob = write_c3d(c3d_file, path)
    # round-trip validation: re-parse what we just wrote and re-serialise
    reparsed = parse_c3d(blob)
    reblob = write_c3d(reparsed)
    ok = reblob == blob
    n_obj = len(reparsed.objects)
    n_v = sum(len(o.vertices) for o in reparsed.objects)
    n_i = sum(len(o.indices) for o in reparsed.objects)
    print(f"  wrote {path}  ({len(blob)} bytes, {n_obj} objs, {n_v} verts, {n_i} idx)  "
          f"round-trip={'OK' if ok else 'FAIL'}")
    for o in reparsed.objects:
        print(f"       - {o.name:14} verts={len(o.vertices):3} idx={len(o.indices):3} "
              f"tex={o.texture!r} mat={tuple(round(x,2) for x in o.material)}")
    if not ok:
        raise SystemExit(f"ROUND-TRIP FAILED for {name}")


def main() -> None:
    print("Generating Stenkovec test c3d objects (NOT installed):")
    _emit("StenkovecG.c3d", build_g())
    _emit("StenkovecO.c3d", build_o())
    print("done.")


if __name__ == "__main__":
    main()
