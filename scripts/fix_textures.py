#!/usr/bin/env python3
"""
Fix discolored / data-hole MacedoniaSkopje patch DDS textures, IN PLACE from the
installed DDS where possible (no re-warp of the incomplete ortho).

Two operations, run in this order:

  1. REFILL data-hole tiles  : t0605, t0705
     The MK WMS zoom-11 rows y=1935..2018 were never downloaded, so these two
     patches render as a milky / washed rectangle. We refill the imagery:
       (a) MK cadastre WMS gap (GRIDSET MSCS6316, EPSG:6316) if the server is up;
       (b) FALLBACK Esri World Imagery for the patch bounds.
     The refilled source is warped to 2048x2048 NORTH-UP on the SAME grid the
     installed neighbours use (the Jun-13 Esri rebuild grid: XDIM=29.987,
     (PATCHES-1-col)*INTERVAL), so the new tile aligns pixel-wise with its
     neighbours. Optionally Reinhard-matched in LAB to the MK neighbours so the
     substituted imagery blends. Recompressed DXT1 (water alpha is added later
     by bake_water.py).

  2. COLOR-CORRECT brown tiles : t0606, t0706, t0607, t0707
     Mild warm white-balance cast (LAB A shifted ~+3 vs good green neighbours).
     Apply CLAMPED Reinhard mean/std colour transfer in LAB toward the reference
     median (validation/lab_transfer_params.json). The A correction is clamped to
     about the measured +3 shift and pixels already within tolerance are skipped
     so real features are not washed out. Recompressed DXT1 (none of these four
     carries water, so they stay DXT1).

Backs up every overwritten DDS to Textures_bak_phase1/ first.

Run BEFORE bake_water.py: t0605/t0705 also need water, so their RGB must be
refilled before their alpha is baked.
"""
import sys
import json
import shutil
import subprocess
import urllib.request
from urllib.parse import urlencode
from pathlib import Path

import numpy as np
from PIL import Image
from skimage import color as skcolor

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".sandbox" / "fix_textures"
VALID = ROOT / "validation" / "textures"
CONDOR_TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
BACKUP = CONDOR_TEX.parent / "Textures_bak_phase1"
NVCOMPRESS = "C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe"
GDALWARP = "C:/Program Files/QGIS 4.0.0/bin/gdalwarp.exe"
GDAL_TRANSLATE = "C:/Program Files/QGIS 4.0.0/bin/gdal_translate.exe"
PARAMS = ROOT / "validation" / "lab_transfer_params.json"

TEX = 2048

# ---- grid: match the installed (Jun-13 Esri rebuild) tiles ----
ULXMAP = 506880.0
ULYMAP = 4700160.0
XDIM_INSTALLED = 29.9869848156182   # the grid the installed neighbours use
PATCHES = 12
INTERVAL = 192


def installed_grid_bounds(col, row):
    """Patch UTM bounds on the SAME grid the installed tiles were built on."""
    j = (PATCHES - 1 - col) * INTERVAL
    i = (PATCHES - 1 - row) * INTERVAL
    e_min = ULXMAP + j * XDIM_INSTALLED
    e_max = ULXMAP + (j + INTERVAL) * XDIM_INSTALLED
    n_max = ULYMAP - i * XDIM_INSTALLED
    n_min = ULYMAP - (i + INTERVAL) * XDIM_INSTALLED
    return e_min, n_min, e_max, n_max


# ---- MK cadastre WMS (direct GeoServer endpoint, arbitrary GetMap) ----
# The gwc/wms tile-cache endpoint only serves a fixed tile grid (rejects
# arbitrary GetMap with HTTP 400). The direct GeoServer WMS below accepts any
# BBOX/WIDTH/HEIGHT, so we fetch a whole patch in one request and let GDAL
# reproject EPSG:6316 -> EPSG:32634.
MK_WMS = "https://e-uslugi.katastar.gov.mk/geo/proxy/geoserver/wms"
MK_HEADERS = {"Referer": "https://e-uslugi.katastar.gov.mk/",
              "User-Agent": "Mozilla/5.0"}


