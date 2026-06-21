# Building autogen design — MacedoniaSkopje (Skopje, North Macedonia)

How the 158k real building footprints become an architecturally-appropriate,
vertex-budgeted, well-distributed set of placed Condor 2 objects that read as
**Macedonian / Balkan and ex-Yugoslav socialist**, not Austro-Hungarian.

This is the *design + sourced research* deliverable. It does **not** install
anything, does **not** open a GUI, and supersedes the "Phase B per-patch merge"
sketch in `docs/building_autogen.md` §3 (see §6.1 for why the model-library
approach beats per-patch baking). The custom hero landmarks (~a dozen named
Skopje icons) are a **separate task** and are deliberately excluded here — this
algorithm only *reserves slots* for them and never tries to model them.

Status of inputs (already done, do not re-run):
- `download_buildings.py` → `.sandbox/buildings/buildings_combined_utm.geojson`
  (189,330 raw footprints: 67,650 OSM + 121,680 Microsoft ML, EPSG:32634).
- `place_buildings.py` → `MacedoniaSkopje.obj` (placeholder), `building_stats.json`,
  `building_patch_groups.json`.
- `c3d.py` → verified `.c3d` reader/writer/builders (round-trips Slovenia2 byte-exact).

---

## 0. TL;DR of the design

1. **Carry the OSM tags through** `place_buildings.py` (it currently throws them
   away). They are the entire basis of classification.
2. **Classify every footprint → one of ~9 object CLASSES** from (OSM type →
   religion → levels → area → neighbourhood density), with the 121,680 untyped
   Microsoft footprints and the 64% `building=yes` OSM footprints resolved by an
   **area + local-density** fallback calibrated below.
3. **Build a small reusable LIBRARY of ~25–40 `.c3d` models** (the Slovenia2
   pattern: 64 models → 7,496 placements). Architecture-appropriate: panelák
   slabs/point-blocks, Balkan houses, **Orthodox** churches, a few mosques,
   flat-roof sheds. Sourced in §3.
6. **Place by instance** via the existing 152-byte `.obj` record: centroid →
   `(posX,posY,posZ)`, longest-edge bearing → `ori`, footprint length / model
   native length → `scale`, class → model name.
5. **Prioritise + budget**: rank by significance, then enforce a **per-tile
   object budget** with an even spatial **grid-thinning** so neighbourhoods stay
   dense where real and thin where real, never clumping or gapping. Budget is an
   engineering choice (Condor has **no documented hard cap** — §1), tuned to the
   ~5.7 km object load distance and draw-call/collision cost.

The whole thing stays deterministic (seeded), pure-Python (numpy + shapely),
sandbox-only, exactly like the forest pipeline.

---

## 1. Condor object/vertex limit — what is actually documented

Researched against the Condor developer forum (the people quoted are
authoritative: **Uroš/`Uros` = Condor developer**; **`wickid` = Condor 3 dev who
wrote the OSM autogen exporter**; **`Cadfael` = Miloš Koch, author of the stock
Slovenia2 landscape**; **JBr = Jiří Brožek, the Airport Maker author**). The
official *Landscape Guide rev2* PDF is image-only / Scribd-gated and could **not**
be machine-read — so every quantitative claim below is **forum/dev-sourced, not
guide-sourced**, and labelled where it is folklore vs a dev statement.

### 1.1 Is there a hard limit? — No documented cap
**There is no documented hard ceiling** on objects per landscape, per tile, or
vertices per single `.c3d`. The constraint is FPS, not a format limit. Evidence:
- Slovenia3 has buildings "well into the 100,000s"; a German C3 landscape totals
  "ca. 250,000 patches" of autogen. (wickid / 6266,
  `condorsoaring.com/forums/viewtopic.php?t=20541`, `…?f=37&t=23026`)
- A search for a 65535/65536 vertex cap found nothing; the only stated penalty
  for huge meshes is FPS, implying they still load. (`…?t=18599`)
- **Could not find** a documented max-vertices-per-`.c3d`. Treat "no hard cap" as
  confirmed; treat any specific ceiling as undocumented.

### 1.2 What actually dominates performance — draw calls, then (in C2) collisions
This is the load-bearing fact for the whole design. From the Condor dev:

