#!/usr/bin/env python3
"""NorthMacedonia — build all 1280 patch DDS textures (MK cadastre over ESRI base,
feather-blended at the national border).

Per patch (exact UTM-34N box from condor_grid.patch_bounds_utm):
  1. Warp the MK zoom-8 cadastre VRT (EPSG:6316 -> UTM-34N) to 2048x2048.
     Inside MK this is high-grade 2.24 m/px ortho; outside MK it is blank.
  2. Load the ESRI World Imagery base PNG (nm_download_esri_patches.py) — covers
     the WHOLE extent incl. the AL/GR/BG/RS/XK border margins.
  3. Build a soft alpha = feathered "inside MK boundary" (UTM-34N signed distance,
     ~FEATHER_M ramp) AND "cadastre actually painted a pixel here". Composite
     MK*alpha + ESRI*(1-alpha): pure cadastre in the interior, pure ESRI outside,
     a smooth feather at the border — no hard seam.
  4. nvcompress -bc1 (DXT1, 2048x2048) -> tCCRR.dds, install to the NM Textures dir.

empty.dds (2048x2048 DXT1, black) is written as the gap fallback.

INVARIANTS (spec §6): DDS 2048x2048 per patch, DXT1 dry (~2.79 MB), stored NORTH-UP
(no transpose). Water DXT3 baking is a SEPARATE second pass (nm_bake_water.py).

Deterministic (fixed boxes + seeded nothing) and parallel (warp pool + bounded GPU).

Run:  CONDOR_LANDSCAPE=nm python scripts/nm_build_textures.py [warp_workers]
"""
import os
import sys
import time
import threading
import subprocess
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("CONDOR_LANDSCAPE", "nm")
from condor_grid import LANDSCAPE_NAME, PATCHES_X, PATCHES_Y, patch_bounds_utm

ROOT = Path(__file__).resolve().parent.parent
NMDIR = ROOT / ".sandbox" / "textures_nm"
MK_VRT = NMDIR / "mk_z8.vrt"
ESRI_DIR = NMDIR / "esri_patch"
WORK = NMDIR / "patch_work"
WORK.mkdir(parents=True, exist_ok=True)
MK_UTM_WKT = NMDIR / "mk_boundary_utm.wkt"
LOG = ROOT / "logs" / "nm_textures.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

CONDOR_TEX = Path(f"C:/Condor2/Landscapes/{LANDSCAPE_NAME}/Textures")
GDALWARP = "C:/Program Files/QGIS 4.0.0/bin/gdalwarp.exe"
NVCOMPRESS = "C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe"

TEX = 2048
FEATHER_M = 600.0          # border blend half-width (metres)
PATCH_M = 5760.0
MPP = PATCH_M / TEX        # 2.8125 m/px

assert LANDSCAPE_NAME == "NorthMacedonia", f"wrong landscape: {LANDSCAPE_NAME}"

_GPU = threading.Semaphore(6)
_lock = threading.Lock()
_done = [0]
_t0 = time.time()
TOTAL = PATCHES_X * PATCHES_Y

# Lazy globals (loaded in main, shared read-only across threads)
_mk_poly = None
_scipy_edt = None


