#!/usr/bin/env python3
"""Stitch Macedonian 2023 orthophoto tiles, reproject to UTM34, and build Condor DDS textures.

Target: 8192 x 8192 pixel DDS DXT1 textures for each of the 9 Condor tiles (3x3).
Source: zoom 11 tiles (0.28 m/px) in EPSG:6316, no embedded georef.
GPU-accelerated DDS compression via NVIDIA Texture Tools nvcompress.

Tile naming: Condor uses CCRR format with SE origin (col 0 = east, row 0 = south).
  col 0 = easternmost third, col 2 = westernmost third
  row 0 = southernmost third, row 2 = northernmost third
"""
import os
import sys
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, **kw):
        return it

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / ".sandbox" / "textures_mk2023_z11"
WORK_DIR = ROOT / ".sandbox" / "ortho_utm_work"
OUT_DIR = ROOT / "output" / "textures"
WORK_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Landscape UTM34 bounds (from DEM header)
UTM_E_MIN = 506880.0
UTM_E_MAX = 575970.011
UTM_N_MIN = 4631069.989
UTM_N_MAX = 4700160.0
UTM_WIDTH = UTM_E_MAX - UTM_E_MIN
UTM_HEIGHT = UTM_N_MAX - UTM_N_MIN

# Condor texture tile grid: 3 cols x 3 rows
TILES_X = 3
TILES_Y = 3
TILE_UTM_WIDTH = UTM_WIDTH / TILES_X
TILE_UTM_HEIGHT = UTM_HEIGHT / TILES_Y

# Target texture size per Condor tile
TEX_SIZE = 8192

NVCOMPRESS = Path("C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe")

QGIS_BIN = Path("C:/Program Files/QGIS 4.0.0/bin")


def gdal_tool(name):
    p = QGIS_BIN / f"{name}.exe"
    if p.exists():
        return str(p)
    return name


def load_metadata():
    return json.loads((SRC_DIR / "metadata.json").read_text())


def parse_tile_coords(filepath):
    """Extract (tx, ty) from a tile filename like z11_x1516_y1535.jpg."""
    m = re.search(r'z11_x(\d+)_y(\d+)\.jpg$', str(filepath))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def discover_source_tiles():
    """Find all tiles, return list of (filepath, tx, ty)."""
    tiles = []
    count = 0

    # Walk the entire source directory tree for z11_*.jpg files
    for dirpath, dirnames, filenames in os.walk(str(SRC_DIR)):
        for fn in filenames:
            if fn.startswith('z11_') and fn.endswith('.jpg'):
                fp = Path(dirpath) / fn
                coords = parse_tile_coords(fn)
                if coords:
                    tiles.append((fp, coords[0], coords[1]))
                    count += 1
                    if count % 100000 == 0:
                        print(f"  ... discovered {count} tiles so far", flush=True)

    print(f"  Total tiles discovered: {len(tiles)}", flush=True)
    return tiles


