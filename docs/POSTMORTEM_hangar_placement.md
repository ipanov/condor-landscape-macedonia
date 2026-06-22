# Postmortem — why the Stenkovec hangar took so many iterations to place

Honest analysis of why a single, large, clearly‑visible hangar was hard to mount to
1–3 m / 1–3°, and what makes it deterministic and first‑shot for the next objects.

## The two concrete bugs (both now fixed)

### 1. Size was wrong (model shrunk to 78%)
**Cause:** the model was scaled to fit the **OSM footprint** (31.5 × 25.2 m). OSM's
polygon for this hangar is **inaccurate** — it reports a squarish 1.25:1 shape, but the
real hangar (seen in the 0.14 m ortho) is **long and narrow**. Trusting OSM's length
shrank the model and pulled the front in ("too much back").
**Fix:** for a **migrated, to‑scale model** (the SoFly LW75 is a model of *this* hangar,
authored in real metres), the **model's own native size is the size authority** — not
OSM. `scale = 1.0`. OSM is used only for the long‑axis bearing, not dimensions.

### 2. Position was ~5.8 m "too far back"
**Cause:** the placement pinned the model by the **mean of its base vertices** `(5.01,
2.56)`. That is **not** the building's centre — it is biased ~5.8 m toward the side with
more vertices. Here the doors + clubroom (`Hangar_DOORS` 22 k, `Hangar_FRONT` 11 k,
`Hangar_CHAIR` 11 k verts) sit on +X, so the vertex average is dragged +X. The texture
match correctly found the **roof centre**, but the model was anchored by the wrong point,
so the building landed 5.8 m off.
**Fix:** anchor by the **geometric footprint centre** (convex‑hull centroid `(0.15,
−0.65)` ≈ AABB centre), which is what the texture match aligns to. Vertex‑density bias
is removed; the constant per‑model offset disappears.

## Why it was hard in the first place (the systemic reasons)

1. **The installed Skopje textures carry a grid drift.** They were built on the older
   29.987 m DEM grid (`condor_grid.py:23‑27`), not exact 30 m, and objects place on the
   `.trn`/object grid. So **placing from real‑world coordinates lands tens of metres off
   the painted building** — and the amount is patch‑dependent and unknown in advance. The
   only reliable anchor is **the painted roof inside the game texture itself**. (The
   permanent cure is the Phase‑0a texture re‑warp in `docs/OBJECT_PLACEMENT.md`; until
   then we mount on the texture.)
2. **The validation was done on the wrong raster.** Early on, the footprint was validated
   against the **cadastre ortho** — a *different* raster than Condor draws — so it looked
   perfect and was wrong in‑sim. Validation must be on the **exact installed `tCCRR.dds`**.
3. **The game texture is coarse (2.8125 m/texel).** A 40 m hangar is ~14 texels, so
   detecting the painted roof centre is good to about **±1 texel ≈ ±2.8 m**. That is the
   floor of texture‑based positioning; tighter needs sub‑texel template matching or
   higher‑resolution textures.
4. **Per‑object data quality varies.** OSM was wrong here; cadastre has **no record** of
   this airfield building (confirmed: 7 sparse rural buildings within 900 m, nearest
   210 m). So no single external source is trustworthy per‑object — the model geometry +
   the painted roof are the dependable inputs.
5. **Model anchor convention matters.** The vertex‑mean vs geometric‑centre difference is
   a silent, constant per‑model error until explicitly handled (bug 2).

These compounded: a coordinate‑based placement (off by the grid drift) + an OSM size (too
small) + a vertex‑mean anchor (5.8 m back) + validation on the wrong raster (hid it) =
"close but wrong" every iteration.

## The deterministic recipe for first‑shot 1–3 m / 1–3° on the next objects

No per‑object guidance; runs fast:
1. **Size** = the migrated model's **native dimensions** (authored in metres). For
   *autogen* shells, size = the footprint polygon (cadastre BUILDING where it exists,
   else MS/Overture), since those models are built to the footprint.
2. **Orientation (long axis)** = the footprint long‑axis bearing (OSM/cadastre vector).
3. **Position** = **template‑match the real footprint against the installed `tCCRR.dds`**
   (the painted roof), so it lands on what Condor draws regardless of grid drift. Use
   sub‑texel refinement of the match peak to push ±2.8 m → ~±1–2 m.
4. **Anchor** = the model's **geometric centroid** (hull centroid), never the vertex mean.
5. **Front/rear (180°)** = the model's own front feature (doors group) pointed at the
   **airfield/road** side.
6. **Validate** = overlay the placed footprint on the **same `tCCRR.dds`** + numeric
   gates; reject if it doesn't sit on the painted roof.

The systematic errors (size, anchor bias, wrong‑raster validation, grid drift) are now
eliminated structurally; the residual is the texture resolution (~1 texel), which
sub‑texel matching and/or the Phase‑0a re‑warp address.
