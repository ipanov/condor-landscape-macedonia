#!/usr/bin/env python3
"""
Download and reproject the forest-classification rasters used by
``generate_forest_maps.py``.

Three open datasets are fetched and warped onto the MacedoniaSkopje
landscape grid (UTM 34N / EPSG:32634, exact 30 m, covering the full
12x12-patch extent ``-te 506880 4631040 576000 4700160``):

  1. Copernicus HRL Dominant Leaf Type 2018 (DLT) - species:
        DLT == 1 -> broadleaved (deciduous)
        DLT == 2 -> coniferous
        254/255  -> nodata / unmapped (treated as 0)
     Source: EEA DiscoMap ImageServer (native EPSG:3035, no auth).

  2. Copernicus HRL Tree Cover Density 2018 (TCD) - canopy percent 0-100:
        254/255  -> nodata (treated as 0)
     Source: EEA DiscoMap ImageServer (native EPSG:3035, no auth).

  3. ESA WorldCover 2021 v200 - land cover class:
        class 10 -> tree cover
     Source: public AWS S3 (HTTPS GET, no auth). Tiles N39E021 + N42E021
     cover the landscape latitude band.

All outputs are written to ``.sandbox/forest_rasters/`` as GeoTIFFs already
reprojected to the landscape grid:

    dlt_utm34_30m.tif      (uint8, nearest)
    tcd_utm34_30m.tif      (uint8, bilinear)
    worldcover_utm34_30m.tif (uint8, nearest)

The script verifies each download is a real raster (GeoTIFF magic / GDAL
can open it) before warping, and skips work when a cached output already
exists (use ``--force`` to refetch).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RASTER_DIR = PROJECT_ROOT / ".sandbox" / "forest_rasters"
RAW_DIR = RASTER_DIR / "raw"

GDAL_BIN = Path("C:/Program Files/QGIS 4.0.0/bin")
GDALWARP = str(GDAL_BIN / "gdalwarp.exe")
GDAL_TRANSLATE = str(GDAL_BIN / "gdal_translate.exe")
GDALBUILDVRT = str(GDAL_BIN / "gdalbuildvrt.exe")
GDALINFO = str(GDAL_BIN / "gdalinfo.exe")

# Landscape grid (UTM 34N, EPSG:32634).  Full 12x12-patch extent.
#   NW pixel-centre 506880 / 4700160, exact 30 m, 12*512=... but grid here is
#   the patch extent in metres: 12 patches * 5760 m = 69120 m square.
TARGET_SRS = "EPSG:32634"
TE = ["506880", "4631040", "576000", "4700160"]  # xmin ymin xmax ymax
TR = ["30", "30"]

# EEA DiscoMap ImageServer endpoints (native EPSG:3035).
DISCOMAP = "https://image.discomap.eea.europa.eu/arcgis/rest/services"
DLT_SERVICE = f"{DISCOMAP}/GioLandPublic/HRL_DominantLeafType2018/ImageServer/exportImage"
TCD_SERVICE = f"{DISCOMAP}/GioLandPublic/HRL_TreeCoverDensity_2018/ImageServer/exportImage"

# Bounding box of the landscape in EPSG:3035 (LAEA), padded slightly so the
# warp has full coverage at the edges.  Provided by the task brief.
BBOX_3035 = (5232004.0, 2148649.0, 5309895.0, 2227181.0)  # xmin ymin xmax ymax
# 30 m native pixels => ~2597x2618.  Round up for safety.
EXPORT_W = 2600
EXPORT_H = 2620

# ESA WorldCover 2021 v200 public S3.
WORLDCOVER_TILES = ["N39E021", "N42E021"]
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)

USER_AGENT = "condor-landscape/1.0"
HEADERS = {"User-Agent": USER_AGENT}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _run(cmd):
    """Run a subprocess command, raising on failure."""
    print("  $", " ".join(str(c) for c in cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout)
        print(res.stderr, file=sys.stderr)
        raise RuntimeError(f"command failed ({res.returncode}): {cmd[0]}")
    return res


def _is_geotiff(path: Path) -> bool:
    """Return True if *path* is a real (TIFF) raster gdal can open."""
    if not path.exists() or path.stat().st_size < 1024:
        return False
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic[:2] not in (b"II", b"MM"):
        # Not a TIFF; likely an HTML/JSON error page.
        return False
    # Confirm GDAL can actually open it.
    res = subprocess.run([GDALINFO, str(path)], capture_output=True, text=True)
    return res.returncode == 0


def _download(url: str, path: Path, params=None, expect_tiff=True):
    """GET *url* to *path*; verify it is a real raster when ``expect_tiff``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  GET {url}")
    if params:
        print(f"      params={params}")
    resp = requests.get(url, params=params, headers=HEADERS, timeout=600, stream=True)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if chunk:
                f.write(chunk)
    size = path.stat().st_size
    print(f"      saved {path.name} ({size:,} bytes, content-type={ctype})")
    if expect_tiff and not _is_geotiff(path):
        head = path.read_bytes()[:200]
        raise RuntimeError(
            f"download is not a valid GeoTIFF: {path}\n  first bytes: {head!r}"
        )
    return path


def _warp_to_grid(src, dst, resample, src_srs=None):
    """gdalwarp *src* onto the landscape grid with the given resampling."""
    cmd = [
        GDALWARP,
        "-overwrite",
        "-t_srs", TARGET_SRS,
        "-te", *TE,
        "-tr", *TR,
        "-r", resample,
        "-ot", "Byte",
        "-of", "GTiff",
        "-co", "COMPRESS=DEFLATE",
    ]
    if src_srs:
        cmd += ["-s_srs", src_srs]
    cmd += [str(src), str(dst)]
    _run(cmd)


