#!/usr/bin/env python3
"""
Bake water into Condor 2 MacedoniaSkopje per-patch DDS textures as a DXT3 alpha
channel.

Mechanism (verified against Slovenia2 water tiles):
  - Water is encoded in the DDS ALPHA channel of a DXT3 (bc2) texture.
  - alpha == 0   -> water
  - alpha == 255 -> land
  - Textures are stored NORTH-UP (top = north, left = west). Do NOT transpose.

Pipeline per affected patch:
  1. Decompress the INSTALLED t{CCRR}.dds -> RGB (PIL reads DXT1/DXT3).
     This works in-place from what is already installed, so we do not need to
     re-warp from the incomplete (76.5%) ortho.
  2. Rasterize a 2048x2048 NORTH-UP alpha mask (init 255 = land; water -> 0):
        - lake / reservoir / river-bank polygons from water.geojson
        - waterway LineStrings from waterways.geojson (river / canal / stream)
     Rendered at 4x (8192) then box-downsampled to 2048 for smooth, antialiased,
     multiple-of-17 alpha edges (matches Slovenia2).
  3. Optionally tint the water RGB toward Slovenia2 water blue so it blends even
     before the water shader kicks in.
  4. Recompress with nvcompress -bc2 -> DXT3 (FourCC 'DXT3', ~5,592,560 bytes,
     12 mips) and copy into the Textures folder.

Patch naming: t{CC}{RR} where CC=column (00=east), RR=row (00=south).
Grid comes from condor_grid (XDIM=30.0) so registration matches the mesh.

Backs up every overwritten DDS to Textures_bak_phase1/ first.

Run AFTER fix_textures.py (refill + color), so any overlap patch (t0605, t0705)
already has corrected RGB before its alpha is baked.
"""
import sys
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pyproj
from shapely.geometry import shape, box
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from condor_grid import (
    LANDSCAPE_NAME,
    patch_bounds_utm, PATCHES_X, PATCHES_Y, UTM_CRS, WGS84_CRS, PATCH_SIZE_M,
)
from forest_utils import load_forest_raster, patch_raster

ROOT = Path(__file__).resolve().parent.parent
# Landscape-scoped inputs/outputs (NM reads osm_nm + bakes the NorthMacedonia
# install).  The water "masks" the texture agent needs are the OSM water.geojson
# + waterways.geojson; this script rasterises them per patch to a DXT3 alpha and
# bakes them in place.  Keep Skopje on its original paths.
_NM = LANDSCAPE_NAME == "NorthMacedonia"
OSM = ROOT / ".sandbox" / ("osm_nm" if _NM else "osm")
WORK = ROOT / ".sandbox" / ("water_bake_nm" if _NM else "water_bake")
VALID = ROOT / "validation" / ("textures_nm" if _NM else "textures")
CONDOR_TEX = Path(f"C:/Condor2/Landscapes/{LANDSCAPE_NAME}/Textures")
BACKUP = CONDOR_TEX.parent / "Textures_bak_phase1"
NVCOMPRESS = "C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe"

TEX = 2048
SS = 2          # supersample factor for antialias (render at TEX*SS then downsample)

# Water selection thresholds
LAKE_AREA_THRESH = 16600.0   # m^2 of polygon water inside a patch
RIVER_LEN_THRESH = 400.0     # m of non-intermittent river inside a patch

# Slovenia2 water blue (R,G,B) to tint water pixels toward (optional cosmetic).
WATER_BLUE = np.array([21, 85, 112], dtype=np.float32)
WATER_TINT = 0.55            # blend water RGB toward WATER_BLUE so it reads as
                             # water (without it, water is transparent over grass)

# Real water-extent rasters (the forest-style fix for over-wide OSM rivers):
# rivers get their TRUE width from JRC Global Surface Water; OSM polygons are
# kept only for lakes/reservoirs/ponds.  ESA WorldCover (already fetched for the
# forest pass) carries a permanent-water class, used as a fallback when the JRC
# raster is absent.
WATER_RASTER = ROOT / ".sandbox" / ("water_rasters_nm" if _NM else "water_rasters") / "occurrence_utm34_30m.tif"
WC_RASTER = ROOT / ".sandbox" / ("forest_rasters_nm" if _NM else "forest_rasters") / "worldcover_utm34_30m.tif"
JRC_OCC_MIN = 40             # JRC GSW occurrence % counted as permanent water
WC_WATER = 80                # ESA WorldCover permanent-water class

