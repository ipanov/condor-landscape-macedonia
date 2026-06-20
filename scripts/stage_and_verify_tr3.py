#!/usr/bin/env python3
"""
Stage corrected .tr3 patches and PROVE the orientation fix on our actual data.

Builds 144 patches from the canonical exactly-30m DEM using the verified
anti-transpose storage op, writes them to a STAGING dir (does NOT touch the
Condor install), then simulates how Condor READS each patch and checks that
every shared edge between neighbouring patches is bit-exact.

Condor reads a stored .tr3 with +row=WEST, +col=NORTH (Slovenia2-verified).
That read is the anti-transpose `S.T[::-1,::-1]`, which is its own inverse:
  - NEW storage  = north_up.T[::-1,::-1]  -> Condor reconstructs north_up  -> seams 0
  - OLD storage  = north_up (identity)    -> Condor sees garbage           -> seams huge
"""
import numpy as np
from pathlib import Path

WIDTH = HEIGHT = 2305
SAMPLES = 193
INTERVAL = 192
PX = PY = 12

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305.raw"
STAGE = ROOT / "output" / "HeightMaps_staging"


def antitranspose(a):
    return a.T[::-1, ::-1]


def north_up_slice(src, c, r):
    j = (PX - 1 - c) * INTERVAL          # west-from-east
    i = (PY - 1 - r) * INTERVAL          # north-from-south (row idx grows southward)
    return src[i:i + SAMPLES, j:j + SAMPLES]


def main():
    src = np.fromfile(SRC, dtype=np.int16).reshape(HEIGHT, WIDTH)
    src = np.where(src < 0, 0, src).astype(np.uint16)
    STAGE.mkdir(parents=True, exist_ok=True)

    stored_new, stored_old = {}, {}
    for c in range(PX):
        for r in range(PY):
            nu = north_up_slice(src, c, r)
            stored_new[(c, r)] = antitranspose(nu)      # corrected storage
            stored_old[(c, r)] = nu                       # buggy identity storage
            (stored_new[(c, r)]).astype(np.uint16).tofile(STAGE / f"h{c:02d}{r:02d}.tr3")

    def max_seam(stored):
        worst = 0
        for c in range(PX):
            for r in range(PY):
                cv = antitranspose(stored[(c, r)]).astype(int)   # what Condor reconstructs
                if c + 1 < PX:                                    # west neighbour
                    nb = antitranspose(stored[(c + 1, r)]).astype(int)
                    worst = max(worst, np.abs(cv[:, 0] - nb[:, INTERVAL]).max())
                if r + 1 < PY:                                    # north neighbour
                    nb = antitranspose(stored[(c, r + 1)]).astype(int)
                    worst = max(worst, np.abs(cv[0, :] - nb[INTERVAL, :]).max())
        return worst

    new_seam = max_seam(stored_new)
    old_seam = max_seam(stored_old)
    n = len(list(STAGE.glob("*.tr3")))
    sz = (STAGE / "h0000.tr3").stat().st_size

    print(f"Staged {n} .tr3 to {STAGE}  (each {sz} bytes, expect 74498)")
    print(f"Elevation range: {src.min()}..{src.max()} m")
    print(f"MAX seam mismatch  NEW (anti-transpose): {new_seam} m   <- expect 0")
    print(f"MAX seam mismatch  OLD (identity bug)  : {old_seam} m   <- the tears you saw")
    print("RESULT:", "PASS - mesh tiles seamlessly" if new_seam == 0 else "FAIL")


if __name__ == "__main__":
    main()
