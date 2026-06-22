# Object placement — catalog, root-cause analysis, and the ONE generic algorithm

Authoritative reference for placing **any** 3D object (custom-migrated models *and*
future auto-generated building shells) onto the Condor 2 ground texture to **1–3 m /
1–3°**, validated **without launching the sim**. Produced from a 5-agent parallel
investigation + an adversarial review by Codex (gpt-5.5/high), 2026-06-22.

> **Design law:** there is **ONE generic, object-agnostic placement engine**
> (`scripts/place_objects.py`). It is driven by a per-object **manifest** + data
> sources. We do **not** write a placement script per object. A hangar, a church, a
> tower and a generic autogen box all go through the same code path.

---

## 0. Why a single large, obvious hangar kept landing wrong (the three real bugs)

| # | Bug | Symptom | Root |
|---|-----|---------|------|
| 1 | **Texture-grid drift, constant −45 E / +45 N (16 texels @ 2048)** | object 45 m NE of the painted building | textures are gdalwarp'd to the **30 m DEM grid** (`patch_bounds_utm`, SE corner 576000/4631040); objects place on the **`.trn`/object grid** (`OBJ_ANCHOR` 575955/4631085). Slovenia2 — where `OBJ_ANCHOR` was calibrated — has its textures on the `.trn` grid, so the gap is *ours*. The whole landscape (forest/water/airports) shares this texture-vs-mesh offset (POSTMORTEM defect #3). |
| 2 | **Orientation measured from the coarse texture** | rotation visibly off (see Screenshot_88) | `detect_place_stenkovec_hangar.py` derives the angle from a bespoke **de-rotated edge projection** that searches tilt only **±12°** around the EPSG:6316 image axes (`:215,:263`). Any building whose long axis is >12° off-axis **cannot** be matched — it locks onto a near-axis-aligned rectangle. And the installed DDS is **2.8125 m/texel** (5760/2048): you physically cannot measure an angle to 1° on it (1° over 34 m ≈ 0.2 texel). |
| 3 | **Front/rear (the 180°) resolved ad-hoc** | doors / façade face the wrong way | per-object heuristics ("greenest direction", a fixed apron azimuth). Undirected geometry can *never* resolve 180° — it needs a directed cue. |

Plus a latent **datum** hazard (bug 4, below) that does not bite the hangar (its
footprint comes from OSM/WGS84) but will bite cadastre-sourced autogen.

**The reframing that fixes all of it:** *position/size/orientation come from a
**vector footprint**, not from pixels; the texture is used only to **validate**.* The
2.8 m/texel installed DDS is a **display asset, not a measuring instrument**.

---

## 1. Coordinate systems — keep them in sync (the user's #1 concern)

| System | EPSG | Role | How it reaches UTM 34N (32634) |
|--------|------|------|--------------------------------|
| Condor object/landscape grid | 32634, anchored at `.trn` header BR + half-pixel → **575955 / 4631085** | where `.obj` records place objects; the in-sim truth | native |
| Source DEM grid | 32634, exact 30 m, SE **576000 / 4631040** | `.tr3` height sampling **and the grid textures are currently warped to** | native |
| Per-patch DDS pixel grid | 32634 (= DEM grid today) | the raster Condor draws; validation raster | `patch_bounds_utm()` |
| **MK cadastre ortho + vector footprints** | **6316** (MGI 1901 / Balkans 7, Bessel) | ortho textures **and** building polygons + floors + use | **pinned** op (see §4) |
| Microsoft GlobalML footprints | 4326 | autogen footprint fallback | clean (WGS84≈ETRS89) |
| Overture / OSM | 4326 | footprint fallback / landmark identity | clean |
| Esri World Imagery | 3857 | independent visual cross-check (≈0.3 m res, **but ~4–8 m absolute** — co-register, never trust absolutely) | via 3857 |

**The mesh is on the `.trn` grid, provably without the sim:** a `.tr3` has *no header
and no coordinates* — it is 193×193 raw heights per 5760 m patch. The only absolute
georeference on disk is the **`.trn` header** (`generate_trn.py`). Condor positions
each patch (mesh **and** texture) by that header. So re-warping textures to the
`.trn`/object grid aligns the texture with the mesh and with objects. (Confirmed by
Codex against `generate_tr3.py` / the spec.)