> "The most critical performance factor is the number of **draw calls**. A draw
> call is invoked for each object within a C3D file… 3000 C3D files × 30 objects
> = 90000 draw calls per frame! … group, group, group … The number of
> **vertices/faces is by far less critical** on today's hardware." — Uros,
> `…?t=18629`

> Optimal C3D = "one merged object, one texture = one draw call." — JBr, same
> thread.

And specifically for **Condor 2** (this is why dense cities tank C2 FPS):

> "the reason for the high framerate hit in Condor2 is the **collision
> detection**. We have optimised it for Condor3." — wickid, `…?t=20541`

So the C2 cost model is **draw calls + collision detection ≫ vertex count**.
Design consequences: minimise distinct *instances/objects*, reuse a small model
set so models stay resident, and don't fear a few-thousand-vertex model.

### 1.3 Object load / view distance — the project's "~5.7 km" is confirmed
Distance is set by **which file the object lives in**, NOT by object size (there
is no "bigger ⇒ visible from further" rule):

| Placement method | Load distance | Source |
|---|---|---|
| **Editor / `.obj`-placed objects** (our path) | **~5.7 km** (Xavier's older C2 figure: 5 km) | wickid `…f=37&t=23026`; Xavier `…?t=20470` |
| **Airport `O`/`G` files** | **23 km** | Xavier `…?t=20470` |
| **Condor 3 autogen patches** (`o<patch>.c3d`) | **up to 17 km (3 patches)** | wickid `…f=37&t=23026` |

The "~5.7 km for editor objects" working number this project already had is
**confirmed verbatim** by the C3 dev. Implication: autogen buildings placed via
`.obj` pop in at ~5.7 km — fine for the valley/ridge-soaring use case, since at
glide height you mostly read the *near* town fabric; don't bother with sub-15 m
detail because it's invisible from soaring altitude (EDB, `…?t=18629`).

### 1.4 LOD — there is none for geometry
Condor 2 has **no per-object geometric LOD** (no detail-mesh swapping). The only
"LOD" is the binary distance cull in §1.3. Cheap distant towns come from the
*author* shipping low-poly merged blocks, not from the engine. (Thread literally
titled "Condor 3 LOD / Object Visibility Distance" is entirely about load
distance — `…f=37&t=23026`.)

### 1.5 The only quantified authoring guidance: ~250 m grouped blocks
No published per-tile/per-landscape budget exists. The **only** numbers in the
record:
- **Group buildings into ~250 m blocks**, one texture each — bigger single
  objects cause a streaming hitch when they enter the 5.7 km range. (GregHart1965
  `…?t=20470`; EDB `…?t=18629`)
- Rule-of-thumb vertex sanity ceiling: a full glider+pilot ≈ **60,000 vertices**;
  a landscape object should be "well below that." (wickid `…?t=20541`) — **dev
  rule-of-thumb, not a spec.**
- "When the FPS drops too much, stop adding objects." (EDB) / "too many objects
  destroy FPS… only manual placement of larger objects (many houses inside one
  object) gives a flyable result." (Cadfael `…?t=19247`)

Slovenia2 is consistent with all of this: **7,496 placements of just 64 distinct
models**; single houses ≈ 38 verts; village-cluster blocks up to ≈ 11,700 verts
(the "~250 m merged block" pattern, well under 60k). Verified by decoding the
installed files in this repo.

### 1.6 The budget this autogen will adopt (an engineering choice, labelled as such)
Condor documents no number, so we pick one and justify it:

- **Target ≤ ~120 distinct placed instances per occupied 5760 m patch** for the
  *single-building* classes, plus any *cluster* models, *plus* unlimited reuse of
  the resident model set. Rationale: draw calls scale with instances×objects; a
  single-mesh house model = 1 object = 1 draw call, so ~120 houses + a few
  clusters per patch ≈ a few hundred draw calls/patch — and only the ≤ ~9 patches
  within 5.7 km are ever resident at once.
- **Hard cap per model ≈ 8,000 vertices** (well under the 60k glider figure), and
  **prefer single-`g`-group, single-texture models** (1 draw call each) per §1.2.
- This is **deliberately conservative** for C2's collision cost. It is a knob
  (`MAX_INSTANCES_PER_PATCH`), not a Condor limit. The busiest patch today is
  CCRR 0703 = 15,895 footprints (central Skopje); §5 thins that to budget while
  keeping the city *looking* dense.

> If/when this targets **Condor 3** instead, switch to the autogen-patch path
> (`o<CCRR>.c3d`, 17 km, near-zero collision cost) and the budget becomes almost
> irrelevant — see §8.

---

## 2. How senior Condor devs do autogen — and what we adapt

### 2.1 Cadfael / Miloš (Slovenia2, Slovakia, Czech) — the aesthetic target, NOT a copyable method
The famous socialist-block landscapes are **hand-placed bespoke models, not
procedural autogen.** Cadfael (Miloš Koch, forum `u=363`, condorworld.eu, active
2026) states he works **manually (~1,200 h/landscape, payware)**, grouping many
house meshes into **one composite town object** for FPS, flattening terrain under
them by hand (`…?t=19247`, `…?t=17866`). His look — clustered, real-size
ex-Yugoslav blocks — is exactly our target, but:
- **His models are payware, all-rights-reserved.** No reuse license found
  anywhere. **Do not lift his `.c3d`s.** To request reuse: forum PM `u=363` /
  `shop@condorworld.eu`. (Flag: must ask; assume no by default.)
- His *technique* we DO adopt: **reuse-heavy small model set + cluster nearby
  houses into composite objects** (decoded directly from Slovenia2: 64 models,
  and the `C*` models are themselves multi-building clusters — `C0V` = 14 sub-
  objects over 154 m, `C2R` = 61 sub-objects over 600 m).

### 2.2 Condor 3 `CondorOSMExporter` — the real footprint-driven blueprint
Author **wickid**. A **Blender 4.0.2 + BLOSM** addon: pulls OSM footprints per
patch via Overpass, **procedurally extrudes** each to its OSM height/levels,
generates flat vs hipped roofs (`roof_flat.py`, `roof_hipped_multi.py`), bakes
facade/roof textures keyed by **material + colour hex**
(`baked_dddddd_plaster`, `…_concrete`, `…_roof_tiles`, `…_brick`, `…_metal`),
samples the real patch terrain for elevation, and emits one **`o<patch>.obj/.mtl`
per patch** → LandscapeEditor → `o<patch>.c3d` autogen (17 km, optimised
collision). (`…?t=22543`, `…?t=22869`, `…f=37&t=23026`.)

What we adapt (the **upstream half** is sim-agnostic and matches our pipeline):
- **footprint → height (levels×3 / height tag / type default) → material/roof
  class → mesh** is exactly our classification chain.
- Its gotchas pre-warn us: zero-width/degenerate polygons crash the roof
  generator (we already `buffer(0)` and area-filter); empty patches < 2 kB
  trip anticheat in C3 (irrelevant on the C2 `.obj` path, relevant if we go C3).

What we **don't** copy: it's GUI/Blender, C3-only autogen layer, 25k buildings →
~20 GB. We stay headless Python on the C2 `.obj` path.

### 2.3 MSFS2020 — the principle only
Bing imagery → **Blackshark.ai** ML → footprint + height + roof + **building
type**, then **procedural** per-building synthesis (PGG, CityEngine-PRT-like),
seeded by footprint+type+landclass. No model library pick. The transferable
principle is the one we already use: **footprint polygon + height + type → an
appropriate shell.** (FSDeveloper overview; Threshold/Blackshark.) Keep this as
inspiration, not a dependency.

### 2.4 Net method we implement
**Footprint-driven classification → small reusable Balkan/socialist `.c3d`
library → per-instance `.obj` placement → significance ranking → even
grid-thinning to a per-tile budget.** It out-automates Cadfael's hand-placement
by driving the *same* clustered-socialist look from the 158k footprints, and
borrows wickid's footprint→height→material logic without his GUI.

---

## 3. Architecture-appropriate object sources (sourced; verify license per page)

All Sketchfab "free" ⇒ usually **CC-BY (attribution required)**, NOT CC0 — fine
for a fan landscape with a credits file; read each model's license box before
shipping. Verified-real links are marked ✓ (page opened during research).

### 3.1 Socialist apartment blocks — the hero asset (BEST free coverage)
- ✓ **Sketchfab "Buildings-Panelki-Free" collection (13 models)** — curated
  panelka/Khrushchyovka set at varying sizes:
  `sketchfab.com/evaddugina/collections/buildings-panelki-free-252e5d3977eb4567a1e12edc5112cc33`
- ✓ **"LOW POLY – SOVIET APARTMENT BUILDING 8K"** by Colin.Greenall — **CC-BY,
  free, 10.4k tris / 5.3k verts**, photoreal 8K bake. Best single free hero slab;
  decimate to ≤ 8k verts.
  `sketchfab.com/3d-models/low-poly-soviet-apartment-building-8k-05229ac1d1f94e6c8cacaad91110c602`
- Tag hubs (filter license = CC-Attribution/CC0):
  `sketchfab.com/tags/khrushchyovka`, `sketchfab.com/tags/plattenbau`
- ✓ **@iskra3d "Iskra: Yugoslav Architecture" (16 models)** — hand-modelled
  ex-Yugoslav: *Apartment Buildings Bor*, *Kindergarten 1980s*, *Yugoslav
  Department Store*, *House of Culture*, socialist *Hotels*, *Post Offices*. The
  **most on-register source for Karpoš**. Confirmed at least one downloadable;
  verify license per model. `sketchfab.com/iskra3d/models`
- Paid backups (clean, ideal poly-count for `.c3d`):
  CGTrader "Russian Soviet Panel Building (Khrushchyovka)" royalty-free,
  **1,655 polys**; CGTrader "Soviet Panel Building"; Free3D "Communist Apartment
  Block" ($20, 3,642 polys).

### 3.2 Balkan / Macedonian houses (PARTIAL free — custom recommended)
- ✓ **"Balkan House"** by TiaDalma — **1.3k tris**, free (verify license; page
  mis-tagged). `sketchfab.com/3d-models/balkan-house-f54d0902934e4e31a56599da889b732d`
- "Lowpoly Mediterranean House" (4DigitalARTS, free); "Lowpoly Buildings"
  (l0wpoly, 45 simple buildings, one 2048² atlas — great filler).
- itch.io "Retro PSX: Balkan Interior/Architecture" ($4.99, explicitly Balkan/USSR).
- **Recommendation: custom-model a 4–6 variant Balkan-house kit** (1–3 storey,
  ~4:12 red-tile pitched roof, plaster / unfinished red-brick) in Blender via
  box-extrude + a photo-texture atlas shot from Skopje-valley villages. This is
  the strongest regional-identity lever and is cheap at `.c3d` poly budgets.

### 3.3 Orthodox churches (decent free; **domed Byzantine, never Catholic spires**)
- ✓ "Byzantine Church" (MWintersberger, free, central dome) —
  `sketchfab.com/3d-models/byzantine-church-edc0cbbcb1834b169379f1ba4ebf513e`
- "Byzantine Church @ Voulkano Monastery" (free, photogrammetry → decimate);
  tag hub `sketchfab.com/tags/orthodox-church`; CC0 via `meshy.ai/tags/orthodox`.
- Need only ~2–3 variants (small village church + a larger town church).

### 3.4 Mosques (Ottoman + minaret; use sparingly)
- ✓ CGTrader "Ottoman Turkic Style Mosque" — **royalty-free, no-AI**, minaret +
  forecourt, BLEND/OBJ/FBX. Best free Ottoman option.
  `cgtrader.com/free-3d-models/architectural/other/ottoman-turkic-style-mosque`
- "Djamaa Djedid" (Sketchfab, free, octagonal minaret). Need ~1–2 variants.

### 3.5 Generic filler packs (TRUE CC0 — bulk mid-rise/commercial/industrial)
- **Kenney City Kits** (CC0): Suburban/Commercial/Industrial/Modular/Retro Urban,
  OBJ/FBX/glTF. `kenney.nl/assets/category:3D?search=building`
- **Quaternius** (CC0): Ultimate/Buildings/Simple Buildings, Downtown MegaKit.
  `quaternius.com` (one-click GLB mirror: poly.pizza).
- **KayKit City Builder Bits** (CC0); Eclair "City Roads GLB Pack" (CC0).
- Recolour these grey-concrete for the socialist register; perfect for distant
  fill / flat-roof commercial-industrial boxes.

### 3.6 Coverage verdict + what must be custom
| Class | Free coverage | Action |
|---|---|---|
| Socialist blocks (panelák) | **Well** (Panelki + iskra3d, mostly CC-BY) | use as-is, decimate the 10k-tri ones |
| Balkan houses | Partial | **custom kit** (Blender box-extrude + photo atlas) |
| Orthodox churches | Well (CC-BY + CC0) | use 2–3, decimate photogrammetry |
| Mosques | OK | 1 free CGTrader Ottoman mosque suffices |
| Generic filler (commercial/industrial/mid-rise) | **Well (CC0)** | Kenney/Quaternius, recolour |
| Named Skopje brutalist icons (Telecom tower, Opera, City Wall) | **None** | **separate custom-landmark task — out of scope here** |

**Flag:** the simplest, most license-clean, and most *regionally accurate* route
for the two bulk classes (houses + paneláky) is to **model ~10 parametric shells
ourselves in Blender** (box + pitched/flat roof + a Balkan photo-texture atlas)
and treat the downloads above as references/variety. That removes all
attribution-tracking risk for the 99% of placements and matches Condor's
single-diffuse low-poly `.c3d` target exactly. Downloads are best used for the
*characterful* minority (churches, mosques, a few distinctive blocks).

---

## 4. Classification — footprint → object class (grounded in the real data)

### 4.1 Data reality that drives the rules (measured from the combined geojson)
- **189,330 footprints; only 67,650 (OSM) carry type tags.** The 121,680
  Microsoft ML footprints have **geometry only** (`src=msft`, no type/levels).
- Of the OSM rows, **64% are `building=yes`** (untyped). Specific types that
  matter: `house` 17,182, `apartments` 1,881, `residential` 1,786, plus
  `industrial/commercial/retail/warehouse/school/hospital/office`, and **108
  mosque / 92 church** footprints, **342 with a `religion` tag (217 muslim /
  124 christian)**.
- **Only ~1.1% carry a height signal** (`building:levels` 2,183, `height` 346).
- **Area/levels separate the classes cleanly** (median m² | median levels):
  `house` 119 | 2 · `apartments` **400 | 6** (p90 levels 10) · `residential`
  161 | 5 · `commercial` 560 | 2 · `industrial` 993 | 1 · `school` 1052 | 2 ·
  `church` 171 · `mosque` 206. MSFT-untyped median 118 m² ≈ `house`.

> **Critical fix:** `place_buildings.py` today keeps only `e,n,area,height,ori`
> and discards `building`, `building:levels`, `height`, `religion`, `name`,
> `amenity`. The redesign must **propagate these tags** into the per-footprint
> record so classification can use them. (Tags survive into the combined geojson;
> they're only lost in `place_buildings`.)

### 4.2 Object classes (map to the §3 model library)
| Class | Model kind (§3) | ~models |
|---|---|---|
| `PANELAK_SLAB` | long socialist slab block | 3–4 length variants |
| `PANELAK_POINT` | socialist point/tower block | 2–3 |
| `HOUSE_SMALL` | 1–2 storey Balkan house, pitched | 4–6 |
| `HOUSE_LARGE` | 3 storey town house / villa, pitched | 2–3 |
| `MIDRISE` | 3–6 storey mixed residential/commercial | 2–3 |
| `SHED_FLAT` | flat-roof commercial/industrial/retail box | 3–4 (scale to fit) |
| `CHURCH_ORTHODOX` | domed Orthodox church | 2–3 |
| `MOSQUE` | Ottoman mosque + minaret | 1–2 |
| `(LANDMARK_RESERVED)` | — skip; separate task owns it | 0 |

### 4.3 The classifier (deterministic precedence; first match wins)
Per footprint with area `A` (m²), `levels` (parsed, may be None), longest/short
edge `L`/`W`, and OSM tags:

```
1. RELIGION / WORSHIP  (tag-driven, highest priority)
   building in {church, cathedral, chapel, monastery}            -> CHURCH_ORTHODOX
   religion == christian (any amenity=place_of_worship)          -> CHURCH_ORTHODOX
   building == mosque OR religion == muslim                      -> MOSQUE
   # NOTE: in MK, Orthodox is the church default. NEVER emit a Catholic model.

2. EXPLICIT NON-RESIDENTIAL TYPE
   building in {industrial, warehouse, retail, commercial,
                hospital, school, university, public, government,
                train_station, hangar, supermarket}              -> SHED_FLAT
                (if A < 300 and levels in 3..6: MIDRISE instead)

3. EXPLICIT RESIDENTIAL TYPE
   building == apartments                                        -> PANELAK_* (4.4)
   building == residential:
        levels >= 4 OR A >= 300                                  -> PANELAK_* (4.4)
        else                                                     -> HOUSE_LARGE
   building in {house, detached, bungalow, terrace, semi*}       -> HOUSE_* by size (4.4)

4. HEIGHT-FIRST FALLBACK (typed `yes` OR untyped MSFT/OSM)
   levels present:
        levels >= 7                                              -> PANELAK_* (4.4)
        levels in 4..6                                           -> MIDRISE
        levels <= 3                                              -> HOUSE_* by size (4.4)

5. AREA + LOCAL-DENSITY FALLBACK  (the 121,680 MSFT + 64% `yes`)
   # local_density = #footprints within 60 m (precomputed via STRtree)
   A >= 700                                                      -> SHED_FLAT
   A >= 350 AND local_density high (urban core)                 -> PANELAK_SLAB
   A >= 350 AND local_density low                               -> HOUSE_LARGE
   120 <= A < 350                                               -> HOUSE_LARGE if dense else HOUSE_SMALL
   A < 120                                                      -> HOUSE_SMALL
```

### 4.4 Sub-selection (which model + height within a class)
- **PANELAK_SLAB vs POINT**: aspect ratio `L/W`. `L/W >= 2.2` → SLAB (pick the
  length variant whose native length is closest to `L`); else POINT.
- **HOUSE_SMALL vs LARGE**: `A < 150 → SMALL`, else `LARGE`. Pick variant by a
  **seeded hash of the footprint id** so the same building always gets the same
  model (deterministic) and neighbours vary (no repetition banding).
- **Height** (only matters for §6 scale-in-Z): `levels×3.0` if tagged, else
  `height` tag, else the **class default** — HOUSE 6 m (2 storey), MIDRISE 15 m,
  PANELAK_SLAB 24 m (8 storey), PANELAK_POINT 30 m (10 storey), SHED 6 m,
  CHURCH/MOSQUE per-model native (don't stretch sacred geometry; place at scale 1
  and let `ori`/footprint pick size buckets instead).

> **Honesty about heights:** 98.9% of footprints have no height data; class
> defaults are an informed prior (apartments median *is* 6 levels in OSM here),
> not fabricated per-building truth. Good enough at 5.7 km soaring view; do not
> oversell it.

---

## 5. Distribution + prioritisation (no clumping, no gaps, within budget)

The aim: the city reads **dense where it is dense and sparse where it is sparse**,
with the *significant* buildings always kept, the long tail thinned **evenly**,
and every patch under the §1.6 budget. Three stages.

### 5.1 Significance score (per footprint)
```
score =  w_area   * log1p(area)                  # bigger ⇒ more visible from air
       + w_height * (levels or default_levels)   # taller ⇒ landmark-ish
       + w_type   * type_weight                  # church/mosque/apartments boosted
       + w_named  * (1 if OSM `name` present else 0)   # named ⇒ real landmark
```
`type_weight`: CHURCH_ORTHODOX/MOSQUE highest, then PANELAK_*, MIDRISE, SHED_FLAT,
HOUSE_LARGE, HOUSE_SMALL lowest. Worship + named + tall always survive thinning.

### 5.2 Even spatial thinning to the per-tile budget (the anti-clump core)
Per patch (5760 m), if the count exceeds `MAX_INSTANCES_PER_PATCH`:
1. **Always keep** the "protected" set: worship, named, `score` above a high
   percentile, and every PANELAK_* (the city's silhouette).
2. **Grid-thin the remainder**: overlay a uniform sub-grid on the patch (e.g.
   48×48 → 120 m cells). In each cell keep the **single highest-score** footprint
   (or top-k where the budget allows), drop the rest. This is **Poisson-like even
   decimation**: it preserves *spatial coverage* (no gaps, no clumps) instead of
   the naive "keep global top-N" which would strip whole outer neighbourhoods to
   feed the centre. Cell size = `patch / sqrt(budget)` so kept count ≈ budget.
3. The dropped footprints are **not wasted**: optionally fold the densest cells
   into a **cluster model** (§6.2) so the city core stays visually solid while
   still ~1 object.

This directly fixes the current pathology: CCRR 0703 = 15,895 footprints would
otherwise be unrenderable; grid-thinning keeps a representative, even ~120–300
of them (+ optional clusters) and the eye still reads "dense Skopje centre."

### 5.3 Neighbour de-duplication
Where OSM and MSFT both mapped the same building (the combine step already
de-stacks within 8 m), an extra `STRtree` pass drops any survivor whose centroid
is within ~4 m of a higher-priority survivor, so no double-walls.

### 5.4 Tunable knobs (top of the script, all seeded/deterministic)
`MAX_INSTANCES_PER_PATCH` (default 120 single-models + clusters), `MIN_AREA_M2`
(40, already), grid cell = `PATCH/sqrt(budget)`, the `w_*` weights, `type_weight`
table, the area/levels thresholds in §4.3, and `RNG_SEED`. Reruns are
byte-identical (forest-pipeline discipline).

---

## 6. How it plugs into `place_buildings.py` + the c3d bake

### 6.1 Why model-library beats per-patch baking (corrects `building_autogen.md` §3)
The existing doc proposed "Phase B: one merged `.c3d` per patch." Decoding
Slovenia2 shows the shipped landscape does **not** do that — it places **64
reusable models** as **7,496 instances** with per-record `scale` (0.28–1.81) and
`ori`. Reasons the library approach wins:
- **Draw calls/resident memory** (§1.2): a handful of resident models reused
  thousands of times is the dev-recommended pattern (EDB/JBr); a distinct heavy
  mesh per patch is more unique geometry to stream at the 5.7 km boundary and
  hitches (§1.5).
- **Architecture control**: a library lets us guarantee *Balkan/Orthodox* models;
  a raw extrude-per-footprint bake (BLOSM-style) gives generic boxes with no
  regional identity — the very thing the user rejects.
- **Heights**: per-instance `scale` already varies size; class-default heights
  (§4.4) give believable silhouettes without per-building height truth.
Keep per-patch **cluster** models only as an *optional booster* for the densest
cells (§6.2), not as the primary mechanism.

### 6.2 Concrete changes to `place_buildings.py`
1. **Carry tags**: in pass 1, keep `building`, `building:levels`, `height`,
   `religion`, `amenity`, `name`, and a stable `fid` per footprint (currently
   dropped). Add `local_density` (STRtree count within 60 m) once.
2. **New module `classify_buildings.py`**: `classify(props, area, L, W,
   local_density) -> (class, model_name, height_m)` implementing §4.3–4.4 against
   a small **`model_library.json`** (`{class: [{name, native_len_m, native_wid_m,
   native_height_m, file}]}`). Pure function, unit-testable.
3. **Significance + thinning `select_buildings.py`**: §5.1 score, §5.2 per-patch
   grid-thin to budget, §5.3 de-dup. Emits the kept set with the chosen
   `model_name` per record.
4. **Record emission** (existing 152-byte writer, unchanged format):
   - `posX = ORIGIN_E - e`, `posY = n - ORIGIN_N`, `posZ = DEM(e,n)` (as now).
   - `ori  = longest_edge_bearing` (as now; already `[0,π)`).
   - `scale = footprint_len / model.native_len` (so the model's footprint matches
     reality from the air) — clamp to e.g. `[0.5, 2.0]` to avoid grotesque
     stretch; for Z-height fidelity prefer **picking a height-bucketed model**
     over Z-stretch, since the `.obj` has only ONE uniform scale (X/Y/Z together).
   - `name = model_name` (e.g. `panelak_slab_b.c3d`).
   Group records by model so Condor batches them (write order by model is enough;
   the format needs no change).
5. **Outputs** (sandbox only): `MacedoniaSkopje.obj` (real models now), updated
   `building_stats.json` with per-class and per-patch-budget histograms, and a
   `building_class_overview.png` QA render (classes coloured on the hillshade,
   same style as `forest_map_overview.png`) to validate distribution **before**
   any install.

### 6.3 The c3d side (library build, separate but specified)
- Acquire/decimate the §3 models → convert each to Condor `.c3d` via
  **ObjectEditor** (OBJ→C3D; the documented path — `docs/condor_airport_workflow.md`),
  single DDS/DXT1 texture per `g`-group, RGB material `1,1,1`, ≤ ~8k verts each.
- Validate each with `scripts/c3d.py` `parse_c3d` round-trip before use (the same
  strictness that catches the "Airport not installed" class of corruption).
- Normalise each model to a **known native footprint** (length/width/height in m)
  recorded in `model_library.json`, with its **local origin at the footprint
  centre** and **+Y = the long axis**, so the placement `scale`/`ori` math in
  §6.2 is exact.
- Install models to `World/Objects/` and the `.obj` alongside the landscape.
  **No re-hash needed for objects** — `.tha`/`.fha` cover only `.tr3`/`.for`
  (verified; objects don't feed the terrain/forest hashes).
- Optional cluster models (§6.2) for the densest cells: bake N footprints'
  prisms into one `bldg_cluster_CCRR_k.c3d` (real positions/heights baked in,
  placed at `scale=1, ori=0`) — the Slovenia2 `C*`-style composite, capped at
  ~8k verts and ~250 m extent per §1.5.

### 6.4 Pipeline position
```
download_buildings.py        (DONE)
        ▼
buildings_combined_utm.geojson  (+ OSM tags, EPSG:32634)
        │  place_buildings.py  --classify  (NEW: carries tags, calls classify+select)
        ▼
MacedoniaSkopje.obj   (real model names)  +  building_stats.json  +  QA overview
        │  c3d.py / ObjectEditor  (build the §3 model library → World/Objects/*.c3d)
        ▼
install .obj + World/Objects/*.c3d  (objects load at ~5.7 km; no re-hash)
```

---

## 7. Validation (before any install — sandbox QA, mirrors the forest gates)
1. **Class plausibility**: per-class counts and area/levels medians match §4.1
   (e.g. PANELAK share ≈ apartments+tall-residential ≈ a few %; HOUSE dominates;
   mosques ≤ ~the significant subset of 108, churches similar — **not all** of
   them).
2. **Distribution**: per-patch instance counts all ≤ budget; the
   `building_class_overview.png` shows even coverage (dense core, sparse rim) with
   no checkerboard clumps and no holes in occupied neighbourhoods.
3. **Geometry sanity**: decode-back a sample of `.obj` records; centroids
   reconstruct to within ~14 m (as already verified for the Zoo), `ori∈[0,π)`,
   `scale∈[0.5,2.0]`, every `name` exists in `World/Objects/`.
4. **`.c3d` integrity**: every library model round-trips through `scripts/c3d.py`.
5. **Determinism**: rerun → byte-identical `.obj`.

---

## 8. If/when this targets Condor 3 instead
Switch the placement target from the C2 `.obj` (5.7 km, collision-heavy) to the
**autogen patch** path: emit `o<CCRR>.obj/.mtl` per patch, batch via CLT3
LandscapeEditor → `o<CCRR>.c3d` (loads to 17 km, near-zero collision cost), then
**delete any `o*.c3d` < 2 kB and regenerate the autogen hash `.oha`** (C3
anticheat). The §4 classification and §3 library carry over unchanged; the §5
per-tile budget becomes largely moot (density is cheap in C3). Either reuse the
official `CondorOSMExporter`/BLOSM (Blender 4.0.2) or keep our headless Python and
just change the output filenames + hashing. This keeps the Balkan/Orthodox model
control that the stock BLOSM extrude lacks.

---

## 9. Uncertainties / flags (do not paper over)
- **No Condor-documented object/vertex cap exists** — the §1.6 budget is *our*
  engineering choice; label it as such, not as a Condor limit.
- **All §1 numbers are forum/dev-sourced**; the Landscape Guide PDF was not
  machine-readable. The 5.7 km / 23 km / 17 km / draw-call / collision facts come
  from the Condor devs (Uros, wickid) on the forum and are reliable, but cite the
  forum, not the guide.
- **Heights are essentially unknown** (98.9% defaulted). Class defaults are an
  informed prior, not per-building truth.
- **121,680 of 189,330 footprints have no type tags** (Microsoft ML); those lean
  entirely on the §4.5 area+density fallback. Expect the classifier to be most
  confident in OSM-mapped Skopje and weakest in MSFT-only rural fabric (mostly
  HOUSE_SMALL, which is the safe default).
- **Cadfael's models are off-limits** without his permission; the §3 sources are
  the legitimate route, and modelling ~10 parametric Balkan/socialist shells
  ourselves is the cleanest path for the bulk classes.
- **Named Skopje landmarks are a separate task** — this design only reserves
  their slots (LANDMARK_RESERVED, skipped) and must not double-build them.
```