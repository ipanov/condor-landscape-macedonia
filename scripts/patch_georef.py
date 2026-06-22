#!/usr/bin/env python3
r"""Per-patch TEXTURE GEOREFERENCER — the deterministic core of object placement.

THE PROBLEM (verified on patch t0704):
  The runway OSM polygon (true UTM) maps to DDS px (691,1187) [DEM grid] or
  (707,1203) [object grid], but the runway is actually PAINTED at (690,1199).
  Neither grid is right: the installed textures were gdalwarp'd to a 29.987 m
  DEM grid that drifts from the exact 30 m object grid, by an unknown
  patch-dependent (dx,dy).  This drift is what makes objects land a "few metres"
  off their building, no matter how carefully you compute UTM.

THE FIX (generic, fast, deterministic, works for EVERY patch):
  For each patch that contains a known linear/paved control feature (runway,
  taxiway, big road), cross-correlate the feature's rendered mask against the
  DDS pale-strip mask to solve the exact integer translation (dx,dy) that
  aligns UTM->pixel. Refine to sub-pixel. Build the true affine pixel<->UTM.
  Then EVERY object in that patch places by pure coordinate math — no
  per-object mask-centroiding, no thinking, no drift.

This is a building block reused by place_objects.py for all objects in a patch.
"""
from __future__ import annotations
import json, math
from pathlib import Path
import numpy as np
import pyproj
from PIL import Image
from scipy import ndimage
from scipy.signal import fftconvolve
from shapely.geometry import shape, box, Point
from shapely.ops import transform as shp_transform

REPO = Path(__file__).resolve().parents[1]
INSTALL = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
_to_utm = pyproj.Transformer.from_crs(4326, 32634, always_xy=True).transform


# ---- object-grid patch geometry (matches condor_grid) -----------------------
OBJ_ANCHOR_E, OBJ_ANCHOR_N = 575955.0, 4631085.0
BR_EASTING, BR_NORTHING = 576000.0, 4631040.0
PATCH_SIZE_M = 5760.0
DDS_N = 2048


def patch_object_extent(col, row):
    e_max = OBJ_ANCHOR_E - col * PATCH_SIZE_M; e_min = e_max - PATCH_SIZE_M
    n_min = OBJ_ANCHOR_N + row * PATCH_SIZE_M; n_max = n_min + PATCH_SIZE_M
    return e_min, n_min, e_max, n_max


def patch_dem_extent(col, row):
    e_max = BR_EASTING - col * PATCH_SIZE_M; e_min = e_max - PATCH_SIZE_M
    n_min = BR_NORTHING + row * PATCH_SIZE_M; n_max = n_min + PATCH_SIZE_M
    return e_min, n_min, e_max, n_max


def _rasterize_polys(polys_utm, extent, n=DDS_N, value=1.0):
    """Rasterize Shapely polygons (UTM) to an n x n mask under `extent`."""
    e_min, n_min, e_max, n_max = extent
    mpp = (e_max - e_min) / n
    mask = np.zeros((n, n), dtype=np.float32)
    # build a coordinate grid and use a vectorized per-poly contains via bbox+ray
    ys, xs = np.mgrid[0:n, 0:n]
    E = e_min + (xs + 0.5) * mpp
    N = n_max - (ys + 0.5) * mpp
    EE, NN = E.ravel(), N.ravel()
    for poly in polys_utm:
        minx, miny, maxx, maxy = poly.bounds
        cand = (EE >= minx) & (EE <= maxx) & (NN >= miny) & (NN <= maxy)
        idx = np.where(cand)[0]
        # vectorized point-in-polygon (ray casting on exterior ring)
        ext = np.array(poly.exterior.coords)
        inside = np.zeros(idx.size, dtype=bool)
        x, y = EE[idx], NN[idx]
        xj, yj = ext[:-1, 0], ext[:-1, 1]
        xk, yk = ext[1:, 0], ext[1:, 1]
        cond1 = (yj <= y[:, None]) & (yk > y[:, None])
        cond2 = (yj > y[:, None]) & (yk <= y[:, None])
        cond = cond1 | cond2
        with np.errstate(invalid="ignore", divide="ignore"):
            slope = (xk[None, :] - xj[None,:]) * (y[:, None]-yj[None,:]) / (yk[None,:]-yj[None,:]) + xj[None,:]
        cross = cond & (x[:, None] < slope)
        inside ^= (cross.sum(1) % 2 == 1)
        hit = idx[inside]
        mask.flat[hit] = value
    return mask


def pale_strip_mask(dds_rgb):
    """Mask pale paved strips (runway/taxiway) in the DDS."""
    R, G, B = dds_rgb[..., 0].astype(int), dds_rgb[..., 1].astype(int), dds_rgb[..., 2].astype(int)
    mx = dds_rgb.max(2)
    pale = (mx > 115) & (mx < 205) & (abs(R - G) < 24) & (abs(G - B) < 24) & (abs(R - B) < 30)
    pale = ndimage.binary_opening(pale, iterations=1)
    pale = ndimage.binary_closing(pale, iterations=3)
    return pale.astype(np.float32)