---

## 2. Data-source catalog (footprint • orientation • size • height)

Ranked for North Macedonia. **Vector-first.** Detect on imagery only to *disambiguate*
which polygon, never to *measure*.

| Rank | Source | Footprint | Orient. | Height | CRS → 32634 | Coverage (Skopje) | Access | License |
|---|---|---|---|---|---|---|---|---|
| **1** | **MK cadastre `RM_OBJECTS`** (АКН) | ✅ surveyed | ✅ | ✅ **floors = MAX(BLDN)/parcel** + use `CODE_US` | 6316 → **pin op** | full | **WFS** `e-uslugi.katastar.gov.mk/geo/proxy/wfs_geoserver_vector`, layer `Public:RM_OBJECTS`, `Referer` header, **sequential + backoff** (≤300 m bbox, ~50 s timeout) | gov; cite АКН |
| 2 | **MS GlobalML** | ✅ ML | ✅ | ⚠️ −1 for NM | 4326 (clean) | ~122k "FYROMakedonija" | quadkey GeoJSONL (`ms_footprints.py`) | ODbL |
| 3 | **Overture buildings** | ✅ (OSM∪MS∪Esri) | ✅ | ⚠️ sparse | 4326 | superset | S3 GeoParquet / DuckDB (`download_buildings.py`) | ODbL |
| 4 | **OSM** | ✅ hand-mapped | ✅ | ⚠️ ~1% | 4326 | dense centre | Overpass / cached `.sandbox/osm/buildings.geojson` | ODbL |
| — | GHS-BUILT-H | ❌ (100 m raster) | — | ⚠️ avg only | 54009 | global | GEE | EC-open |
| — | Google Open Buildings / EUBUCCO | — | — | — | — | **do NOT cover NM** (dead ends) | — | — |

**Fusion order:** cadastre footprint+floors → MS (rural fill, geometry only) → Overture
(superset cross-check) → OSM (name/amenity match + the rare real `building:levels`) →
GHS-BUILT-H (coarse height prior only). **Always the BUILDING polygon, never the
parcel** (`KIS:PARCELI` is always larger than the object). Cadastre is the *only*
full-attribute source — cache aggressively, scrape once, politely.

---

## 3. Texture resolution reality (why detection-on-the-DDS can't hit 1–3 m)

- Installed DDS = **2048² over a 5760 m patch = 2.8125 m/texel** (both landscapes;
  header-verified). A 34 m hangar is ~12 texels; a 10 m house ~4 texels.
- Source ortho: Skopje built from **cadastre zoom-11 = 0.28 m/px** (good); NM from
  **zoom-8 = 2.24 m/px** (the documented regression). Either way the **2048²/patch DDS
  hard-caps the output at 2.81 m/texel** — rebuilding only sharpens antialiasing.
- **Verdict:** edge localisation on the DDS is ±1 texel ≈ **±2.8 m at best**, worse with
  DXT1/JPEG ringing. So: **measure pose on a vector footprint** (cadastre 0.08–0.14 m)
  or, if detecting, on a **separate high-res reference** (cadastre **zoom-12 = 0.14 m/px**
  — both highest-res *and* best-georeferenced for MK; Esri 0.31 m only as a
  co-registered cross-check). The DDS is for the **drift check**, not measurement.

---

## 4. Datum handling (latent, fix before cadastre-autogen)

EPSG:6316 = MGI 1901 / Balkans 7 (Bessel). pyproj **and** gdalwarp default to the
**5 m-accuracy** `MGI 1901 to WGS 84 (1)` Helmert because no NM transformation grid is
installed. Better ops exist (Codex confirmed on this stack: **`MGI 1901 to WGS 84 (10)`,
EPSG operation 6206, accuracy 2 m**, selected with `--spatial-test intersects`; AREC's
own 7-param via ETRS89 EPSG:6205 claims ~0.7 m but is not exposed by the installed PROJ).

