#!/usr/bin/env python3
"""
Generate per-patch 512x512 uint8 forest masks for the MacedoniaSkopje
Condor 2 landscape.

Forest value encoding (matching Slovenia2 .for conventions, verified):
    0 = no trees
    1 = coniferous forest
    2 = deciduous (broadleaved) forest

ORIENTATION (critical)
----------------------
Condor stores ``.for`` (like ``.tr3``) as the ANTI-TRANSPOSE of a north-up
GDAL array: ``arr.T[::-1, ::-1]``.  Verified against Slovenia2 (anti-transpose
IoU 0.62 vs identity 0.39).  Therefore EVERY mask in this script is authored
north-up (row 0 = north, col 0 = west) and the anti-transpose is applied as the
very last step before ``tofile``.  File names are ``CCRR.for`` (no prefix),
CC = patch column (00 = EAST), RR = patch row (00 = SOUTH).

Data sources
------------
  - OSM landuse=forest / natural=wood polygons   -> crisp forest footprints
  - Copernicus HRL Dominant Leaf Type 2018 (DLT) -> species (1 broadleaf, 2 conifer)
  - Copernicus HRL Tree Cover Density 2018 (TCD) -> canopy %, fragmentation
  - ESA WorldCover 2021 (class 10 = tree)        -> tree presence
  - EEA CORINE Land Cover 2018 (311/312/313)     -> species fallback
  - Copernicus GLO-30 DEM (elevation & aspect)   -> species model + Vodno bias
  - OSM roads, rail, water, buildings, URBAN landuse, runways -> exclusions

The forest classification rasters (DLT/TCD/WorldCover) are produced by
``download_forest_rasters.py`` and cached, already warped onto the landscape
grid, in ``.sandbox/forest_rasters/``.

Outputs
-------
  - C:/Condor2/Landscapes/MacedoniaSkopje/ForestMaps/CCRR.for  (144 files)
  - validation/forest/  (overview + per-patch stats + orientation overlays)
"""

import json
import math
from pathlib import Path

import numpy as np
import pyproj
import requests
from PIL import Image
from scipy import ndimage
from shapely.geometry import Polygon, shape, box
from shapely.ops import transform as shp_transform
from shapely.prepared import prep
from shapely.strtree import STRtree

from condor_grid import (
    LANDSCAPE_NAME,
    PATCHES_X,
    PATCHES_Y,
    PATCH_MASK_SIZE,
    patch_bounds_utm,
    UTM_CRS,
    WGS84_CRS,
)
from forest_utils import (
    load_dem,
    patch_elevations,
    patch_aspect,
    load_geojson_features,
    load_forest_raster,
    patch_raster,
    buffer_roads,
    buffer_railways,
    buffer_buildings,
    buffer_waterways,
    buffer_urban,
)
from osm_io import load_geojson
from rasterize import rasterize_mask


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_NM = LANDSCAPE_NAME == "NorthMacedonia"
# All inputs/outputs are landscape-scoped so the NM build never clobbers the
# Skopje pilot (and vice-versa).
OSM_DIR = PROJECT_ROOT / ".sandbox" / ("osm_nm" if _NM else "osm")
RASTER_DIR = PROJECT_ROOT / ".sandbox" / ("forest_rasters_nm" if _NM else "forest_rasters")
OUT_DIR = Path(f"C:/Condor2/Landscapes/{LANDSCAPE_NAME}/ForestMaps")
VALIDATION_DIR = PROJECT_ROOT / "validation" / ("forest_nm" if _NM else "forest")
# The terrain agent writes the flattened NM DEM here; poll for it before running.
if _NM:
    DEM_PATH = PROJECT_ROOT / "sources" / "dem" / "northmacedonia_dem_30m_7681x6145_flat.raw"
    DEM_PATH_FALLBACK = PROJECT_ROOT / "sources" / "dem" / "northmacedonia_dem_30m_7681x6145.raw"
else:
    DEM_PATH = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305_flat.raw"
    DEM_PATH_FALLBACK = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_30m_2305.raw"
CLC_PATH = PROJECT_ROOT / ".sandbox" / "clc2018_1024.png"

# Forest classification rasters (warped to the landscape grid).
DLT_PATH = RASTER_DIR / "dlt_utm34_30m.tif"
TCD_PATH = RASTER_DIR / "tcd_utm34_30m.tif"
WC_PATH = RASTER_DIR / "worldcover_utm34_30m.tif"

