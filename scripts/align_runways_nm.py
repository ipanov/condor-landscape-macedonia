#!/usr/bin/env python3
r"""
align_runways_nm.py -- generic glider-centering fix for ALL NorthMacedonia airports.

PROBLEM (confirmed in-sim at Ohrid LWOH): gliders spawn OFF the used runway strip
because the 14 .apt centers came from OSM/OurAirports runway data, which does not
sit on the ACTUAL used (paved / mown) runway centerline.

THE VERIFIED CONVENTION (from the Stenkovec fix; data/airports_aligned.json LWSN
comment, .sandbox/stenkovec_fix/): in Condor the aerotow glider spawns ON the .apt
runway axis with ZERO lateral offset, ~170 m in from the threshold; the tug spawns
St=f(width) to one side. So glider-off-the-strip  <=>  the .apt axis is off the
used runway centerline. The FIX = put the .apt axis ON the measured used centerline
(whole-degree heading; widths unchanged -- they drive the tug's lateral offset).

METHOD (same as the Stenkovec fix, generalised):
  For each airport, find the patch ortho t{CC}{RR}.dds covering it (condor_grid
  patch_bounds_utm + the airport lat/lon). The NM textures were built with
  build_patch_textures.py via `gdalwarp -te e_min n_min e_max n_max` on the EXACT
  30 m grid (patch_bounds_utm), GDAL top-left origin, so the DDS reads NORTH-UP and
  UTM->pixel is condor_grid.utm_to_pixel(...,2048,2048) directly (2.8125 m/texel).

  1. Build a search CORRIDOR oriented along the OSM heading through the OSM center,
     length (L + slack), half-width ~runway-width-plus-margin. Within the corridor:
       * paved runways (asphalt/concrete): isolate bright low-saturation pixels;
       * grass strips: isolate the mown-lane luminance outlier vs the field.
  2. Canny edge map (corridor-masked) -> HoughLinesP. Keep only segments whose
     azimuth is within +/-12 deg of the OSM heading (the two long runway-parallel
     edges + paint dashes). Robust line fit (PCA on edge endpoints + a perpendicular
     median clamp, exactly like align_runways.py) to recover the centerline
     midpoint + azimuth in UTM.
  3. Cross-check the measured azimuth against the OSM runway-ends azimuth; if Hough
     is not confident, fall back to a luminance-PCA centroid fit; if THAT is not
     confident either, keep the OSM center (flagged keep_osm) so a bad detection can
     never throw a runway off its patch.

OUTPUT:
  data/airports_nm_aligned.json     -- mirror of airports_aligned.json structure;
                                       center_lat/lon moved onto the measured
                                       centerline midpoint, true_heading = measured
                                       azimuth (kept to ~2 dp; generate_apt rounds
                                       to WHOLE degrees for the .apt). widths kept.
                                       ends translated onto the new axis.
  .sandbox/nm_airports/<icao>_measure.png   per-airport detection overlay
  .sandbox/nm_airports/alignment_report.json machine-readable findings + corrections

The recenter magnitude is CAPPED at 0.5*L so a mis-detection cannot relocate the
airport; anything past the cap keeps OSM and is flagged.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
import pyproj
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import condor_grid as g  # honours CONDOR_LANDSCAPE (must be nm here)

ROOT = Path(__file__).resolve().parent.parent
NAME = g.LANDSCAPE_NAME
TEX_DIR = Path(f"C:/Condor2/Landscapes/{NAME}/Textures")
OUT_DIR = ROOT / ".sandbox" / "nm_airports"
AIRPORTS_JSON = ROOT / "data" / "airports_nm.json"
ALIGNED_JSON = ROOT / "data" / "airports_nm_aligned.json"
REPORT_JSON = OUT_DIR / "alignment_report.json"

TEX = 2048                                   # px per patch
PATCH_M = g.PATCH_SIZE_M                      # 5760 m
MPP = PATCH_M / TEX                           # 2.8125 m / texel

_T = pyproj.Transformer.from_crs(4326, 32634, always_xy=True)
_TINV = pyproj.Transformer.from_crs(32634, 4326, always_xy=True)

# ---------------------------------------------------------------------------
# VERIFIED ground-truth overrides: perpendicular correction from the OSM axis,
# in metres (+perp = RIGHT of the OSM heading, -perp = LEFT), plus a whole-degree
# heading. Applied verbatim, bypassing auto-detection.
#
# LWSN (Stenkovec): the Stenkovec glider-centering fix is ALREADY verified in-sim
# on the MacedoniaSkopje landscape (data/airports_aligned.json LWSN comment +
# .sandbox/stenkovec_fix/). Its grass strip is faint in the NM ortho t2525 and
# auto-detection caught the wrong field edge (+21 m, opposite sign), so we apply
# the verified value directly. The verified center (UTM 532175.9/4656466.7) lies
# -24.1 m PERPENDICULAR (NW) and only -5.2 m along-track from the NM-OSM center;
# we take the pure lateral component at the verified whole-degree heading 121.
# ---------------------------------------------------------------------------
OVERRIDES = {
    "LWSN": {"perp_off_m": -24.1, "heading": 121.0,
             "source": "verified Stenkovec fix (airports_aligned.json, in-sim)"},
}


# --------------------------------------------------------------------------- #
# Geometry helpers (NM exact-30m texture grid).
# --------------------------------------------------------------------------- #
def airport_patch(e: float, n: float) -> tuple[int, int]:
    """Condor (col=east0, row=south0) patch containing UTM (e, n)."""
    col = int((g.BR_EASTING - e) // PATCH_M)
    row = int((n - g.BR_NORTHING) // PATCH_M)
    return col, row


def utm_to_px(e: float, n: float, bounds) -> tuple[float, float]:
    e_min, n_min, e_max, n_max = bounds
    px = (e - e_min) / PATCH_M * TEX
    py = (n_max - n) / PATCH_M * TEX
    return px, py


def px_to_utm(px: float, py: float, bounds) -> tuple[float, float]:
    e_min, n_min, e_max, n_max = bounds
    e = e_min + px / TEX * PATCH_M
    n = n_max - py / TEX * PATCH_M
    return e, n


def axis_delta(a: float, b: float) -> float:
    """Signed smallest difference between two axis bearings (mod 180), a->b."""
    return (b - a + 90.0) % 180.0 - 90.0


def ends_azimuth(rwy: dict) -> float:
    """UTM azimuth (deg, 0..360, clockwise from N) from the runway ENDS -- the
    same projection the painted strip lives in (matches generate_apt)."""
    ends = rwy.get("ends")
    if not (ends and len(ends) >= 2):
        return float(rwy["true_heading"]) % 360.0
    a = _T.transform(ends[0]["lon"], ends[0]["lat"])
    b = _T.transform(ends[1]["lon"], ends[1]["lat"])
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360.0


# --------------------------------------------------------------------------- #
# The detector: corridor + Canny/Hough robust centerline fit.
# --------------------------------------------------------------------------- #
def _perp_offset_profile(signal_px, along, perp, corridor, Wm, L,
                         along_clip):
    """Given a boolean strip-pixel mask, return the best PERPENDICULAR offset of
    the strip from the OSM axis by scanning a 1-D profile of strip-pixel counts
    vs perpendicular distance (metres), restricted to the central along-track
    window. The peak (densest perpendicular band) is the used-strip center.

    This is the robust core for grass strips: instead of PCA over the whole
    corridor (which a long road/field-edge dominates), we look ONLY at the
    perpendicular density profile within +/- along_clip, so the strip's
    perpendicular position is recovered even when it is faint -- and the result
    is, by construction, a pure lateral correction along the OSM heading.
    """
    sel = signal_px & corridor & (np.abs(along) <= along_clip)
    if sel.sum() < 80:
        return None
    pvals = perp[sel]
    # histogram of perpendicular offsets, 1 texel (2.8 m) bins, smoothed
    lo, hi = -float(np.max(np.abs(pvals))) - 1, float(np.max(np.abs(pvals))) + 1
    bins = np.arange(-max(Wm * 3, 90), max(Wm * 3, 90) + MPP, MPP)
    hist, edges = np.histogram(pvals, bins=bins)
    if hist.sum() < 80:
        return None
    # smooth with a runway-width box so the peak is the strip, not a 1-bin spike
    k = max(3, int(round(Wm / MPP)) | 1)
    kern = np.ones(k) / k
    sm = np.convolve(hist.astype(float), kern, mode="same")
    centers = 0.5 * (edges[:-1] + edges[1:])
    peak = int(np.argmax(sm))
    # sub-bin centroid around the peak (+/- width)
    w = max(2, int(round(Wm / MPP)))
    a0, a1 = max(0, peak - w), min(len(centers), peak + w + 1)
    seg = hist[a0:a1].astype(float)
    if seg.sum() <= 0:
        return None
    perp_center = float(np.average(centers[a0:a1], weights=seg))
    # strength: peak density vs median density (how distinct the strip is)
    base = float(np.median(sm[sm > 0])) if (sm > 0).any() else 1.0
    strength = float(sm[peak] / max(base, 1e-6))
    return {"perp_off_m": perp_center, "strength": round(strength, 2),
            "n_px": int(sel.sum())}


def measure_runway(rgb: np.ndarray, bounds, cE: float, cN: float,
                   L: float, Wm: float, hdg: float, paved: bool) -> dict:
    """Measure the used-runway centerline from the ortho.

    Returns dict with found / methods + the recovered PERPENDICULAR offset of the
    used strip from the OSM axis (the correction we apply) and a measured
    azimuth. Two independent estimators, cross-checked:

      A. HOUGH: Canny edges (corridor-masked) -> HoughLinesP, keep only segments
         within +/-12 deg of OSM heading, robust line fit (PCA + perpendicular
         clamp + ALONG-TRACK clip to the declared length so adjacent aprons/roads
         cannot inflate the span or bias the midpoint). Gives center + azimuth.
      B. PERP-PROFILE: density profile of strip pixels (bright low-sat for paved;
         luminance outlier for grass) vs perpendicular distance, within the
         declared along-track window. Gives a pure lateral offset + a strength.

    The two are reconciled in the caller; either alone can be confident.
    """
    H, Wd, _ = rgb.shape
    r = rgb[:, :, 0].astype(np.float32)
    gg = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    mx = np.maximum(np.maximum(r, gg), b)
    mn = np.minimum(np.minimum(r, gg), b)
    bright = mx
    sat = np.where(mx > 1, (mx - mn) / np.maximum(mx, 1), 0.0)
    lum = 0.299 * r + 0.587 * gg + 0.114 * b

    # corridor in pixel space (oriented along OSM heading). Keep along-track only
    # a little past the declared length so a long road in line with the strip
    # cannot dominate (the OSM along-track position is reliable; only the LATERAL
    # position is what we are correcting).
    th = math.radians(hdg)
    a_e, a_n = math.sin(th), math.cos(th)        # along centerline (E,N)
    p_e, p_n = math.cos(th), -math.sin(th)       # perpendicular (E,N)
    yy, xx = np.mgrid[0:H, 0:Wd]
    E = bounds[0] + (xx + 0.5) / TEX * PATCH_M
    N = bounds[3] - (yy + 0.5) / TEX * PATCH_M
    dE = E - cE
    dN = N - cN
    along = dE * a_e + dN * a_n
    perp = dE * p_e + dN * p_n
    along_clip = L / 2.0 + 60.0                  # tight: declared length + margin
    CORR_HALF_LEN = L / 2.0 + 150.0
    CORR_HALF_W = max(Wm * 2.0, 120.0)
    corridor = (np.abs(along) <= CORR_HALF_LEN) & (np.abs(perp) <= CORR_HALF_W)

    diag = {"corridor_half_len_m": round(CORR_HALF_LEN, 1),
            "corridor_half_w_m": round(CORR_HALF_W, 1),
            "along_clip_m": round(along_clip, 1)}

    # ---- strip-pixel mask (paved: bright low-sat; grass: luminance outlier) ----
    if paved:
        strip_px = (bright > 120) & (sat < 0.18)
    else:
        cvals = lum[corridor]
        if cvals.size:
            m = float(np.median(cvals))
            mad = float(np.median(np.abs(cvals - m))) + 1e-3
            z = (lum - m) / (1.4826 * mad)
            # mown grass strip is usually BRIGHTER (drier/cut) than the field
            strip_px = (z > 0.8) & (sat < 0.40)
        else:
            strip_px = np.zeros_like(corridor)

    # ---------------- Estimator A: Canny + HoughLinesP --------------------------
    gray = np.clip(lum, 0, 255).astype(np.uint8)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    med = float(np.median(gray[corridor])) if corridor.any() else 128.0
    lo = int(max(0, 0.66 * med)); hi = int(min(255, 1.33 * med))
    edges = cv2.Canny(gray, lo, hi, apertureSize=3)
    edges = (edges * corridor.astype(np.uint8)).astype(np.uint8)
    min_len_px = max(25, int(0.30 * L / MPP))
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180.0, threshold=50,
                            minLineLength=min_len_px, maxLineGap=int(50 / MPP))
    kept = []
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0, :]:
            az = math.degrees(math.atan2((x2 - x1), -(y2 - y1))) % 180.0
            if abs(axis_delta(hdg % 180.0, az)) <= 12.0:
                kept.append((x1, y1, x2, y2, az, math.hypot(x2 - x1, y2 - y1)))
    diag["hough_total"] = 0 if lines is None else int(len(lines))
    diag["hough_kept"] = len(kept)

    hough = None
    if len(kept) >= 2:
        pts, wts = [], []
        for x1, y1, x2, y2, az, length in kept:
            for qx, qy in ((x1, y1), (x2, y2), (0.5 * (x1 + x2), 0.5 * (y1 + y2))):
                pts.append((qx, qy)); wts.append(length)
        pts = np.array(pts, np.float64); wts = np.array(wts, np.float64)
        # ALONG-TRACK clip the endpoints to the declared strip window
        ea = (bounds[0] + (pts[:, 0] + 0.5) / TEX * PATCH_M) - cE
        na = (bounds[3] - (pts[:, 1] + 0.5) / TEX * PATCH_M) - cN
        al = ea * a_e + na * a_n
        keepm = np.abs(al) <= along_clip
        if keepm.sum() >= 4:
            pts, wts = pts[keepm], wts[keepm]
        mean = np.average(pts, axis=0, weights=wts)
        d = pts - mean
        cov = (d * wts[:, None]).T @ d / wts.sum()
        evals, evecs = np.linalg.eigh(cov)
        major = evecs[:, int(np.argmax(evals))]
        elong = float(math.sqrt(evals.max() / max(evals.min(), 1e-6)))
        perp_ax = np.array([-major[1], major[0]])
        s = d @ perp_ax
        s_med = float(np.median(s)); win_px = max(Wm, 25.0) / MPP
        sel = np.abs(s - s_med) <= win_px
        if sel.sum() >= 4:
            mean = np.average(pts[sel], axis=0, weights=wts[sel])
        t = (pts - mean) @ major
        t_lo, t_hi = float(np.percentile(t, 2)), float(np.percentile(t, 98))
        center_px = mean + major * 0.5 * (t_lo + t_hi)
        aE, aN = float(major[0]), -float(major[1])
        axis_hdg = math.degrees(math.atan2(aE, aN)) % 180.0
        cE_h, cN_h = px_to_utm(center_px[0], center_px[1], bounds)
        perp_off_h = (cE_h - cE) * p_e + (cN_h - cN) * p_n
        hough = {"center_utm": [round(cE_h, 1), round(cN_h, 1)],
                 "axis_heading_deg": round(axis_hdg, 2),
                 "span_m": round((t_hi - t_lo) * MPP, 1),
                 "elongation": round(elong, 2),
                 "perp_off_m": round(perp_off_h, 2)}

    # ---------------- Estimator B: perpendicular density profile ----------------
    prof = _perp_offset_profile(strip_px, along, perp, corridor, Wm, L, along_clip)

    diag["strip_px_in_corridor"] = int((strip_px & corridor).sum())

    out = {"found": bool(hough or prof), **diag}
    if hough:
        out["hough"] = hough
    if prof:
        out["perp_profile"] = prof
    if not out["found"]:
        out["reason"] = "no runway signal in corridor"
    return out


# --------------------------------------------------------------------------- #
def main() -> int:
    if NAME != "NorthMacedonia":
        print(f"ERROR: this script is NM-only; set CONDOR_LANDSCAPE=nm "
              f"(got {NAME}).")
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = json.loads(AIRPORTS_JSON.read_text(encoding="utf-8"))
    aligned = json.loads(json.dumps(data))   # deep copy for the aligned output
    report = {"landscape": NAME, "mpp": MPP, "airports": []}

    for ap, ap_adj in zip(data["airports"], aligned["airports"]):
        icao = ap["icao"]
        rwy = ap["runways"][0]
        L = float(rwy["length_m"])
        Wm = float(rwy["width_m"])
        clat = float(rwy.get("center_lat", ap["lat"]))
        clon = float(rwy.get("center_lon", ap["lon"]))
        cE, cN = _T.transform(clon, clat)
        col, row = airport_patch(cE, cN)
        bounds = g.patch_bounds_utm(col, row)
        paved = rwy.get("surface", "") in ("asphalt", "concrete")
        osm_az = ends_azimuth(rwy)

        tex = TEX_DIR / f"t{col:02d}{row:02d}.dds"
        entry = {
            "icao": icao, "name": ap.get("name", ""),
            "patch": f"t{col:02d}{row:02d}", "surface": rwy.get("surface", ""),
            "osm_center_utm": [round(cE, 1), round(cN, 1)],
            "osm_ends_azimuth": round(osm_az, 2),
            "json_true_heading": rwy["true_heading"],
            "len_w": [L, Wm],
        }
        if not tex.exists():
            entry["error"] = f"texture missing: {tex}"
            entry["applied"] = False
            report["airports"].append(entry)
            print(f"{icao}: MISSING texture {tex.name} -> keep OSM")
            continue

        rgb = np.asarray(Image.open(tex).convert("RGB"))
        det = measure_runway(rgb, bounds, cE, cN, L, Wm, osm_az, paved)
        entry["detection"] = det

        th = math.radians(osm_az)
        a_e, a_n = math.sin(th), math.cos(th)
        p_e, p_n = math.cos(th), -math.sin(th)

        # ------------------------------------------------------------------ #
        # DECISION: reconcile the two estimators into a single perpendicular
        # correction + heading. Conservative -- only apply when the strip is
        # genuinely detected and cross-checks pass; otherwise keep OSM (the best
        # available datum) so a mis-detection can never relocate the airport.
        # ------------------------------------------------------------------ #
        applied = False
        new_E, new_N, new_az = cE, cN, osm_az
        perp_off = 0.0
        decision = OVERRIDES.get(icao)

        if decision is not None:
            # Hard ground-truth override (e.g. LWSN: the Stenkovec fix is already
            # verified in-sim on the Skopje landscape; auto-detection of its faint
            # grass strip in this ortho is unreliable, so we trust the verified
            # value). decision = {"perp_off_m":..,"heading":..,"source":..}
            perp_off = float(decision["perp_off_m"])
            new_az = float(decision.get("heading", osm_az))
            new_E = cE + perp_off * p_e
            new_N = cN + perp_off * p_n
            applied = True
            entry["override"] = decision.get("source", "manual ground-truth")
            entry["lateral_correction_m"] = round(perp_off, 2)
            entry["heading_delta_deg"] = round(axis_delta(osm_az % 180, new_az % 180), 2)
            entry["confident"] = True
        else:
            hough = det.get("hough")
            prof = det.get("perp_profile")
            cand_perp = []
            hough_ok = False
            if hough is not None:
                d_hdg = axis_delta(osm_az % 180.0, hough["axis_heading_deg"])
                span_ok = 0.45 * L <= hough["span_m"] <= 1.6 * L
                elong_ok = hough["elongation"] >= 4.0
                hough_ok = abs(d_hdg) <= 6.0 and span_ok and elong_ok
                entry["hough_d_hdg"] = round(d_hdg, 2)
                entry["hough_ok"] = hough_ok
                if hough_ok:
                    cand_perp.append(("hough", hough["perp_off_m"], hough["axis_heading_deg"]))
            prof_ok = False
            if prof is not None:
                # a distinct strip stands well above the corridor's median density
                prof_ok = prof["strength"] >= 1.8 and prof["n_px"] >= 150
                entry["prof_strength"] = prof["strength"]
                entry["prof_ok"] = prof_ok
                if prof_ok:
                    cand_perp.append(("perp_profile", prof["perp_off_m"], None))

            # reconcile: prefer agreement between the two estimators.
            chosen = None
            if hough_ok and prof_ok and \
                    abs(hough["perp_off_m"] - prof["perp_off_m"]) <= max(Wm, 15.0):
                # both agree -> average, use hough heading
                perp_off = 0.5 * (hough["perp_off_m"] + prof["perp_off_m"])
                new_az = hough["axis_heading_deg"]
                chosen = "hough+profile"
            elif hough_ok:
                perp_off = hough["perp_off_m"]
                new_az = hough["axis_heading_deg"]
                chosen = "hough"
            elif prof_ok:
                perp_off = prof["perp_off_m"]
                new_az = osm_az            # profile gives no heading -> keep OSM
                chosen = "perp_profile"

            cap = 0.5 * L
            within_cap = abs(perp_off) <= cap
            meaningful = abs(perp_off) >= 4.0      # < strip half-width-ish -> noise
            entry["chosen_estimator"] = chosen
            entry["lateral_correction_m"] = round(perp_off, 2)
            conf = chosen is not None and within_cap and meaningful
            entry["confident"] = bool(conf)
            if conf:
                # keep heading within the OSM-named hemisphere
                if abs(((new_az - osm_az + 180) % 360) - 180) > 90:
                    new_az = (new_az + 180.0) % 360.0
                new_E = cE + perp_off * p_e
                new_N = cN + perp_off * p_n
                applied = True
        entry["applied"] = applied

        # write back into the aligned json (lat/lon + heading + translated ends)
        if applied:
            lon2, lat2 = _TINV.transform(new_E, new_N)
            r_adj = ap_adj["runways"][0]
            r_adj["center_lat"] = round(lat2, 6)
            r_adj["center_lon"] = round(lon2, 6)
            r_adj["true_heading"] = round(new_az, 2)
            r_adj["_aligned_from_imagery"] = True
            r_adj["_align_method"] = entry.get("override") or entry.get("chosen_estimator")
            r_adj["_lateral_correction_m"] = round(perp_off, 2)
            # translate the ENDS by the same lateral vector so they stay on-axis
            shift_e = perp_off * p_e
            shift_n = perp_off * p_n
            for end in r_adj.get("ends", []):
                ee, nn = _T.transform(end["lon"], end["lat"])
                lon_e, lat_e = _TINV.transform(ee + shift_e, nn + shift_n)
                end["lon"] = round(lon_e, 6)
                end["lat"] = round(lat_e, 6)
            # mirror onto the airport-level lat/lon (keep ARP roughly on strip mid)
            ap_adj["lat"] = round(lat2, 6)
            ap_adj["lon"] = round(lon2, 6)

        report["airports"].append(entry)

        # --- detection overlay PNG ---
        _save_overlay(rgb, bounds, cE, cN, L, Wm, osm_az, det,
                      new_E if applied else cE, new_N if applied else cN,
                      new_az, icao, applied)

        est = entry.get("override") or entry.get("chosen_estimator") or "-"
        msg = (f"{icao} {entry['patch']} {entry['surface']:8s} "
               f"est={str(est)[:16]:16s} "
               f"lat.corr={entry.get('lateral_correction_m','?'):>7} m "
               f"applied={str(applied):5s} conf={entry.get('confident')}")
        print(msg)

    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    aligned["_comment_aligned"] = (
        "Imagery-aligned NorthMacedonia airports: each runway center moved onto "
        "the USED runway centerline measured from the installed ortho t{CC}{RR}.dds "
        "with align_runways_nm.py (Canny+HoughLinesP robust fit, fallback luminance "
        "PCA), cross-checked vs OSM ends azimuth. Glider spawns ON this axis with "
        "zero lateral offset (verified Stenkovec convention), so it now lands on the "
        "strip. Headings are whole-degree-safe (generate_apt rounds). Widths kept "
        "(they drive the tug). Ends translated by the same lateral vector.")
    ALIGNED_JSON.write_text(json.dumps(aligned, indent=2), encoding="utf-8")
    print(f"\nReport : {REPORT_JSON}")
    print(f"Aligned: {ALIGNED_JSON}")
    return 0


def _save_overlay(rgb, bounds, cE, cN, L, Wm, osm_az, det, newE, newN, new_az,
                  icao, applied):
    """Save a cropped overlay: OSM footprint (red), detected centerline (green),
    final .apt axis (yellow). Saved big for readability."""
    pcx, pcy = utm_to_px(cE, cN, bounds)
    R = int((L / 2 + 250) / MPP)
    x0 = max(0, int(pcx - R)); y0 = max(0, int(pcy - R))
    x1 = min(rgb.shape[1], int(pcx + R)); y1 = min(rgb.shape[0], int(pcy + R))
    crop = rgb[y0:y1, x0:x1].copy()
    if crop.size == 0:
        return
    SCALE = 2
    big = Image.fromarray(crop).resize(
        (crop.shape[1] * SCALE, crop.shape[0] * SCALE), Image.LANCZOS)
    d = ImageDraw.Draw(big, "RGBA")

    def C(e, n):
        px, py = utm_to_px(e, n, bounds)
        return ((px - x0) * SCALE, (py - y0) * SCALE)

    def line_az(e, n, az, length, fill, width):
        th = math.radians(az)
        ae, an = math.sin(th), math.cos(th)
        d.line([C(e - ae * length / 2, n - an * length / 2),
                C(e + ae * length / 2, n + an * length / 2)], fill=fill, width=width)

    # OSM footprint rectangle (red)
    th = math.radians(osm_az)
    s, c = math.sin(th), math.cos(th)
    hl, hw = L / 2, Wm / 2
    corners = [(cE - hl * s - hw * c, cN - hl * c + hw * s),
               (cE + hl * s - hw * c, cN + hl * c + hw * s),
               (cE + hl * s + hw * c, cN + hl * c - hw * s),
               (cE - hl * s + hw * c, cN - hl * c - hw * s)]
    d.polygon([C(*p) for p in corners], outline=(255, 60, 60, 255), width=2)
    d.ellipse([*[(v) for v in _pt(C(cE, cN), -5)], *_pt(C(cE, cN), 5)],
              outline=(255, 60, 60, 255), width=2)

    # detected centerline(s): Hough = green, perp-profile lateral = magenta dashes
    hough = det.get("hough")
    if hough is not None:
        dE, dN = hough["center_utm"]
        line_az(dE, dN, hough["axis_heading_deg"], L, (60, 255, 60, 255), 2)
    prof = det.get("perp_profile")
    if prof is not None:
        # draw the profile-derived centerline as a perpendicular-shifted OSM axis
        po = prof["perp_off_m"]
        th2 = math.radians(osm_az)
        pe2, pn2 = math.cos(th2), -math.sin(th2)
        pE, pN = cE + po * pe2, cN + po * pn2
        line_az(pE, pN, osm_az, L, (255, 60, 255, 220), 1)

    # final .apt axis (yellow) + computed glider spawn (~170 m in from threshold)
    line_az(newE, newN, new_az, L + 340, (255, 230, 0, 255), 2)
    # glider spawn: Condor spawns ~170 m IN from the .apt runway END (extended L);
    # along +axis from the FAR end. Use the end nearer OSM end[0].
    spawn = _glider_spawn(newE, newN, new_az, L)
    sx, sy = C(*spawn)
    d.ellipse([sx - 7, sy - 7, sx + 7, sy + 7], fill=(0, 160, 255, 255),
              outline=(0, 0, 0, 255))
    d.text((sx + 9, sy - 6), "glider spawn", fill=(0, 200, 255, 255))

    d.rectangle([4, 4, 360, 86], fill=(0, 0, 0, 150))
    d.text((10, 8), f"{icao}  applied={applied}  hdg(meas)={new_az:.1f} "
                    f"OSMaz={osm_az:.1f}", fill=(255, 255, 255, 255))
    d.text((10, 26), "red = OSM footprint   green = detected centerline",
           fill=(220, 220, 220, 255))
    d.text((10, 44), "yellow = final .apt axis", fill=(255, 230, 0, 255))
    d.text((10, 62), "blue dot = computed glider spawn (~170 m in)",
           fill=(0, 200, 255, 255))
    big.save(OUT_DIR / f"{icao}_measure.png")


def _pt(xy, dd):
    return (xy[0] + dd, xy[1] + dd)


def _glider_spawn(E, N, az, L):
    """Computed Condor glider spawn: along the axis, ~170 m IN from one end of
    the DECLARED runway (the .apt extends L by +340 so the spawn sits on the real
    threshold). Place it 170 m in from end[0] direction (-axis end)."""
    th = math.radians(az)
    ae, an = math.sin(th), math.cos(th)
    half = L / 2.0
    # spawn = far (-) end + 170 m inwards
    sE = E - ae * half + ae * 170.0
    sN = N - an * half + an * 170.0
    return sE, sN


if __name__ == "__main__":
    sys.exit(main())
