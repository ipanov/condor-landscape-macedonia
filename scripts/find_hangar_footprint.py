#!/usr/bin/env python3
"""Find the Stenkovec hangar's vector footprint and overlay candidates on the
installed t0704 texture, so placement uses an exact polygon (position +
orientation + size) and is verified against the painted ground truth.

Deterministic, on-disk, no GUI. Outputs .sandbox/hangar_candidates.png + a table.
"""
import os, sys, math, json
sys.path.insert(0, 'scripts')
import condor_grid as G
import pyproj
from shapely.geometry import shape
from shapely.ops import transform as shp_transform
from PIL import Image, ImageDraw

INSTALL = 'C:/Condor2/Landscapes/MacedoniaSkopje/'
# current placed hangar UTM (the .obj record) = search centre
CENTER_E, CENTER_N = 531842.2, 4656465.6
RADIUS = 450.0

to_utm = pyproj.Transformer.from_crs(G.WGS84_CRS, G.UTM_CRS, always_xy=True).transform

def load_utm(path):
    fc = json.load(open(path, encoding='utf-8'))
    out = []
    for f in fc.get('features', []):
        g = f.get('geometry')
        if not g:
            continue
        try:
            geom = shp_transform(to_utm, shape(g))
        except Exception:
            continue
        out.append((geom, f.get('properties', {})))
    return out

# ---- gather buildings near the strip ---------------------------------------
blds = load_utm('.sandbox/osm/buildings.geojson')
near = []
for geom, props in blds:
    if geom.is_empty:
        continue
    c = geom.centroid
    d = math.hypot(c.x - CENTER_E, c.y - CENTER_N)
    if d <= RADIUS and geom.geom_type in ('Polygon', 'MultiPolygon'):
        near.append((d, geom, props))
near.sort(key=lambda t: t[0])
print(f'{len(near)} buildings within {RADIUS:.0f} m of the placed hangar UTM\n')

def rect_axis(poly):
    mrr = poly.minimum_rotated_rectangle
    xs, ys = mrr.exterior.coords.xy
    def edge(i, j):
        dx, dy = xs[j]-xs[i], ys[j]-ys[i]
        return math.hypot(dx, dy), math.degrees(math.atan2(dx, dy)) % 180.0
    L1, a1 = edge(0, 1); L2, a2 = edge(1, 2)
    return (L1, L2, a1) if L1 >= L2 else (L2, L1, a2)

# ---- object-grid patch bounds for t0704 ------------------------------------
col, row = 7, 4
e_max = G.OBJ_ANCHOR_E - col * G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + row * G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M
def tpx(e, n):
    return ((e - e_min)/(e_max - e_min)*2048, (n_max - n)/(n_max - n_min)*2048)

tex = Image.open(INSTALL + 'Textures/t0704.dds').convert('RGB')
# crop window around the cluster, generous
cE, cN = CENTER_E - 120, CENTER_N + 40       # bias toward the buildings (west/north)
ccx, ccy = tpx(cE, cN)
half = 230
box = (int(ccx-half), int(ccy-half), int(ccx+half), int(ccy+half))
Z = 4
img = tex.crop(box).resize(((box[2]-box[0])*Z, (box[3]-box[1])*Z), Image.LANCZOS)
dr = ImageDraw.Draw(img, 'RGBA')
def to_img(e, n):
    px, py = tpx(e, n)
    return ((px - box[0])*Z, (py - box[1])*Z)

print(f"{'#':>2} {'dist':>5} {'area':>6} {'L x W (m)':>12} {'azim':>5}  tags")
cands = []
for i, (d, geom, props) in enumerate(near[:18]):
    polys = geom.geoms if geom.geom_type == 'MultiPolygon' else [geom]
    L, W, az = rect_axis(geom if geom.geom_type == 'Polygon' else max(polys, key=lambda p: p.area))
    cands.append((i, geom, props, L, W, az))
    tags = {k: props[k] for k in ('building', 'aeroway', 'name', 'man_made') if k in props}
    print(f'{i:>2} {d:5.0f} {geom.area:6.0f} {L:5.1f} x {W:4.1f} {az:5.0f}  {tags}')
    # draw outline + index
    for poly in polys:
        pts = [to_img(x, y) for x, y in poly.exterior.coords]
        col_ = (0, 255, 0, 255) if (props.get('aeroway') == 'hangar' or geom.area > 250) else (255, 180, 0, 220)
        dr.line(pts, fill=col_, width=2)
    c = geom.centroid
    ix, iy = to_img(c.x, c.y)
    dr.text((ix-3, iy-6), str(i), fill=(255, 0, 0))
# mark the current (wrong) placement
mx, my = to_img(CENTER_E, CENTER_N)
dr.line([mx-10, my, mx+10, my], fill=(255, 0, 255), width=2)
dr.line([mx, my-10, mx, my+10], fill=(255, 0, 255), width=2)
dr.text((mx+8, my+8), 'current .obj', fill=(255, 0, 255))
img.save('.sandbox/hangar_candidates.png')
print('\nwrote .sandbox/hangar_candidates.png  (green=large/hangar, orange=small, magenta=current placement)')
