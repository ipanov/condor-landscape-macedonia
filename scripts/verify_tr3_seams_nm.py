#!/usr/bin/env python3
"""
Verify shared-edge continuity (mesh-tear check) across ALL neighbouring .tr3
patches of the installed landscape. Grid-driven via condor_grid.

The stored .tr3 is the ANTI-TRANSPOSE of a north-up patch (patch.T[::-1,::-1]),
which is self-inverse: applying it again recovers the north-up patch with
row 0 = NORTH, col 0 = WEST. We read every patch back to north-up and check:
  * horizontal neighbours (c, c+1): patch c+1 is WEST of patch c, so the WEST
    column of c must equal the EAST column of c+1 (they share that vertex line).
  * vertical neighbours   (r, r+1): patch r+1 is NORTH of patch r, so the NORTH
    row of r+1 must equal the SOUTH row of r.

Reports the max absolute mismatch (metres) over all shared edges. PASS = 0 m.
"""

import sys
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import condor_grid as g  # noqa: E402

N = 193
PX, PY = g.PATCHES_X, g.PATCHES_Y
HM = Path(f"C:/Condor2/Landscapes/{g.LANDSCAPE_NAME}/HeightMaps")


def read_northup(c, r):
    """Load hCCRR.tr3 and undo the anti-transpose -> north-up (row0=N, col0=W)."""
    a = np.fromfile(HM / f"h{c:02d}{r:02d}.tr3", dtype="<u2").reshape(N, N)
    return a.T[::-1, ::-1]  # self-inverse


def main():
    # Cache all patches as north-up.
    grid = {}
    missing = []
    for c in range(PX):
        for r in range(PY):
            p = HM / f"h{c:02d}{r:02d}.tr3"
            if not p.exists():
                missing.append((c, r))
                continue
            grid[(c, r)] = read_northup(c, r)
    if missing:
        print(f"MISSING {len(missing)} patches, e.g. {missing[:5]}")

    max_h = 0.0  # horizontal (E-W) neighbour mismatch
    max_v = 0.0  # vertical (N-S) neighbour mismatch
    worst_h = worst_v = None
    n_h = n_v = 0

    for (c, r), A in grid.items():
        # horizontal: c+1 is WEST of c.
        if (c + 1, r) in grid:
            B = grid[(c + 1, r)]
            # WEST column of A (col 0) vs EAST column of B (col -1).
            d = np.abs(A[:, 0].astype(np.int32) - B[:, -1].astype(np.int32)).max()
            n_h += 1
            if d > max_h:
                max_h, worst_h = d, (c, r)
        # vertical: r+1 is NORTH of r.
        if (c, r + 1) in grid:
            B = grid[(c, r + 1)]
            # NORTH row of A (row 0) vs SOUTH row of B (row -1).
            d = np.abs(A[0, :].astype(np.int32) - B[-1, :].astype(np.int32)).max()
            n_v += 1
            if d > max_v:
                max_v, worst_v = d, (c, r)

    print(f"Landscape {g.LANDSCAPE_NAME}: {len(grid)} patches ({PX}x{PY})")
    print(f"  horizontal seams checked: {n_h}  max mismatch: {max_h} m  worst@{worst_h}")
    print(f"  vertical   seams checked: {n_v}  max mismatch: {max_v} m  worst@{worst_v}")
    ok = (max_h == 0 and max_v == 0 and not missing)
    print("  RESULT:", "PASS (all shared edges 0 m)" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
