# Building auto-placement (MacedoniaSkopje)

How real-world building footprints become a Condor 2 object-**placement** file
(`MacedoniaSkopje.obj`) that references a building model `.c3d`. This documents
the **placement** stage only; the model geometry (`.c3d`) is produced separately
by `scripts/c3d.py`.

Pipeline: `download_buildings.py` (footprints) -> **`place_buildings.py`** (this
doc) -> `c3d.py` (model) -> install + re-hash.

---

## 1. Pipeline

```
sources (Overture + Microsoft ML + OSM)
        │  scripts/download_buildings.py   (DONE — do not re-run; ~190k footprints)
        ▼
.sandbox/buildings/buildings_combined_utm.geojson   (67 MB, EPSG:32634)
        │  scripts/place_buildings.py
        ▼
.sandbox/buildings/MacedoniaSkopje.obj              (152-byte placement records)
.sandbox/buildings/building_stats.json              (histograms / distributions)
.sandbox/buildings/building_patch_groups.json       (per-patch merge plan, optional)
        │  scripts/c3d.py   (builds the building model(s) referenced by the .obj)
        ▼
World/Objects/building.c3d  (Phase A)  or  bldg_CCRR.c3d per patch (Phase B)
        │  install .obj + .c3d into C:/Condor2/Landscapes/MacedoniaSkopje/
        ▼
LandscapeEditor.exe -hash MacedoniaSkopje   (only if .tr3/.for changed; .obj/.c3d
                                             do not feed the terrain/forest hashes)
```

`place_buildings.py` is pure-Python (numpy + shapely), **downloads nothing**,
**opens no GUI**, and **writes only into `.sandbox/buildings/`** — never the
Condor install. Installation is a deliberate separate manual copy.

### What it computes per footprint
| Output | Source | Notes |
|--------|--------|-------|
| position | UTM **centroid** of the polygon | EPSG:32634, from the merged geojson |
| orientation | bearing of the **longest edge** (min-rotated-rect long axis), radians | normalised to `[0, π)` — a box footprint is symmetric under a half-turn |
| height | `building:levels`×3 m, else `height` tag, else **3 m** | only ~1% of footprints carry a height signal; the rest default to single-storey |
| footprint area | shapely polygon area (m²) | used for the significance ranking |
| patch | Condor `CCRR` (col 0 = EAST, row 0 = SOUTH) | for histograms + the per-patch merge plan |

### Clip + significance filter (order matters)
1. **Clip** to the landscape bbox by centroid: `E 506880–576000, N 4631040–4700160`.
2. **Significance filter first:** drop footprints `area < 40 m²` (sheds, garages,
   ML trace noise). Tunable with `--min-area`.
3. **Rank** survivors by `area` desc, then `height` desc, so a `--limit N` cut
   keeps the most visually prominent structures.

`posZ` (terrain altitude) is sampled from the canonical 30 m runway-flattened DEM
`sources/dem/macedonia_skopje_dem_30m_2305_flat.raw` — the same DEM the mesh and
forest use, so buildings sit on the rendered ground. (Condor also drapes objects
onto terrain at load, so an exact `posZ` is belt-and-braces, not strictly required.)

### Run
```bash
python scripts/place_buildings.py                 # full run (sample .obj + stats)
python scripts/place_buildings.py --emit-grouping # + per-patch merge plan json
python scripts/place_buildings.py --limit 20000   # keep top-N significant only
python scripts/place_buildings.py --min-area 60   # stricter cut
python scripts/place_buildings.py --c3d-name bldg.c3d --scale 1.0
```

---

## 2. The `.obj` placement binary (152-byte records)

A `<Name>.obj` is a **flat array of fixed 152-byte little-endian records, no file
header**. One record = one placed object. Verified against
`C:/Condor2/Landscapes/Slovenia2/Slovenia2.obj` (`1,139,392 / 152 = 7,496`
objects) and `flxhu/condor2` `condor_obj_file_tool.py`.

| Offset | Type | Field | Meaning |
|-------:|------|-------|---------|
| 0  | float32 | `posX`  | `origin_E - easting` |
| 4  | float32 | `posY`  | `northing - origin_N` |
| 8  | float32 | `posZ`  | terrain altitude, m ASL |
| 12 | float32 | `scale` | uniform scale (1.0 = model native size) |
| 16 | float32 | `ori`   | orientation, **radians** (clockwise from grid east/north convention; a box is half-turn symmetric so we emit `[0, π)`) |
| 20 | uint8   | `nameLen` | length of the model filename |
| 21 | char[131] | `name`  | `.c3d` filename, ASCII, null-padded |

`20 + 1 + 131 = 152`.

### Coordinate origin (critical)
Object placement uses the landscape's **south-east corner** as origin:

```
origin_E = 576000.0
origin_N = 4631040.0

easting  = origin_E - posX        # posX grows WESTWARD
northing = origin_N + posY        # posY grows NORTHWARD
```

