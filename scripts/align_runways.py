#!/usr/bin/env python3
"""
align_runways.py  --  STEP 1 of the runway flatten/align task.

Validate each runway's center + true heading against the ACTUAL runway visible
in the per-patch ortho DDS texture, BEFORE flattening the mesh.

For each airport (LWSK h0402, LWSN h0704, LW67 h0306):
  * Decompress the patch DDS to north-up RGB (PIL reads it north-up because the
    texture was built with gdalwarp -te e_min n_min e_max n_max, GDAL top-left
    origin).
  * Project the airports.json runway footprint rectangle (center, L x W, true
    heading) to patch pixels and overlay it.
  * For the paved LWSK runway, auto-detect the real asphalt centerline (long,
    grey, low-saturation strip) and measure its center + bearing in UTM; report
    the delta vs json. If detection is confident, emit an ADJUSTED center/heading.
  * For the grass strips (LWSN / LW67) try the same detector; if not confident,
    keep json values and flag it.
  * Save overlay PNGs to validation/runways/.

IMPORTANT geometry note: the textures were rendered with the OLD 29.987 m/px
patch corner (BR drift = exactly 1 px / 30 m vs the exact-30m mesh). To make the
overlay land on the imagery correctly we project with the TEXTURE patch bounds
(old spacing). Results are reported in UTM (CRS-shared), so the measured center
is directly usable by the exact-30m flattener. The constant ~1px texture/mesh
offset is pre-existing and out of scope here.

Outputs:
  validation/runways/<icao>_overlay.png      json footprint on texture
  validation/runways/<icao>_detected.png     detected centerline (if found)
  validation/runways/alignment_report.json   machine-readable findings
  data/airports_aligned.json                 json with any adjusted centers/hdg
"""

import json
import math
from pathlib import Path

import numpy as np
import pyproj
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
TEX_DIR = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
OUT_DIR = ROOT / "validation" / "runways"
AIRPORTS_JSON = ROOT / "data" / "airports.json"
ALIGNED_JSON = ROOT / "data" / "airports_aligned.json"
REPORT_JSON = OUT_DIR / "alignment_report.json"

# Texture geometry (OLD spacing, matches how the DDS were rendered).
ULX, ULY = 506880.0, 4700160.0
XDIM_TEX = 29.9869848156182
W = H = 2305
BR_E_TEX = ULX + (W - 1) * XDIM_TEX
BR_N_TEX = ULY - (H - 1) * XDIM_TEX
PATCH_M = 5760.0
TEX = 2048  # px per patch

# WGS84 -> UTM34N for the json lat/lon centers.
_T = pyproj.Transformer.from_crs(4326, 32634, always_xy=True)

# airport -> (col, row) verified earlier.
PATCH = {"LWSK": (4, 2), "LWSN": (7, 4), "LW67": (3, 6)}


def patch_bounds_tex(col, row):
    """Texture (old-spacing) UTM bounds for Condor patch (col=east0, row=south0)."""
    e_max = BR_E_TEX - col * PATCH_M
    e_min = e_max - PATCH_M
    n_min = BR_N_TEX + row * PATCH_M
    n_max = n_min + PATCH_M
    return e_min, n_min, e_max, n_max


def utm_to_px(e, n, e_min, n_max):
    """North-up patch pixel (texture orientation)."""
    px = (e - e_min) / PATCH_M * TEX
    py = (n_max - n) / PATCH_M * TEX
    return px, py


def footprint_corners_utm(e, n, L, W_, hdg_deg):
    """4 corners of an oriented runway rectangle in UTM (E,N)."""
    th = math.radians(hdg_deg)
    s, c = math.sin(th), math.cos(th)
    hl, hw = L / 2.0, W_ / 2.0
    de_c, dn_c = hl * s, hl * c          # along centerline
    de_p, dn_p = hw * c, -hw * s         # perpendicular (+90)
    return [
        (e - de_c - de_p, n - dn_c - dn_p),
        (e + de_c - de_p, n + dn_c - dn_p),
        (e + de_c + de_p, n + dn_c + dn_p),
        (e - de_c + de_p, n - dn_c + dn_p),
    ]


def draw_footprint(draw, corners, e_min, n_max, color, width=4):
    pts = [utm_to_px(e, n, e_min, n_max) for (e, n) in corners]
    pts.append(pts[0])
    draw.line(pts, fill=color, width=width)