def _mk_getmap(bbox_6316, size, fmt="image/jpeg"):
    params = {
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": "APP_DATA:ORTOFOTO_2023", "STYLES": "",
        "FORMAT": fmt, "SRS": "EPSG:6316",
        "BBOX": ",".join(str(v) for v in bbox_6316),
        "WIDTH": size, "HEIGHT": size,
    }
    url = f"{MK_WMS}?{urlencode(params)}"
    req = urllib.request.Request(url, headers=MK_HEADERS)
    return urllib.request.urlopen(req, timeout=120).read()


def backup_dds(name):
    src = CONDOR_TEX / f"{name}.dds"
    dst = BACKUP / f"{name}.dds"
    if src.exists() and not dst.exists():
        shutil.copy2(src, dst)


# ===========================================================================
# REFILL
# ===========================================================================
def fetch_mk_patch(e_min, n_min, e_max, n_max, name):
    """Fetch the patch from the MK WMS as ONE GetMap (in EPSG:6316, oversampled),
    georeference it, and return a VRT path in EPSG:6316. None if WMS unusable.

    Oversample 2x (4096) so the subsequent warp to UTM34 keeps full detail."""
    import pyproj
    t = pyproj.Transformer.from_crs("EPSG:32634", "EPSG:6316", always_xy=True)
    xs, ys = [], []
    for ex, ny in [(e_min, n_min), (e_min, n_max), (e_max, n_min), (e_max, n_max)]:
        x, y = t.transform(ex, ny)
        xs.append(x); ys.append(y)
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    # pad slightly so the rotated UTM patch is fully covered by the 6316 bbox
    pad = 60.0
    x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
    size = 4096
    try:
        data = _mk_getmap((x0, y0, x1, y1), size)
    except Exception as e:
        print(f"    MK WMS GetMap error: {type(e).__name__}: {str(e)[:120]}")
        return None
    if len(data) < 5000 or data[:2] != b"\xff\xd8":
        print(f"    MK WMS returned non-image / tiny ({len(data)}B)")
        return None
    jpg = WORK / f"{name}_mk.jpg"
    jpg.write_bytes(data)
    # sanity: reject near-uniform / milky responses
    arr = np.array(Image.open(jpg).convert("RGB")).reshape(-1, 3)
    if (arr > 235).all(axis=1).mean() > 0.5 or arr.std() < 5:
        print("    MK WMS response looks blank/milky; rejecting")
        return None
    # world file in EPSG:6316: top-left origin, y decreasing down.
    res_x = (x1 - x0) / size
    res_y = (y1 - y0) / size
    (jpg.with_suffix(".jpw")).write_text(
        f"{res_x}\n0.0\n0.0\n{-res_y}\n{x0 + res_x/2}\n{y1 - res_y/2}\n")
    vrt = WORK / f"{name}_mk.vrt"
    r = subprocess.run([GDAL_TRANSLATE, "-of", "VRT", "-a_srs", "EPSG:6316",
                        str(jpg), str(vrt)], capture_output=True, text=True)
    if r.returncode != 0 or not vrt.exists():
        print(f"    gdal_translate to VRT failed: {r.stderr[:120]}")
        return None
    return vrt


def fetch_esri(e_min, n_min, e_max, n_max, name):
    """Esri World Imagery export (UTM34, server-side reproject) -> GeoTIFF."""
    url = ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
           "MapServer/export?"
           f"bbox={e_min},{n_min},{e_max},{n_max}&bboxSR=32634&imageSR=32634"
           f"&size={TEX},{TEX}&format=jpg&f=image")
    jpg = WORK / f"{name}_esri.jpg"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=90).read()
            if len(data) < 2000:
                raise ValueError("tiny response")
            jpg.write_bytes(data)
            # Esri export with explicit bbox+size IS already the patch extent,
            # north-up, in UTM34. Just open it.
            return jpg
        except Exception:
            continue
    return None