def build_vrt(tiles, meta):
    """Build a GDAL VRT XML file that georeferences all source JPEG tiles.

    Each tile (tx, ty) has its origin at:
      X = ORIGIN_X + tx * TILE_SIZE_M
      Y = ORIGIN_Y + ty * TILE_SIZE_M
    The pixel size is TILE_SIZE_M / 256 = resolution_m.
    Y axis goes downward in raster space but the CRS Y increases upward,
    so we use -resolution_m for the Y pixel size.
    """
    vrt_path = WORK_DIR / "ortho_6316.vrt"

    origin_x = meta['origin_x']
    origin_y = meta['origin_y']
    tile_size_m = meta['tile_size_m']
    tile_size_px = meta['tile_size_px']  # 256
    res = meta['resolution_m']  # 0.28

    # Compute global raster extent
    all_tx = [t[1] for t in tiles]
    all_ty = [t[2] for t in tiles]
    tx_min, tx_max = min(all_tx), max(all_tx)
    ty_min, ty_max = min(all_ty), max(all_ty)

    # Global pixel dimensions
    nx = tx_max - tx_min + 1
    ny = ty_max - ty_min + 1
    raster_x_size = nx * tile_size_px
    raster_y_size = ny * tile_size_px

    # Global geotransform origin (top-left corner of the mosaic)
    # X increases right, Y increases up in the CRS
    # Raster origin is top-left, so we use the max-Y tile's top edge
    global_x_origin = origin_x + tx_min * tile_size_m
    global_y_origin = origin_y + (ty_max + 1) * tile_size_m  # top edge of topmost tile

    print(f"  VRT raster size: {raster_x_size} x {raster_y_size}")
    print(f"  Tile range: X [{tx_min}, {tx_max}], Y [{ty_min}, {ty_max}]")
    print(f"  Global origin: ({global_x_origin}, {global_y_origin})")

    # Write the VRT XML
    # Use streaming write for the huge file
    print(f"  Writing VRT with {len(tiles)} tile entries...", flush=True)
    with open(str(vrt_path), 'w') as f:
        f.write(f'''<VRTDataset rasterXSize="{raster_x_size}" rasterYSize="{raster_y_size}">
  <SRS dataAxisToSRSAxisMapping="1,2">EPSG:6316</SRS>
  <GeoTransform>{global_x_origin}, {res}, 0, {global_y_origin}, 0, {-res}</GeoTransform>
''')
        # 3 bands: R, G, B
        for band_idx in range(1, 4):
            color = ['Red', 'Green', 'Blue'][band_idx - 1]
            f.write(f'''  <VRTRasterBand dataType="Byte" band="{band_idx}">
    <ColorInterp>{color}</ColorInterp>
''')
            for filepath, tx, ty in tiles:
                # Pixel offset within the global raster
                x_off = (tx - tx_min) * tile_size_px
                y_off = (ty_max - ty) * tile_size_px  # flip Y: high ty = low row
                fp_str = str(filepath).replace('&', '&amp;')
                f.write(f'''    <SimpleSource>
      <SourceFilename relativeToVRT="0">{fp_str}</SourceFilename>
      <SourceBand>{band_idx}</SourceBand>
      <SourceProperties RasterXSize="{tile_size_px}" RasterYSize="{tile_size_px}" DataType="Byte" BlockXSize="{tile_size_px}" BlockYSize="1" />
      <SrcRect xOff="0" yOff="0" xSize="{tile_size_px}" ySize="{tile_size_px}" />
      <DstRect xOff="{x_off}" yOff="{y_off}" xSize="{tile_size_px}" ySize="{tile_size_px}" />
    </SimpleSource>
''')
            f.write('  </VRTRasterBand>\n')
        f.write('</VRTDataset>\n')

    vrt_size_mb = vrt_path.stat().st_size / 1024 / 1024
    print(f"  VRT written: {vrt_path} ({vrt_size_mb:.1f} MB)")
    return vrt_path


def condor_tile_bounds(col, row):
    """Get UTM34 bounds for a Condor tile (col, row) where (0,0) = SE corner.

    col 0 = easternmost third (highest easting)
    row 0 = southernmost third (lowest northing)
    """
    # col 0 = east side, so easting starts from the right
    e_min = UTM_E_MAX - (col + 1) * TILE_UTM_WIDTH
    e_max = UTM_E_MAX - col * TILE_UTM_WIDTH
    # row 0 = south side
    n_min = UTM_N_MIN + row * TILE_UTM_HEIGHT
    n_max = UTM_N_MIN + (row + 1) * TILE_UTM_HEIGHT
    return e_min, n_min, e_max, n_max


def condor_tile_name(col, row):
    """Condor tile name in CCRR format: t0000 to t0202."""
    return f"t{col:02d}{row:02d}"


def gdalwarp_tile(col, row, vrt_path):
    """Reproject one Condor tile from EPSG:6316 VRT to UTM34 BMP."""
    tile_name = condor_tile_name(col, row)
    e_min, n_min, e_max, n_max = condor_tile_bounds(col, row)

    bmp_path = WORK_DIR / f"{tile_name}.bmp"

    print(f"  [{tile_name}] Bounds: E[{e_min:.1f}, {e_max:.1f}] N[{n_min:.1f}, {n_max:.1f}]")

    cmd = [
        gdal_tool("gdalwarp"),
        "-s_srs", "EPSG:6316",
        "-t_srs", "EPSG:32634",
        "-te", str(e_min), str(n_min), str(e_max), str(n_max),
        "-ts", str(TEX_SIZE), str(TEX_SIZE),
        "-r", "bilinear",
        "-ot", "Byte",
        "-of", "BMP",
        "-overwrite",
        "-wo", "NUM_THREADS=ALL_CPUS",
        "-multi",
        str(vrt_path), str(bmp_path)
    ]
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    if result.returncode != 0:
        print(f"  [{tile_name}] gdalwarp FAILED ({elapsed:.1f}s):")
        print(f"    stdout: {result.stdout[:500]}")
        print(f"    stderr: {result.stderr[:500]}")
        return None

    size_mb = bmp_path.stat().st_size / 1024 / 1024 if bmp_path.exists() else 0
    print(f"  [{tile_name}] gdalwarp done ({elapsed:.1f}s, {size_mb:.1f} MB)")
    return tile_name, str(bmp_path)


