#!/usr/bin/env python3
r"""Mount a 3D object on the building as PAINTED IN THE GAME TEXTURE.

Inputs are ONLY: (1) the exact installed game texture t<CCRR>.dds, (2) the 3D object
(.c3d), (3) the OSM/GIS footprint VECTOR (for the size+orientation+which-side-is-front;
the 2.8 m/texel texture is too coarse to measure 1 deg, but it is the ground truth for
WHERE the building is painted). NO other texture (no cadastre ortho, no Esri) is used.

Method (exactly what the user asked for):
  1. footprint SHAPE from OSM (length, width, long-axis azimuth, road/apron side).
  2. POSITION = slide that footprint rectangle over the GAME texture's building-mask
     near the seed and take the (dE,dN) that best covers a painted building -> the
     hangar's centroid AS PAINTED. This is immune to the installed-texture grid drift
     (the old 29.987 m build) because it locks onto the actual painted roof.
  3. ORIENTATION = OSM long-axis; FRONT/REAR (180 deg) = OSM/GIS automatically (doors
     face the runway/apron side; --flip to override the binary if it reads backwards).
  4. VALIDATE by overlaying the mounted object footprint + door arrow ON THE GAME
     TEXTURE. Commit only on --commit.

CLI: python scripts/mount_on_game_texture.py [--flip] [--commit]
"""
from __future__ import annotations
import argparse, json, math, struct, shutil, sys
from pathlib import Path
import numpy as np
import pyproj
from PIL import Image, ImageDraw
from scipy import ndimage
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import condor_grid as G          # noqa: E402
import c3d as C3                 # noqa: E402

INSTALL = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
TEXDIR = INSTALL / "Textures"
OBJDIR = INSTALL / "World" / "Objects"
OBJFILE = INSTALL / "MacedoniaSkopje.obj"
DEM = REPO / "sources/dem/macedonia_skopje_dem_30m_2305_flat.raw"
DEM_ULX, DEM_ULY, DEM_W, DEM_PX = 506880.0, 4700160.0, 2305, 30.0
OUT = REPO / ".sandbox/placement"; OUT.mkdir(parents=True, exist_ok=True)
COL, ROW = 7, 4
NAME = "StenkovecHangar.c3d"
_to_utm = pyproj.Transformer.from_crs(4326, 32634, always_xy=True).transform


def osm_footprint():
    best = None
    for f in json.loads((REPO / ".sandbox/osm/buildings.geojson").read_text(encoding="utf-8"))["features"]:
        if f.get("properties", {}).get("aeroway") == "hangar":
            g = shp_transform(_to_utm, shape(f["geometry"]))
            if g.geom_type == "MultiPolygon":
                g = max(g.geoms, key=lambda p: p.area)
            if math.hypot(g.centroid.x - 531842, g.centroid.y - 4656466) < 300:
                best = g; break
    if best is None:
        raise SystemExit("OSM hangar not found")
    mrr = best.minimum_rotated_rectangle
    xs, ys = mrr.exterior.coords.xy
    edges = sorted(((math.hypot(xs[i+1]-xs[i], ys[i+1]-ys[i]),
                     math.degrees(math.atan2(xs[i+1]-xs[i], ys[i+1]-ys[i])) % 180)
                    for i in range(4)), key=lambda t: -t[0])
    L, az = edges[0]; Wd = edges[2][0]
    return dict(poly=best, L=L, W=Wd, az=az, cE=best.centroid.x, cN=best.centroid.y)


def runway_bearing(cE, cN):
    p = REPO / ".sandbox/osm/runways.geojson"
    if not p.exists():
        return None
    from shapely.geometry import Point
    pt = Point(cE, cN)
    best = None
    for f in json.loads(p.read_text(encoding="utf-8"))["features"]:
        g = shp_transform(_to_utm, shape(f["geometry"]))
        try:
            npn = g.boundary.interpolate(g.boundary.project(pt))   # nearest point on the strip
        except Exception:
            npn = g.centroid
        d = g.distance(pt)
        if best is None or d < best[0]:
            best = (d, math.degrees(math.atan2(npn.x - cE, npn.y - cN)) % 360.0)
    return best[1] if best else None


# object-grid texel mapping for patch (COL,ROW)
e_max = G.OBJ_ANCHOR_E - COL*G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + ROW*G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M
MPP = G.PATCH_SIZE_M / 2048.0
def tpx(e, n): return ((e-e_min)/(e_max-e_min)*2048.0, (n_max-n)/(n_max-n_min)*2048.0)


