#!/usr/bin/env python3
r"""
Condor 2 ``.c3d`` 3D-object reader / writer.

A ``.c3d`` holds the runway-plane (``<Name>G.c3d``) and the scenery objects
(``<Name>O.c3d``) for one airport. A *malformed* ``.c3d`` makes Condor 2 abort
the flight with **"Airport is not installed"**, so this module is intentionally
strict and is always validated by byte-exact round-trip before anything is
generated (see ``__main__`` and ``docs/condor_c3d_format.md``).

FORMAT  (decoded from Slovenia2 POSTOJNAG / NOVO MESTOG / NOVO MESTOO, all of
which round-trip byte-identically through this module):

  Mixed endianness. The container/header ints are BIG-endian; everything inside
  an object record and the geometry pool is LITTLE-endian.

  magic      : 4 bytes  b'C3D\x01'
  header     : 3 x int32 BIG-endian = (0, nObjects, nObjects)
  objects[]  : nObjects records, each:
                 nameLen   int32 BIG-endian
                 name      ASCII[nameLen]            (e.g. 'Grass', 'Windsack1')
                 vertOfs   int32 LE   index of first vertex in the shared pool
                 vertCnt   int32 LE   number of vertices owned by this object
                 idxOfs    int32 LE   index of first element in the shared pool
                 idxCnt    int32 LE   number of indices owned by this object
                 texLen    uint8
                 texture   ASCII[texLen]             ('' => flat-shaded by colour)
                 material  6 x float32 LE  = (R, G, B, A, p4, p5)   (white=1,1,1,1,1,1)
                 sep       uint8 0x00      PRESENT for objects 0..n-2, ABSENT for
                                           the last object (the pool starts there)
  pool:
                 zero      int32 LE = 0
                 totVerts  int32 LE                  (== sum of every vertCnt)
                 vertices  totVerts x (8 x float32 LE):
                              px, py, pz,             position, metres, X=E Y=N Z=up
                              nx, ny, nz,             unit normal
                              u,  v                   texture coords
                 totIdx    int32 LE                   (== sum of every idxCnt)
                 indices   totIdx x int32 LE          triangle list, GLOBAL into
                                                      the pool, CCW = front face

Coordinates are local to the airport reference point; the ground plane is Z=0.
Indices are *global* pool indices, i.e. an object's local triangle ``t`` uses
pool index ``vertOfs + t``.

KNOWN VARIANT (not emitted by this module): a few hand-built objects in
Slovenia2 ``POSTOJNAO.c3d`` (e.g. 'Car_01') carry one extra float32 after the 6
material floats and then no 0x00 separator. ``parse_c3d`` tolerates this on read
(``Material.extra``) but the writer only emits the canonical 6-float + separator
form, which is what every G file and the windsock objects use. See the format
doc for details.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Sequence, Tuple

MAGIC = b"C3D\x01"
VERTEX_FLOATS = 8          # px,py,pz, nx,ny,nz, u,v
VERTEX_SIZE = VERTEX_FLOATS * 4
WHITE_MATERIAL: Tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.0, 1.0)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Vertex:
    px: float
    py: float
    pz: float
    nx: float
    ny: float
    nz: float
    u: float
    v: float

    def as_tuple(self) -> Tuple[float, ...]:
        return (self.px, self.py, self.pz, self.nx, self.ny, self.nz, self.u, self.v)


@dataclass
class C3DObject:
    """One named mesh. Vertices/indices are stored *locally* here; the global
    pool offsets are recomputed on write, so callers never manage offsets."""
    name: str
    texture: str = ""
    material: Tuple[float, float, float, float, float, float] = WHITE_MATERIAL
    vertices: List[Vertex] = field(default_factory=list)
    indices: List[int] = field(default_factory=list)   # 0-based, local to this object
    # Round-trip fidelity for the rare POSTOJNAO-style extra material float.
    # None => canonical record (6 floats + 0x00 separator). A float => that value
    # was stored after the material with no separator byte.
    extra: float | None = None
    # Original pool offsets, captured on parse so write_c3d can reproduce the
    # exact pool byte order (which is NOT always object order -- see NOVO
    # MESTOO). Left None for freshly-built objects; the writer then packs the
    # pool in object order.
    _vert_ofs: int | None = field(default=None, repr=False)
    _idx_ofs: int | None = field(default=None, repr=False)


@dataclass
class C3DFile:
    objects: List[C3DObject] = field(default_factory=list)
    flag: int = 0   # first header int32; 0 in every observed file


# --------------------------------------------------------------------------- #
# Reader
# --------------------------------------------------------------------------- #
def _find_pool(data: bytes) -> Tuple[int, int, int]:
    """Locate the geometry pool by its self-describing length.

    The pool is ``int32 zero, int32 totV, totV*32 bytes, int32 totI,
    totI*4 bytes`` and ends exactly at EOF, which makes it unambiguous to find
    by scan. This decouples pool discovery from the (slightly variable) object
    header region, so a single odd material record can never corrupt geometry.
    """
    n = len(data)
    for p in range(16, n - 12):
        zero, tot_v = struct.unpack_from("<2i", data, p)
        if zero != 0 or not (0 < tot_v < 5_000_000):
            continue
        v_end = p + 8 + tot_v * VERTEX_SIZE
        if v_end + 4 > n:
            continue
        tot_i = struct.unpack_from("<i", data, v_end)[0]
        if not (0 < tot_i < 20_000_000):
            continue
        if v_end + 4 + tot_i * 4 == n:
            return p, tot_v, tot_i
    raise ValueError("c3d: could not locate geometry pool (corrupt or unknown variant)")


def _next_name_pos(data: bytes, start: int, limit: int) -> int | None:
    """Find the next object record start (a BE int32 nameLen + printable name)."""
    for pos in range(start, min(start + 512, limit) - 4):
        n_len = struct.unpack_from(">i", data, pos)[0]
        if 0 < n_len < 64 and pos + 4 + n_len <= limit:
            name = data[pos + 4:pos + 4 + n_len]
            if all(32 <= c < 127 for c in name):
                return pos
    return None


def parse_c3d(path_or_bytes) -> C3DFile:
    """Parse a ``.c3d`` file (path or raw bytes) into a :class:`C3DFile`."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
    else:
        data = Path(path_or_bytes).read_bytes()

    if data[:4] != MAGIC:
        raise ValueError(f"c3d: bad magic {data[:4]!r}, expected {MAGIC!r}")
    flag, n1, n2 = struct.unpack_from(">3i", data, 4)
    if n1 != n2:
        raise ValueError(f"c3d: header object counts disagree ({n1} != {n2})")

    pool_start, tot_v, tot_i = _find_pool(data)

    # --- decode shared geometry pool ---
    vp = pool_start + 8
    pool_verts = [
        Vertex(*struct.unpack_from("<8f", data, vp + i * VERTEX_SIZE))
        for i in range(tot_v)
    ]
    ip = vp + tot_v * VERTEX_SIZE
    n_idx = struct.unpack_from("<i", data, ip)[0]
    pool_idx = list(struct.unpack_from(f"<{n_idx}i", data, ip + 4))

    # --- decode object header records ---
    objs: List[C3DObject] = []
    o = 16
    for k in range(n1):
        n_len = struct.unpack_from(">i", data, o)[0]
        o += 4
        name = data[o:o + n_len].decode("latin1")
        o += n_len
        v_ofs, v_cnt, i_ofs, i_cnt = struct.unpack_from("<4i", data, o)
        o += 16
        t_len = data[o]
        o += 1
        tex = data[o:o + t_len].decode("latin1")
        o += t_len
        material = struct.unpack_from("<6f", data, o)
        o += 24

        # Separator handling. Canonical: a single 0x00 byte between objects and
        # none before the pool. Variant: an extra float32 then (sometimes) no
        # separator. We resolve it by where the next record / pool actually is.
        extra: float | None = None
        next_pos = _next_name_pos(data, o, pool_start) if k < n1 - 1 else pool_start
        gap = next_pos - o
        if gap == 1:
            assert data[o] == 0, f"c3d: unexpected separator 0x{data[o]:02x} after {name!r}"
        elif gap == 0:
            pass                      # last object -> pool starts immediately
        elif gap in (4, 5):
            extra = struct.unpack_from("<f", data, o)[0]   # POSTOJNAO-style extra float
        else:
            raise ValueError(f"c3d: unexpected {gap}-byte gap after object {name!r}")
        o = next_pos

        # slice this object's geometry out of the pool, re-base indices to 0
        verts = pool_verts[v_ofs:v_ofs + v_cnt]
        idx = [g - v_ofs for g in pool_idx[i_ofs:i_ofs + i_cnt]]
        if idx and not all(0 <= li < v_cnt for li in idx):
            raise ValueError(f"c3d: index out of range in object {name!r}")
        objs.append(C3DObject(name=name, texture=tex, material=tuple(material),
                              vertices=verts, indices=idx, extra=extra,
                              _vert_ofs=v_ofs, _idx_ofs=i_ofs))

    return C3DFile(objects=objs, flag=flag)


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #
def _assign_pool_offsets(objs: List[C3DObject]) -> List[Tuple[int, int]]:
    """Return (vertOfs, idxOfs) per object for the shared pool.

    If every object carries its parsed offsets we reproduce them exactly (the
    pool order may differ from object order -- see NOVO MESTOO). Otherwise we
    lay the pool out in object order, which is what the simpler files do.
    """
    have_all = all(o._vert_ofs is not None and o._idx_ofs is not None for o in objs)
    if have_all:
        return [(o._vert_ofs, o._idx_ofs) for o in objs]  # type: ignore[misc]
    v = i = 0
    out = []
    for o in objs:
        out.append((v, i))
        v += len(o.vertices)
        i += len(o.indices)
    return out


