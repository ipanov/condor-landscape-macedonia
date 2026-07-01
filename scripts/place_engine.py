#!/usr/bin/env python3
r"""Deterministic, object-agnostic placement PIPELINE + CLI on top of mount_engine.

ONE command places ANY set of 3D objects (custom or autogen) onto the painted
footprints in the GAME texture: no AI, no per-object code, multi-object, fast.

  python scripts/place_engine.py [--only ID,ID] [--commit]

Reads data/placement_manifest.json. Each object: c3d + a footprint SEED source
(OSM/coords) for the rough location + long-axis prior + optional front groups. The
engine then registers the model's footprint OUTLINE to the building edges in the
installed tCCRR.dds (mount_engine.register_core), anchors by the GEOMETRIC centroid,
uses the model's NATIVE size, and writes/accumulates the .obj records.
"""
from __future__ import annotations
import argparse
import json
import math
import shutil
import struct
import sys
from pathlib import Path

import numpy as np
import pyproj
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import condor_grid as G          # noqa: E402
import mount_engine as ME        # noqa: E402

REC = 152
DEM = REPO / "sources/dem/macedonia_skopje_dem_30m_2305_flat.raw"
DEM_ULX, DEM_ULY, DEM_W, DEM_PX = 506880.0, 4700160.0, 2305, 30.0
_to_utm = pyproj.Transformer.from_crs(4326, 32634, always_xy=True).transform