Rules:
1. **Pin one operation** (start with EPSG op 6206) and use the **same** in `pyproj` and
   `gdalwarp -ct`; **verify empirically** (a known cadastre point must land on the known
   ortho pixel) rather than trusting the number.
2. **Common-mode cancellation only holds when the same datum+op+build produced both
   layers.** The **existing installed textures** were built with the GDAL *default* (5 m)
   op — so to place on *today's* textures you must reproject footprints with the **same
   default op** (or, for OSM/MS in 4326, the datum issue doesn't arise). After a re-warp
   with a pinned op, switch all footprint reprojections to that same pinned op.
3. MS/OSM/Overture are 4326 — they never share the cadastre's MGI datum, so mixing them
   with 6316 cadastre always carries the MGI↔WGS84 bias unless you pin.

---

## 5. The ONE generic algorithm

### Phase 0 — one-time landscape fixes
- **0a. Re-warp ALL raster layers to the OBJECT/`.trn` grid.** Add
  `condor_grid.patch_bounds_condor()` (anchored at `OBJ_ANCHOR`, **not** a redefinition
  of `patch_bounds_utm`, which DEM/TR3 extraction depends on). Migrate **textures +
  water bake + forest + the validators** to it in one deliberate staged pass (Codex:
  re-warping *only* the DDS would just rename the drift). After 0a, object-at-true-UTM
  lands on the painted building with **no** correction and the DDS is a trustworthy
  validation raster. *Rejected alternative:* baking −45/+45 into `obj_record_xy` — it
  would move objects off the Slovenia2-calibrated anchor and still leave footprints
  needing a correction.
- **0b. Pin the datum op** (§4) in `condor_grid`, shared by the warp and every reproject.

Until 0a runs, placement targets the **painted texture** by applying the explicit frame
correction `(E−45, N+45)` (= `OBJ_ANCHOR − BR` ; see `TEXTURE_FRAME_CORRECTION`) — an
explicit `target_frame: installed_texture_dem_grid`, not a hidden nudge.

### Phase 1 — footprint sourcing (vector-first, fused; §2)
Resolve the object's **BUILDING** polygon from the best available source; reproject with
the frame-consistent datum op. Imagery only picks between conflicting polygons.

### Phase 2 — model→footprint pose (generic; the core)
- **position** = footprint area-weighted (shapely) centroid.
- **orientation mod 90°** = **length-weighted edge-orientation histogram folded mod 90°**
  (robust on L/T/U and near-square — every wall edge votes), cross-checked with the
  minimum-rotated-rectangle. **Never** from texture pixels.
- **long-vs-short (mod 90 → mod 180)** = match the model's base-outline aspect to the
  footprint OBB.
- **front/rear (the 180°)** by a **directed cue**, in priority:
  1. **model self-cue** — a "front" feature baked into the source mesh (custom: the
     hangar's `Hangar_Door` material at local +X; autogen shell: the modelled façade
     side). *This is the answer to "which side is the front?" — it lives in the model.*
  2. **world cue** — the footprint edge nearest/parallel to the adjacent OSM road = the
     street façade; or an `entrance=*` node; or the runway/apron azimuth for airport
     objects.
  3. **asymmetry** — an L/T/U footprint breaks 180° by registration IoU (require a margin).
  4. **generic box, no cue** → front = street side if a road exists, else a **deterministic
     default** with `front_confidence: none` (180° is visually unobservable on an
     untextured/symmetric box, so do **not** block bulk autogen — Codex). A *hero* or
     façade-textured object with an unresolved front **is REJECTED, never guessed.**
- **scale** = uniform `sqrt(area)` for detailed meshes (preserve proportions; **flag** the
  aspect residual if model≠real), anisotropic L/W to the OBB for generic boxes.
- **height** from cadastre floors → posZ = **DEM altitude** at (E,N) (absolute, not 0).

### Phase 3 — game-free validation (three INDEPENDENT gates; reject on any fail)
Round-trip the **would-be `.obj` record + installed `.c3d`** back to a world footprint
(validate the bytes, not the intent), then:
1. **Vector geometry** (`verify_object_placement.py`): centroid ≤ 2 m, long-axis azimuth
   (mod 180) ≤ 2°, long/short size ≤ 2 m, IoU ≥ 0.85 (landmark) / 0.75 (autogen), and
   **source-conflict** check across cadastre/MS/OSM.
2. **Directed front + chirality** *(new — Codex hole)*: front residual ≤ 90° vs the cue
   (catches the 180° flip that all mod-180 gates miss); a **mirror/chirality** assertion
   (the winning `footprint_registration` hypothesis must **not** be mirrored — a mirror is
   a hard FAIL, not a note).
3. **Installed-DDS drift, NUMERIC** *(new — Codex hole)*: sample the installed texture
   under the placed polygon vs its surround (edge/roof evidence score) — not just an
   overlay image — to catch a texture-vs-mesh grid mismatch even when vector+placement
   share a UTM frame.

Plus a **3-panel overlay** (cadastre 0.14 m authority | Esri 0.31 m co-registered |
installed DDS 2.8 m drift-check) for the human's eyes, and a `posZ == DEM ± tol` assert.

### Phase 4 — scale to thousands
- **bulk** autogen via landscape `.obj` (**5.7 km** load); **heroes near an airport** via
  `<Name>O.c3d` (**23 km**); Condor-3 autogen patches later.
- one object + one DDS per c3d (1 draw call); **white material RGB 1/1/1**; store the
  **full** `Landscapes\<Name>\World\Textures\<file>.dds` path in the c3d (Slovenia2
  convention) — not the bare filename. Per-tile vertex/draw-call budget; group dense
  cells into ~250 m composite cluster objects.

---

## 6. Engine architecture (object-agnostic; built on existing scripts)

**Manifest (per object), the only thing that differs between a hangar and an autogen box:**
```
id, model_c3d,
model.base_footprint_source,        # base-Z slice of the c3d
model.front_axis | front_materials, # the self-cue; null for generic boxes
symmetry_class,                     # rect | square | L | round | none
scale_mode,                         # uniform_landmark | anisotropic_autogen
target.sources, target.selector,    # cadastre|ms|osm|overture + how to pick
height_source,                      # cadastre_floors | osm_levels | ghsl_prior
placement_scope,                    # landscape_obj (5.7km) | airport_o_c3d (23km)
confidence_policy,                  # reject_if_unresolved | default_if_unresolved
texture_frame                       # object_grid (post-0a) | installed_texture_dem_grid
```

**Modules** (reuse what exists; add the gaps):
- `condor_grid.py` — `patch_bounds_condor`, `TEXTURE_FRAME_CORRECTION`, pinned datum ops *(added)*.
- `footprint_registration.py` — pose solver *(extend: edge-orientation-histogram seed; mirror = hard reject)*.
- `place_objects.py` — the ONE generic engine *(new; generalises `place_object.py`, which is currently hardcoded to the hangar)*.
- `verify_object_placement.py` — validator *(extend: front, chirality, numeric-DDS gates)*.
- footprint-source adapters: `ms_footprints.py`, `download_cadastre_buildings.py`, OSM/Overture loaders.
- `c3d.py` / `batch_migrate.py` — model→c3d (already production).

---

## 7. Model sourcing (recap)
- The repo's `batch_migrate.py` (Blender bake + `nvcompress` + `obj_to_c3d`, round-trip
  gated) is the production model→c3d path. Use it; never ObjectEditor.
- **FS2020 is a licensing NO-GO for *redistribution*** (encrypted scenery is
  unextractable; even the loose SoFly hangar glTF is a third party's copyrighted add-on;
  Google/Bing 3D Tiles are a hard ToS prohibition). `migrate_stenkovec_objects.py` is a
  valid **technique demo / personal-use** proof, not a shippable-asset source.
- **Default for the thousands of autogen buildings = self-built parametric shells from
  the cadastre/OSM footprint + a photo-façade DDS** (CC0, regionally accurate,
  Condor-optimal). Reserve CC0 (Kenney/Quaternius) + CC-BY (Sketchfab w/ CREDITS) for
  characterful hero/filler models.
