#!/usr/bin/env python3
"""NorthMacedonia — SECOND-PASS water bake into installed per-patch DDS (DXT3 alpha).

This is the second texture pass, run AFTER nm_build_textures.py has installed the
1280 dry DXT1 DDS. It encodes OSM water into the DDS ALPHA channel of a DXT3 (bc2)
texture (alpha 0 = water, 255 = land), exactly as the verified Skopje bake_water.py
and as Slovenia2's water tiles — textures stay NORTH-UP (no transpose).

DATA: reads the NM-extent water vectors produced by the forest/water agent at
.sandbox/osm_nm/  (water.geojson [lakes/reservoirs/river polygons] and, when
present, waterways.geojson [river/stream centrelines]). If a JRC/WorldCover water
raster for the NM grid is provided (.sandbox/water_rasters_nm/), it can be wired in
the same way as Skopje; until then this uses the OSM vectors only.

Mechanism per affected patch (in place on the install):
  1. Decompress the installed t{CCRR}.dds -> RGB (no re-warp needed).
  2. Rasterize a 2048x2048 NORTH-UP alpha (255=land, 0=water) from water polygons
     + waterway line buffers, supersampled SSx and box-downsampled for clean edges.
  3. Tint water RGB toward Slovenia2 blue (cosmetic) and recompress -bc2 -> DXT3.
  4. Back up every overwritten DDS to Textures_bak_dry/ first.

Run:  CONDOR_LANDSCAPE=nm python scripts/nm_bake_water.py
Grid + patch boxes come from condor_grid (XDIM=30.0), so water registers to mesh.
"""
import os
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
os.environ.setdefault("CONDOR_LANDSCAPE", "nm")
from condor_grid import (
    LANDSCAPE_NAME, patch_bounds_utm, PATCHES_X, PATCHES_Y,
    UTM_CRS, WGS84_CRS, PATCH_SIZE_M,
)

ROOT = Path(__file__).resolve().parent.parent
OSM = ROOT / ".sandbox" / "osm_nm"           # forest/water agent's NM-extent data
WORK = ROOT / ".sandbox" / "nm_water_bake"
CONDOR_TEX = Path(f"C:/Condor2/Landscapes/{LANDSCAPE_NAME}/Textures")
BACKUP = CONDOR_TEX.parent / "Textures_bak_dry"
NVCOMPRESS = "C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe"

TEX = 2048
SS = 2
LAKE_AREA_THRESH = 16600.0
RIVER_LEN_THRESH = 400.0
WATER_BLUE = np.array([21, 85, 112], dtype=np.float32)
WATER_TINT = 0.55

assert LANDSCAPE_NAME == "NorthMacedonia", f"wrong landscape: {LANDSCAPE_NAME}"
_tr = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)


def _to_utm(geom):
    return shp_transform(lambda x, y, z=None: _tr.transform(x, y), geom)


def load_water_polygons():
    f = OSM / "water.geojson"
    if not f.exists():
        return []
    data = json.load(open(f, encoding="utf-8"))
    polys = []
    for feat in data.get("features", []):
        g = feat.get("geometry")
        if not g:
            continue
        try:
            geom = _to_utm(shape(g))
        except Exception:
            continue
        if geom.is_valid and not geom.is_empty and geom.area > 0:
            polys.append(geom)
    return polys


def load_waterways():
    """(rivers, minor): rivers=(geom,interm,width_m), minor=(geom,interm).
    Falls back to empty if no waterways.geojson yet."""
    from collections import defaultdict
    f = OSM / "waterways.geojson"
    if not f.exists():
        return [], []
    data = json.load(open(f, encoding="utf-8"))
    feats = []
    name_len = defaultdict(float)
    for feat in data.get("features", []):
        g = feat.get("geometry")
        p = feat.get("properties", {})
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

    def width(nm):
        low = nm.lower()
        if "вардар" in low or "vardar" in low:
            return 38.0
        L = name_len.get(nm, 0.0)
        if L >= 45000:
            return 20.0
        if L >= 15000:
            return 12.0
        return 9.0

    rivers, minor = [], []
    for geom, ww, nm, interm in feats:
        (rivers.append((geom, interm, width(nm))) if ww == "river"
         else minor.append((geom, interm)))
    return rivers, minor


def _ex(e, e_min, size):
    return (e - e_min) / PATCH_SIZE_M * size


def _ny(n, n_max, size):
    return (n_max - n) / PATCH_SIZE_M * size


def _iter_polys(geom):
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        for p in geom.geoms:
            yield from _iter_polys(p)


def _iter_lines(geom):
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "LineString":
        yield geom
    elif geom.geom_type in ("MultiLineString", "GeometryCollection"):
        for p in geom.geoms:
            yield from _iter_lines(p)