# CLC2018 legend colours from EEA Discomap symbol definitions.
CLC_FOREST_RGB = {
    311: (128, 255, 0),   # broad-leaved forest -> deciduous
    312: (0, 166, 0),     # coniferous forest   -> coniferous
    313: (77, 255, 0),    # mixed forest        -> mixed
}
CLC_BBOX = (41.831486767410745, 21.082855860867607,
            42.45004875746223, 21.923851720627418)

# Tree-presence and canopy thresholds.
TCD_TREE_MIN = 40        # primary: HRL tree-cover-density % for forest presence
TCD_WC_MIN = 30          # ESA WorldCover trees count only with >= this canopy %
TCD_OSM_MIN = 20         # OSM-mapped forest kept where any real canopy exists

# Treeline.
TREELINE_TOP = 2300.0    # no trees above this
TREELINE_FADE = 2100.0   # start thinning here

# DLT raster codes.
DLT_BROADLEAF = 1
DLT_CONIFER = 2
DLT_NODATA = {0, 254, 255}

# WorldCover tree class.
WC_TREE = 10

# Mt. Vodno conifer bias (black-pine afforestation south of Skopje).  Geographic
# Gaussian centred on the summit, applied in the 500-1100 m band.
#
# SCALING NOTE (PIPELINES.md §8): this Gaussian is Skopje-SPECIFIC and is a soft,
# local boost for the Vodno/Crna Gora black-pine plantations.  For the full North
# Macedonia map it is DISABLED -- across the whole country, inflating conifer with
# a single Skopje-centred bump would be wrong, so species there rely on the HRL
# DLT prior + elevation model alone (which is data-accurate).  ``USE_VODNO_BIAS``
# is False under CONDOR_LANDSCAPE=nm and True for the Skopje pilot, keeping the
# pilot byte-identical.
USE_VODNO_BIAS = not _NM
VODNO_E = 533395.0
VODNO_N = 4645813.0
VODNO_SIGMA = 9000.0     # metres (covers the Vodno + Crna Gora pine belts)
VODNO_ELEV_LO = 500.0
VODNO_ELEV_HI = 1100.0
VODNO_MAX_BIAS = 0.45    # added to conifer prob at the centre of the bias

# CLC is a Skopje-bbox WMS snapshot; it does not cover the full NM extent, so the
# CORINE species hint is Skopje-only.  NM relies on HRL DLT + elevation.
USE_CLC = not _NM

# DLT-LED species (NM): make the HRL Dominant-Leaf-Type AUTHORITATIVE rather than a
# soft nudge.  Over the whole country the elevation curve (which trends strongly
# coniferous above ~1100 m) inflates conifer ~2.6x vs reality in the mountainous
# interior, because the DLT==1 (broadleaf) prior was only a mild -0.18 nudge.  The
# brief is explicit: rely on HRL DLT, do NOT inflate conifer.  So for NM, DLT==1 ->
# deciduous and DLT==2 -> conifer decisively (the elevation model only governs the
# ~1.3% of canopy where DLT is nodata).  Skopje keeps the original soft-prior blend
# (its Vodno black-pine belt is under-mapped by the 100 m DLT, hence the boost).
DLT_LED_SPECIES = _NM


# -----------------------------------------------------------------------------
# CLC load (optional fallback species source)
# -----------------------------------------------------------------------------

def download_clc_image(path: Path, width: int = 1024, height: int = 1024):
    """Download the CORINE Land Cover 2018 raster for the landscape bbox."""
    print("Downloading CORINE Land Cover 2018 WMS image...")
    south, west, north, east = CLC_BBOX
    bbox = f"{south},{west},{north},{east}"
    url = (
        "https://image.discomap.eea.europa.eu/arcgis/services/Corine/CLC2018_WM/MapServer/WMSServer?"
        "service=WMS&request=GetMap&version=1.3.0&layers=12&styles=default&crs=EPSG%3A4326"
        f"&bbox={bbox}&width={width}&height={height}&format=image%2Fpng24"
    )
    resp = requests.get(url, headers={"User-Agent": "condor-landscape/1.0"}, timeout=120)
    resp.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(resp.content)
    print(f"  saved {path}")


def load_clc_class_image(path: Path):
    """Return an HxW array of CLC forest class codes (311/312/313) or 0."""
    img = np.array(Image.open(path).convert("RGB"))
    h, w = img.shape[:2]
    clc = np.zeros((h, w), dtype=np.uint16)
    for code, rgb in CLC_FOREST_RGB.items():
        mask = np.all(img == rgb, axis=2)
        clc[mask] = code
    return clc


