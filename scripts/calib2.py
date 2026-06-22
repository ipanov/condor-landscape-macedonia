#!/usr/bin/env python3
"""Find the texel shift that snaps OSM footprints onto the painted roofs at
Stenkovec. Draws each OSM polygon UNSHIFTED (thin red) and SHIFTED by (DX,DY)
texels (thick cyan/orange). Whichever lands on the painted building reveals the
object<->texture grid offset to apply to placement. Usage: calib2.py [DX DY]
"""
import sys, math, json
sys.path.insert(0, 'scripts')
import condor_grid as G
import pyproj
from shapely.geometry import shape
from shapely.ops import transform as shp_transform
from PIL import Image, ImageDraw

DX = float(sys.argv[1]) if len(sys.argv) > 1 else -16.0
DY = float(sys.argv[2]) if len(sys.argv) > 2 else -16.0
INSTALL = 'C:/Condor2/Landscapes/MacedoniaSkopje/'
CE, CN = 531845.3, 4656470.4
to_utm = pyproj.Transformer.from_crs(G.WGS84_CRS, G.UTM_CRS, always_xy=True).transform
near = []
for f in json.load(open('.sandbox/osm/buildings.geojson', encoding='utf-8'))['features']:
    g = shp_transform(to_utm, shape(f['geometry']))
    if g.geom_type in ('Polygon', 'MultiPolygon') and math.hypot(g.centroid.x-CE, g.centroid.y-CN) <= 340:
        near.append((g, f.get('properties', {})))

col, row = 7, 4
e_max = G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M
def tpx(e, n): return ((e-e_min)/(e_max-e_min)*2048, (n_max-n)/(n_max-n_min)*2048)
hx, hy = tpx(CE, CN)
cxs, cys = hx+DX, hy+DY                     # predicted painted-hangar texel
tex = Image.open(INSTALL+'Textures/t0704.dds').convert('RGB')
half = 55; Z = 9
box = (int(cxs-half), int(cys-half), int(cxs+half), int(cys+half))
img = tex.crop(box).resize(((box[2]-box[0])*Z, (box[3]-box[1])*Z), Image.LANCZOS)
dr = ImageDraw.Draw(img, 'RGBA')
def toi(px, py): return ((px-box[0])*Z, (py-box[1])*Z)
for g, p in near:
    ish = p.get('aeroway') == 'hangar'
    polys = g.geoms if g.geom_type == 'MultiPolygon' else [g]
    for poly in polys:
        raw = [tpx(x, y) for x, y in poly.exterior.coords]
        dr.line([toi(px, py) for px, py in raw], fill=(255, 0, 0, 150), width=1)            # unshifted
        dr.line([toi(px+DX, py+DY) for px, py in raw],
                fill=(0, 255, 255, 255) if ish else (255, 160, 0, 230), width=3 if ish else 2)  # shifted
img.save('.sandbox/calib2.png')
print(f'wrote .sandbox/calib2.png  shift=({DX},{DY}) texel = ({DX*2.8125:.0f},{-DY*2.8125:.0f}) m (E,N)')
print('thin red=OSM as-is; thick cyan/orange=OSM shifted. Aligned thick outlines => that shift is the fix.')
