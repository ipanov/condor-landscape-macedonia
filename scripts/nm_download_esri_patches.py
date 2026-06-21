#!/usr/bin/env python3
"""NorthMacedonia — per-patch ESRI World Imagery base layer (server-reprojected).

For the full NM grid this fetches ONE ESRI ExportImage request per patch, with the
patch's exact UTM-34N box (from condor_grid.patch_bounds_utm), reprojected
server-side to 2048x2048. This is the universal base layer: it covers the whole
extent including the AL/GR/BG/RS/XK border margins where the MK cadastre has no
data. MK-interior patches are later overlaid with the higher-grade cadastre ortho
and feather-blended at the national border (see nm_build_textures.py).

Output: .sandbox/textures_nm/esri_patch/tCCRR.png  (kept as PNG for compositing;
the final DDS is produced by nm_build_textures.py, not here).

Deterministic (patch boxes are fixed) and parallel (many download workers).

Run:  CONDOR_LANDSCAPE=nm python scripts/nm_download_esri_patches.py [workers]
"""
import os
import sys
import time
import threading
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("CONDOR_LANDSCAPE", "nm")
from condor_grid import (
    LANDSCAPE_NAME, PATCHES_X, PATCHES_Y, patch_bounds_utm,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".sandbox" / "textures_nm" / "esri_patch"
OUT.mkdir(parents=True, exist_ok=True)
LOG = ROOT / "logs" / "nm_esri.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

TEX = 2048
EXPORT = ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
          "MapServer/export")

assert LANDSCAPE_NAME == "NorthMacedonia", f"wrong landscape: {LANDSCAPE_NAME}"

_lock = threading.Lock()
_done = [0]
_t0 = time.time()
TOTAL = PATCHES_X * PATCHES_Y


def _log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        print(line, flush=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")


def esri_url(b):
    e_min, n_min, e_max, n_max = b
    return (f"{EXPORT}?bbox={e_min},{n_min},{e_max},{n_max}"
            f"&bboxSR=32634&imageSR=32634&size={TEX},{TEX}&format=jpg&f=image")


def fetch_patch(col, row):
    name = f"t{col:02d}{row:02d}"
    png = OUT / f"{name}.png"
    if png.exists() and png.stat().st_size > 5000:
        with _lock:
            _done[0] += 1
        return name, "cached"
    b = patch_bounds_utm(col, row)
    jpg = OUT / f"{name}.jpg"
    for attempt in range(5):
        try:
            req = urllib.request.Request(esri_url(b),
                                         headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=120).read()
            if len(data) < 3000:
                raise ValueError(f"tiny response {len(data)}B")
            jpg.write_bytes(data)
            break
        except Exception as e:
            if attempt == 4:
                _log(f"{name} DL FAIL: {e}")
                return name, "failed"
            time.sleep(1.5 * (attempt + 1))
    # Normalise to PNG RGB (compositor reads PNG)
    try:
        from PIL import Image
        Image.open(jpg).convert("RGB").save(png)
    except Exception as e:
        _log(f"{name} PNG FAIL: {e}")
        return name, "failed"
    jpg.unlink(missing_ok=True)
    with _lock:
        _done[0] += 1
        n = _done[0]
    if n % 40 == 0 or n == TOTAL:
        el = time.time() - _t0
        rate = n / el if el else 0
        eta = (TOTAL - n) / rate if rate else 0
        _log(f"[{n}/{TOTAL}] {name} OK | {el:.0f}s | {rate:.2f}/s | ETA {eta:.0f}s")
    return name, "ok"


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    open(LOG, "a").write(f"\n=== START ESRI {LANDSCAPE_NAME} {TOTAL} patches @ {TEX} "
                         f"workers={workers} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    _log(f"ESRI base: {TOTAL} patches, {workers} workers -> {OUT}")
    patches = [(c, r) for r in range(PATCHES_Y) for c in range(PATCHES_X)]
    fails = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_patch, c, r): (c, r) for c, r in patches}
        for f in as_completed(futs):
            nm, st = f.result()
            if st == "failed":
                fails.append(nm)
    el = time.time() - _t0
    _log(f"DONE ESRI in {el:.0f}s. fails={len(fails)} {fails[:20]}")
    if fails:
        (ROOT / ".sandbox" / "textures_nm" / "esri_fails.txt").write_text("\n".join(fails))


if __name__ == "__main__":
    main()
