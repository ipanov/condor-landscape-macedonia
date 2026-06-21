# Condor 2 Landscape Pipelines — MacedoniaSkopje

Authoritative description of every generation pipeline in this repo: what it
produces, the data it consumes, the invariants it must hold, how to run it, and
how to scale it from the 12×12 Skopje pilot to **full North Macedonia**.

Read this together with `docs/condor_landscape_spec.md` (the binary file-format
spec) and `CLAUDE.md` (the hard rules). Never guess a format — both are verified.

---

## 0. The grid is the single source of truth

Everything is parameterised from **`scripts/condor_grid.py`**. Change it once and
every pipeline rescales. The current Skopje pilot:

| Constant | Value | Meaning |
|----------|-------|---------|
| `ULXMAP, ULYMAP` | 506880, 4700160 | Top-left (NW) corner, UTM 34N (EPSG:32634) |
| `XDIM` | **30.0** | DEM metres/pixel — must be *exactly* 30, not 29.987 |
| `WIDTH, HEIGHT` | 2305 | Full DEM samples (= patches·192 + 1) |
| `PATCHES_X, PATCHES_Y` | 12 | Patch grid (→ 144 patches) |
| `TILES_X, TILES_Y` | 3 | Tile grid (→ 9 tiles) |
| `PATCH_SIZE_M` | 5760 | Patch edge = 64·90 = 192·30 |
| `PATCH_MASK_SIZE` | 512 | Forest mask px/patch |

Derived corners (verify after any change): bottom-right easting/northing
`575910 / 4631130`, raster span `2304·30 = 69 120 m` per side.

**Condor coordinate convention (memorise this):** patch filenames are `CCRR`
where **CC = column, 00 = EAST** and **RR = row, 00 = SOUTH**. So `c=0` is the
east edge, `r=0` is the south edge. Files: heightmaps `hCCRR.tr3`, textures
`tCCRR.dds`, forest `CCRR.for` (no prefix).

---

## 1. Phase 1 — Terrain

| Step | Script | Output | Size |
|------|--------|--------|------|
| Flatten runways in the DEM | `flatten_runways.py` | `sources/dem/..._30m_2305_flat.raw` | 2305²·2 B |
| 90 m overview heightmap | `generate_trn.py` | `MacedoniaSkopje.trn` (768×768) | 1,179,684 B |
| 144 per-patch heightmaps | `generate_tr3.py` | `HeightMaps/hCCRR.tr3` (193×193) | 74,498 B each |
| Airports / turnpoints | `generate_apt.py`, `generate_cup.py` | `.apt`, `.cup` | — |
| Thermal map | `generate_tdm.py` | `MacedoniaSkopje.tdm` (768×768) | 589,832 B |
| Flight-planner map | `generate_flight_planner_map.py` | `MacedoniaSkopje.bmp` (768×768) | ~2.4 MB |

### Hard invariants
- `.trn` is the **768×768** (patches·64) 90 m overview — **not** the 2305² DEM.
  Its 3 header float fields are pixel spacing and MUST be `(90.0, -90.0, 90.0)`.
  Header origin = bottom-right `575910 / 4631130`.
- `.bmp` and `.tdm` dimensions MUST equal the `.trn` (768×768).
- `.tr3` are **193×193 uint16**, stored as the **anti-transpose** of a north-up
  patch — `patch.T[::-1, ::-1]` (see §5). This was *the* mesh-tear bug: writing
  north-up with no rotation diverges neighbour edges by up to ~1.5 km.

