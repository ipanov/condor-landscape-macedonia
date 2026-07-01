# Recovering the SoFly LWXX Stenkovec objects from MSFS (GPU-geometry capture)

Status of the SoFly *LWXX Skopje Airfield Collection* (MSFS2020), an add‑on the user
**owns** (personal, install‑only — never committed). Goal: get the ~32 airfield objects
(restaurant, WingAir building + church, fences, containers, sign, chairs, LW67 building +
hangar + guardhouse + well, Pipistrel/Bitola liveries) into Condor 2 as textured `.c3d`.

This doc is the verified method, the exact capture procedure, what is autonomous vs. what
needs the user, and the honest yield/effort vs. the reuse/parametric alternative.

---

## 0. The two-part inventory (what's already recoverable vs. what's locked)

| Group | Where | Geometry | Status |
|---|---|---|---|
| **Main glider hangar** (Aeroklub Skopje) | `SimObjects/Landmarks/stenkovec-hangar/model/LW75_Main_Hangar.gltf` (+`.bin`) | **OPEN glTF 2.0** — 84,248 verts / 59,016 tris, 9 albedo DDS | **DONE.** Migrated, textured, winding‑fixed → `.sandbox/airport_objects/StenkovecHangar.c3d` (round‑trip byte‑identical). Not in the archive. |
| **~32 scenery objects** | compiled into `scenery/lixmycig.fsarchive` (23.7 MB) | **ENCRYPTED** (RSA‑2048, magic `RASA` v2.3) | Geometry locked. **Textures are unpacked** (32 `*_ALBD.PNG.DDS`, all 4096² except chairs/fence/container 2048² and the sign 512², in `scenery/Stenkovec/LW75/texture/`). |

So the **single biggest object (the hangar) is already done**; the recovery effort below is only
for the remaining 32 scenery objects whose meshes are inside the encrypted archive.

The 32 locked albedo skins (the object list, by texture stem):
```
1 2 3 4  BEAMS  ENTRANCE ENTRANCE_2
LW75_1_1 LW75_1_2 LW75_2 LW75_3_1 LW75_3_2 LW75_3_3 LW75_3_ROOF   (restaurant)
LW75_CHAIRS LW75_FENCE_1 LW75_WORKING_HOURS_SIGN
LW75_WINGAIR LW75_WINGAIR_CHURCH LW75_WINGAIR_CONTAINER           (WingAir bldg + church)
LW67_BUILDING_1 LW67_HANGAR LW67_CONSTRUCTION_GUARDHOUSE LW67_CONSTRUCTION-WELL-CONTAINER
PIPISTREL_1 PIPISTREL_2 PIPISTREL_3
BITOLA_1 BITOLA_2 BITOLA_3 BITOLSKIO_1 BITOLSKIO_2
```

---

## 1. Why decryption / the SDK are dead ends (don't reattempt)

- **`.fsarchive` is one‑way DRM.** `fspackagetool.exe` (SDK, `E:/MSFS_SDK/MSFS SDK/Tools/bin/`)
  is a **compiler only** — its own usage is `fspackagetool <project .xml> [-rebuild] [-mirroring]`.
  There is **no `-unpack`/`-extract`/`-decrypt` verb**. (Verified directly: ran the tool, read its
  usage. The earlier guess in `scripts/extract_fsarchive.py` that `-unpack` exists is **wrong** —
  it does not.) The archive is encrypted at install with a Microsoft‑Store license key not in the file.
- **MSFS Dev Mode cannot re‑emit an installed encrypted package.** The Project/Scenery Editors are
  source‑project compilers; "Load in Editor" only loads **your own project's** asset groups, "Save
  Scenery" writes placement BGL (not geometry), and there is **no "export loaded object to glTF"**
  anywhere in Dev Mode. The Asobo Blender add‑on **explicitly refuses** to import packaged models
  ("cannot import glTF files that have been built into a … package through the Package Builder").
  `fsdevmodelauncher.exe` only fast‑launches the sim (skips intro videos) — no export.
  Sources: docs.flightsimulator.com (Using_The_SDK, The_Project_Editor, Using_The_Scenery_Editor),
  github.com/AsoboStudio/glTF-Blender-IO-MSFS.
- **No community `.fsarchive` decryptor / MSFS scenery‑object ripper exists.** ModelConverterX reads
  only *un*encrypted object BGLs. (fsdeveloper.com, scenerydesign.org.)

**The only remaining route to the locked geometry is to capture it from the GPU while MSFS renders it.**

---

## 2. The chosen capture method

**RenderDoc 1.42 (DX11 frame capture).** Rationale, all source‑grounded (2024‑2026):

- RenderDoc reliably **exports per‑draw mesh data (POSITION + TEXCOORD + indices)** from a captured
  frame (Mesh Viewer → VS Input → Export to CSV, or the Python API). UVs recover. (renderdoc.org docs.)
