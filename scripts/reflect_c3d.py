#!/usr/bin/env python3
"""Reflect a .c3d across its local X (East) axis to fix a baked-in mirror
(summer-house-on-wrong-side). Negates px+nx and reverses triangle winding so
front faces stay outward (CCW). Backs up, round-trip verifies, and renders a
before/after top-down so the chirality flip is visible. Usage: reflect_c3d.py <c3d>
"""
import sys, shutil, math
sys.path.insert(0, 'scripts')
import c3d as C3
import numpy as np
from PIL import Image, ImageDraw

p = sys.argv[1] if len(sys.argv) > 1 else \
    'C:/Condor2/Landscapes/MacedoniaSkopje/World/Objects/StenkovecHangar.c3d'

def topdown(mesh, path, title):
    base = [(v.px, v.py, v.pz) for ob in mesh.objects for v in ob.vertices]
    annex = {id(v) for ob in mesh.objects if ob.name.split('_')[-1] in
             ('CHAIR', 'TABLE', 'CHALKBOARD', 'FRONT')
             for v in ob.vertices}
    xy = np.array([(x, y) for x, y, z in base])
    xmin, xmax, ymin, ymax = xy[:, 0].min()-4, xy[:, 0].max()+4, xy[:, 1].min()-4, xy[:, 1].max()+4
    S = 520; sc = (S-20)/max(xmax-xmin, ymax-ymin)
    img = Image.new('RGB', (S, S), (245, 245, 245)); dr = ImageDraw.Draw(img, 'RGBA')
    def L(x, y): return (10+(x-xmin)*sc, 10+(ymax-y)*sc)
    for ob in mesh.objects:
        isannex = ob.name.split('_')[-1] in ('CHAIR', 'TABLE', 'CHALKBOARD', 'FRONT')
        for t in range(0, len(ob.indices), 3):
            tri = [ob.vertices[ob.indices[t+k]] for k in range(3)]
            fill = (0, 200, 0, 120) if isannex else (60, 90, 200, 70)
            dr.polygon([L(v.px, v.py) for v in tri], fill=fill)
    cx, cy = xy[:, 0].mean(), xy[:, 1].mean()
    dr.line([L(cx, cy), L(cx, cy+(ymax-ymin)*0.42)], fill=(0, 150, 0), width=3)  # +N
    dr.text((12, 12), title, fill=(0, 0, 0))
    dr.text((12, 26), 'green=annex/summer-house  blue=hangar  green line=North', fill=(0, 0, 0))
    img.save(path); print('  wrote', path)

m = C3.parse_c3d(p)
nv = sum(len(o.vertices) for o in m.objects)
print(f'reflecting {p}  ({nv} verts, {len(m.objects)} objs)')
topdown(m, '.sandbox/hangar_before_mirror.png', 'BEFORE (as installed)')

shutil.copy(p, p + '.bak_premirror')
for ob in m.objects:
    for v in ob.vertices:
        v.px = -v.px; v.nx = -v.nx
    ix = ob.indices
    for t in range(0, len(ix) - 2, 3):
        ix[t+1], ix[t+2] = ix[t+2], ix[t+1]
C3.write_c3d(m, p)

m2 = C3.parse_c3d(p)
nv2 = sum(len(o.vertices) for o in m2.objects)
assert nv2 == nv, f'vertex count changed {nv}->{nv2}'
print(f'  OK reflected + winding-reversed, round-trip verts={nv2}, backup .bak_premirror')
topdown(m2, '.sandbox/hangar_after_mirror.png', 'AFTER (reflected across East)')