### Runway flatten + safety gates
`flatten_runways.py` carves a flat plateau over each runway footprint
(LWSK 238 m, LWSN 318 m, LW67 371 m) so ground/tow/winch starts work.
`stage_and_verify_flat_tr3.py` regenerates the 144 patches into
`output/HeightMaps_flat_staging/` and runs **three gates before install**:
1. **SEAMS** — max shared-edge mismatch between all neighbours must be **0 m**
   (reconstructs Condor's read transform on every patch pair).
2. **PLATEAU** — each runway patch is flat at its exact target elevation.
3. **ISOLATION** — every staged patch is byte-identical to the known-good backup
   *except* the 3 runway patches.

Exit 0 only if all three pass. This is the model for any terrain change: **stage,
gate, then install** — never write straight to the install unverified.

---

## 2. Phase 2 — Textures

| Step | Script | Notes |
|------|--------|-------|
| Download ortho | `download_mk_ortho_2023*.py`, `download_all_quadrants.py` | MK 2023 cadastre WMS (EPSG:6316), Esri fallback |
| Build patch DDS | `build_patch_textures.py` | `gdalwarp` per patch → `nvcompress -bc1` (DXT1) → `tCCRR.dds` |
| Bake water | `bake_water.py` | Overlays OSM water/rivers, re-encodes affected tiles **DXT3** (alpha) in place |
| Fix discoloured tiles | `fix_textures.py` | Re-warps + LAB colour-matches a tile to its 8-neighbour median |

### Hard invariants
- DDS are **2048×2048 per patch** (DXT1 ≈ 2.79 MB, DXT3 ≈ 5.59 MB). **Never
  8192×8192** — it BSODs the GPU driver.
- `empty.dds` MUST be 2048×2048 DXT1 (never 4×4).
- Textures are stored **north-up** (top = north, left = west). Do **not**
  transpose (unlike `.tr3`/`.for`).
- `bake_water.py` / `fix_textures.py` operate **in place** on the install and
  back up every overwritten tile to `Textures_bak_phase1/`.

---

## 3. Forest auto-generation — `generate_forest_maps.py`

The headline pipeline. Produces **144 × `CCRR.for`** (512×512 uint8, 262,144 B)
in `…/ForestMaps/`. Value encoding: **0 = none, 1 = coniferous, 2 = deciduous**.

### 3.1 Why it looks right: extent is real satellite canopy, not guesswork
The whole design principle: **let authoritative remote-sensing data place the
forest; use OSM only to refine.** Forest *extent* is a continuous threshold of
canopy density, so it follows terrain organically and is seam-free across patch
boundaries.

```
canopy =  (TCD ≥ 40)                          # HRL Tree-Cover-Density, primary
       |  (WorldCover == tree  & TCD ≥ 30)     # ESA WorldCover fills HRL gaps
       |  (OSM forest polygon  & TCD ≥ 20)     # mapped woods, where any canopy exists
forest = canopy  AND NOT exclusions            # roads/water/rail/buildings/urban/runways
forest = despeckle(forest)                     # 1-px opening + drop <3-px components
forest[elev > 2300] = 0                        # treeline; fade 2100–2300 m
```

Thresholds live at the top of the script (`TCD_TREE_MIN=40`, `TCD_WC_MIN=30`,
`TCD_OSM_MIN=20`). Raise `TCD_TREE_MIN` for sparser/higher-altitude-only forest,
lower it for denser. On the Skopje pilot this yields **35.2 % forest cover**,
**IoU 0.79 against the raw satellite canopy** — i.e. the forest mask genuinely
reproduces where the trees actually are.

### 3.2 Two design rules learned the hard way (do not regress)
- **OSM is a *booster*, never a *gate*.** OSM forest in Macedonia maps only ~⅓
  of real canopy. `OSM AND satellite` under-maps forest ~3× (15 % vs 44 % real
  canopy) and produces blocky coverage that follows OSM digitisation, not
  terrain. Always **union** OSM with satellite canopy.
- **`.for` is binary presence+species — there is no density channel.** So stand
  fragmentation must come from *holes in the extent*, which the naturally-holey
  TCD raster already provides. Do **not** add per-patch random thinning to
  "fragment" it: a per-patch RNG makes patches bimodal (full vs collapsed) and
  produces hard tree-density seams at every patch boundary. The continuous
  threshold + a deterministic `despeckle()` is correct and seam-free.

### 3.3 Species (conifer vs deciduous)
Precedence, highest first:
1. **OSM leaf tags** — `leaf_type=needleleaved`/`leaf_cycle=evergreen` → conifer;
   `broadleaved` → deciduous.
2. **Probability model** (`conifer_probability`) for everything else: folds in
   elevation, slope aspect, CORINE class, value-noise, the **HRL DLT** as a soft
   prior, and a **Mt-Vodno Gaussian bias** (black-pine afforestation belt south
   of Skopje: centre `533395 / 4645813`, σ 9 km, in the 500–1100 m band).
3. **Hard floor** — any pixel the HRL DLT confirms as coniferous is forced to
   conifer.

Result: conifer = **3.1 % of forest**, which is *data-accurate* for this region
(HRL DLT says 1.6 % of canopy is conifer; the small Vodno/OSM boost is
deliberate and defensible — do not inflate it to "look balanced").

### 3.4 Exclusions (rasterised as no-tree)
OSM water (+5 m shoreline), waterways, roads (class-width buffers), railways,
buildings, **urban/settlement landuse**, and airport-runway rectangles. Keeps
trees off roads, water, rooftops and runways per the quality standard.

### 3.5 Data sources (`download_forest_rasters.py`, run first)
| Layer | Product | Source | Warp |
|-------|---------|--------|------|
| TCD | Copernicus HRL Tree-Cover-Density 2018 (0–100 %) | EEA DiscoMap ImageServer (EPSG:3035) | bilinear |
| DLT | Copernicus HRL Dominant-Leaf-Type 2018 (1=broadleaf, 2=conifer) | EEA DiscoMap ImageServer (EPSG:3035) | nearest |
| WorldCover | ESA WorldCover 2021 v200 (class 10 = tree) | ESA public S3 | nearest |

All are warped onto the landscape grid → `.sandbox/forest_rasters/*_utm34_30m.tif`.
OSM vectors are cached GeoJSON in `.sandbox/osm/`. DEM = the canonical flattened
`sources/dem/macedonia_skopje_dem_30m_2305_flat.raw`.

### 3.6 Run
```bash
python scripts/download_forest_rasters.py     # once; caches rasters
python scripts/generate_forest_maps.py        # 144 .for -> install + validation/forest/
tools/CLT2.7/LandscapeEditor.exe -hash MacedoniaSkopje   # regenerate .fha
```
The run is **deterministic** (seeded RNG only) → identical `.for` every time, so
hashes stay valid across reruns. It writes a geographic QA overview
(`validation/forest/forest_map_overview.png`, hillshade + grid) and stats.

---

## 4. Hashing — after ANY `.tr3` or `.for` change

```bash
tools/CLT2.7/LandscapeEditor.exe -hash MacedoniaSkopje
```
Headless, exit 0, **no GUI** — the only confirmed-safe CLT CLI command. Rewrites
`MacedoniaSkopje.tha` (terrain hash, covers `.trn`/`.tr3`) and `.fha` (forest
hash, covers `.for`) — each holds one non-zero entry per patch. Skip this and
Condor rejects the landscape. It is **mandatory** after terrain or forest edits.

---

## 5. Orientation — the rule that bites everyone

Condor stores `.tr3` **and** `.for` as the **anti-transpose** of a north-up
array: `stored = A.T[::-1, ::-1]`. Authored north-up (row 0 = north, col 0 =
west), the transform is applied once, last, before `tofile`. Verified against
Slovenia2 (anti-transpose IoU 0.62 vs 0.39 identity) and on our seams (0 m).
The anti-transpose is **self-inverse**: apply it again to read back to north-up.

**Diagnostic gotcha (cost real time once):** to render the installed patches as
one geographic image, place each north-up patch at block **`(11-row, 11-col)`**
and do **not** apply a global `[::-1, ::-1]` to the assembled mosaic — that flips
each patch's *internals* 180°, mismatching neighbours and faking "patch seams"
while leaving per-patch fractions correct. If fractions match but the picture is
blocky, suspect the *render*, not the data. Validate extent with **IoU against
the raw satellite canopy**, not by eyeballing a hand-assembled mosaic.

---

## 6. Validation — `verify_phase1.py`

Checks every file against the spec: `.trn` 768²/zone/origin/spacing, 144 `.tr3`
@74,498 B, `.tdm`/`.bmp` = TRN dims, 144 `.for` @262,144 B, `.tha`/`.fha` have
144 non-zero entries, textures present, directory structure. Run after any
regeneration. (Loading-screen BMPs are a separate cosmetic deliverable and the
only expected failures until real MK glider photos are added.)

---

## 7. Reproduce from scratch (run order)

```bash
# Phase 1 — terrain
python scripts/flatten_runways.py
python scripts/generate_trn.py
python scripts/generate_tr3.py
python scripts/stage_and_verify_flat_tr3.py     # gates: seams=0, plateau, isolation
python scripts/generate_apt.py
python scripts/generate_cup.py
python scripts/generate_tdm.py
python scripts/generate_flight_planner_map.py

# Phase 2 — textures
python scripts/download_mk_ortho_2023_zoom11.py
python scripts/build_patch_textures.py
python scripts/bake_water.py
python scripts/fix_textures.py                  # only if specific tiles are off-colour

# Forest
python scripts/download_forest_rasters.py
python scripts/generate_forest_maps.py

# Seal + verify
tools/CLT2.7/LandscapeEditor.exe -hash MacedoniaSkopje
python scripts/verify_phase1.py
```

---

## 8. Scaling to full North Macedonia

The pipelines are grid-driven, so expansion is a **reparameterisation**, not a
rewrite:

1. **Pick the bounding box** for full North Macedonia in UTM 34N. Set `ULXMAP`,
   `ULYMAP`, `PATCHES_X`, `PATCHES_Y` in `condor_grid.py` so `WIDTH/HEIGHT =
   patches·192 + 1`. Keep `XDIM = 30.0` exactly. Condor landscapes are limited
   to ~64×64 patches; full MK (~250×170 km) fits comfortably within one tileset
   if patch counts are chosen accordingly.
2. **Re-source the DEM** (Copernicus GLO-30) for the new extent → `sources/dem/`.
3. **Re-run Phase 1** — `.trn` becomes `(patches·64)²`; `.tr3` count = patches².
   The seam/plateau/isolation gates carry over unchanged.
4. **Textures** — extend the ortho download to the new bbox; `build_patch_textures`
   loops the new patch grid automatically.
5. **Forest** — `download_forest_rasters.py` already exports by the grid bbox, so
   it scales for free; DLT/TCD/WorldCover cover all of Europe. `generate_forest_maps`
   loops `PATCHES_X × PATCHES_Y`. **No thresholds need changing** — they are
   physical (canopy %, treeline metres), not region-specific. The Vodno bias is
   Skopje-specific; generalise it to a small table of named conifer belts, or
   drop it and rely on HRL DLT alone for the wider map.
6. **Airports** — add the rest of MK's fields to `data/airports.json`; the
   flatten + apt + cup steps consume it generically.
7. **Hash + validate** as in §4 and §6.

The invariants in §1/§2/§3/§5 are **format-level** and do not change with extent.

### 8.1 NM build — as implemented (`CONDOR_LANDSCAPE=nm`)

The full North Macedonia landscape is selected by the env var
**`CONDOR_LANDSCAPE=nm`** (read by `condor_grid.py`). Grid: **40×32 = 1280
patches**, NW `447690 / 4694070`, BR `678090 / 4509750`, DEM **7681×6145** @30 m,
UTM 34N. Name `NorthMacedonia`; install `C:/Condor2/Landscapes/NorthMacedonia/`.

Every pipeline writes to **landscape-scoped paths** so the Skopje pilot is never
clobbered (and is byte-identical under the default env):

| Asset | Skopje (default) | NM (`CONDOR_LANDSCAPE=nm`) |
|-------|------------------|----------------------------|
| Forest rasters | `.sandbox/forest_rasters/` | `.sandbox/forest_rasters_nm/` |
| OSM cache | `.sandbox/osm/` | `.sandbox/osm_nm/` |
| Airports JSON | `data/airports.json` | `data/airports_nm.json` |
| Runway footprints | — | `data/northmacedonia_runway_footprints.geojson` |
| DEM (flattened) | `…_2305_flat.raw` | `…_7681x6145_flat.raw` |
| Forest validation | `validation/forest/` | `validation/forest_nm/` |

Run order (forest + water + airports — `nm`):
```bash
export CONDOR_LANDSCAPE=nm
python scripts/download_forest_rasters.py        # DLT/TCD/WorldCover -> forest_rasters_nm (auto bbox/3035-tiling/WC tiles)
python scripts/download_osm_nm.py --workers 4    # TILED Overpass (10x6 @0.30deg, mirror-rotating, resumable) -> osm_nm
python scripts/generate_apt.py                   # NorthMacedonia.apt (14 airfields, whole-deg heading, width->tug)
python scripts/flatten_runways.py --footprints   # runway flatten footprints for the terrain agent (+flattens DEM if present)
python scripts/generate_forest_maps.py --workers <ncores> --wait-dem 1800   # 1280 .for, parallel, polls for the DEM
python scripts/bake_water.py                     # DXT3 water bake from osm_nm water.geojson/waterways.geojson
tools/CLT2.7/LandscapeEditor.exe -hash NorthMacedonia   # .fha (after the .for exist)
```

Key NM-specific differences from the pilot (do not regress):
- **`download_osm_nm.py`** replaces the single-bbox Skopje OSM scripts for the big
  extent: it **tiles** the bbox (a single Overpass query over 250×170 km times
  out), rotates across Overpass mirrors on 429/timeout, caches each `(layer,tile)`
  raw response under `.sandbox/osm_nm/_tiles/` (resumable), and emits every layer
  the forest/water passes need — `forest` (with `leaf_type`), `water`,
  `waterways`, `roads_lines`, `railways_lines`, `buildings`, `runways`,
  `settlements`, plus `aerodromes.json` for airport discovery.
- **Vodno conifer Gaussian is DISABLED for NM** (`USE_VODNO_BIAS=False`): a single
  Skopje-centred bump would mis-colour the whole country. Species rely on the
  **HRL DLT** soft prior + elevation model (conifer ≈ data-accurate, not inflated).
  The Skopje-bbox CORINE hint is likewise off (`USE_CLC=False`) — it doesn't cover
  the NM extent.
- **`generate_forest_maps.py` is parallel** (`ProcessPoolExecutor`, one shared-data
  load per worker, deterministic seeded RNG → byte-identical regardless of worker
  count). The QA overview is assembled from per-patch thumbnails (NM full-res is
  too large to hold). Extent algorithm, anti-transpose, exclusions, despeckle and
  treeline are **unchanged** from §3.
- **Airports**: `data/airports_nm.json` is the NM superset (14 fields: LWSK, LWOH,
  LWSN, LW67, LW66/Prilep-Malo-Konjari, LW74/Bitola-Logovardi, LWPR/Dolneni, LW70,
  LW73/Štip, LWST, LWNE/Negotino, LWDK/Demir-Kapija, LWGR/Gradsko, LW71/Sveti-
  Nikole), runway geometry from OSM `aeroway=runway` ends cross-checked with
  OurAirports. The pilot `data/airports.json` (3 fields) is untouched.
