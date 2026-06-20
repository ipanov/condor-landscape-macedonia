#!/usr/bin/env python3
"""
Generate improved 512x512 uint8 forest masks for each Condor patch.

Forest value encoding (matching Slovenia2 .for conventions):
    0 = no trees
    1 = coniferous forest
    2 = deciduous forest

Data sources:
  - OSM landuse=forest / natural=wood polygons (forest extent)
  - OSM leaf_type / leaf_cycle tags (explicit type when present)
  - EEA CORINE Land Cover 2018 WMS (broadleaved / coniferous / mixed)
  - Copernicus GLO-30 DEM (elevation & aspect driven mixing)
  - OSM roads, railways, buildings, water, runways (exclusions)

The script writes:
  - C:/Condor2/Landscapes/MacedoniaSkopje/ForestMaps/CCRR.for
  - .sandbox/forest_validation/ overview images
"""

import json
import math
import urllib.parse
from pathlib import Path

import numpy as np
import pyproj
import requests
from PIL import Image
from scipy import ndimage
from shapely.geometry import Polygon, LineString, shape, box
from shapely.ops import transform as shp_transform
from shapely.prepared import prep
from shapely.strtree import STRtree

from condor_grid import (
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
    buffer_roads,
    buffer_railways,
    buffer_buildings,
    buffer_waterways,
)
from osm_io import load_geojson
from rasterize import rasterize_mask


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OSM_DIR = PROJECT_ROOT / ".sandbox" / "osm"
OUT_DIR = Path("C:/Condor2/Landscapes/MacedoniaSkopje/ForestMaps")
VALIDATION_DIR = PROJECT_ROOT / ".sandbox" / "forest_validation"
DEM_PATH = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_2305_flat.raw"
CLC_PATH = PROJECT_ROOT / ".sandbox" / "clc2018_1024.png"

# CLC2018 legend colours from EEA Discomap symbol definitions.
CLC_FOREST_RGB = {
    311: (128, 255, 0),   # broad-leaved forest -> deciduous
    312: (0, 166, 0),     # coniferous forest   -> coniferous
    313: (77, 255, 0),    # mixed forest        -> mixed
}

# WMS bbox used to fetch CLC image (lat,lon order for EPSG:4326 in WMS 1.3.0)
CLC_BBOX = (41.831486767410745, 21.082855860867607, 42.45004875746223, 21.923851720627418)


# -----------------------------------------------------------------------------
# CLC download / load
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


def clc_class_at(clc_array, lon, lat):
    """Sample the CLC image at a WGS-84 lon/lat point."""
    south, west, north, east = CLC_BBOX
    h, w = clc_array.shape
    if lon < west or lon > east or lat < south or lat > north:
        return 0
    px = int((lon - west) / (east - west) * w)
    py = int((north - lat) / (north - south) * h)
    px = np.clip(px, 0, w - 1)
    py = np.clip(py, 0, h - 1)
    return int(clc_array[py, px])


# -----------------------------------------------------------------------------
# Airport runway polygons (from data/airports.json)
# -----------------------------------------------------------------------------

def _runway_rectangle(center_lat, center_lon, true_heading, length_m, width_m,
                      transformer_to_utm):
    """Return a UTM Polygon for a runway rectangle."""
    # Convert center to UTM
    cx, cy = transformer_to_utm.transform(center_lon, center_lat)
    # Heading is true bearing clockwise from north.  Compute half-diagonal corners.
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


