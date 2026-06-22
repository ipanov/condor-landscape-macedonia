"""
Common Condor 2 grid definitions for the MacedoniaSkopje landscape.

Condor uses a bottom-right (south-east) origin for tile/patch naming:
  - tile/patch column 0 is the east-most column
  - tile/patch row 0 is the south-most row
"""

from pathlib import Path
import pyproj

# Landscape calibration (UTM 34N, EPSG:32634)
import os as _os
# Landscape selection (PIPELINES.md §8): CONDOR_LANDSCAPE=nm builds the full North
# Macedonia grid (40x32 patches, NW 447690/4694070, excludes Sofia/Pristina/
# Thessaloniki/Tirana); default is the original 12x12 Skopje pilot. Expansion is a pure
# reparameterisation -- every script importing condor_grid rescales automatically.
_LS = _os.environ.get("CONDOR_LANDSCAPE", "skopje").lower()
_NM = _LS in ("nm", "northmacedonia", "full")
LANDSCAPE_NAME = "NorthMacedonia" if _NM else "MacedoniaSkopje"
ULXMAP = 447690.0 if _NM else 506880.0
ULYMAP = 4694070.0 if _NM else 4700160.0
# EXACTLY 30 m. The DEM was previously 29.9869848156182 m/px, which drifted the
# texture grid ~30 m from the mesh grid at the SE corner. The mesh (.trn/.tr3)
# is built on exact 30 m, so textures must use 30 m too. NOTE: textures already
# installed predate this fix (built on 29.987 m) — rebuild them via
# build_patch_textures.py to get pixel-perfect mesh/texture registration.
XDIM = 30.0                              # EXACTLY 30 m (a 29.987 m grid drifts mesh vs texture)
_PX, _PY = (40, 32) if _NM else (12, 12)
WIDTH = _PX * 192 + 1                     # skopje 2305 ; nm 7681
HEIGHT = _PY * 192 + 1                    # skopje 2305 ; nm 6145

UTM_CRS = pyproj.CRS.from_epsg(32634)
WGS84_CRS = pyproj.CRS.from_epsg(4326)

# Landscape extents in UTM
BR_EASTING = ULXMAP + (WIDTH - 1) * XDIM
BR_NORTHING = ULYMAP - (HEIGHT - 1) * XDIM

# Tile / patch geometry
TILE_SIZE_M = 23040.0  # 23.04 km
PATCH_SIZE_M = 5760.0  # 5.76 km
TILES_X = (_PX + 3) // 4
TILES_Y = (_PY + 3) // 4
PATCHES_X = _PX
PATCHES_Y = _PY

# Texture / mask resolutions
TILE_MASK_SIZE = 8192
PATCH_MASK_SIZE = 512


def project_to_utm(geoms):
    """Project a list of WGS84 shapely geometries to UTM 34N."""
    transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)

    def _transform(x, y, z=None):
        return transformer.transform(x, y)

    from shapely.ops import transform as shp_transform
    return [shp_transform(_transform, g) for g in geoms if g and g.is_valid]


def tile_bounds_utm(tile_col, tile_row):
    """Return (min_e, min_n, max_e, max_n) for a Condor tile."""
    e_max = BR_EASTING - tile_col * TILE_SIZE_M
    e_min = e_max - TILE_SIZE_M
    n_min = BR_NORTHING + tile_row * TILE_SIZE_M
    n_max = n_min + TILE_SIZE_M
    return e_min, n_min, e_max, n_max


def patch_bounds_utm(patch_col, patch_row):
    """Return (min_e, min_n, max_e, max_n) for a Condor patch."""
    e_max = BR_EASTING - patch_col * PATCH_SIZE_M
    e_min = e_max - PATCH_SIZE_M
    n_min = BR_NORTHING + patch_row * PATCH_SIZE_M
    n_max = n_min + PATCH_SIZE_M
    return e_min, n_min, e_max, n_max


def utm_to_pixel(e, n, bounds, width, height):
    """Map UTM coordinate to top-left oriented pixel coordinate."""
    min_e, min_n, max_e, max_n = bounds
    px = (e - min_e) / (max_e - min_e) * width
    py = (max_n - n) / (max_n - min_n) * height
    return px, py