def _log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        print(line, flush=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")


def _inside_mask(col, row):
    """Feathered alpha in [0,1] for 'inside MK boundary' over this patch's box,
    as a (TEX,TEX) float32 north-up array (row 0 = north). Smooth ramp of width
    ~FEATHER_M straddling the border; 1 well inside MK, 0 well outside."""
    from rasterio.features import rasterize  # noqa
    from shapely.geometry import box
    e_min, n_min, e_max, n_max = patch_bounds_utm(col, row)
    pbox = box(e_min, n_min, e_max, n_max)
    # Fast path: patch entirely inside or outside MK -> constant mask, no EDT.
    if _mk_poly.contains(pbox):
        return np.ones((TEX, TEX), np.float32)
    if not _mk_poly.intersects(pbox):
        return np.zeros((TEX, TEX), np.float32)
    inter = _mk_poly.intersection(pbox)
    if inter.is_empty:
        return np.zeros((TEX, TEX), np.float32)
    # Rasterize MK interior at TEX resolution, north-up (transform: ulx,res,0,uly,0,-res)
    transform = (e_min, MPP, 0.0, n_max, 0.0, -MPP)
    from affine import Affine
    aff = Affine.from_gdal(*transform)
    inside = rasterize([(inter, 1)], out_shape=(TEX, TEX), transform=aff,
                       fill=0, dtype="uint8", all_touched=True).astype(bool)
    # Signed distance (px) from the border: + inside, - outside.
    edt = _scipy_edt
    d_in = edt(inside)            # distance to nearest outside px, for inside px
    d_out = edt(~inside)          # distance to nearest inside px, for outside px
    signed = np.where(inside, d_in, -d_out).astype(np.float32) * MPP  # metres
    # Smoothstep ramp over [-FEATHER_M/2, +FEATHER_M/2].
    a = np.clip((signed + FEATHER_M / 2) / FEATHER_M, 0.0, 1.0)
    return (a * a * (3 - 2 * a)).astype(np.float32)


def _warp_mk(col, row, dst_tif):
    e_min, n_min, e_max, n_max = patch_bounds_utm(col, row)
    cmd = [GDALWARP, "-s_srs", "EPSG:6316", "-t_srs", "EPSG:32634",
           "-te", str(e_min), str(n_min), str(e_max), str(n_max),
           "-ts", str(TEX), str(TEX), "-r", "bilinear", "-ot", "Byte",
           "-of", "GTiff", "-co", "COMPRESS=NONE", "-overwrite",
           "-wo", "NUM_THREADS=2", "-q", str(MK_VRT), str(dst_tif)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and dst_tif.exists()


def process_patch(col, row):
    name = f"t{col:02d}{row:02d}"
    dst = CONDOR_TEX / f"{name}.dds"
    if dst.exists() and dst.stat().st_size > 2_000_000:
        with _lock:
            _done[0] += 1
        return name, "cached"

    from PIL import Image
    esri_png = ESRI_DIR / f"{name}.png"
    e_min, n_min, e_max, n_max = patch_bounds_utm(col, row)
    pbox_inside_mk = None

    # --- ESRI base (must exist; it is the universal layer) ---
    esri = None
    if esri_png.exists():
        esri = np.asarray(Image.open(esri_png).convert("RGB"), dtype=np.float32)

    # --- MK cadastre warp (skip entirely for patches with no MK overlap) ---
    from shapely.geometry import box
    overlaps_mk = _mk_poly.intersects(box(e_min, n_min, e_max, n_max))
    mk = None
    if overlaps_mk:
        mk_tif = WORK / f"{name}_mk.tif"
        if _warp_mk(col, row, mk_tif):
            mk = np.asarray(Image.open(mk_tif).convert("RGB"), dtype=np.float32)
            mk_tif.unlink(missing_ok=True)

    # --- Decide the composite ---
    if mk is not None and esri is not None:
        alpha = _inside_mask(col, row)                      # 1=MK, 0=ESRI
        mk_painted = (mk.sum(2) > 18).astype(np.float32)    # cadastre data present
        # Soften the data-presence edge a touch so cadastre holes feather too.
        a = alpha * mk_painted
        a = a[..., None]
        comp = (mk * a + esri * (1.0 - a)).astype(np.uint8)
    elif mk is not None:                                    # no ESRI (shouldn't happen)
        comp = mk.astype(np.uint8)
    elif esri is not None:
        comp = esri.astype(np.uint8)
    else:
        _log(f"{name} NO SOURCE (esri+mk both missing) -> empty fallback")
        return name, "nosrc"

    png = WORK / f"{name}.png"
    Image.fromarray(comp, "RGB").save(png)
    dds_tmp = WORK / f"{name}.dds"
    with _GPU:
        subprocess.run([NVCOMPRESS, "-bc1", "-highest", "-mipfilter", "kaiser",
                        "-color", "-clamp", "-silent", str(png), str(dds_tmp)],
                       capture_output=True)
    png.unlink(missing_ok=True)
    if not dds_tmp.exists():
        _log(f"{name} NVCOMPRESS FAIL")
        return name, "nvfail"
    shutil.copy2(str(dds_tmp), str(dst))
    dds_tmp.unlink(missing_ok=True)

    with _lock:
        _done[0] += 1
        n = _done[0]
    if n % 40 == 0 or n == TOTAL:
        el = time.time() - _t0
        rate = n / el if el else 0
        eta = (TOTAL - n) / rate if rate else 0
        src = "MK+ESRI" if mk is not None else "ESRI"
        _log(f"[{n}/{TOTAL}] {name} {src} {dst.stat().st_size // 1024}KB | "
             f"{el:.0f}s | {rate:.2f}/s | ETA {eta:.0f}s")
    return name, "ok"


def create_empty_dds():
    from PIL import Image
    bp = WORK / "empty.png"
    Image.new("RGB", (TEX, TEX), (0, 0, 0)).save(bp)
    ed = WORK / "empty.dds"
    subprocess.run([NVCOMPRESS, "-bc1", "-highest", "-silent", str(bp), str(ed)],
                   capture_output=True)
    bp.unlink(missing_ok=True)
    if ed.exists():
        shutil.copy2(str(ed), str(CONDOR_TEX / "empty.dds"))
        _log(f"empty.dds {ed.stat().st_size} bytes")


def main():
    global _mk_poly, _scipy_edt
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    CONDOR_TEX.mkdir(parents=True, exist_ok=True)
    if not MK_VRT.exists():
        raise SystemExit("MK VRT missing; run nm_build_mk_vrt.py")
    from shapely import wkt as _wkt
    _mk_poly = _wkt.loads(MK_UTM_WKT.read_text())
    from scipy.ndimage import distance_transform_edt as _edt
    _scipy_edt = _edt

    open(LOG, "a").write(f"\n=== START TEXTURES {LANDSCAPE_NAME} {TOTAL} patches "
                         f"workers={workers} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    _log(f"Build {TOTAL} patch DDS -> {CONDOR_TEX} (warp_workers={workers}, GPU=6, "
         f"feather={FEATHER_M:.0f}m)")
    create_empty_dds()

    patches = [(c, r) for r in range(PATCHES_Y) for c in range(PATCHES_X)]
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_patch, c, r): (c, r) for c, r in patches}
        for f in as_completed(futs):
            try:
                nm, st = f.result()
                results[nm] = st
            except Exception as e:
                _log(f"EXC {futs[f]}: {e}")
    el = time.time() - _t0
    ok = sum(1 for v in results.values() if v in ("ok", "cached"))
    bad = {k: v for k, v in results.items() if v not in ("ok", "cached")}
    n_dds = len(list(CONDOR_TEX.glob("t*.dds")))
    _log(f"DONE in {el:.0f}s. ok={ok}/{TOTAL} installed_dds={n_dds} bad={bad}")
    if bad:
        (NMDIR / "texture_fails.txt").write_text("\n".join(f"{k} {v}" for k, v in bad.items()))


if __name__ == "__main__":
    main()
