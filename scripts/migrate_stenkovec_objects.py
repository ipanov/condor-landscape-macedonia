#!/usr/bin/env python3
r"""
Migrate the SoFly *LWXX Skopje Airfield Collection* (MSFS2020) Stenkovec models
into staged, TEXTURED Condor 2 ``.c3d`` objects.

Source (verified on disk):
  F:/FS2020/Official/OneStore/sofly-lwxx-airfields/
    SimObjects/Landmarks/stenkovec-hangar/model/LW75_Main_Hangar.gltf (+ .bin)
        -> the real Aeroklub Skopje glider hangar, 141 meshes / ~66 k verts,
           12 materials over 8 albedo DDS textures. Standard glTF 2.0, plain
           float32 POSITION; NORMAL = signed-byte/127 (VEC4), TEXCOORD_0 =
           signed-short/1024 (Asobo ASOBO_asset_optimized v4.4, verified
           empirically: max raw 15360/1024 == 15.0 clean UV).
    scenery/Stenkovec/LW75/...  -> the rest of the airfield (restaurant, fences,
           guardhouse, LW67 hangar). GEOMETRY IS ENCRYPTED inside
           scenery/lixmycig.fsarchive (magic 'RASA', high-entropy) and is NOT
           extractable -- only its textures sit unpacked on disk. Reported, not
           converted.

This script converts ONLY the open glTF (the hangar). It:
  * walks the glTF scene graph, baking each node's T*R*S into world space,
  * decodes attributes honouring the Asobo packing,
  * maps glTF (X=right, Y=up, Z=back/RH) -> Condor (X=East, Y=North, Z=up),
  * groups primitives by albedo image -> one C3DObject per texture (lossless;
    keeps the original per-material UVs, no atlas rebake),
  * recentres on the footprint centroid (origin) with base z = 0,
  * converts each referenced MSFS DXT DDS -> a Condor DXT1 DDS via nvcompress,
  * writes <name>.c3d into .sandbox/airport_objects/ and round-trips it.

SANDBOX ONLY -- does not touch the install, the .apt, or the .obj.
"""
from __future__ import annotations
import json, math, struct, subprocess, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3d  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SRC = Path("F:/FS2020/Official/OneStore/sofly-lwxx-airfields")
HANGAR_DIR = SRC / "SimObjects/Landmarks/stenkovec-hangar"
GLTF = HANGAR_DIR / "model/LW75_Main_Hangar.gltf"
TEXDIR = HANGAR_DIR / "texture"
OUT = REPO / ".sandbox/airport_objects"
WORK = OUT / "_work"
PNGDIR = WORK / "tex_png"
DDSDIR = OUT          # Condor DDS sit next to the .c3d so the engine resolves them
NVCOMPRESS = Path("C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe")

for d in (OUT, WORK, PNGDIR, DDSDIR):
    d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# glTF accessor decoding
# --------------------------------------------------------------------------- #
_CT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
       5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def load_gltf(path: Path):
    d = json.loads(path.read_text())
    buf = (path.parent / d["buffers"][0]["uri"]).read_bytes()
    return d, buf


def read_accessor(d, buf, ai: int) -> np.ndarray:
    a = d["accessors"][ai]
    bv = d["bufferViews"][a["bufferView"]]
    off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    n = a["count"]
    nc = _NCOMP[a["type"]]
    fmt, sz = _CT[a["componentType"]]
    stride = bv.get("byteStride") or (sz * nc)
    out = np.empty((n, nc), dtype=np.float64)
    sfmt = "<%d%s" % (nc, fmt)
    for i in range(n):
        out[i] = struct.unpack_from(sfmt, buf, off + i * stride)
    return out


def node_matrix(node) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T  # column-major
    T = np.eye(4)
    if "translation" in node:
        T[:3, 3] = node["translation"]
    R = np.eye(4)
    if "rotation" in node:
        x, y, z, w = node["rotation"]
        R[:3, :3] = _quat_to_mat(x, y, z, w)
    S = np.eye(4)
    if "scale" in node:
        S[0, 0], S[1, 1], S[2, 2] = node["scale"]
    return T @ R @ S


