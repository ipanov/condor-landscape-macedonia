#!/usr/bin/env python3
"""
stage_and_verify_flat_tr3.py

Regenerate the 144 .tr3 patches from the FLATTENED 30m raw into a STAGING dir,
then run three independent gates before anything is allowed near the install:

  GATE 1  SEAMS:    Max shared-edge mismatch between all neighbouring patches
                    must be 0 m (Condor reads .tr3 with +row=WEST, +col=NORTH,
                    i.e. the anti-transpose S.T[::-1,::-1]). This reproduces the
                    proven seam check from stage_and_verify_tr3.py, on the
                    flattened data.
  GATE 2  PLATEAU:  The three runway patches (h0402 LWSK 238, h0704 LWSN 318,
                    h0306 LW67 371) must each contain a flat plateau at the
                    target elevation over the runway footprint.
  GATE 3  ISOLATION: Every staged patch must be BYTE-IDENTICAL to the good
                    installed backup EXCEPT the three runway patches. (Confirms
                    the flatten changed nothing else and the regen is otherwise
                    a faithful reproduction of the verified mesh.)

Exit code 0 only if all gates pass. Prints a structured report. Does NOT install.
"""

import sys
import math
import hashlib
from pathlib import Path

import numpy as np
import pyproj

ROOT = Path(__file__).resolve().parent.parent
FLAT_RAW = ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305_flat.raw"
STAGE = ROOT / "output" / "HeightMaps_flat_staging"
BACKUP = Path("C:/Condor2/Landscapes/MacedoniaSkopje/_good_phase1_20260620/HeightMaps")

WIDTH = HEIGHT = 2305
SAMPLES = 193
INTERVAL = 192
PX = PY = 12

# runway patches -> (target elev m, ICAO, aligned lon/lat/hdg/L/W for plateau probe)
import json
_AIRPORTS = ROOT / "data" / "airports_aligned.json"
if not _AIRPORTS.exists():
    _AIRPORTS = ROOT / "data" / "airports.json"
_AP = json.loads(_AIRPORTS.read_text(encoding="utf-8"))
_PATCH_OF = {"LWSK": "h0402", "LWSN": "h0704", "LW67": "h0306"}
RUNWAY_PATCHES = {}
for ap in _AP["airports"]:
    r = ap["runways"][0]
    RUNWAY_PATCHES[_PATCH_OF[ap["icao"]]] = {
        "elev": int(round(ap["elevation_m"])),
        "icao": ap["icao"],
        "lon": r["center_lon"], "lat": r["center_lat"],
        "hdg": r["true_heading"], "L": r["length_m"], "W": r["width_m"],
    }

ULX, ULY, XDIM = 506880.0, 4700160.0, 30.0
_T = pyproj.Transformer.from_crs(4326, 32634, always_xy=True)


def antitranspose(a):
    return a.T[::-1, ::-1]


def north_up_slice(src, c, r):
    j = (PX - 1 - c) * INTERVAL
    i = (PY - 1 - r) * INTERVAL
    return src[i:i + SAMPLES, j:j + SAMPLES]


def generate_staging():
    src = np.fromfile(FLAT_RAW, dtype=np.int16).reshape(HEIGHT, WIDTH)
    src = np.where(src < 0, 0, src).astype(np.uint16)
    STAGE.mkdir(parents=True, exist_ok=True)
    stored = {}
    for c in range(PX):
        for r in range(PY):
            nu = north_up_slice(src, c, r)
            s = antitranspose(nu)
            stored[(c, r)] = s
            s.astype(np.uint16).tofile(STAGE / f"h{c:02d}{r:02d}.tr3")
    return stored


def gate_seams(stored):
    worst = 0
    worst_at = None
    for c in range(PX):
        for r in range(PY):
            cv = antitranspose(stored[(c, r)]).astype(int)   # Condor reconstruction
            if c + 1 < PX:
                nb = antitranspose(stored[(c + 1, r)]).astype(int)
                m = int(np.abs(cv[:, 0] - nb[:, INTERVAL]).max())
                if m > worst:
                    worst, worst_at = m, f"h{c:02d}{r:02d}|h{c+1:02d}{r:02d} (W)"
            if r + 1 < PY:
                nb = antitranspose(stored[(c, r + 1)]).astype(int)
                m = int(np.abs(cv[0, :] - nb[INTERVAL, :]).max())
                if m > worst:
                    worst, worst_at = m, f"h{c:02d}{r:02d}|h{c:02d}{r+1:02d} (N)"
    return worst, worst_at


