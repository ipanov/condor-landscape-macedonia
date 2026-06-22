#!/usr/bin/env python3
"""Generic, robust object placement for Condor 2 (custom .c3d or autogen).

Pipeline (the professional method):
  1. target footprint polygon  (MS Footprints / OSM aeroway=hangar / cadastre)
  2. model base outline (.c3d)  -> footprint_registration.register() => ori, scale, centroid
  3. front/rear via a DOMAIN RULE measured from data (hangar doors face the apron/
     runway; the clubroom 'annex' faces away) -- breaks the near-square symmetry
  4. texture<->object GRID CORRECTION (textures on DEM grid, objects on .trn grid)
  5. write the .obj record + overlay-verify on the INSTALLED DDS before trusting it.

CLI:  python scripts/place_object.py [--commit]
"""
import os, sys, math, json, struct, shutil
sys.path.insert(0, 'scripts')
import condor_grid as G
import c3d as C3
import footprint_registration as FR
import pyproj, numpy as np
from shapely.geometry import shape, MultiPoint
from shapely.ops import transform as shp_transform
from PIL import Image, ImageDraw

INSTALL = 'C:/Condor2/Landscapes/MacedoniaSkopje/'
NAME = os.environ.get('OBJ', 'StenkovecHangar.c3d')
COMMIT = '--commit' in sys.argv[1:]
to_utm = pyproj.Transformer.from_crs(G.WGS84_CRS, G.UTM_CRS, always_xy=True).transform

# ---- 1. target footprint: OSM aeroway=hangar (authoritative + named) --------
poly = None
for f in json.load(open('.sandbox/osm/buildings.geojson', encoding='utf-8'))['features']:
    if f.get('properties', {}).get('aeroway') == 'hangar':
        g = shp_transform(to_utm, shape(f['geometry']))
        if math.hypot(g.centroid.x-531842, g.centroid.y-4656466) < 200:
            poly = g; break
assert poly is not None, 'hangar polygon not found'
bE0, bN0 = poly.centroid.x, poly.centroid.y

# ---- 2. model base outline + annex (clubroom) direction --------------------
mesh = C3.parse_c3d(INSTALL + 'World/Objects/' + NAME)
base = [(v.px, v.py) for ob in mesh.objects for v in ob.vertices if v.pz < 0.6]
outline = list(MultiPoint(base).convex_hull.exterior.coords)
mcx, mcy = np.mean([p[0] for p in base]), np.mean([p[1] for p in base])
annex = [(v.px, v.py) for ob in mesh.objects
         if ob.name.split('_')[-1] in ('CHAIR', 'TABLE', 'CHALKBOARD', 'INTERIOR_2')
         for v in ob.vertices]
aax, aay = np.mean([p[0] for p in annex]), np.mean([p[1] for p in annex])
annex_local_az = math.degrees(math.atan2(aax-mcx, aay-mcy)) % 360.0   # local bearing of clubroom

# ---- 3. registration (position + scale + axis) -----------------------------
reg = FR.register(outline, poly, face_azimuth=None)
ori0 = math.degrees(reg['ori_rad']) % 360.0
scale = reg['scale'] * 0.85 / max(reg['scale'], 1e-6) if False else reg['scale']
print(f"register: ori={ori0:.1f} scale={reg['scale']:.3f} iou={reg['iou']:.2f} "
      f"flip={reg['flip']} rms={reg['rms_m']:.2f} m")

# ---- 3b. DOMAIN RULE: clubroom faces AWAY from the runway ------------------
# runway centroid from cached OSM runways -> bearing hangar->runway = apron side.
run = json.load(open('.sandbox/osm/runways.geojson', encoding='utf-8'))
rc = None
for f in run['features']:
    g = shp_transform(to_utm, shape(f['geometry']))
    d = math.hypot(g.centroid.x-bE0, g.centroid.y-bN0)
    if d < 2000 and (rc is None or d < rc[0]):
        rc = (d, g.centroid.x, g.centroid.y)
