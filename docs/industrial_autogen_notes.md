# Industrial autogen — MacedoniaSkopje (Skopje, North Macedonia)

How the big Skopje-area industrial landmarks (refineries, power/CHP plants, steel
works, cement silos, chimneys, cooling towers, storage tanks, ferroalloy stacks)
become **max-quality placed Condor objects**, and which load-distance path each
deserves. This is the *research + staged deliverable* doc. It does **not** install
anything and does **not** touch the `.obj`/`.apt`. It complements
`docs/building_autogen_design.md` (the bulk-housing autogen) — industrial
landmarks are a small, hand-curated set placed at a **longer view distance** than
ordinary buildings.

All Condor facts below are **forum/dev-sourced** (the official Landscape Guide PDF
is image-only). The condorsoaring.com forum returns HTTP 403 to direct fetch, so
quotes were extracted via search; spot-check against the live threads (logged in)
before quoting verbatim. Thread IDs are cited inline.

---

## 0. TL;DR

1. **Distance is set by which FILE the object lives in, not by object size.** A
   150 m chimney placed via the landscape editor pops in at the *same* ~5.7 km as
   a shed. There is **no "bigger ⇒ visible from further" rule** (dev-stated, t=23026).
2. So big industrial landmarks must NOT use the 5.7 km `.obj` editor path. Use a
   long-range path:
   - **Condor 2 (this project today): attach the landmark to the nearest airport's
     `<Name>O.c3d` → 23 km load, no multiplayer-compat break.** OKTA, TE-TO and
     Makstil all sit near **LWSK** (Skopje Intl) / **LWSN** (Stenkovec), so this is
     geographically natural. (Xavier, t=20470.)
   - **Condor 3 (future): autogen patch `o<CCRR>.c3d` → 17 km, dev-preferred**
     (better collision + loading), but editing autogen requires regenerating the
     `.oha` hash and bumping the scenery version in the `.ini`, which **breaks
     multiplayer compatibility** until everyone re-syncs. Slovenia3 puts *all*
     landmarks in autogen. (wickid, t=23026.)
