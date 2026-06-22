---
name: migrate-objects-to-condor
description: >-
  Use when adding 3D objects / buildings / landmarks / airport scenery to a Condor 2
  landscape — i.e. FIND a model, DOWNLOAD/EXTRACT it, MIGRATE it (any 3D format → Condor
  .c3d + DDS), and PLACE it on the building as painted in the game texture. Reusable for
  ANY city or airport (Skopje, Ohrid, Bitola, …) and ANY source (FS2020/MSFS scenery via
  the SDK, CC0/CC-BY models online, or objects from other installed Condor landscapes).
  Triggers: "add the city landmarks", "migrate the FS2020 airport objects", "place the
  hangars", "populate Ohrid with custom objects", "find and place a 3D model of <X>",
  "convert this glb/obj/skp to Condor", "put the Cessna on the apron".
---

# Migrate & place 3D objects into a Condor 2 landscape

ONE reusable pipeline: **SEARCH → ACQUIRE → MIGRATE → PLACE → VALIDATE**. The *thinking*
steps (search, choose the best model, resolve which side faces where) are LLM-driven;
**migration and placement are DETERMINISTIC services** (tested scripts) — never reinvent
them. Full source/tool catalog + the algorithm rationale: `docs/OBJECT_PLACEMENT.md`;
the hard-won failure analysis: `docs/POSTMORTEM_hangar_placement.md`.

> **Golden rule (the day-long lesson):** an object must land on the building **as
> PAINTED in the installed `tCCRR.dds`** — the exact raster Condor draws. Do NOT place
> from raw lat/lon and do NOT validate against any *other* raster (cadastre ortho, Esri,
> Google). Match the footprint *in the game texture*, anchor by the **geometric**
> centroid, use the model's **native** size, and overlay-verify on that same texture
> before committing.

---

## 0. The deterministic services (always call these)

| Service | Script | What it does |
|---|---|---|
| **MIGRATE (any format)** | `scripts/batch_migrate.py` | glTF/GLB/OBJ/SKP/FBX → textured `.c3d` + DDS. Blender bake (`glb_to_baked.py`) + `nvcompress` + `obj_to_c3d.py`, round-trip-verified, parallel. Manifest-driven. |
| **MIGRATE (MSFS glTF fast path)** | `scripts/migrate_stenkovec_objects.py` | Asobo glTF → textured `.c3d` (handedness-preserving, winding-fixed, per-albedo DDS). *Currently hard-coded to one glTF — generalize to a `--gltf <path> --name <id>` arg when batching.* |
| **PLACE (deterministic, tested)** | `scripts/place_engine.py` + `scripts/mount_engine.py` | Registers the model footprint to the painted building in the installed texture; geometric-centroid anchor, native size, front cue. Multi-object, accumulates `.obj` records, backs up. Tests: `tests/test_mount_engine.py` (8 pass). |
| **VALIDATE** | overlay PNGs in `.sandbox/placement/<id>_engine.png` | The engine renders the placed footprint on the game texture; Read it and confirm it sits on the painted building. |

Run placement: `CONDOR_LANDSCAPE=skopje python scripts/place_engine.py --only <ID,...> --commit`
(omit `--commit` for a dry-run + overlays).

---

## 1. SEARCH (LLM)  — pick the best model per object
For each target (a landmark name, or every object in an airport's scenery), in priority:
1. **The user's owned source** — FS2020/MSFS scenery (`F:/FS2020/...`), or an installed
   Condor landscape (`C:/Condor2/Landscapes/Slovenia2/...`). Highest fidelity, already
   to-scale. These are **personal/licensed → install-only, never git.**
2. **CC0** (Kenney, Quaternius, Poly Pizza CC0, OpenGameArt) — public-shippable, no login.
3. **CC-BY** (Sketchfab/Poly-Pizza/CGTrader-free) — public-shippable *with attribution*
   (keep `.sandbox/citymodels/CREDITS.md`); usually needs a login.
4. **Parametric build** — when no model exists (most brutalist icons): build real geometry
   from the footprint + reference drawings (architectuul.com, sosbrutalism.org), photo-
   facade DDS. Not a generic box (rule #11).
Avoid Trimble 3D-Warehouse (GML) and Sketchfab-Standard/RF for **public** release — those
are personal-only. Record the source + license per object.

## 2. ACQUIRE
- **CC0/CC-BY no-login:** download the direct file (`static.poly.pizza/<uuid>.glb`, etc.).
- **Login-gated (Sketchfab):** drive a browser (Playwright MCP) to the model page; if it
  needs sign-in, register/login with the user's readable mailbox and complete the email
  verification, then download. If a **passkey/2FA** blocks you, queue that one tap for the
  user — don't loop.
- **FS2020 encrypted `.fsarchive` (RASA):** decrypt with `fspackagetool.exe` from the MSFS
  SDK (`<SDK>\Tools\bin\`) on a COPY of the owned archive: `fspackagetool -unpack <copy> <out>`.
  Loose `.gltf` SimObjects need no decryption. (Install the SDK via MSFS Dev Mode →
  "Discover the SDK" if absent.)

## 3. MIGRATE  →  lower-quality-than-original, Condor-optimal
Feed each acquired model to `batch_migrate.py`. Honour the licensing condition (keep it
clearly **below** the source's fidelity): decimate to Condor scale, single DDS per object,
white material RGB 1/1/1, store the **full** `Landscapes\<Name>\World\Textures\<file>.dds`
path in the `.c3d`. Output → `.sandbox/<target>/` then copy `.c3d`+`.dds` into the install's
`World/Objects/` + `World/Textures/`.

## 4. PLACE  — manifest-driven, deterministic
Add a manifest entry per object and run `place_engine.py`:
- **Building with a painted roof** → `"footprint":{"source":"osm",...}` (or a cadastre/MS
  polygon): the engine matches it *in the game texture*. Give `front.model_groups` (the
  door/façade object-name substrings) + `front.world_azimuth` to resolve the 180°.
- **Monument / tower / aircraft (no roof)** → `"footprint":{"source":"static","E":..,"N":..,"ori":..}`
  in OBJECT-grid UTM. Convert lat/lon: `E,N = to_utm(lon,lat); oe,on = condor_grid.painted_texture_xy(E,N)`.
- `scale_mode:"native"` for to-scale migrated models; footprint-fit for generic shells.

## 5. VALIDATE (game-free) then in-sim
Dry-run, **Read every `.sandbox/placement/<id>_engine.png`**, confirm the footprint sits
on the painted building. Commit only what passes; objects need **no hash** — reload the
landscape. Then the human confirms in-sim.

---

## Licensing & repo hygiene (non-negotiable)
- **Binary objects (`.c3d`/`.dds`) go into the Condor install only — NEVER `git add` them.**
  `.sandbox/` is git-ignored; the install lives in `C:/Condor2/` (outside the repo). The
  **public repo carries only the pipeline + a manifest of SOURCES**, so the build is
  reproducible without redistributing anyone's assets.
- FS2020/Slovenia2/Trimble assets = the user's owned/licensed content → personal,
  install-only. CC0/CC-BY = public-shippable (CC-BY needs the CREDITS line).

## Reuse for a new city/airport (e.g. Ohrid)
1. `CONDOR_LANDSCAPE=<sel>` selects the grid (all transforms reparameterise automatically).
2. List the dozen landmark targets (+ any FS2020 airport scenery).
3. Run SEARCH→ACQUIRE→MIGRATE→PLACE→VALIDATE above; the same `place_engine.py` and
   `batch_migrate.py` work unchanged — only the manifest + sources differ.
