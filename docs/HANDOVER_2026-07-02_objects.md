# HANDOVER — Stenkovec/Skopje object population + autogen (2026-07-02)

Session ended early (token budget). This doc is the complete state + next actions for
whichever agent continues. Read `CLAUDE.md` (esp. rules 5/7/10/11), the skill
`.claude/skills/migrate-objects-to-condor/SKILL.md`, `docs/OBJECT_PLACEMENT.md`, and
`docs/objects/msfs_object_recovery.md` before touching anything.

**User's goal (verbatim intent):** finish the Skopje/Stenkovec MVP — (1) populate
Stenkovec (LWSN) with the full SoFly LWXX object set equivalents, (2) same for Skopje
International (LWSK), (3) landmarks = best DOWNLOADED models (user explicitly rejects
self-made parametric landmarks: "avoid doing them from scratch"), (4) autogen for
bridges + industrial (refinery/power plants) + pylon lines from OSM/cadastre with
overlay validation, (5) then extend to Kumanovo (LW67). Work parallel where possible.
Accounts for downloads: user's IlyaPanov8@gmail.com or Ilya.Retech@Retechgoogle.com
(NOT IlyaPanov83@gmail.com — full). Sketchfab login via Playwright is authorized if a
must-have model is login-gated; prefer no-login sources first (licensing table in
`docs/industrial_autogen_notes.md` §4).

---

## Git state

- Branch `batch-migrate-pipeline`. Committed this session:
  - `1f5e746` — footprint-pose placement mode (place_engine 'footprint' source),
    generalized glTF migrator (`--gltf/--texdir/--name/--allow-winding`),
    fsarchive-decryption-impossible correction, `docs/objects/msfs_object_recovery.md`,
    `make_debug_apt_skopje.py`. Manifest = 4 verified objects.
  - (this commit) — `scripts/generate_pylons.py` + this handover.
- NOT committed: `scripts/generate_bridges.py` (bridge agent was still finalizing at
  interruption — verify then commit), `dogfight/` (unknown, not ours, leave).
- Installed-but-not-in-git (by design, install-only rule): everything under
  `C:/Condor2/Landscapes/MacedoniaSkopje/World/` and `.sandbox/`.

## Installed landscape state (MacedoniaSkopje)

- `MacedoniaSkopje.obj` = 4 records (608 B): StenkovecHangar, Cessna172,
  MillenniumCross, TelecomTowerAEK — all placed via `place_engine.py`, overlays
  verified this session (`.sandbox/placement/*_engine.png`).
- **WARNING: installed `.apt` has 4 records incl. `ZDebugSkopjeCity`** (debug virtual
  airport over the city landmarks, useful for in-sim object inspection). BEFORE SHIP:
  `python scripts/generate_apt.py` to restore the real 3, then regenerate
  `.bmp/.tdm/.cup` (apt-freshness rule 10) via `python scripts/build_landscape.py`,
  then `verify_landscape.py`.
- `World/Objects/` also holds stale `StenkovecHangar.c3d.bak_*` backups (harmless).
- 8 landmark c3ds exist in `.sandbox/landmarks/` (self-made — user wants downloads
  instead); 4 CC stand-ins in `.sandbox/citymodels_c3d/` (were installed once, then
  pulled during the placement-precision rework; their DDS still sit in World/Textures).

## Background-agent outputs (all sandbox-only, TRUE UTM 34N coords)

| Stream | State at interruption | Verify before use |
|---|---|---|
| **Pylons** | **DONE + QA-verified.** `scripts/generate_pylons.py` (committed), `.sandbox/pylons/placements.json` = 785 towers (400kV 122 @scale1.0, 220kV 103 @1.0, 110kV 560 @0.75), `report.json`, `qa/` (overview + 2 texture-overlay closeups, agent-inspected, on-corridor within 10 m). **Convention: `ori_deg` aims model +Y = LINE azimuth (arms ⟂ wires); `arm_az_deg = ori+90` also recorded.** Model: `.sandbox/industrial/pylon.c3d` (18×9.33×40.25 m). Deterministic (SHA-verified reruns). | spot-read the 2 closeups |
| **Bridges** | **DONE + QA-verified** (agent completed, all 12 overlay QA pngs inspected: decks 1–2 px on the painted crossings; rail viaducts dead-centred over 0.7–1.4 km). `scripts/generate_bridges.py` (committed), `.sandbox/bridges/`: **357 c3ds** (146,344 verts total), `placements.json` (E/N = TRUE UTM centroid, **ori MUST stay 0 — mesh carries the bearing**, deck_z=7.0), shared `bridges.dds`, `report.json` (histogram + 1086-skip list: 1044 <40 m, Stone Bridge name-matched out, 6 foot-decks deduped vs road decks, 33 central-Vardar crossings kept incl. Goce Delčev/Kiro Gligorov/Art Bridge). Deterministic (sha256-identical reruns); all c3ds round-trip byte-exact. OSM cache `.sandbox/osm/bridges.geojson`. | none — done; just integrate |
| **Industrial** | **FAILED — agent hit the API session limit** (resets ~05:10 Europe/Skopje) after exploratory reads; NO deliverables (`scripts/place_industrial.py` and `.sandbox/industrial/placements_refined.json` do NOT exist). Re-run from scratch. Spec: per-structure instances from `.sandbox/industrial/osm_industrial.json` (49 tanks, 43 silos, 31 chimneys, 2 cooling towers, ~60 largest halls with long-axis ori; OKTA +3 columns +1 flare as 'prior'; skip halls within 400 m of LWSK/LWSN/LW67), scale=real/native clamped [0.4,2.5], TRUE-UTM records, QA overlays for the 5 hero sites (OKTA/TE-TO/Makstil/USJE/Jugohrom) using the verified `footprints_to_obj`/`validate_bridge` texture georef. Models already staged: `.sandbox/industrial/*.c3d` + `industrial_models.json` (native dims). | re-run the brief; inspect all 5 site QA pngs |

