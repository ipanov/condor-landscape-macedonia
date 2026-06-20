#!/usr/bin/env python3
"""
Fast rebuild of all 144 MacedoniaSkopje patch textures from Esri World Imagery.

- One direct ExportImage request per patch (UTM 34N bbox -> 2048x2048 JPG),
  server-side reprojection, so no tile stitching.
- Patch bounds match the .tr3 mesh patches EXACTLY (DEM 30 m grid, XDIM step),
  so textures align to the terrain.
- Parallel downloads (saturate bandwidth) + GPU nvcompress (bounded) pipeline:
  each patch is compressed and installed as soon as its image arrives.
- Progress + ETA logged to .sandbox/texture_rebuild_progress.log every patch.
"""
import urllib.request, time, subprocess, shutil, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".sandbox" / "esri_patch"; WORK.mkdir(parents=True, exist_ok=True)
CONDOR_TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
NVCOMPRESS = "C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe"
LOG = ROOT / ".sandbox" / "texture_rebuild_progress.log"

ULXMAP = 506880.0; ULYMAP = 4700160.0; XDIM = 29.9869848156182
PATCHES = 12; INTERVAL = 192; TEX = 2048; TOTAL = PATCHES * PATCHES
DL_WORKERS = 16
GPU = threading.Semaphore(4)

def bounds(col, row):
    # Exactly the .tr3 patch extent: col=0 east, row=0 south
    j = (PATCHES - 1 - col) * INTERVAL
    i = (PATCHES - 1 - row) * INTERVAL
    e_min = ULXMAP + j * XDIM
    e_max = ULXMAP + (j + INTERVAL) * XDIM
    n_max = ULYMAP - i * XDIM
    n_min = ULYMAP - (i + INTERVAL) * XDIM
    return e_min, n_min, e_max, n_max

def esri_url(b):
    e_min, n_min, e_max, n_max = b
    return ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export?"
            f"bbox={e_min},{n_min},{e_max},{n_max}&bboxSR=32634&imageSR=32634"
            f"&size={TEX},{TEX}&format=jpg&f=image")

lock = threading.Lock(); done = [0]; t0 = time.time()

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with lock:
        print(line, flush=True)
        with open(LOG, "a") as f: f.write(line + "\n")

def process(col, row):
    name = f"t{col:02d}{row:02d}"
    jpg = WORK / f"{name}.jpg"; png = WORK / f"{name}.png"; dds = WORK / f"{name}.dds"
    for attempt in range(4):
        try:
            req = urllib.request.Request(esri_url(bounds(col, row)), headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=90).read()
            if len(data) < 2000: raise ValueError("tiny response")
            jpg.write_bytes(data); break
        except Exception as e:
            if attempt == 3:
                log(f"{name} DOWNLOAD FAIL: {e}"); return name, False
            time.sleep(1.5 * (attempt + 1))
    try:
        Image.open(jpg).convert("RGB").save(png)
    except Exception as e:
        log(f"{name} PNG FAIL: {e}"); return name, False
    with GPU:
        subprocess.run([NVCOMPRESS, "-bc1", "-highest", "-silent", str(png), str(dds)], capture_output=True)
    if not dds.exists():
        log(f"{name} NVCOMPRESS FAIL"); return name, False
    shutil.copy2(str(dds), str(CONDOR_TEX / f"{name}.dds"))
    jpg.unlink(missing_ok=True); png.unlink(missing_ok=True)
    with lock:
        done[0] += 1; n = done[0]
    el = time.time() - t0; rate = n / el if el > 0 else 0; eta = (TOTAL - n) / rate if rate > 0 else 0
    log(f"[{n}/{TOTAL}] {name} OK | {el:.0f}s | {rate:.2f}/s | ETA {eta:.0f}s")
    return name, True

def main():
    open(LOG, "w").close()
    log(f"START Esri rebuild: {TOTAL} patches @ {TEX}x{TEX} DXT1, {DL_WORKERS} dl workers")
    patches = [(c, r) for r in range(PATCHES) for c in range(PATCHES)]
    fails = []
    with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
        futs = {ex.submit(process, c, r): (c, r) for c, r in patches}
        for f in as_completed(futs):
            nm, ok = f.result()
            if not ok: fails.append(nm)
    log(f"DONE in {time.time()-t0:.0f}s. OK={done[0]}/{TOTAL} fails={fails}")

if __name__ == "__main__":
    main()