def build_alpha(col, row, tree, polys, rivers, minor):
    e_min, n_min, e_max, n_max = patch_bounds_utm(col, row)
    b = box(e_min, n_min, e_max, n_max)
    size = TEX * SS
    img = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(img)
    for idx in tree.query(b):
        geom = polys[idx]
        if not geom.intersects(b):
            continue
        try:
            clipped = geom.intersection(b)
        except Exception:
            continue
        for poly in _iter_polys(clipped):
            ext = [(_ex(x, e_min, size), _ny(y, n_max, size)) for x, y in poly.exterior.coords]
            if len(ext) >= 3:
                d.polygon(ext, fill=255)
            for ring in poly.interiors:
                pts = [(_ex(x, e_min, size), _ny(y, n_max, size)) for x, y in ring.coords]
                if len(pts) >= 3:
                    d.polygon(pts, fill=0)
    minor_w = max(int(round(5.0 / PATCH_SIZE_M * size)), 2)
    for geom, interm, width_m in rivers:
        if not geom.intersects(b):
            continue
        try:
            clipped = geom.intersection(b)
        except Exception:
            continue
        rw = max(int(round(width_m / PATCH_SIZE_M * size)), 2 * SS)
        for line in _iter_lines(clipped):
            pts = [(_ex(x, e_min, size), _ny(y, n_max, size)) for x, y in line.coords]
            if len(pts) >= 2:
                d.line(pts, fill=255, width=rw, joint="curve")
    for geom, interm in minor:
        if interm or not geom.intersects(b):
            continue
        try:
            clipped = geom.intersection(b)
        except Exception:
            continue
        for line in _iter_lines(clipped):
            pts = [(_ex(x, e_min, size), _ny(y, n_max, size)) for x, y in line.coords]
            if len(pts) >= 2:
                d.line(pts, fill=255, width=minor_w, joint="curve")
    water = np.asarray(img, np.float32).reshape(TEX, SS, TEX, SS).mean(axis=(1, 3))
    return np.clip(255.0 - water, 0, 255).astype(np.uint8)


def compute_water_patches(tree, polys, rivers):
    out = []
    for r in range(PATCHES_Y):
        for c in range(PATCHES_X):
            b = box(*patch_bounds_utm(c, r))
            parea = 0.0
            for idx in tree.query(b):
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


def bake(col, row, tree, polys, rivers, minor):
    name = f"t{col:02d}{row:02d}"
    src = CONDOR_TEX / f"{name}.dds"
    if not src.exists():
        return name, "MISSING installed dds"
    alpha = build_alpha(col, row, tree, polys, rivers, minor)
    wfrac = float((alpha == 0).mean())
    rgb = np.array(Image.open(src).convert("RGB"), np.float32)
    wmask = alpha < 128
    if wmask.any():
        rgb[wmask] = rgb[wmask] * (1 - WATER_TINT) + WATER_BLUE * WATER_TINT
    rgba = np.dstack([rgb.astype(np.uint8), alpha])
    png = WORK / f"{name}.png"
    Image.fromarray(rgba, "RGBA").save(png)
    dds = WORK / f"{name}.dds"
    r = subprocess.run([NVCOMPRESS, "-bc2", "-alpha", "-highest",
                        "-mipfilter", "kaiser", "-clamp", "-silent", str(png), str(dds)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not dds.exists():
        return name, f"nvcompress fail: {r.stderr[:160]}"
    bdst = BACKUP / f"{name}.dds"
    if not bdst.exists():
        shutil.copy2(src, bdst)
    shutil.copy2(dds, src)
    sz = src.stat().st_size
    png.unlink(missing_ok=True)
    dds.unlink(missing_ok=True)
    return name, f"OK DXT3 {sz}B water={wfrac*100:.2f}%"


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    BACKUP.mkdir(parents=True, exist_ok=True)
    if not (OSM / "water.geojson").exists():
        print(f"PENDING: no NM water vectors at {OSM/'water.geojson'} yet. "
              f"Water bake deferred until the forest/water agent provides them.")
        return
    print(f"Loading NM water geometry from {OSM} ...")
    polys = load_water_polygons()
    rivers, minor = load_waterways()
    tree = STRtree(polys)
    print(f"  polygons={len(polys)} rivers={len(rivers)} minor={len(minor)}")
    if not rivers and not minor:
        print("  NOTE: no waterways.geojson — baking lakes/reservoirs polygons only "
              "(rivers pending the forest/water agent).")
    patches = compute_water_patches(tree, polys, rivers)
    print(f"Significant-water patches: {len(patches)}")
    results = {}
    for i, (c, r) in enumerate(patches, 1):
        nm, st = bake(c, r, tree, polys, rivers, minor)
        results[nm] = st
        if i % 10 == 0 or i == len(patches):
            print(f"  [{i}/{len(patches)}] {nm}: {st}", flush=True)
    ok = sum(1 for v in results.values() if v.startswith("OK"))
    print(f"\nNM WATER BAKE DONE: {ok}/{len(patches)} OK")
    (ROOT / "validation").mkdir(exist_ok=True)
    (ROOT / "validation" / "nm_water_bake_results.json").write_text(
        json.dumps({"patches": list(results), "results": results}, indent=2))


if __name__ == "__main__":
    main()
