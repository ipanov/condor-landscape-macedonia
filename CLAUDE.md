# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

All in `scripts/`, run with `python scripts/<name>.py`.

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

Condor 2 runs as **elevated process** with custom DirectX UI. Standard input injection (SendInput, mouse_event, PostMessage) is blocked by UIPI. The MCP `ui_click` tool works intermittently. For visual testing, coordinate with the user for clicks, then take screenshots with `screenshot_control`. DPI scaling is 125%.

## Current Status (2026-06-20)

**Complete & verified:** .trn (768×768/90m), .tr3 (144, anti-transpose, seams 0m, runways flattened + gated), .apt, .cup, .tdm, .bmp, .ini, DDS textures (144 @2048×2048 + 86 water-baked DXT3 + 6 colour-fixed tiles), **forest maps (144, continuous satellite-canopy algorithm, 35.2% cover, IoU 0.79 vs canopy, seam-free)**, hashes (.tha/.fha regenerated). `verify_phase1.py` → 49/52 PASS.

**Needs testing:** Launch Condor (fully exit+relaunch to clear terrain cache), verify no crash, check airport positions and mesh.

**Remaining:** Loading screens (real MK glider photos — the only `verify_phase1` failures), 3D objects, complete ortho download. Then expand to full North Macedonia (see `docs/PIPELINES.md` §8).