**Integration (task #11, not started):** write `scripts/install_autogen_placements.py`
— bulk installer for the three placements.json sets. CRITICAL: `place_engine.py
--commit` dedups .obj records BY C3D NAME → unusable for multi-instance autogen
(Slovenia2 = 7,496 records of 64 models). The bulk installer must APPEND repeated-name
152-byte records: `posX,posY = condor_grid.obj_record_xy(*condor_grid.painted_texture_xy(E,N))`,
z = DEM (`place_engine.dem_alt`), ori per stream convention above, backup `.obj`
first, idempotent (regenerate the whole autogen block each run; keep the 4+ hero
records). Copy c3ds+DDS to `World/Objects` + `World/Textures` (paths inside c3d must
be full `Landscapes\MacedoniaSkopje\World\Textures\<f>.dds`). Objects need NO re-hash.
Consider `.obj` budget: 785 pylons + ~30 bridges + ~200 industrial ≈ fine per
`docs/building_autogen_design.md` §1 (draw calls dominate; these are 1-object c3ds).

## Stenkovec SoFly object set (task #2 — main thread, mid-flight)

**Ground truth established this session (do not re-derive):**
- SoFly geometry is DRM-locked (verified dead end; `docs/objects/msfs_object_recovery.md`).
  Chosen route (doc §6, MAX-QUALITY-compatible): parametric shells with EXACT traced
  footprints + real unpacked 4096² SoFly albedo materials for walls + photo roof.
- Viewable atlas PNGs: `.sandbox/sofly_view/*.png` (all 20+ converted).
- **Atlas→building assignment (decided):**
  - `LW75_3_*` (dark facades) + `3_ROOF` → restaurant complex = OSM 207 m² @
    (531750.3, 4656460.0) + 214 m² @ (531769.5, 4656441.1), dark hip roofs, terrace
    with chair rows to its S (visible in ortho ≈ 531757, 4656427).
  - `LW75_1_*` (BLUE walls + blue-white CHECKERED band) → club building 617 m² @
    (531854.1, 4656390.1).
  - `LW75_2` (grey strip facades) → long white shed 395 m² @ (531696.0, 4656392.9).
  - `LW75_WINGAIR` (concrete + red rust doors + vertical "WINGAIR TEAM" banner) →
    Wingair Team hall 620 m² @ (532148.7, 4656603.3) — **OSM square is offset from the
    true L-shaped roof; trace roof from Esri, register on painted texture.**
  - `PIPISTREL_1/2/3` = **PFC Pipistrel Flying Club BUILDING** (not liveries!) 681 m²
    @ (532036.2, 4656376.1), OSM height=8 m/2 lvl, grey walls + blue-grey metal roof.
  - `LW75_WORKING_HOURS_SIGN` (512², clean bilingual gate sign) → gate pillar at the
    compound entrance (S of restaurant parking, trace ≈ 531773, 4656410).
  - `LW75_FENCE_1` (precast concrete panels) → WingAir compound perimeter (visible
    enclosure) + entrance fence runs.
  - `LW75_CHAIRS` (bench/table strips) → terrace rows S of restaurant.
  - `LW75_WINGAIR_CONTAINER` → small white container W/SW of WingAir hall.
  - **Detected props (computational, Esri z19 exact-window):** chapel candidate 20 m²
    @ E 532129.4 N 4656593.4 az 42°; container 6 m² @ E 532132.8 N 4656590.9.
    Renders: `.sandbox/sofly_view/_detect_wingair.png`, `_detect_restaurant.png`
    (restaurant one saved but NOT yet inspected).
  - `BITOLA_*`/`BITOLSKIO_*` = Bitola airfield (OUTSIDE 12×12 grid — skip).
    `ENTRANCE/ENTRANCE_2` = beige domed complex, NOT Stenkovec (unidentified — skip).
    `BEAMS`, `1/2/3/4` numbered = restaurant-complex detail parts (pergola/walls) —
    optional material sources.
- **Roof texture source decision:** the z11 cadastre cache
  (`.sandbox/textures_mk2023_z11/`) has a BROKEN/unresolved georef for object-level
  crops (verified: hangar + Stone Bridge windows show wrong scenes; whole-cache
  mosaic incoherent — see `.sandbox/sofly_view/_cache_mosaic.png`). DO NOT burn time
  there. Use `scripts/satellite.py fetch_window` (Esri z19, georef VERIFIED against
  OSM/hangar) for roof crops, color-matched (LAB mean/std) to the installed texture
  crop so objects blend with the painted ground. This is the sanctioned fallback
  (quality-standards.md). Flag: investigate the z11 cache mapping someday.
- **Placement:** buildings with OSM footprints → place_engine legacy `osm`/`footprint`
  modes (slide-refines against the painted texture); props (chapel/container/sign/
  chairs/fence) → `static` entries with Esri-traced coords converted via
  `condor_grid.painted_texture_xy` (frames agree ≈2–3 m — hangar overlay is the proof).
  Build all geometry WORLD-ALIGNED (mesh carries bearing, ori=0), origin = footprint
  centroid, base z=0, CCW-outward, WHITE_MATERIAL, one C3DObject per texture (usually
  1), full texture path. Round-trip via `scripts/c3d.py`, gate via
  `scripts/validate_model.py`, QC render, then manifest + `place_engine.py --only ...`
  dry-run → READ the overlays → `--commit`.
- Next concrete step was: write `scripts/build_sofly_objects.py` with the spec table
  above (exact-window Esri roof crops + atlas wall regions; pattern for the Mesh
  accumulator exists in `scripts/build_industrial_models.py`).

## Other pending tasks (task list ids)

- **#3 apron gliders:** `.sandbox/reuse/{Cobra15,Grob103}/` staged c3ds → static
  manifest entries on the Stenkovec apron near the hangar/Cessna (Cessna at painted
  E 531820, N 4656503 for reference spacing), dry-run overlay, commit. 10-minute task.
- **#4 landmarks (downloads):** replace self-made `.sandbox/landmarks/*.c3d` where a
  real model exists. Search order per skill §1 (CC0 → CC-BY → Trimble personal-only).
  `MillenniumCross_gold.c3d` (from 3D-Warehouse .skp) already staged. Sketchfab needs
  login (user authorized, see top). Batch-convert via `scripts/batch_migrate.py`
  manifest (`.sandbox/citymodels/migrate_manifest.json` shows the pattern), validate
  via `validate_model.py`, place via `footprint` mode entries (near_utm seeds are in
  the git history of `data/placement_manifest.json` @ commit 41fa64c/41fa46c — `git
  show 41fa46c:data/placement_manifest.json` lists all 8 landmark coords).
- **#8 LWSK:** no SoFly scenery exists. Terminal/tower/cargo from footprints + Esri
  roofs + parametric facades (same builder as SoFly set), or downloaded models.
- **#9 Kumanovo LW67:** SoFly textures exist (`LW67_BUILDING_1`, `LW67_HANGAR`,
  `LW67_CONSTRUCTION_GUARDHOUSE`, `LW67_CONSTRUCTION-WELL-CONTAINER` in the same
  texture dir) + Slovenia2 gliding hangars (`C:/Condor2/Landscapes/Slovenia2/Airports/
  *G.c3d`, 84 World/Objects c3ds) as reuse donors (install-only, never git).
- **#10 ship:** restore .apt (see WARNING above) → build_landscape metadata pass →
  `verify_landscape.py` full → in-sim flight-planner check (elevated pyautogui
  method, memory `reference_condor_automation`; debug airport helps inspect city
  objects before removal) → commit/push → PR to master.

## Key commands

```bash
python scripts/place_engine.py [--only ID,ID] [--commit]   # overlays in .sandbox/placement/
python scripts/validate_model.py <c3d> --texdir <dir>       # model quality gate
python scripts/batch_migrate.py --manifest <json>           # any-format → c3d+DDS
CONDOR_LANDSCAPE=skopje python scripts/build_landscape.py   # orchestrator (metadata fast path)
python scripts/verify_landscape.py                          # ship gate
tools/CLT2.7/LandscapeEditor.exe -hash MacedoniaSkopje      # ONLY after .tr3/.for changes (not needed for objects)
```
