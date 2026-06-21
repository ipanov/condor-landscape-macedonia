#!/usr/bin/env python3
r"""
make_lwsk_spawn_overlay.py  --  ISSUE 2 proof for LWSK.

Decodes the INSTALLED MacedoniaSkopje.apt LWSK record and renders, on the
installed (now colour-harmonised) ortho patch t0402.dds:

  * the .apt runway axis (Direction degrees) through the .apt centre (midpoint)
    -- cyan
  * the declared runway rectangle midpoint +/- declaredLen/2 (the strip Condor
    reconstructs; declaredLen = real 2950 + 340 spawn extension)            -- green
  * the GROUND/AEROTOW glider spawn point Condor uses: ON the axis, ~170 m IN
    from the declared runway END (into the lower-numbered/into-wind end), where
    the towplane ballet begins                                              -- yellow dot
  * the measured painted centerline from align_lwsk_runway (lwsk_alignment.json)
    for reference                                                           -- red

Then a quantitative PASS/FAIL: the glider spawn must fall on the PAINTED runway
(within +/- runwayWidth/2 of the measured centerline, and inside the painted
along-track span). This proves the aerotow glider spawns on the runway, the same
property we fixed at Stenkovec.

Output: validation/runways/LWSK_spawn_overlay.png (+ _crop.png).
"""
from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import numpy as np
import pyproj
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures/t0402.dds")
APT = Path("C:/Condor2/Landscapes/MacedoniaSkopje/MacedoniaSkopje.apt")
MEAS = ROOT / "validation" / "runways" / "lwsk_alignment.json"
OUT = ROOT / "validation" / "runways" / "LWSK_spawn_overlay.png"

# texture geometry (OLD 29.987 m/px spacing, as the DDS were rendered)
ULX, ULY = 506880.0, 4700160.0
XDIM_TEX = 29.9869848156182
W = H = 2305
BR_E_TEX = ULX + (W - 1) * XDIM_TEX
BR_N_TEX = ULY - (H - 1) * XDIM_TEX
PATCH_M = 5760.0
TEXPX = 2048
PCOL, PROW = 4, 2

_T = pyproj.Transformer.from_crs(4326, 32634, always_xy=True)

# Condor spawns the aerotow glider ~170 m IN from the .apt runway end (forum
# t=19413/t=22592; generate_apt.py extends the declared length by 2*170 so this
# lands on the real threshold).
SPAWN_INSET_M = 170.0


def patch_bounds_tex(col, row):
    e_max = BR_E_TEX - col * PATCH_M
    e_min = e_max - PATCH_M
    n_min = BR_N_TEX + row * PATCH_M
    n_max = n_min + PATCH_M
    return e_min, n_min, e_max, n_max


E_MIN, N_MIN, E_MAX, N_MAX = patch_bounds_tex(PCOL, PROW)


def utm_to_px(e, n):
    return ((e - E_MIN) / PATCH_M * TEXPX, (N_MAX - n) / PATCH_M * TEXPX)