def clc_codes_for_patch(clc_array, lon, lat):
    """Vectorised CLC sampling for a patch's per-pixel lon/lat arrays (north-up)."""
    h, w = clc_array.shape
    south, west, north, east = CLC_BBOX
    pxs = ((lon - west) / (east - west) * w).astype(int)
    pys = ((north - lat) / (north - south) * h).astype(int)
    pxs = np.clip(pxs, 0, w - 1)
    pys = np.clip(pys, 0, h - 1)
    return clc_array[pys, pxs]


# -----------------------------------------------------------------------------
# Airport runway polygons (from data/airports.json)
# -----------------------------------------------------------------------------

def _runway_rectangle(center_lat, center_lon, true_heading, length_m, width_m,
                      transformer_to_utm):
    """Return a UTM Polygon for a runway rectangle."""
    cx, cy = transformer_to_utm.transform(center_lon, center_lat)
    angle = math.radians(true_heading)
    perp = angle + math.pi / 2.0
    dx = (length_m / 2.0) * math.sin(angle)
    dy = (length_m / 2.0) * math.cos(angle)
    px = (width_m / 2.0) * math.sin(perp)
    py = (width_m / 2.0) * math.cos(perp)
    corners = [
        (cx - dx - px, cy - dy - py),
        (cx + dx - px, cy + dy - py),
        (cx + dx + px, cy + dy + py),
        (cx - dx + px, cy - dy + py),
    ]
    return Polygon(corners)