# -----------------------------------------------------------------------------
# DiscoMap HRL exports (DLT / TCD)
# -----------------------------------------------------------------------------

def _export_discomap(service: str, out_raw: Path, tag: str) -> Path:
    """Export an HRL layer from DiscoMap, tiling 2x2 if a single export fails.

    Returns the path to a single GeoTIFF (or VRT) in EPSG:3035 covering the
    landscape bbox.
    """
    xmin, ymin, xmax, ymax = BBOX_3035
    common = {
        "bboxSR": "3035",
        "imageSR": "3035",
        "format": "tiff",
        "pixelType": "U8",
        "f": "image",
    }

    # 1) Try a single full-extent export first.
    single = out_raw.with_name(f"{tag}_3035_full.tif")
    params = dict(common)
    params["bbox"] = f"{xmin},{ymin},{xmax},{ymax}"
    params["size"] = f"{EXPORT_W},{EXPORT_H}"
    try:
        _download(service, single, params=params)
        # Sanity: ensure it carries georeferencing and is not a tiny stub.
        info = subprocess.run([GDALINFO, str(single)], capture_output=True, text=True)
        if "Coordinate System is" in info.stdout or "PROJCRS" in info.stdout or "3035" in info.stdout:
            print(f"  {tag}: single export OK")
            return single
        print(f"  {tag}: single export missing CRS, falling back to tiling")
    except Exception as exc:
        print(f"  {tag}: single export failed ({exc}); tiling 2x2")

    # 2) Tile into a 2x2 grid and mosaic with gdalbuildvrt.
    tiles = []
    mx = (xmin + xmax) / 2.0
    my = (ymin + ymax) / 2.0
    sub_w = EXPORT_W // 2 + 2
    sub_h = EXPORT_H // 2 + 2
    cells = [
        ("ll", xmin, ymin, mx, my),
        ("lr", mx, ymin, xmax, my),
        ("ul", xmin, my, mx, ymax),
        ("ur", mx, my, xmax, ymax),
    ]
    for name, bx0, by0, bx1, by1 in cells:
        tpath = out_raw.with_name(f"{tag}_3035_{name}.tif")
        p = dict(common)
        p["bbox"] = f"{bx0},{by0},{bx1},{by1}"
        p["size"] = f"{sub_w},{sub_h}"
        _download(service, tpath, params=p)
        tiles.append(tpath)

    vrt = out_raw.with_name(f"{tag}_3035.vrt")
    _run([GDALBUILDVRT, "-overwrite", str(vrt)] + [str(t) for t in tiles])
    return vrt


def fetch_dlt(force=False) -> Path:
    out = RASTER_DIR / "dlt_utm34_30m.tif"
    if out.exists() and not force:
        print(f"[DLT] cached {out}")
        return out
    print("[DLT] Dominant Leaf Type 2018")
    src = _export_discomap(DLT_SERVICE, RAW_DIR / "dlt.tif", "dlt")
    _warp_to_grid(src, out, resample="near", src_srs="EPSG:3035")
    return out


def fetch_tcd(force=False) -> Path:
    out = RASTER_DIR / "tcd_utm34_30m.tif"
    if out.exists() and not force:
        print(f"[TCD] cached {out}")
        return out
    print("[TCD] Tree Cover Density 2018")
    src = _export_discomap(TCD_SERVICE, RAW_DIR / "tcd.tif", "tcd")
    # TCD is a continuous percent surface -> bilinear gives smoother thinning.
    _warp_to_grid(src, out, resample="bilinear", src_srs="EPSG:3035")
    return out


# -----------------------------------------------------------------------------
# ESA WorldCover
# -----------------------------------------------------------------------------

def fetch_worldcover(force=False) -> Path:
    out = RASTER_DIR / "worldcover_utm34_30m.tif"
    if out.exists() and not force:
        print(f"[WorldCover] cached {out}")
        return out
    print("[WorldCover] ESA WorldCover 2021 v200")
    tile_paths = []
    for tile in WORLDCOVER_TILES:
        tpath = RAW_DIR / f"worldcover_{tile}.tif"
        if tpath.exists() and _is_geotiff(tpath) and not force:
            print(f"  cached tile {tpath.name}")
        else:
            _download(WORLDCOVER_URL.format(tile=tile), tpath)
        tile_paths.append(tpath)

    vrt = RAW_DIR / "worldcover.vrt"
    _run([GDALBUILDVRT, "-overwrite", str(vrt)] + [str(t) for t in tile_paths])
    # WorldCover is EPSG:4326; let gdalwarp read CRS from the file.
    _warp_to_grid(vrt, out, resample="near")
    return out


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="refetch/rewarp even if cached outputs exist")
    args = parser.parse_args()

    RASTER_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if not Path(GDALWARP).exists():
        sys.exit(f"gdalwarp not found at {GDALWARP}")

    results = {}
    results["dlt"] = fetch_dlt(force=args.force)
    results["tcd"] = fetch_tcd(force=args.force)
    results["worldcover"] = fetch_worldcover(force=args.force)

    print("\n=== Forest raster summary ===")
    for name, path in results.items():
        info = subprocess.run([GDALINFO, "-stats", str(path)],
                              capture_output=True, text=True).stdout
        size_line = next((l for l in info.splitlines() if l.startswith("Size is")), "")
        minmax = next((l.strip() for l in info.splitlines()
                       if "Minimum=" in l), "")
        print(f"{name:11s} {path}")
        print(f"            {size_line}  {minmax}")


if __name__ == "__main__":
    main()