def _quat_to_mat(x, y, z, w):
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ], dtype=np.float64)


# --------------------------------------------------------------------------- #
# material -> albedo image
# --------------------------------------------------------------------------- #
def material_albedo(d, mi: int):
    m = d["materials"][mi]
    pbr = m.get("pbrMetallicRoughness", {})
    bct = pbr.get("baseColorTexture")
    if bct is None:
        return None
    tex = d["textures"][bct["index"]]
    src = tex.get("source")
    ext = tex.get("extensions", {}).get("MSFT_texture_dds")
    if ext is not None:
        src = ext.get("source", src)
    if src is None:
        return None
    return d["images"][src]["uri"]


# --------------------------------------------------------------------------- #
# Asobo attribute unpacking
# --------------------------------------------------------------------------- #
UV_SCALE = 1024.0     # verified: max raw short 15360 / 1024 == 15.0
NORMAL_SCALE = 127.0  # signed byte normalize


def primitive_geometry(d, buf, prim):
    """Return (pos Nx3 float, nrm Nx3 float, uv Nx2 float, idx Mx1 int)."""
    pos = read_accessor(d, buf, prim["attributes"]["POSITION"])[:, :3]
    if "NORMAL" in prim["attributes"]:
        nrm = read_accessor(d, buf, prim["attributes"]["NORMAL"])[:, :3] / NORMAL_SCALE
    else:
        nrm = np.zeros_like(pos)
    if "TEXCOORD_0" in prim["attributes"]:
        uv = read_accessor(d, buf, prim["attributes"]["TEXCOORD_0"])[:, :2] / UV_SCALE
    else:
        uv = np.zeros((len(pos), 2))
    idx = read_accessor(d, buf, prim["indices"]).astype(np.int64).ravel()
    return pos, nrm, uv, idx