def airport_runway_polygons(apron_buffer=30.0):
    """Runway rectangles from data/airports.json with an apron clearance buffer."""
    transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)
    airports_path = PROJECT_ROOT / "data" / "airports.json"
    with open(airports_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    polys = []
    for ap in data.get("airports", []):
        for rwy in ap.get("runways", []):
            poly = _runway_rectangle(
                rwy["center_lat"], rwy["center_lon"], rwy["true_heading"],
                rwy["length_m"], rwy["width_m"], transformer,
            )
            if apron_buffer:
                poly = poly.buffer(apron_buffer)
            polys.append(poly)
    return polys


# -----------------------------------------------------------------------------
# Species probability model
# -----------------------------------------------------------------------------

def _value_noise(shape, seed):
    """Deterministic smooth value noise in [0,1] (two octaves)."""
    h, w = shape
    rng = np.random.RandomState(seed)

    def _octave(gh, gw, rng):
        grid = rng.rand(gh + 1, gw + 1)
        ys = np.linspace(0, gh, h, endpoint=False)
        xs = np.linspace(0, gw, w, endpoint=False)
        yy, xx = np.meshgrid(ys, xs, indexing="ij")
        coords = np.stack([yy, xx], axis=0)
        return ndimage.map_coordinates(grid, coords, order=1, mode="reflect")

    coarse = _octave(max(2, h // 64), max(2, w // 64), rng)
    fine = _octave(max(4, h // 16), max(4, w // 16), rng)
    combined = 0.6 * coarse + 0.4 * fine
    return np.clip(combined, 0.0, 1.0)


def _vodno_bias(ee, nn, elev):
    """Geographic conifer boost over the Vodno / Crna Gora pine belts.

    A Gaussian in UTM space centred on Vodno summit, gated to the 500-1100 m
    elevation band where the black-pine afforestation actually sits.  Returns 0
    everywhere when ``USE_VODNO_BIAS`` is False (the full-NM map -- see note at
    the constant) so conifer is not artificially inflated outside Skopje.
    """
    if not USE_VODNO_BIAS:
        return np.zeros_like(np.asarray(elev, dtype=np.float32))
    d2 = (ee - VODNO_E) ** 2 + (nn - VODNO_N) ** 2
    geo = np.exp(-d2 / (2.0 * VODNO_SIGMA ** 2))
    band = np.clip(
        np.minimum((elev - VODNO_ELEV_LO) / 150.0,
                   (VODNO_ELEV_HI - elev) / 150.0),
        0.0, 1.0,
    )
    return VODNO_MAX_BIAS * geo * band


def conifer_probability(elev, aspect, clc_code, noise, ee, nn, dlt=None):
    """Probability [0,1] that a forest pixel is coniferous.

    Tuned for the Skopje region.  Blends an elevation response, an aspect
    modifier (south = drier = pine), value noise for realistic mixing, a CORINE
    class hint, the Vodno geographic bias, and (when supplied) the Copernicus
    HRL Dominant Leaf Type as a *prior*.

    DLT is used as a soft prior rather than a hard veto: the HRL product is
    100 m-era and is known to under-map small black-pine afforestation, which is
    extensive on Vodno / Skopska Crna Gora.  DLT==2 (conifer) is honoured
    directly by the caller; here DLT==2 strongly boosts and DLT==1 (broadleaf)
    nudges toward deciduous without overriding a strong elevation/Vodno signal.

    Macedonia vegetation zones (Skopje):
      < 500 m   sub-Mediterranean oak/hornbeam  -> deciduous
      500-800 m thermophilous oak, black pine plantations on slopes
      800-1100 m oak-beech, increasing pine
      1100-1500 m beech-fir, trending coniferous
      1500-2000 m coniferous
      > 2000 m  sub-alpine pine/juniper
    """
    elev = np.asarray(elev, dtype=np.float32)
    aspect = np.asarray(aspect, dtype=np.float32)
    clc_code = np.asarray(clc_code)
    noise = np.asarray(noise, dtype=np.float32)
    ee = np.asarray(ee, dtype=np.float64)
    nn = np.asarray(nn, dtype=np.float64)

    # Elevation response (shifted up vs the old curve to give realistic conifer
    # share in the dominant 500-1100 m forest band).
    p_elev = np.piecewise(
        elev,
        [
            elev < 500,
            (elev >= 500) & (elev < 800),
            (elev >= 800) & (elev < 1100),
            (elev >= 1100) & (elev < 1500),
            (elev >= 1500) & (elev < 2000),
            elev >= 2000,
        ],
        [
            0.05,                                       # lowland deciduous
            lambda e: 0.05 + (e - 500) / 300 * 0.15,    # 0.05 -> 0.20
            lambda e: 0.20 + (e - 800) / 300 * 0.25,    # 0.20 -> 0.45
            lambda e: 0.45 + (e - 1100) / 400 * 0.35,   # 0.45 -> 0.80
            lambda e: 0.80 + (e - 1500) / 500 * 0.15,   # 0.80 -> 0.95
            0.95,                                        # sub-alpine
        ],
    )

    # Aspect: south-facing mid-elevation slopes favour pine, north favours beech.
    aspect_rad = np.deg2rad(aspect)
    south_factor = -np.cos(aspect_rad)  # +1 south, -1 north
    aspect_weight = np.clip(1.0 - np.abs(elev - 1100) / 600, 0.0, 1.0)
    p_aspect = 0.12 * south_factor * aspect_weight

    # Fine-scale mixing.
    p_noise = 0.22 * (noise - 0.5)

    # CORINE class hint.
    p = p_elev.copy()
    p = np.where(clc_code == 312, 0.90, p)   # CORINE coniferous
    p = np.where(clc_code == 311, 0.06, p)   # CORINE broad-leaved
    # 313 (mixed) -> keep elevation model.

    p = p + p_aspect + p_noise + _vodno_bias(ee, nn, elev)

    if dlt is not None:
        dlt = np.asarray(dlt)
        if DLT_LED_SPECIES:
            # DLT authoritative (NM): broadleaf -> firmly deciduous, conifer ->
            # firmly conifer.  Only DLT-nodata keeps the (mild) elevation model,
            # damped toward deciduous so high-altitude nodata does not re-inflate
            # conifer.  Keeps conifer share ~ the HRL DLT truth (~8% of canopy).
            p = 0.30 * p  # damp the elevation curve where DLT is silent
            p = np.where(dlt == DLT_CONIFER, 0.95, p)
            p = np.where(dlt == DLT_BROADLEAF, 0.04, p)
        else:
            # HRL DLT soft prior (additive nudge, not a veto) -- Skopje pilot.
            p = np.where(dlt == DLT_CONIFER, p + 0.45, p)     # confirmed pine -> strong boost
            p = np.where(dlt == DLT_BROADLEAF, p - 0.18, p)   # mapped broadleaf -> mild deciduous lean

    return np.clip(p, 0.0, 1.0)


# -----------------------------------------------------------------------------
# Density-driven fragmentation
# -----------------------------------------------------------------------------

def despeckle(forest_mask):
    """Deterministic clean-up of a north-up forest mask (no RNG -> no seams).

    A 1-px binary opening removes isolated tree pixels and hairline protrusions,
    then connected components smaller than 3 px are dropped.  Both operations are
    local morphology, so results are identical regardless of patch tiling and
    introduce no per-patch boundary discontinuities.
    """
    forest_mask = forest_mask.astype(bool)
    if not forest_mask.any():
        return forest_mask
    cleaned = ndimage.binary_opening(forest_mask, iterations=1)
    labels, n = ndimage.label(cleaned)
    if n:
        sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, n + 1))
        small = np.where(sizes < 3)[0] + 1
        if small.size:
            cleaned[np.isin(labels, small)] = False
    return cleaned


# -----------------------------------------------------------------------------
# Shared state (loaded once per worker process) + per-patch worker
# -----------------------------------------------------------------------------

# Overview thumbnail resolution per patch (keeps the assembled QA image to a sane
# size: NM = 40*64 x 32*64 = 2560x2048).
THUMB = 64

# Module globals populated by ``_init_worker``.  Process pools fork/spawn a fresh
# interpreter per worker, so each loads the (read-only) rasters / geoms / DEM once
# and reuses them across its assigned patches.  This avoids pickling giant shapely
# trees through the queue.
_G = {}


def _load_shared():
    """Load every shared input (rasters, OSM geoms + trees, DEM) into a dict.

    Called once in the parent (to validate + report) and once per worker.
    """
    for p in (DLT_PATH, TCD_PATH, WC_PATH):
        if not p.exists():
            raise SystemExit(
                f"Missing forest raster: {p}\nRun scripts/download_forest_rasters.py first."
            )
    dlt_arr, dlt_aff = load_forest_raster(DLT_PATH)
    tcd_arr, tcd_aff = load_forest_raster(TCD_PATH)
    wc_arr, wc_aff = load_forest_raster(WC_PATH)

    transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)
    forest_features = load_geojson_features(OSM_DIR / "forest.geojson", transformer)
    osm_conifer, osm_deciduous = [], []
    for geom, props in forest_features:
        lt = props.get("leaf_type", "")
        lc = props.get("leaf_cycle", "")
        if lt == "needleleaved" or lc == "evergreen":
            osm_conifer.append(geom)
        elif lt == "broadleaved":
            osm_deciduous.append(geom)
    forest_geoms = [g for g, _ in forest_features]

    def _opt_features(name):
        path = OSM_DIR / name
        return load_geojson_features(path, transformer) if path.exists() else []

    water_geoms = [shape(f["geometry"]) for f in
                   load_geojson(OSM_DIR / "water.geojson").get("features", [])] \
        if (OSM_DIR / "water.geojson").exists() else []
    water_geoms = [shp_transform(lambda x, y, z=None: transformer.transform(x, y), g)
                   for g in water_geoms if g and g.is_valid]
    water_geoms = [g.buffer(5.0) for g in water_geoms]

    waterway_polys = buffer_waterways(_opt_features("waterways.geojson"))

    if (OSM_DIR / "roads_lines.geojson").exists():
        road_features = _opt_features("roads_lines.geojson")
    else:
        road_features = _opt_features("roads.geojson")
    road_polys = buffer_roads(road_features)

    if (OSM_DIR / "railways_lines.geojson").exists():
        rail_features = _opt_features("railways_lines.geojson")
    else:
        rail_features = _opt_features("railways.geojson")
    rail_polys = buffer_railways(rail_features, radius=10.0)

    building_polys = buffer_buildings(_opt_features("buildings.geojson"), radius=5.0)
    urban_polys = buffer_urban(_opt_features("settlements.geojson"))

    runway_polys = [g for g, _ in _opt_features("runways.geojson")]
    runway_polys.extend(airport_runway_polygons())

    exclusion_geoms = (water_geoms + waterway_polys + road_polys + rail_polys
                       + building_polys + urban_polys + runway_polys)

    clc_array = None
    if USE_CLC and CLC_PATH.exists():
        clc_array = load_clc_class_image(CLC_PATH)

    g = dict(
        dlt=(dlt_arr, dlt_aff), tcd=(tcd_arr, tcd_aff), wc=(wc_arr, wc_aff),
        forest_geoms=forest_geoms, osm_conifer=osm_conifer, osm_deciduous=osm_deciduous,
        exclusion_geoms=exclusion_geoms,
        forest_tree=STRtree(forest_geoms), exclusion_tree=STRtree(exclusion_geoms),
        dem=load_dem(DEM_PATH),
        utm_to_wgs84=pyproj.Transformer.from_crs(UTM_CRS, WGS84_CRS, always_xy=True),
        clc_array=clc_array,
        counts=dict(forest=len(forest_geoms), osm_conifer=len(osm_conifer),
                    osm_deciduous=len(osm_deciduous), exclusions=len(exclusion_geoms)),
    )
    return g


def _init_worker():
    _G.update(_load_shared())


def process_patch(args):
    """Generate + write one ``CCRR.for``; return (col,row,c1,c2,n_stands,thumb).

    Pure function of (patch index, shared read-only globals) -> deterministic.
    Uses the same seeded RNG as the original serial code, so for a fixed grid the
    output is byte-identical regardless of worker count / scheduling.
    """
    patch_col, patch_row = args
    g = _G
    dlt_arr, dlt_aff = g["dlt"]
    tcd_arr, tcd_aff = g["tcd"]
    wc_arr, wc_aff = g["wc"]
    forest_geoms = g["forest_geoms"]
    exclusion_geoms = g["exclusion_geoms"]
    forest_tree = g["forest_tree"]
    exclusion_tree = g["exclusion_tree"]
    dem = g["dem"]
    clc_array = g["clc_array"]
    utm_to_wgs84 = g["utm_to_wgs84"]

    bounds = patch_bounds_utm(patch_col, patch_row)
    tile_box = prep(box(*bounds))
    seed = patch_row * 100 + patch_col + 12345

    min_e, min_n, max_e, max_n = bounds
    xs = np.linspace(min_e, max_e, PATCH_MASK_SIZE, endpoint=False) \
        + (max_e - min_e) / (2 * PATCH_MASK_SIZE)
    ys = np.linspace(max_n, min_n, PATCH_MASK_SIZE, endpoint=False) \
        - (max_n - min_n) / (2 * PATCH_MASK_SIZE)
    ee, nn = np.meshgrid(xs, ys)

    forest_boundary = rasterize_mask(
        forest_geoms, bounds, PATCH_MASK_SIZE, PATCH_MASK_SIZE,
        foreground=1, background=0, tree=forest_tree, prepared_box=tile_box,
    ).astype(bool)
    exclusion_mask = rasterize_mask(
        exclusion_geoms, bounds, PATCH_MASK_SIZE, PATCH_MASK_SIZE,
        foreground=1, background=0, tree=exclusion_tree, prepared_box=tile_box,
    ).astype(bool)

    tcd = patch_raster(tcd_arr, tcd_aff, patch_col, patch_row, order=1)
    wc = patch_raster(wc_arr, wc_aff, patch_col, patch_row, order=0)
    dlt = patch_raster(dlt_arr, dlt_aff, patch_col, patch_row, order=0)

    # Continuous canopy union (NO per-patch RNG -> seam-free). Primary = TCD;
    # WorldCover fills HRL gaps; OSM forest unioned wherever any canopy exists.
    canopy = (
        (tcd >= TCD_TREE_MIN)
        | ((wc == WC_TREE) & (tcd >= TCD_WC_MIN))
        | (forest_boundary & (tcd >= TCD_OSM_MIN))
    )
    forest_mask = canopy & (~exclusion_mask)
    forest_mask = despeckle(forest_mask)
    forest_mask = forest_mask & (~exclusion_mask)

    elev = patch_elevations(dem, patch_col, patch_row)
    aspect = patch_aspect(dem, patch_col, patch_row)
    noise = _value_noise((PATCH_MASK_SIZE, PATCH_MASK_SIZE), seed=seed)
    if clc_array is not None:
        lon, lat = utm_to_wgs84.transform(ee, nn)
        clc_codes = clc_codes_for_patch(clc_array, lon, lat)
    else:
        clc_codes = np.zeros((PATCH_MASK_SIZE, PATCH_MASK_SIZE), dtype=np.uint16)

    # Species: explicit OSM leaf tags win; else probability model (HRL DLT soft
    # prior + elevation/aspect; Vodno bias disabled for NM); DLT==2 hard floor.
    species = np.zeros((PATCH_MASK_SIZE, PATCH_MASK_SIZE), dtype=np.uint8)
    osm_typed = np.zeros((PATCH_MASK_SIZE, PATCH_MASK_SIZE), dtype=bool)
    if g["osm_conifer"]:
        m = rasterize_mask(g["osm_conifer"], bounds, PATCH_MASK_SIZE,
                           PATCH_MASK_SIZE, 1, 0).astype(bool)
        species[m] = 1
        osm_typed |= m
    if g["osm_deciduous"]:
        m = rasterize_mask(g["osm_deciduous"], bounds, PATCH_MASK_SIZE,
                           PATCH_MASK_SIZE, 1, 0).astype(bool)
        sel = (~osm_typed) & m
        species[sel] = 2
        osm_typed |= m

    dlt_valid = np.where(np.isin(dlt, list(DLT_NODATA)), 0, dlt)
    decide = forest_mask & (~osm_typed)
    if decide.any():
        p = conifer_probability(
            elev[decide], aspect[decide], clc_codes[decide],
            noise[decide], ee[decide], nn[decide], dlt=dlt_valid[decide],
        )
        species[decide] = np.where(p > 0.5, 1, 2).astype(np.uint8)
    species[forest_mask & (dlt == DLT_CONIFER)] = 1

    forest_type = (species * forest_mask).astype(np.uint8)

    forest_type[elev > TREELINE_TOP] = 0
    transition = (elev >= TREELINE_FADE) & (elev <= TREELINE_TOP)
    if transition.any():
        thin_prob = (elev[transition] - TREELINE_FADE) / (TREELINE_TOP - TREELINE_FADE)
        trng = np.random.RandomState(seed=seed + 99999)
        trand = trng.rand(int(transition.sum()))
        forest_type[transition] = np.where(
            trand < thin_prob, 0, forest_type[transition]).astype(np.uint8)

    _, n_stands = ndimage.label(forest_type > 0)

    # ANTI-TRANSPOSE to Condor storage order, then write.
    stored = forest_type.T[::-1, ::-1]
    filename = f"{patch_col:02d}{patch_row:02d}.for"
    stored.tofile(OUT_DIR / filename)

    c1 = int((forest_type == 1).sum())
    c2 = int((forest_type == 2).sum())

    # Downsampled thumbnail (north-up) for the assembled overview.
    thumb = forest_type[::PATCH_MASK_SIZE // THUMB, ::PATCH_MASK_SIZE // THUMB].copy()
    return patch_col, patch_row, c1, c2, n_stands, thumb


# -----------------------------------------------------------------------------
# Main generation (parallel across cores)
# -----------------------------------------------------------------------------

def main():
    import argparse
    import os
    import time
    from concurrent.futures import ProcessPoolExecutor

    global DEM_PATH

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4)),
                    help="parallel worker processes (default = all cores)")
    ap.add_argument("--wait-dem", type=int, default=0,
                    help="poll up to N seconds for the DEM to appear before failing")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Landscape: {LANDSCAPE_NAME}  ({PATCHES_X}x{PATCHES_Y} = "
          f"{PATCHES_X * PATCHES_Y} patches)")
    print(f"  OSM:    {OSM_DIR}")
    print(f"  raster: {RASTER_DIR}")
    print(f"  DEM:    {DEM_PATH}")
    print(f"  out:    {OUT_DIR}")

    # ---- poll for the DEM (the terrain agent produces it) ----
    deadline = time.time() + args.wait_dem
    while not DEM_PATH.exists():
        if DEM_PATH_FALLBACK.exists():
            print(f"  flattened DEM absent; using unflattened fallback {DEM_PATH_FALLBACK.name}")
            DEM_PATH = DEM_PATH_FALLBACK
            break
        if time.time() >= deadline:
            raise SystemExit(
                f"DEM not found: {DEM_PATH}\n"
                f"  (fallback {DEM_PATH_FALLBACK} also missing)\n"
                "  The terrain agent must produce the NM DEM first. Re-run with "
                "--wait-dem 1800 to poll."
            )
        print(f"  waiting for DEM {DEM_PATH.name} ... ({int(deadline - time.time())}s left)")
        time.sleep(20)

    # ---- validate inputs + report layer counts (parent loads once) ----
    shared = _load_shared()
    c = shared["counts"]
    print(f"Loaded: forest={c['forest']} (conifer-tag={c['osm_conifer']}, "
          f"deciduous-tag={c['osm_deciduous']}), exclusions={c['exclusions']}, "
          f"DEM {shared['dem'].shape}")
    del shared  # workers reload their own copy

    patches = [(col, row) for row in range(PATCHES_Y) for col in range(PATCHES_X)]
    print(f"Generating {len(patches)} patches on {args.workers} workers...")

    overview = np.zeros((PATCHES_Y * THUMB, PATCHES_X * THUMB), dtype=np.uint8)
    total = {"conifer": 0, "deciduous": 0}
    stand_count_total = 0
    done = 0
    t0 = time.time()

    def _consume(res):
        nonlocal done, stand_count_total
        patch_col, patch_row, c1, c2, n_stands, thumb = res
        total["conifer"] += c1
        total["deciduous"] += c2
        stand_count_total += n_stands
        gy = (PATCHES_Y - 1 - patch_row) * THUMB
        gx = (PATCHES_X - 1 - patch_col) * THUMB
        overview[gy:gy + THUMB, gx:gx + THUMB] = thumb
        done += 1
        if done % 50 == 0 or done == len(patches):
            rate = done / max(1e-6, time.time() - t0)
            eta = (len(patches) - done) / max(1e-6, rate)
            print(f"  {done}/{len(patches)} patches  ({rate:.1f}/s, ETA {eta:.0f}s)")

    if args.workers <= 1:
        _init_worker()
        for pc in patches:
            _consume(process_patch(pc))
    else:
        with ProcessPoolExecutor(max_workers=args.workers,
                                 initializer=_init_worker) as ex:
            for res in ex.map(process_patch, patches, chunksize=4):
                _consume(res)

    # ---- validation (overview already at THUMB resolution) ----
    _write_validation_images(overview, shared_dem=None, total=total,
                             stand_count_total=stand_count_total)

    n_patches = PATCHES_X * PATCHES_Y
    forest_px = total["conifer"] + total["deciduous"]
    print(f"\nForest map generation complete ({time.time() - t0:.0f}s).")
    print(f"  Conifer:   {total['conifer']:,}")
    print(f"  Deciduous: {total['deciduous']:,}")
    print(f"  Forest fraction: {forest_px / (n_patches * 512 * 512):.2%}")
    if forest_px:
        print(f"  Conifer share of forest: {total['conifer'] / forest_px:.2%}")
    print(f"  Total stands (connected components): {stand_count_total:,}")


