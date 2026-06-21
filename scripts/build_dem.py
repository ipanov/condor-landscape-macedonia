#!/usr/bin/env python3
"""
Build the canonical exactly-30 m UTM-34N DEM raw for a Condor landscape from
Copernicus GLO-30 COG tiles (AWS open-data bucket).

Grid-driven: reads ULXMAP/ULYMAP/WIDTH/HEIGHT/XDIM and LANDSCAPE_NAME from
`condor_grid` (set CONDOR_LANDSCAPE=nm for full North Macedonia, default = the
12x12 Skopje pilot). The output exactly reproduces the convention of the existing
`macedonia_skopje_dem_30m_2305.raw`:

  * EPSG:32634 (UTM 34N), pixel = EXACTLY 30 m (so mesh and texture grids share
    one raster — a 29.987 m grid drifts ~30 m at the SE corner).
  * WIDTH x HEIGHT samples = (patches*192 + 1) per side (shared patch-boundary
    vertices). Skopje 2305x2305 ; North Macedonia 7681x6145.
  * `-te` is aligned to the CELL EDGES (NW pixel CENTRE = ULXMAP/ULYMAP, so the
    west/north edge sits half a pixel outside), `-ts WIDTH HEIGHT`.
  * Signed int16 little-endian, headerless, GDAL TOP-LEFT row order
    (row 0 = NORTH, last row = SOUTH). NoData clamped to 0 at consume time by
    generate_trn / generate_tr3; here NoData is written as -32768.

Steps
  1. Ensure every GLO-30 1-degree tile covering the WGS84 footprint of the grid
     is present in sources/dem/ (download the missing ones, in parallel).
  2. Build a VRT mosaic of those tiles.
  3. gdalwarp -> EPSG:32634, 30 m, bilinear, exact -te/-ts -> int16 raw (+ .hdr).

Network/tooling: AWS bucket https://copernicus-dem-30m.s3.amazonaws.com (public,
no auth); GDAL from QGIS. Deterministic: same grid -> byte-identical raw.

Usage:
  CONDOR_LANDSCAPE=nm python scripts/build_dem.py
  python scripts/build_dem.py                 # skopje (rebuilds the 2305 raw)
"""

import os
import sys
import math
import struct
import subprocess
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))
import condor_grid as g  # noqa: E402

import numpy as np  # noqa: E402
import pyproj  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEM_DIR = ROOT / "sources" / "dem"

# GDAL from QGIS install (same as build_patch_textures.py).
QGIS_BIN = Path("C:/Program Files/QGIS 4.0.0/bin")
GDALWARP = str(QGIS_BIN / "gdalwarp.exe")
GDALBUILDVRT = str(QGIS_BIN / "gdalbuildvrt.exe")
GDAL_TRANSLATE = str(QGIS_BIN / "gdal_translate.exe")

AWS_BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"

# Output paths derive from the landscape size. NW pixel CENTRE = (ULXMAP, ULYMAP).
OUT_RAW = DEM_DIR / f"{'northmacedonia' if g.LANDSCAPE_NAME == 'NorthMacedonia' else 'macedonia_skopje'}_dem_30m_{g.WIDTH}x{g.HEIGHT}.raw"
OUT_HDR = OUT_RAW.with_suffix(".hdr")
OUT_VRT = DEM_DIR / f"glo30_mosaic_{g.LANDSCAPE_NAME}.vrt"


def tile_name(lat: int, lon: int) -> str:
    """GLO-30 COG basename for the 1-degree tile whose SW corner is (lat, lon)."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def required_tiles() -> list[tuple[int, int]]:
    """(lat, lon) integer SW corners of every GLO-30 tile covering the grid's
    WGS84 footprint. Samples densely along the UTM cell-edge boundary because the
    UTM->WGS84 mapping is non-linear (the lon span is widest mid-latitude)."""
    half = g.XDIM / 2.0
    w = g.ULXMAP - half
    n = g.ULYMAP + half
    e = g.BR_EASTING + half
    s = g.BR_NORTHING - half
    t = pyproj.Transformer.from_crs(32634, 4326, always_xy=True)
    lons: list[float] = []
    lats: list[float] = []
    for ee in np.linspace(w, e, 64):
        for nn in (n, s):
            lo, la = t.transform(ee, nn)
            lons.append(lo)
            lats.append(la)
    for nn in np.linspace(s, n, 64):
        for ee in (w, e):
            lo, la = t.transform(ee, nn)
            lons.append(lo)
            lats.append(la)
    lon0, lon1 = math.floor(min(lons)), math.floor(max(lons))
    lat0, lat1 = math.floor(min(lats)), math.floor(max(lats))
    tiles = [(lat, lon)
             for lat in range(lat0, lat1 + 1)
             for lon in range(lon0, lon1 + 1)]
    return tiles


def download_tile(lat: int, lon: int) -> tuple[str, str]:
    base = tile_name(lat, lon)
    dst = DEM_DIR / f"{base}.tif"
    if dst.exists() and dst.stat().st_size > 1_000_000:
        return base, "cached"
    url = f"{AWS_BUCKET}/{base}/{base}.tif"
    tmp = dst.with_suffix(".tif.part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "condor-dem/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        tmp.replace(dst)
        return base, f"downloaded {dst.stat().st_size} B"
    except urllib.error.HTTPError as ex:
        if tmp.exists():
            tmp.unlink()
        # Ocean-only tiles legitimately don't exist in GLO-30; tolerate 404 so the
        # land tiles still mosaic (the warp fills absent area with NoData).
        if ex.code == 404:
            return base, "MISSING-404 (ocean/no-tile, skipped)"
        return base, f"HTTP {ex.code}"
    except Exception as ex:  # noqa: BLE001
        if tmp.exists():
            tmp.unlink()
        return base, f"ERROR {ex}"


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + "\n" + r.stderr + "\n")
        raise SystemExit(f"command failed ({r.returncode}): {cmd[0]}")
    if r.stdout.strip():
        print(r.stdout.strip())


def write_envi_hdr(samples: int, lines: int) -> None:
    """ENVI .hdr matching the existing skopje DEM header (map info, int16, bsq)."""
    txt = f"""ENVI
