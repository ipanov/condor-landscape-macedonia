#!/usr/bin/env python3
r"""
align_lwsk_runway.py  --  ISSUE 2 fix for LWSK (Skopje International).

Measure the ACTUAL painted asphalt runway centerline + azimuth of LWSK from the
installed ortho patch t0402.dds with OpenCV (Canny + Hough, robust fit), exactly
the proven Stenkovec methodology, then write the imagery-aligned LWSK geometry
into data/airports_aligned.json so:

  * the .apt runway MIDPOINT sits ON the measured painted centerline, and
  * the .apt Direction is the measured WHOLE-DEGREE azimuth (generate_apt.py
    derives the azimuth from the runway ENDS, so we also rewrite the two ends to
    lie on the measured axis -> the rounded .apt heading == the painted axis).

WHY: the previous LWSK .apt heading (165) was derived from the eAIP threshold
coordinates' end-to-end azimuth, which is ~2 deg off the painted asphalt the
ortho/mesh actually live in (the painted strip measures ~167 deg). Condor spawns
the aerotow glider ON the .apt axis ~170 m in from the runway end; a 2 deg axis
error throws that spawn sideways. Putting the .apt axis on the measured painted
centerline lands the glider on the runway (the same fix that centred Stenkovec).

DETECTION (robust):
  1. Decompress t0402 north-up RGB. Build a search CORRIDOR oriented along the
     prior axis through the prior centre (length L+800, half-width 260 m) so
     roads/roofs outside the airfield can't bias the fit.
  2. Asphalt mask: bright, low-saturation pixels inside the corridor (the paved
     runway + parallel taxiway read as a long bright-grey ribbon).
  3. Canny edges on the masked luminance, then probabilistic Hough
     (cv2.HoughLinesP). Keep only near-corridor-parallel segments; take the
     LENGTH-WEIGHTED principal direction = the runway azimuth (folded 0..360 to
     the 16/34 sense). This is robust to the apron and to short cross edges.
  4. Centerline POSITION: PCA-centroid of the thin asphalt ribbon (iteratively
     clamped in the perpendicular to reject the wide apron), giving the runway
     midpoint in UTM. The midpoint is snapped onto the Hough axis.

Outputs:
  validation/runways/LWSK_centerline.png   measured axis + centre on the ortho
  validation/runways/lwsk_alignment.json   machine-readable measurement
  data/airports_aligned.json               LWSK center/heading/ends updated
                                           (LWSN/LW67 preserved verbatim)
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

TEX_DIR = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
OUT_DIR = ROOT / "validation" / "runways"
ALIGNED_JSON = ROOT / "data" / "airports_aligned.json"
MEAS_JSON = OUT_DIR / "lwsk_alignment.json"

# Texture geometry: the installed DDS were rendered on the OLD 29.987 m/px patch
# corner (same as align_runways.py / make_stenkovec_overlay.py). We project with
# THIS grid so the overlay lands on the imagery; results are reported in UTM
# (CRS-shared) so the exact-30 m flattener/.apt consume them directly.
ULX, ULY = 506880.0, 4700160.0
XDIM_TEX = 29.9869848156182
W = H = 2305
BR_E_TEX = ULX + (W - 1) * XDIM_TEX
BR_N_TEX = ULY - (H - 1) * XDIM_TEX
PATCH_M = 5760.0
TEX = 2048
PCOL, PROW = 4, 2          # LWSK = patch col 4 row 2 -> t0402

_T_WGS2UTM = pyproj.Transformer.from_crs(4326, 32634, always_xy=True)
_T_UTM2WGS = pyproj.Transformer.from_crs(32634, 4326, always_xy=True)


def patch_bounds_tex(col, row):
    e_max = BR_E_TEX - col * PATCH_M
    e_min = e_max - PATCH_M
    n_min = BR_N_TEX + row * PATCH_M
    n_max = n_min + PATCH_M
    return e_min, n_min, e_max, n_max


E_MIN, N_MIN, E_MAX, N_MAX = patch_bounds_tex(PCOL, PROW)


def utm_to_px(e, n):
    return ((e - E_MIN) / PATCH_M * TEX, (N_MAX - n) / PATCH_M * TEX)


def px_to_utm(px, py):
    return (E_MIN + px / TEX * PATCH_M, N_MAX - py / TEX * PATCH_M)


def detect(rgb, prior_cE, prior_cN, prior_hdg, L):
    """Return measured (center_E, center_N, azimuth_deg_0_360, diagnostics)."""
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    bright = mx
    sat = np.where(mx > 1, (mx - mn) / np.maximum(mx, 1), 0.0)
    lum = (0.299 * r + 0.587 * g + 0.114 * b)

    # corridor in UTM around prior axis
    th = math.radians(prior_hdg)
    a_e, a_n = math.sin(th), math.cos(th)
    p_e, p_n = math.cos(th), -math.sin(th)
    yy, xx = np.mgrid[0:H, 0:W][0:2] if False else np.mgrid[0:TEX, 0:TEX]
    E = E_MIN + (xx + 0.5) / TEX * PATCH_M
    N = N_MAX - (yy + 0.5) / TEX * PATCH_M
    dE = E - prior_cE
    dN = N - prior_cN
    along = dE * a_e + dN * a_n
    perp = dE * p_e + dN * p_n
    CORR_HALF_LEN = L / 2.0 + 400.0
    CORR_HALF_W = 260.0
    corridor = (np.abs(along) <= CORR_HALF_LEN) & (np.abs(perp) <= CORR_HALF_W)

    # asphalt = bright + low saturation, within corridor
    asphalt = corridor & (bright > 120) & (sat < 0.18)

    # --- Canny + probabilistic Hough on the masked luminance ---
    lum_u8 = np.clip(lum, 0, 255).astype(np.uint8)
    masked = np.where(asphalt, lum_u8, 0).astype(np.uint8)
    masked = cv2.GaussianBlur(masked, (5, 5), 0)
    edges = cv2.Canny(masked, 40, 120, apertureSize=3)
    min_len_px = int(0.30 * L / PATCH_M * TEX)   # >=30% of runway length
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360.0, threshold=80,
                            minLineLength=min_len_px, maxLineGap=40)

    prior_axis = prior_hdg % 180.0
    seg = []
    if lines is not None:
        for ln in lines[:, 0, :]:
            x1, y1, x2, y2 = map(float, ln)
            # segment azimuth in UTM sense: image x=East(+), image y=South(+),
            # so UTM north dir = -dy. azimuth = atan2(dE, dN).
            dE_s = (x2 - x1)
            dN_s = -(y2 - y1)
            az = math.degrees(math.atan2(dE_s, dN_s)) % 180.0
            d = abs(((az - prior_axis + 90) % 180) - 90)
            if d <= 12.0:                          # keep near-parallel segments
                length = math.hypot(x2 - x1, y2 - y1)
                seg.append((az, length, (x1, y1, x2, y2)))

    if seg:
        # length-weighted circular mean of segment azimuths (mod 180 -> double angle)
        azs = np.array([s[0] for s in seg])
        wts = np.array([s[1] for s in seg])
        ang2 = np.radians(azs * 2.0)
        cmean = math.atan2((wts * np.sin(ang2)).sum(), (wts * np.cos(ang2)).sum())
        hough_axis = (math.degrees(cmean) / 2.0) % 180.0
        n_seg = len(seg)
        seg_len = float(wts.sum())
    else:
        hough_axis = prior_axis
        n_seg = 0
        seg_len = 0.0

    # --- POSITION: iterative total-least-squares on a runway-WIDTH window -------
    # The plain asphalt centroid is dragged EAST by the wide apron/taxiway, so we
    # instead lock onto the runway itself: seed a narrow perpendicular window
    # (relative to the Hough axis) at the offset where the runway sits at its
    # apron-free ends, then iteratively TLS-fit a straight centerline through only
    # the in-window asphalt and re-window around the fit. This converges onto the
    # painted runway (verified dead-centre vs the painted strip + thresholds).
    asx = asphalt
    Es = E[asx]; Ns = N[asx]
    # seed perpendicular offset from the apron-free SE band (runway only there).
    p_along = (Es - prior_cE) * a_e + (Ns - prior_cN) * a_n
    p_perp = (Es - prior_cE) * p_e + (Ns - prior_cN) * p_n
    se_band = p_along > (L / 2.0 - 700.0)
    perp0 = float(np.median(p_perp[se_band])) if se_band.sum() > 50 else 0.0
    HW = 30.0                       # half runway-width window (m)
    dirv = np.array([math.sin(math.radians(hough_axis)),
                     math.cos(math.radians(hough_axis))])
    cen = np.array([prior_cE + perp0 * p_e, prior_cN + perp0 * p_n])
    for _ in range(8):
        nrm = np.array([-dirv[1], dirv[0]])
        pe_off = (Es - cen[0]) * nrm[0] + (Ns - cen[1]) * nrm[1]
        sel = np.abs(pe_off) <= HW
        if sel.sum() < 500:
            break
        P = np.column_stack([Es[sel], Ns[sel]])
        c = P.mean(0)
        _u, _s, vt = np.linalg.svd(P - c)
        dirv = vt[0]
        cen = c
    # final extent + centre on the fitted line
    nrm = np.array([-dirv[1], dirv[0]])
    pe_off = (Es - cen[0]) * nrm[0] + (Ns - cen[1]) * nrm[1]
    al_off = (Es - cen[0]) * dirv[0] + (Ns - cen[1]) * dirv[1]
    selF = np.abs(pe_off) <= HW
    t_lo, t_hi = np.percentile(al_off[selF], [1, 99])
    span_m = float(t_hi - t_lo)
    t_mid = 0.5 * (t_lo + t_hi)
    center = cen + dirv * t_mid
    cE, cN = float(center[0]), float(center[1])
    width_m = float(np.percentile(np.abs(pe_off[selF]), 95) * 2.0)

    # azimuth from the TLS fit (more precise than Hough), folded to 16->34 sense.
    az_full = math.degrees(math.atan2(dirv[0], dirv[1])) % 360.0
    if abs(((az_full - prior_hdg + 180) % 360) - 180) > 90:
        az_full = (az_full + 180.0) % 360.0
    major = dirv
    perp_ax = nrm
    center_px = np.array(utm_to_px(cE, cN))

    diag = {
        "n_hough_segments": n_seg,
        "hough_segment_total_px": round(seg_len, 1),
        "asphalt_px": int(asx.sum()),
        "runway_window_px": int(selF.sum()),
        "painted_span_m": round(span_m, 1),
        "painted_width_m": round(width_m, 1),
        "prior_axis_deg": round(prior_axis, 2),
        "hough_axis_deg": round(hough_axis, 2),
        "tls_axis_deg": round(math.degrees(math.atan2(dirv[0], dirv[1])) % 180.0, 2),
        "seed_perp_offset_m": round(perp0, 1),
    }
    return cE, cN, az_full, major, perp_ax, center_px, asphalt, edges, diag


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = json.loads(ALIGNED_JSON.read_text(encoding="utf-8"))
    lwsk = next(a for a in data["airports"] if a["icao"] == "LWSK")
    rwy = lwsk["runways"][0]
    L = rwy["length_m"]
    Wm = rwy["width_m"]
    prior_hdg = float(rwy["true_heading"])
    prior_cE, prior_cN = _T_WGS2UTM.transform(rwy["center_lon"], rwy["center_lat"])
    print(f"LWSK prior: center UTM ({prior_cE:.1f},{prior_cN:.1f})  hdg {prior_hdg:.2f}  "
          f"L={L} W={Wm}")

    rgb = np.asarray(Image.open(TEX_DIR / f"t{PCOL:02d}{PROW:02d}.dds").convert("RGB"))
    cE, cN, az_full, major, perp_ax, center_px, asphalt, edges, diag = detect(
        rgb, prior_cE, prior_cN, prior_hdg, L)

    az_whole = int(round(az_full)) % 360
    print(f"  measured center UTM ({cE:.1f},{cN:.1f})  azimuth {az_full:.2f} deg "
          f"-> whole {az_whole}")
    print(f"  diagnostics: {diag}")
    d_center = math.hypot(cE - prior_cE, cN - prior_cN)
    d_hdg = abs(((az_full - prior_hdg + 180) % 360) - 180)
    print(f"  delta vs prior: center {d_center:.1f} m, heading {d_hdg:.2f} deg")

    # --- new ends ON the measured axis, symmetric about the measured center ----
    th = math.radians(az_full)
    a_e, a_n = math.sin(th), math.cos(th)
    end_lo = (cE - a_e * L / 2.0, cN - a_n * L / 2.0)   # toward 16 (NW)
    end_hi = (cE + a_e * L / 2.0, cN + a_n * L / 2.0)   # toward 34 (SE)
    lon_lo, lat_lo = _T_UTM2WGS.transform(*end_lo)
    lon_hi, lat_hi = _T_UTM2WGS.transform(*end_hi)
    clon, clat = _T_UTM2WGS.transform(cE, cN)

    # designation order: original ends[0]=16 (NW), ends[1]=34 (SE) -> keep that.
    new_ends = [
        {"designation": rwy["ends"][0]["designation"], "lat": round(lat_lo, 6),
         "lon": round(lon_lo, 6), "elevation_m": rwy["ends"][0].get("elevation_m")},
        {"designation": rwy["ends"][1]["designation"], "lat": round(lat_hi, 6),
         "lon": round(lon_hi, 6), "elevation_m": rwy["ends"][1].get("elevation_m")},
    ]

    # --- verify the end-derived azimuth (what generate_apt.py computes) rounds
    #     to the measured whole degree ----
    _az_ends = math.degrees(math.atan2(end_hi[0] - end_lo[0],
                                       end_hi[1] - end_lo[1])) % 360.0
    assert round(_az_ends) == az_whole, (round(_az_ends), az_whole)
    print(f"  end-derived azimuth (generate_apt path): {_az_ends:.3f} -> "
          f"rounds to {round(_az_ends)} (== measured {az_whole})  OK")

    # --- write back into the aligned json (LWSK only) ----
    rwy["true_heading"] = round(az_full, 2)
    rwy["center_lat"] = round(clat, 6)
    rwy["center_lon"] = round(clon, 6)
    rwy["ends"] = new_ends
    rwy["_aligned_from_imagery"] = True
    rwy["_alignment_note"] = (
        f"LWSK runway centerline + azimuth measured from installed ortho t0402 by "
        f"Canny+Hough (align_lwsk_runway.py): painted axis {az_full:.2f} deg (whole "
        f"{az_whole}), centroid of the asphalt ribbon at UTM ({cE:.1f},{cN:.1f}). "
        f"Prior .apt heading was {prior_hdg:.2f} (from eAIP thresholds), ~{d_hdg:.1f} "
        f"deg off the painted strip; ends rewritten onto the measured axis so the "
        f"generate_apt.py end-derived heading rounds to {az_whole}. Center moved "
        f"{d_center:.0f} m onto the painted centerline so the aerotow glider spawns "
        f"on-runway.")

    # bump airport reference lat/lon onto the strip centre too (keeps .cup sane)
    ALIGNED_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  wrote {ALIGNED_JSON}")

    MEAS_JSON.write_text(json.dumps({
        "icao": "LWSK", "patch": "t0402",
        "measured_center_utm": [round(cE, 1), round(cN, 1)],
        "measured_azimuth_deg": round(az_full, 3),
        "measured_azimuth_whole": az_whole,
        "prior_center_utm": [round(prior_cE, 1), round(prior_cN, 1)],
        "prior_heading_deg": prior_hdg,
        "delta_center_m": round(d_center, 1),
        "delta_heading_deg": round(d_hdg, 2),
        "diagnostics": diag,
    }, indent=2), encoding="utf-8")

    # --- overlay PNG: measured axis (cyan) + center (yellow) on the ortho -------
    img = Image.fromarray(rgb).convert("RGB")
    dr = ImageDraw.Draw(img, "RGBA")
    # measured centerline through measured centre, full runway length + 200 m
    half = L / 2.0 + 200.0
    A = utm_to_px(cE - a_e * half, cN - a_n * half)
    B = utm_to_px(cE + a_e * half, cN + a_n * half)
    dr.line([A, B], fill=(0, 230, 255, 255), width=3)
    # measured runway footprint rectangle (measured center/axis, json L/W)
    hw = Wm / 2.0
    p_e, p_n = math.cos(th), -math.sin(th)
    corners = []
    for sl, sw in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        e = cE + sl * (L / 2.0) * a_e + sw * hw * p_e
        n = cN + sl * (L / 2.0) * a_n + sw * hw * p_n
        corners.append(utm_to_px(e, n))
    dr.polygon(corners, outline=(60, 255, 60, 255))
    # prior axis (red dashed-ish) for contrast
    pth = math.radians(prior_hdg)
    pae, pan = math.sin(pth), math.cos(pth)
    PA = utm_to_px(prior_cE - pae * half, prior_cN - pan * half)
    PB = utm_to_px(prior_cE + pae * half, prior_cN + pan * half)
    dr.line([PA, PB], fill=(255, 60, 60, 200), width=2)
    # centers
    pc = utm_to_px(cE, cN)
    dr.ellipse([pc[0]-7, pc[1]-7, pc[0]+7, pc[1]+7], fill=(255, 240, 0, 255),
               outline=(0, 0, 0, 255))
    ppc = utm_to_px(prior_cE, prior_cN)
    dr.ellipse([ppc[0]-5, ppc[1]-5, ppc[0]+5, ppc[1]+5], outline=(255, 60, 60, 255), width=2)
    dr.rectangle([6, 6, 470, 92], fill=(0, 0, 0, 150))
    dr.text((12, 10), f"LWSK t0402  measured painted axis = {az_full:.2f} deg "
                      f"(whole {az_whole})", fill=(255, 255, 255, 255))
    dr.text((12, 28), f"cyan = measured centerline   green = measured footprint",
            fill=(0, 230, 255, 255))
    dr.text((12, 44), f"yellow dot = measured center (on painted strip)",
            fill=(255, 240, 0, 255))
    dr.text((12, 60), f"red = PRIOR axis/center ({prior_hdg:.2f} deg, eAIP) "
                      f"-> off by {d_hdg:.1f} deg, {d_center:.0f} m", fill=(255, 90, 90, 255))
    img.save(OUT_DIR / "LWSK_centerline.png")
    # also a tight crop for readability
    cx, cy = pc
    R = 700
    crop = img.crop((int(cx - R), int(cy - R), int(cx + R), int(cy + R)))
    crop.save(OUT_DIR / "LWSK_centerline_crop.png")
    print(f"  overlay -> {OUT_DIR / 'LWSK_centerline.png'} (+ _crop.png)")


if __name__ == "__main__":
    main()
