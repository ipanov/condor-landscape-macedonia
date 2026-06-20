#!/usr/bin/env python3
"""Launch 4 parallel orthophoto download processes for the full tile grid."""
import os
import sys
import math
import time
import subprocess
from pathlib import Path
from pyproj import Transformer

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "download_mk_ortho_2023_zoom11.py"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ORIGIN_X = 7397000.634424793
ORIGIN_Y = 4521901.793180252
RES = 0.28
TILE_SIZE_PX = 256
TILE_SIZE_M = TILE_SIZE_PX * RES
UTM_E_MIN, UTM_E_MAX = 506880.0, 575970.011
UTM_N_MIN, UTM_N_MAX = 4631069.989, 4700160.0

t = Transformer.from_crs("EPSG:32634", "EPSG:6316", always_xy=True)
xs, ys = [], []
for e, n in [(UTM_E_MIN, UTM_N_MIN), (UTM_E_MIN, UTM_N_MAX), (UTM_E_MAX, UTM_N_MIN), (UTM_E_MAX, UTM_N_MAX)]:
    x, y = t.transform(e, n)
    xs.append(x)
    ys.append(y)
X_MIN, X_MAX = min(xs), max(xs)
Y_MIN, Y_MAX = min(ys), max(ys)

TX_MIN = int(math.floor((X_MIN - ORIGIN_X) / TILE_SIZE_M))
TX_MAX = int(math.floor((X_MAX - ORIGIN_X) / TILE_SIZE_M)) + 1
TY_MIN = int(math.floor((Y_MIN - ORIGIN_Y) / TILE_SIZE_M))
TY_MAX = int(math.floor((Y_MAX - ORIGIN_Y) / TILE_SIZE_M)) + 1

MX = (TX_MIN + TX_MAX) // 2
MY = (TY_MIN + TY_MAX) // 2

REGIONS = [
    ("q0_SW", TX_MIN, MX, TY_MIN, MY),
    ("q1_SE", MX + 1, TX_MAX, TY_MIN, MY),
    ("q2_NW", TX_MIN, MX, MY + 1, TY_MAX),
    ("q3_NE", MX + 1, TX_MAX, MY + 1, TY_MAX),
]

CONCURRENCY = int(sys.argv[1]) if len(sys.argv) > 1 else 128
CACHE_INDEX = str(ROOT / ".sandbox" / "textures_mk2023_z11" / "cache_index.txt")


def tail_log(path, n=5):
    try:
        with open(path, "r", errors="ignore") as f:
            lines = f.readlines()
            return "".join(lines[-n:]).strip()
    except Exception:
        return ""


def main():
    print(f"Launching {len(REGIONS)} download regions with concurrency={CONCURRENCY}", flush=True)
    procs = []
    for name, tx_min, tx_max, ty_min, ty_max in REGIONS:
        log_path = LOG_DIR / f"download_{name}.log"
        logf = open(log_path, "w")
        cmd = [
            sys.executable, str(SCRIPT),
            str(tx_min), str(tx_max), str(ty_min), str(ty_max), str(CONCURRENCY), CACHE_INDEX
        ]
        p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, text=True)
        procs.append((name, p, log_path, logf))
        print(f"  {name}: pid={p.pid} log={log_path}", flush=True)

    try:
        while any(p.poll() is None for _, p, _, _ in procs):
            time.sleep(30)
            print("\n--- 30s progress report ---", flush=True)
            for name, p, log_path, _ in procs:
                status = "running" if p.poll() is None else f"done ({p.returncode})"
                tail = tail_log(log_path, 3)
                print(f"[{name}] {status}", flush=True)
                if tail:
                    for line in tail.splitlines():
                        print(f"  {line}", flush=True)
    except KeyboardInterrupt:
        print("Interrupted, terminating downloads...", flush=True)
        for _, p, _, _ in procs:
            p.terminate()

    # Final status
    print("\n=== Final download status ===", flush=True)
    for name, p, _, logf in procs:
        logf.close()
        print(f"{name}: exit={p.returncode}", flush=True)


if __name__ == "__main__":
    main()