def detect_runway(rgb, e_min, n_max, want_paved, cE, cN, L, Wm, hdg):
    """
    Detect a runway strip inside a CORRIDOR around the json centerline.

    The json position is roughly correct (within ~a few hundred metres); a
    whole-patch search drowns the runway in roads/roofs/field edges. So we build
    a search corridor oriented along the json heading -- length (L + 1000) m,
    half-width 220 m -- and only look for runway pixels there. Within the
    corridor we PCA-fit the bright/low-saturation paved pixels (asphalt reads as
    bright grey here) to recover the true center + bearing.

    Returns a dict (found True/False) with center (UTM), axis heading (0..180),
    span/width estimates and a confidence-relevant elongation + mask fraction.
    """
    H, Wd, _ = rgb.shape
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    bright = mx
    sat = np.where(mx > 1, (mx - mn) / np.maximum(mx, 1), 0.0)
    lum = 0.299 * r + 0.587 * g + 0.114 * b

    # --- corridor mask in pixel space ---
    # along-track unit vector (E,N) at json heading, and perpendicular.
    th = math.radians(hdg)
    a_e, a_n = math.sin(th), math.cos(th)       # along centerline (E,N)
    p_e, p_n = math.cos(th), -math.sin(th)      # perpendicular (E,N)
    # pixel grid -> UTM
    yy, xx = np.mgrid[0:H, 0:Wd]
    E = e_min + (xx + 0.0) / TEX * PATCH_M
    N = n_max - (yy + 0.0) / TEX * PATCH_M
    dE = E - cE
    dN = N - cN
    along = dE * a_e + dN * a_n
    perp = dE * p_e + dN * p_n
    CORR_HALF_LEN = L / 2.0 + 500.0
    CORR_HALF_W = 220.0
    corridor = (np.abs(along) <= CORR_HALF_LEN) & (np.abs(perp) <= CORR_HALF_W)

    if want_paved:
        runway_px = corridor & (bright > 130) & (sat < 0.15)
    else:
        # grass strip: within the corridor, the mowed/bare strip is usually a
        # luminance outlier vs the surrounding field; allow a looser, lower-conf
        # signature. Compute stats from corridor pixels only.
        cvals = lum[corridor]
        if cvals.size:
            med = float(np.median(cvals))
            mad = float(np.median(np.abs(cvals - med))) + 1e-3
            z = (lum - med) / (1.4826 * mad)
            runway_px = corridor & (np.abs(z) > 1.2) & (sat < 0.35)
        else:
            runway_px = np.zeros_like(corridor)

    frac = float(runway_px.mean())
    corr_frac = float(runway_px.sum()) / max(float(corridor.sum()), 1.0)
    ys, xs = np.where(runway_px)
    if xs.size < 150:
        return {"found": False, "reason": "few in-corridor runway px",
                "mask_frac": frac, "corridor_hit_frac": round(corr_frac, 4)}

    pts = np.column_stack([xs.astype(np.float32), ys.astype(np.float32)])
    mean = pts.mean(axis=0)
    cov = np.cov((pts - mean).T)
    evals, evecs = np.linalg.eigh(cov)
    major = evecs[:, np.argmax(evals)]
    elong = float(math.sqrt(evals.max() / max(evals.min(), 1e-6)))
    perp_ax = np.array([-major[1], major[0]])

    # Robustly isolate the THIN runway line from adjacent broad paved areas
    # (apron / parallel taxiway). Iteratively clamp the perpendicular offset to a
    # narrow window around its running median so the apron mass stops biasing the
    # centerline. Width is the spread of the surviving thin strip.
    t = (pts - mean) @ major
    s = (pts - mean) @ perp_ax
    sel = np.ones(pts.shape[0], dtype=bool)
    PERP_WIN_M = 35.0
    perp_win_px = PERP_WIN_M / PATCH_M * TEX
    for _ in range(4):
        s_med = float(np.median(s[sel]))
        sel = np.abs(s - s_med) <= perp_win_px
        if sel.sum() < 100:
            sel = np.abs(s - s_med) <= 2 * perp_win_px
            break

    # Along-track extent of the thin strip. Use the LARGEST CONTIGUOUS run of
    # along-track positions (100 m bins with >=1 hit) so a disconnected apron /
    # threshold blob at one end can't stretch the measured span or bias the mid.
    ts = t[sel]
    bin_m = 100.0
    bin_px = bin_m / PATCH_M * TEX
    order = np.argsort(ts)
    ts_sorted = ts[order]
    b_lo = math.floor(ts_sorted.min() / bin_px)
    b_hi = math.ceil(ts_sorted.max() / bin_px)
    occ = np.zeros(b_hi - b_lo + 1, dtype=bool)
    occ[((ts_sorted / bin_px).astype(int) - b_lo)] = True
    # find longest run of True allowing single-bin gaps
    best_s = best_len = cur_s = 0
    i = 0
    n = occ.size
    while i < n:
        if occ[i]:
            j = i
            gap = 0
            while j + 1 < n and (occ[j + 1] or gap == 0):
                if not occ[j + 1]:
                    gap += 1
                else:
                    gap = 0
                j += 1
            if (j - i) > best_len:
                best_len, best_s = (j - i), i
            i = j + 1
        else:
            i += 1
    run_lo = (b_lo + best_s) * bin_px
    run_hi = (b_lo + best_s + best_len + 1) * bin_px
    in_run = (ts >= run_lo) & (ts <= run_hi)
    t_lo = float(np.percentile(ts[in_run], 1))
    t_hi = float(np.percentile(ts[in_run], 99))
    span_px = float(t_hi - t_lo)
    t_center = 0.5 * (t_lo + t_hi)                # geometric mid of the main run
    s_center = float(np.median(s[sel][in_run]))
    center_px = mean + major * t_center + perp_ax * s_center

    sp = s[sel][in_run]
    width_px = float(np.percentile(sp, 95) - np.percentile(sp, 5))

    dx, dy = float(major[0]), float(major[1])
    aE, aN = dx, -dy
    axis_hdg = (math.degrees(math.atan2(aE, aN))) % 180.0

    dcE = e_min + center_px[0] / TEX * PATCH_M
    dcN = n_max - center_px[1] / TEX * PATCH_M
    span_m = span_px / TEX * PATCH_M
    width_m = width_px / TEX * PATCH_M

    return {
        "found": True,
        "mask_frac": frac,
        "corridor_hit_frac": round(corr_frac, 4),
        "elongation": round(elong, 2),
        "center_utm": [round(dcE, 1), round(dcN, 1)],
        "center_px": [round(float(center_px[0]), 1), round(float(center_px[1]), 1)],
        "axis_heading_deg": round(axis_hdg, 2),
        "span_m": round(span_m, 1),
        "width_m": round(width_m, 1),
        "n_mask_px": int(xs.size),
    }


