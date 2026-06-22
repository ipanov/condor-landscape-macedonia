#!/usr/bin/env python3
"""Place the Stenkovec hangar .c3d exactly on its OSM vector footprint and verify
against the installed t0704 texture. Deterministic, on-disk, repeatable.

  vector polygon (aeroway=hangar) -> centroid + min-rotated-rect (pos, azimuth)
  model .c3d      -> local footprint centroid + PCA long-axis (intrinsic offset)
  ori = building_azimuth - model_PCA_azimuth   (un-double-counts the heading)
  ref = building_centroid - scale*R(ori)*model_centroid  (centres model on bldg)

Run with no args = DRY RUN (writes .sandbox/hangar_place_overlay.png only).
Run with --commit to also write the single-record .obj and back up the old one.
Optional: --az <deg> force the building azimuth; --scale <s>; --mirror to reflect
the model across its long axis (fixes the summer-house-on-wrong-side mirror).
"""
import os, sys, math, json, struct, shutil
sys.path.insert(0, 'scripts')
import condor_grid as G
import c3d as C3
import pyproj
import numpy as np
from shapely.geometry import shape, MultiPoint
from shapely.ops import transform as shp_transform
from PIL import Image, ImageDraw

INSTALL = 'C:/Condor2/Landscapes/MacedoniaSkopje/'
NAME = 'StenkovecHangar.c3d'
CENTER_E, CENTER_N = 531842.2, 4656465.6

args = sys.argv[1:]
COMMIT = '--commit' in args
MIRROR = '--mirror' in args
def argval(flag, default):
    return float(args[args.index(flag)+1]) if flag in args else default
FORCE_AZ = argval('--az', None) if '--az' in args else None
SCALE = argval('--scale', 0.85)

# ---- 1. OSM hangar polygon -------------------------------------------------
to_utm = pyproj.Transformer.from_crs(G.WGS84_CRS, G.UTM_CRS, always_xy=True).transform
best = None
for f in json.load(open('.sandbox/osm/buildings.geojson', encoding='utf-8'))['features']:
    p = f.get('properties', {})
    if p.get('aeroway') == 'hangar':
        g = shp_transform(to_utm, shape(f['geometry']))
        d = math.hypot(g.centroid.x - CENTER_E, g.centroid.y - CENTER_N)
        if d < 200 and (best is None or d < best[0]):
            best = (d, g, p)
assert best, 'no aeroway=hangar near Stenkovec'
_, poly, props = best
bC = poly.centroid; bE, bN = bC.x, bC.y
mrr = poly.minimum_rotated_rectangle
xs, ys = mrr.exterior.coords.xy
def edge(i, j):
    dx, dy = xs[j]-xs[i], ys[j]-ys[i]
    return math.hypot(dx, dy), math.degrees(math.atan2(dx, dy)) % 180.0
L1, a1 = edge(0, 1); L2, a2 = edge(1, 2)
bL, bW, bAz = (L1, L2, a1) if L1 >= L2 else (L2, L1, a2)
if FORCE_AZ is not None:
    bAz = FORCE_AZ
print(f"OSM hangar '{props.get('name')}'  centroid UTM {bE:.1f},{bN:.1f}  "
      f"rect {bL:.1f}x{bW:.1f} m  azimuth {bAz:.1f} deg")

# ---- 2. model geometry -----------------------------------------------------
mesh = C3.parse_c3d(INSTALL + 'World/Objects/' + NAME)
base = [(v.px, v.py) for ob in mesh.objects for v in ob.vertices if v.pz < 0.6]
annex = [(v.px, v.py) for ob in mesh.objects if ob.name.split('_')[-1] in
         ('CHAIR', 'TABLE', 'CHALKBOARD', 'FRONT') for v in ob.vertices if v.pz < 3.0]
xy = np.array(base); cx, cy = xy[:, 0].mean(), xy[:, 1].mean()
d = xy - [cx, cy]; ev, evec = np.linalg.eigh(d.T @ d)
maj = evec[:, np.argmax(ev)]
pca_az = math.degrees(math.atan2(maj[0], maj[1])) % 180.0
print(f'model PCA long-axis {pca_az:.1f} deg, local centroid ({cx:.2f},{cy:.2f})')

ori_deg = (bAz - pca_az) % 360.0
ori = math.radians(ori_deg)
print(f'-> ori = {bAz:.1f} - {pca_az:.1f} = {ori_deg:.1f} deg   scale={SCALE}  mirror={MIRROR}')

