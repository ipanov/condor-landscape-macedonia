#!/usr/bin/env python3
r"""Model-quality gate — validate a migrated .c3d is placement-ready and not a disaster
BEFORE it goes into the landscape. Catches the exact failures that got rejected:
  - DEGENERATE base footprint (the Toše-Proeski-Arena case: base ring collapsed to a
    point at z-min -> the placement engine cannot texture-match -> forced static guess).
  - MISSING / FLAT texture (missing roofs / untextured generics -> "looks like nothing").
  - too-low poly (crude stand-ins).
  - missing height (flat).
Renders a top+oblique QC image for the final visual call.

Usage:
  python scripts/validate_model.py <c3d> [--texdir DIR] [--min-verts 600] [--min-base-area 25]
Exit 0 = PASS, 1 = FAIL. Prints a JSON verdict. Importable: validate(c3d_path,...).
"""
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import c3d as C3                      # noqa: E402


def _texture_flatness(c3d_path, texdirs):
    """Return (min_std, n_textures, n_missing): the LEAST-varied texture's pixel std.
    A flat/near-constant DDS (std ~0) = baked vertex-colour or a missing roof texture."""
    from PIL import Image
    cf = C3.parse_c3d(c3d_path)
    texs = {o.texture for o in cf.objects if o.texture}
    stds, missing = [], 0
    for t in texs:
        name = Path(t.replace("\\", "/")).name
        p = None
        for d in texdirs:
            cand = Path(d) / name
            if cand.exists():
                p = cand; break
        if p is None:
            missing += 1; continue
        try:
            arr = np.asarray(Image.open(p).convert("RGB")).astype(np.float32)
            stds.append(float(arr.std()))
        except Exception:
            missing += 1
    return (min(stds) if stds else 0.0), len(texs), missing


def validate(c3d_path, texdirs=None, min_verts=600, min_base_area=25.0,
             min_height=2.0, min_tex_std=6.0, render=True):
    from shapely.geometry import MultiPoint
    c3d_path = Path(c3d_path)
    texdirs = texdirs or [c3d_path.parent, c3d_path.parent.parent / "Textures"]
    cf = C3.parse_c3d(c3d_path)
    av = np.array([(v.px, v.py, v.pz) for o in cf.objects for v in o.vertices], float)
    fails, warns = [], []
    nverts = len(av)
    if nverts < min_verts:
        warns.append(f"low poly: {nverts} verts < {min_verts} (likely a crude stand-in)")

    zmin, zmax = av[:, 2].min(), av[:, 2].max()
    height = zmax - zmin
    if height < min_height:
        fails.append(f"no height: z-extent {height:.1f} m (flat)")

    base = av[av[:, 2] <= zmin + 0.75][:, :2]
    try:
        base_area = MultiPoint([tuple(p) for p in base]).convex_hull.area if len(base) >= 3 else 0.0
    except Exception:
        base_area = 0.0
    bdx, bdy = (np.ptp(base[:, 0]), np.ptp(base[:, 1])) if len(base) else (0, 0)
    full_dx, full_dy = np.ptp(av[:, 0]), np.ptp(av[:, 1])
    full_area = float(full_dx * full_dy)
    # DEGENERATE = a BUILDING-sized body (>300 m2 footprint) whose base ring collapsed
    # (tiny absolute, or <5% of the body) -> the migration broke the footprint, the engine
    # can't texture-match it. Size-aware so a small object (aircraft, thin monument) whose
    # base is naturally small (landing gear, a column) is NOT flagged.
    if full_area > 300 and base_area < max(min_base_area, 0.05 * full_area):
        fails.append(f"DEGENERATE base: {bdx:.1f}x{bdy:.1f} m, hull area {base_area:.0f} m2 "
                     f"(full body is {full_dx:.0f}x{full_dy:.0f}) -> engine cannot texture-match; "
                     f"re-migrate so the base ring is the real footprint")

    tex_std, ntex, nmiss = _texture_flatness(c3d_path, texdirs)
    if ntex == 0:
        fails.append("no texture referenced (untextured)")
    elif nmiss:
        fails.append(f"{nmiss}/{ntex} textures MISSING on disk")
    elif tex_std < min_tex_std:
        fails.append(f"FLAT texture (min pixel std {tex_std:.1f} < {min_tex_std}: baked solid "
                     f"colour / missing roof detail)")

    qc_png = None
    if render:
        qc_png = REPO / ".sandbox/qc" / f"{c3d_path.stem}.png"
        qc_png.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([sys.executable, str(REPO / "scripts/render_c3d.py"), str(c3d_path),
                            "--out", str(qc_png), "--view", "both"]
                           + sum([["--texdir", str(d)] for d in texdirs], []),
                           check=True, capture_output=True, timeout=120)
        except Exception as e:                       # noqa: BLE001
            warns.append(f"render failed: {e}")
            qc_png = None

    verdict = {
        "model": c3d_path.name, "ok": not fails, "verts": nverts,
        "height_m": round(float(height), 1),
        "base_dims_m": [round(float(bdx), 1), round(float(bdy), 1)],
        "base_hull_area_m2": round(float(base_area), 0),
        "full_dims_m": [round(float(full_dx), 1), round(float(full_dy), 1)],
        "min_tex_std": round(tex_std, 1), "n_textures": ntex, "n_textures_missing": nmiss,
        "fails": fails, "warns": warns, "qc_render": str(qc_png) if qc_png else None,
    }
    return verdict


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("c3d")
    ap.add_argument("--texdir", action="append", default=[])
    ap.add_argument("--min-verts", type=int, default=600)
    ap.add_argument("--min-base-area", type=float, default=25.0)
    ap.add_argument("--no-render", action="store_true")
    a = ap.parse_args(argv)
    td = a.texdir or None
    v = validate(a.c3d, texdirs=td, min_verts=a.min_verts, min_base_area=a.min_base_area,
                 render=not a.no_render)
    print(json.dumps(v, indent=2))
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