def angle_axis_delta(a, b):
    """Smallest difference between two axis bearings (mod 180), signed a->b."""
    d = (b - a + 90.0) % 180.0 - 90.0
    return d


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = json.loads(AIRPORTS_JSON.read_text(encoding="utf-8"))
    report = {"airports": []}

    # deep copy for adjusted output
    aligned = json.loads(json.dumps(data))

    for ap, ap_adj in zip(data["airports"], aligned["airports"]):
        icao = ap["icao"]
        col, row = PATCH[icao]
        e_min, n_min, e_max, n_max = patch_bounds_tex(col, row)
        rwy = ap["runways"][0]
        L = rwy["length_m"]
        Wm = rwy["width_m"]
        hdg = rwy["true_heading"]
        clat, clon = rwy["center_lat"], rwy["center_lon"]
        cE, cN = _T.transform(clon, clat)

        tex = TEX_DIR / f"t{col:02d}{row:02d}.dds"
        rgb = np.asarray(Image.open(tex).convert("RGB"))

        # ---- overlay json footprint ----
        img = Image.fromarray(rgb).convert("RGB")
        draw = ImageDraw.Draw(img)
        corners = footprint_corners_utm(cE, cN, L, Wm, hdg)
        draw_footprint(draw, corners, e_min, n_max, (255, 0, 0), width=5)
        # centerline + center marker
        th = math.radians(hdg)
        end1 = (cE + (L / 2) * math.sin(th), cN + (L / 2) * math.cos(th))
        end2 = (cE - (L / 2) * math.sin(th), cN - (L / 2) * math.cos(th))
        p1 = utm_to_px(*end1, e_min, n_max)
        p2 = utm_to_px(*end2, e_min, n_max)
        draw.line([p1, p2], fill=(255, 255, 0), width=2)
        pc = utm_to_px(cE, cN, e_min, n_max)
        draw.ellipse([pc[0] - 6, pc[1] - 6, pc[0] + 6, pc[1] + 6],
                     outline=(0, 255, 255), width=3)
        overlay_path = OUT_DIR / f"{icao}_overlay.png"
        img.save(overlay_path)

        # ---- detect real runway ----
        want_paved = rwy.get("surface", "") == "asphalt"
        det = detect_runway(rgb, e_min, n_max, want_paved, cE, cN, L, Wm, hdg)

        entry = {
            "icao": icao,
            "patch": f"h{col:02d}{row:02d}",
            "json_center_utm": [round(cE, 1), round(cN, 1)],
            "json_true_heading": hdg,
            "json_len_w": [L, Wm],
            "surface": rwy.get("surface", ""),
            "overlay_png": str(overlay_path),
            "detection": det,
        }

        adjusted = False
        if det.get("found"):
            # build a detected overlay
            img2 = Image.fromarray(rgb).convert("RGB")
            d2 = ImageDraw.Draw(img2)
            # json footprint (red) for reference
            draw_footprint(d2, corners, e_min, n_max, (255, 0, 0), width=3)
            # detected footprint (green): use json L/W at detected center+heading
            dE_, dN_ = det["center_utm"]
            dhdg = det["axis_heading_deg"]
            dcorners = footprint_corners_utm(dE_, dN_, L, Wm, dhdg)
            draw_footprint(d2, dcorners, e_min, n_max, (0, 255, 0), width=3)
            img2.save(OUT_DIR / f"{icao}_detected.png")
            entry["detected_png"] = str(OUT_DIR / f"{icao}_detected.png")

            d_center_m = math.hypot(dE_ - cE, dN_ - cN)
            d_hdg = angle_axis_delta(hdg, dhdg)
            entry["delta_center_m"] = round(d_center_m, 1)
            entry["delta_heading_deg"] = round(d_hdg, 2)

            # Decide whether to ADJUST. Confident if: strongly elongated within
            # the corridor and the detected span is a plausible fraction of the
            # json length. (Paved runways pass easily; faint grass strips usually
            # won't, so they keep the json values.) Adjustment magnitude is capped
            # below so a bad detection can never throw the runway out of its patch.
            conf = (
                det["elongation"] >= 5.0
                and 0.5 * L <= det["span_m"] <= 1.5 * L
            )
            entry["confident"] = bool(conf)
            if conf and (d_center_m > 20.0 or abs(d_hdg) > 1.5) \
                    and d_center_m < 0.5 * L:
                # apply adjustment back to lat/lon for the aligned json
                lon_adj, lat_adj = pyproj.Transformer.from_crs(
                    32634, 4326, always_xy=True).transform(dE_, dN_)
                rwy_adj = ap_adj["runways"][0]
                rwy_adj["center_lat"] = round(lat_adj, 6)
                rwy_adj["center_lon"] = round(lon_adj, 6)
                # keep heading axis within json's named direction (~hdg, not +180)
                new_hdg = dhdg if abs(angle_axis_delta(hdg, dhdg)) < 90 else (dhdg + 180) % 360
                rwy_adj["true_heading"] = round(new_hdg, 2)
                rwy_adj["_aligned_from_imagery"] = True
                adjusted = True
        entry["adjusted"] = adjusted
        report["airports"].append(entry)

    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    ALIGNED_JSON.write_text(json.dumps(aligned, indent=2), encoding="utf-8")

    # console summary
    for e in report["airports"]:
        print(f"\n=== {e['icao']} ({e['patch']}, {e['surface']}) ===")
        print(f"  json center UTM : {e['json_center_utm']}  hdg {e['json_true_heading']}")
        d = e["detection"]
        if d.get("found"):
            print(f"  detected center : {d['center_utm']}  axis {d['axis_heading_deg']}  "
                  f"span {d['span_m']}m width {d['width_m']}m elong {d['elongation']:.1f} "
                  f"maskfrac {d['mask_frac']:.4f}")
            print(f"  delta           : center {e['delta_center_m']} m, "
                  f"heading {e['delta_heading_deg']} deg, confident={e.get('confident')}")
            print(f"  ADJUSTED        : {e['adjusted']}")
        else:
            print(f"  detection       : NOT FOUND ({d.get('reason')}, "
                  f"maskfrac {d.get('mask_frac', 0):.4f}) -> keep json")
    print(f"\nReport : {REPORT_JSON}")
    print(f"Aligned: {ALIGNED_JSON}")


if __name__ == "__main__":
    main()
