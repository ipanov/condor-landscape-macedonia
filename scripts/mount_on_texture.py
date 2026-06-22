#!/usr/bin/env python3
"""Mount a model on the building OUTLINE measured directly in the installed texture
(the ground truth the user sees). One deterministic routine, O(1) per object, no
hand-tuning, no guessed scale:

  detect painted roof outline -> centroid + oriented rect (angle, L, W)   [from texture]
  scale = L_roof / L_model ; ori = roof_angle + front-rule                [measured]
  position = roof centroid mapped via the OBJECT grid                     [in-sim exact]

Outputs a proof overlay (detected outline vs mounted model). --commit writes .obj.
"""
import os, sys, math, json, struct, shutil
sys.path.insert(0, 'scripts')
import condor_grid as G
import c3d as C3
import pyproj, numpy as np
from shapely.geometry import shape, MultiPoint
from shapely.ops import transform as shp_transform
from scipy import ndimage
from PIL import Image, ImageDraw
import cv2

INSTALL = 'C:/Condor2/Landscapes/MacedoniaSkopje/'
NAME = 'StenkovecHangar.c3d'
COMMIT = '--commit' in sys.argv[1:]
to_utm = pyproj.Transformer.from_crs(G.WGS84_CRS, G.UTM_CRS, always_xy=True).transform

# OSM hangar centroid (seed only) + grid correction -> painted-roof seed
poly = None
for f in json.load(open('.sandbox/osm/buildings.geojson', encoding='utf-8'))['features']:
    if f.get('properties', {}).get('aeroway') == 'hangar':
        g = shp_transform(to_utm, shape(f['geometry']))
        if math.hypot(g.centroid.x-531842, g.centroid.y-4656466) < 200:
            poly = g; break
col, row = 7, 4
De_min, Dn_min, De_max, Dn_max = G.patch_bounds_utm(col, row)
Oe_min = (G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M) - G.PATCH_SIZE_M
On_max = (G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M) + G.PATCH_SIZE_M
seedE = poly.centroid.x + (Oe_min-De_min); seedN = poly.centroid.y + (On_max-Dn_max)

e_max = G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M
mpp = G.PATCH_SIZE_M/2048.0
def tpx(e, n): return ((e-e_min)/(e_max-e_min)*2048, (n_max-n)/(n_max-n_min)*2048)
def px2utm(px, py): return e_min+px/2048*(e_max-e_min), n_max-py/2048*(n_max-n_min)

tex = Image.open(INSTALL + 'Textures/t0704.dds').convert('RGB')
sx, sy = tpx(seedE, seedN)
H = 26
x0, y0 = int(sx-H), int(sy-H)
crop = np.asarray(tex.crop((x0, y0, x0+2*H, y0+2*H))).astype(np.float32)
R, Gc, B = crop[..., 0], crop[..., 1], crop[..., 2]
mx = crop.max(2); mn = crop.min(2); sat = (mx-mn)/(mx+1e-6); val = mx/255.0
roof = (~((Gc >= R-2) & (Gc >= B-2) & (sat > 0.10))) & (val > 0.20)   # not-grass, not-shadow
roof = ndimage.binary_closing(roof, iterations=2)
roof = ndimage.binary_opening(roof, iterations=1)
roof = ndimage.binary_fill_holes(roof)
lbl, nl = ndimage.label(roof)
cl = lbl[H, H]
if cl == 0:                       # seed not on roof: take nearest sizable blob
    best = None
    for i in range(1, nl+1):
        ys, xs = np.where(lbl == i)
        if len(xs) < 25:
            continue
        d = math.hypot(xs.mean()-H, ys.mean()-H)
        if best is None or d < best[0]:
            best = (d, i)
    cl = best[1] if best else 0
ys, xs = np.where(lbl == cl)
pts = np.column_stack([xs, ys]).astype(np.int32)
(rcx, rcy), (rw, rh), rang = cv2.minAreaRect(pts)
boxp = cv2.boxPoints(((rcx, rcy), (rw, rh), rang))
# long edge azimuth (texel y = +south so dN = -dpy)
edges = [boxp[(i+1) % 4]-boxp[i] for i in range(4)]
le = edges[int(np.argmax([np.hypot(*e) for e in edges]))]
roof_az = math.degrees(math.atan2(le[0]*mpp, -le[1]*mpp)) % 180.0
L, Wd = max(rw, rh)*mpp, min(rw, rh)*mpp
cE, cN = px2utm(x0+rcx, y0+rcy)
print(f'DETECTED roof in texture: centroid UTM {cE:.1f},{cN:.1f}  size {L:.1f} x {Wd:.1f} m  axis {roof_az:.1f} deg')

