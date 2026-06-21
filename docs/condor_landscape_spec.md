# Condor 2 Landscape File Format Specification

Verified against Slovenia2 reference landscape and official documentation.
Sources: Condor Landscape Guide rev2, SoaringTools Tutorial, flxhu/condor2 GitHub,
Condor forums (condorsoaring.com/forums), binary verification of Slovenia2 files.

## Grid System

| Unit | Size (pixels at 90m) | Size (meters) | Description |
|------|---------------------|---------------|-------------|
| Pixel | 1 | 90 m | TRN overview resolution |
| Patch | 64 x 64 | 5,760 x 5,760 m | Basic terrain/texture/forest unit |
| Tile | 256 x 256 (4x4 patches) | 23,040 x 23,040 m | Grouping unit (not used for files) |

**Origin:** Bottom-right (south-east). Patch `0000` = SE corner.
**Naming:** `CCRR` format — CC=column (0=east), RR=row (0=south).

### Grid Formulas

```
TRN_width  = patches_x * 64
TRN_height = patches_y * 64
Full_DEM   = patches_x * 192 + 1   (shared boundary vertices)
BR_easting  = UL_easting + (TRN_width - 1) * 90.0
BR_northing = UL_northing - (TRN_height - 1) * 90.0
```

## 1. `.trn` — Source Heightmap Overview

**Header: 36 bytes, little-endian:**

| Offset | Type | Field | Value | Notes |
|--------|------|-------|-------|-------|
| 0 | int32 | width | patches_x * 64 | e.g., 768 for 12 patches |
| 4 | int32 | height | patches_y * 64 | e.g., 768 for 12 patches |
| 8 | float32 | pixel_size_x | **90.0** | ALWAYS 90.0 — do NOT change |
| 12 | float32 | pixel_size_y | **-90.0** | ALWAYS -90.0 — do NOT change |
| 16 | float32 | pixel_size_z | **90.0** | ALWAYS 90.0 — do NOT change |
| 20 | float32 | br_easting | varies | UTM easting of BR pixel center |
| 24 | float32 | br_northing | varies | UTM northing of BR pixel center |
| 28 | uint16 | utm_zone | 34 | UTM zone number |
| 30 | uint16 | pad | 0 | |
| 32 | uint16 | hemisphere | 78 | ASCII 'N'=78, 'S'=83 |
| 34 | uint16 | pad | 0 | |

**Data:** `width * height` uint16 LE elevations in meters, rows south-to-north.
**File size:** `36 + width * height * 2`

**WARNING:** The 3 float fields (90.0, -90.0, 90.0) are pixel spacing, NOT rotation
angles. Setting them to 30.0 causes "Access violation" crash. The .trn is ALWAYS
at 90m resolution. Full 30m resolution is in .tr3 patches.

## 2. `.tr3` — Per-Patch Heightmap (30m resolution)

- **No header.** Pure binary data.
- **193 x 193 uint16** LE elevations in meters.
- File size: 74,498 bytes.
- 193 samples = 192 intervals * 30m = 5,760m = one patch.
- Adjacent patches share boundary vertices.
- **Orientation: ANTI-TRANSPOSE of north-up GDAL**, i.e. `patch.T[::-1, ::-1]`
  (in stored .tr3: +row = WEST, +col = NORTH). Verified against Slovenia2 by
  shared-edge continuity (correct op → boundary vertices bit-exact). The older
  "180° rotation" wording was WRONG — it omits the transpose; `rot90(x,2)` alone
  does NOT match Condor and tears the mesh. Prefer building via RawToTrn.exe
  (Flip vertical ON, 30 m), which applies the correct orientation by construction.
- Naming: `hCCRR.tr3`

## 3. `.apt` — Airports

**Fixed 72-byte records, no file header.**

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| 0 | uint8 | name_length | |
| 1-31 | char[31] | name | Null-padded ASCII |
| 32-35 | float32 | unused | Always 0.0 |
| 36-39 | float32 | latitude | WGS-84 decimal degrees |
| 40-43 | float32 | longitude | WGS-84 decimal degrees |
| 44-47 | float32 | elevation | Meters ASL |
| 48-51 | int32 | runway_direction | Degrees true |
| 52-55 | int32 | runway_length | Meters |
| 56-59 | int32 | freq_or_id | |
| 60-63 | uint32 | flags1 | 0x00000000 |
| 64-67 | float32 | flatten_radius | ~120-150 meters |
| 68-71 | uint32 | flags2 | 0x00010000 or 0x00000100 |

