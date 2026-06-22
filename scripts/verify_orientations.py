#!/usr/bin/env python3
"""Decisive, non-guessing orientation check: render the model footprint at the 4
axis-quadrants over the installed texture, with the model's OWN front (Hangar_FRONT)
and clubroom marked, so the correct mount is PICKED by alignment + the door-faces-
open-space rule -- not hand-tuned. scale=1.0 (real model size, no shrink).
"""
import os, sys, math, json
sys.path.insert(0, 'scripts')
import condor_grid as G
import c3d as C3
import footprint_registration as FR
import pyproj, numpy as np
from shapely.geometry import shape, MultiPoint
from shapely.ops import transform as shp_transform
from PIL import Image, ImageDraw

INSTALL = 'C:/Condor2/Landscapes/MacedoniaSkopje/'
NAME = 'StenkovecHangar.c3d'
SCALE = float(os.environ.get('SCALE', 1.0))
to_utm = pyproj.Transformer.from_crs(G.WGS84_CRS, G.UTM_CRS, always_xy=True).transform

poly = None
for f in json.load(open('.sandbox/osm/buildings.geojson', encoding='utf-8'))['features']:
    if f.get('properties', {}).get('aeroway') == 'hangar':
        g = shp_transform(to_utm, shape(f['geometry']))
        if math.hypot(g.centroid.x-531842, g.centroid.y-4656466) < 200:
            poly = g; break
bE0, bN0 = poly.centroid.x, poly.centroid.y

mesh = C3.parse_c3d(INSTALL + 'World/Objects/' + NAME)
base = [(v.px, v.py) for ob in mesh.objects for v in ob.vertices if v.pz < 0.6]
front = [(v.px, v.py) for ob in mesh.objects if ob.name == 'Hangar_FRONT' for v in ob.vertices]
annex = [(v.px, v.py) for ob in mesh.objects
         if ob.name.split('_')[-1] in ('CHAIR', 'TABLE', 'CHALKBOARD') for v in ob.vertices]
mcx, mcy = np.mean([p[0] for p in base]), np.mean([p[1] for p in base])
fcx, fcy = np.mean([p[0] for p in front]), np.mean([p[1] for p in front])
front_local_az = math.degrees(math.atan2(fcx-mcx, fcy-mcy)) % 360
print(f"model FRONT (Hangar_FRONT) local bearing = {front_local_az:.0f}deg from centroid")

reg = FR.register(list(MultiPoint(base).convex_hull.exterior.coords), poly)
ori0 = math.degrees(reg['ori_rad']) % 360.0

# grid correction
col, row = 7, 4
De_min, Dn_min, De_max, Dn_max = G.patch_bounds_utm(col, row)
Oe_min = (G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M) - G.PATCH_SIZE_M
On_max = (G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M) + G.PATCH_SIZE_M
tE, tN = bE0 + (Oe_min-De_min), bN0 + (On_max-Dn_max)
e_max = G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M
def tpx(e, n): return ((e-e_min)/(e_max-e_min)*2048, (n_max-n)/(n_max-n_min)*2048)
tex = Image.open(INSTALL + 'Textures/t0704.dds').convert('RGB')
ccx, ccy = tpx(tE, tN); half, Z = 30, 11
box = (int(ccx-half), int(ccy-half), int(ccx+half), int(ccy+half))
W = (box[2]-box[0])*Z

def render(ori_deg):
    ori = math.radians(ori_deg); co, so = math.cos(ori), math.sin(ori)
    def place(lx, ly):
        ax, ay = (lx-mcx)*SCALE, (ly-mcy)*SCALE
        return tE + ax*co + ay*so, tN - ax*so + ay*co
    img = tex.crop(box).resize((W, W), Image.LANCZOS); dr = ImageDraw.Draw(img, 'RGBA')
    def toi(e, n):
        px, py = tpx(e, n); return ((px-box[0])*Z, (py-box[1])*Z)
    dr.line([toi(*place(x, y)) for x, y in MultiPoint(base).convex_hull.exterior.coords], fill=(255, 40, 40, 255), width=3)
    fh = MultiPoint([place(x, y) for x, y in front]).convex_hull
    if fh.geom_type == 'Polygon':
        dr.line([toi(x, y) for x, y in fh.exterior.coords], fill=(0, 160, 255, 255), width=4)   # FRONT/doors = blue
    if annex:
        ah = MultiPoint([place(x, y) for x, y in annex]).convex_hull
        if ah.geom_type == 'Polygon':
            dr.line([toi(x, y) for x, y in ah.exterior.coords], fill=(0, 255, 0, 255), width=3)
    fwaz = (ori_deg + front_local_az) % 360
    dr.text((6, 6), f'ori={ori_deg:.0f}  front faces {fwaz:.0f}deg', fill=(255, 255, 0))
    return img

montage = Image.new('RGB', (W*2+6, W*2+6), (20, 20, 20))
for k in range(4):
    im = render((ori0 + k*90) % 360)
    montage.paste(im, ((k % 2)*(W+6), (k//2)*(W+6)))
montage.save('.sandbox/orient_quadrants.png')
print(f"saved .sandbox/orient_quadrants.png  (TL=ori0 TR=+90 BL=+180 BR=+270; ori0={ori0:.0f})")
print("blue=FRONT/doors, green=clubroom, red=footprint. Pick the tile whose red box")
print("matches the painted roof AND whose blue front faces the open airfield.")