apron_dir = math.degrees(math.atan2(rc[1]-bE0, rc[2]-bN0)) % 360.0 if rc else 30.0
annex_target = (apron_dir + 180.0) % 360.0                      # clubroom opposite the apron
def angdiff(a, b): return abs((a-b+180) % 360 - 180)
cands = [(ori0 + k*90.0) % 360.0 for k in range(4)]
ori = min(cands, key=lambda o: angdiff((o + annex_local_az) % 360.0, annex_target))
ori = (ori + float(os.environ.get('ROT', 0))) % 360.0   # manual 90deg-quadrant override
print(f"apron bearing {apron_dir:.0f}deg -> clubroom should face {annex_target:.0f}deg; "
      f"chose ori={ori:.1f} (annex faces {(ori+annex_local_az)%360:.0f})  [ROT={os.environ.get('ROT',0)}]")
ori_rad = math.radians(ori); co, so = math.cos(ori_rad), math.sin(ori_rad)

# ---- 4. grid correction (texture DEM grid -> object .trn grid) -------------
col, row = 7, 4
De_min, Dn_min, De_max, Dn_max = G.patch_bounds_utm(col, row)
Oe_min = (G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M) - G.PATCH_SIZE_M
On_max = (G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M) + G.PATCH_SIZE_M
tE, tN = bE0 + (Oe_min-De_min), bN0 + (On_max-Dn_max)
tE += float(os.environ.get('NUDGEE', 0)); tN += float(os.environ.get('NUDGEN', 0))

def place(lx, ly):
    ax, ay = (lx-mcx)*scale, (ly-mcy)*scale
    return tE + ax*co + ay*so, tN - ax*so + ay*co
refE = tE + (-mcx*scale)*co + (-mcy*scale)*so
refN = tN - (-mcx*scale)*so + (-mcy*scale)*co
posX, posY = G.obj_record_xy(refE, refN)

# ---- 5. overlay verify on installed DDS ------------------------------------
e_max = G.OBJ_ANCHOR_E - col*G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
n_min = G.OBJ_ANCHOR_N + row*G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M
def tpx(e, n): return ((e-e_min)/(e_max-e_min)*2048, (n_max-n)/(n_max-n_min)*2048)
tex = Image.open(INSTALL + 'Textures/t0704.dds').convert('RGB')
ccx, ccy = tpx(tE, tN); half = int(os.environ.get('HALF', 26)); Z = int(os.environ.get('ZOOM', 14))
box = (int(ccx-half), int(ccy-half), int(ccx+half), int(ccy+half))
img = tex.crop(box).resize(((box[2]-box[0])*Z, (box[3]-box[1])*Z), Image.LANCZOS)
dr = ImageDraw.Draw(img, 'RGBA')
def toi(e, n):
    px, py = tpx(e, n); return ((px-box[0])*Z, (py-box[1])*Z)
hull = MultiPoint([place(x, y) for x, y in base]).convex_hull
dr.line([toi(x, y) for x, y in hull.exterior.coords], fill=(255, 40, 40, 255), width=3)
ah = MultiPoint([place(x, y) for x, y in annex]).convex_hull
if ah.geom_type == 'Polygon':
    dr.line([toi(x, y) for x, y in ah.exterior.coords], fill=(0, 255, 0, 255), width=3)
dr.line([toi(x, y) for x, y in poly.exterior.coords], fill=(0, 255, 255, 200), width=1)
mx, my = toi(tE, tN); dr.ellipse([mx-4, my-4, mx+4, my+4], fill=(255, 0, 255))
dr.text((6, 6), f'red=model green=clubroom cyan=OSM  ori={ori:.0f} scale={scale:.2f} iou={reg["iou"]:.2f}', fill=(255, 255, 0))
img.save('.sandbox/place_object_overlay.png')
print(f'.obj posX={posX:.1f} posY={posY:.1f} ori={ori:.1f} scale={scale:.2f}  -> .sandbox/place_object_overlay.png')

if COMMIT:
    objp = INSTALL + 'MacedoniaSkopje.obj'
    old = open(objp, 'rb').read(); posZ = struct.unpack_from('<5f', old, 0)[2] if len(old) >= 20 else 319.0
    nm = NAME.encode('latin1')
    rec = (struct.pack('<5f', posX, posY, posZ, scale, ori_rad) + bytes([len(nm)]) + nm).ljust(152, b'\x00')
    shutil.copy(objp, objp + '.bak_placeobj'); open(objp, 'wb').write(rec)
    print(f'COMMITTED .obj  posZ={posZ:.1f}  backup .bak_placeobj')
