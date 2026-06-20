---
description: Condor 2 file format constraints — prevents crashes from wrong dimensions or formats
globs: scripts/generate_*.py, scripts/build_*.py, scripts/process_*.py
---

# Condor File Format Rules

Before generating ANY landscape file (.trn, .bmp, .tdm, .dds, .for, .tr3, .apt), you MUST read `docs/condor_landscape_spec.md` for the exact format specification. Never guess dimensions or header values.

## Dimension Constraints

These three groups of files MUST have matching dimensions within each group:

**Group 1 — TRN dimensions (patches × 64):**
- `.trn` heightmap overview
- `.bmp` flight planner map  
- `.tdm` thermal map

For MacedoniaSkopje: **768×768**

**Group 2 — Full DEM dimensions (patches × 192 + 1):**
- Source DEM (used only for .tr3 extraction)

For MacedoniaSkopje: **2305×2305**

**Group 3 — Per-patch dimensions:**
- `.tr3` heightmaps: **193×193**
- `.dds` textures: **2048×2048**
- `.for` forest maps: **512×512**

## TRN Header

The 3 float fields at offset 8-19 are pixel spacing. They are ALWAYS `(90.0, -90.0, 90.0)`. Never modify these values.

## DDS Textures

Always 2048×2048 per patch. Never 8192×8192 (causes GPU driver crash). Use `nvcompress -bc1` for DXT1 compression.