# Skopska Crna Gora tiles whose RGB was colour-corrected by fix_textures (their
# phase-1 backup predates that fix), so re-bake from the installed copy.
# Skopje-only: tiles whose RGB was colour-corrected after the phase-1 backup.
# Empty for NM (those tile names do not exist in the NM grid).
_COLORFIX_TILES = set() if _NM else {"t0605", "t0606", "t0607", "t0705", "t0706", "t0707"}

_OCC = _OCC_AFF = _WC = _WC_AFF = None


def load_water_rasters():
    """Load JRC occurrence + WorldCover into module globals (north-up, geographic)."""
    global _OCC, _OCC_AFF, _WC, _WC_AFF
    if WATER_RASTER.exists():
        _OCC, _OCC_AFF = load_forest_raster(WATER_RASTER)
    if WC_RASTER.exists():
        _WC, _WC_AFF = load_forest_raster(WC_RASTER)
    print(f"  water rasters: JRC occ={'ok' if _OCC is not None else 'MISSING'}, "
          f"WorldCover={'ok' if _WC is not None else 'MISSING'}")


# ---------------------------------------------------------------------------
# Geometry loading (reads geojson directly; does NOT touch osm_io.py)
# ---------------------------------------------------------------------------
_transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)


def _to_utm(geom):
    return shp_transform(lambda x, y, z=None: _transformer.transform(x, y), geom)


def load_water_polygons():
    data = json.load(open(OSM / "water.geojson", encoding="utf-8"))
    polys = []
    for f in data.get("features", []):
        g = f.get("geometry")
        if not g:
            continue
        # Drop wide riverbank polygons (water=river): they sit offset from the
        # ortho river and over-thicken it. Rivers come from the centreline buffers
        # below; keep lakes/reservoirs/ponds (the real smooth lake shapes).
        if (f.get("properties") or {}).get("water") == "river":
            continue
        try:
            geom = _to_utm(shape(g))
        except Exception:
            continue
        if geom.is_valid and not geom.is_empty and geom.area > 0:
            polys.append(geom)
    return polys


def load_waterways():
    """Return (rivers, minor). rivers items are (utm_geom, intermittent, width_m);
    width scales with the named river's total length so the Vardar (Вардар, the
    longest) gets its full width and small tributaries stay thin. Smooth buffers,
    no raster."""
    from collections import defaultdict
    data = json.load(open(OSM / "waterways.geojson", encoding="utf-8"))
    feats = []
    name_len = defaultdict(float)
    for f in data.get("features", []):
        g = f.get("geometry")
        p = f.get("properties", {})
        if not g:
            continue
        try:
            geom = _to_utm(shape(g))
        except Exception:
            continue
        if geom.is_empty:
            continue
        nm = str(p.get("name", "") or "")
        ww = p.get("waterway")
        interm = str(p.get("intermittent", "")).lower() in ("yes", "true", "1")
        feats.append((geom, ww, nm, interm))
        if ww == "river":
            name_len[nm] += geom.length

    def river_width(nm):
        low = nm.lower()
        if "вардар" in low or "vardar" in low:
            return 38.0                       # the main river — full width
        L = name_len.get(nm, 0.0)
        if L >= 45000:                        # Треска / Пчиња / Lepenc tier
            return 20.0
        if L >= 15000:
            return 12.0
        return 9.0                            # small named rivers

    rivers, minor = [], []
    for geom, ww, nm, interm in feats:
        if ww == "river":
            rivers.append((geom, interm, river_width(nm)))
        else:
            minor.append((geom, interm))
    return rivers, minor


# ---------------------------------------------------------------------------
# Rasterization (NORTH-UP)
# ---------------------------------------------------------------------------
def _e_to_px(e, e_min, size):
    return (e - e_min) / PATCH_SIZE_M * size


def _n_to_py(n, n_max, size):
    # north-up: top row = n_max
    return (n_max - n) / PATCH_SIZE_M * size


def _poly_to_pixels(poly, e_min, n_max, size):
    return [(_e_to_px(x, e_min, size), _n_to_py(y, n_max, size))
            for x, y in poly.exterior.coords]


def _line_to_pixels(coords, e_min, n_max, size):
    return [(_e_to_px(x, e_min, size), _n_to_py(y, n_max, size)) for x, y in coords]