This is the convention Condor's `.obj` reader uses and matches the `CCRR` naming
(col 0 = east, row 0 = south). Encoding then immediately decoding a known
footprint (Skopje Zoo, source centroid `E≈534537, N≈4650510`) reconstructs to
within ~14 m — confirming the sign convention end-to-end.

> Slovenia2 writes a stray `0x0d` byte as the first pad char after the name;
> harmless (the reader uses `nameLen`). `place_buildings.py` pads cleanly with
> nulls.

### Minimal reader/writer
```python
import struct
REC = 152
def encode(posx, posy, posz, scale, ori, name):
    b = name.encode("ascii")[:131]
    return struct.pack("<5fB", posx, posy, posz, scale, ori, len(b)) + b.ljust(131, b"\x00")

def decode(rec):
    posx, posy, posz, scale, ori, nlen = struct.unpack("<5fB", rec[:21])
    return posx, posy, posz, scale, ori, rec[21:21+nlen].decode("ascii")
```

---

## 3. How the `.obj` consumes the building `.c3d`

Every record names a model in `World/Objects/`. The `.c3d` is the actual mesh +
texture (binary Condor model; see `docs/condor_file_formats.md` §3.7 and the
`_c3d_*` decode scripts). The placement file does **not** contain geometry — it
only says *which* model, *where*, *how big*, *which way*.

`place_buildings.py` writes a **placeholder name** (`building.c3d`) into every
record. `scripts/c3d.py` must produce a model under that exact name (or you
re-run with `--c3d-name` to match whatever `c3d.py` emits). The name in the
record and the file in `World/Objects/` must match exactly.

### Two consumption modes

**Phase A — one shared model (what this sample emits).**
All 158,526 records reference a single `building.c3d` (e.g. a generic textured
box, scaled per-record). Condor batches draw calls by model, so a single shared
mesh is cheap regardless of count. Good for a fast in-sim sanity pass. Height is
*not* expressed (the `.obj` has only a uniform `scale`); a generic box reads as
roughly single-storey. To vary height with one model you would need a unit-cube
`.c3d` and a height-encoding `scale`, which also stretches X/Y — acceptable only
for crude blocks.

**Phase B — one merged model per patch (the quality target).**
Bake one `.c3d` per occupied 5760 m patch (`bldg_CCRR.c3d`) containing every
building in that patch as real extruded prisms at their true heights, then
rewrite each record's `name` to its patch model and set `scale=1.0`,
`ori=0`, with the per-building position/orientation **baked into the mesh**.
`--emit-grouping` writes `building_patch_groups.json` (per-patch list of
`e,n,area,height,ori`) as the input contract for that bake. This gives correct
heights and the **ideal one-draw-call-per-patch** budget.

> Draw-call budget: with per-patch merges the **busiest patch (CCRR 0703 ≈ 15,900
> buildings — central Skopje)** becomes one very heavy `.c3d`. `c3d.py` must keep
> the per-patch mesh within Condor's poly/LOD limits (decimate, drop the smallest
> footprints, or split tall vs short). The stats file reports the worst case.

After installing `.obj` + `.c3d`, **no re-hash is needed for objects** — `.tha`/
`.fha` cover only `.tr3`/`.for`. Re-hash only if terrain or forest changed.

---

## 4. Sample run output (this repo, defaults)

`python scripts/place_buildings.py --emit-grouping`

| Metric | Value |
|--------|-------|
| Raw footprints (Overture+MSFT+OSM) | 189,330 |
| Outside bbox / invalid | 33 / 0 |
| Dropped `< 40 m²` | 30,771 |
| **Placed (significant)** | **158,526** (64,512 OSM + 94,014 MSFT) |
| `.obj` size | 24,095,952 B (`= 158,526 × 152`) |
| Patches occupied | 137 / 144 (7 empty, all rural) |
| Buildings/patch min·median·max | 1 · 173 · **15,895** (CCRR 0703) |
| Footprint area median·p95·max | 123 · 531 · 130,310 m² |
| Height median·p95·max | 3 · 3 · 155 m (**98.9% defaulted to 3 m**) |

**Outputs (all in `.sandbox/buildings/`, NOT installed):**
- `MacedoniaSkopje.obj` — placement records (placeholder `building.c3d`)
- `building_stats.json` — full histograms + the merge strategy block
- `building_patch_groups.json` — per-patch merge plan for Phase B

### Known limitations
- **Height is essentially unknown:** only ~1.1% of footprints carry
  `building:levels`/`height`; everything else is the 3 m single-storey default.
  Heights only matter once Phase B bakes real prisms. A future improvement is a
  land-use/area heuristic (e.g. taller in the city core) — not done here to avoid
  fabricating data.
- **Placeholder model:** the sample references `building.c3d`, which `c3d.py`
  must still produce. Nothing is installed; this is a sandbox artifact.
- **Phase A height fidelity:** a single shared model cannot express per-building
  height through the `.obj`'s single uniform `scale` without also distorting the
  footprint. Real heights require Phase B (or a small set of height-bucketed
  models).