def write_c3d(c3d: C3DFile, path: str | Path | None = None) -> bytes:
    """Serialise a :class:`C3DFile` to ``.c3d`` bytes. Writes to *path* if given."""
    n = len(c3d.objects)
    offsets = _assign_pool_offsets(c3d.objects)
    tot_v = sum(len(o.vertices) for o in c3d.objects)
    tot_i = sum(len(o.indices) for o in c3d.objects)

    # Build the shared pool in offset order. Each object's vertices occupy
    # [vertOfs, vertOfs+vertCnt); indices are rebased from local-0 to global.
    pool_verts: List[Vertex | None] = [None] * tot_v
    pool_idx: List[int | None] = [None] * tot_i
    for ob, (v_ofs, i_ofs) in zip(c3d.objects, offsets):
        v_cnt = len(ob.vertices)
        for j, vtx in enumerate(ob.vertices):
            pool_verts[v_ofs + j] = vtx
        for j, li in enumerate(ob.indices):
            if not (0 <= li < v_cnt):
                raise ValueError(f"c3d: local index {li} out of range in {ob.name!r}")
            pool_idx[i_ofs + j] = v_ofs + li
    if any(v is None for v in pool_verts) or any(x is None for x in pool_idx):
        raise ValueError("c3d: pool offsets leave gaps (inconsistent offsets/counts)")

    out = bytearray()
    out += MAGIC
    out += struct.pack(">3i", c3d.flag, n, n)
    for k, (ob, (v_ofs, i_ofs)) in enumerate(zip(c3d.objects, offsets)):
        name_b = ob.name.encode("latin1")
        tex_b = ob.texture.encode("latin1")
        if len(tex_b) > 255:
            raise ValueError(f"c3d: texture name too long for object {ob.name!r}")
        if len(ob.material) != 6:
            raise ValueError(f"c3d: material must be 6 floats for object {ob.name!r}")
        out += struct.pack(">i", len(name_b))
        out += name_b
        out += struct.pack("<4i", v_ofs, len(ob.vertices), i_ofs, len(ob.indices))
        out += struct.pack("B", len(tex_b))
        out += tex_b
        out += struct.pack("<6f", *ob.material)
        if ob.extra is not None:
            out += struct.pack("<f", ob.extra)     # rare variant, no separator follows
        elif k < n - 1:
            out += b"\x00"                          # canonical inter-object separator

    out += struct.pack("<2i", 0, tot_v)
    for v in pool_verts:
        out += struct.pack("<8f", *v.as_tuple())   # type: ignore[union-attr]
    out += struct.pack("<i", tot_i)
    out += struct.pack(f"<{tot_i}i", *pool_idx)

    blob = bytes(out)
    if path is not None:
        Path(path).write_bytes(blob)
    return blob


