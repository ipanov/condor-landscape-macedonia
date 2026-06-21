---
name: condor-landscape-build
description: This skill should be used when building, rebuilding, extending, or shipping a Condor 2 soaring landscape in this repo (e.g. "build NorthMacedonia", "regenerate the flight planner map", "rebuild the tdm/cup/bmp", "expand to full North Macedonia", "is the landscape done", "verify the landscape", "ship the landscape"). It defines the ONE generic, enforced build+verify workflow so no pipeline step is silently missed or aimed at the wrong landscape.
version: 1.0.0
---

# Condor 2 Landscape — generic build + completeness gate

Every Condor landscape in this repo is built by ONE orchestrator and gated by ONE
verifier, both driven by `CONDOR_LANDSCAPE`. You never hand-run individual
generators ad hoc — that is exactly how the NorthMacedonia `.bmp/.tdm/.cup` got
missed (the flight planner couldn't display or fly it). Use the orchestrator; let
the verifier define "done".

## Landscape selection

`CONDOR_LANDSCAPE` chooses the landscape; everything else reparameterises from
`scripts/condor_grid.py` (`LANDSCAPE_NAME`, `WIDTH/HEIGHT`, `PATCHES_X/Y`,
`patch_bounds_utm`, …). Two are wired today:

| `CONDOR_LANDSCAPE` | Name             | Patches | `.trn`/`.bmp`/`.tdm` |
|--------------------|------------------|---------|----------------------|
| (unset) / `skopje` | MacedoniaSkopje  | 12×12   | 768×768              |
| `nm`               | NorthMacedonia   | 40×32   | 2560×2048            |

Install dir is always `C:/Condor2/Landscapes/<Name>/`.

## Build — `scripts/build_landscape.py`

```bash
CONDOR_LANDSCAPE=nm python scripts/build_landscape.py            # FAST metadata
python scripts/build_landscape.py                               # skopje, metadata
CONDOR_LANDSCAPE=nm python scripts/build_landscape.py --with-textures --with-forest
CONDOR_LANDSCAPE=nm python scripts/build_landscape.py --with-all   # + DEM download
```

One entry point runs every step in dependency order:

```
dem -> trn -> tr3 -> flatten-runways -> re-tr3 -> apt -> cup -> tdm -> bmp
    -> textures -> forest -> water-bake -> hash -> verify
```

- Each step is **idempotent + resumable**: skipped when its output exists and is
  FRESH (newer than its inputs). Re-run a step with `--force`, or target steps
  with `--only STEP[,STEP]`, `--from STEP`, `--skip STEP`.
- **Heavy stages are gated** (`dem`, `textures`/`water-bake`, `forest`) behind
  `--with-dem` / `--with-textures` / `--with-forest` (`--with-all` enables all).
  The DEFAULT run is the fast metadata subset (trn…bmp + hash + verify) and is
  what you use after any map-area or airport edit.
- `--list` prints the resolved plan; `--dry-run` prints the commands without
  running them.
- Texture/water-bake route to the per-landscape script automatically (skopje
  `build_patch_textures.py`/`bake_water.py`; nm `nm_build_textures.py`/
  `nm_bake_water.py`).
- The `hash` step is `tools/CLT2.7/LandscapeEditor.exe -hash <Name>` — the one
  safe headless CLT CLI (exit 0, no GUI). It re-stamps `.tha`/`.fha` after any
  `.tr3`/`.for` change.

## Verify — `scripts/verify_landscape.py` (the definition of done)

```bash
CONDOR_LANDSCAPE=nm python scripts/verify_landscape.py                 # full gate
CONDOR_LANDSCAPE=nm python scripts/verify_landscape.py --metadata-only # fast subset
```

HARD gate (non-zero exit, never a silent skip) that REQUIRES every file Condor
needs to load+fly: `.ini`, `.trn`, all `.tr3` (count == patches²), `.apt`, `.cup`,
`.tdm` (dims == `.trn`), `.bmp` (dims == `.trn`), `.obj` (0 B allowed),
`Textures/*.dds` (count, 2048², DXT1/DXT3) + `empty.dds`, `ForestMaps/*.for`
(count), `.tha`/`.fha` (entry counts), `Images/*.jpg` (≥1).

It also enforces **freshness** — the bug this whole workflow exists to prevent:
- `.bmp` / `.tdm` must be ≥ as new as `.trn` (map area changed → regen)
- `.bmp` / `.cup` must be ≥ as new as `.apt` (airports changed → regen)
- `.tha` / `.fha` must be ≥ as new as the newest `.tr3` / `.for`

A stale file is a FAIL with a "regenerate X" message. `--metadata-only` checks the
load-critical + freshness subset and skips the heavy `.dds`/`.for` (use it to gate
a fast metadata build; use the full gate before declaring the landscape shippable).

## The completeness hook (enforcement)

`.claude/hooks/verify_landscapes.sh` (a `Stop` hook, registered in
`.claude/settings.json`) runs `verify_landscape.py --metadata-only` for every
installed landscape whenever the repo has uncommitted changes, and **fails (exit
2)** if any landscape is incomplete or stale — so an unfinished/stale landscape
can't be quietly declared done. It auto-discovers `C:/Condor2/Landscapes/*`
(override via `.claude/hooks/landscapes.txt`). Hooks load at session start; after
editing the hook, restart Claude.

## "Done" is TWO things

A landscape is done only when BOTH hold:
1. `verify_landscape.py` passes (full gate), AND
2. the **flight planner has been opened in-sim** and the map renders + an airborne
   start works (the parent/human confirms this; it can't be checked from disk).

After ANY change to the map area (DEM/`.trn`) or airports (`.apt`), you MUST
regenerate `.bmp` + `.tdm` + `.cup` (then re-hash) — the verifier's freshness
checks fail otherwise.

## Adding a new landscape

1. Add its branch to `scripts/condor_grid.py` (`CONDOR_LANDSCAPE` selector →
   `LANDSCAPE_NAME`, NW corner, `PATCHES_X/Y`). Everything else reparameterises.
2. Add the `selector_for()` case in `.claude/hooks/verify_landscapes.sh`.
3. Run `CONDOR_LANDSCAPE=<sel> python scripts/build_landscape.py --with-all`, then
   the full `verify_landscape.py`, then open the flight planner in-sim.

Authoritative byte-layout / data-source detail lives in `docs/PIPELINES.md` and
`docs/condor_landscape_spec.md`. Never guess a file format.
