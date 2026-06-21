"""
obj_to_c3d.py — convert a triangulated, UV'd OBJ + a DDS into a single-object
Condor .c3d (one g-group, one DDS), then byte-exact round-trip verify.

This is the canonical, repo-tracked copy of the per-object OBJ->C3D converter that
``scripts/batch_migrate.py`` imports as a library. It is the same converter first
proven in ``.sandbox/landmarks/_work/obj_to_c3d.py`` (kept there unchanged); the
only difference is the sys.path insert below points at the sibling ``scripts/``
folder so ``c3d`` resolves wherever this file is invoked from.

Condor target: one C3DObject carrying the whole model, texture = a Condor-relative
DDS path, material = (1,1,1,1, p4,p5). UVs from the OBJ are used directly.
Coord mapping: OBJ is exported Y-forward / Z-up (glb_to_baked), and Condor C3D is
X=East Y=North Z=up — so OBJ (x,y,z) maps straight to C3D (px,py,pz).

NOTE on V flip: OBJ/Blender UV origin is bottom-left; the reference Slovenia2 DDS
objects use the same 0..1 convention straight from their modeller, so we keep V
as-is. (If a texture looks vertically mirrored in-sim, flip to 1-v here.)
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from c3d import C3DFile, C3DObject, Vertex, parse_c3d, write_c3d


def load_obj(path):
    """Return (positions[list of (x,y,z)], uvs[list of (u,v)], normals, faces).
    faces is a list of triangles; each is 3 tuples (vi, ti, ni) 0-based (or None)."""
    P, T, N, F = [], [], [], []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("v "):
                _, x, y, z = line.split()[:4]
                P.append((float(x), float(y), float(z)))
            elif line.startswith("vt "):
                p = line.split()
                T.append((float(p[1]), float(p[2])))
            elif line.startswith("vn "):
                _, x, y, z = line.split()[:4]
                N.append((float(x), float(y), float(z)))
            elif line.startswith("f "):
                toks = line.split()[1:]
                tri = []
                for t in toks:
                    a = (t.split("/") + ["", "", ""])[:3]
                    vi = int(a[0]) - 1 if a[0] else None
                    ti = int(a[1]) - 1 if a[1] else None
                    ni = int(a[2]) - 1 if a[2] else None
                    tri.append((vi, ti, ni))
                # fan-triangulate (already triangulated, but be safe)
                for k in range(1, len(tri) - 1):
                    F.append((tri[0], tri[k], tri[k + 1]))
    return P, T, N, F


def build_c3d_object(name, obj_path, texture_path, material=(1.0, 1.0, 1.0, 1.0, 0.0, 0.9),
                     flip_v=False):
    """Build one C3DObject. We expand to per-corner unique vertices (pos+uv+normal),
    which matches how the reference objects store geometry (a vertex carries its own
    uv/normal, so shared positions with different uvs become distinct vertices)."""
    P, T, N, F = load_obj(obj_path)
    verts = []
    indices = []
    cache = {}
    for tri in F:
        for (vi, ti, ni) in tri:
            px, py, pz = P[vi]
            if ti is not None and ti < len(T):
                u, v = T[ti]
            else:
                u, v = 0.0, 0.0
            if flip_v:
                v = 1.0 - v
            if ni is not None and ni < len(N):
                nx, ny, nz = N[ni]
            else:
                nx, ny, nz = 0.0, 0.0, 1.0
            key = (vi, round(u, 6), round(v, 6), round(nx, 4), round(ny, 4), round(nz, 4))
            idx = cache.get(key)
            if idx is None:
                idx = len(verts)
                cache[key] = idx
                verts.append(Vertex(px, py, pz, nx, ny, nz, u, v))
            indices.append(idx)
    obj = C3DObject(name=name, texture=texture_path, material=tuple(material),
                    vertices=verts, indices=indices)
    return obj


def write_single(name, obj_path, texture_path, out_c3d, material=(1.0,1.0,1.0,1.0,0.0,0.9),
                 flip_v=False):
    obj = build_c3d_object(name, obj_path, texture_path, material, flip_v)
    f = C3DFile(objects=[obj], flag=0)
    blob = write_c3d(f, out_c3d)
    # round-trip verify
    reparsed = parse_c3d(blob)
    reblob = write_c3d(reparsed)
    ok = reblob == blob
    o0 = reparsed.objects[0]
    ntri = len(o0.indices) // 3
    print(f"  C3D '{name}': verts={len(obj.vertices)} tris={ntri} tex='{texture_path}' "
          f"bytes={len(blob)} roundtrip={'IDENTICAL' if ok else 'DIFFERS'}")
    if not ok:
        m = min(len(blob), len(reblob))
        d = next((i for i in range(m) if blob[i] != reblob[i]), m)
        raise SystemExit(f"  ROUND-TRIP FAILED first diff @ {d} ({len(blob)} vs {len(reblob)})")
    return len(obj.vertices), ntri


if __name__ == "__main__":
    # CLI: name obj tex out [flip_v]
    a = sys.argv[1:]
    name, obj_path, tex, out = a[0], a[1], a[2], a[3]
    flip = len(a) > 4 and a[4].lower() in ("1", "true", "flip")
    write_single(name, obj_path, tex, out, flip_v=flip)