def building_mask(gray_rgb):
    R, Gc, B = gray_rgb[..., 0], gray_rgb[..., 1], gray_rgb[..., 2]
    mx = gray_rgb.max(2); mn = gray_rgb.min(2)
    sat = (mx - mn) / (mx + 1e-6); val = mx / 255.0
    veg = (Gc >= R - 2) & (Gc >= B - 2) & (sat > 0.10)
    roof = (~veg) & (val > 0.20)
    roof = ndimage.binary_closing(roof, iterations=1)
    return roof.astype(np.float32)


def refine_position(fp, seedE, seedN, search_m=38.0, step=1.0):
    """Slide the OSM rectangle over the GAME-texture building-mask; return the (E,N)
    where it best covers a painted building (max inside-mask minus border-ring-mask)."""
    half = int((max(fp["L"], fp["W"]) + 2*search_m) / MPP)
    sx, sy = tpx(seedE, seedN)
    x0, y0 = int(sx - half), int(sy - half)
    crop = np.asarray(Image.open(TEXDIR / f"t{COL:02d}{ROW:02d}.dds").convert("RGB"))[y0:y0+2*half, x0:x0+2*half].astype(np.float32)
    mask = building_mask(crop)
    # rectangle (axis az) corner offsets in metres
    az = math.radians(fp["az"]); ux, uy = math.sin(az), math.cos(az); vx, vy = math.cos(az), -math.sin(az)
    hl, hw = fp["L"]/2, fp["W"]/2
    yy, xx = np.mgrid[0:2*half, 0:2*half]
    best = None
    for dE in np.arange(-search_m, search_m+0.1, step):
        for dN in np.arange(-search_m, search_m+0.1, step):
            cE, cN = seedE+dE, seedN+dN
            px, py = tpx(cE, cN); lx, ly = px-x0, py-y0
            # transform pixel grid to rect-local metres
            mE = (xx-lx)*MPP; mN = -(yy-ly)*MPP
            a = mE*ux + mN*uy; b = mE*vx + mN*vy
            inside = (np.abs(a) <= hl) & (np.abs(b) <= hw)
            ring = (np.abs(a) <= hl+6) & (np.abs(b) <= hw+6) & (~inside)
            if inside.sum() < 20:
                continue
            score = mask[inside].mean() - 0.6*mask[ring].mean()
            if best is None or score > best[0]:
                best = (score, cE, cN)
    return best[1], best[2], best[0], (x0, y0, crop, mask)


def model_base(name):
    cf = C3.parse_c3d(OBJDIR / name)
    allv = [(v.px, v.py, v.pz) for o in cf.objects for v in o.vertices]
    zmin = min(v[2] for v in allv)
    base = np.array([(x, y) for x, y, z in allv if z <= zmin + 0.75])
    # GEOMETRIC footprint centre (convex-hull centroid), NOT the vertex mean: the vertex
    # mean is biased ~5.8 m toward the dense-vertex side (doors/clubroom), which shifts the
    # placed building "too far back". The hull centroid is the true centre the texture
    # template match aligns to.
    from shapely.geometry import MultiPoint as _MP
    _hull = _MP([tuple(p) for p in base]).convex_hull
    cx, cy = float(_hull.centroid.x), float(_hull.centroid.y)
    dx, dy = float(np.ptp(base[:, 0])), float(np.ptp(base[:, 1]))
    L = max(dx, dy)
    axis = 90.0 if dx >= dy else 0.0
    return cx, cy, L, axis, base