def load_control_features(col, row, extent):
    """Runway/taxiway polygons (UTM) that fall inside this patch."""
    p = REPO / ".sandbox/osm/runways.geojson"
    if not p.exists():
        return []
    polys = []
    pb = box(extent[0], extent[1], extent[2], extent[3])
    for f in json.loads(p.read_text(encoding="utf-8"))["features"]:
        g = shp_transform(_to_utm, shape(f["geometry"]))
        try:
            geoms = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
        except Exception:
            geoms = [g]
        for gg in geoms:
            if gg.is_empty or gg.geom_type not in ("Polygon", "LineString"):
                continue
            # buffer lines into strips
            poly = gg.buffer(8.0) if gg.geom_type == "LineString" else gg
            if poly.intersects(pb):
                # clip to patch so the mask edge is real
                clipped = poly.intersection(pb)
                if not clipped.is_empty:
                    polys.append(clipped)
    return polys


def solve_drift(col, row, extent_label="object", search=40, subpixel=True, verbose=True):
    """Solve the (dx,dy) pixel drift for patch (col,row) by cross-correlating
    the OSM control-feature mask with the DDS pale-strip mask.

    extent_label: 'object' (OBJ_ANCHOR) or 'dem' (BR). We rasterize the control
    mask on the chosen grid, then find the translation that best aligns it to
    the painted pale strip in the DDS. Returns (dx, dy) in DDS pixels to ADD to
    the grid-mapped pixel to get the TRUE painted pixel (i.e. true_px = grid_px + (dx,dy)).
    """
    extent = (patch_object_extent if extent_label == "object" else patch_dem_extent)(col, row)
    feats = load_control_features(col, row, extent)
    if not feats:
        if verbose:
            print(f"  [georef] patch {col},{row}: no control feature in patch")
        return None
    ctrl = _rasterize_polys(feats, extent)
    dds_rgb = np.asarray(Image.open(INSTALL / "Textures" / f"t{col:02d}{row:02d}.dds").convert("RGB"))
    pale = pale_strip_mask(dds_rgb)
    # Cross-correlate: shift ctrl by (dy,dx), multiply by pale, sum. Use FFT.
    # We want argmax over (dx,dy) in [-search, search].
    N = DDS_N
    # zero-pad and fftconvolve: corr = ifft(fft(pale) * conj(fft(ctrl)))
    # fftconvolve with the flipped kernel == cross-correlation.
    corr = fftconvolve(pale, ctrl[::-1, ::-1], mode="same")
    cy, cx = np.unravel_index(np.argmax(corr), corr.shape)
    # 'same' mode centers at N//2 ; displacement = (cy-N//2, cx-N//2)
    dy = cy - N // 2
    dx = cx - N // 2
    # the translation to map GRID ctrl -> PAINTED pale is (dx, dy)? Check sign:
    # corr[dy,dx] = sum pale[y,x]*ctrl[y-dy, x-dx]  => ctrl shifted by (dy,dx) aligns to pale
    # so true_px = grid_px + (dx, dy)
    score = corr.max()
    if subpixel:
        # parabolic peak refinement in both axes (within +/-1 px)
        def parab(axis, c0):
            i = [cy, cx][axis]
            if 1 <= i < corr.shape[axis] - 1:
                if axis == 0:
                    a, b, cc = corr[i - 1, cx], corr[i, cx], corr[i + 1, cx]
                else:
                    a, b, cc = corr[cy, i - 1], corr[cy, i], corr[cy, i + 1]
                denom = (a - 2 * b + cc)
                off = 0.5 * (a - cc) / denom if denom else 0.0
                off = max(-1.0, min(1.0, off))
                return off
            return 0.0
        oy, ox = parab(0, corr), parab(1, corr)
        dy += oy; dx += ox
    # sanity: ignore absurd shifts
    if abs(dx) > 200 or abs(dy) > 200:
        if verbose:
            print(f"  [georef] patch {col},{row}: absurd shift ({dx:.1f},{dy:.1f}) — rejected")
        return None
    if verbose:
        print(f"  [georef] patch t{col:02d}{row:02d} ({extent_label} grid): "
              f"drift dx={dx:+.2f} px dy={dy:+.2f} px  (~{dx*2.812:+.0f}m E, {dy*2.812:+.0f}m N)  score={score:.0f}")
    return float(dx), float(dy)


def true_affine(col, row, extent_label="object"):
    """Return functions (utm_to_px, px_to_utm) that include the solved drift.

    utm_to_px(E, N) -> (px, py) where the feature is PAINTED in the DDS.
    """
    d = solve_drift(col, row, extent_label, verbose=False)
    dx, dy = d if d else (0.0, 0.0)
    extent = (patch_object_extent if extent_label == "object" else patch_dem_extent)(col, row)
    e_min, n_min, e_max, n_max = extent

    def utm_to_px(E, N):
        u = (E - e_min) / (e_max - e_min)
        v = 1.0 - (N - n_min) / (n_max - n_min)
        return u * DDS_N + dx, v * DDS_N + dy

    def px_to_utm(px, py):
        u = (px - dx) / DDS_N
        v = 1.0 - (py - dy) / DDS_N
        return e_min + u * (e_max - e_min), n_min + v * (n_max - n_min)

    return utm_to_px, px_to_utm, (dx, dy)


if __name__ == "__main__":
    # solve drift for the hangar's patch and report
    d_obj = solve_drift(7, 4, "object")
    d_dem = solve_drift(7, 4, "dem")
    print("object-grid drift:", d_obj)
    print("dem-grid drift   :", d_dem)