def _write_validation_images(overview, shared_dem, total, stand_count_total):
    """Geographic overview RGB (north-up) + stats text.

    ``overview`` is the assembled THUMB-resolution mask (north-up: row 0 = north,
    col 0 = west).  Conifer green, deciduous brown, no-tree dark grey, with a
    light patch grid.  At THUMB px/patch the NM overview is 2560x2048.
    """
    oh, ow = overview.shape
    rgb = np.full((oh, ow, 3), 30, dtype=np.uint8)   # dark grey no-tree base
    rgb[overview == 1] = [34, 139, 34]    # conifer = dark green
    rgb[overview == 2] = [210, 105, 30]   # deciduous = warm brown

    from PIL import ImageDraw as PilImageDraw
    img = Image.fromarray(rgb)
    draw = PilImageDraw.Draw(img)
    for col in range(PATCHES_X + 1):
        x = col * THUMB
        if x < ow:
            draw.line([(x, 0), (x, oh - 1)], fill=(80, 80, 80), width=1)
    for row in range(PATCHES_Y + 1):
        y = row * THUMB
        if y < oh:
            draw.line([(0, y), (ow - 1, y)], fill=(80, 80, 80), width=1)
    img.save(VALIDATION_DIR / "forest_map_overview.png")

    # Stats are over the THUMB overview (a representative sample of the full
    # 512-px masks); fractions match the full-res .for to within sampling noise.
    total_px = oh * ow
    n_con = int((overview == 1).sum())
    n_dec = int((overview == 2).sum())
    n_forest = n_con + n_dec
    # Full-resolution authoritative pixel totals come from the accumulated counts.
    full_forest = total["conifer"] + total["deciduous"]
    full_total = PATCHES_X * PATCHES_Y * PATCH_MASK_SIZE * PATCH_MASK_SIZE
    with open(VALIDATION_DIR / "forest_stats.txt", "w") as f:
        f.write("Forest map statistics\n=====================\n")
        f.write(f"Landscape: {LANDSCAPE_NAME}\n")
        f.write(f"Grid: {PATCHES_X}x{PATCHES_Y} patches, {PATCH_MASK_SIZE}x{PATCH_MASK_SIZE} px\n")
        f.write(f"Full-res pixels:   {full_total:>14,}\n")
        f.write(f"Forest pixels:     {full_forest:>14,}  ({full_forest/full_total:.2%})\n")
        f.write(f"  Coniferous:      {total['conifer']:>14,}  ({total['conifer']/full_total:.2%})\n")
        f.write(f"  Deciduous:       {total['deciduous']:>14,}  ({total['deciduous']/full_total:.2%})\n")
        f.write(f"  Conifer share:   {total['conifer']/max(full_forest,1):.2%} of forest\n")
        f.write(f"Total stands:      {stand_count_total:>14,}\n")
        f.write(f"\nOverview thumbnail ({THUMB}px/patch): {ow}x{oh}, "
                f"forest {n_forest/total_px:.2%}\n")
    print(f"Saved validation overview + stats to {VALIDATION_DIR}")


if __name__ == "__main__":
    main()
