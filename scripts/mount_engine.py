#!/usr/bin/env python3
r"""Deterministic, object-agnostic mount engine for Condor 2.

Places ANY 3D object so its base footprint OUTLINE aligns to the building as painted
in the GAME texture, by maximizing edge overlap (translation + rotation, sub-pixel) +
a non-grass interior term. No AI, no per-object tuning, no other raster -- only the
installed tCCRR.dds and the model. Works for any footprint shape (rectangle, L, narrow,
square); proven by tests/test_mount_engine.py against synthetic ground truth.

CORE (testable on a plain array):
  register_core(outline_local, img_rgb, affine, az_prior, model_axis, scale, seed_EN, ...)
    -> {cE, cN, ori_deg, score}   maximizing edge-on-outline minus grass-inside.

Conventions match condor_grid / verify_object_placement:
  world = (cE,cN) + R(ori).(scale.local) ; R=[[cos,sin],[-sin,cos]] ; local +Y -> az ori.
"""
from __future__ import annotations
import math
import numpy as np


# --------------------------------------------------------------------------- #
# Feature maps + geometry helpers (pure, testable)
# --------------------------------------------------------------------------- #
def feature_maps(img_rgb):
    """Return (edge[0..1], grass[0..1]) maps from an RGB uint8 image."""
    img = np.asarray(img_rgb).astype(np.float32)
    R, G, B = img[..., 0], img[..., 1], img[..., 2]
    gray = 0.299 * R + 0.587 * G + 0.114 * B
    gx = np.zeros_like(gray); gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    edge = np.hypot(gx, gy)
    edge /= (edge.max() + 1e-6)
    grass = np.clip(G - np.maximum(R, B), 0, 255) / 255.0
    return edge, grass


def densify(outline, step=1.0):
    """Densify a closed polygon's edges to points ~`step` metres apart."""
    out = np.asarray(outline, float)
    pts = []
    n = len(out)
    for i in range(n):
        a, b = out[i], out[(i + 1) % n]
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        k = max(1, int(round(d / step)))
        for t in range(k):
            pts.append(a + (b - a) * (t / k))
    return np.array(pts) if pts else out


def _affine_px(EN, affine):
    sx, ox, sy, oy = affine
    return sx * EN[:, 0] + ox, sy * EN[:, 1] + oy


def _sample(arr, EN, affine):
    H, W = arr.shape
    px, py = _affine_px(EN, affine)
    xs = np.clip(px, 0, W - 1).astype(np.int32)
    ys = np.clip(py, 0, H - 1).astype(np.int32)
    return float(arr[ys, xs].mean())


def _rot_offsets(pts, ori_deg, scale):
    o = math.radians(ori_deg); co, so = math.cos(o), math.sin(o)
    x = pts[:, 0] * scale; y = pts[:, 1] * scale
    return np.column_stack([co * x + so * y, -so * x + co * y])   # world-relative dE,dN


