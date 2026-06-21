# Condor 2 `.c3d` 3D-object format

Reverse-engineered from the **Slovenia2** landscape and verified by **byte-exact
round-trip** of six reference files. Writer/reader: [`scripts/c3d.py`](../scripts/c3d.py).

A `.c3d` holds the renderable geometry for one airport:

| File | Purpose | Typical objects |
|------|---------|-----------------|
| `<Name>G.c3d` | the **ground / runway plane** | `Grass`, `Asphalt`, `GrassPaint`, `AsphaltPaint` |
| `<Name>O.c3d` | the scenery **objects** | hangars, cars, trees, `Windsack*` (windsock) |

> **Why this matters:** a malformed `.c3d` makes Condor 2 abort the flight with
> **"Airport is not installed"**. The `.c3d` name must match the `.apt` airport
> name exactly (`StenkovecG.c3d` / `StenkovecO.c3d` for an airport named
> `Stenkovec`). A virtual airport (no `.c3d`) is valid for airborne starts; a
> *copied* `.c3d` from another landscape is what causes the crash. Always
> generate via `scripts/c3d.py` and round-trip before installing.

## Endianness (mixed!)

- The **container header** integers are **big-endian**.
- **Everything else** — object header ints, the material floats, the whole
  geometry pool — is **little-endian**.

Verify exactly; do not assume one endianness throughout.

## Byte layout

```
magic        4 bytes      b'C3D\x01'
header       3 × int32 BE  (0, nObjects, nObjects)        # the two counts are equal

objects[nObjects], each record:
  nameLen    int32 BE
  name       ASCII[nameLen]                               # 'Grass', 'Windsack1', ...
  vertOfs    int32 LE      first vertex index in the shared pool
  vertCnt    int32 LE      vertices owned by this object
  idxOfs     int32 LE      first element index in the shared pool
  idxCnt     int32 LE      indices owned by this object
  texLen     uint8
  texture    ASCII[texLen]                                # '' => flat-shaded by colour
  material   6 × float32 LE   (R, G, B, A, p4, p5)        # white = 1,1,1,1,1,1
  sep        uint8 0x00       present for objects 0..n-2, ABSENT for the last object

geometry pool (immediately after the last object record):
  zero       int32 LE = 0
  totVerts   int32 LE                                     # == Σ vertCnt
  vertices   totVerts × (8 × float32 LE):
                px, py, pz       position, metres   (X = east, Y = north, Z = up)
                nx, ny, nz       unit normal
                u,  v            texture coordinates
  totIdx     int32 LE                                     # == Σ idxCnt
  indices    totIdx × int32 LE                            # triangle list, GLOBAL pool indices
```

### Key invariants (all verified)

- `nObjects` appears **twice** in the header and the two values are equal.
- `Σ vertCnt == totVerts` and `Σ idxCnt == totIdx`.
- The first pool int32 is **0**, then the vertex count. (Read as
  `int32 zero, int32 totVerts`.)
- Indices are **global** pool indices: an object's local triangle index `t`
  resolves to pool index `vertOfs + t`. They span exactly `0 .. totVerts-1`
  across the file.
- The file ends **exactly** at the last index (no trailer). This makes the pool
  unambiguous to locate by scanning for its self-describing length, which
  `scripts/c3d.py` does — decoupling pool decode from the object header region.

### Vertex semantics

8 float32 = **position(3) + normal(3) + UV(2)**, stride 32 bytes. Confirmed by:
all 554 normals in `POSTOJNAG.c3d` are exactly unit length, and the UV pair lies
in/around `[0,1]`. Ground planes sit at `Z = 0`; the asphalt strip sits a hair
above the grass to avoid z-fighting.

### Winding

**Counter-clockwise (CCW) is the front face.** Verified on `POSTOJNAG`'s flat
`Asphalt` object: the geometric triangle normal agrees with the stored vertex
normal (and points +Z up) for all 31 triangles.

### Pool order ≠ object order

The pool is **not** required to be laid out in object-declaration order. In
`NOVO MESTOO.c3d` the object order is `Cobra, Mercedes, …, WindsockS2` while the
pool order (by `vertOfs`) is `WindsockS2, Windsack2, WindsockS1, …`. The reader
captures each object's `vertOfs`/`idxOfs` so the writer can reproduce the exact
pool byte order; freshly-built objects are packed in object order (which the
simpler files use).

### Material / coordinate notes

- `material[0:4]` is RGBA, used as a flat colour when `texture == ''` (e.g. the
  orange windsock flag `Windsack1` has zero normals + zero UVs and is coloured
  purely by `material = (0.788, 0.812, 0.690, 1.0, …)`). `material[4:6]` are two
  further render parameters; white objects use all-ones.
- The windsock flag is **double-sided**: 3 vertices, 6 indices
  (`[0,2,1, 2,0,1]`).
- Coordinates are local to the airport reference point. Real-world orientation
  of the strip is carried by the `.apt` runway heading, so the G-file mesh is a
  local plane (the references store arbitrarily-rotated local planes; our
  generated Stenkovec strip is the canonical north-aligned form).

## Known non-canonical variant (`POSTOJNAO.c3d`)

A few hand-built objects (e.g. `Car_01`) carry **one extra float32 (`0.001`)
after the 6 material floats and no `0x00` separator**, and the object that
follows is embedded back-to-back. This appears to be an older/extended material
encoding. `scripts/c3d.py` **tolerates a trailing extra float on read**
(`C3DObject.extra`) for the simple 4-byte-gap case, but `POSTOJNAO.c3d` chains
this in a way the current reader rejects with a clear error. It is **not**
required for landscape generation: every `*G.c3d` and the windsock objects use
the canonical form, which round-trips byte-identically. If a future task needs
to read `POSTOJNAO`-class files, extend the separator handling in `parse_c3d`.

## Verified reference files

| File | Objects | Verts | Round-trip |
|------|--------:|------:|------------|
| `POSTOJNAG.c3d`  | 3  | 554  | byte-identical |
| `NOVO MESTOG.c3d`| 3  | 649  | byte-identical |
| `CELJEG.c3d`     | 4  | 1361 | byte-identical |
| `PTUJG.c3d`      | 4  | 639  | byte-identical |
| `NOVO MESTOO.c3d`| 10 | 6351 | byte-identical (out-of-order pool) |
| `POSTOJNAO.c3d`  | 16 | 8544 | not supported (documented variant) |

Reproduce: `python scripts/c3d.py`

## Generating objects

`scripts/c3d.py` exposes builders that emit canonical geometry:

- `make_quad(name, half_x, half_y, texture=, material=, z=, uv_scale=, double_sided=)`
  — flat ground rectangle, CCW (+Z up).
- `make_triangle(name, p0, p1, p2, …, double_sided=True)` — windsock-flag style
  triangle (flat-shaded, zero normals/UVs), matching `Windsack1`.
- `make_box(name, sx, sy, z0, z1, …)` — axis-aligned box with outward normals
  (e.g. a windsock pole).

See [`scripts/generate_stenkovec_c3d.py`](../scripts/generate_stenkovec_c3d.py)
for a complete `G` + `O` example written to `.sandbox/airports_test/`.