# --------------------------------------------------------------------------- #
# Texture conversion: MSFS .PNG.DDS (DXT1/3/5) -> PNG -> Condor DXT1 .dds
# --------------------------------------------------------------------------- #
def convert_texture(msfs_dds: Path, out_name: str, alpha: bool) -> str:
    """Decode an MSFS DDS, write a top-mip PNG, recompress to a Condor DDS.

    Returns the Condor DDS filename (no path). Uses DXT5 if alpha present,
    else DXT1. Caps the longest side at 2048 (Condor object-texture sanity)."""
    from PIL import Image
    png = PNGDIR / (out_name + ".png")
    dds = DDSDIR / (out_name + ".dds")
    if not dds.exists():
        im = Image.open(msfs_dds)
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        # cap size
        w, h = im.size
        m = max(w, h)
        if m > 2048:
            s = 2048.0 / m
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        if not alpha:
            im = im.convert("RGB")
        im.save(png)
        fmt = "-bc3" if alpha else "-bc1"   # bc3=DXT5, bc1=DXT1
        r = subprocess.run([str(NVCOMPRESS), fmt, "-silent", str(png), str(dds)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not dds.exists():
            raise RuntimeError(f"nvcompress failed for {out_name}: {r.stderr[-400:]}")
    return dds.name


def msfs_dds_has_alpha(msfs_dds: Path) -> bool:
    """DXT1 = opaque (no usable alpha); DXT3/DXT5 = alpha. Read fourCC at 0x54."""
    hdr = msfs_dds.read_bytes()[:96]
    fourcc = hdr[0x54:0x58]
    return fourcc in (b"DXT3", b"DXT5")


# --------------------------------------------------------------------------- #
# Main conversion
# --------------------------------------------------------------------------- #
def gltf_to_condor():
    d, buf = load_gltf(GLTF)
    nodes = d["nodes"]
    scene = d["scenes"][d.get("scene", 0)]

    # accumulate world matrices over the (flat, but handle children) graph
    world = {}

    def walk(ni, parent):
        M = parent @ node_matrix(nodes[ni])
        world[ni] = M
        for c in nodes[ni].get("children", []):
            walk(c, M)

    for root in scene["nodes"]:
        walk(root, np.eye(4))

    # collect geometry grouped by albedo image
    groups: dict[str, dict] = {}   # image_uri -> {pos,nrm,uv,idx}
    for ni, M in world.items():
        node = nodes[ni]
        if "mesh" not in node:
            continue
        N3 = M[:3, :3]
        Ninv_t = np.linalg.inv(N3).T
        for prim in d["meshes"][node["mesh"]]["primitives"]:
            img = material_albedo(d, prim["material"])
            key = img or "__untextured__"
            pos, nrm, uv, idx = primitive_geometry(d, buf, prim)
            # world transform
            ph = np.c_[pos, np.ones(len(pos))]
            posw = (M @ ph.T).T[:, :3]
            nrmw = (Ninv_t @ nrm.T).T
            ln = np.linalg.norm(nrmw, axis=1, keepdims=True)
            ln[ln == 0] = 1.0
            nrmw = nrmw / ln
            g = groups.setdefault(key, dict(pos=[], nrm=[], uv=[], idx=[], base=0))
            base = g["base"]
            g["pos"].append(posw)
            g["nrm"].append(nrmw)
            g["uv"].append(uv)
            g["idx"].append(idx + base)
            g["base"] = base + len(pos)

    # finalize arrays
    for k, g in groups.items():
        g["pos"] = np.concatenate(g["pos"])
        g["nrm"] = np.concatenate(g["nrm"])
        g["uv"] = np.concatenate(g["uv"])
        g["idx"] = np.concatenate(g["idx"])

    # global recentre: footprint centroid (XY) at origin, min-Z -> 0, in CONDOR axes.
    #
    # AXIS MAP -- HANDEDNESS-PRESERVING (det +1), the MSFS->ENU identity:
    #   Condor X (East)  = glTF X     (MSFS +X = East)
    #   Condor Y (North) = -glTF Z    (MSFS -Z = North)
    #   Condor Z (Up)    = glTF Y     (MSFS +Y = Up)
    # Both frames are right-handed, so this is a pure rotation (NO mirror): the model
    # enters Condor in its true authored real-world orientation. (Verified: the linear
    # part's determinant is +1; the placement `ori` then rotates the building's E-W
    # authored ridge onto the real N-S ridge -- see detect_place_stenkovec_hangar.py.)
    all_pos = np.concatenate([g["pos"] for g in groups.values()])
    cx = all_pos[:, 0]                 # Condor X = glTF X
    cy = -all_pos[:, 2]                # Condor Y = -glTF Z
    cz = all_pos[:, 1]                 # Condor Z = glTF Y
    # centre on XY mid of AABB, drop base to z=0
    ox = (cx.min() + cx.max()) / 2.0
    oy = (cy.min() + cy.max()) / 2.0
    oz = cz.min()

    objects = []
    tex_report = {}
    for img, g in groups.items():
        P = g["pos"]; Nn = g["nrm"]; UV = g["uv"]; IDX = g["idx"]
        vx = P[:, 0] - ox
        vy = -P[:, 2] - oy
        vz = P[:, 1] - oz
        nx = Nn[:, 0]
        ny = -Nn[:, 2]
        nz = Nn[:, 1]
        verts = [c3d.Vertex(float(vx[i]), float(vy[i]), float(vz[i]),
                            float(nx[i]), float(ny[i]), float(nz[i]),
                            float(UV[i, 0]), float(UV[i, 1])) for i in range(len(P))]
        # WINDING FIX. This Asobo/MSFS asset is authored CW-front (its triangle winding
        # is OPPOSITE to its vertex normals -- verified on the raw glTF: 100% of tris have
        # cross(v1-v0,v2-v0) . normal < 0). The det+1 axis map preserves that winding, so
        # a verbatim index copy yields CW-front geometry in Condor, whose convention is
        # CCW=front -> the whole textured hull renders INSIDE-OUT (back-faces), which in
        # sim reads as a wrong/"mirrored" building. We REVERSE each triangle (swap the 2nd
        # and 3rd index) so the emitted winding is CCW-front and agrees with the authored
        # outward normals. (This is the real cause of the reported mirror, not the axes.)
        idx_arr = np.asarray(IDX, dtype=np.int64).reshape(-1, 3)[:, ::-1].reshape(-1)
        # texture (convert the referenced MSFS DDS -> Condor DDS)
        if img and img != "__untextured__":
            msfs = TEXDIR / img
            stem = img.replace(".PNG.DDS", "").replace(".PNG.dds", "")
            alpha = msfs_dds_has_alpha(msfs)
            tex_name = convert_texture(msfs, "STK_" + stem, alpha)
            tex_report[img] = tex_name
        else:
            tex_name = ""
        obj = c3d.C3DObject(name="Hangar_" + (img.split("_ALBD")[0] if img else "Plain"),
                            texture=tex_name, material=c3d.WHITE_MATERIAL,
                            vertices=verts, indices=[int(i) for i in idx_arr])
        objects.append(obj)

    return objects, tex_report, dict(ox=ox, oy=oy, oz=oz,
                                     dx=float(cx.max() - cx.min()),
                                     dy=float(cy.max() - cy.min()),
                                     dz=float(cz.max() - cz.min()))


def main():
    objects, tex_report, info = gltf_to_condor()
    cf = c3d.C3DFile(objects=objects)
    out_path = OUT / "StenkovecHangar.c3d"
    blob = c3d.write_c3d(cf, out_path)

    # round-trip verify
    reparsed = c3d.parse_c3d(blob)
    rebuilt = c3d.write_c3d(reparsed)
    rt_ok = rebuilt == blob

    # winding verify: emitted triangles must be CCW-front == geometric normal AGREES
    # with the authored vertex normal (the fix above reversed the CW-front MSFS winding).
    agree = disagree = 0
    for o in objects:
        V = o.vertices
        for t in range(0, len(o.indices) - 2, 3):
            a, b, c = V[o.indices[t]], V[o.indices[t + 1]], V[o.indices[t + 2]]
            pa = np.array([a.px, a.py, a.pz]); pb = np.array([b.px, b.py, b.pz]); pc = np.array([c.px, c.py, c.pz])
            gn = np.cross(pb - pa, pc - pa)
            nl = np.linalg.norm(gn)
            if nl < 1e-9:
                continue
            gn /= nl
            vn = np.array([a.nx + b.nx + c.nx, a.ny + b.ny + c.ny, a.nz + b.nz + c.nz])
            vnl = np.linalg.norm(vn)
            if vnl < 1e-6:
                continue
            d = gn @ (vn / vnl)
            if d > 0.3:
                agree += 1
            elif d < -0.3:
                disagree += 1
    wind_ok = agree > 10 * max(disagree, 1)

    tot_v = sum(len(o.vertices) for o in objects)
    tot_t = sum(len(o.indices) for o in objects) // 3
    print("=== StenkovecHangar.c3d ===")
    print(f"  objects (one per albedo): {len(objects)}")
    print(f"  total vertices : {tot_v}")
    print(f"  total triangles: {tot_t}")
    print(f"  footprint extent (Condor m): dx={info['dx']:.1f}  dy={info['dy']:.1f}  height dz={info['dz']:.1f}")
    print(f"  file size: {len(blob)} bytes")
    print(f"  round-trip byte-identical: {rt_ok}")
    print(f"  winding CCW-front (normals agree): {wind_ok}  "
          f"(agree={agree}, disagree={disagree})")
    print(f"  textures converted: {len(tex_report)}")
    for src, dst in sorted(tex_report.items()):
        print(f"     {src}  ->  {dst}")
    if not wind_ok:
        raise SystemExit("WINDING CHECK FAILED: emitted faces are not CCW-front/outward")
    return out_path, objects, tex_report, info, rt_ok, tot_v, tot_t


if __name__ == "__main__":
    main()