def _iter_polys(geom):
    if geom is None or geom.is_empty:
        return
    t = geom.geom_type
    if t == "Polygon":
        yield geom
    elif t in ("MultiPolygon", "GeometryCollection"):
        for part in geom.geoms:
            yield from _iter_polys(part)


def _iter_lines(geom):
    if geom is None or geom.is_empty:
        return
    t = geom.geom_type
    if t == "LineString":
        yield geom
    elif t in ("MultiLineString", "GeometryCollection"):
        for part in geom.geoms:
            yield from _iter_lines(part)


def build_alpha_mask(col, row, poly_tree, polys, rivers, minor):
    """Return a (2048,2048) uint8 alpha mask: 255=land, 0=water (north-up)."""
    e_min, n_min, e_max, n_max = patch_bounds_utm(col, row)
    b = box(e_min, n_min, e_max, n_max)
    size = TEX * SS

    # Render WATER as white (255) on black bg, then invert -> alpha.
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)

    # Polygons (lakes, reservoirs, river banks) with holes handled.
    for idx in poly_tree.query(b):
        geom = polys[idx]
        if not geom.intersects(b):
            continue
        try:
            clipped = geom.intersection(b)
        except Exception:
            continue
        for poly in _iter_polys(clipped):
            ext = _poly_to_pixels(poly, e_min, n_max, size)
            if len(ext) >= 3:
                draw.polygon(ext, fill=255)
            for interior in poly.interiors:
                pts = _line_to_pixels(list(interior.coords), e_min, n_max, size)
                if len(pts) >= 3:
                    draw.polygon(pts, fill=0)

    # OSM river/stream LINES drawn as SMOOTH buffers at realistic per-river width
    # (Vardar full width; tributaries thin). Smooth curves, no 30 m raster, so the
    # shorelines antialias cleanly instead of pixelating.
    minor_w = max(int(round(5.0 / PATCH_SIZE_M * size)), 2)
    for geom, interm, width_m in rivers:
        if not geom.intersects(b):
            continue
        try:
            clipped = geom.intersection(b)
        except Exception:
            continue
        rw_px = max(int(round(width_m / PATCH_SIZE_M * size)), 2 * SS)
        for line in _iter_lines(clipped):
            pts = _line_to_pixels(list(line.coords), e_min, n_max, size)
            if len(pts) >= 2:
                draw.line(pts, fill=255, width=rw_px, joint="curve")
    for geom, interm in minor:
        if interm:
            continue  # skip intermittent trickles
        if not geom.intersects(b):
            continue
        try:
            clipped = geom.intersection(b)
        except Exception:
            continue
        for line in _iter_lines(clipped):
            pts = _line_to_pixels(list(line.coords), e_min, n_max, size)
            if len(pts) >= 2:
                draw.line(pts, fill=255, width=minor_w, joint="curve")

    # Box-downsample (antialias) 4096 -> 2048: average over SSxSS blocks. OSM
    # polygons + smooth line buffers give clean antialiased shorelines (the JRC
    # 30 m raster was removed -- it pixelated the Vardar into jagged stair-steps).
    water = np.asarray(img, dtype=np.float32).reshape(TEX, SS, TEX, SS).mean(axis=(1, 3))

    # water in [0,255] where 255=full water. alpha = 255 - water (0=water, 255=land).
    alpha = np.clip(255.0 - water, 0, 255).astype(np.uint8)
    return alpha


# ---------------------------------------------------------------------------
# Bake one patch
# ---------------------------------------------------------------------------
def backup_dds(name):
    src = CONDOR_TEX / f"{name}.dds"
    dst = BACKUP / f"{name}.dds"
    if src.exists() and not dst.exists():
        shutil.copy2(src, dst)