def warp_to_patch(src_vrt, e_min, n_min, e_max, n_max, name):
    """gdalwarp a georeferenced source to 2048x2048 north-up UTM34 PNG path."""
    tif = WORK / f"{name}_warp.tif"
    png = WORK / f"{name}_warp.png"
    cmd = [GDALWARP, "-t_srs", "EPSG:32634",
           "-te", str(e_min), str(n_min), str(e_max), str(n_max),
           "-ts", str(TEX), str(TEX), "-r", "bilinear",
           "-ot", "Byte", "-of", "GTiff", "-co", "COMPRESS=NONE",
           "-overwrite", str(src_vrt), str(tif)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not tif.exists():
        return None, f"gdalwarp failed: {r.stderr[:160]}"
    subprocess.run([GDAL_TRANSLATE, "-of", "PNG", "-b", "1", "-b", "2", "-b", "3",
                    str(tif), str(png)], capture_output=True, text=True)
    tif.unlink(missing_ok=True)
    if not png.exists():
        return None, "gdal_translate to PNG failed"
    return png, "ok"


def neighbor_lab_ref(name):
    """Median LAB mean/std of the 8-neighbourhood installed tiles (good MK)."""
    col = int(name[1:3]); row = int(name[3:5])
    means, stds = [], []
    for dc in (-1, 0, 1):
        for dr in (-1, 0, 1):
            if dc == 0 and dr == 0:
                continue
            nc, nr = col + dc, row + dr
            if not (0 <= nc < PATCHES and 0 <= nr < PATCHES):
                continue
            nm = f"t{nc:02d}{nr:02d}"
            # skip the other hole / discolored tiles as reference
            if nm in ("t0605", "t0705", "t0606", "t0706", "t0607", "t0707"):
                continue
            p = CONDOR_TEX / f"{nm}.dds"
            if not p.exists():
                continue
            lab = skcolor.rgb2lab(np.array(Image.open(p).convert("RGB")) / 255.0)
            f = lab.reshape(-1, 3)
            means.append(f.mean(0)); stds.append(f.std(0))
    if not means:
        return None
    return np.median(means, axis=0), np.median(stds, axis=0)


def reinhard_match(rgb, ref_mean, ref_std, src_mean=None, src_std=None):
    """Full Reinhard LAB transfer (used for refilled imagery blending)."""
    lab = skcolor.rgb2lab(rgb / 255.0)
    f = lab.reshape(-1, 3)
    sm = f.mean(0) if src_mean is None else np.asarray(src_mean)
    ss = f.std(0) if src_std is None else np.asarray(src_std)
    ss = np.where(ss < 1e-3, 1.0, ss)
    out = (f - sm) / ss * ref_std + ref_mean
    out = out.reshape(lab.shape)
    rgb2 = np.clip(skcolor.lab2rgb(out) * 255.0, 0, 255).astype(np.uint8)
    return rgb2


def compress_dxt1(png_path, name, save_preview=False, before_rgb=None):
    dds = WORK / f"{name}.dds"
    cmd = [NVCOMPRESS, "-bc1", "-highest", "-mipfilter", "kaiser",
           "-color", "-clamp", "-silent", str(png_path), str(dds)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dds.exists():
        return None, f"nvcompress failed: {r.stderr[:160]}"
    backup_dds(name)
    shutil.copy2(dds, CONDOR_TEX / f"{name}.dds")
    sz = (CONDOR_TEX / f"{name}.dds").stat().st_size
    if save_preview:
        VALID.mkdir(parents=True, exist_ok=True)
        after = np.array(Image.open(png_path).convert("RGB"))
        Image.fromarray(after).resize((512, 512)).save(VALID / f"{name}_after.png")
        if before_rgb is not None:
            Image.fromarray(before_rgb).resize((512, 512)).save(VALID / f"{name}_before.png")
    dds.unlink(missing_ok=True)
    return sz, "ok"


def lab_stat_str(rgb):
    lab = skcolor.rgb2lab(rgb / 255.0).reshape(-1, 3)
    m = lab.mean(0); s = lab.std(0)
    return f"L={m[0]:.2f} A={m[1]:.2f} B={m[2]:.2f} (std {s[0]:.2f}/{s[1]:.2f}/{s[2]:.2f})"


def do_refill(name, results):
    col = int(name[1:3]); row = int(name[3:5])
    e_min, n_min, e_max, n_max = installed_grid_bounds(col, row)
    before = np.array(Image.open(CONDOR_TEX / f"{name}.dds").convert("RGB"))

    source_used = None
    png = None
    # (a) MK WMS (single GetMap, preferred — matches the rest of the landscape)
    vrt = fetch_mk_patch(e_min, n_min, e_max, n_max, name)
    if vrt is not None:
        png, msg = warp_to_patch(vrt, e_min, n_min, e_max, n_max, name)
        if png is not None:
            source_used = "MK_WMS"
    # (b) Esri fallback
    if png is None:
        ejpg = fetch_esri(e_min, n_min, e_max, n_max, name)
        if ejpg is not None:
            png = ejpg
            source_used = "Esri"
    if png is None:
        results[name] = "REFILL FAILED (both MK and Esri); installed tile kept"
        print(f"  {name}: {results[name]}")
        return

    rgb = np.array(Image.open(png).convert("RGB"))
    before_str = lab_stat_str(before.astype(np.float64))
    # Blend substituted imagery toward MK neighbours so it matches.
    ref = neighbor_lab_ref(name)
    if ref is not None:
        ref_mean, ref_std = ref
        rgb = reinhard_match(rgb.astype(np.float64), ref_mean, ref_std)
    out_png = WORK / f"{name}_refill.png"
    Image.fromarray(rgb).save(out_png)
    sz, msg = compress_dxt1(out_png, name, save_preview=True, before_rgb=before)
    if sz is None:
        results[name] = f"REFILL recompress failed: {msg}"
    else:
        after_str = lab_stat_str(rgb.astype(np.float64))
        results[name] = (f"REFILL OK via {source_used} DXT1 {sz} bytes | "
                         f"before {before_str} -> after {after_str}")
    print(f"  {name}: {results[name]}")


# ===========================================================================
# COLOR CORRECT (clamped Reinhard LAB)
# ===========================================================================
A_CLAMP = 4.0   # max LAB-A correction magnitude (~the measured +3 shift, +margin)
L_CLAMP = 4.0
B_CLAMP = 3.0
SKIP_TOL_A = 1.0  # skip pixels whose A is already within tol of target


def do_color(name, params, results):
    p = CONDOR_TEX / f"{name}.dds"
    before = np.array(Image.open(p).convert("RGB"))
    before_f = before.astype(np.float64)
    lab = skcolor.rgb2lab(before_f / 255.0)
    f = lab.reshape(-1, 3).copy()

    ref = np.asarray(params["_reference"]["mean"])
    ref_std = np.asarray(params["_reference"]["std"])
    src_mean = np.asarray(params[name]["src_mean"])
    src_std = np.asarray(params[name]["src_std"])
    src_std = np.where(src_std < 1e-3, 1.0, src_std)

    # Full Reinhard target per pixel.
    target = (f - src_mean) / src_std * ref_std + ref
    delta = target - f
    # Clamp the per-pixel correction so we only nudge by ~the measured cast.
    delta[:, 0] = np.clip(delta[:, 0], -L_CLAMP, L_CLAMP)
    delta[:, 1] = np.clip(delta[:, 1], -A_CLAMP, A_CLAMP)
    delta[:, 2] = np.clip(delta[:, 2], -B_CLAMP, B_CLAMP)
    # Skip pixels already green enough (A already <= target A within tol):
    # the cast is +A (toward red); only pull A down where it is too high.
    already_ok = (f[:, 1] <= (ref[1] + SKIP_TOL_A))
    delta[already_ok, 1] = np.minimum(delta[already_ok, 1], 0.0)

    out = f + delta
    out = out.reshape(lab.shape)
    rgb2 = np.clip(skcolor.lab2rgb(out) * 255.0, 0, 255).astype(np.uint8)

    out_png = WORK / f"{name}_color.png"
    Image.fromarray(rgb2).save(out_png)
    before_str = lab_stat_str(before_f)
    after_str = lab_stat_str(rgb2.astype(np.float64))
    sz, msg = compress_dxt1(out_png, name, save_preview=True, before_rgb=before)
    if sz is None:
        results[name] = f"COLOR recompress failed: {msg}"
    else:
        results[name] = (f"COLOR OK DXT1 {sz} bytes | before {before_str} -> "
                         f"after {after_str}")
    print(f"  {name}: {results[name]}")


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    BACKUP.mkdir(parents=True, exist_ok=True)
    VALID.mkdir(parents=True, exist_ok=True)
    params = json.load(open(PARAMS))
    results = {}

    print("=== STEP 1: REFILL data-hole tiles (t0605, t0705) ===")
    for name in ("t0605", "t0705"):
        do_refill(name, results)

    print("\n=== STEP 2: COLOR-CORRECT brown tiles (t0606,t0706,t0607,t0707) ===")
    for name in ("t0606", "t0706", "t0607", "t0707"):
        do_color(name, params, results)

    print("\nFIX TEXTURES DONE")
    (ROOT / "validation" / "fix_textures_results.json").write_text(
        json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