# =========================================================================== #
# AUTHORITATIVE Condor-2 object-placement transform (.obj records + .c3d).
#
# This block is the SINGLE SOURCE OF TRUTH for converting a real-world UTM
# footprint into a Condor `.obj` placement record and a `.c3d` local mesh.
# Every placement script MUST import these functions; do not re-derive the
# constants anywhere else.
#
# --- HOW IT WAS CALIBRATED (observable ground truth, no guessing) ----------
# Reverse-engineered from the Slovenia2 reference landscape (a correctly-made,
# shipping Condor 2 landscape) on 2026-06-21:
#   * Decoded all 7496 records of Slovenia2.obj (152-byte records:
#     posX,posY,posZ,scale,ori_radians, u8 namelen, name incl '.c3d').
#   * Decoded the Slovenia2.trn header: BR pixel-CENTRE = (616140, 5040540),
#     2560x1792 @ 90 m, zone 33N.
#   * Overlaid the decoded building objects (B1/B2/B3/B-PZ models) on the
#     INSTALLED Slovenia2 ortho DDS at high zoom over 8 dense patches
#     (371 buildings) and cross-correlated the markers against bright rooftops.
#
# --- POSITION (verified) ---------------------------------------------------
# The naive "anchor = .trn header BR pixel CENTRE" decode put every object
# dE=+50.6 m / dN=-45.0 m off the rooftops -- i.e. exactly HALF a 90 m TRN
# pixel in BOTH axes (45 = 90/2). This is the ~50 m "objects are off" bug.
# The correct anchor is the SOUTH-EAST CORNER of the BR pixel:
#       anchor_E = BR_E + 45  (east edge of the landscape grid)
#       anchor_N = BR_N - 45  (south edge of the landscape grid)
#   posX = anchor_E - E      (metres WEST of the east edge)
#   posY = N - anchor_N      (metres NORTH of the south edge)
# After this fix the residual over 371 buildings on 6 patches is
#   weighted-mean dE=-0.6 m, dN=+3.3 m  (median dE=0.0, dN=+2.8 m) -- i.e.
# objects land ON the painted rooftops, within one 2.8 m texel.  (Validation
# images: .sandbox/s2_village_tight.png  = before, off in the trees;
#          .sandbox/s2_village_CORRECTED.png = after, on the roofs.)
#
# --- ORIENTATION (verified) ------------------------------------------------
# c3d meshes are modelled with the object's reference axis along LOCAL +Y
# (proven: every Slovenia2 airport GrassPaint runway has its long axis at
# local azimuth 0/180 regardless of the real runway heading -- the heading is
# applied at placement, not baked into the mesh).  `ori` is the COMPASS
# AZIMUTH (radians, clockwise from North) that local +Y should point to in the
# world; Slovenia2's ori values are clean whole-degree headings (0,15,74,107,
# 147 deg...).  The world placement Condor performs is:
#       world = ref + R(-ori) . local         (R = standard math CCW)
#   so   local (0,1) [+Y]  ->  world (sin ori, cos ori)  == azimuth `ori`
#        local (1,0) [+X]  ->  world (cos ori,-sin ori)  == azimuth `ori`+90
# Verified against the Novo Mesto runway centreline (rwdir 50 deg traces the
# painted grass: .sandbox/s2_rw_centerline.png) and B-PZ barn footprints
# (.sandbox/s2_BPZ_final.png).
#
# Because the mesh reference axis is +Y, a footprint built relative to its
# centroid in (x=East, y=North) metres needs ori=0 to appear true-north, and
# ori = the building's long-edge azimuth to appear rotated -- footprint_to_local
# below keeps the polygon in true E/N so the prism is NOT pre-rotated and ori
# alone spins it.
# =========================================================================== #

# SE-corner object anchor for MacedoniaSkopje (BR pixel centre +/- half pixel).
#
# CRITICAL: the anchor is derived from the **.trn header BR pixel-CENTRE**, which
# is the 90 m overview grid (768 x 90 m), NOT from BR_EASTING/BR_NORTHING above.
# BR_EASTING/BR_NORTHING are computed from the 30 m DEM (2305 x 30 m) and are 90 m
# different (576000 / 4631040) because a 2305@30 m grid does not share the same SE
# corner as a 768@90 m grid. The Slovenia2 calibration was done against the .trn
# header, so the object anchor MUST use the header values. Always prefer
# obj_anchor_from_trn(<installed .trn>) when expanding the region.
TRN_BR_EASTING = ULXMAP + (PATCHES_X * 64 - 1) * 90.0   # 575910.0  (.trn header BR_E)
TRN_BR_NORTHING = ULYMAP - (PATCHES_Y * 64 - 1) * 90.0  # 4631130.0 (.trn header BR_N)
_HALF_TRN_PX = 45.0                          # half of the 90 m TRN overview pixel
OBJ_ANCHOR_E = TRN_BR_EASTING + _HALF_TRN_PX   # 575955.0  (grid EAST edge)
OBJ_ANCHOR_N = TRN_BR_NORTHING - _HALF_TRN_PX  # 4631085.0 (grid SOUTH edge)