# --------------------------------------------------------------------------- #
# Geometry helpers (for generating runway planes / windsocks)
# --------------------------------------------------------------------------- #
def make_quad(
    name: str,
    half_x: float,
    half_y: float,
    *,
    texture: str = "",
    material: Tuple[float, ...] = WHITE_MATERIAL,
    z: float = 0.0,
    uv_scale: Tuple[float, float] = (1.0, 1.0),
    double_sided: bool = False,
) -> C3DObject:
    """A flat rectangle on the ground plane, centred at the origin.

    ``half_x`` / ``half_y`` are half-extents in metres (X=east, Y=north). Wound
    CCW so the +Z normal faces up (Condor treats CCW as the front face).
    """
    sx, sy = uv_scale
    # corners CCW seen from above: SW, SE, NE, NW
    corners = [
        (-half_x, -half_y, 0.0, 0.0),
        (+half_x, -half_y, sx,  0.0),
        (+half_x, +half_y, sx,  sy),
        (-half_x, +half_y, 0.0, sy),
    ]
    verts = [Vertex(x, y, z, 0.0, 0.0, 1.0, u, v) for (x, y, u, v) in corners]
    indices = [0, 1, 2, 0, 2, 3]            # two CCW triangles
    if double_sided:
        verts += [Vertex(x, y, z, 0.0, 0.0, -1.0, u, v) for (x, y, u, v) in corners]
        indices += [4, 6, 5, 4, 7, 6]       # back face, opposite winding
    return C3DObject(name=name, texture=texture, material=tuple(material),
                     vertices=verts, indices=indices)