description = {{
{OUT_RAW.stem} - canonical exactly-30m DEM for {g.LANDSCAPE_NAME}.
Copernicus GLO-30 mosaic warped to EPSG:32634 (UTM 34N).
UL pixel CENTER = E {g.ULXMAP:.0f}, N {g.ULYMAP:.0f}. Pixel = EXACTLY 30 m.
{samples} x {lines} = 192*{g.PATCHES_X}+1 by 192*{g.PATCHES_Y}+1 (shared patch boundary vertices).
Signed int16 little-endian, headerless raw, GDAL top-left row order (row 0 = north).}}
samples = {samples}
lines   = {lines}
bands   = 1
header offset = 0
file type = ENVI Standard
data type = 2
interleave = bsq
byte order = 0
map info = {{UTM, 1, 1, {g.ULXMAP:.0f}, {g.ULYMAP:.0f}, 30, 30, 34, North,WGS-84}}
coordinate system string = {{PROJCS["WGS_1984_UTM_Zone_34N",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",21.0],PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]}}
band names = {{Band 1}}
data ignore value = -32768
"""
    OUT_HDR.write_text(txt)


def main() -> None:
    DEM_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Landscape: {g.LANDSCAPE_NAME}  grid {g.WIDTH}x{g.HEIGHT} @ {g.XDIM} m")
    print(f"  NW pixel-centre E={g.ULXMAP:.0f} N={g.ULYMAP:.0f}")
    print(f"  SE pixel-centre E={g.BR_EASTING:.0f} N={g.BR_NORTHING:.0f}")

    tiles = required_tiles()
    print(f"GLO-30 tiles required ({len(tiles)}): "
          + ", ".join(tile_name(la, lo).split('COG_10_')[1].replace('_DEM', '')
                      for la, lo in tiles))

    # 1. Download missing tiles in parallel.
    present: list[Path] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(download_tile, la, lo): (la, lo) for la, lo in tiles}
        for fut in as_completed(futs):
            base, status = fut.result()
            print(f"  {base}: {status}")
    for la, lo in tiles:
        p = DEM_DIR / f"{tile_name(la, lo)}.tif"
        if p.exists() and p.stat().st_size > 1_000_000:
            present.append(p)
    if not present:
        raise SystemExit("No GLO-30 tiles available; aborting.")
    print(f"Mosaicking {len(present)} tiles.")

    # 2. VRT mosaic (sorted for determinism).
    present = sorted(present)
    run([GDALBUILDVRT, "-overwrite", str(OUT_VRT), *map(str, present)])

    # 3. Warp -> EPSG:32634, exactly 30 m, exact extent, bilinear -> int16 raw.
    # -te uses the CELL EDGES (NW pixel CENTRE +/- half a pixel).
    half = g.XDIM / 2.0
    te_xmin = g.ULXMAP - half
    te_ymax = g.ULYMAP + half
    te_xmax = g.BR_EASTING + half
    te_ymin = g.BR_NORTHING - half
    out_tif = OUT_RAW.with_suffix(".tif")
    run([
        GDALWARP, "-overwrite",
        "-t_srs", "EPSG:32634",
        "-te", f"{te_xmin:.3f}", f"{te_ymin:.3f}", f"{te_xmax:.3f}", f"{te_ymax:.3f}",
        "-ts", str(g.WIDTH), str(g.HEIGHT),
        "-r", "bilinear",
        "-ot", "Int16",
        "-dstnodata", "-32768",
        "-of", "GTiff",
        "-co", "TILED=YES",
        str(OUT_VRT), str(out_tif),
    ])

    # GeoTIFF -> headerless ENVI raw (top-left order, int16 LE).
    run([
        GDAL_TRANSLATE,
        "-of", "ENVI",
        "-ot", "Int16",
        str(out_tif), str(OUT_RAW),
    ])
    # gdal_translate ENVI writes its own .hdr; overwrite with our documented one.
    write_envi_hdr(g.WIDTH, g.HEIGHT)

    # Validate dimensions + report.
    arr = np.fromfile(OUT_RAW, dtype="<i2")
    exp = g.WIDTH * g.HEIGHT
    if arr.size != exp:
        raise SystemExit(f"raw size {arr.size} != expected {exp}")
    arr = arr.reshape(g.HEIGHT, g.WIDTH)
    valid = arr[arr > -32768]
    print("\n=== DEM RAW WRITTEN ===")
    print(f"  path : {OUT_RAW}")
    print(f"  dims : {g.WIDTH} x {g.HEIGHT}  ({OUT_RAW.stat().st_size} bytes, int16 LE)")
    print(f"  order: GDAL top-left (row 0 = NORTH @ N={g.ULYMAP:.0f})")
    print(f"  elev : min={int(valid.min())} max={int(valid.max())} mean={valid.mean():.1f} m")
    print(f"  row0 (north) mean={arr[0][arr[0] > -32768].mean():.1f} ; "
          f"rowN (south) mean={arr[-1][arr[-1] > -32768].mean():.1f}")
    print(f"  hdr  : {OUT_HDR}")


if __name__ == "__main__":
    main()
