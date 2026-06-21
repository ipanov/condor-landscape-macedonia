# `.c3d` writer — validation report

Date: 2026-06-21 · Module: [`scripts/c3d.py`](../scripts/c3d.py) ·
Format spec: [`condor_c3d_format.md`](condor_c3d_format.md)

## Method

The `.c3d` format was decoded from the Slovenia2 landscape and the model proven
by **byte-exact round-trip**: parse a reference file → re-serialise → assert the
output equals the original bytes. Because Condor aborts ("Airport is not
installed") on a malformed `.c3d`, nothing is generated until the round-trip
passes, and the generated test files are themselves round-tripped after writing.
No files were written to `C:/Condor2`; no GUI was launched.

## 1. Round-trip of reference files

`python scripts/c3d.py`:

```
c3d round-trip self-test (canonical files):
  IDENTICAL POSTOJNAG.c3d            objs= 3 verts=  554
  IDENTICAL NOVO MESTOG.c3d          objs= 3 verts=  649
  IDENTICAL CELJEG.c3d               objs= 4 verts= 1361
  IDENTICAL PTUJG.c3d                objs= 4 verts=  639
  IDENTICAL NOVO MESTOO.c3d          objs=10 verts= 6351
ALL CANONICAL FILES IDENTICAL

known variant (informational, not required to round-trip):
  ERROR     POSTOJNAO.c3d: c3d: unexpected 100-byte gap after object 'Car_01'
```

**5 / 5 canonical files reproduce byte-for-byte**, covering:

- both required `*G.c3d` runway-plane files (POSTOJNA, NOVO MESTO),
- two more `G` files for breadth (CELJE, PTUJ, incl. 4-object files),
- the required `O` windsock file (NOVO MESTO O — `Windsack1/2`, `WindsockS1/2`),
  which additionally exercises the **out-of-order geometry pool** (pool order ≠
  object order).

### Documented equivalents / limitation

- The original task note read the first object's two zero int32s as padding;
  they are in fact `vertOfs = 0` and `idxOfs = 0` — the per-object fields are
  `(vertOfs, vertCnt, idxOfs, idxCnt)` into shared pools, **not** inline
  geometry. With that correction the layout is exact.
- The "post-material int32" in the original note is the **`0x00` inter-object
  separator** (1 byte), present between objects and absent before the pool.
- `POSTOJNAO.c3d` is a hand-built scenery file whose `Car_01` object uses an
  extended material encoding (an extra `0.001` float, no separator, with the
  next object embedded back-to-back). It is **not** needed for landscape
  generation and is intentionally rejected with a clear error rather than
  guessed at. Every runway-plane and windsock object uses the canonical form.

## 2. Generated test objects

`python scripts/generate_stenkovec_c3d.py` → `.sandbox/airports_test/`
(NOT installed):

```
  wrote StenkovecG.c3d  (455 bytes, 2 objs, 8 verts, 12 idx)  round-trip=OK
       - Grass          verts=  4 idx=  6 tex='Grass.bmp'   mat=(1,1,1,1,1,1)
       - Asphalt        verts=  4 idx=  6 tex='Asphalt.bmp' mat=(1,1,1,1,1,1)
  wrote StenkovecO.c3d  (1172 bytes, 2 objs, 27 verts, 42 idx)  round-trip=OK
       - WindsockPole   verts= 24 idx= 36 tex=''  mat=(0.6,0.6,0.6,1,1,1)
       - Windsack1      verts=  3 idx=  6 tex=''  mat=(0.85,0.45,0.1,1,0,1)
```

Independent geometry re-check (re-parsed from disk):

```
StenkovecG.c3d (455 bytes): magic OK, header BE=(0,2,2), n1==n2
  Grass    X[-25.0,25.0] Y[-600.0,600.0] Z[0.00,0.00] badIdx=0 normalsUnit=True tris=2
  Asphalt  X[ -2.0, 2.0] Y[-600.0,600.0] Z[0.01,0.01] badIdx=0 normalsUnit=True tris=2
  Grass quad = 50 x 1200 m (expect 50 x 1200) -> PASS

StenkovecO.c3d (1172 bytes): magic OK, header BE=(0,2,2), n1==n2
  WindsockPole X[39.8,40.2] Y[-0.2,0.2] Z[0.00,6.00] badIdx=0 normalsUnit=True tris=12
  Windsack1    X[40.0,42.5] Y[-0.7,0.7] Z[5.40,6.00] badIdx=0 normalsUnit=True tris=2
```

- `StenkovecG.c3d` — flat grass runway quad **1200 × 50 m**, centred at the
  origin on the ground plane (Z=0), white material, CCW (+Z up), plus a thin
  asphalt centre strip 0.01 m above to avoid z-fighting — same object structure
  as the Slovenia2 `*G.c3d` files (`Grass` / `Asphalt`).
- `StenkovecO.c3d` — a `Windsack1` double-sided orange flag triangle (flat-
  shaded, zero normals/UVs, exactly like the reference) on a 6 m `WindsockPole`
  box, placed 40 m east of the runway centreline.

All indices in range, all non-flag normals unit length, header counts consistent.

## 3. Determinism & isolation

- Re-running the generator produces **identical** bytes:
  `StenkovecG.c3d` md5 `a1bafa28a09ca602a59f64c099cf1680`,
  `StenkovecO.c3d` md5 `017f9fa0564896360650fab12200ed2a` (stable across runs).
- Confirmed **no** `Stenkovec*` files exist under
  `C:/Condor2/Landscapes/MacedoniaSkopje/Airports/` — the test objects live only
  in `.sandbox/airports_test/`.

## Status

`parse_c3d` / `write_c3d` are verified byte-exact on all canonical reference
files (G and O). Test `StenkovecG.c3d` / `StenkovecO.c3d` generated and
self-validated, not installed.

**Limit hit:** `POSTOJNAO.c3d`'s extended-material variant is not parsed (clear
error, documented). It is irrelevant to runway-plane / windsock generation; all
required object classes round-trip exactly.

**Not done here (out of scope of this task):** in-Condor visual confirmation
(requires launching Condor, which needs user approval per project rules) and
wiring these objects into a real Macedonia airport `.apt`. Real ground-start
runways would still come from Airport Maker → ObjectEditor per
`docs/condor_airport_workflow.md`; this module now makes a *programmatic* path
to canonical `.c3d` geometry available and proven.