# Verified Slovenia2 residual (reported so callers can cite the calibration).
OBJ_TRANSFORM_RESIDUAL_M = (-0.6, 3.3)    # (dE, dN) weighted-mean over 371 bldgs


def obj_anchor_from_trn(trn_path):
    """Return the SE-corner object anchor (anchor_E, anchor_N) for ANY landscape
    by reading its ``.trn`` header BR pixel-centre and adding the half-pixel.

    Use this when the region is re-parameterised (expanded) so the anchor stays
    derived from the actual installed header rather than a hard-coded constant.
    """
    import struct
    data = Path(trn_path).read_bytes()
    br_e, br_n = struct.unpack_from("<2f", data, 20)
    return br_e + _HALF_TRN_PX, br_n - _HALF_TRN_PX


def obj_record_xy(e, n, anchor=None):
    """UTM (E, N) metres -> Condor ``.obj`` (posX, posY) placement offsets.

    posX = anchor_E - E  (metres west of the grid east edge),
    posY = N - anchor_N  (metres north of the grid south edge).
    `anchor` defaults to the MacedoniaSkopje SE corner (OBJ_ANCHOR_E/N); pass
    the result of obj_anchor_from_trn() for a different/expanded landscape.

    VERIFIED against Slovenia2.obj: residual ~0 m on painted rooftops (the
    earlier centre-of-pixel anchor was off by exactly +45/-45 m).
    """
    if anchor is None:
        ax, ay = OBJ_ANCHOR_E, OBJ_ANCHOR_N
    else:
        ax, ay = anchor
    return ax - e, n - ay


def obj_world_xy(pos_x, pos_y, anchor=None):
    """Inverse of :func:`obj_record_xy`: (posX, posY) -> UTM (E, N).

    E = anchor_E - posX ;  N = anchor_N + posY.  Used by validators that decode
    installed records back to the ground.
    """
    if anchor is None:
        ax, ay = OBJ_ANCHOR_E, OBJ_ANCHOR_N
    else:
        ax, ay = anchor
    return ax - pos_x, ay + pos_y


def heading_deg_to_ori(azimuth_deg):
    """Compass heading in degrees (clockwise from North) -> ``.obj`` ``ori``
    radians, folded to [0, 2*pi).

    ``ori`` is simply the azimuth in radians: Condor rotates the mesh so its
    local +Y reference axis points to this compass bearing (world = ref +
    R(-ori).local).  So a north-aligned model uses ori=0; a model whose long
    axis runs 050 deg uses ori = radians(50).  VERIFIED: Slovenia2 ori values
    are exactly whole-degree headings.
    """
    import math
    return math.radians(float(azimuth_deg)) % (2.0 * math.pi)


def heading_rad_to_ori(azimuth_rad):
    """As :func:`heading_deg_to_ori` but the input bearing is already radians."""
    import math
    return float(azimuth_rad) % (2.0 * math.pi)


def footprint_to_local(coords, centroid):
    """Footprint exterior ring -> c3d LOCAL (x=East, y=North) metres about the
    centroid, ready for ``c3d.make_prism`` (which extrudes it as ori=0, i.e.
    true-north; ``ori`` then rotates the whole prism at placement).

    `coords`   : iterable of (E, N) UTM vertex pairs (the building outline).
    `centroid` : (cE, cN) UTM centroid used as the local origin.

    The polygon is NOT pre-rotated -- it keeps its true plan shape in true E/N,
    matching the Slovenia2 convention where the mesh is north-true and the only
    rotation is the per-record ``ori``.  +x is East, +y is North, consistent
    with the c3d vertex frame (px=E, py=N, pz=up).
    """
    cE, cN = centroid
    return [(float(x) - cE, float(y) - cN) for (x, y) in coords]


# =========================================================================== #
# OBJECT-GRID patch bounds + the texture<->object frame correction.
#
# THE ~45 m DRIFT (verified, see docs/OBJECT_PLACEMENT.md and the
# reference_object_texture_grid_drift memory):
#   * Textures are gdalwarp'd to patch_bounds_utm() == the 30 m DEM grid
#     (SE corner BR_EASTING/BR_NORTHING = 576000/4631040 for Skopje).
#   * Objects place on the .trn/object grid (OBJ_ANCHOR_E/N = 575955/4631085).
#   * Constant delta = (OBJ_ANCHOR_E - BR_EASTING, OBJ_ANCHOR_N - BR_NORTHING)
#     = (-45 E, +45 N) = -16/+16 texels on a 2048 patch.
#
# `patch_bounds_condor` is a SEPARATE function from `patch_bounds_utm` ON PURPOSE
# (Codex review): DEM/.tr3 EXTRACTION depends on patch_bounds_utm, so we must NOT
# redefine it. The Phase-0a permanent fix re-warps the RASTER layers (textures,
# water bake, forest) to patch_bounds_condor so the painted ground lands on the
# .trn/object grid the mesh + objects already use; then object-at-true-UTM needs
# NO correction and the installed DDS becomes a trustworthy validation raster.
# =========================================================================== #