def read_apt_lwsk():
    data = APT.read_bytes()
    for i in range(len(data) // 72):
        r = data[i * 72:(i + 1) * 72]
        nlen = r[0]
        name = r[1:1 + nlen].decode("latin1", "ignore").strip()
        if name.upper().startswith("SKOPJE"):
            lat, lon, elev = struct.unpack_from("<fff", r, 36)
            rwdir, rwlen, width = struct.unpack_from("<iii", r, 48)
            return name, lat, lon, elev, rwdir, rwlen, width
    raise SystemExit("LWSK not found in .apt")


def main():
    name, lat, lon, elev, rwdir, rwlen, width = read_apt_lwsk()
    cE, cN = _T.transform(lon, lat)
    print(f".apt {name}: centre ({lat:.5f},{lon:.5f}) -> UTM ({cE:.1f},{cN:.1f})")
    print(f"         dir={rwdir} deg  declaredLen={rwlen} m  width={width} m")

    th = math.radians(rwdir)
    a_e, a_n = math.sin(th), math.cos(th)        # along axis (toward dir)
    p_e, p_n = math.cos(th), -math.sin(th)       # perpendicular

    half = rwlen / 2.0
    end_lo = (cE - a_e * half, cN - a_n * half)  # the dir-origin end (16, NW)
    end_hi = (cE + a_e * half, cN + a_n * half)  # the dir end (34, SE)

    # Glider spawn: ~170 m in from an end, on the axis. Condor uses the into-wind
    # end; we show the NW(16) end inset (both are symmetric for this proof).
    spawn = (end_lo[0] + a_e * SPAWN_INSET_M, end_lo[1] + a_n * SPAWN_INSET_M)
    spawn_hi = (end_hi[0] - a_e * SPAWN_INSET_M, end_hi[1] - a_n * SPAWN_INSET_M)

    # measured painted centerline (for PASS/FAIL distance)
    meas = json.loads(MEAS.read_text())
    mE, mN = meas["measured_center_utm"]
    maz = math.radians(meas["measured_azimuth_deg"])
    m_ae, m_an = math.sin(maz), math.cos(maz)
    m_pe, m_pn = math.cos(maz), -math.sin(maz)
    painted_span = meas["diagnostics"]["painted_span_m"]

    def perp_dist_to_painted(pt):
        dE, dN = pt[0] - mE, pt[1] - mN
        perp = dE * m_pe + dN * m_pn
        along = dE * m_ae + dN * m_an
        return abs(perp), along

    d_spawn, a_spawn = perp_dist_to_painted(spawn)
    d_spawn_hi, a_spawn_hi = perp_dist_to_painted(spawn_hi)
    print(f"\nGlider spawn (170 m in from NW end): UTM ({spawn[0]:.1f},{spawn[1]:.1f})")
    print(f"  perpendicular distance to painted centerline: {d_spawn:.1f} m "
          f"(runway half-width {width/2:.0f} m)")
    print(f"  along-track position vs painted span: {a_spawn:+.0f} m "
          f"(|<= {painted_span/2:.0f} m| = inside)")
    print(f"Glider spawn (170 m in from SE end): perp {d_spawn_hi:.1f} m, "
          f"along {a_spawn_hi:+.0f} m")

    on_runway = (d_spawn <= width / 2.0 and abs(a_spawn) <= painted_span / 2.0
                 and d_spawn_hi <= width / 2.0 and abs(a_spawn_hi) <= painted_span / 2.0)
    print(f"\n  -> {'PASS' if on_runway else 'FAIL'}: both aerotow glider spawns "
          f"land ON the painted runway (perp <= half-width, inside span)")

    # ---- overlay ----
    img = Image.open(TEX).convert("RGB")
    dr = ImageDraw.Draw(img, "RGBA")
    # measured painted centerline (red)
    mh = painted_span / 2.0 + 150
    A = utm_to_px(mE - m_ae * mh, mN - m_an * mh)
    B = utm_to_px(mE + m_ae * mh, mN + m_an * mh)
    dr.line([A, B], fill=(255, 60, 60, 220), width=2)
    # .apt axis (cyan)
    P1 = utm_to_px(*end_lo); P2 = utm_to_px(*end_hi)
    dr.line([P1, P2], fill=(0, 230, 255, 255), width=3)
    # declared runway rectangle (green)
    hw = width / 2.0
    corners = []
    for (be, bn) in (end_lo, end_hi):
        pass
    rect = [
        (end_lo[0] - hw * p_e, end_lo[1] - hw * p_n),
        (end_hi[0] - hw * p_e, end_hi[1] - hw * p_n),
        (end_hi[0] + hw * p_e, end_hi[1] + hw * p_n),
        (end_lo[0] + hw * p_e, end_lo[1] + hw * p_n),
    ]
    dr.polygon([utm_to_px(*c) for c in rect], outline=(60, 255, 60, 255))
    # spawn points (yellow)
    for sp, tag in ((spawn, "glider spawn (16 end)"), (spawn_hi, "glider spawn (34 end)")):
        X, Y = utm_to_px(*sp)
        dr.ellipse([X - 7, Y - 7, X + 7, Y + 7], fill=(255, 240, 0, 255),
                   outline=(0, 0, 0, 255))
        dr.text((X + 9, Y - 6), tag, fill=(255, 240, 0, 255))
    # centre marker
    cx, cy = utm_to_px(cE, cN)
    dr.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=(0, 230, 255, 255), width=2)

    dr.rectangle([6, 6, 520, 110], fill=(0, 0, 0, 160))
    dr.text((12, 10), f"LWSK Skopje Intl  .apt dir={rwdir} deg  declLen={rwlen} m  "
                      f"w={width} m", fill=(255, 255, 255, 255))
    dr.text((12, 28), "cyan  = .apt runway axis    green = declared strip",
            fill=(0, 230, 255, 255))
    dr.text((12, 44), f"yellow = aerotow glider spawn (170 m in from each end)",
            fill=(255, 240, 0, 255))
    dr.text((12, 60), f"red   = measured painted centerline (Canny+TLS)",
            fill=(255, 90, 90, 255))
    dr.text((12, 76), f"spawn perp to painted: {d_spawn:.1f} m / {d_spawn_hi:.1f} m "
                      f"(half-width {width/2:.0f} m)  -> "
                      f"{'ON RUNWAY' if on_runway else 'OFF'}",
            fill=(120, 255, 120, 255) if on_runway else (255, 80, 80, 255))

    img.save(OUT)
    # crop around the centre for readability
    R = 760
    crop = img.crop((int(cx - R), int(cy - R), int(cx + R), int(cy + R)))
    crop.save(OUT.with_name("LWSK_spawn_overlay_crop.png"))
    print(f"\n  overlay -> {OUT} (+ _crop.png)")
    return on_runway


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
