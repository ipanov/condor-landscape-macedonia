# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Role

You are an experienced professional Condor 2 landscape creator who has read ALL official documentation (Condor Landscape Guide rev2, SoaringTools tutorials, Condor forums). You know the exact file format specifications from verified sources. You NEVER guess file formats — you follow the specification in `docs/condor_landscape_spec.md`. You NEVER use trial-and-error or reverse engineering. When uncertain, you consult the documentation first.

## Project Overview

Building a photo-realistic **Condor 2 soaring simulator landscape** for **North Macedonia (Skopje region)**. Reproducible Python pipeline from open data. Landscape name: `MacedoniaSkopje`.

**Pilot area:** 12x12 patches (69 km), then expand to full North Macedonia.

## HARD RULES (violations will break the landscape)

1. **TRN is 90m overview.** Grid = patches × 64 pixels. Float fields MUST be `(90.0, -90.0, 90.0)`. NEVER change these values. Full spec: `docs/condor_landscape_spec.md`
2. **BMP and TDM dimensions MUST match TRN** (768×768 for 12×12 patches).
3. **DDS textures are per-PATCH at 2048×2048.** NEVER 8192×8192 (causes BSOD). 144 files for our landscape.
4. **empty.dds MUST be 2048×2048 DXT1** (not 4×4).
5. **Airport .c3d files are OPTIONAL — and NEVER copied from another landscape.** An airport defined in `.apt` with **no** c3d is a valid *virtual* airport — airborne start works (Landscape Guide rev2, p.18). A **mismatched/copied c3d is what causes the "Airport is not installed" crash** (verified: prior agent copied Slovenia2/PTUJ → crash). Real runways (for ground/tow/winch starts) are produced by **Airport Maker (Jiří Brožek) → ObjectEditor.exe** (OBJ→C3D), saved as `<Name>G.c3d`/`<Name>O.c3d` matching the `.apt` name exactly. The right-click **"Use generic files"** in the editor is a dead Condor‑1 relic — it fails in C2. Full procedure: `docs/condor_airport_workflow.md`.
6. **Hashes MUST be regenerated** after any .tr3 or .for change: `tools/CLT2.7/LandscapeEditor.exe -hash MacedoniaSkopje`
7. **No desktop disruption.** Never pop up GUI windows without explicit user approval.
8. **Loading screens: real Macedonian glider photos ONLY.** No stock photos, no other countries, no paragliders.
9. **Never guess formats.** Read `docs/condor_landscape_spec.md` first. When in doubt, check Slovenia2 to verify but do NOT treat it as the specification source.
10. **A landscape is NOT done until `scripts/verify_landscape.py` passes AND the flight planner has been opened in-sim** (map renders + airborne start works). Build via the ONE orchestrator — `CONDOR_LANDSCAPE=<sel> python scripts/build_landscape.py` — never by hand-running individual generators (that is how NorthMacedonia once shipped without its `.bmp`/`.tdm`/`.cup`). **Any change to the map area (DEM/`.trn`) or airports (`.apt`) REQUIRES regenerating `.bmp` + `.tdm` + `.cup`** (then re-hash); `verify_landscape.py`'s freshness checks fail otherwise. The `Stop` hook `.Codex/hooks/verify_landscapes.sh` enforces this. All generators are grid-driven via `CONDOR_LANDSCAPE` (`skopje` default, `nm` = NorthMacedonia) — see `.Codex/skills/condor-landscape-build/SKILL.md` and `docs/PIPELINES.md` §0a.
11. **MAX QUALITY — no approximation, ever** (user's non-negotiable rule, regardless of time/tools). Custom objects: keep the source model's **FULL vertex/face count** — Condor 2 has no hard vertex cap (FPS is the only limit; a glider ≈ 60k verts, so a landmark can be tens of thousands). **Never decimate to a round number, never extrude a generic box as a fallback.** Migrate the **real, highest-detail model with its textures baked to a DDS** (textured `.c3d`). Textures: highest-resolution source (cadastre **zoom 11**, never zoom 8) + high-quality DXT. If a source/tool/step would degrade quality, find a better one or report it — do **not** silently approximate. (This is the rule the NorthMacedonia regression broke — see `docs/POSTMORTEM_northmacedonia.md`.)
    - **Placement precision — strictly enforced:** every placed object sits on its real-world footprint within **1–3 m**, oriented within **1–3°**, with correct (un-mirrored) handedness, and **scaled to the real OBJECT's dimensions within 1–3 m** — the *building* outline **detected in the ortho texture (SAM/ML/GPU)** or the cadastre **BUILDING** polygon, **NEVER the parcel** (the parcel/land area is always larger than the object; do not scale to it). Derive position + bearing + size by detecting the object in the texture; placement from raw lat/lon + a guessed scale/heading is **forbidden**. Validate the placed footprint over the texture (overlay) **before** install — a failing placement does not ship.

## Landscape Specifications

| Parameter | Value |
|-----------|-------|
| Full DEM | 2305×2305, 30m, UTM 34N (EPSG:32634) |
| TRN overview | 768×768, 90m |
| Patches | 12×12 = 144 |
| Tiles | 3×3 = 9 |
| Top-left | E=506880, N=4700160 |
| BR (TRN header) | E=575910, N=4631130 |
| Patch size | 5,760 m (64 px at 90m, 192 intervals at 30m) |
| Condor origin | Bottom-right (SE). CCRR: col 0=east, row 0=south |
| Condor install | `C:/Condor2/Landscapes/MacedoniaSkopje/` |

## File Size Reference

| File | Dimensions | Size |
|------|-----------|------|
| .trn | 768×768 | 1,179,684 bytes |
| .tr3 | 193×193 × 144 | 74,498 bytes each |
| .bmp | 768×768 32bpp | ~2.4 MB |
| .tdm | 768×768 | 589,832 bytes |
| .dds | 2048×2048 × 144 | ~2.7 MB (DXT1) or ~5.6 MB (DXT3) each |
| .for | 512×512 × 144 | 262,144 bytes each |
| .apt | 3 airports | 216 bytes |

## Pipeline Scripts

All in `scripts/`, run with `python scripts/<name>.py`. Everything is grid-driven via `CONDOR_LANDSCAPE` (`skopje` default, `nm` = NorthMacedonia).

### Orchestrator + gate (use these, not the individual generators)
- **`build_landscape.py`** — ONE entry point; runs every step in dependency order (`dem → trn → tr3 → flatten-runways → re-tr3 → apt → cup → tdm → bmp → textures → forest → water-bake → hash → verify`), idempotent + resumable. Heavy stages gated behind `--with-dem/--with-textures/--with-forest/--with-all`; default = fast metadata. `CONDOR_LANDSCAPE=nm python scripts/build_landscape.py`.
- **`verify_landscape.py`** — HARD completeness + freshness gate (the definition of "done"). Fails on any missing OR stale file (`.bmp`/`.tdm`/`.cup` older than `.trn`/`.apt` ⇒ stale). `--metadata-only` for the fast subset. See `docs/PIPELINES.md` §0a and `.Codex/skills/condor-landscape-build/`.

### Phase 1 — Terrain
1. `flatten_runways.py` — Flatten runway areas in source DEM
2. `generate_trn.py` — Resample DEM 2305→768 and write .trn (90m overview)
3. `generate_tr3.py` — Extract 144 patches from full DEM (30m, 193×193)
4. `generate_apt.py` — Binary .apt (72-byte records)
5. `generate_cup.py` — SeeYou .cup turnpoints
6. `generate_tdm.py` — Thermal map at 768×768
7. `generate_flight_planner_map.py` — Topographic BMP at 768×768

### Phase 2 — Textures & Forest
> **Full pipeline reference — data sources, invariants, run order, and how to scale to all of North Macedonia: [`docs/PIPELINES.md`](docs/PIPELINES.md).**
8. `download_mk_ortho_2023.py` — MK 2023 orthophoto (EPSG:6316, zoom 11)
9. `download_all_quadrants.py` — Parallel download wrapper
10. `build_patch_textures.py` — 144 patch DDS via gdalwarp + nvcompress (2048×2048 DXT1)
11. `bake_water.py` / `fix_textures.py` — bake OSM water (→DXT3) / LAB colour-fix off-tiles, in place
12. `download_forest_rasters.py` — Copernicus HRL TCD + DLT + ESA WorldCover → landscape grid
13. `generate_forest_maps.py` — 512×512 forest masks; extent = continuous satellite canopy `(TCD≥40 ∪ WorldCover ∪ OSM) − exclusions`, species from DLT + OSM leaf tags. See `docs/PIPELINES.md` §3
14. `generate_loading_screens.py` — Loading screen JPEGs

### Shared Modules
- `condor_grid.py` — Grid constants, UTM transforms
- `osm_io.py` — Overpass API, GeoJSON
- `rasterize.py` — Shapely→raster masks
- `forest_utils.py` — DEM sampling, raster→patch resampling, OSM buffer functions

## Tools

| Tool | Location |
|------|----------|
| CLT 2.7 | `tools/CLT2.7/` |
| nvcompress | `C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe` |
| GDAL | `C:/Program Files/QGIS 4.0.0/bin/` (gdalwarp, gdal_translate, gdalbuildvrt) |
| Windows MCP | `.sandbox/mcp/windows-mcp-server/Sbroenne.WindowsMcp.exe` |

## Data Sources

- **DEM:** Copernicus GLO-30 in `sources/dem/`
- **Airports:** `data/airports.json`
- **Ortho:** MK 2023 WMS (e-uslugi.katastar.gov.mk), EPSG:6316, `.sandbox/textures_mk2023_z11/`
- **OSM:** Cached GeoJSON in `.sandbox/osm/`
- **CORINE:** CLC2018 WMS for forest classification (species fallback)
- **Forest canopy/species:** Copernicus HRL Tree-Cover-Density + Dominant-Leaf-Type 2018 (EEA DiscoMap), ESA WorldCover 2021 v200 → `.sandbox/forest_rasters/` via `download_forest_rasters.py`

## Airports

| ICAO | Name | Lat | Lon | Elev | Runway |
|------|------|-----|-----|------|--------|
| LWSK | Skopje International | 41.9638 | 21.6207 | 238m | 16/34, 2950m |
| LWSN | Stenkovec | 42.0594 | 21.3888 | 318m | 12/30, 1200m |
| LW67 | Kumanovo | 42.1578 | 21.6939 | 371m | 12/30, 1200m |

## GUI Automation

Condor 2 runs **elevated** (it has no elevation manifest — it inherits the elevated Codex shell) with a custom DirectX UI (no Win32 controls). The windows-automation MCP is **medium-integrity**, so its `ui_click`/`keyboard_control` are UIPI-blocked on Condor. **Drive Condor from the elevated shell via Python + `pyautogui`**: `ctypes.windll.user32.SetProcessDPIAware()` first; use PHYSICAL coords from `user32.GetWindowRect` (NOT the MCP's logical bounds — that off-by-DPI bug makes clicks miss); find windows by class via `EnumWindows`. **Screenshots via `PIL.ImageGrab.grab(bbox=..., all_screens=True)`** → save to file → Read it (screen capture is NOT integrity-blocked). Verified menu fractions: `TGUIForm` FREE FLIGHT (0.134, 0.340); `TGUIFlightPlannerForm` Start flight (0.955, 0.96), Landscape dropdown top-right ~(0.92, 0.12); the 3D window's class starts with `Condor`. This is the **mandatory in-sim validation loop (rule 10)** — not optional. DPI 125%. Full technique: memory `reference_condor_automation`.

## Current Status (2026-06-21)

**MacedoniaSkopje (12×12) — complete & verified:** .trn (768×768/90m), .tr3 (144, anti-transpose, seams 0m, runways flattened + gated), .apt, .cup, .tdm, .bmp, .ini, .obj (0 B), DDS textures (144 @2048×2048 + 86 water-baked DXT3 + 6 colour-fixed tiles), **forest maps (144, continuous satellite-canopy algorithm, 35.2% cover, IoU 0.79 vs canopy, seam-free)**, hashes (.tha/.fha), Images/. **`verify_landscape.py` → PASS (43/43, full gate).**

**NorthMacedonia (40×32, `CONDOR_LANDSCAPE=nm`) — complete & verified:** .trn (2560×2048), .tr3 (1280, flattened), .apt (14 airports), **.cup (14 airports), .tdm (2560×2048), .bmp (2560×2048) — newly generated (were missing)**, .ini, .obj (0 B), DDS textures (1280 @2048×2048 + water-baked), forest maps (1280), hashes (.tha/.fha), Images/ (3 TEMP placeholder jpgs — real MK glider photos pending). **`verify_landscape.py` → PASS (43/43, full gate).**

**Workflow now enforced:** build via `scripts/build_landscape.py` (generic orchestrator); ship-gate `scripts/verify_landscape.py` (fails on missing/stale); `Stop` hook `.Codex/hooks/verify_landscapes.sh` blocks an incomplete/stale landscape from being declared done. See `docs/PIPELINES.md` §0a and `.Codex/skills/condor-landscape-build/`.

**Needs in-sim testing (the human/parent confirms):** open the flight planner for NorthMacedonia, confirm the map renders and an airborne start works (the disk-side gate passes; in-sim is the other half of "done").

**Remaining:** real MK glider loading screens (NM + Skopje), 3D objects. Both landscapes' metadata/terrain/textures/forest are complete.