def airport_runway_polygons():
    transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)
    airports_path = PROJECT_ROOT / "data" / "airports.json"
    with open(airports_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    polys = []
    for ap in data.get("airports", []):
        for rwy in ap.get("runways", []):
            poly = _runway_rectangle(
                rwy["center_lat"],
                rwy["center_lon"],
                rwy["true_heading"],
                rwy["length_m"],
                rwy["width_m"],
                transformer,
            )
            polys.append(poly)
    return polys


# -----------------------------------------------------------------------------
# Forest type assignment
# -----------------------------------------------------------------------------

def _value_noise(shape, seed):
    """Deterministic smooth value noise in [0,1].

    Uses two octaves (coarse + fine) to produce spatially coherent patches
    that break up large monotone blobs into many smaller forest clusters.
    """
    h, w = shape
    rng = np.random.RandomState(seed)

    def _octave(gh, gw, rng):
        grid = rng.rand(gh + 1, gw + 1)
        ys = np.linspace(0, gh, h, endpoint=False)
        xs = np.linspace(0, gw, w, endpoint=False)
        yy, xx = np.meshgrid(ys, xs, indexing="ij")
        coords = np.stack([yy, xx], axis=0)
        return ndimage.map_coordinates(grid, coords, order=1, mode="reflect")

    # Coarse octave: ~64 px period (landscape-scale variation)
    coarse = _octave(max(2, h // 64), max(2, w // 64), rng)
    # Fine octave: ~16 px period (patch-scale fragmentation)
    fine = _octave(max(4, h // 16), max(4, w // 16), rng)

    # Blend: 60% coarse + 40% fine.  This creates large regions with
    # smaller internal structure, yielding many small forest clearings.
    combined = 0.6 * coarse + 0.4 * fine
    return np.clip(combined, 0.0, 1.0)


def _conifer_probability(elev, aspect, clc_code, noise):
    """
    Return probability [0,1] that a forest pixel is coniferous.

    Macedonia vegetation zones (Skopje region):
      < 500 m   : sub-Mediterranean oak/hornbeam  -> almost entirely deciduous
      500-800 m : thermophilous oak zone           -> deciduous dominant, rare pine
      800-1100 m: beech zone, some black pine      -> increasingly mixed
      1100-1500 m: beech-fir transition            -> mixed, trending coniferous
      1500-2000 m: coniferous (Scots/Bosnian pine, fir, spruce)
      > 2000 m  : sub-alpine pine/juniper          -> strongly coniferous
      > 2300 m  : treeline                         -> no trees (handled elsewhere)

    South-facing slopes at mid-elevations tend toward drier pine communities;
    north-facing slopes favour beech (deciduous).
    """
    elev = np.asarray(elev, dtype=np.float32)
    aspect = np.asarray(aspect, dtype=np.float32)
    clc_code = np.asarray(clc_code)
    noise = np.asarray(noise, dtype=np.float32)

    # Piecewise-linear elevation response tuned for Macedonia.
    # Returns the "base" conifer probability from elevation alone.
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
            0.03,                                        # lowland deciduous
            lambda e: 0.03 + (e - 500) / 300 * 0.07,    # 0.03 -> 0.10
            lambda e: 0.10 + (e - 800) / 300 * 0.25,    # 0.10 -> 0.35
            lambda e: 0.35 + (e - 1100) / 400 * 0.35,   # 0.35 -> 0.70
            lambda e: 0.70 + (e - 1500) / 500 * 0.20,   # 0.70 -> 0.90
            0.95,                                        # sub-alpine
        ],
    )

    # Aspect modifier: south-facing mid-elevation slopes get a conifer boost
    # (drier = more pine), north-facing get a deciduous boost (cooler = beech).
    aspect_rad = np.deg2rad(aspect)
    south_factor = -np.cos(aspect_rad)  # +1 for south, -1 for north
    # Aspect effect is strongest in the mixed zone (800-1500 m) and weak
    # at extremes where the outcome is already decided.
    aspect_weight = np.clip(1.0 - np.abs(elev - 1100) / 600, 0.0, 1.0)
    p_aspect = 0.12 * south_factor * aspect_weight

    # Noise introduces fine-scale realistic mixing (patches of pine among beech).
    p_noise = 0.25 * (noise - 0.5)

    # Base probability from CORINE class when available.
    p_base = p_elev.copy()
    p_base = np.where(clc_code == 312, 0.90, p_base)   # CORINE coniferous
    p_base = np.where(clc_code == 311, 0.08, p_base)   # CORINE broad-leaved
    p_base = np.where(clc_code == 313, p_elev, p_base)  # CORINE mixed -> use elevation model

    p = p_base + p_aspect + p_noise
    return np.clip(p, 0.0, 1.0)


# -----------------------------------------------------------------------------
# Main generation
# -----------------------------------------------------------------------------

def _utm_to_wgs84(e, n):
    transformer = pyproj.Transformer.from_crs(UTM_CRS, WGS84_CRS, always_xy=True)
    return transformer.transform(e, n)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # Load / cache CLC image
    # -------------------------------------------------------------------------
    if not CLC_PATH.exists():
        download_clc_image(CLC_PATH)
    clc_array = load_clc_class_image(CLC_PATH)
    print(f"Loaded CLC image {clc_array.shape}, forest pixels: {(clc_array != 0).sum()}")

    # -------------------------------------------------------------------------
    # Load OSM data
    # -------------------------------------------------------------------------
    transformer = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)

    forest_features = load_geojson_features(OSM_DIR / "forest.geojson", transformer)
    print(f"Loaded {len(forest_features)} forest/wood features")

    # Split OSM forest by explicit leaf_type tags.
    osm_conifer = []
    osm_deciduous = []
    osm_mixed = []
    osm_unknown = []
    for geom, props in forest_features:
        lt = props.get("leaf_type", "")
        lc = props.get("leaf_cycle", "")
        if lt == "needleleaved" or lc == "evergreen":
            osm_conifer.append(geom)
        elif lt == "broadleaved":
            osm_deciduous.append(geom)
        elif lt == "mixed":
            osm_mixed.append(geom)
        else:
            osm_unknown.append(geom)
    print(f"  OSM leaf type: conifer={len(osm_conifer)}, deciduous={len(osm_deciduous)}, "
          f"mixed={len(osm_mixed)}, unknown={len(osm_unknown)}")

    # Exclusion layers
    # ---- Water bodies (polygonal: lakes, reservoirs) ----
    water_geoms = [shape(f["geometry"]) for f in load_geojson(OSM_DIR / "water.geojson").get("features", [])]
    water_geoms = [shp_transform(lambda x, y, z=None: transformer.transform(x, y), g)
                   for g in water_geoms if g and g.is_valid]

    # ---- Waterways (linear: rivers, streams, canals) ----
    waterway_features = load_geojson_features(OSM_DIR / "waterways.geojson", transformer)
    waterway_polys = buffer_waterways(waterway_features)

    # ---- Roads: use roads_lines.geojson (LineStrings) for best coverage ----
    roads_lines_path = OSM_DIR / "roads_lines.geojson"
    roads_path = OSM_DIR / "roads.geojson"
    if roads_lines_path.exists():
        road_features = load_geojson_features(roads_lines_path, transformer)
        print(f"  Using roads_lines.geojson ({len(road_features)} line features)")
    else:
        road_features = load_geojson_features(roads_path, transformer)
        print(f"  Fallback to roads.geojson ({len(road_features)} polygon features)")
    road_polys = buffer_roads(road_features)

    # ---- Railways ----
    rail_features = load_geojson_features(OSM_DIR / "railways.geojson", transformer)
    rail_polys = buffer_railways(rail_features, radius=10.0)

    # ---- Buildings (with 5 m clearance buffer) ----
    building_features = load_geojson_features(OSM_DIR / "buildings.geojson", transformer)
    building_polys = buffer_buildings(building_features, radius=5.0)

    # ---- Runways ----
    runway_features = load_geojson_features(OSM_DIR / "runways.geojson", transformer)
    runway_polys = [g for g, _ in runway_features]
    runway_polys.extend(airport_runway_polygons())

    exclusion_geoms = (water_geoms + waterway_polys + road_polys + rail_polys
                       + building_polys + runway_polys)
    print(f"Loaded exclusion geometries: water={len(water_geoms)}, "
          f"waterways={len(waterway_polys)}, roads={len(road_polys)}, "
          f"rail={len(rail_polys)}, buildings={len(building_polys)}, "
          f"runways={len(runway_polys)}")

    # -------------------------------------------------------------------------
    # Load DEM
    # -------------------------------------------------------------------------
    dem = load_dem(DEM_PATH)
    print(f"Loaded DEM {dem.shape}")

    # Spatial indexes (re-used for every patch)
    forest_geoms = [g for g, _ in forest_features]
    forest_tree = STRtree(forest_geoms)
    exclusion_tree = STRtree(exclusion_geoms)
    utm_to_wgs84 = pyproj.Transformer.from_crs(UTM_CRS, WGS84_CRS, always_xy=True)

    # -------------------------------------------------------------------------
    # Rasterize patch by patch
    # -------------------------------------------------------------------------
    overview = np.zeros((PATCHES_Y * PATCH_MASK_SIZE, PATCHES_X * PATCH_MASK_SIZE), dtype=np.uint8)
    total_pixels = {"conifer": 0, "deciduous": 0}

    for patch_row in range(PATCHES_Y):
        for patch_col in range(PATCHES_X):
            bounds = patch_bounds_utm(patch_col, patch_row)
            tile_box = prep(box(*bounds))

            # 1) Forest extent from OSM
            forest_mask = rasterize_mask(
                forest_geoms,
                bounds,
                PATCH_MASK_SIZE,
                PATCH_MASK_SIZE,
                foreground=1,
                background=0,
                tree=forest_tree,
                prepared_box=tile_box,
            )

            # 2) Exclusions
            exclusion_mask = rasterize_mask(
                exclusion_geoms,
                bounds,
                PATCH_MASK_SIZE,
                PATCH_MASK_SIZE,
                foreground=1,
                background=0,
                tree=exclusion_tree,
                prepared_box=tile_box,
            )

            forest_mask = (forest_mask & (~exclusion_mask.astype(bool))).astype(np.uint8)

            # 3) Explicit OSM leaf-type masks
            mask_conifer = rasterize_mask(
                osm_conifer, bounds, PATCH_MASK_SIZE, PATCH_MASK_SIZE, foreground=1, background=0
            ).astype(bool)
            mask_deciduous = rasterize_mask(
                osm_deciduous, bounds, PATCH_MASK_SIZE, PATCH_MASK_SIZE, foreground=1, background=0
            ).astype(bool)
            mask_mixed = rasterize_mask(
                osm_mixed, bounds, PATCH_MASK_SIZE, PATCH_MASK_SIZE, foreground=1, background=0
            ).astype(bool)

            # 4) Elevation & aspect
            elev = patch_elevations(dem, patch_col, patch_row)
            aspect = patch_aspect(dem, patch_col, patch_row)

            # 5) Value noise for realistic mixing
            noise = _value_noise((PATCH_MASK_SIZE, PATCH_MASK_SIZE),
                                 seed=patch_row * 100 + patch_col + 12345)

            # 6) Sample CLC for every pixel (only inside forest)
            min_e, min_n, max_e, max_n = bounds
            xs = np.linspace(min_e, max_e, PATCH_MASK_SIZE, endpoint=False) + (max_e - min_e) / (2 * PATCH_MASK_SIZE)
            ys = np.linspace(max_n, min_n, PATCH_MASK_SIZE, endpoint=False) - (max_n - min_n) / (2 * PATCH_MASK_SIZE)
            ee, nn = np.meshgrid(xs, ys)
            lon, lat = utm_to_wgs84.transform(ee, nn)

            # vectorized CLC sampling
            clc_codes = np.zeros((PATCH_MASK_SIZE, PATCH_MASK_SIZE), dtype=np.uint16)
            h, w = clc_array.shape
            south, west, north, east = CLC_BBOX
            pxs = ((lon - west) / (east - west) * w).astype(int)
            pys = ((north - lat) / (north - south) * h).astype(int)
            pxs = np.clip(pxs, 0, w - 1)
            pys = np.clip(pys, 0, h - 1)
            clc_codes = clc_array[pys, pxs]

            # 7) Assign types
            forest_type = np.zeros((PATCH_MASK_SIZE, PATCH_MASK_SIZE), dtype=np.uint8)

            # OSM explicit tags override everything
            forest_type[mask_conifer & forest_mask.astype(bool)] = 1
            forest_type[mask_deciduous & forest_mask.astype(bool)] = 2

            # Mixed / unknown / untagged forest pixels
            decide_mask = forest_mask.astype(bool) & (~mask_conifer) & (~mask_deciduous)
            if decide_mask.any():
                p_conifer = _conifer_probability(
                    elev[decide_mask],
                    aspect[decide_mask],
                    clc_codes[decide_mask],
                    noise[decide_mask],
                )
                # Deterministic per-pixel choice
                # Use the smooth probability surface directly; this creates
                # spatially coherent conifer/deciduous patches instead of a
                # salt-and-pepper speckle.
                chosen = np.where(p_conifer > 0.5, 1, 2)
                forest_type[decide_mask] = chosen.astype(np.uint8)

            # 7b) Treeline exclusion: remove trees above ~2300 m
            # Gradual fade: full forest below 2100 m, random thinning
            # 2100-2300 m, no trees above 2300 m.
            treeline_mask = elev > 2300
            forest_type[treeline_mask] = 0
            # Thin out the transition zone (2100-2300 m)
            transition = (elev >= 2100) & (elev <= 2300)
            if transition.any():
                thin_prob = (elev[transition] - 2100.0) / 200.0  # 0 at 2100, 1 at 2300
                thin_rng = np.random.RandomState(seed=patch_row * 100 + patch_col + 99999)
                thin_rand = thin_rng.rand(int(transition.sum()))
                forest_type[transition] = np.where(
                    thin_rand < thin_prob, 0, forest_type[transition]
                ).astype(np.uint8)

            # 8) NORTH-UP UTM (row 0 = north, col 0 = west) — identical to the
            # .dds textures and .tr3 (3D-engine convention). forest_type is built
            # north-up above (ys descend from north), so NO flip is applied.

            # 9) Write
            filename = f"{patch_col:02d}{patch_row:02d}.for"
            out_path = OUT_DIR / filename
            forest_type.tofile(out_path)

            c1 = int((forest_type == 1).sum())
            c2 = int((forest_type == 2).sum())
            total_pixels["conifer"] += c1
            total_pixels["deciduous"] += c2

            # Fill overview (top-left orientation for validation image)
            overview[patch_row * PATCH_MASK_SIZE:(patch_row + 1) * PATCH_MASK_SIZE,
                     patch_col * PATCH_MASK_SIZE:(patch_col + 1) * PATCH_MASK_SIZE] = forest_type

            print(f"Wrote {filename}: conifer={c1:>6} deciduous={c2:>6} "
                  f"excluded={int((forest_mask == 0).sum()):>6}")

    # -------------------------------------------------------------------------
    # Validation images
    # -------------------------------------------------------------------------
    _write_validation_images(overview, dem)

    print("\nForest map generation complete.")
    print(f"  Total conifer pixels:   {total_pixels['conifer']:,}")
    print(f"  Total deciduous pixels: {total_pixels['deciduous']:,}")
    print(f"  Forest fraction: {(total_pixels['conifer'] + total_pixels['deciduous']) / (144 * 512 * 512):.2%}")


def _write_validation_images(overview, dem):
    """Create overview RGB images of the new forest map."""
    from scipy.ndimage import zoom
    from PIL import ImageDraw as PilImageDraw

    oh, ow = overview.shape

    # Resample DEM to overview size for hillshade background
    dem_f = dem.astype(np.float32)
    bg = zoom(dem_f, (oh / dem.shape[0], ow / dem.shape[1]), order=1)

    # Simple analytical hillshade (azimuth 315, altitude 45)
    dy, dx = np.gradient(bg)
    slope = np.sqrt(dx ** 2 + dy ** 2)
    shade = (dx * 0.7071 + dy * 0.7071) / (np.sqrt(slope ** 2 + 1))
    shade = ((shade + 1) * 0.5 * 200 + 28).clip(0, 255).astype(np.uint8)

    rgb = np.zeros((oh, ow, 3), dtype=np.uint8)
    rgb[..., 0] = shade
    rgb[..., 1] = shade
    rgb[..., 2] = shade

    # Conifer = dark green, deciduous = warm brown
    conifer_mask = overview == 1
    deciduous_mask = overview == 2
    rgb[conifer_mask] = [34, 139, 34]
    rgb[deciduous_mask] = [210, 105, 30]

    img = Image.fromarray(rgb)
    draw = PilImageDraw.Draw(img)

    # Draw patch grid lines
    for col in range(PATCHES_X + 1):
        x = col * PATCH_MASK_SIZE
        if x < ow:
            draw.line([(x, 0), (x, oh - 1)], fill=(80, 80, 80), width=1)
    for row in range(PATCHES_Y + 1):
        y = row * PATCH_MASK_SIZE
        if y < oh:
            draw.line([(0, y), (ow - 1, y)], fill=(80, 80, 80), width=1)

    img.save(VALIDATION_DIR / "forest_map_overview.png")

    # ---- Statistics summary ----
    total = oh * ow
    n_conifer = int(conifer_mask.sum())
    n_deciduous = int(deciduous_mask.sum())
    n_forest = n_conifer + n_deciduous
    with open(VALIDATION_DIR / "forest_stats.txt", "w") as f:
        f.write(f"Forest map statistics\n")
        f.write(f"=====================\n")
        f.write(f"Grid: {PATCHES_X}x{PATCHES_Y} patches, {PATCH_MASK_SIZE}x{PATCH_MASK_SIZE} px each\n")
        f.write(f"Total pixels:     {total:>12,}\n")
        f.write(f"Forest pixels:    {n_forest:>12,}  ({n_forest/total:.2%})\n")
        f.write(f"  Coniferous:     {n_conifer:>12,}  ({n_conifer/total:.2%})\n")
        f.write(f"  Deciduous:      {n_deciduous:>12,}  ({n_deciduous/total:.2%})\n")
        f.write(f"  Conifer ratio:  {n_conifer/max(n_forest,1):.2%} of forest\n")
        f.write(f"No-forest pixels: {total-n_forest:>12,}  ({(total-n_forest)/total:.2%})\n")

    print(f"Saved validation images to {VALIDATION_DIR}")


if __name__ == "__main__":
    main()