def nvcompress_tile(tile_name, bmp_path):
    """GPU-compress one BMP to DDS DXT1 with full mipmaps."""
    dds_path = OUT_DIR / f"{tile_name}.dds"
    cmd = [
        str(NVCOMPRESS),
        "-bc1",
        "-highest",
        "-mipfilter", "kaiser",
        "-color",
        "-clamp",
        "-silent",
        bmp_path,
        str(dds_path)
    ]
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    if result.returncode != 0:
        print(f"  [{tile_name}] nvcompress FAILED ({elapsed:.1f}s):")
        print(f"    stdout: {result.stdout[:500]}")
        print(f"    stderr: {result.stderr[:500]}")
        return None

    size_mb = dds_path.stat().st_size / 1024 / 1024 if dds_path.exists() else 0
    print(f"  [{tile_name}] nvcompress done ({elapsed:.1f}s, {size_mb:.1f} MB)")
    return tile_name, str(dds_path)


def copy_to_condor(tile_name, dds_path):
    dest = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
    dest.mkdir(parents=True, exist_ok=True)
    dest_file = dest / f"{tile_name}.dds"
    shutil.copy2(dds_path, dest_file)
    print(f"  [{tile_name}] Copied to {dest_file}")


def main():
    t_start = time.time()
    print("=" * 60)
    print("Condor Landscape Texture Pipeline")
    print("=" * 60)

    print("\n[1/5] Loading metadata...")
    meta = load_metadata()
    print(f"  Source zoom: {meta['zoom']}, resolution: {meta['resolution_m']} m/px")
    print(f"  Tile size: {meta['tile_size_px']}px = {meta['tile_size_m']}m")

    vrt_path = WORK_DIR / "ortho_6316.vrt"
    if vrt_path.exists() and vrt_path.stat().st_size > 100_000_000:
        print(f"\n[2/5] Reusing existing VRT ({vrt_path.stat().st_size / 1024 / 1024:.1f} MB)")
        print(f"[3/5] Skipped (VRT already built)")
    else:
        print("\n[2/5] Discovering source tiles...")
        tiles = discover_source_tiles()
        if not tiles:
            print("ERROR: No tiles found!")
            sys.exit(1)

        print(f"\n[3/5] Building VRT from {len(tiles)} tiles...")
        vrt_path = build_vrt(tiles, meta)

    # Generate tile list
    tile_list = []
    for col in range(TILES_X):
        for row in range(TILES_Y):
            tile_list.append((col, row))

    print(f"\n[4/5] Reprojecting and compressing {len(tile_list)} Condor tiles...")
    print(f"  Target: {TEX_SIZE}x{TEX_SIZE} DDS DXT1")
    print(f"  UTM tile size: {TILE_UTM_WIDTH:.1f} x {TILE_UTM_HEIGHT:.1f} m")

    compressed = {}
    for i, (col, row) in enumerate(tile_list):
        tile_name = condor_tile_name(col, row)
        print(f"\n--- Tile {i+1}/{len(tile_list)}: {tile_name} (col={col}, row={row}) ---")

        # Step A: gdalwarp to BMP
        result = gdalwarp_tile(col, row, vrt_path)
        if result is None:
            print(f"  SKIPPING {tile_name} due to gdalwarp failure")
            continue
        tile_name, bmp_path = result

        # Step B: nvcompress to DDS
        result = nvcompress_tile(tile_name, bmp_path)
        if result is None:
            print(f"  SKIPPING {tile_name} due to nvcompress failure")
            continue
        tile_name, dds_path = result
        compressed[tile_name] = dds_path

        # Clean up BMP to save disk space
        bmp_file = Path(bmp_path)
        if bmp_file.exists():
            bmp_file.unlink()
            print(f"  [{tile_name}] Cleaned up BMP")

    print(f"\n[5/5] Copying {len(compressed)} DDS files to Condor landscape...")
    for tile_name, dds_path in compressed.items():
        copy_to_condor(tile_name, dds_path)

    elapsed = time.time() - t_start
    print(f"\n{'=' * 60}")
    print(f"Pipeline complete! {len(compressed)}/{len(tile_list)} tiles processed in {elapsed:.0f}s")
    print(f"Output: {OUT_DIR}")
    print(f"Condor: C:/Condor2/Landscapes/MacedoniaSkopje/Textures/")
    if len(compressed) < len(tile_list):
        print(f"WARNING: {len(tile_list) - len(compressed)} tiles failed!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