3. **The performance bottleneck is DRAW CALLS, not vertices.** "group, group,
   group" → ideally one merged object + one texture = 1 draw call per C3D. A
   *single* hero landmark may exceed the ~5000-vert soft guideline (dev: "a nuclear
   power plant of which you will have only one in view" can be higher poly); but
   *repeated* objects (pylons, generic boxes) must be ultra-low-poly.
4. **Recognition from soaring altitude comes from the configuration + texture, not
   fine geometry** — the tank-farm grid + column cluster + flare reads as "OKTA";
   long halls + stacks read as "Makstil"; a compact block + cooling tower reads as
   "TE-TO". Spend the polygon budget on silhouette and a good DDS.
5. **Staged-first set (sandbox only, this deliverable):** real-metre, base-z=0,
   round-trip-verified `.c3d` for the refinery columns + tanks, a tall chimney, a
   cooling tower, a flare stack, a silo cluster, and a factory/power hall, each
   with a baked DDS, in `.sandbox/industrial/`. The parent installs + verifies
   in-sim and chooses the airport-O vs autogen attachment.

---

## 1. The three placement paths and their load distances (the central decision)

| Placement path | File | Load distance | Source |
|---|---|---|---|
| **Landscape-editor object** (`.obj` → `World/Objects/*.c3d`) — the bulk-building path | per-object `.c3d` | **~5.7 km** (older Xavier figure ~5 km) | wickid, [t=23026](https://www.condorsoaring.com/forums/viewtopic.php?f=37&t=23026); [t=20470](https://www.condorsoaring.com/forums/viewtopic.php?t=20470) |
| **Airport `O`/`G` files** | `<Name>O.c3d` / `<Name>G.c3d` | **23 km** (loads when glider within 23 km of the airport) | Xavier, [t=20470](https://www.condorsoaring.com/forums/viewtopic.php?t=20470) |
| **Condor 3 autogen patch** | `o<CCRR>.c3d` ("o" + patch number) | **up to 17 km** (3 patches) | wickid, [t=23026](https://www.condorsoaring.com/forums/viewtopic.php?f=37&t=23026) |

The money quote (wickid, t=23026): *"You need to add your object either to the
Autogen patches or to the airportO file, otherwise the visibility distance would
be too short. … If you add it to the AirportO, you won't have compatibility
problems. If you modify one or more Autogen files, you need to generate a new
`.oha` file, and the scenery won't be compatible anymore. … But the Autogen
integration is much more efficient, so it's better to use it."*

### Path recommendation for THIS project (Condor 2)

| Object group | Path | Why |
|---|---|---|
| **Hero landmarks** (OKTA columns+flare+tanks, TE-TO block+cooling tower+stack, Makstil halls+stacks+cooling tower, Titan cement silos+kiln stack, Jugohrom stack cluster) | **airport `O` file (23 km)** | They are unique "singles", they sit near LWSK/LWSN, and the `O` path has **no compat break**. Reserve `O` for genuinely unique landmarks — each instance adds its verts to that file (Xavier). |
| **Repeated filler** (rows of pylons, generic factory boxes, extra tanks) | accept **5.7 km `.obj`** in C2, or move to **autogen** when targeting C3 | Many of them; individually they don't need 23 km, and `O`-file vertex bloat must be avoided. |

**Folklore vs fact:** "tall object ⇒ drawn from further" is **folklore, explicitly
false**. The 5.7 / 17 / 23 km figures are **dev/author-stated**.

> NOTE on the airport-O route mechanics: the landmark's local mesh origin must be
> expressed **relative to the airport reference point** (the same convention
> `scripts/c3d.py` already uses for `<Name>G/O.c3d`), not the `.trn` SE corner that
> the `.obj` path uses. The staged models here are built **base-centred at z=0 in
> real metres**, so the parent only needs to translate each into the chosen
> airport's local frame (Δ = site_UTM − airport_ref_UTM) and drop it into the `O`
> object list. Do **not** copy a `.c3d` from another landscape (the "Airport not
> installed" crash — CLAUDE.md rule #5).

---

## 2. Modelling & optimisation rules (dev-stated)

- **C3D = one OBJ = a scene of sub-objects; each sub-object = one `g`-group = one
  material = one texture.** A power-station C3D legitimately holds cooling towers
  (one texture), chimney (another), buildings (another) as separate groups —
  "they use 3 textures between them" (t=19234). This matches `c3d.py`'s
  one-`C3DObject`-per-texture layout and the StenkovecHangar precedent (9 objects,
  9 albedo DDS).
- **Draw calls dominate, not vertices** (t=18629): *"A draw call is invoked for
  each object within a C3D file … 3000 C3D × 30 objects = 90 000 draw calls."*
  Golden rule **"group, group, group"**; ideal = 1 object + 1 texture per C3D.
  *"The number of vertices/faces is by far less critical on today's hardware."*
- **Vertex guidance (soft, not a hard cap):** general scenery assets "should not be
  over **5000 vertices**"; a 14 000-vert asset cost ~10% FPS nearby; **a single
  landmark in view can be higher** (dev's "nuclear power plant" example); a
  transmission tower decimated **95%** with "hardly any visible impact". Repeated
  objects → every vertex matters.
- **RGB material must be 1.0/1.0/1.0 (white)** on every textured material or Condor
  darkens/hides the texture (the #1 "textures don't show" cause). `c3d.py` already
  defaults `WHITE_MATERIAL`.
- **Soaring-sim philosophy:** "landmarks … should be done with reasonable
  minimalistic number of vertices. An eye can be cheated a lot by a good texture on
  a simple object." Cooling tower ≈ "a cube with a cylinder"; warehouse ≈ "a
  flattish cube with a shallow triangular roof."
- **Never drop internet models in raw** (dev warning): downloaded models "can
  easily end up with hundreds of thousands of vertices … that will kill your FPS."
  Always decimate + re-texture to a single diffuse. (This is why the photogrammetry
  refinery/cooling-tower captures below are **reference only**, not for placement.)

The community wishlist (t=18599) is exactly this project's shopping list: "masts
(several heights), power stations including cooling towers, power pylons,
warehouses/industrial units … cooling towers, chimneys, water tanks, … cranes;
power lines/pylons, cell towers, broadcast masts." Confirms these are recognised
missing generics we build ourselves.

---

## 3. CLT / ObjectEditor / autogen handling (dev-stated)

- **OBJ → C3D (canonical manual path):** open OBJ in `ObjectEditor.exe`; set every
  textured material RGB to **1/1/1**; fix each texture path to
  `Landscapes/<Name>/World/Textures/<file>`; save → reopen to verify; place `.c3d`
  in `…/World/Objects/`, DDS in `…/World/Textures/`. (t=17819, t=18941.)
- **No official headless OBJ→C3D tool exists** (Jan 2025). `objecteditor.exe in.obj
  out.c3d` does NOT work. Community workaround: a **dummy landscape as a batch
  converter** (rename OBJs to patch numbers, let LandscapeEditor build the C3Ds,
  rename back); **LandscapeEditor runs out of memory on large batches** → kill +
  restart frees RAM and resume. (t=19743.)
  > **This repo bypasses all of that:** `scripts/c3d.py` writes the `.c3d` binary
  > directly (round-trips Slovenia2 byte-exact), and `migrate_stenkovec_objects.py`
  > is a proven **glTF → textured-`.c3d` + DDS** converter. So we never touch
  > ObjectEditor or the dummy-landscape trick — we generate `.c3d` headlessly and
  > only round-trip-verify.
- **Autogen build (C3, future):** autogen C3Ds map to patches (same numbering, "o"
  prefix), built via a **Blender 4.0.2 plugin** that imports the terrain; authoritative
  spec = `CondorOSMExporter manual.pdf` ch.7 inside the CLT3 toolkit (not
  web-indexed — read from the toolkit install). Custom textures →
  `<Name>/Autogen/Textures/`. Editing autogen → regenerate `.oha`, bump scenery
  version in `.ini`.
- **Hashing:** terrain stays `LandscapeEditor.exe -hash <Name>` (`.tha`/`.fha`).
  **Objects need no terrain re-hash**; only C3 autogen uses the separate `.oha`.

---

## 4. Licensing (for a freely-distributed non-commercial fan landscape)

The decisive constraint for **bundling a model inside a redistributed landscape**
is the *redistribution / standalone-extraction* clause, not commercial-vs-non.

| Source | Default licence | Attribution? | Bundle in redistributed landscape? | Gotcha |
|---|---|---|---|---|
| **Polyhaven** | CC0 | No (appreciated) | **Yes** (explicitly allows redistribution) | Cleanest. No industrial *models* there though (HDRIs/textures only). |
| **Quaternius** | CC0 | No | **Yes** | No factory kit; has CC0 Silo/WaterTank/WaterTower props (via poly.pizza). |
| **Kenney** | CC0 | No | **Yes** | **Factory Kit / Conveyor Kit / City Kit (Industrial)** — only true CC0 industrial buildings; single colour-map → 1 DDS. Stylised/low-poly. |
| **Poly Pizza** | mostly CC0, **per-model varies** | CC0: no | Yes for CC0 items | Aggregator — some items CC-BY; check each (API gives the attribution string). |
| **Sketchfab (free)** | **CC-BY by default** | **Yes** | **Conditional/risky** | Sketchfab licence forbids redistributing the asset "as a stand-alone file … that allows third parties to extract" it — a sim shipping loose `.c3d`+DDS could breach this even under CC-BY. NC/ND/SA further restrict. |
| **Sketchfab Standard** | royalty-free | No | **No standalone redistribution** | Avoid for bundling. |
| **CGTrader free** | seller-chosen CC (usually CC-BY) | usually yes | Conditional | "Editorial" flag = no sim use. |
| **CGTrader Royalty-Free (paid)** | RF | No | **No** (must Incorporate + prevent extraction) | — |
| **3D Warehouse / Trimble (General Model License)** | GML | keep notices | **Conditional-yes** as a **Combined Work** with "substantial additional content" (a landscape qualifies); no standalone redistribution | AS-IS. |

**Project policy:** prefer **CC0** (Kenney/Quaternius/Polyhaven/CC0-Poly-Pizza) for
anything bundled — zero redistribution risk. **CC-BY** (Sketchfab/CGTrader-free) is
usable only with (a) attribution kept and (b) acceptance that the converted `.c3d`
inside the landscape is a "work incorporating" the model, not standalone
redistribution — the Sketchfab standalone-extraction clause is the real snag for a
fan sim shipping loose files. **Avoid** Sketchfab/CGTrader Standard/RF-paid and
anything Editorial-flagged.

**CREDITS file (required for CC-BY, good practice for CC0):** per CC-BY model store
`"<Title>" by <Author>, <URL>, CC-BY 4.0 https://creativecommons.org/licenses/by/4.0/`.
A `.sandbox/industrial/CREDITS.md` is maintained alongside the staged models and
must ship with the landscape.

---

## 5. Real Skopje industrial sites (from OSM) + reference geometry

Extracted by `scripts/extract_industrial_sites.py` (Overpass over the 12×12 pilot
bbox `W=21.083 S=41.828 E=21.924 N=42.454`). Raw dump:
`.sandbox/industrial/osm_industrial.json` (1 314 features). Curated site list with
priorities + suggested models: `.sandbox/industrial/industrial.json`. Counts:

| OSM kind | n | object-worthy? |
|---|---|---|
| industrial_building | 601 | big halls → yes |
| industrial_landuse (zone polygons) | 303 | framing only |
| works | 187 | medium |
| power=plant/generator | 97 | **mostly SOLAR farms** (flat, NOT objects); the few thermal ones (Te-To/Toplifikacija) are landmarks |
| storage_tank | 49 | **yes** (OKTA farm = 9× ~46 m tanks) |
| silo | 43 | **yes** (Titan cement = ~20 silos) |
| substation | 38 | low value, skipped |
| chimney | 31 | **yes** (TE-TO ×2, Makstil ×5, Jugohrom ×12) |
| cooling_tower | 2 | **yes** (one at Makstil, 40 m base dia) |

### The named hero sites (verified, with corrections)

- **OKTA refinery** (`Рафинерија ОКТА`, zone 125 ha @ ~41.998 N 21.655 E, near
  LWSK). Built 1980, **refining stopped Jan 2013 → now a fuel storage terminal**.
  Visible signature = a **tank farm** (9 tagged ~46 m-dia tanks in OSM) + a modest
  **distillation-column** cluster + a **flare stack** (columns/flare are NOT in
  OSM → model + hand-place). Model the flare **cold (no flame)**. ([abarrelfull](http://abarrelfull.wikidot.com/okta-skopje-refinery),
  [GEO](https://globalenergyobservatory.org/geoid/6600))
- **TE-TO Skopje** (`Те-То` / `Топлификација Исток`, @ ~41.994 N 21.453 E, Gazi
  Baba). **Gas combined-cycle cogeneration** (~220 MWe / 160 MWth, 2012). **Correction
  vs first guess:** it is a compact CCGT block + **cooling tower** + a *moderate*
  HRSG stack — **not** a giant coal smokestack. OSM tags 2 chimneys here.
  ([gem.wiki](https://www.gem.wiki/Skopje_CHP_power_station))
- **Makstil / Železara steelworks** (zone 352 ha @ ~42.016 N 21.468 E — the largest
  industrial zone in-region). **Correction:** **electric-arc-furnace + plate-rolling
  mill**, NOT a blast furnace, and **not** ArcelorMittal (the AM long-products halls
  are a sub-site). Signature = two huge **rolling-mill halls** (OSM real footprints
  HRM 804×204 m, CRM 555×252 m) + **5 chimneys** + **1 cooling tower** (40 m base).
  Reference photos: [thebeautyofsteel/makstil](https://thebeautyofsteel.com/steel-plants-archive/makstil-skopje/).
- **Titan Cementarnica USJE** (`Титан - Цементарница УСЈЕ`, zone 95 ha @ ~41.961 N
  21.453 E, SW gorge). ~20 tall **cement silos** (8–24 m dia) + a tall **kiln
  preheater stack**.
- **Jugohrom ferroalloys** (`Југохром`, zone 34 ha @ ~42.069 N 21.117 E, Jegunovce,
  NW pilot edge). **12 tightly-clustered chimneys** = a strong off-gas-stack skyline;
  the nearest big industrial site to the soaring ridges W of Skopje.
- **OHIS** (chemical, 94 ha @ ~41.962 N 21.484 E) and **Skopje brewery / Pivara**
  (east bank near Te-To) — lower priority, on record in `industrial.json`.

> **Excluded:** the large `power=plant` polygons that are **solar farms**
> (`plant:source=solar`) — flat, belong to the texture/landcover pass, not objects.
> And 38 substations (low soaring value).

### Generic-refinery dimensions (stand-ins; OKTA specifics aren't public)

OSM tags **0** of these sites with a height, so `industrial.json` heights are
**documented priors** flagged `height_src='prior'`, from real plant type:
- Distillation/fractionation columns: 0.65–6 m dia, **6–60 m tall** (banding rings
  + platform decks as *texture*, not geometry).
- Flare stack: **6–183 m**; a plausible OKTA flare ~30–60 m.
- Crude floating-roof tank: ~**39 m dia × 21 m tall** (100k bbl); OKTA ~46 m dia.
  Floating roof = flat slightly-recessed top; fixed roof = shallow cone. Spacing
  ≈ 0.5× neighbour dia, bund walls around groups.
- Chimney/smokestack: tall slightly-tapered cylinder; **no verified 250 m stack in
  Skopje** — don't oversize (the tallest Skopje *buildings* are Cevahir ~130–142 m).
- Cooling tower: hyperbolic waisted shell (coal-style ref) **or** a boxy
  mechanical-draft block with fan stacks (TE-TO CCGT cooling).
- Factory/process halls: large low rectangular boxes, shallow gable or
  sawtooth/north-light roof.

---

## 6. Model sources used + the staged-first set

Full sourced shopping list (with per-model licence, poly/vert, textured, formats)
is recorded in `.sandbox/industrial/industrial_models.json`. Highlights:

| Type | Best free option | Licence | Verts/tris | Textured |
|---|---|---|---|---|
| Chimney/smokestack | **Industrial Smoke Stacks** (Sketchfab) | CC-BY | 1 440 v / 2 868 f | Yes, **4K** ×3 |
| Cylindrical tank farm | **Large Industrial Storage Tanks** (Sketchfab) | CC-BY | 6 444 v / 9 811 f | Yes ×8 |
| Refinery columns/equipment | **Gas/Oil Tank/Refinery/Storage** (Sketchfab) | CC-BY | 63 586 v / 110 104 f | Yes ×9 |
| Transmission pylon | **Pylon** by rhcreations (Sketchfab) | CC-BY | 3 524 v / 5 806 f | Yes ×6 |
| Cooling tower (clean) | **Nuclear Cooling Tower Base Mesh** (CGTrader) | RF-free | 5 800 v | No → texture it |
| Factory/warehouse (CC0) | **Kenney City Kit (Industrial) / Conveyor / Factory Kit** | **CC0** | low-poly | colour-map |
| Spherical LPG tank | **Spherical Tank 1500 MT** (Sketchfab) | CC-BY | 119 936 v | No → texture |

**Gaps with NO good free textured model → custom-modelled** (built parametrically
in `scripts/build_industrial_models.py`, full detail, baked DDS):
- **Flare stack** (nothing exists; trivial = lattice/cyl mast + tapered pipe + cold
  tip).
- **Spherical LPG tank** (geometry exists untextured; we paint a metal diffuse).
- **Clean lightweight cooling tower** (only heavy photogrammetry or untextured base
  mesh free).
- **Gas holder/gasometer** (only photogrammetry shells; fake from cylinder + lattice).
- **Distillation column** as a clean primitive (the Sketchfab refinery is 110k tris
  — reference only; a parametric banded column is cheaper and cleaner).

> Because the CC-BY downloads are not on this machine and several key types are
> outright gaps, **the staged-first set is built parametrically at full detail with
> baked procedural DDS** (`build_industrial_models.py`) — every model real-metre,
> base-centred z=0, single diffuse, round-trip-verified through `c3d.py`. These are
> production-usable AND double as the exact size/origin templates to drop a
> downloaded CC-BY mesh onto later (same native dimensions recorded in
> `industrial_models.json`). This honours CLAUDE.md rule #11: full vertex count, no
> box-fallback for the landmark shapes (the columns/towers/tanks are true cylinders
> and a true hyperboloid, not stand-in cubes), real textures (baked DDS), and the
> low-detail-only types are flagged above.

### Staged outputs (`.sandbox/industrial/`)
- `<name>.c3d` — the models (round-trip verified).
- `<name>.dds` — baked diffuse per model (DXT1/DXT5 via nvcompress).
- `industrial.json` — the real sites (coords/footprints/priority/model mapping).
- `industrial_models.json` — per-model manifest: `{site?, model_c3d, native dims,
  verts, textured, source_url, license}` + the placement records
  `[{site, model_c3d, lat, lon, height_m, ori_deg}]`.
- `CREDITS.md` — attribution for any CC-BY source folded in.
- `industrial_overview.png` — QA render of every staged model (no GUI).

---

## 7. What must still be custom-modelled / decided (flags — do not paper over)

- **Distillation columns, flare stack, OKTA tank farm layout** are NOT in OSM →
  positions are hand-set from the refinery process-area centroid; refine against the
  ortho.
- **All heights are priors** (OSM tags none) — believable at 23 km soaring view, not
  per-structure truth. Refine from an aviation-obstacle DB or EIA if found.
- **Airport-O vs C3-autogen** attachment is the parent's call (this doc recommends
  airport-O for C2; the models are origin-agnostic, base-z=0, so either works).
- **CC-BY downloads** (Smoke Stacks, Storage Tanks, Pylon, refinery) are NOT yet on
  disk — fold them in later for extra fidelity, adding each to `CREDITS.md`. The
  staged parametric set stands in the meantime and defines the drop-in templates.
- **Pylon lines** as repeated objects belong on the 5.7 km `.obj` (C2) or autogen
  (C3) path, never the `O` file (vertex bloat).