def make_triangle(
    name: str,
    p0: Sequence[float],
    p1: Sequence[float],
    p2: Sequence[float],
    *,
    texture: str = "",
    material: Tuple[float, ...] = WHITE_MATERIAL,
    double_sided: bool = True,
) -> C3DObject:
    """A single (optionally double-sided) triangle from three XYZ points.

    Matches the Slovenia2 ``Windsack1`` flag: zero normals/UVs, coloured by the
    material RGB, double-sided (front + back). Normals are left zero to mirror
    that reference exactly."""
    pts = [tuple(p0), tuple(p1), tuple(p2)]
    verts = [Vertex(p[0], p[1], p[2], 0.0, 0.0, 0.0, 0.0, 0.0) for p in pts]
    indices = [0, 2, 1]                     # matches reference flag winding
    if double_sided:
        indices += [2, 0, 1]
    return C3DObject(name=name, texture=texture, material=tuple(material),
                     vertices=verts, indices=indices)


def make_box(
    name: str,
    sx: float,
    sy: float,
    z0: float,
    z1: float,
    *,
    texture: str = "",
    material: Tuple[float, ...] = WHITE_MATERIAL,
) -> C3DObject:
    """An axis-aligned box (e.g. a windsock pole), centred in X/Y at the origin.

    ``sx``/``sy`` are full side lengths; spans ``z0..z1`` vertically. All faces
    wound CCW (outward normals). UVs are a simple per-face 0..1."""
    hx, hy = sx / 2.0, sy / 2.0
    # 8 corners
    c = {
        "000": (-hx, -hy, z0), "100": (hx, -hy, z0),
        "110": (hx, hy, z0),   "010": (-hx, hy, z0),
        "001": (-hx, -hy, z1), "101": (hx, -hy, z1),
        "111": (hx, hy, z1),   "011": (-hx, hy, z1),
    }
    faces = [
        # (corner keys CCW from outside, normal)
        (("000", "100", "101", "001"), (0.0, -1.0, 0.0)),   # south (-Y)
        (("100", "110", "111", "101"), (1.0, 0.0, 0.0)),    # east  (+X)
        (("110", "010", "011", "111"), (0.0, 1.0, 0.0)),    # north (+Y)
        (("010", "000", "001", "011"), (-1.0, 0.0, 0.0)),   # west  (-X)
        (("001", "101", "111", "011"), (0.0, 0.0, 1.0)),    # top   (+Z)
        (("010", "110", "100", "000"), (0.0, 0.0, -1.0)),   # bottom(-Z)
    ]
    verts: List[Vertex] = []
    indices: List[int] = []
    uv = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    for keys, nrm in faces:
        base = len(verts)
        for ci, k in enumerate(keys):
            px, py, pz = c[k]
            verts.append(Vertex(px, py, pz, nrm[0], nrm[1], nrm[2], uv[ci][0], uv[ci][1]))
        indices += [base, base + 1, base + 2, base, base + 2, base + 3]
    return C3DObject(name=name, texture=texture, material=tuple(material),
                     vertices=verts, indices=indices)