def patch_of(E, N):
    return (int((G.OBJ_ANCHOR_E - E) // G.PATCH_SIZE_M),
            int((N - G.OBJ_ANCHOR_N) // G.PATCH_SIZE_M))


def texture_crop(col, row, seed_EN, half_m, install):
    """Crop installed tCCRR.dds around seed_EN; affine maps OBJECT-grid UTM -> crop px."""
    from PIL import Image
    e_min = G.OBJ_ANCHOR_E - (col + 1) * G.PATCH_SIZE_M
    n_max = G.OBJ_ANCHOR_N + (row + 1) * G.PATCH_SIZE_M
    s = 2048.0 / G.PATCH_SIZE_M
    fpx = (seed_EN[0] - e_min) * s; fpy = (n_max - seed_EN[1]) * s
    half = int(half_m * s)
    bx0, by0 = int(fpx - half), int(fpy - half)
    tex = Image.open(Path(install) / "Textures" / f"t{col:02d}{row:02d}.dds").convert("RGB")
    img = np.asarray(tex.crop((bx0, by0, bx0 + 2 * half, by0 + 2 * half)))
    affine = (s, -e_min * s - bx0, -s, n_max * s - by0)
    return img, affine


def dem_alt(E, N):
    dem = np.fromfile(DEM, dtype="<u2").reshape(DEM_W, DEM_W)
    c = min(max(int(round((E - DEM_ULX) / DEM_PX)), 0), DEM_W - 1)
    r = min(max(int(round((DEM_ULY - N) / DEM_PX)), 0), DEM_W - 1)
    return float(dem[r, c])


# --------------------------------------------------------------------------- #
# GLOBAL building-footprint source (the authoritative pose: centroid + edge axis).
# Per docs/OBJECT_PLACEMENT.md: position/orientation come from the VECTOR footprint,
# NEVER from hand-typed lat/lon. Texture is used only to validate (the overlay).
# --------------------------------------------------------------------------- #
_FP_DATASET = REPO / ".sandbox/buildings/buildings_combined_utm.geojson"  # cadastre u MS-GlobalML u OSM
_FP_CACHE = None


def _load_footprints():
    global _FP_CACHE
    if _FP_CACHE is None:
        feats = json.loads(_FP_DATASET.read_text(encoding="utf-8"))["features"]
        polys = []
        for f in feats:
            try:
                g = shape(f["geometry"])
            except Exception:
                continue
            if not g.is_empty:
                c = g.centroid
                polys.append((c.x, c.y, g))
        _FP_CACHE = polys
    return _FP_CACHE


def footprint_pose(near_utm, min_area=20.0, tol=45.0):
    """Return (centroid_E, centroid_N, principal_axis_deg, area) of the building
    footprint at `near_utm` (true UTM) in the global combined dataset, or None.
    Picks the polygon CONTAINING the seed, else the nearest centroid within `tol`."""
    from shapely.geometry import Point
    E, N = float(near_utm[0]), float(near_utm[1])
    pt = Point(E, N)
    cand = sorted(((math.hypot(cx - E, cy - N), g) for cx, cy, g in _load_footprints()
                   if abs(cx - E) < 120 and abs(cy - N) < 120), key=lambda t: t[0])
    hit = next(((d, g) for d, g in cand[:8] if (g.contains(pt) or d < tol) and g.area >= min_area), None)
    if hit is None:
        return None
    g = hit[1]
    mrr = g.minimum_rotated_rectangle
    xs, ys = mrr.exterior.coords.xy
    axis = max(((math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i]),
                 math.degrees(math.atan2(xs[i + 1] - xs[i], ys[i + 1] - ys[i])) % 180)
                for i in range(4)))[1]
    return float(g.centroid.x), float(g.centroid.y), float(axis), float(g.area)


def seed_and_prior(obj):
    fp = obj["footprint"]
    if fp["source"] == "osm":
        best = None
        for f in json.loads((REPO / ".sandbox/osm/buildings.geojson").read_text(encoding="utf-8"))["features"]:
            if str(f.get("properties", {}).get(fp["filter_key"])) != str(fp["filter_val"]):
                continue
            g = shp_transform(_to_utm, shape(f["geometry"]))
            if g.geom_type == "MultiPolygon":
                g = max(g.geoms, key=lambda p: p.area)
            near = fp.get("near_utm")
            d = math.hypot(g.centroid.x - near[0], g.centroid.y - near[1]) if near else 0.0
            if near and d > fp.get("radius_m", 300):
                continue
            if best is None or (near and d < best[0]):
                best = (d, g)
        g = best[1]
        xs, ys = g.minimum_rotated_rectangle.exterior.coords.xy
        az = max(((math.hypot(xs[i+1]-xs[i], ys[i+1]-ys[i]),
                   math.degrees(math.atan2(xs[i+1]-xs[i], ys[i+1]-ys[i])) % 180) for i in range(4)))[1]
        seed = (g.centroid.x + G.TEXTURE_FRAME_CORRECTION[0], g.centroid.y + G.TEXTURE_FRAME_CORRECTION[1])
        return seed, az
    if fp["source"] == "coords_utm":
        return G.painted_texture_xy(fp["E"], fp["N"]), float(fp.get("az", 0.0))
    raise SystemExit(f"seed source {fp['source']!r} unsupported")


def building_mask(rgb):
    """Roof mask in a game-texture crop: not-vegetation, not-deep-shadow."""
    from scipy import ndimage
    img = np.asarray(rgb).astype(np.float32)
    R, Gc, B = img[..., 0], img[..., 1], img[..., 2]
    mx = img.max(2); mn = img.min(2)
    sat = (mx - mn) / (mx + 1e-6); val = mx / 255.0
    veg = (Gc >= R - 2) & (Gc >= B - 2) & (sat > 0.10)
    roof = (~veg) & (val > 0.20)
    return ndimage.binary_closing(roof, iterations=1).astype(np.float32)


def refine_position_core(L, W, az_deg, seed_EN, img, affine, search_m=30.0, step=1.5):
    """PROVEN deterministic position: slide the footprint rectangle (L x W at az) over the
    building mask; return the (cE,cN) [object-grid UTM] that best covers a painted building.
    Pure function on an image + affine -> testable on synthetic data."""
    mask = building_mask(img)
    H, Wd = mask.shape
    sx, ox, sy, oy = affine
    yy, xx = np.mgrid[0:H, 0:Wd]
    Egrid = (xx - ox) / sx; Ngrid = (yy - oy) / sy           # UTM of every pixel
    azr = math.radians(az_deg)
    ux, uy = math.sin(azr), math.cos(azr); vx, vy = math.cos(azr), -math.sin(azr)
    hl, hw = L / 2.0, W / 2.0
    best = None
    for dE in np.arange(-search_m, search_m + 0.01, step):
        for dN in np.arange(-search_m, search_m + 0.01, step):
            cE, cN = seed_EN[0] + dE, seed_EN[1] + dN
            mE = Egrid - cE; mN = Ngrid - cN
            a = mE * ux + mN * uy; b = mE * vx + mN * vy
            inside = (np.abs(a) <= hl) & (np.abs(b) <= hw)
            if inside.sum() < 20:
                continue
            ring = (np.abs(a) <= hl + 6) & (np.abs(b) <= hw + 6) & (~inside)
            sc = mask[inside].mean() - 0.6 * (mask[ring].mean() if ring.any() else 0.0)
            if best is None or sc > best[0]:
                best = (sc, float(cE), float(cN))
    return best[1], best[2], best[0]


def place_one(obj, install, search_m=30.0):
    objdir = Path(install) / "World" / "Objects"
    c3d_path = objdir / obj["c3d"]
    mf = ME.model_footprint(c3d_path)
    fp = obj["footprint"]
    if fp["source"] == "static":
        # non-building objects (aircraft, monuments, towers) have no painted footprint to
        # match -> placed at fixed OBJECT-GRID coords + heading from the manifest.
        cE, cN = float(fp["E"]), float(fp["N"]); ori = float(fp.get("ori", 0.0))
        scale = float(obj.get("scale", 1.0)); z = dem_alt(cE, cN)
        col, row = patch_of(cE, cN)
        img, affine = texture_crop(col, row, (cE, cN), max(mf["nat_L"], mf["nat_W"]) + 20, install)
        return dict(id=obj["id"], c3d=obj["c3d"], cE=cE, cN=cN, ori=ori, scale=scale, z=z,
                    score=1.0, col=col, row=row, mf=mf, img=img, affine=affine)
    if fp["source"] == "footprint":
        # AUTHORITATIVE: pose from the GLOBAL building footprint (centroid + edge axis),
        # NOT hand-typed coords. position -> painted-texture frame so it lands on what
        # Condor draws; orientation -> footprint principal axis folded with the model's
        # own local axis; the 180 resolved by a directed front cue when present.
        res = footprint_pose(fp["near_utm"], min_area=fp.get("min_area", 20.0),
                             tol=fp.get("tol", 45.0))
        if res is None:
            raise SystemExit(f"{obj['id']}: NO footprint within tol at {fp['near_utm']} "
                             f"(use source 'static' for a non-building monument)")
        tE, tN, axis, area = res
        cE, cN = G.painted_texture_xy(tE, tN)
        base_ori = (axis - mf["axis"]) % 180.0
        cands = [base_ori % 360.0, (base_ori + 180.0) % 360.0]
        fl = ME.front_local_azimuth(c3d_path, obj.get("front", {}).get("model_groups"),
                                    obj.get("front", {}).get("rear_groups"))
        fw = obj.get("front", {}).get("world_azimuth")
        if fl is not None and fw is not None:
            ori = min(cands, key=lambda o: abs(((fl + o) % 360 - fw + 180) % 360 - 180))
        elif fp.get("ori") is not None:        # optional manual 180 pick (axis still from footprint)
            ori = min(cands, key=lambda o: abs(((o - float(fp["ori"])) + 180) % 360 - 180))
        else:
            ori = cands[0]
        scale = float(obj.get("scale", 1.0)); z = dem_alt(cE, cN)
        col, row = patch_of(cE, cN)
        img, affine = texture_crop(col, row, (cE, cN), max(mf["nat_L"], mf["nat_W"]) + 20, install)
        return dict(id=obj["id"], c3d=obj["c3d"], cE=cE, cN=cN, ori=ori, scale=scale, z=z,
                    score=1.0, col=col, row=row, mf=mf, img=img, affine=affine, fp_area=area, fp_axis=axis)
    seed, az = seed_and_prior(obj)
    scale = 1.0 if obj.get("scale_mode", "native") == "native" else float(obj.get("scale", 1.0))
    col, row = patch_of(*seed)
    half = search_m + max(mf["nat_L"], mf["nat_W"]) / 2 + 12
    img, affine = texture_crop(col, row, seed, half, install)
    cE, cN, score = refine_position_core(mf["nat_L"] * scale, mf["nat_W"] * scale, az,
                                         seed, img, affine, search_m=search_m)
    base_ori = (az - mf["axis"]) % 180.0
    cands = [base_ori % 360.0, (base_ori + 180.0) % 360.0]
    fl = ME.front_local_azimuth(c3d_path, obj.get("front", {}).get("model_groups"),
                                obj.get("front", {}).get("rear_groups"))
    fw = obj.get("front", {}).get("world_azimuth")
    if fl is not None and fw is not None:
        ori = min(cands, key=lambda o: abs(((fl + o) % 360 - fw + 180) % 360 - 180))
    else:
        ori = cands[0]
    z = dem_alt(cE, cN)
    return dict(id=obj["id"], c3d=obj["c3d"], cE=cE, cN=cN, ori=ori, scale=scale, z=z,
                score=score, col=col, row=row, mf=mf, img=img, affine=affine)


def make_record(c3d_name, r):
    o = math.radians(r["ori"])
    posX, posY = G.obj_record_xy(r["cE"], r["cN"])     # outline centred on geometric centroid
    nm = c3d_name.encode("ascii")
    return struct.pack("<5f", posX, posY, r["z"], r["scale"], o % (2*math.pi)) + bytes([len(nm)]) + nm.ljust(131, b"\x00")


def overlay(r, out_png):
    from PIL import Image, ImageDraw
    mf = r["mf"]; o = math.radians(r["ori"]); co, so = math.cos(o), math.sin(o)
    out = mf["outline"] * r["scale"]
    wE = r["cE"] + co*out[:, 0] + so*out[:, 1]; wN = r["cN"] - so*out[:, 0] + co*out[:, 1]
    sx, ox, sy, oy = r["affine"]; Z = 8
    vis = Image.fromarray(r["img"]).resize((r["img"].shape[1]*Z, r["img"].shape[0]*Z), Image.LANCZOS)
    dr = ImageDraw.Draw(vis)
    pts = [((sx*e+ox)*Z, (sy*n+oy)*Z) for e, n in zip(wE, wN)]
    dr.line(pts + [pts[0]], fill=(255, 40, 40), width=3)
    cx = (sx*r["cE"]+ox)*Z; cy = (sy*r["cN"]+oy)*Z
    dr.ellipse([cx-4, cy-4, cx+4, cy+4], fill=(255, 0, 255))
    dr.text((6, 6), f"{r['id']}  red=placed footprint on GAME texture  score={r['score']:.3f}", fill=(255, 255, 0))
    vis.save(out_png)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default=str(REPO / "data/placement_manifest.json"))
    ap.add_argument("--install", default="C:/Condor2/Landscapes/MacedoniaSkopje")
    ap.add_argument("--only", default="")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    man = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    only = set(s for s in a.only.split(",") if s) or None
    out_dir = REPO / ".sandbox/placement"; out_dir.mkdir(parents=True, exist_ok=True)
    objfile = Path(a.install) / f"{man.get('landscape', 'MacedoniaSkopje')}.obj"
    recs = []
    for obj in man["objects"]:
        if only and obj["id"] not in only:
            continue
        r = place_one(obj, a.install)
        overlay(r, out_dir / f"{r['id']}_engine.png")
        print(f"  {r['id']:22s} E={r['cE']:.1f} N={r['cN']:.1f} ori={r['ori']:.2f} "
              f"scale={r['scale']:.3f} z={r['z']:.0f} score={r['score']:.3f} t{r['col']:02d}{r['row']:02d}")
        recs.append((r["c3d"], make_record(r["c3d"], r)))
    if a.commit:
        keep = {}
        if objfile.exists():
            shutil.copy2(objfile, str(objfile) + ".bak_engine")
            d = objfile.read_bytes()
            for k in range(len(d)//REC):
                raw = d[k*REC:(k+1)*REC]; nl = raw[20]; keep[raw[21:21+nl].decode("latin1").lower()] = raw
        for nm, rec in recs:
            keep[nm.lower()] = rec
        objfile.write_bytes(b"".join(keep.values()))
        print(f"  [commit] {objfile.name}: {len(keep)} records")
    return recs


if __name__ == "__main__":
    main()
