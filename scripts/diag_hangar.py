#!/usr/bin/env python3
"""Deterministic, on-disk diagnostic for the Stenkovec hangar placement.

Renders (no GUI):
  1. the hangar .c3d top-down silhouette (North up, East right), coloured by
     height so the low 'summer house' annex is distinguishable from the tall
     hangar shed -> tells us the side it is on and whether the model is mirrored.
  2. the INSTALLED t0704 patch texture, with a marker at the pixel where the
     current .obj record actually lands -> tells us the position drift vs the
     painted building.

Everything is computed through condor_grid's authoritative transform so the two
images share one coordinate system. Outputs to .sandbox/.
"""
import os, sys, math, struct
sys.path.insert(0, 'scripts')
import condor_grid as G
import c3d as C3
import numpy as np
from PIL import Image, ImageDraw

INSTALL = 'C:/Condor2/Landscapes/MacedoniaSkopje/'
OUT = '.sandbox/'
os.makedirs(OUT, exist_ok=True)

# ---- 1. read the current hangar .obj record --------------------------------
objp = INSTALL + 'MacedoniaSkopje.obj'
data = open(objp, 'rb').read()
assert len(data) % 152 == 0, f'.obj size {len(data)} not a multiple of 152'
recs = []
for off in range(0, len(data), 152):
    posX, posY, posZ, scale, ori = struct.unpack_from('<5f', data, off)
    nl = data[off + 20]
    name = data[off + 21:off + 21 + nl].decode('latin1')
    recs.append(dict(posX=posX, posY=posY, posZ=posZ, scale=scale, ori=ori, name=name))
print(f'.obj has {len(recs)} record(s):')
for r in recs:
    E, N = G.obj_world_xy(r['posX'], r['posY'])
    print(f"  {r['name']:24} posX={r['posX']:.1f} posY={r['posY']:.1f} "
          f"scale={r['scale']:.3f} ori={math.degrees(r['ori']):.1f}deg  -> UTM {E:.1f},{N:.1f}")

hangar = next(r for r in recs if 'Hangar' in r['name'] or 'HANGAR' in r['name'].upper())
hE, hN = G.obj_world_xy(hangar['posX'], hangar['posY'])

# ---- which patch + pixel does it land in? (OBJECT-anchor grid) -------------
# patch bounds derived from the OBJECT anchor (the grid Condor actually places
# objects on), NOT BR_EASTING/BR_NORTHING (the 30 m DEM grid 45 m away).
col = round((G.OBJ_ANCHOR_E - hE) / G.PATCH_SIZE_M - 0.5)
row = round((hN - G.OBJ_ANCHOR_N) / G.PATCH_SIZE_M - 0.5)
e_max = G.OBJ_ANCHOR_E - col * G.PATCH_SIZE_M
e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + row * G.PATCH_SIZE_M
n_max = n_min + G.PATCH_SIZE_M
obj_bounds = (e_min, n_min, e_max, n_max)
print(f'\nhangar UTM {hE:.1f},{hN:.1f} -> patch col={col} row={row} (t{col:02d}{row:02d})')
print(f'  OBJECT-grid patch bounds E[{e_min:.0f},{e_max:.0f}] N[{n_min:.0f},{n_max:.0f}]')

def utm_px(e, n, bounds, size=2048):
    mi_e, mi_n, ma_e, ma_n = bounds
    px = (e - mi_e) / (ma_e - mi_e) * size
    py = (ma_n - n) / (ma_n - mi_n) * size
    return px, py

px_obj, py_obj = utm_px(hE, hN, obj_bounds)
print(f'  -> object lands at t-texel ({px_obj:.0f},{py_obj:.0f}) on the OBJECT grid')

# also where the TEXTURE-grid (patch_bounds_utm) would put the same UTM, to
# quantify the texture-vs-object drift on this very patch.
tb = G.patch_bounds_utm(col, row)
px_tex, py_tex = utm_px(hE, hN, tb)
print(f'  -> same UTM on the TEXTURE/DEM grid = texel ({px_tex:.0f},{py_tex:.0f}); '
      f'drift = ({px_obj-px_tex:.0f},{py_obj-py_tex:.0f}) texels '
      f'= ({(px_obj-px_tex)*G.PATCH_SIZE_M/2048:.0f},{(py_obj-py_tex)*G.PATCH_SIZE_M/2048:.0f}) m')

# ---- 2. render the hangar .c3d top-down ------------------------------------
c3p = INSTALL + 'World/Objects/' + hangar['name']
mesh = C3.parse_c3d(c3p)
allxy = []
print(f'\n.c3d {hangar["name"]}: {len(mesh.objects)} object(s)')
for ob in mesh.objects:
    xs = [v.px for v in ob.vertices]; ys = [v.py for v in ob.vertices]; zs = [v.pz for v in ob.vertices]
    allxy += list(zip(xs, ys, zs))
    print(f'  {ob.name:18} verts={len(ob.vertices):5} tris={len(ob.indices)//3:5} '
          f'tex={ob.texture[-28:]!r:30} X[{min(xs):.1f},{max(xs):.1f}] Y[{min(ys):.1f},{max(ys):.1f}] Z[{min(zs):.1f},{max(zs):.1f}]')

