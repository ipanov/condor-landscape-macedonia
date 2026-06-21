#!/usr/bin/env python3
r"""
Validation overlay for the FIXED Stenkovec airport.

Renders, on top of the installed ortho patch ``t0704.dds`` (Stenkovec = Condor
patch col=07 row=04):

  * the MEASURED painted-runway axis (Canny edge-fit of the two long parallel
    airfield edges in the ortho, ~120.7 deg UTM)  -- cyan
  * the NEW (fixed) ``StenkovecG.c3d`` grass quad, transformed EXACTLY as Condor
    will: strip modelled along local +Y, rotated about the airport ARP by the
    ``.apt`` Direction (121 deg), projected to texture pixels                -- lime
  * the OLD (broken, installed) grass quad, transformed the same way, to show the
    double-rotation crossing the runway                                     -- red
  * the windsock pole position from the new ``StenkovecO.c3d``              -- yellow dot

Condor rotation convention (PROVEN against Northern_Greece/DolneniG, whose +Y
strip + .apt dir=119.32 reproduces the painted 119.32 deg azimuth):
    UTM offset (E,N) of a local airport point (x=E_local, y=N_local) =
        E =  x*cos(th) + y*sin(th)
        N = -x*sin(th) + y*cos(th)
    with th = .apt Direction (azimuth, clockwise from north).

Output: ``.sandbox/airport_runway_fixed.png``  (the deliverable overlay).
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import numpy as np
import pyproj
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3d

ROOT = Path(__file__).resolve().parent.parent
TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures/t0704.dds")
APT = Path("C:/Condor2/Landscapes/MacedoniaSkopje/MacedoniaSkopje.apt")
FIXED_G = ROOT / ".sandbox" / "airports_fixed" / "StenkovecG.c3d"
FIXED_O = ROOT / ".sandbox" / "airports_fixed" / "StenkovecO.c3d"
OLD_G = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Airports/StenkovecG.c3d")
OUT = ROOT / ".sandbox" / "airport_runway_fixed.png"

# --- texture geometry: the DDS were rendered with the OLD 29.987 m/px spacing ---
ULX, ULY = 506880.0, 4700160.0
XDIM_TEX = 29.9869848156182
W = H = 2305
BR_E_TEX = ULX + (W - 1) * XDIM_TEX
BR_N_TEX = ULY - (H - 1) * XDIM_TEX
PATCH_M = 5760.0
TEXPX = 2048
PCOL, PROW = 7, 4

_T = pyproj.Transformer.from_crs(4326, 32634, always_xy=True)

# Measured painted-runway azimuth (Canny edge-fit, this script's sibling analysis).
MEASURED_AZ = 120.7


def patch_bounds_tex(col: int, row: int):
    e_max = BR_E_TEX - col * PATCH_M
    e_min = e_max - PATCH_M
    n_min = BR_N_TEX + row * PATCH_M
    n_max = n_min + PATCH_M
    return e_min, n_min, e_max, n_max


E_MIN, N_MIN, E_MAX, N_MAX = patch_bounds_tex(PCOL, PROW)


def utm_to_px(e: float, n: float):
    return ((e - E_MIN) / PATCH_M * TEXPX, (N_MAX - n) / PATCH_M * TEXPX)


def read_apt_stenkovec():
    data = APT.read_bytes()
    for i in range(len(data) // 72):
        r = data[i * 72:(i + 1) * 72]
        nlen = r[0]
        name = r[1:1 + nlen].decode("latin1", "ignore").strip()
        if name.upper().startswith("STENKOVEC"):
            lat, lon, elev = struct.unpack_from("<fff", r, 36)
            rwdir = struct.unpack_from("<i", r, 48)[0]
            return name, lat, lon, elev, rwdir
    raise SystemExit("Stenkovec not found in .apt")


def local_to_utm(x: float, y: float, cx: float, cy: float, th_deg: float):
    """Condor airport transform (convention A, proven vs Dolneni)."""
    th = math.radians(th_deg)
    e = cx + (x * math.cos(th) + y * math.sin(th))
    n = cy + (-x * math.sin(th) + y * math.cos(th))
    return e, n


def quad_outline_px(obj: c3d.C3DObject, cx, cy, th):
    """Return the convex outline (in tex px) of a c3d object's footprint after
    the Condor airport rotation -- used to draw the grass strip boundary."""
    pts = [(v.px, v.py) for v in obj.vertices]
    # for a quad the 4 distinct corners are enough; use the convex hull of all
    import itertools
    xy = np.array(pts)
    # unique
    uniq = np.unique(np.round(xy, 3), axis=0)
    # order by angle around centroid for a clean polygon
    c = uniq.mean(0)
    ang = np.arctan2(uniq[:, 1] - c[1], uniq[:, 0] - c[0])
    ring = uniq[np.argsort(ang)]
    out = []
    for x, y in ring:
        e, n = local_to_utm(x, y, cx, cy, th)
        out.append(utm_to_px(e, n))
    return out


def main():
    name, lat, lon, elev, rwdir = read_apt_stenkovec()
    cx, cy = _T.transform(lon, lat)
    print(f".apt: {name}  center=({lat:.5f},{lon:.5f}) -> UTM ({cx:.1f},{cy:.1f})  dir={rwdir} deg")

    img = Image.open(TEX).convert("RGB")
    a = np.array(img)

    # crop around the airport for a readable overlay (keep full-res, upscale x2)
    pcx, pcy = utm_to_px(cx, cy)
    R = 340
    x0 = int(pcx - R); y0 = int(pcy - R)
    x1 = int(pcx + R); y1 = int(pcy + R)
    crop = a[y0:y1, x0:x1].copy()
    SCALE = 2
    big = Image.fromarray(crop).resize((crop.shape[1] * SCALE, crop.shape[0] * SCALE), Image.LANCZOS)
    d = ImageDraw.Draw(big, "RGBA")

    def to_crop(px, py):
        return ((px - x0) * SCALE, (py - y0) * SCALE)

    # --- measured painted-runway axis (cyan), through ARP at MEASURED_AZ ---------
    th_m = math.radians(MEASURED_AZ)
    aE, aN = math.sin(th_m), math.cos(th_m)
    L = 620.0
    A = utm_to_px(cx - aE * L, cy - aN * L)
    B = utm_to_px(cx + aE * L, cy + aN * L)
    d.line([to_crop(*A), to_crop(*B)], fill=(0, 220, 255, 255), width=2)

    # --- OLD broken grass quad (red), transformed by .apt dir -------------------
    old = c3d.parse_c3d(OLD_G)
    old_g = max(old.objects, key=lambda o: len(o.vertices))
    ring = quad_outline_px(old_g, cx, cy, rwdir)
    d.polygon([to_crop(*p) for p in ring], outline=(255, 40, 40, 255), width=3)

    # --- NEW fixed grass quad (lime), transformed by .apt dir -------------------
    new = c3d.parse_c3d(FIXED_G)
    new_g = [o for o in new.objects if o.name == "Grass3D"][0]
    ring = quad_outline_px(new_g, cx, cy, rwdir)
    d.polygon([to_crop(*p) for p in ring], outline=(60, 255, 60, 255), width=3)
    # centreline paint outline (thin white)
    paint = [o for o in new.objects if o.name == "Asphaltpaint"]
    if paint:
        rp = quad_outline_px(paint[0], cx, cy, rwdir)
        d.polygon([to_crop(*p) for p in rp], outline=(255, 255, 255, 255), width=1)

    # --- windsock pole position from fixed O (yellow dot) -----------------------
    o_obj = c3d.parse_c3d(FIXED_O)
    pole = [o for o in o_obj.objects if o.name == "Pole"][0]
    pxy = np.array([[v.px, v.py] for v in pole.vertices]).mean(0)
    we, wn = local_to_utm(pxy[0], pxy[1], cx, cy, rwdir)
    wpx = utm_to_px(we, wn)
    X, Y = to_crop(*wpx)
    d.ellipse([X - 6, Y - 6, X + 6, Y + 6], fill=(255, 240, 0, 255), outline=(0, 0, 0, 255))
    d.text((X + 8, Y - 6), "windsock", fill=(255, 240, 0, 255))

    # legend
    d.rectangle([6, 6, 348, 96], fill=(0, 0, 0, 150))
    d.text((12, 10), f"Stenkovec  .apt dir={rwdir}deg   measured painted az={MEASURED_AZ}deg", fill=(255, 255, 255, 255))
    d.text((12, 28), "cyan  = MEASURED painted-runway axis (ortho edge-fit)", fill=(0, 220, 255, 255))
    d.text((12, 44), "lime  = FIXED grass quad (along +Y, rotated by .apt dir)", fill=(60, 255, 60, 255))
    d.text((12, 60), "red   = OLD broken quad (pre-rotated -> double rotation)", fill=(255, 80, 80, 255))
    d.text((12, 76), "yellow= windsock pole (fixed O)", fill=(255, 240, 0, 255))

    big.save(OUT)
    print(f"saved overlay -> {OUT}")

    # --- quantitative PASS/FAIL: fixed grass long-axis azimuth vs measured ------
    ring_utm = []
    for v in new_g.vertices:
        ring_utm.append(local_to_utm(v.px, v.py, cx, cy, rwdir))
    ring_utm = np.array(ring_utm)
    rc = ring_utm - ring_utm.mean(0)
    _, _, vt = np.linalg.svd(rc, full_matrices=False)
    fixed_az = math.degrees(math.atan2(vt[0][0], vt[0][1])) % 180
    err = abs(((fixed_az - MEASURED_AZ + 90) % 180) - 90)
    print(f"\nFIXED grass quad UTM long-axis azimuth = {fixed_az:.2f} deg")
    print(f"MEASURED painted-runway azimuth        = {MEASURED_AZ:.2f} deg")
    print(f"alignment error                         = {err:.2f} deg  -> "
          f"{'PASS (<=3 deg, AERO p.110 budget)' if err <= 3.0 else 'FAIL'}")

    # old quad azimuth for contrast
    old_utm = np.array([local_to_utm(v.px, v.py, cx, cy, rwdir) for v in old_g.vertices])
    oc = old_utm - old_utm.mean(0)
    _, _, ovt = np.linalg.svd(oc, full_matrices=False)
    old_az = math.degrees(math.atan2(ovt[0][0], ovt[0][1])) % 180
    old_err = abs(((old_az - MEASURED_AZ + 90) % 180) - 90)
    print(f"\n(for contrast) OLD quad azimuth = {old_az:.2f} deg -> error {old_err:.2f} deg (the disaster)")
    return err <= 3.0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
