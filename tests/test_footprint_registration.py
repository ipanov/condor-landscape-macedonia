#!/usr/bin/env python3
"""Tests for scripts/footprint_registration.py

Five test cases (no real data or external resources needed):
  T1 – L-shaped polygon: apply a known rotation (37°) + scale (1.4) + translation
       to make a synthetic target; register; assert recovered ori within 1.5°,
       scale within 3%, IoU > 0.90, and L-corner lands on the correct side.
  T2 – Near-square with a small annex bump on one side: assert front/rear resolved
       (annex lands on the correct side relative to face_azimuth).
  T3 – Perfectly symmetric square + face_azimuth=120°: assert result faces ~120°.
  T4 – Scale recovery: 6×10 model → 9×15 target, expected scale=1.5.
  T5 – Orientation round-trip: 12 planted azimuths on the L-shape, all recovered
       within 2°.

Each test prints  [PASS] / [FAIL]  with details.
Exit code 1 if any test fails.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

# Ensure scripts/ is on the path regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from footprint_registration import register  # noqa: E402

PASS_COUNT = 0
FAIL_COUNT = 0


# ─────────────────────────────────────────────────────────────────────────────
# Test infrastructure
# ─────────────────────────────────────────────────────────────────────────────

def _check(name: str, cond: bool, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    tag = "PASS" if cond else "FAIL"
    msg = f"  [{tag}] {name}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    if cond:
        PASS_COUNT += 1
    else:
        FAIL_COUNT += 1


def _rotate_pts(pts: np.ndarray, theta_deg: float) -> np.ndarray:
    """Rotate a Nx2 array by theta_deg using the COMPASS convention (CW from N).
    Matches the Condor / condor_grid R matrix: local +Y -> world azimuth theta.
    """
    th = math.radians(theta_deg)
    c, s = math.cos(th), math.sin(th)
    R = np.array([[c, s], [-s, c]])
    return (R @ pts.T).T


def _shapely_centroid(pts: np.ndarray) -> np.ndarray:
    """Return the shapely (area-weighted) centroid of the polygon given by pts."""
    poly = Polygon(pts.tolist())
    return np.array([poly.centroid.x, poly.centroid.y])


# ─────────────────────────────────────────────────────────────────────────────
# Shape factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_L_model() -> np.ndarray:
    """L-shaped footprint in LOCAL metres, centred on the vertex mean.

    Vertices (before centring):
      (0,0)-(10,0)-(10,4)-(4,4)-(4,8)-(0,8)
    Area = 10×4 + 4×4 = 56 m².
    The short-arm SE corner at local (10,0) is the unique 'pointy' vertex that
    identifies the L orientation.
    """
    raw = np.array([
        [0, 0], [10, 0], [10, 4], [4, 4], [4, 8], [0, 8],
    ], dtype=float)
    return raw - raw.mean(axis=0)


def _make_near_square_model() -> np.ndarray:
    """10×10 near-square with a 2×2 annex bump on the +Y (North/front) face.

    The annex bump is the front marker (local +Y direction = "front").
    """
    raw = np.array([
        (-5, -5), (5, -5), (5, 5), (1, 5), (1, 7), (-1, 7), (-1, 5), (-5, 5),
    ], dtype=float)
    return raw - raw.mean(axis=0)


def _make_square_model(side: float = 10.0) -> np.ndarray:
    """Perfectly symmetric square, centred at origin."""
    h = side / 2.0
    return np.array([(-h, -h), (h, -h), (h, h), (-h, h)], dtype=float)


# ─────────────────────────────────────────────────────────────────────────────
# T1: L-shaped polygon, known rotation + scale, flip correctness
# ─────────────────────────────────────────────────────────────────────────────

def test_L_shape():
    """T1: L-shape, planted ori=37°, scale=1.4.

    Build the target polygon by applying the known transform, then register and
    check the recovered parameters.  The L-corner check maps the distinctive
    short-arm SE vertex through the recovered transform and verifies it lands
    where the known transform would have put it.
    """
    print("\n[T1] L-shaped polygon — planted ori=37°, scale=1.4")
    known_ori_deg = 37.0
    known_scale = 1.4

    model_pts = _make_L_model()
    world_origin = np.array([534000.0, 4652000.0])

    # Build target: rotate model×scale, translate to world_origin.
    transformed = _rotate_pts(model_pts * known_scale, known_ori_deg) + world_origin
    target_poly = Polygon(transformed.tolist())

    result = register(model_pts.tolist(), target_poly)

    ori_deg = math.degrees(result["ori_rad"])
    scale_r = result["scale"]
    iou = result["iou"]

    ori_err = abs((ori_deg - known_ori_deg + 180.0) % 360.0 - 180.0)
    scale_err_pct = abs(scale_r - known_scale) / known_scale * 100.0

    _check("ori recovered within 1.5°", ori_err <= 1.5,
           f"got {ori_deg:.2f}°, expected {known_ori_deg:.1f}°, err={ori_err:.3f}°")
    _check("scale recovered within 3%", scale_err_pct <= 3.0,
           f"got {scale_r:.4f}, expected {known_scale:.4f}, err={scale_err_pct:.2f}%")
    _check("IoU > 0.90", iou > 0.90, f"iou={iou:.4f}")

    # Flip correctness: transform the L-corner (short-arm SE vertex) via the
    # RECOVERED transform and compare to the expected world position.
    #
    # IMPORTANT: register() re-centres model_pts on the SHAPELY centroid (area-
    # weighted), not the vertex mean.  We must do the same here so local_corner
    # is in the same coordinate frame that register() uses internally.
    mc = _shapely_centroid(model_pts)
    local_corner = model_pts[1] - mc      # (10,0) - shapely_centroid, in register's frame
    local_scaled = local_corner * scale_r
    th = result["ori_rad"]
    c_th, s_th = math.cos(th), math.sin(th)
    # Condor convention: world_vec = R · local_vec
    world_corner = np.array([
        c_th * local_scaled[0] + s_th * local_scaled[1],
        -s_th * local_scaled[0] + c_th * local_scaled[1],
    ]) + np.array([result["posE"], result["posN"]])

    # Expected: same corner in the same local frame, via the KNOWN transform,
    # placed at the target polygon's shapely centroid.
    target_c = np.array([target_poly.centroid.x, target_poly.centroid.y])
    local_corner_known_scaled = local_corner * known_scale
    expected_corner = _rotate_pts(local_corner_known_scaled[None, :], known_ori_deg)[0] + target_c

    corner_err = float(np.linalg.norm(world_corner - expected_corner))
    _check("L-corner lands on correct side (< 0.5 m)", corner_err < 0.5,
           f"corner error = {corner_err:.4f} m")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# T2: Near-square with annex bump — front/rear resolved
# ─────────────────────────────────────────────────────────────────────────────

def test_near_square_annex():
    """T2: Near-square 10×10 with a 2×2 annex bump on the +Y (North/front) face.

    The model's +Y is its "front" (the annex side).  We plant the target at
    210° azimuth and use face_azimuth=210° to resolve the ambiguity.

    Checks:
      - IoU > 0.85 (shape matched well).
      - The model's +Y world direction is within 30° of 210°.
      - The annex (front bump) in world space is in the expected direction.
    """
    print("\n[T2] Near-square with annex — front/rear resolved via face_azimuth")
    planted_ori_deg = 210.0
    planted_scale = 1.2
    face_az = 210.0

    model_pts = _make_near_square_model()
    world_origin = np.array([534200.0, 4652200.0])

    transformed = _rotate_pts(model_pts * planted_scale, planted_ori_deg) + world_origin
    target_poly = Polygon(transformed.tolist())

    result = register(model_pts.tolist(), target_poly, face_azimuth=face_az)

    ori_deg = math.degrees(result["ori_rad"])
    iou = result["iou"]

    _check("IoU > 0.85", iou > 0.85, f"iou={iou:.4f}")

    # +Y world direction = ori_rad (compass); check within 30° of planted_ori.
    face_err = abs((ori_deg - face_az + 180.0) % 360.0 - 180.0)
    _check("front (+Y) direction within 30° of face_azimuth=210°", face_err <= 30.0,
           f"ori={ori_deg:.2f}°, expected ≈{face_az:.1f}°, err={face_err:.2f}°")

    # Annex tip direction: local coords of annex top ≈ (0, 7) re-centred on shapely centroid.
    mc = _shapely_centroid(model_pts)
    annex_local = np.array([0.0, 7.0]) - mc
    annex_scaled = annex_local * result["scale"]
    th = result["ori_rad"]
    c_th, s_th = math.cos(th), math.sin(th)
    annex_world = np.array([
        c_th * annex_scaled[0] + s_th * annex_scaled[1],
        -s_th * annex_scaled[0] + c_th * annex_scaled[1],
    ]) + np.array([result["posE"], result["posN"]])
    vec = annex_world - np.array([result["posE"], result["posN"]])
    annex_dir = math.degrees(math.atan2(vec[0], vec[1])) % 360.0   # compass atan2(E,N)
    annex_err = abs((annex_dir - face_az + 180.0) % 360.0 - 180.0)
    _check("annex (front bump) in correct world direction (< 40°)", annex_err <= 40.0,
           f"annex_dir={annex_dir:.1f}°, planted={face_az:.1f}°, err={annex_err:.1f}°")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# T3: Perfectly symmetric square + face_azimuth=120°
# ─────────────────────────────────────────────────────────────────────────────

def test_square_face_azimuth():
    """T3: Symmetric 10×10 square + face_azimuth=120° breaks the 4-fold symmetry.

    A square's IoU is identical for 0°/90°/180°/270° rotations, so face_azimuth
    must select the orientation.  The nearest 90°-step to 120° is 90° (30° away),
    so the result should be within 45° of 120°.
    """
    print("\n[T3] Symmetric square + face_azimuth=120° — orientation resolved")
    face_az = 120.0
    side = 10.0

    model_pts = _make_square_model(side)
    world_origin = np.array([534400.0, 4652400.0])
    target_poly = Polygon((model_pts + world_origin).tolist())

    result = register(model_pts.tolist(), target_poly, face_azimuth=face_az)

    ori_deg = math.degrees(result["ori_rad"]) % 360.0
    iou = result["iou"]

    _check("IoU > 0.95 (perfect square overlap)", iou > 0.95, f"iou={iou:.4f}")

    face_err = abs((ori_deg - face_az + 180.0) % 360.0 - 180.0)
    _check("ori within 45° of face_azimuth=120°", face_err <= 45.0,
           f"ori={ori_deg:.2f}°, face_az={face_az:.1f}°, err={face_err:.2f}°")

    pos_err = math.hypot(result["posE"] - world_origin[0],
                         result["posN"] - world_origin[1])
    _check("placement centroid within 0.01 m of target centroid", pos_err < 0.01,
           f"pos error = {pos_err:.6f} m")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# T4: Scale recovery — rectangle 6×10 vs 9×15 → scale 1.5
# ─────────────────────────────────────────────────────────────────────────────

def test_scale_recovery():
    """T4: Scale recovery from area ratio only (no rotation involved).

    Model: axis-aligned rectangle 6×10 m (area=60 m²).
    Target: axis-aligned rectangle 9×15 m (area=135 m²).
    Expected scale = sqrt(135/60) = sqrt(2.25) = 1.5.
    """
    print("\n[T4] Scale recovery — 6×10 model -> 9×15 target, expected scale=1.5")
    model_pts = np.array([(-3, -5), (3, -5), (3, 5), (-3, 5)], dtype=float)
    world_origin = np.array([534600.0, 4652600.0])
    target_pts = np.array([(-4.5, -7.5), (4.5, -7.5), (4.5, 7.5), (-4.5, 7.5)], dtype=float)
    target_poly = Polygon((target_pts + world_origin).tolist())

    result = register(model_pts.tolist(), target_poly)

    scale_err = abs(result["scale"] - 1.5) / 1.5 * 100.0
    _check("scale recovered within 3% of 1.5", scale_err <= 3.0,
           f"got {result['scale']:.4f}, expected 1.5, err={scale_err:.2f}%")
    _check("IoU > 0.90", result["iou"] > 0.90, f"iou={result['iou']:.4f}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# T5: Orientation round-trip — 12 azimuths on the L-shape
# ─────────────────────────────────────────────────────────────────────────────

def test_orientation_round_trip():
    """T5: Orientation round-trip for a range of azimuths on the asymmetric L-shape.

    For each planted azimuth, the recovered ori must be within 2.0°.
    """
    print("\n[T5] Orientation round-trip — 12 planted azimuths on L-shape")
    model_pts = _make_L_model()
    world_origin = np.array([535000.0, 4653000.0])
    test_azimuths = [0, 15, 37, 60, 90, 120, 147, 180, 210, 270, 315, 350]
    passed = 0
    total = len(test_azimuths)
    for planted_deg in test_azimuths:
        transformed = _rotate_pts(model_pts, planted_deg) + world_origin
        target_poly = Polygon(transformed.tolist())
        result = register(model_pts.tolist(), target_poly)
        ori_deg = math.degrees(result["ori_rad"]) % 360.0
        err = abs((ori_deg - planted_deg + 180.0) % 360.0 - 180.0)
        ok = err <= 2.0
        if ok:
            passed += 1
        else:
            print(f"    azimuth {planted_deg}° -> recovered {ori_deg:.2f}°, err={err:.2f}°")
    _check(f"all {total} azimuths recovered within 2.0°", passed == total,
           f"{passed}/{total} passed")
    return passed, total


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 64)
    print("footprint_registration — TEST SUITE")
    print("=" * 64)

    r1 = test_L_shape()
    r2 = test_near_square_annex()
    r3 = test_square_face_azimuth()
    r4 = test_scale_recovery()
    r5 = test_orientation_round_trip()

    print()
    print("=" * 64)
    print(f"RESULTS: {PASS_COUNT} PASS  /  {FAIL_COUNT} FAIL  "
          f"(total {PASS_COUNT + FAIL_COUNT} checks)")
    print("=" * 64)

    print()
    print("API SUMMARY (register() return dict from T1):")
    for k, v in r1.items():
        if not k.startswith("_"):
            print(f"  {k}: {v!r}")
    print("  [diagnostics]")
    print(f"  _mrr_ratio: {r1['_mrr_ratio']!r}  (long-edge scale cross-check)")
    print(f"  _mirrored:  {r1['_mirrored']!r}")

    if FAIL_COUNT > 0:
        sys.exit(1)
