# Postmortem — NorthMacedonia full-country build (REJECTED)

The 1,280-patch full-North-Macedonia landscape was built fast and in parallel,
passed every disk-side gate, and was then **rejected on first in-sim test** as a
full regression: soft/worse textures, visible MK↔ESRI border seams, forest/water/
airports visibly misaligned to the ground, gliders spawning off the runway at
13/14 airports, and a khaki flight-planner map. This is the honest analysis.

## Root cause (one sentence)
**Coverage and speed were prioritised over quality and *integration* validation:**
every stage was verified in isolation (file counts, sizes, freshness, the
`verify_landscape` gate) but the *combined visual result was never checked, and
never in-sim,* until the user flew it — so every defect surfaced at once, at the end.

## Specific defects
1. **Texture resolution regression.** NM used MK cadastre at **zoom 8 (2.24 m/px)**;
   the Skopje pilot used **zoom 11**. The cadastre GWC endpoint rejected arbitrary
   GetMap, so the agent dropped to a coarse pyramid level barely finer than the
   2.8 m/px patch output → soft textures. **The GPU (`nvcompress`) WAS used; the
   SOURCE imagery was coarser** — that is the quality loss, not the compression.
2. **Border-blend seams.** MK cadastre ↔ ESRI World Imagery joined with a 600 m
   feather but **no LAB colour-match**, so the ESRI tone differs from cadastre and
   the seam is visible (e.g. Lake Ohrid's Albanian shore).
3. **Cross-layer misalignment.** Textures were warped to the **30 m-DEM patch grid**
   while the mesh/forest/airports use the **`.trn` grid** — the same ~90 m
   texture-vs-mesh offset already documented for the `.obj` transform, **never
   fixed for the textures themselves**. Result: forests/water/airports look shifted
   off the painted ground.
4. **Airports.** Only Stenkovec received the painted-centerline centering fix; the
   other 13 used raw OSM/OurAirports runway geometry → gliders spawn off the strip.
5. **Flight-planner map.** The hypsometric ramp collapsed to khaki across NM's wide
   0–2741 m range; water was not keyed to blue.
6. **Process churn.** The two follow-up fix-agents (airport centering, map palette)
   hit API errors (Overloaded / socket closed) and left partial state — making the
   landscape look even more broken.

## Why Skopje is good and NM is not (the real lesson)
Skopje (the MVP) was built **incrementally with the user's in-sim eyes at each
step** — every defect was caught and corrected in a tight loop. NM was a **blind
parallel batch** validated only against file existence, so nothing caught the
visual/alignment problems until the whole thing was assembled and flown.

**Lessons:**
- Scaling must **replicate the proven recipe exactly** (zoom-11 cadastre; warp to
  the `.trn` grid the mesh uses) — not improvise (zoom 8; DEM-grid warp).
- **"Done" requires in-sim visual + cross-layer-alignment validation,** not just
  file presence/freshness. The `verify_landscape` gate must grow a visual/alignment
  check, or "done" stays a lie.
- Build **region-by-region with an in-sim check per region,** not one blind sweep.

## NM recovery plan (LATER, not now — landscape is kept, parked)
Re-source cadastre at zoom 11 (tile the GWC pyramid correctly); warp textures to
the `.trn` grid; LAB colour-match the ESRI border blend; apply the generic
airport-centering to all 14; fix the map ramp + blue water. Do it **incrementally,
verifying each region in-sim**, the way Skopje was done.

## Decision
Focus returns to the **MacedoniaSkopje MVP**. NM stays on disk for a future,
properly-validated improvement pass.
