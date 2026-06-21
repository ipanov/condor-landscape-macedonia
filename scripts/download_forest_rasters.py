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
import math
import os
import subprocess
import sys
from pathlib import Path

import pyproj
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from condor_grid import (
    LANDSCAPE_NAME,
    ULXMAP,
    ULYMAP,
    XDIM,
    PATCHES_X,
    PATCHES_Y,
    PATCH_SIZE_M,
    UTM_CRS,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Output to a landscape-specific sandbox so the Skopje rasters are never
# clobbered when the NM grid is selected (CONDOR_LANDSCAPE=nm).
_RASTER_SUBDIR = "forest_rasters_nm" if LANDSCAPE_NAME == "NorthMacedonia" else "forest_rasters"
RASTER_DIR = PROJECT_ROOT / ".sandbox" / _RASTER_SUBDIR
RAW_DIR = RASTER_DIR / "raw"

GDAL_BIN = Path("C:/Program Files/QGIS 4.0.0/bin")
GDALWARP = str(GDAL_BIN / "gdalwarp.exe")
GDAL_TRANSLATE = str(GDAL_BIN / "gdal_translate.exe")
GDALBUILDVRT = str(GDAL_BIN / "gdalbuildvrt.exe")
GDALINFO = str(GDAL_BIN / "gdalinfo.exe")

# ---------------------------------------------------------------------------
# Landscape grid, fully derived from condor_grid (UTM 34N, EPSG:32634, exact
# 30 m).  Expansion to the full NM grid is a pure reparameterisation -- the
# target extent / EPSG:3035 export bbox / WorldCover tile list all follow the
# bbox, so no constants need hand-editing.
# ---------------------------------------------------------------------------
TARGET_SRS = "EPSG:32634"
# UTM target extent (pixel-EDGE aligned to the patch grid, matching the mesh).
# The Skopje pilot historically used the NW pixel-centre as xmin (506880) with a
# 69120 m span; the patch-metres span (PATCHES * PATCH_SIZE_M) is identical, so
# this stays byte-compatible for Skopje while scaling for NM.
_E0 = ULXMAP
_E1 = ULXMAP + PATCHES_X * PATCH_SIZE_M
_N1 = ULYMAP
_N0 = ULYMAP - PATCHES_Y * PATCH_SIZE_M
TE = [f"{_E0:.0f}", f"{_N0:.0f}", f"{_E1:.0f}", f"{_N1:.0f}"]  # xmin ymin xmax ymax
TR = [f"{XDIM:.0f}", f"{XDIM:.0f}"]

# EEA DiscoMap ImageServer endpoints (native EPSG:3035).
DISCOMAP = "https://image.discomap.eea.europa.eu/arcgis/rest/services"
DLT_SERVICE = f"{DISCOMAP}/GioLandPublic/HRL_DominantLeafType2018/ImageServer/exportImage"
TCD_SERVICE = f"{DISCOMAP}/GioLandPublic/HRL_TreeCoverDensity_2018/ImageServer/exportImage"


def _bbox_3035():
    """EPSG:3035 (LAEA) bounding box of the landscape grid, padded 1 px.

    The DiscoMap ImageServers are native EPSG:3035; exporting in the native CRS
    keeps the HRL pixels crisp before the final warp to UTM 34N.
    """
    t = pyproj.Transformer.from_crs(UTM_CRS, pyproj.CRS.from_epsg(3035), always_xy=True)
    xs, ys = [], []
    for e in (_E0, _E1):
        for n in (_N0, _N1):
            x, y = t.transform(e, n)
            xs.append(x)
            ys.append(y)
    pad = 60.0  # ~2 px LAEA padding for full edge coverage
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


BBOX_3035 = _bbox_3035()  # xmin ymin xmax ymax
# Native HRL pixels are 100 m in 3035 but we request at ~30 m so the warp has
# enough source resolution; round up for safety.
EXPORT_W = int(math.ceil((BBOX_3035[2] - BBOX_3035[0]) / 30.0)) + 4
EXPORT_H = int(math.ceil((BBOX_3035[3] - BBOX_3035[1]) / 30.0)) + 4
# DiscoMap exportImage caps a single request at 4100 px/side, so tile when large.
EXPORT_MAX = 4000


def _worldcover_tiles():
    """ESA WorldCover v200 3x3-degree tile names covering the landscape bbox.

    Tiles are named S/N{lat}E/W{lon} on a 3-degree grid (floor to multiple of 3).
    """
    t = pyproj.Transformer.from_crs(UTM_CRS, pyproj.CRS.from_epsg(4326), always_xy=True)
    lons, lats = [], []
    for e in (_E0, _E1):
        for n in (_N0, _N1):
            lo, la = t.transform(e, n)
            lons.append(lo)
            lats.append(la)
    lat_lo = int(math.floor(min(lats) / 3.0) * 3)
    lat_hi = int(math.floor(max(lats) / 3.0) * 3)
    lon_lo = int(math.floor(min(lons) / 3.0) * 3)
    lon_hi = int(math.floor(max(lons) / 3.0) * 3)
    tiles = []
    for la in range(lat_lo, lat_hi + 1, 3):
        for lo in range(lon_lo, lon_hi + 1, 3):
            ns = "N" if la >= 0 else "S"
            ew = "E" if lo >= 0 else "W"
            tiles.append(f"{ns}{abs(la):02d}{ew}{abs(lo):03d}")
    return tiles


# ESA WorldCover 2021 v200 public S3.
WORLDCOVER_TILES = _worldcover_tiles()
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

def _export_discomap(service: str, out_raw: Path, tag: str, force=False) -> Path:
    """Export an HRL layer from DiscoMap as a tiled EPSG:3035 GeoTIFF mosaic.

    The DiscoMap exportImage endpoint caps a single request at ~4100 px/side, and
    the full North Macedonia bbox is ~8443x7205 px at 30 m, so the export is
    split into an Nx M grid of <=EXPORT_MAX-px tiles georeferenced via an ESRI
    world-file and mosaicked with gdalbuildvrt.  Each tile is cached, so a
    re-run resumes rather than re-downloading.
    """
    xmin, ymin, xmax, ymax = BBOX_3035
    common = {
        "bboxSR": "3035",
        "imageSR": "3035",
        "format": "tiff",
        "pixelType": "U8",
        "f": "image",
    }

    ncols = max(1, math.ceil(EXPORT_W / EXPORT_MAX))
    nrows = max(1, math.ceil(EXPORT_H / EXPORT_MAX))
    dx = (xmax - xmin) / ncols
    dy = (ymax - ymin) / nrows
    sub_w = math.ceil(EXPORT_W / ncols) + 2
    sub_h = math.ceil(EXPORT_H / nrows) + 2
    print(f"  {tag}: 3035 export grid {ncols}x{nrows} ({sub_w}x{sub_h}px tiles)")

    tiles = []
    for r in range(nrows):
        for c in range(ncols):
            bx0 = xmin + c * dx
            bx1 = xmin + (c + 1) * dx
            by1 = ymax - r * dy
            by0 = ymax - (r + 1) * dy
            tpath = out_raw.with_name(f"{tag}_3035_r{r}c{c}.tif")
            if tpath.exists() and _is_geotiff(tpath) and not force:
                print(f"    cached {tpath.name}")
                tiles.append(tpath)
                continue
            p = dict(common)
            p["bbox"] = f"{bx0},{by0},{bx1},{by1}"
            p["size"] = f"{sub_w},{sub_h}"
            # ArcGIS returns the TIFF without embedded georef in some configs;
            # write a sidecar world-file so gdalbuildvrt can place the tile.
            _download(service, tpath, params=p)
            if not _has_crs(tpath):
                px = (bx1 - bx0) / sub_w
                py = (by1 - by0) / sub_h
                tfw = tpath.with_suffix(".tfw")
                tfw.write_text(
                    f"{px:.10f}\n0.0\n0.0\n{-py:.10f}\n"
                    f"{bx0 + px / 2:.6f}\n{by1 - py / 2:.6f}\n"
                )
                # Stamp the CRS via a .prj so gdal reads EPSG:3035.
                _run([GDAL_TRANSLATE, "-a_srs", "EPSG:3035", "-of", "GTiff",
                      str(tpath), str(tpath.with_name(tpath.stem + "_geo.tif"))])
                tpath = tpath.with_name(tpath.stem + "_geo.tif")
            tiles.append(tpath)

    if len(tiles) == 1:
        return tiles[0]
    vrt = out_raw.with_name(f"{tag}_3035.vrt")
    _run([GDALBUILDVRT, "-overwrite", str(vrt)] + [str(t) for t in tiles])
    return vrt


def _has_crs(path: Path) -> bool:
    info = subprocess.run([GDALINFO, str(path)], capture_output=True, text=True)
    out = info.stdout
    return ("PROJCRS" in out or "Coordinate System is" in out) and "3035" in out


def fetch_dlt(force=False) -> Path:
    out = RASTER_DIR / "dlt_utm34_30m.tif"
    if out.exists() and not force:
        print(f"[DLT] cached {out}")
        return out
    print("[DLT] Dominant Leaf Type 2018")
    src = _export_discomap(DLT_SERVICE, RAW_DIR / "dlt.tif", "dlt", force=force)
    _warp_to_grid(src, out, resample="near", src_srs="EPSG:3035")
    return out


def fetch_tcd(force=False) -> Path:
    out = RASTER_DIR / "tcd_utm34_30m.tif"
    if out.exists() and not force:
        print(f"[TCD] cached {out}")
        return out
    print("[TCD] Tree Cover Density 2018")
    src = _export_discomap(TCD_SERVICE, RAW_DIR / "tcd.tif", "tcd", force=force)
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

    print(f"Landscape: {LANDSCAPE_NAME}  ({PATCHES_X}x{PATCHES_Y} patches)")
    print(f"  UTM target extent (TE): {TE}")
    print(f"  EPSG:3035 export bbox:  {tuple(round(v) for v in BBOX_3035)}  "
          f"({EXPORT_W}x{EXPORT_H}px)")
    print(f"  WorldCover tiles:       {WORLDCOVER_TILES}")
    print(f"  Output dir:             {RASTER_DIR}")

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
