#!/usr/bin/env python3
r"""
Footprint-registration engine for Condor 2 object placement.

Registers a 3-D building model's base outline onto a real-world cadastre footprint
polygon, recovering the correct position, uniform scale, and full orientation
(including front/rear) via shape-matching rather than relying on a symmetric
bounding box.

CONVENTION (matches condor_grid.py throughout):
  - Local coords: x = East, y = North, metres, centred on the model origin.
  - `ori` is a COMPASS AZIMUTH in radians (clockwise from North).
  - World placement: world = ref + R · local
        where  R = [[cos(ori), sin(ori)],
                    [-sin(ori), cos(ori)]]
    so   local +Y (North)  ->  world azimuth ori
         local +X (East)   ->  world azimuth ori + 90°
  - This is identical to the Condor `.obj` record convention verified in
    condor_grid.py (Slovenia2 calibration, 371 buildings, residual ~0 m).

PUBLIC API
----------
register(model_outline, target_polygon, face_azimuth=None) -> dict
    model_outline  : list[(x_E, y_N)]  – base vertices in LOCAL metres about the
                     model's own centroid (pass the convex hull or exterior ring of
                     the c3d base; centroid is recomputed internally via the shapely
                     area-weighted centroid, which matches the target polygon centroid
                     convention and gives exact IoU = 1.0 when the shapes are identical).
    target_polygon : shapely.geometry.Polygon in UTM metres (E, N).
    face_azimuth   : optional compass bearing in DEGREES that the model's local +Y
                     ("front") should face in the world.  Ignored unless two or more
                     hypotheses score within 5% of the best IoU (used to break the
                     ambiguity of a symmetric shape like a square).

Returns a dict with keys:
    ori_rad  – compass azimuth (rad) to write into the .obj record (condor_grid
               convention: local +Y -> world azimuth ori_rad).
    posE     – target polygon centroid Easting  (metres, UTM).
    posN     – target polygon centroid Northing (metres, UTM).
    scale    – uniform scale to apply to the model.
    rms_m    – RMS residual between scaled+rotated model outline and target
               boundary (metres) – a measure of shape fit quality.
    iou      – Intersection-over-Union of the scaled+rotated model polygon vs
               the target polygon (0–1, 1 = perfect overlap).
    flip     – label of the winning hypothesis (e.g. '0', '90', '180', '270'
               or 'mirror+0' etc.).

METHOD
------
1. Scale: sqrt(area(target) / area(model_polygon)).  Both areas are computed via
   shapely, so they are consistent with the centroid computation.  The long-edge
   ratio of the two minimum_rotated_rectangles is also reported as a cross-check.

2. Centring: the model outline is centred on its shapely (area-weighted) centroid,
   NOT the vertex mean.  This is critical: for a concave polygon the shapely centroid
   differs from the vertex mean by a non-trivial offset, causing a permanent
   translation error if the vertex mean is used.

3. Initial rotation: align the long axes of the two minimum_rotated_rectangles.
   Since a rectangle's long axis is undirected (lies in [0, π)), each alignment
   yields two candidates 180° apart; we generate both.

4. Hypothesis set: {initial_rotation + 0°/90°/180°/270°} ∪ {mirrored variants
   of the same set} = 16 hypotheses total.  For each hypothesis:
     a. Apply mirror (reflect x -> -x in local frame) if in the mirrored set.
     b. Apply scale + rotation (about the model shapely centroid).
     c. Translate model centroid -> target centroid.
     d. Compute IoU (primary) and symmetric Hausdorff distance (secondary).
   The hypothesis with the highest IoU wins (ties broken by smallest Hausdorff).

5. face_azimuth tie-breaking: if supplied, among all hypotheses within 5% of the
   best IoU pick the one whose local +Y world azimuth (= ori_rad) is within 90° of
   face_azimuth.  This resolves a perfectly symmetric square.

6. Fine search: ±10° around the winning rotation in 0.5° steps, maximising IoU.

7. Return `ori_rad` in the condor_grid convention (local +Y -> world azimuth ori).

Dependencies: numpy, shapely (both standard in this repo).  scipy is used for
Hausdorff distance if available; otherwise a pure-numpy fallback is used.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
from shapely.geometry import Polygon, MultiPolygon


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_polygon(geom) -> Polygon:
    """Return a valid Polygon from a shapely geometry (handles MultiPolygon)."""
    if isinstance(geom, MultiPolygon):
        geom = max(geom.geoms, key=lambda g: g.area)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def _outline_to_array(outline: Sequence[tuple[float, float]]) -> np.ndarray:
    """Convert a list of (x, y) pairs to a float64 Nx2 array, removing closure."""
    pts = np.array(outline, dtype=np.float64)
    if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    return pts


def _rotation_matrix(theta: float) -> np.ndarray:
    """2-D rotation matrix for compass azimuth `theta` (radians).

    Implements the Condor convention: world = R · local, where local +Y -> azimuth theta.
        R = [[cos(theta), sin(theta)],
             [-sin(theta), cos(theta)]]
    """
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, s], [-s, c]])


def _transform_points(pts: np.ndarray, theta: float, scale: float,
                      mirror: bool = False) -> np.ndarray:
    """Scale, optionally mirror (x -> -x), then rotate.

    All operations are about the origin (caller must have centred pts on the
    shapely centroid first).
    """
    p = pts.copy()
    p *= scale
    if mirror:
        p[:, 0] = -p[:, 0]
    R = _rotation_matrix(theta)
    return (R @ p.T).T


def _pts_to_polygon(pts: np.ndarray, translate: np.ndarray | None = None) -> Polygon:
    """Build a shapely Polygon from a Nx2 array, optionally translating."""
    p = pts if translate is None else pts + translate
    poly = Polygon(p.tolist())
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly


def _iou(poly_a: Polygon, poly_b: Polygon) -> float:
    """Intersection-over-Union of two shapely Polygons."""
    if poly_a.is_empty or poly_b.is_empty:
        return 0.0
    inter = poly_a.intersection(poly_b)
    union = poly_a.union(poly_b)
    if union.is_empty:
        return 0.0
    return float(inter.area / union.area)


def _symmetric_hausdorff(pts_a: np.ndarray, pts_b: np.ndarray) -> float:
    """Symmetric Hausdorff distance (max of the two directed distances)."""
    try:
        from scipy.spatial.distance import directed_hausdorff
        h1 = directed_hausdorff(pts_a, pts_b)[0]
        h2 = directed_hausdorff(pts_b, pts_a)[0]
        return max(h1, h2)
    except ImportError:
        pass
    # Pure-numpy fallback: O(n×m) but fast enough for building outlines (<200 pts).
    def _directed(A: np.ndarray, B: np.ndarray) -> float:
        diffs = A[:, None, :] - B[None, :, :]   # (nA, nB, 2)
        dists = np.sqrt((diffs ** 2).sum(axis=2))
        return float(dists.min(axis=1).max())
    return max(_directed(pts_a, pts_b), _directed(pts_b, pts_a))


def _rms_to_boundary(transformed_pts: np.ndarray, target_poly: Polygon) -> float:
    """RMS distance from each transformed model vertex to the nearest point on
    the target polygon boundary."""
    from shapely.geometry import Point
    boundary = target_poly.exterior
    dists_sq = [boundary.distance(Point(float(x), float(y))) ** 2
                for x, y in transformed_pts]
    return float(np.sqrt(np.mean(dists_sq)))


def _mrr_long_axis_azimuth(poly: Polygon) -> float:
    """Compass azimuth (radians, in [0, π)) of the LONG axis of the
    minimum rotated rectangle of `poly`.  Long axis is undirected."""
    mrr = poly.minimum_rotated_rectangle
    if mrr.is_empty or mrr.geom_type != "Polygon":
        return 0.0
    cs = list(mrr.exterior.coords)[:4]
    best_len, best_az = -1.0, 0.0
    for i in range(4):
        x0, y0 = cs[i]
        x1, y1 = cs[(i + 1) % 4]
        dx, dy = x1 - x0, y1 - y0      # dx = East diff, dy = North diff
        L = math.hypot(dx, dy)
        if L > best_len:
            best_len = L
            best_az = math.atan2(dx, dy)  # compass: atan2(dE, dN)
    # Fold to [0, π) — long axis is undirected
    return best_az % math.pi


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def register(
    model_outline: Sequence[tuple[float, float]],
    target_polygon,
    face_azimuth: float | None = None,
) -> dict:
    """Register a model's base outline onto a target footprint polygon.

    Parameters
    ----------
    model_outline
        List of (x_E, y_N) vertices in LOCAL metres, nominally centred on the
        model origin (e.g. the exterior ring of the c3d base vertices).  The
        caller may pass them already centred or not — this function re-centres on
        the shapely area-weighted centroid for consistency.  A closing duplicate
        is stripped automatically.
    target_polygon
        shapely.geometry.Polygon in UTM metres (E, N).  Its shapely centroid is
        used as the placement point.
    face_azimuth
        Optional compass bearing in DEGREES that the model's local +Y ("front")
        should face in the world.  Ignored unless two or more hypotheses are
        within 5% of the best IoU (used to break the ambiguity of a symmetric
        shape like a square).

    Returns
    -------
    dict with keys: ori_rad, posE, posN, scale, rms_m, iou, flip.
    See module docstring for full description.  Two extra diagnostic keys (not
    part of the formal spec) are also returned: ``_mrr_ratio`` (long-edge scale
    cross-check) and ``_mirrored`` (True if the winning hypothesis is a mirror).
    """
    # ── 0. Input validation and normalisation ────────────────────────────────
    target_polygon = _ensure_polygon(target_polygon)
    if target_polygon.is_empty or target_polygon.area <= 0:
        raise ValueError("target_polygon is empty or has zero area")

    pts_raw = _outline_to_array(model_outline)
    if len(pts_raw) < 3:
        raise ValueError("model_outline must have at least 3 vertices")

    # Build shapely polygon for the model (using the raw coords as provided by caller).
    model_poly_raw = Polygon(pts_raw.tolist())
    if not model_poly_raw.is_valid:
        model_poly_raw = model_poly_raw.buffer(0)
    if model_poly_raw.is_empty or model_poly_raw.area <= 0:
        raise ValueError("model_outline forms a degenerate (zero-area) polygon")

    # Centre model points on the SHAPELY centroid (area-weighted), not the vertex mean.
    # This is critical for concave polygons: the shapely centroid != vertex mean,
    # and using the wrong centre causes a permanent translation error.
    mc = np.array([model_poly_raw.centroid.x, model_poly_raw.centroid.y])
    pts = pts_raw - mc

    # Recreate model polygon centred on (0,0) for area + MRR computations.
    model_poly = Polygon(pts.tolist())
    if not model_poly.is_valid:
        model_poly = model_poly.buffer(0)

    # ── 1. Scale ─────────────────────────────────────────────────────────────
    # Primary: sqrt(target area / model polygon area).  Both via shapely → consistent.
    model_area = model_poly.area
    target_area = target_polygon.area
    if model_area <= 0:
        raise ValueError("model_outline polygon has zero area after centring")
    scale = math.sqrt(target_area / model_area)

    # Cross-check via min-rotated-rect long-edge ratio (diagnostic only).
    mrr_model = model_poly.minimum_rotated_rectangle
    mrr_target = target_polygon.minimum_rotated_rectangle
    _mrr_ratio = 1.0
    if (not mrr_model.is_empty and not mrr_target.is_empty
            and mrr_model.geom_type == "Polygon"
            and mrr_target.geom_type == "Polygon"):
        def _long_edge(p: Polygon) -> float:
            cs = list(p.exterior.coords)[:4]
            return max(
                math.hypot(cs[(i + 1) % 4][0] - cs[i][0],
                           cs[(i + 1) % 4][1] - cs[i][1])
                for i in range(4)
            )
        le_m = _long_edge(mrr_model)
        le_t = _long_edge(mrr_target)
        if le_m > 0:
            _mrr_ratio = le_t / le_m

    # ── 2. Initial rotation from aligned MRR long axes ───────────────────────
    az_model = _mrr_long_axis_azimuth(model_poly)
    az_target = _mrr_long_axis_azimuth(target_polygon)

    # MRR long axis is undirected and lies in [0, π), so there are two 180°-apart
    # alignments that bring model_axis onto target_axis.
    initial_rotations = [
        (az_target - az_model) % math.pi,
        (az_target - az_model + math.pi) % (2.0 * math.pi),
    ]

    # ── 3. Hypothesis set ────────────────────────────────────────────────────
    # Each initial rotation spawns 4 candidates (+0/90/180/270°) and a mirrored
    # version of those, giving 16 hypotheses total.
    quarter = math.pi / 2.0
    hypotheses: list[tuple[float, bool, str]] = []   # (theta_rad, mirror, label)
    for base_theta in initial_rotations:
        for step in range(4):
            theta = (base_theta + step * quarter) % (2.0 * math.pi)
            hypotheses.append((theta, False, str(step * 90)))
    for base_theta in initial_rotations:
        for step in range(4):
            theta = (base_theta + step * quarter) % (2.0 * math.pi)
            hypotheses.append((theta, True, f"mirror+{step * 90}"))

    # ── 4. Evaluate hypotheses ───────────────────────────────────────────────
    target_c = np.array([target_polygon.centroid.x, target_polygon.centroid.y])
    target_bdry = np.array(target_polygon.exterior.coords)

    results = []
    for theta, mirror, label in hypotheses:
        t_pts = _transform_points(pts, theta, scale, mirror=mirror)
        poly_t = _pts_to_polygon(t_pts, translate=target_c)
        iou_val = _iou(poly_t, target_polygon)
        haus = _symmetric_hausdorff(t_pts + target_c, target_bdry)
        results.append((iou_val, -haus, theta, mirror, label, t_pts, poly_t))

    # Sort: highest IoU first, then smallest Hausdorff as tiebreaker.
    results.sort(key=lambda r: (r[0], r[1]), reverse=True)
    best_iou = results[0][0]

    # ── 5. face_azimuth tie-breaking ─────────────────────────────────────────
    # Among hypotheses within 5% of the best IoU, prefer the one whose
    # local +Y -> world direction (= ori = theta in the Condor convention) is
    # within 90° of face_azimuth.
    chosen_idx = 0
    if face_azimuth is not None:
        face_az_rad = math.radians(float(face_azimuth))
        threshold = best_iou * 0.95 if best_iou > 0 else 0.0
        for i, r in enumerate(results):
            if r[0] < threshold:
                break
            theta_i = r[2]
            # local +Y (0,1) maps to world azimuth = theta (Condor R convention).
            # Mirroring maps local (0,1) -> (0,1) (y-component unchanged) so the
            # +Y direction is unaffected.
            angular_diff = abs((theta_i - face_az_rad + math.pi) % (2.0 * math.pi) - math.pi)
            if angular_diff < math.pi / 2.0:   # within 90° of desired facing direction
                chosen_idx = i
                break

    iou_val, _, theta, mirror, label, t_pts, poly_t = results[chosen_idx]

    # ── 6. Fine rotation search ±10° around winner, step 0.5° ───────────────
    fine_best_iou = iou_val
    fine_best_theta = theta
    fine_best_pts = t_pts
    fine_best_poly = poly_t

    for deg_delta in np.arange(-10.0, 10.5, 0.5):
        if deg_delta == 0.0:
            continue
        theta_f = (theta + math.radians(deg_delta)) % (2.0 * math.pi)
        t_pts_f = _transform_points(pts, theta_f, scale, mirror=mirror)
        poly_f = _pts_to_polygon(t_pts_f, translate=target_c)
        iou_f = _iou(poly_f, target_polygon)
        if iou_f > fine_best_iou:
            fine_best_iou = iou_f
            fine_best_theta = theta_f
            fine_best_pts = t_pts_f
            fine_best_poly = poly_f

    final_theta = fine_best_theta
    final_pts = fine_best_pts
    final_poly = fine_best_poly
    final_iou = fine_best_iou

    # ── 7. Final quality metrics ──────────────────────────────────────────────
    rms_m = _rms_to_boundary(final_pts + target_c, target_polygon)
    ori_rad = float(final_theta % (2.0 * math.pi))

    return {
        "ori_rad": ori_rad,
        "posE": float(target_polygon.centroid.x),
        "posN": float(target_polygon.centroid.y),
        "scale": float(scale),
        "rms_m": float(rms_m),
        "iou": float(final_iou),
        "flip": label,
        # Diagnostics (not in formal spec but useful for callers)
        "_mrr_ratio": float(_mrr_ratio),
        "_mirrored": bool(mirror),
    }