# --------------------------------------------------------------------------- #
# THE DETERMINISTIC REGISTRATION CORE
# --------------------------------------------------------------------------- #
def register_core(outline_local, img_rgb, affine, az_prior_deg, model_axis_deg,
                  scale=1.0, seed_EN=(0.0, 0.0), search_m=28.0,
                  coarse_step_m=3.0, grass_weight=0.6):
    """Align the model footprint OUTLINE to building edges in img_rgb. Deterministic.

    outline_local : Nx2 (E,N) metres, centred on the model geometric centroid.
    img_rgb       : HxWx3 uint8 game-texture crop.
    affine        : (sx,ox,sy,oy) so px=sx*E+ox, py=sy*N+oy (world UTM -> image pixels).
    az_prior_deg  : footprint long-axis bearing (mod 180) seed.
    model_axis_deg: the outline's long-axis LOCAL azimuth (90 if long axis is +X).
    Returns dict(cE,cN,ori_deg,score) maximizing mean edge on the outline minus grass inside.
    """
    edge, grass = feature_maps(img_rgb)
    bnd = densify(np.asarray(outline_local, float), step=1.0)
    inr = bnd * 0.55      # shrunk ring -> interior samples (outline is centred on centroid)
    sE, sN = seed_EN

    def score(cE, cN, ori):
        rb = _rot_offsets(bnd, ori, scale); ri = _rot_offsets(inr, ori, scale)
        b = _sample(edge, np.column_stack([cE + rb[:, 0], cN + rb[:, 1]]), affine)
        g = _sample(grass, np.column_stack([cE + ri[:, 0], cN + ri[:, 1]]), affine)
        return b - grass_weight * g

    bases = [(az_prior_deg - model_axis_deg) % 360.0,
             (az_prior_deg - model_axis_deg + 180.0) % 360.0]
    best = None
    for ob in bases:
        for dth in np.arange(-12.0, 12.01, 2.0):
            ori = (ob + dth) % 360.0
            for dE in np.arange(-search_m, search_m + 0.01, coarse_step_m):
                for dN in np.arange(-search_m, search_m + 0.01, coarse_step_m):
                    s = score(sE + dE, sN + dN, ori)
                    if best is None or s > best[0]:
                        best = (s, sE + dE, sN + dN, ori)
    # fine refine (sub-texel / sub-degree) around the winner
    _, bE, bN, bo = best
    for _ in range(2):
        improved = False
        for dth in np.arange(-2.0, 2.01, 0.25):
            ori = (bo + dth) % 360.0
            for dE in np.arange(-coarse_step_m, coarse_step_m + 0.01, 0.5):
                for dN in np.arange(-coarse_step_m, coarse_step_m + 0.01, 0.5):
                    s = score(bE + dE, bN + dN, ori)
                    if s > best[0]:
                        best = (s, bE + dE, bN + dN, ori); improved = True
        _, bE, bN, bo = best
        coarse_step_m = 1.0
        if not improved:
            break
    return {"score": float(best[0]), "cE": float(best[1]), "cN": float(best[2]),
            "ori_deg": float(best[3] % 360.0)}


# --------------------------------------------------------------------------- #
# Model footprint (geometric centroid + outline + native dims + long axis)
# --------------------------------------------------------------------------- #
def model_footprint(c3d_path, base_z_window=0.75):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import c3d as C3
    from shapely.geometry import MultiPoint
    cf = C3.parse_c3d(c3d_path)
    allv = [(v.px, v.py, v.pz) for o in cf.objects for v in o.vertices]
    zmin = min(v[2] for v in allv)
    base = np.array([(x, y) for x, y, z in allv if z <= zmin + base_z_window])
    hull = MultiPoint([tuple(p) for p in base]).convex_hull
    cx, cy = float(hull.centroid.x), float(hull.centroid.y)        # GEOMETRIC centre
    outline = np.array(hull.exterior.coords)[:-1] - [cx, cy]       # centred outline
    dx = float(base[:, 0].max() - base[:, 0].min())
    dy = float(base[:, 1].max() - base[:, 1].min())
    axis = 90.0 if dx >= dy else 0.0                               # long-axis local azimuth
    return dict(centroid=(cx, cy), outline=outline, dx=dx, dy=dy,
                nat_L=max(dx, dy), nat_W=min(dx, dy), axis=axis)


def front_local_azimuth(c3d_path, front_groups, rear_groups):
    """Model 'front' azimuth at ori=0 from named groups (the model self-cue). The
    front is the +direction of the front group, or OPPOSITE the rear group."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import c3d as C3
    cf = C3.parse_c3d(c3d_path)

    def gcentroid(subs):
        pts = [(v.px, v.py) for o in cf.objects
               if any(s.lower() in o.name.lower() for s in subs) for v in o.vertices]
        return (float(np.mean([p[0] for p in pts])), float(np.mean([p[1] for p in pts]))) if pts else None
    fc = gcentroid(front_groups) if front_groups else None
    rc = gcentroid(rear_groups) if rear_groups else None
    if fc is not None:
        return math.degrees(math.atan2(fc[0], fc[1])) % 360.0
    if rc is not None:
        return (math.degrees(math.atan2(rc[0], rc[1])) + 180.0) % 360.0
    return None
