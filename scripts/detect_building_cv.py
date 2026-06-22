#!/usr/bin/env python3
"""Measure a building's true centroid + orientation DIRECTLY in the installed
patch texture (deterministic CV, no LLM/SAM). Segments the roof (non-vegetation)
near a seed, fits a minimum-area oriented rectangle, and reports the centroid in
OBJECT-grid UTM (so an object placed there lands exactly on the painted roof) and
the long-edge azimuth. Writes .sandbox/hangar_detect.json + a verification overlay.

This is the repeatable detector behind precise placement -- the mask source is
swappable (here: color/vegetation segmentation; could be Florence-2 / SAM mask),
but the geometry (oriented-bbox -> azimuth + texel->UTM) is the part that matters.
"""
import os, sys, math, json
sys.path.insert(0, 'scripts')
import condor_grid as G
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

INSTALL = 'C:/Condor2/Landscapes/MacedoniaSkopje/'
col, row = int(os.environ.get('COL', 7)), int(os.environ.get('ROW', 4))
# seed = approx painted-building location (OSM centroid + grid correction)
seedE = float(os.environ.get('SEEDE', 531800.3))
seedN = float(os.environ.get('SEEDN', 4656515.4))
HALF = int(os.environ.get('HALF', 40))           # crop half-size in texels

e_max = G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M
mpp = G.PATCH_SIZE_M / 2048.0
def tpx(e, n): return ((e-e_min)/(e_max-e_min)*2048, (n_max-n)/(n_max-n_min)*2048)
def px2utm(px, py): return e_min + px/2048*(e_max-e_min), n_max - py/2048*(n_max-n_min)

tex = Image.open(INSTALL + f'Textures/t{col:02d}{row:02d}.dds').convert('RGB')
sx, sy = tpx(seedE, seedN)
x0, y0, x1, y1 = int(sx-HALF), int(sy-HALF), int(sx+HALF), int(sy+HALF)
crop = np.asarray(tex.crop((x0, y0, x1, y1))).astype(np.float32)
R, Gc, B = crop[..., 0], crop[..., 1], crop[..., 2]
mx = crop.max(2); mn = crop.min(2)
sat = (mx-mn)/(mx+1e-6); val = mx/255.0
veg = (Gc >= R-2) & (Gc >= B-2) & (sat > 0.12)   # grass/trees: green-dominant
dark = val < 0.18                                 # deep shadow
bld = (~veg) & (~dark)
bld = ndimage.binary_opening(bld, iterations=1)
bld = ndimage.binary_closing(bld, iterations=2)
bld = ndimage.binary_fill_holes(bld)
lbl, nl = ndimage.label(bld)
cen = np.array([HALF, HALF])
best = None
for i in range(1, nl+1):
    ys, xs = np.where(lbl == i); area = len(xs)
    if area < 35 or area > (2*HALF)**2*0.65:
        continue
    c = np.array([xs.mean(), ys.mean()])
    d = np.hypot(*(c-cen))
    if d < HALF*0.7 and (best is None or area > best[0]):
        best = (area, i, xs, ys)
assert best, 'no building-like blob near seed; adjust SEED/HALF or segmentation'
_, _, xs, ys = best

# oriented bounding box
pts = np.column_stack([xs, ys]).astype(np.float64)
try:
    import cv2
    rect = cv2.minAreaRect(pts.astype(np.int32))
    (rcx, rcy), (rw, rh), rang = rect
    boxp = cv2.boxPoints(rect)
    method = 'cv2.minAreaRect'
except Exception:
    # PCA fallback
    c = pts.mean(0); dd = pts - c
    cov = dd.T @ dd; ev, evec = np.linalg.eigh(cov)
    maj = evec[:, np.argmax(ev)]; mino = evec[:, np.argmin(ev)]
    proj_a = dd @ maj; proj_b = dd @ mino
    rw, rh = proj_a.max()-proj_a.min(), proj_b.max()-proj_b.min()
    rcx, rcy = c
    corners = []
    for sa in (proj_a.min(), proj_a.max()):
        for sb in (proj_b.min(), proj_b.max()):
            corners.append(c + sa*maj + sb*mino)
    boxp = np.array([corners[0], corners[1], corners[3], corners[2]])
    rang = math.degrees(math.atan2(maj[1], maj[0])); method = 'PCA'

# long-edge vector -> azimuth (texel y is +South, so dN = -dpy)
boxp = np.array(boxp, float)
edges = [(boxp[(i+1) % 4]-boxp[i]) for i in range(4)]
elen = [np.hypot(*e) for e in edges]
le = edges[int(np.argmax(elen))]
dE, dN = le[0]*mpp, -le[1]*mpp
az = math.degrees(math.atan2(dE, dN)) % 180.0
L, W = max(rw, rh)*mpp, min(rw, rh)*mpp
cE, cN = px2utm(x0+rcx, y0+rcy)
print(f'detector={method}  blob_area={len(xs)} px')
print(f'centroid texel ({x0+rcx:.1f},{y0+rcy:.1f}) -> object-grid UTM {cE:.1f},{cN:.1f}')
print(f'oriented bbox  L x W = {L:.1f} x {W:.1f} m   long-edge azimuth = {az:.1f} deg')
json.dump({'E': cE, 'N': cN, 'az': az, 'L': L, 'W': W, 'col': col, 'row': row},
          open('.sandbox/hangar_detect.json', 'w'), indent=2)

# overlay
Z = 11
img = tex.crop((x0, y0, x1, y1)).resize(((x1-x0)*Z, (y1-y0)*Z), Image.LANCZOS)
dr = ImageDraw.Draw(img, 'RGBA')
# mask outline (yellow) by drawing blob pixels faint
m = np.zeros((y1-y0, x1-x0), bool); m[ys, xs] = True
edge = m & ~ndimage.binary_erosion(m)
ey, ex = np.where(edge)
for px, py in zip(ex, ey):
    dr.rectangle([px*Z, py*Z, px*Z+Z, py*Z+Z], fill=(255, 255, 0, 90))
dr.line([tuple(p*Z) for p in np.vstack([boxp, boxp[0]])], fill=(255, 0, 0, 255), width=3)
dr.ellipse([rcx*Z-5, rcy*Z-5, rcx*Z+5, rcy*Z+5], fill=(255, 0, 255))
dr.text((6, 6), f'{method}: az={az:.1f}deg  {L:.0f}x{W:.0f}m  (red=fit, yellow=roof mask)', fill=(255, 255, 0))
img.save('.sandbox/hangar_detect.png')
print('wrote .sandbox/hangar_detect.png + hangar_detect.json')
