#!/usr/bin/env python3
"""Calibrate the constant OSM->painted-texture offset at Stenkovec by overlaying
all nearby OSM building polygons (mapped through the OBJECT grid) on t0704.
The elongated apron buildings make the shift obvious; their common offset to the
painted roofs is the correction to apply to every object placement on this patch.
"""
import sys, math, json
sys.path.insert(0, 'scripts')
import condor_grid as G
import pyproj
from shapely.geometry import shape
from shapely.ops import transform as shp_transform
from PIL import Image, ImageDraw

INSTALL = 'C:/Condor2/Landscapes/MacedoniaSkopje/'
CE, CN = 531845.3, 4656470.4   # OSM hangar centroid
to_utm = pyproj.Transformer.from_crs(G.WGS84_CRS, G.UTM_CRS, always_xy=True).transform
near = []
for f in json.load(open('.sandbox/osm/buildings.geojson', encoding='utf-8'))['features']:
    g = shp_transform(to_utm, shape(f['geometry']))
    if g.geom_type not in ('Polygon', 'MultiPolygon'):
        continue
    if math.hypot(g.centroid.x-CE, g.centroid.y-CN) <= 380:
        near.append((g, f.get('properties', {})))

col, row = 7, 4
e_max = G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M
def tpx(e, n): return ((e-e_min)/(e_max-e_min)*2048, (n_max-n)/(n_max-n_min)*2048)
tex = Image.open(INSTALL+'Textures/t0704.dds').convert('RGB')
ccx, ccy = tpx(CE, CN); half = 120; Z = 6
box = (int(ccx-half), int(ccy-half), int(ccx+half), int(ccy+half))
img = tex.crop(box).resize(((box[2]-box[0])*Z, (box[3]-box[1])*Z), Image.LANCZOS)
dr = ImageDraw.Draw(img, 'RGBA')
def toi(e, n):
    px, py = tpx(e, n); return ((px-box[0])*Z, (py-box[1])*Z)
for i, (g, p) in enumerate(sorted(near, key=lambda t: -t[0].area)):
    polys = g.geoms if g.geom_type == 'MultiPolygon' else [g]
    ish = p.get('aeroway') == 'hangar'
    for poly in polys:
        dr.line([toi(x, y) for x, y in poly.exterior.coords],
                fill=(0, 255, 255, 255) if ish else (255, 120, 0, 230), width=2 if ish else 1)
    c = g.centroid; ix, iy = toi(c.x, c.y)
    dr.ellipse([ix-3, iy-3, ix+3, iy+3], fill=(255, 0, 255))
    dr.text((ix+3, iy-6), ('HANGAR' if ish else f'{g.area:.0f}'), fill=(255, 255, 0))
# texel ruler every 10 texels (28 m)
for gx in range(box[0]-(box[0] % 10), box[2], 10):
    X = (gx-box[0])*Z; dr.line([X, 0, X, img.height], fill=(255, 255, 255, 30))
for gy in range(box[1]-(box[1] % 10), box[3], 10):
    Y = (gy-box[1])*Z; dr.line([0, Y, img.width, Y], fill=(255, 255, 255, 30))
img.save('.sandbox/calib_offset.png')
print('wrote .sandbox/calib_offset.png  cyan=hangar orange=others magenta=OSM centroid; grid=10 texel=28 m')
print(f'window texels x[{box[0]},{box[2]}] y[{box[1]},{box[3]}]  hangar centroid texel ({ccx:.0f},{ccy:.0f})')
