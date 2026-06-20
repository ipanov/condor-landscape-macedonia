# Condor 2 — Airport Creation Workflow (authoritative, generic)

Region-agnostic procedure for adding airports to **any** Condor 2 landscape. Verified
against primary sources; do **not** create airports by copying another landscape's files.

## Sources (verified)
- **Condor Landscape Guide ENGLISH rev2** (official) — `docs/reference/Condor_Landscape_Guide_rev2.pdf`
  (+ extracted `docs/reference/_guide_text.txt`). Airport section ≈ p.18.
- **SoaringTools "Condor Scenery Tutorial" rev18** — `docs/reference/Condor_Scenery_Tutorial18.pdf`
  (+ `_tutorial_text.txt`). Airport chapter pp.9–24 (Airport Maker → ObjectEditor).
- Condor forum: "Use generic files" is a dead CST1 relic in C2 — viewtopic.php?t=22029.
- Condor forum: Landscape Editor writes `.apt`/`.obj`, never the c3d — viewtopic.php?t=19009.
- Condor forum: ObjectEditor OBJ import rules — viewtopic.php?t=17819.

## The two tiers of airport

| Tier | Files in `Airports/` | Start modes | When |
|------|----------------------|-------------|------|
| **Virtual** (default) | *none* | **Airborne only** | Instant; airport exists from `.apt` alone |
| **Real runway** | `<Name>G.c3d` + `<Name>O.c3d` | Airborne **+ ground/tow/winch** | Landable runway |

> Guide rev2 (p.18, verbatim): *"the airport will be purely **virtual**, as there will be no 3D
> model placed in the landscape. However, airport data defined in the Landscape editor will allow
> us to start in the air over the airport position."* … *"There is no runway and the terrain is not
> adjusted for tow or winch take-offs. **Use airborne start** until [the 3D model is created]."*

## "Airport is not installed" — cause and rule

- **Cause:** a `<Name>G.c3d`/`<Name>O.c3d` is present but **mismatched** — i.e. copied from another
  airport/landscape, so its geometry/identity does not match the `.apt` airport. Condor rejects it
  while *"Creating home airfield"* and shuts down.
- **NEVER** copy another landscape's c3d (e.g. Slovenia2/PTUJ). That is the failure, not the fix.
- A **missing** c3d does **not** crash — it yields a valid virtual airport (airborne start).
- **Naming is exact:** the c3d base name must equal the `.apt` airport name + `G`/`O` suffix
  (spaces allowed, e.g. `Skopje InternationalG.c3d`). The model origin `0,0,0` = **runway center**.

## Creating a REAL runway (the only sanctioned path — no reverse engineering)

### 1. Define the airport (Landscape Editor) — writes `.apt`, not the c3d
- `View/Modify` → check **Airports** → right-click the list → **Add**.
- Fields: name, **center** Lat/Lon (decimal °, middle of runway), elevation (m),
  runway length (m), width (m), direction (° true, decimal since LE 2.0.1+).
- Do **not** use right-click **"Use generic files"** — dead CST1 relic, fails in C2.

### 2. Flatten the runway terrain (Landscape Editor) — separate from the c3d
- `Height Map` view → zoom to runway → **FLATTEN** icon.
- radius slightly wider than runway; altitude = airport elevation; edge slope ≈ **1:3**.
- Airport `altitude` in Properties **must equal** the flatten altitude or the runway floats/sinks.
- After flattening, **re-export the terrain hash** (`.tha`).

### 3. Generate the runway model (Airport Maker, Jiří Brožek) — grass strip
- Download from SoaringTools (https://www.soaringtools.org/index.php/downloads/); unpack.
- Enter runway width & length **in meters**; markers/grass/windsock options.
- **Generate OBJ Files** → save into the landscape's `Airports/` folder, filename = airport name.
  Produces `<Name>G.obj/.mtl` and `<Name>O.obj/.mtl`.
- Asphalt runways: use **Condor_Tiles → Simple Objects** (Asphalt + Pole + Windsock) instead.

### 4. Convert OBJ → C3D (ObjectEditor.exe, in `tools/CLT2.7/`)
- `File → Open Obj` → `<Name>G.obj` → `File → Save to C3D`. Repeat for `<Name>O.obj`.
- Result: `<Name>G.c3d` + `<Name>O.c3d` — *"the only two files required in the Airports folder."*
- **OBJ import rules** (else import fails): triangulated mesh; **one material + one texture per part**;
  material RGB = 1/1/1; decimal separator `.` (not `,` — locale trap); texture paths point under
  `Landscapes/<Name>/World/Textures/`.

### 5. Finalize
- Re-export hashes after any terrain/forest change: `tools/CLT2.7/LandscapeEditor.exe -hash <Name>`.
- `G` = ground (smooth, glider rolls on it). `O` = objects (windsocks/buildings, **crashable**;
  windsock proxies named `Windsack1/2/3`).