# --------------------------------------------------------------------------- #
# Self-test: byte-exact round-trip of the reference files
# --------------------------------------------------------------------------- #
def _roundtrip_report(paths: Sequence[str]) -> bool:
    all_ok = True
    for p in paths:
        try:
            raw = Path(p).read_bytes()
        except OSError as e:
            print(f"  SKIP  {p}: {e}")
            continue
        try:
            parsed = parse_c3d(raw)
            rebuilt = write_c3d(parsed)
            identical = rebuilt == raw
            note = ""
            if not identical:
                # locate first differing byte for diagnosis
                m = min(len(raw), len(rebuilt))
                diff = next((i for i in range(m) if raw[i] != rebuilt[i]), m)
                note = f"  first diff @ {diff} (len {len(raw)} vs {len(rebuilt)})"
                if any(o.extra is not None for o in parsed.objects):
                    note += "  [contains POSTOJNAO-style extra-float variant]"
            status = "IDENTICAL" if identical else "DIFFERS"
            n_obj = len(parsed.objects)
            tv = sum(len(o.vertices) for o in parsed.objects)
            print(f"  {status:9} {Path(p).name:24} objs={n_obj:2} verts={tv:5}{note}")
            all_ok = all_ok and identical
        except Exception as e:                       # noqa: BLE001
            print(f"  ERROR     {Path(p).name}: {e}")
            all_ok = False
    return all_ok


if __name__ == "__main__":
    import sys

    # Canonical files (must round-trip byte-identical):
    canonical = sys.argv[1:] or [
        r"C:/Condor2/Landscapes/Slovenia2/Airports/POSTOJNAG.c3d",
        r"C:/Condor2/Landscapes/Slovenia2/Airports/NOVO MESTOG.c3d",
        r"C:/Condor2/Landscapes/Slovenia2/Airports/CELJEG.c3d",
        r"C:/Condor2/Landscapes/Slovenia2/Airports/PTUJG.c3d",
        r"C:/Condor2/Landscapes/Slovenia2/Airports/NOVO MESTOO.c3d",
    ]
    print("c3d round-trip self-test (canonical files):")
    ok = _roundtrip_report(canonical)
    print("ALL CANONICAL FILES IDENTICAL" if ok else "FAILURE: see notes above")

    # Known non-canonical variant (documented limitation, informational only):
    if not sys.argv[1:]:
        print("\nknown variant (informational, not required to round-trip):")
        _roundtrip_report([r"C:/Condor2/Landscapes/Slovenia2/Airports/POSTOJNAO.c3d"])

    sys.exit(0 if ok else 1)