**Airport name must match .c3d filenames** in Airports/ directory:
- `<Name>G.c3d` — ground model
- `<Name>O.c3d` — objects model
- `<Name>/*.dds` — airport-specific textures (if any)

## 4. `.tdm` — Thermal Map

**Header: 8 bytes** (int32 width, int32 height).
**Data:** width * height uint8 values (0=no thermals, 255=strongest).
**Dimensions MUST match .trn** (e.g., 768x768).
Resolution: 90m per pixel.

## 5. `.bmp` — Flight Planner Map

- Standard Windows BMP, **32-bit** (XRGB or BGRA).
- **Dimensions MUST match .trn** (e.g., 768x768).
- BMP rows stored bottom-up (standard Windows convention).

## 6. `.dds` — Ground Textures (PER-PATCH)

- **One texture per PATCH** (NOT per tile).
- **2048 x 2048** pixels per patch.
- Ground resolution: 5760m / 2048px = 2.8 m/pixel.
- **DXT3** for patches with water (alpha channel for transparency).
- **DXT1** for dry patches (no alpha, half file size).
- 12 mip levels.
- Naming: `tCCRR.dds`
- `empty.dds`: **2048x2048 DXT1** fallback.

**NEVER use 8192x8192** — that's source image size before splitting, not final DDS.

## 7. `.for` — Forest Maps (PER-PATCH)

- **No header.** 512 x 512 uint8 values.
- 0=no trees, 1=coniferous, 2=deciduous.
- **180° rotation** (Condor SE-origin).
- Naming: `CCRR.for` (no prefix).

## 8. `.tha` / `.fha` — Hash Files

- ASCII text, CRLF line endings.
- Format: `CCRR <space> <hash_value>` per line.
- CCRR zero-padded to 6 digits (e.g., `000000`).
- `.tha` from .tr3 files, `.fha` from .for files.
- **Mandatory** — without valid hashes, Condor disables pitch control.

## 9. `.ini` — Configuration

```ini
[General]
Version=1.00
RealtimeShading=1
```

## 10. `.cup` — Turnpoints

SeeYou CSV format. `DDMM.mmmH` coordinates. `m` suffix on elevation.

## 11. `.obj` — 3D Object Placements

**152-byte records, no header:**

| Offset | Type | Field |
|--------|------|-------|
| 0-3 | float32 | posX (header_easting - absolute_easting) |
| 4-7 | float32 | posY (absolute_northing - header_northing) |
| 8-11 | float32 | posZ (altitude) |
| 12-15 | float32 | scale (1.0 = original) |
| 16-19 | float32 | orientation (radians) |
| 20 | uint8 | name_length |
| 21-151 | char[131] | c3d filename (null-padded) |

**VERIFIED against Slovenia2.obj (2026-06-21) — three things the table above hides,
each of which makes objects silently invisible if wrong:**
- **The name MUST include the `.c3d` extension** (Slovenia2 stores `"C1R.c3d"`, len=7),
  not the bare stem. Wrong → Condor can't resolve the model, object never appears.
- **posZ is the ABSOLUTE terrain altitude** at the placement (Slovenia2 rec=488 m),
  NOT 0 and NOT a height offset. Sample the DEM; posZ=0 buries objects at sea level.
- **origin = the `.trn` HEADER easting/northing** (floats at byte 14/18 of the .trn;
  MacedoniaSkopje = 575910 / 4631130), NOT the grid/patch corner. posX = header_E −
  E, posY = N − header_N. Using the grid corner shifts everything by one 90 m pixel.
- The 131-byte name field after the string is **leftover editor heap memory** (a
  constant template of stale pointers across all records) — Condor ignores it, so
  null-padding on write is correct.

## 12. Loading Screens

- JPEG format, `Images/0.jpg` through `Images/N.jpg`.
- Typical resolution: 1920x1080.
- Shown randomly during landscape loading.

## Directory Structure

```
<Name>/
    <Name>.trn .apt .bmp .tdm .cup .ini .obj .tha .fha
    HeightMaps/hCCRR.tr3
    ForestMaps/CCRR.for
    Textures/tCCRR.dds + empty.dds
    Airports/<Name>G.c3d <Name>O.c3d <Name>/*.dds
    Images/0.jpg ... N.jpg
    World/Objects/*.c3d  World/Textures/*.dds
    Working/  (editor working files)
```