xy = np.array([(x, y) for (x, y, z) in allxy])
zz = np.array([z for (x, y, z) in allxy])
cx, cy = xy[:, 0].mean(), xy[:, 1].mean()
# PCA principal (long) axis of the footprint, as a compass azimuth (cw from N)
d = xy - [cx, cy]
cov = d.T @ d
evals, evecs = np.linalg.eigh(cov)
major = evecs[:, np.argmax(evals)]            # (dx_E, dy_N)
az = (math.degrees(math.atan2(major[0], major[1]))) % 180.0
print(f'\nfootprint centroid(local)=({cx:.2f},{cy:.2f})  span '
      f'X={xy[:,0].max()-xy[:,0].min():.1f}m Y={xy[:,1].max()-xy[:,1].min():.1f}m  '
      f'PCA long-axis azimuth={az:.1f}deg (model-local, before ori)')

# top-down raster: North up. colour by height (low annex = blue, tall = red).
S = 700; pad = 6.0
xmin, xmax = xy[:, 0].min() - pad, xy[:, 0].max() + pad
ymin, ymax = xy[:, 1].min() - pad, xy[:, 1].max() + pad
sc = (S - 20) / max(xmax - xmin, ymax - ymin)
img = Image.new('RGB', (S, S), (245, 245, 245)); dr = ImageDraw.Draw(img, 'RGBA')
def L(x, y):
    return (10 + (x - xmin) * sc, 10 + (ymax - y) * sc)   # North up
zmin, zmax = zz.min(), zz.max()
for ob in mesh.objects:
    vs = ob.vertices
    for t in range(0, len(ob.indices), 3):
        a, b, c = ob.indices[t], ob.indices[t+1], ob.indices[t+2]
        tri = [vs[a], vs[b], vs[c]]
        zmean = sum(v.pz for v in tri) / 3
        f = (zmean - zmin) / (zmax - zmin + 1e-6)
        col_ = (int(40 + 215*f), int(80), int(220 - 180*f), 90)   # blue(low)->red(high)
        dr.polygon([L(v.px, v.py) for v in tri], fill=col_)
# centroid + North arrow + summer-house hint
dr.line([L(cx, cy), L(cx, cy + (ymax-ymin)*0.4)], fill=(0, 150, 0), width=3)  # +N
dr.ellipse([L(cx, cy)[0]-4, L(cx, cy)[1]-4, L(cx, cy)[0]+4, L(cx, cy)[1]+4], fill=(0, 0, 0))
dr.text((12, 12), 'c3d TOP-DOWN  North up, East right (green=+N)', fill=(0, 0, 0))
dr.text((12, 26), f'blue=low roof (annex)  red=tall (hangar)  PCA={az:.0f}deg', fill=(0, 0, 0))
img.save(OUT + 'hangar_topdown.png')
print(f'wrote {OUT}hangar_topdown.png')

# ---- 3. installed t0704 texture with the landing marker --------------------
texp = None
for cand in (f'Textures/t{col:02d}{row:02d}.dds', f'Textures/t{col:02d}{row:02d}.bmp'):
    if os.path.exists(INSTALL + cand):
        texp = INSTALL + cand; break
print(f'\ntexture: {texp}')
if texp:
    try:
        tex = Image.open(texp).convert('RGB')
    except Exception as e:
        print('  PIL could not open DDS:', e)
        tex = None
    if tex is not None:
        tex2 = tex.resize((1024, 1024))
        d2 = ImageDraw.Draw(tex2)
        mx, my = px_obj/2, py_obj/2
        d2.ellipse([mx-10, my-10, mx+10, my+10], outline=(255, 0, 0), width=3)
        d2.line([mx-16, my, mx+16, my], fill=(255, 0, 0), width=2)
        d2.line([mx, my-16, mx, my+16], fill=(255, 0, 0), width=2)
        d2.text((10, 10), f't{col:02d}{row:02d}  red=where .obj lands', fill=(255, 255, 0))
        tex2.save(OUT + 'hangar_tex_full.png')
        # tight crop around the landing point
        cxp, cyp = int(px_obj), int(py_obj)
        crop = tex.crop((max(0, cxp-300), max(0, cyp-300), min(2048, cxp+300), min(2048, cyp+300)))
        crop = crop.resize((600, 600))
        dc = ImageDraw.Draw(crop)
        dc.line([300-20, 300, 300+20, 300], fill=(255, 0, 0), width=2)
        dc.line([300, 300-20, 300, 300+20], fill=(255, 0, 0), width=2)
        crop.save(OUT + 'hangar_tex_crop.png')
        print(f'wrote {OUT}hangar_tex_full.png and hangar_tex_crop.png')