# (dE, dN) to add to a TRUE-UTM point to target the building as PAINTED in the
# CURRENT (DEM-grid) installed textures. After the Phase-0a re-warp this becomes
# (0, 0). Sign: painted = true + correction  (= true E-45, true N+45 for Skopje).
TEXTURE_FRAME_CORRECTION = (OBJ_ANCHOR_E - BR_EASTING, OBJ_ANCHOR_N - BR_NORTHING)


def patch_bounds_condor(patch_col, patch_row, anchor=None):
    """Patch UTM box on the OBJECT/.trn grid (what objects + mesh use).

    Same shape as patch_bounds_utm but anchored at OBJ_ANCHOR (the .trn-header SE
    corner) instead of the 30 m-DEM SE corner. Use THIS as the gdalwarp ``-te`` for
    the Phase-0a texture/water/forest re-warp so the painted raster shares the
    object grid. `anchor` defaults to (OBJ_ANCHOR_E, OBJ_ANCHOR_N); pass the result
    of obj_anchor_from_trn() for an expanded landscape.
    """
    ax, ay = (OBJ_ANCHOR_E, OBJ_ANCHOR_N) if anchor is None else anchor
    e_max = ax - patch_col * PATCH_SIZE_M
    e_min = e_max - PATCH_SIZE_M
    n_min = ay + patch_row * PATCH_SIZE_M
    n_max = n_min + PATCH_SIZE_M
    return e_min, n_min, e_max, n_max


def painted_texture_xy(e, n):
    """Map a TRUE-UTM (E, N) to the point that lands on the building as PAINTED in
    the CURRENT DEM-grid installed textures (applies TEXTURE_FRAME_CORRECTION).

    Use for placement on TODAY's textures (target_frame='installed_texture_dem_grid').
    After the Phase-0a re-warp, TEXTURE_FRAME_CORRECTION is (0,0) and this is identity.
    """
    return e + TEXTURE_FRAME_CORRECTION[0], n + TEXTURE_FRAME_CORRECTION[1]


# --------------------------------------------------------------------------- #
# Datum-pinned transformers (CRS sync — the user's "no projection errors" rule).
#
# EPSG:6316 (MGI 1901 / Balkans 7) -> 32634 defaults to a 5 m-accuracy Helmert
# because no North-Macedonia transformation grid is installed. We PIN one
# operation and reuse it everywhere so the texture warp and every footprint
# reprojection share one datum realization. IMPORTANT: to place on the EXISTING
# installed textures (built with the GDAL DEFAULT op), use pinned=False so the
# footprint reproject is common-mode with those textures; switch to pinned=True
# only after a re-warp that also used the pinned op. Always verify EMPIRICALLY
# (a known cadastre point must land on the known ortho pixel) — see §4 of the doc.
# --------------------------------------------------------------------------- #
_CADASTRE_CRS_EPSG = 6316


def transformer_to_utm(src_epsg, pinned=False):
    """pyproj Transformer from `src_epsg` to UTM 34N (always_xy).

    For src 4326 (OSM/MS/Overture) the datum is clean and `pinned` is irrelevant.
    For src 6316 (cadastre/ortho) `pinned=True` requests the higher-accuracy NM
    operation; `pinned=False` (default) uses PROJ's default op — the one the
    installed DEM-grid textures were built with.
    """
    if pinned and int(src_epsg) == _CADASTRE_CRS_EPSG:
        try:
            # Prefer the higher-accuracy NM operation when available, restricting
            # candidate ops to the landscape's area of use so PROJ ranks NM first.
            from pyproj.transformer import TransformerGroup
            aoi = pyproj.aoi.AreaOfInterest(20.45, 40.85, 23.05, 42.40)  # NM bbox
            grp = TransformerGroup(f"EPSG:{src_epsg}", "EPSG:32634",
                                   always_xy=True, area_of_interest=aoi)
            if grp.transformers:
                # transformers are ordered best-accuracy-first within the AOI
                return grp.transformers[0]
        except Exception:
            pass
    return pyproj.Transformer.from_crs(f"EPSG:{src_epsg}", UTM_CRS, always_xy=True)
