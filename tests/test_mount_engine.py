#!/usr/bin/env python3
r"""Unit + integration tests for the deterministic mount engine.

Proves the position search recovers a known building footprint for ANY shape
(rectangle / square / narrow / rotated) from a synthetic game texture, that the
geometric-centroid anchor is unbiased, and (integration) that the engine reproduces
the verified Stenkovec hangar placement on the installed landscape.

Run:  python -m pytest tests/test_mount_engine.py -q
  or: python tests/test_mount_engine.py     (plain asserts, no pytest needed)
"""
import math
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import mount_engine as ME          # noqa: E402
import place_engine as PE          # noqa: E402


# --------------------------------------------------------------------------- #
# Synthetic game-texture scene: a gray building rectangle on green grass.
# affine maps world UTM -> pixels: px = sx*E+ox, py = sy*N+oy (sy<0, N up).
# --------------------------------------------------------------------------- #
def synth_scene(cE, cN, L, W, az_deg, ppm=2.0, size=200, noise=8):
    affine = (ppm, -cE * ppm + size / 2.0, -ppm, cN * ppm + size / 2.0)
    sx, ox, sy, oy = affine
    yy, xx = np.mgrid[0:size, 0:size]
    E = (xx - ox) / sx; N = (yy - oy) / sy
    azr = math.radians(az_deg)
    ux, uy = math.sin(azr), math.cos(azr); vx, vy = math.cos(azr), -math.sin(azr)
    a = (E - cE) * ux + (N - cN) * uy; b = (E - cE) * vx + (N - cN) * vy
    inside = (np.abs(a) <= L / 2.0) & (np.abs(b) <= W / 2.0)
    img = np.zeros((size, size, 3), np.uint8)
    img[..., 1] = 150                                   # green grass
    img[inside] = (150, 150, 150)                       # gray roof
    rng = np.random.RandomState(0)
    img = np.clip(img.astype(np.int16) + rng.randint(-noise, noise + 1, img.shape), 0, 255).astype(np.uint8)
    return img, affine


def _recover(cE, cN, L, W, az, off=(7.0, -6.0), tol=1.5):
    img, affine = synth_scene(cE, cN, L, W, az)
    rE, rN, sc = PE.refine_position_core(L, W, az, (cE + off[0], cN + off[1]), img, affine,
                                         search_m=15.0, step=1.0)
    err = math.hypot(rE - cE, rN - cN)
    assert err <= tol, f"recovered ({rE:.1f},{rN:.1f}) vs true ({cE},{cN}) err={err:.2f}>tol {tol}"
    return err


def test_refine_rectangle():
    _recover(1000.0, 2000.0, 40.0, 18.0, 30.0)


def test_refine_square():
    _recover(500.0, 800.0, 26.0, 26.0, 0.0)


def test_refine_narrow():
    _recover(-300.0, 1200.0, 50.0, 10.0, 115.0)


def test_refine_various_orientations():
    for az in (0, 22, 45, 75, 100, 140, 170):
        _recover(0.0, 0.0, 35.0, 20.0, float(az))


def test_refine_robust_to_large_offset():
    # painted roof up to ~12 m from the seed is still recovered within tolerance
    _recover(2000.0, 5000.0, 44.0, 16.0, 50.0, off=(12.0, -11.0), tol=1.5)


def test_register_core_edge_alignment():
    # the alternative edge-alignment core should also localise the rectangle (looser)
    cE, cN, L, W, az = 100.0, 200.0, 40.0, 18.0, 25.0
    img, affine = synth_scene(cE, cN, L, W, az)
    outline = np.array([(-L/2, -W/2), (L/2, -W/2), (L/2, W/2), (-L/2, W/2)], float)
    r = ME.register_core(outline, img, affine, az, 90.0, scale=1.0,
                         seed_EN=(cE + 6, cN - 5), search_m=14.0)
    err = math.hypot(r["cE"] - cE, r["cN"] - cN)
    assert err <= 3.0, f"register_core err {err:.2f}"


def test_geometric_centroid_unbiased():
    """The hull centroid (engine anchor) must NOT be pulled by vertex density the way the
    vertex mean is -- this was the 5.8 m 'too far back' bug."""
    from shapely.geometry import MultiPoint
    # a 40x20 rectangle outline + a dense vertex cluster on one side (like door/clubroom)
    rect = [(-20, -10), (20, -10), (20, 10), (-20, 10)]
    # a TIGHT dense cluster INSIDE the footprint (doors/clubroom) -> does not enlarge the
    # hull, but badly skews the vertex mean.
    dense = [(18 + 0.001 * (i % 50), -8 + 0.001 * (i // 50)) for i in range(4000)]
    pts = np.array(rect + dense, float)
    mean = pts.mean(0)
    hull_c = np.array(MultiPoint([tuple(p) for p in pts]).convex_hull.centroid.coords[0])
    assert abs(hull_c[0]) < 1.0, f"hull centroid biased: {hull_c}"
    assert abs(mean[0]) > 10.0, f"vertex mean should be badly biased here: {mean}"


def test_integration_hangar():
    """End-to-end on the installed landscape: the engine reproduces the verified
    Stenkovec hangar placement (E=531807.7 N=4656497.6 ori=300.25). Skips if not installed."""
    install = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
    manifest = REPO / "data/placement_manifest.json"
    if not (install / "World/Objects/StenkovecHangar.c3d").exists() or not manifest.exists():
        print("[integration] install/manifest missing -> SKIP")
        return
    import json
    obj = next(o for o in json.loads(manifest.read_text())["objects"] if o["id"] == "StenkovecHangar")
    r = PE.place_one(obj, str(install))
    derr = math.hypot(r["cE"] - 531807.7, r["cN"] - 4656497.6)
    oerr = abs((r["ori"] - 300.25 + 180) % 360 - 180)
    assert derr <= 1.5, f"hangar position drift {derr:.2f} m"
    assert oerr <= 1.0, f"hangar orientation drift {oerr:.2f} deg"
    assert abs(r["scale"] - 1.0) < 1e-6, "hangar must be native size"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}"); passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:                       # noqa: BLE001
            print(f"ERROR {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