def gate_plateau():
    """Probe the runway centre + along-track for each runway patch (reads the
    staged .tr3 back through Condor's read transform to be faithful)."""
    ok = True
    rows = []
    for patch, info in RUNWAY_PATCHES.items():
        c = int(patch[1:3]); r = int(patch[3:5])
        stored = np.fromfile(STAGE / f"{patch}.tr3", dtype=np.uint16).reshape(SAMPLES, SAMPLES)
        north_up = antitranspose(stored)  # back to north-up 193x193 patch slice
        e_c, n_c = _T.transform(info["lon"], info["lat"])
        # patch north-up origin in full-raw pixels
        j0 = (PX - 1 - c) * INTERVAL
        i0 = (PY - 1 - r) * INTERVAL
        th = math.radians(info["hdg"])
        hits = []
        for s in range(-int(info["L"] / 2) + 60, int(info["L"] / 2) - 60 + 1, 30):
            ee = e_c + s * math.sin(th); nn = n_c + s * math.cos(th)
            gpx = (ee - ULX) / XDIM; gpy = (ULY - nn) / XDIM
            li = int(round(gpy)) - i0; lj = int(round(gpx)) - j0
            if 0 <= li < SAMPLES and 0 <= lj < SAMPLES:
                hits.append(int(north_up[li, lj]))
        target = info["elev"]
        plateau_ok = len(hits) > 0 and all(v == target for v in hits)
        ok = ok and plateau_ok
        rows.append((patch, info["icao"], target, len(hits),
                     (min(hits) if hits else None), (max(hits) if hits else None),
                     plateau_ok))
    return ok, rows


def gate_isolation():
    """Byte-compare every staged patch against the good backup."""
    if not BACKUP.exists():
        return False, [], f"backup dir missing: {BACKUP}"
    differ = []
    missing = []
    for c in range(PX):
        for r in range(PY):
            name = f"h{c:02d}{r:02d}.tr3"
            a = STAGE / name
            b = BACKUP / name
            if not b.exists():
                missing.append(name)
                continue
            ha = hashlib.md5(a.read_bytes()).hexdigest()
            hb = hashlib.md5(b.read_bytes()).hexdigest()
            if ha != hb:
                differ.append(name)
    return True, (sorted(differ), missing), None


def main():
    print(f"Source : {FLAT_RAW}")
    print(f"Staging: {STAGE}")
    print(f"Backup : {BACKUP}\n")

    stored = generate_staging()
    n = len(list(STAGE.glob("*.tr3")))
    sz = (STAGE / "h0000.tr3").stat().st_size
    print(f"Staged {n} .tr3 (each {sz} bytes, expect 74498)\n")

    # GATE 1
    seam, seam_at = gate_seams(stored)
    g1 = seam == 0
    print(f"GATE 1  SEAMS    : max mismatch = {seam} m"
          + (f" at {seam_at}" if seam_at else "") + f"   -> {'PASS' if g1 else 'FAIL'}")

    # GATE 2
    g2, rows = gate_plateau()
    print(f"GATE 2  PLATEAU  : -> {'PASS' if g2 else 'FAIL'}")
    for patch, icao, tgt, k, lo, hi, okp in rows:
        print(f"    {patch} {icao}: target {tgt} m, {k} probes, range {lo}..{hi}"
              f"  {'OK' if okp else 'BAD'}")

    # GATE 3
    g3ok, payload, err = gate_isolation()
    if err:
        print(f"GATE 3  ISOLATION: ERROR {err}")
        g3 = False
    else:
        differ, missing = payload
        expected = {"h0402.tr3", "h0704.tr3", "h0306.tr3"}
        g3 = (set(differ) == expected) and not missing
        print(f"GATE 3  ISOLATION: {len(differ)} patches differ from backup "
              f"-> {'PASS' if g3 else 'FAIL'}")
        print(f"    differ: {differ}")
        print(f"    expected exactly: {sorted(expected)}")
        if missing:
            print(f"    MISSING in backup: {missing}")
        extra = set(differ) - expected
        absent = expected - set(differ)
        if extra:
            print(f"    UNEXPECTED extra changed patches: {sorted(extra)}")
        if absent:
            print(f"    runway patches that did NOT change (problem): {sorted(absent)}")

    all_ok = g1 and g2 and g3
    print(f"\nRESULT: {'ALL GATES PASS - safe to install' if all_ok else 'FAIL - do NOT install'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