- **Force MSFS to DX11.** MSFS's own in‑sim capture tool produces **black frames under DX12**, and
  RenderDoc had a DX12 "device‑removed" capture regression in 1.43 → **we installed 1.42** (the last
  build before that regression). (devsupport.flightsimulator.com; github.com/baldurk/renderdoc#3815.)
- Nsight Graphics is the DX12 fallback if DX11 capture fails, but it has the same object‑separation
  limit (below) and a clunkier export, so RenderDoc/DX11 is the primary.

### The hard limitation (set expectations before capturing)
A GPU frame is delivered as **draw calls split on MATERIAL boundaries, in object‑LOCAL space** — *not*
as clean, world‑placed buildings:
- One physical building with N materials → **N separate fragments**; adjacent objects sharing an
  atlas can land in **one** draw. There is no "select the church" button — you hand‑pick draws.
- Vertices are **local/model space** (each fragment sits at its own origin). To place an object you
  read its **model matrix from the draw's constant buffer**; instanced scenery needs the per‑instance
  matrix array. (renderdoc.org Mesh Viewer; fsdeveloper.com threads.)
- **No documented success capturing `MSFS.exe` directly** — the community rips photogrammetry from a
  *browser*, not the sim. UWP/anti‑tamper may block injection and EasyAntiCheat may CTD the sim. Treat
  a successful in‑sim capture as **likely‑but‑unproven**; if injection is blocked, this route ends and
  the reuse/parametric alternative (§6) is the answer.

**Net:** RenderDoc yields *object‑space, per‑material triangle fragments with UVs that one person
assembles by hand, per object* — useful, but far from a turnkey "export building.obj".

---

## 3. Toolchain — installed & verified (autonomous, done)

| Tool | Location | State |
|---|---|---|
| **RenderDoc 1.42** | `C:/Program Files/RenderDoc/qrenderdoc.exe` (+`renderdoccmd.exe`) | **Installed** (silent MSI). `renderdoccmd version` → `v1.42`, exit 0. Bundles Python 3.6 for the in‑app scripting console. MSI cached at `.sandbox/tools/RenderDoc_1.42_64.msi`. |
| nvcompress | `C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe` | present (used by the migrator) |
| Blender + Asobo MSFS glTF I/O | (user) — only needed to re‑export captured fragments to glTF | user installs if pursuing capture |
| Repo migrator (generalized) | `scripts/migrate_stenkovec_objects.py` | **generalized** to take any glTF (below) |

---

## 4. EXACT capture procedure (REQUIRES the user — it's their licensed sim + GUI)

Autonomous setup is done (§3). The capture itself **must be the user** — it needs MSFS flown to the
airfield and a frame grabbed in the live, licensed GUI; do not automate the elevated MSFS window.

**A. Prep MSFS (one time)**
1. MSFS → Options → Graphics → **Rendering = DirectX 11** (NOT DX12 — DX12 capture is black). Apply, restart MSFS.
2. **Disable every overlay** (Xbox Game Bar, Steam, GeForce Experience/ShadowPlay, RTSS/Afterburner) and DLSS/FSR/XeSS — overlays break captures, upscalers corrupt geometry.

**B. Inject RenderDoc**
3. Launch `C:/Program Files/RenderDoc/qrenderdoc.exe`. Because MSFS is already running elevated under
   the Store/UWP sandbox, prefer **File → Inject into Process → FlightSimulator.exe** (vs. launch‑capture,
   which the UWP packaging usually blocks). If inject lists no PID / fails → injection is blocked;
   **stop and use §6**. (Don't loop on it.)

**C. Fly to the objects**
4. Free flight, depart/teleport to **Stenkovec LW75 — 41.0597 N, 21.3849 E** (and LW67 Kumanovo
   42.1578 N, 21.6939 E for those objects). Park on the apron, **camera close** so full‑detail LOD0 is
   resident (distant LODs are decimated — capture close or you rip a crude LOD).
5. Frame the target object(s) tightly in view; minimise other scenery in frame to reduce draw clutter.

**D. Capture**
6. In the RenderDoc overlay press **F12** (or PrtScn) to grab the frame. Repeat per object/viewpoint —
   one tight capture per building beats one wide capture of everything.

**E. Extract meshes (back to autonomous once a `.rdc` exists)**
7. Open the `.rdc` in qrenderdoc → **Event Browser**: walk draw calls, use the **Mesh Viewer** +
   the texture preview to identify which draw(s) are your object (match the bound albedo DDS name —
   e.g. `LW75_WINGAIR_CHURCH` — to the object).
8. For each target draw: **Mesh Viewer → VS Input → right‑click → Export to CSV** (positions+UVs+indices),
   and note the bound albedo texture. For many draws, script it in the **Python console** (qrenderdoc)
   instead of clicking. Read the draw's **model matrix** (Pipeline State / constant buffer) if you need
   world placement — though our placement engine re‑derives position from the texture anyway (§5), so
   local‑space geometry is fine.
9. In **Blender**: import the CSVs (e.g. the blender‑renderdoc‑csv‑importer), delete non‑target
   fragments, **join** the object's fragments into one mesh, keep/clean the UVs, and **export glTF 2.0**
   (`<object>.gltf` + `.bin`). One glTF per object.

---

## 5. Feed the repo pipeline (autonomous, ready)

`scripts/migrate_stenkovec_objects.py` was **generalized** from hangar‑hardcoded to take any glTF.
Default (no args) still migrates the open hangar **byte‑identically** (verified: MD5 unchanged,
84,248 v / 59,016 t, round‑trip OK). For a captured/exported object:

```bash
# the texture dir is the UNPACKED SoFly scenery textures (already on disk):
TEX="F:/FS2020/Official/OneStore/sofly-lwxx-airfields/scenery/Stenkovec/LW75/texture"

python scripts/migrate_stenkovec_objects.py \
    --gltf .sandbox/captures/LW75_WINGAIR_CHURCH.gltf \
    --texdir "$TEX" \
    --name StenkovecChurch \
    --allow-winding        # ripped glTF winding differs from the MSFS hangar's; inspect the QC render
```
It groups primitives by albedo image → one `C3DObject` per texture, maps glTF(X=right,Y=up,Z=back) →
Condor(X=East,Y=North,Z=up) handedness‑preserving, converts each referenced MSFS DDS → Condor DXT1/5,
and writes `.sandbox/airport_objects/<name>.c3d`. Note the albedo the glTF references must match a file
in `--texdir` (the captured material name should equal the SoFly stem, e.g. `*_ALBD.PNG.DDS`).

Then the existing object‑agnostic gates/placement (unchanged):
```bash
python scripts/validate_model.py .sandbox/airport_objects/StenkovecChurch.c3d --texdir .sandbox/airport_objects
# add the object to data/placement_manifest.json (c3d + footprint seed + long-axis prior), then:
python scripts/place_engine.py --only StenkovecChurch          # registers footprint to the painted texture
python scripts/place_engine.py --only StenkovecChurch --commit # writes the .obj placement
```
`place_engine` re‑derives **position + bearing** by registering the model footprint to the building as
painted in the installed ortho texture, so the capture's missing world transform is not a blocker.

---

## 6. Honest yield/effort vs. the reuse/parametric alternative — RECOMMENDATION

**RenderDoc capture, realistic yield:** **low‑to‑moderate, high manual cost, and gated on injection
even working.** Best case per object ≈ 20–60 min of Blender fragment‑surgery (identify draws → delete
neighbours → join → fix UVs → export), times ~32 objects, *plus* the unproven risk that UWP/EAC blocks
injection outright (then yield = 0). You'd get triangle‑soup‑ish, possibly‑duplicated, LOD‑dependent
meshes — quality well below the clean open‑glTF hangar.

**Reuse alternative — already available on disk:** the installed **Slovenia2** library
(`C:/Condor2/Landscapes/Slovenia2/`) has **84 generic `.c3d`** incl. `World/Objects/Bled_church.c3d`,
`Church Urlsja Gora.c3d`, and **8 airport gliding hangars** (`Airports/Lesce-BledG.c3d`, `CELJEG.c3d`,
`AjdovscinaG.c3d`, … 22–88 KB) — a ready stand‑in for **LW67 hangar / building / guardhouse** and a
**church**. Gliders already staged in `.sandbox/reuse/` (Cessna172, Cobra15, Grob103). These drop
straight into `place_engine` with zero capture.

**Parametric build:** for the **restaurant, fences, containers, sign, Pipistrel/Bitola panels** — simple
boxes/planes — a small parametric generator skinned with the **already‑unpacked 4096² albedo DDS** gives
a *better, cleaner* result than a GPU rip (correct UVs, exact footprint from the texture, full‑res skin),
for a fraction of the effort. The textures we have are the high‑value part; the geometry is trivial.

### Recommendation (tiered, MAX‑QUALITY‑compatible)
1. **Hangar — keep the open‑glTF migration (done).** Highest‑detail real model; no capture needed.
2. **Restaurant / WingAir bldg / church / fences / containers / sign / Pipistrel‑Bitola panels —
   parametric build skinned with the unpacked albedo DDS.** Cleaner UVs + exact texture‑derived
   footprint than a rip; this is the recommended default for the 32.
3. **LW67 hangar/building/guardhouse — reuse the Slovenia2 gliding‑hangar/building generics** (instant,
   proven), or parametric if a closer match is wanted.
4. **RenderDoc capture — reserve for the few objects where a parametric box is genuinely inadequate**
   (a distinctive silhouette the user insists on), and **only after** confirming injection works (step B).
   Treat it as a targeted last resort, not the bulk method.

This respects the quality mandate (real highest‑detail hangar + full‑res unpacked skins + exact
texture‑detected footprints) **without** betting the whole 32‑object effort on an unproven, manual GPU
rip. The toolchain for capture is installed and the migrator is ready *if* a specific object needs it.