# ---- model footprint (its own oriented rect) -------------------------------
mesh = C3.parse_c3d(INSTALL + 'World/Objects/' + NAME)
base = [(v.px, v.py) for ob in mesh.objects for v in ob.vertices if v.pz < 0.6]
front = [(v.px, v.py) for ob in mesh.objects if ob.name == 'Hangar_FRONT' for v in ob.vertices]
mp = np.array(base, np.float32)
(_, _), (mw, mh), _ = cv2.minAreaRect(mp)
model_L = max(mw, mh)
mcx, mcy = mp[:, 0].mean(), mp[:, 1].mean()
fcx, fcy = np.mean([p[0] for p in front]), np.mean([p[1] for p in front])
front_local_az = math.degrees(math.atan2(fcx-mcx, fcy-mcy)) % 360
scale = L / model_L
print(f'model long={model_L:.1f} m  ->  scale = {L:.1f}/{model_L:.1f} = {scale:.3f}  (NOT a guess: matches the roof)')

# ---- front-rule: doors face the most-open (greenest) direction -------------
opens = []
for a in range(0, 360, 30):
    ra = math.radians(a)
    sxx = int(H + math.sin(ra)*H*0.8); syy = int(H - math.cos(ra)*H*0.8)
    sxx = min(2*H-1, max(0, sxx)); syy = min(2*H-1, max(0, syy))
    g = crop[syy, sxx]; greenness = g[1]-(g[0]+g[2])/2
    opens.append((greenness, a))
open_az = max(opens)[1]
print(f'open/apron direction (greenest) = {open_az} deg  -> doors face there')

# choose ori among 4 quadrants of roof_az so the model FRONT faces open_az
cands = [(roof_az + k*90) % 360 for k in range(4)]
def angd(a, b): return abs((a-b+180) % 360-180)
ori = min(cands, key=lambda o: angd((o+front_local_az) % 360, open_az))
ori_rad = math.radians(ori); co, so = math.cos(ori_rad), math.sin(ori_rad)
print(f'front local {front_local_az:.0f} -> chose ori={ori:.1f} (front faces {(ori+front_local_az)%360:.0f})')

def place(lx, ly):
    ax, ay = (lx-mcx)*scale, (ly-mcy)*scale
    return cE + ax*co + ay*so, cN - ax*so + ay*co
refE = cE + (-mcx*scale)*co + (-mcy*scale)*so
refN = cN - (-mcx*scale)*so + (-mcy*scale)*co
posX, posY = G.obj_record_xy(refE, refN)

# ---- proof overlay ---------------------------------------------------------
Z = 13
img = tex.crop((x0, y0, x0+2*H, y0+2*H)).resize((2*H*Z, 2*H*Z), Image.LANCZOS)
dr = ImageDraw.Draw(img, 'RGBA')
def toi(e, n):
    px, py = tpx(e, n); return ((px-x0)*Z, (py-y0)*Z)
# detected roof mask outline (yellow) + minAreaRect (cyan)
m = np.zeros((2*H, 2*H), bool); m[ys, xs] = True
edge = m & ~ndimage.binary_erosion(m); ey, ex = np.where(edge)
for px, py in zip(ex, ey):
    dr.rectangle([px*Z, py*Z, px*Z+Z, py*Z+Z], fill=(255, 255, 0, 70))
dr.line([tuple(p*Z) for p in np.vstack([boxp, boxp[0]])], fill=(0, 230, 255, 255), width=2)
# mounted model (red) + front (blue)
dr.line([toi(*place(x, y)) for x, y in MultiPoint(base).convex_hull.exterior.coords], fill=(255, 40, 40, 255), width=3)
fh = MultiPoint([place(x, y) for x, y in front]).convex_hull
if fh.geom_type == 'Polygon':
    dr.line([toi(x, y) for x, y in fh.exterior.coords], fill=(0, 160, 255, 255), width=4)
dr.text((6, 6), f'yellow=detected roof  cyan=its rect {L:.0f}x{Wd:.0f}m@{roof_az:.0f}  red=model scale={scale:.2f} ori={ori:.0f}', fill=(255, 255, 0))
img.save('.sandbox/mount_overlay.png')
print(f'.obj posX={posX:.1f} posY={posY:.1f} ori={ori:.1f} scale={scale:.3f} -> .sandbox/mount_overlay.png')

if COMMIT:
    objp = INSTALL + 'MacedoniaSkopje.obj'
    old = open(objp, 'rb').read(); posZ = struct.unpack_from('<5f', old, 0)[2]
    nm = NAME.encode('latin1')
    rec = (struct.pack('<5f', posX, posY, posZ, scale, ori_rad)+bytes([len(nm)])+nm).ljust(152, b'\x00')
    shutil.copy(objp, objp+'.bak_mount'); open(objp, 'wb').write(rec)
    print(f'COMMITTED  posZ={posZ:.1f}')