def dem_alt(E, N):
    dem = np.fromfile(DEM, dtype="<u2").reshape(DEM_W, DEM_W)
    c = min(max(int(round((E-DEM_ULX)/DEM_PX)), 0), DEM_W-1)
    r = min(max(int(round((DEM_ULY-N)/DEM_PX)), 0), DEM_W-1)
    return float(dem[r, c])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--flip", action="store_true", help="flip the 180 deg front/rear")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args(argv)

    fp = osm_footprint()
    rb = runway_bearing(fp["cE"], fp["cN"])
    mcx, mcy, mL, maxis, base = model_base(NAME)
    mdx = float(base[:, 0].max() - base[:, 0].min()); mdy = float(base[:, 1].max() - base[:, 1].min())
    nat_L, nat_W = max(mdx, mdy), min(mdx, mdy)
    # RESTORE the 3D model's NATIVE real size. The SoFly model IS the real hangar; OSM's
    # footprint is unreliable here (wrong size + aspect), so we do NOT shrink to it.
    scale = 1.0
    print(f"MODEL native size {nat_L:.1f} x {nat_W:.1f} m  scale=1.0 (real size from the 3D model); "
          f"OSM long-axis {fp['az']:.1f} deg, runway bearing {rb}")
    # POSITION: slide the NATIVE model footprint over the GAME texture to the painted roof.
    tmpl = dict(L=nat_L, W=nat_W, az=fp["az"], cE=fp["cE"], cN=fp["cN"])
    cE, cN, score, (x0, y0, crop, mask) = refine_position(tmpl, fp["cE"], fp["cN"])
    print(f"PAINTED centroid in game texture (object grid): {cE:.1f}, {cN:.1f}  (match score {score:.3f})")
    # ori: align model long-axis to OSM long-axis; 2 candidates 180 apart
    base_ori = (fp["az"] - maxis) % 180.0
    cands = [base_ori % 360.0, (base_ori + 180.0) % 360.0]
    # FRONT/REAR from OSM: doors face the runway/apron side -> the long-wall normal
    # nearest the runway bearing. (long-wall normals = az +- 90.)
    wallA = (fp["az"] + 90.0) % 360.0; wallB = (fp["az"] - 90.0) % 360.0
    def angd(a, b): return abs((a-b+180) % 360 - 180)
    door_world = wallA if (rb is None or angd(wallA, rb) <= angd(wallB, rb)) else wallB
    # choose ori so the model's +Y (north at ori0) ... we resolve by which candidate puts
    # the model's door wall toward door_world. The model long axis is maxis; doors are on
    # a long wall, normal = maxis+90 at ori0 -> after ori, (maxis+90+ori). pick nearest door_world.
    ori = min(cands, key=lambda o: angd((maxis + 90.0 + o) % 360.0, door_world))
    if args.flip:
        ori = (ori + 180.0) % 360.0
    z = dem_alt(cE, cN)
    print(f"scale {scale:.3f}  ori {ori:.2f} deg  doors face {door_world:.0f} deg (runway side)  posZ {z:.0f} m  flip={args.flip}")

    # ---- overlay on the GAME texture ----
    o = math.radians(ori); co, so = math.cos(o), math.sin(o)
    def place(lx, ly):
        ax, ay = (lx-mcx)*scale, (ly-mcy)*scale
        return cE + co*ax + so*ay, cN - so*ax + co*ay
    Z = 9; half = crop.shape[0]//2
    img = Image.fromarray(crop.astype(np.uint8)).resize((crop.shape[1]*Z, crop.shape[0]*Z), Image.LANCZOS)
    dr = ImageDraw.Draw(img, "RGBA")
    def toi(e, n):
        px, py = tpx(e, n); return ((px-x0)*Z, (py-y0)*Z)
    from shapely.geometry import MultiPoint
    hull = MultiPoint([place(x, y) for x, y in base]).convex_hull
    dr.line([toi(x, y) for x, y in hull.exterior.coords], fill=(255, 40, 40, 255), width=3)
    pe = toi(cE, cN)
    tipE = cE + math.sin(math.radians(door_world))*nat_L*0.6
    tipN = cN + math.cos(math.radians(door_world))*nat_L*0.6
    ti = toi(tipE, tipN)
    dr.line([pe[0], pe[1], ti[0], ti[1]], fill=(0, 160, 255, 255), width=4)   # door direction
    dr.ellipse([pe[0]-5, pe[1]-5, pe[0]+5, pe[1]+5], fill=(255, 0, 255))
    dr.text((6, 6), f"red=model on PAINTED roof  blue=doors->{door_world:.0f}deg  ori={ori:.0f} scale={scale:.2f}", fill=(255, 255, 0))
    p = OUT / "hangar_on_game_texture.png"; img.save(p)
    print(f"overlay (GAME texture): {p}")

    # record
    refE = cE - (co*(scale*mcx) + so*(scale*mcy))
    refN = cN - (-so*(scale*mcx) + co*(scale*mcy))
    posX, posY = G.obj_record_xy(refE, refN)
    if args.commit:
        nm = NAME.encode("ascii")
        rec = (struct.pack("<5f", posX, posY, z, scale, o % (2*math.pi)) + bytes([len(nm)]) + nm.ljust(131, b"\x00"))
        shutil.copy2(OBJFILE, str(OBJFILE)+".bak_gametex")
        OBJFILE.write_bytes(rec)
        print(f"COMMITTED  posX={posX:.1f} posY={posY:.1f} ori={ori:.1f} (backup .bak_gametex)")
    else:
        print(f"(dry run) posX={posX:.1f} posY={posY:.1f} ori={ori:.1f}")


if __name__ == "__main__":
    main()