def bake_patch(col, row, poly_tree, polys, rivers, minor, save_preview=False):
    name = f"t{col:02d}{row:02d}"
    src_dds = CONDOR_TEX / f"{name}.dds"
    if not src_dds.exists():
        return name, "MISSING installed dds"

    alpha = build_alpha_mask(col, row, poly_tree, polys, rivers, minor)
    water_frac = float((alpha == 0).mean())

    # Decompress the PRE-WATER RGB (the phase-1 backup) so a re-bake with a
    # narrower river does not leave blue-tinted ghosts where the old wide water
    # was. The 6 colour-corrected tiles are read from the installed copy so their
    # fix is preserved (their backup predates the colour fix).
    orig_dds = BACKUP / f"{name}.dds"
    read_dds = src_dds if (name in _COLORFIX_TILES or not orig_dds.exists()) else orig_dds
    rgb = np.array(Image.open(read_dds).convert("RGB"), dtype=np.float32)

    # Tint water pixels toward Slovenia2 blue (cosmetic, helps non-shader views).
    wmask = (alpha < 128)
    if wmask.any():
        rgb[wmask] = rgb[wmask] * (1 - WATER_TINT) + WATER_BLUE * WATER_TINT

    rgba = np.dstack([rgb.astype(np.uint8), alpha])
    out_png = WORK / f"{name}.png"
    Image.fromarray(rgba, "RGBA").save(out_png)

    out_dds = WORK / f"{name}.dds"
    # bc2 = DXT3 (explicit alpha). -alpha keeps the alpha channel.
    cmd = [NVCOMPRESS, "-bc2", "-alpha", "-highest",
           "-mipfilter", "kaiser", "-clamp", "-silent",
           str(out_png), str(out_dds)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out_dds.exists():
        return name, f"nvcompress failed: {r.stderr[:160]}"

    backup_dds(name)
    shutil.copy2(out_dds, CONDOR_TEX / f"{name}.dds")
    sz = (CONDOR_TEX / f"{name}.dds").stat().st_size

    if save_preview:
        VALID.mkdir(parents=True, exist_ok=True)
        # alpha preview (white=land, black=water) + rgb thumbnail
        Image.fromarray(alpha).resize((512, 512)).save(VALID / f"{name}_alpha.png")
        Image.fromarray(rgba[:, :, :3]).resize((512, 512)).save(VALID / f"{name}_water_rgb.png")

    out_png.unlink(missing_ok=True)
    out_dds.unlink(missing_ok=True)
    return name, f"OK DXT3 {sz} bytes water={water_frac*100:.2f}%"


def compute_water_patches(poly_tree, polys, rivers):
    """Return sorted list of (col,row) patches with significant water."""
    out = []
    for r in range(PATCHES_Y):
        for c in range(PATCHES_X):
            e_min, n_min, e_max, n_max = patch_bounds_utm(c, r)
            b = box(e_min, n_min, e_max, n_max)
            parea = 0.0
            for idx in poly_tree.query(b):
                try:
                    parea += polys[idx].intersection(b).area
                except Exception:
                    pass
            rlen = 0.0
            for geom, interm, _w in rivers:
                if interm:
                    continue
                if geom.intersects(b):
                    try:
                        rlen += geom.intersection(b).length
                    except Exception:
                        pass
            if parea > LAKE_AREA_THRESH or rlen > RIVER_LEN_THRESH:
                out.append((c, r))
    return out


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    BACKUP.mkdir(parents=True, exist_ok=True)
    VALID.mkdir(parents=True, exist_ok=True)

    print("Loading water geometry...")
    polys = load_water_polygons()
    rivers, minor = load_waterways()
    poly_tree = STRtree(polys)
    print(f"  polygons={len(polys)} rivers={len(rivers)} minor={len(minor)}")

    patches = compute_water_patches(poly_tree, polys, rivers)
    names = [f"t{c:02d}{r:02d}" for c, r in patches]
    print(f"Significant-water patches: {len(patches)}")
    print("  " + " ".join(names))

    # Save a few previews (first patch + must-do samples).
    preview_set = {"t0300", "t0901", "t0009", "t0805", "t1005"}

    results = {}
    for i, (c, r) in enumerate(patches, 1):
        name = f"t{c:02d}{r:02d}"
        nm, status = bake_patch(c, r, poly_tree, polys, rivers, minor,
                                save_preview=(name in preview_set))
        results[nm] = status
        print(f"  [{i}/{len(patches)}] {nm}: {status}", flush=True)

    ok = sum(1 for v in results.values() if v.startswith("OK"))
    print(f"\nWATER BAKE DONE: {ok}/{len(patches)} OK")
    bad = {k: v for k, v in results.items() if not v.startswith("OK")}
    if bad:
        print("Failures:")
        for k, v in bad.items():
            print(f"  {k}: {v}")
    # Emit machine-readable summary.
    (ROOT / "validation" / "water_bake_results.json").write_text(
        json.dumps({"patches": names, "results": results}, indent=2))


if __name__ == "__main__":
    main()
