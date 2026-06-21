#!/usr/bin/env python3
"""Build a single GDAL VRT over the downloaded MK zoom-8 cadastre tiles.

Each tile is a plain 256x256 JPG with no embedded georeferencing, but its EPSG:6316
geotransform is fully determined by its (tx, ty) grid index and the MSCS6316 origin
and zoom-8 resolution. Rather than litter the cache with 78k worldfiles, we emit one
VRT XML directly, computing each tile's DstRect in the mosaic. The VRT's CRS is
EPSG:6316; per-patch warps (nm_build_textures.py) reproject it to UTM-34N.

Output: .sandbox/textures_nm/mk_z8.vrt

Run:  python scripts/nm_build_mk_vrt.py
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TILE_DIR = ROOT / ".sandbox" / "textures_nm" / "mk_z8"
TILESET = ROOT / ".sandbox" / "textures_nm" / "mk_z8_tiles.json"
VRT = ROOT / ".sandbox" / "textures_nm" / "mk_z8.vrt"

ORIGIN_X = 7397000.634424793
ORIGIN_Y = 4521901.793180252
RES = 2.24                 # zoom 8 m/px
TILE_PX = 256
TILE_M = TILE_PX * RES     # 573.44 m

# Use GDAL's authoritative EPSG:6316 (MGI 1901 / Balkans zone 7), exactly as the
# verified Skopje pipeline (process_mk_ortho.py) — NOT a hand-written WKT.


def tile_path(tx, ty):
    return TILE_DIR / f"x{tx // 256}" / f"y{ty // 256}" / f"z8_x{tx}_y{ty}.jpg"


def main():
    ts = json.loads(TILESET.read_text())
    tiles = [tuple(t) for t in ts["tiles"]]
    present = [(tx, ty) for (tx, ty) in tiles if tile_path(tx, ty).exists()]
    print(f"tiles in set: {len(tiles)}  present on disk: {len(present)}")
    if not present:
        raise SystemExit("no MK tiles present; run nm_download_mk_z8.py first")

    txs = [t[0] for t in present]
    tys = [t[1] for t in present]
    tx_min, tx_max = min(txs), max(txs)
    ty_min, ty_max = min(tys), max(tys)
    # Mosaic raster size (cols increase east with tx, rows increase DOWN as ty
    # DECREASES — GWC ty increases north, image rows increase south).
    ncols = (tx_max - tx_min + 1) * TILE_PX
    nrows = (ty_max - ty_min + 1) * TILE_PX
    # Top-left (NW) of mosaic in 6316:
    ulx = ORIGIN_X + tx_min * TILE_M
    uly = ORIGIN_Y + (ty_max + 1) * TILE_M
    gt = (ulx, RES, 0.0, uly, 0.0, -RES)

    lines = []
    lines.append(f'<VRTDataset rasterXSize="{ncols}" rasterYSize="{nrows}">')
    lines.append('  <SRS dataAxisToSRSAxisMapping="1,2">EPSG:6316</SRS>')
    lines.append('  <GeoTransform>{:.10f}, {:.10f}, {:.10f}, '
                 '{:.10f}, {:.10f}, {:.10f}</GeoTransform>'.format(*gt))
    for band, color in [(1, "Red"), (2, "Green"), (3, "Blue")]:
        lines.append(f'  <VRTRasterBand dataType="Byte" band="{band}">')
        lines.append(f'    <ColorInterp>{color}</ColorInterp>')
        for (tx, ty) in present:
            xoff = (tx - tx_min) * TILE_PX
            yoff = (ty_max - ty) * TILE_PX
            rel = os.path.relpath(tile_path(tx, ty), VRT.parent).replace("\\", "/")
            lines.append(f'    <SimpleSource>')
            lines.append(f'      <SourceFilename relativeToVRT="1">{rel}</SourceFilename>')
            lines.append(f'      <SourceBand>{band}</SourceBand>')
            lines.append(f'      <SrcRect xOff="0" yOff="0" xSize="{TILE_PX}" ySize="{TILE_PX}"/>')
            lines.append(f'      <DstRect xOff="{xoff}" yOff="{yoff}" xSize="{TILE_PX}" ySize="{TILE_PX}"/>')
            lines.append(f'    </SimpleSource>')
        lines.append('  </VRTRasterBand>')
    lines.append('</VRTDataset>')

    VRT.write_text("\n".join(lines))
    print(f"wrote {VRT}  ({ncols}x{nrows}, {len(present)} sources/band)")
    print(f"  mosaic 6316 bounds: ulx={ulx:.1f} uly={uly:.1f} "
          f"lrx={ulx + ncols * RES:.1f} lry={uly - nrows * RES:.1f}")


if __name__ == "__main__":
    main()