# ---- texture<->object grid correction --------------------------------------
# Textures are warped to patch_bounds_utm (DEM grid); objects place on the .trn
# (object-anchor) grid. A feature painted at true UTM (E,N) therefore DISPLAYS at
# (E + (Oe_min-De_min), N + (On_max-Dn_max)). To land the object ON the painted
# building we add that same display shift to the OSM centroid. Verified at
# Stenkovec: shift = (-45 m E, +45 m N) == -16 texels (calib2).
col, row = 7, 4
De_min, Dn_min, De_max, Dn_max = G.patch_bounds_utm(col, row)
Oe_min = (G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M) - G.PATCH_SIZE_M
On_max = (G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M) + G.PATCH_SIZE_M
corrE, corrN = Oe_min - De_min, On_max - Dn_max
tE, tN = bE + corrE, bN + corrN
# optional fine nudge (m, E/N) from the verification overlay
tE += float(os.environ.get('NUDGEE', 0)); tN += float(os.environ.get('NUDGEN', 0))
print(f'grid correction (texture DEM->object .trn): ({corrE:+.0f},{corrN:+.0f}) m  '
      f'-> placement centroid UTM {tE:.1f},{tN:.1f}')

co, so = math.cos(ori), math.sin(ori)
def place(lx, ly):
    ax = (lx - cx) * SCALE; ay = (ly - cy) * SCALE
    if MIRROR:                                   # reflect across the long (local-y/PCA) axis
        ax = -ax
    dE = ax*co + ay*so; dN = -ax*so + ay*co
    return tE + dE, tN + dN

# .obj reference point (model local origin) so the footprint centroid lands on tC
axc, ayc = -cx*SCALE, -cy*SCALE
if MIRROR: axc = -axc
refE = tE + (axc*co + ayc*so); refN = tN + (-axc*so + ayc*co)
posX, posY = G.obj_record_xy(refE, refN)
print(f'.obj  posX={posX:.1f} posY={posY:.1f} posZ=? ori={ori_deg:.1f} scale={SCALE}')

# ---- 3. verify overlay on t0704 -------------------------------------------
col, row = 7, 4
e_max = G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M
def tpx(e, n):
    return ((e-e_min)/(e_max-e_min)*2048, (n_max-n)/(n_max-n_min)*2048)
tex = Image.open(INSTALL + 'Textures/t0704.dds').convert('RGB')
ccx, ccy = tpx(tE, tN); half = int(os.environ.get('HALF', 70)); Z = int(os.environ.get('ZOOM', 8))
box = (int(ccx-half), int(ccy-half), int(ccx+half), int(ccy+half))
img = tex.crop(box).resize(((box[2]-box[0])*Z, (box[3]-box[1])*Z), Image.LANCZOS)
dr = ImageDraw.Draw(img, 'RGBA')
def toi(e, n):
    px, py = tpx(e, n); return ((px-box[0])*Z, (py-box[1])*Z)
# OSM polygon (cyan) + min-rect (yellow)
dr.line([toi(x, y) for x, y in poly.exterior.coords], fill=(0, 255, 255, 255), width=2)
dr.line([toi(x, y) for x, y in zip(xs, ys)], fill=(255, 255, 0, 200), width=1)
# model base hull (red) + annex hull (green) placed
hull = MultiPoint([place(lx, ly) for lx, ly in base]).convex_hull
dr.line([toi(x, y) for x, y in hull.exterior.coords], fill=(255, 40, 40, 255), width=3)
if annex:
    ah = MultiPoint([place(lx, ly) for lx, ly in annex]).convex_hull
    if ah.geom_type == 'Polygon':
        dr.line([toi(x, y) for x, y in ah.exterior.coords], fill=(0, 255, 0, 255), width=3)
# corrected placement centroid (magenta) + N arrow; OSM raw centroid (cyan dot)
mx, my = toi(tE, tN); dr.ellipse([mx-4, my-4, mx+4, my+4], fill=(255, 0, 255))
ox, oy = toi(bE, bN); dr.ellipse([ox-3, oy-3, ox+3, oy+3], fill=(0, 255, 255))
nx, ny = toi(tE, tN+30); dr.line([mx, my, nx, ny], fill=(255, 255, 255), width=2)
dr.text((6, 6), f'cyan=OSM hangar  red=model  green=annex(should be REAR-LEFT)  ori={ori_deg:.0f} az={bAz:.0f} mir={MIRROR}', fill=(255, 255, 0))
img.save('.sandbox/hangar_place_overlay.png')
print('wrote .sandbox/hangar_place_overlay.png')

# ---- 4. commit -------------------------------------------------------------
if COMMIT:
    # sample terrain altitude at bC for posZ from the .obj's existing value? keep
    # the existing posZ (terrain elevation) from the current record.
    objp = INSTALL + NAME.replace('.c3d', '') and INSTALL + 'MacedoniaSkopje.obj'
    old = open(objp, 'rb').read()
    posZ = struct.unpack_from('<5f', old, 0)[2] if len(old) >= 20 else 318.0
    nm = NAME.encode('latin1')
    rec = struct.pack('<5f', posX, posY, posZ, SCALE, ori) + bytes([len(nm)]) + nm
    rec = rec.ljust(152, b'\x00')
    shutil.copy(objp, objp + '.bak_place')
    open(objp, 'wb').write(rec)
    print(f'COMMITTED .obj ({len(rec)} bytes), backup .bak_place  posZ={posZ:.1f}')
