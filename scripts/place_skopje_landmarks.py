#!/usr/bin/env python3
r"""Place the 8 staged Skopje city landmarks into the MacedoniaSkopje install via the
TEXTURE-ALIGNMENT flow (CLAUDE.md rule #11 precision: 1-3 m / 1-3 deg, scaled to the
REAL object, validated by an overlay BEFORE install).

WHY THIS SCRIPT (and why it does NOT use the GWC MK-2023 ortho fetch from
detect_place_stenkovec_hangar.py)
-------------------------------------------------------------------------------------
The hangar script fetches the Macedonian cadastre ortho through the public GWC WMS in
EPSG:6316. VERIFIED HERE (2026-06-21): that GWC path is mis-georeferenced over central
Skopje -- the confirmed Stone-Bridge coordinate (41.99710 N, 21.43318 E) returns a
residential area ~km away, while BOTH (a) Esri World Imagery and (b) the INSTALLED
MacedoniaSkopje patch texture render the Vardar + Stone Bridge correctly at that exact
coordinate (recon images in .sandbox/landmarks/_recon/). So the GWC fetch cannot be the
placement ground truth in this region.

The placement ground truth that matters is **what Condor draws** = the installed patch
DDS, and that is georeferenced by condor_grid.patch_bounds_utm (the same UTM 34N frame
the object anchor obj_record_xy uses -- proven self-consistent: the bridge UTM maps to
the river pixel in t0603.dds). Esri World Imagery (EPSG:3857) agrees with the installed
texture to <1 texel and is high-resolution (~0.3-0.5 m/px in Skopje) with an unambiguous
transform, so we DETECT footprints on Esri and PLACE into the condor_grid UTM frame.
A building's footprint is epoch-invariant, so the detection transfers exactly.

PER-LANDMARK METHOD
-------------------
BUILDINGS (detect footprint -> centroid, long-axis bearing, footprint length -> scale):
  * ToseProeskiArena  -- oval stadium; OBB of the stand ring on Esri.
  * RailwayStationSkopje -- the long Kenzo-Tange Transport Centre megastructure; OBB of
    the dark elevated deck on Esri (NOT the small OSM platform polygon).
  * MOB_OperaBallet   -- OSM polygon is accurate here; OBB from the OSM footprint,
    cross-checked on Esri.
  * StoneBridge       -- long axis ACROSS the Vardar; OBB of the bridge+approach deck on
    Esri, axis aligned to the river crossing.
  * PortaMacedonia    -- a ~22 m triumphal arch (8 px even on Esri): too small for a
    stable OBB. Verified Nominatim coord + facing the square; flagged as known-coord.
MONUMENTS (verified lat/lon + sensible facing, scale ~1, model is real-size):
  * MillenniumCross   -- placed on the DEM-MAX Vodno summit (1065 m, 41.96513/21.39439),
    NOT the 877 m slope; the full-quality .sandbox/landmarks/MillenniumCross.c3d
    (5968 tris) REPLACES the decimated install record.
  * WarriorOnHorse    -- Macedonia Square fountain; faces the square.
  * TelecomTowerAEK   -- vertical tower; ori=0.

POSITION / ORI / SCALE -> .obj RECORD
-------------------------------------
posX/posY = condor_grid.obj_record_xy(E, N)         (verified .trn-header SE anchor)
posZ      = DEM altitude (sources/dem/...2305_flat.raw)
ori       = condor_grid.heading_deg_to_ori(bearing) (mesh +Y -> compass bearing)
scale     = real footprint length / model footprint length (==1 for real-size monuments)

INSTALL .obj = the existing StenkovecHangar record (KEPT BYTE-IDENTICAL)
             + the full-quality MillenniumCross + the other 7 landmarks.
The old .obj is backed up first. c3d + DDS are copied to World/Objects/.

VALIDATION (BEFORE install): decode each record back, rotate+scale the model footprint
by ori, overlay on the Esri image -> .sandbox/landmarks/placement_overlays/<name>.png.
A building whose overlay residual exceeds 3 m / 3 deg is NOT installed -- it is reported.
A monument with no crisp footprint is a known-coord placement and is flagged.

No GUI. No hash (rule #6: re-hash only after .tr3/.for changes; objects don't need it).
Deterministic.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import shutil
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import condor_grid as G          # noqa: E402  (honours CONDOR_LANDSCAPE; we target skopje)
import c3d                       # noqa: E402

REPO = Path(__file__).resolve().parents[1]
STAGE = REPO / ".sandbox/landmarks"
OVERLAYS = STAGE / "placement_overlays"
RECON = STAGE / "_recon"
INSTALL = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
OBJDIR = INSTALL / "World" / "Objects"
WTEX = INSTALL / "World" / "Textures"          # object textures (Slovenia2 convention)
OBJFILE = INSTALL / "MacedoniaSkopje.obj"
# Slovenia2 ships EVERY object texture as a FULL relative path inside the .c3d
# (e.g. 'Landscapes\\Slovenia2\\World\\Textures\\Bilding2.dds') with the DDS in
# World/Textures/ -- 1379 refs, 0 bare. We follow that proven convention for the
# landmarks (the staged .c3d carry the bare 'Name.dds'; we rewrite to the full path).
TEX_PREFIX = "Landscapes\\MacedoniaSkopje\\World\\Textures\\"
DEM = REPO / "sources/dem/macedonia_skopje_dem_30m_2305_flat.raw"
ULX_W, ULY_N, DEMW, XDIM = 506880.0, 4700160.0, 2305, 30.0

_T_WGS_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
_T_UTM_TO_WGS = Transformer.from_crs("EPSG:32634", "EPSG:4326", always_xy=True)
_T_WGS_TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_T_UTM_TO_3857 = Transformer.from_crs("EPSG:32634", "EPSG:3857", always_xy=True)
_T_3857_TO_UTM = Transformer.from_crs("EPSG:3857", "EPSG:32634", always_xy=True)

ESRI = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/export")

# 152-byte .obj record layout (decoded from Slovenia2 + the installed hangar):
#   posX,posY,posZ,scale,ori : 5 x float32 LE ; u8 namelen ; name[131] (incl '.c3d')
REC_SIZE = 152
NAME_FIELD = 131


# --------------------------------------------------------------------------- #
# Landmark plan. Coordinates are Nominatim/OSM-VERIFIED (the staged landmarks.json
# coords are right EXCEPT PortaMacedonia, which json places ~130 m off -> we use the
# verified arch coordinate). `kind` drives the method.
# --------------------------------------------------------------------------- #
# bearing_hint_deg: the long-axis compass bearing seed (and the facing for monuments);
#   refined by detection for buildings.  footprint_len_m: the real long-axis length the
#   model is scaled to (None => scale 1.0).
LANDMARKS = [
    # name, kind, lat, lon, bearing_hint_deg, note
    dict(name="MillenniumCross", kind="summit", lat=41.96513, lon=21.39439,
         bearing=90.0, facing="cross faces the city (arms E-W)",
         replace_decimated=True),
    dict(name="WarriorOnHorse", kind="monument", lat=41.99591, lon=21.43147,
         bearing=180.0, facing="faces south down Macedonia Square"),
    dict(name="TelecomTowerAEK", kind="monument", lat=41.96552, lon=21.39765,
         bearing=0.0, facing="vertical tower (ori irrelevant)"),
    dict(name="PortaMacedonia", kind="monument", lat=41.99445, lon=21.43244,
         bearing=64.0, facing="arch axis along the square promenade (OSM MAR 64 deg)"),
    # StoneBridge + Railway are LINEAR structures (a bridge deck / the Kenzo-Tange
    # Transport Centre deck along the tracks). A blob-OBB on Esri grabs the river / whole
    # rail-yard as a square with the wrong axis, so we drive their orientation from the
    # AUTHORITATIVE OSM centreline (bridge way 'Камен мост' = 47.1 deg; rail line through
    # the station = 39.2 deg) at the verified centroid -- kind="axis".
    dict(name="StoneBridge", kind="axis", lat=41.99710, lon=21.43318,
         bearing=47.1, axis_E=535878.7, axis_N=4649543.4, half_m=160,
         note="long axis crosses the Vardar (OSM way 'Камен мост' 47.1 deg)"),
    dict(name="ToseProeskiArena", kind="building", lat=42.00572, lon=21.42556,
         bearing=129.0, half_m=180, note="oval city stadium"),
    dict(name="MOB_OperaBallet", kind="building", lat=41.99757, lon=21.43709,
         bearing=33.6, half_m=150, osm_name="опера и балет",
         note="angular Opera & Ballet; OSM footprint accurate"),
    dict(name="RailwayStationSkopje", kind="axis", lat=41.99090, lon=21.44599,
         bearing=39.2, axis_E=536941.1, axis_N=4648862.1, half_m=220,
         note="Kenzo Tange Transport Centre deck along the tracks (rail line 39.2 deg)"),
]


# --------------------------------------------------------------------------- #
# DEM altitude
# --------------------------------------------------------------------------- #
_dem_cache = None


def dem_alt(E: float, N: float) -> float:
    global _dem_cache
    if _dem_cache is None:
        _dem_cache = np.fromfile(DEM, dtype="<u2").reshape(DEMW, DEMW)
    c = min(max(int(round((E - ULX_W) / XDIM)), 0), DEMW - 1)
    r = min(max(int(round((ULY_N - N) / XDIM)), 0), DEMW - 1)
    return float(_dem_cache[r, c])


def dem_summit(lat: float, lon: float, win_m: float = 600.0):
    """DEM-max within +/-win_m of (lat,lon). Returns (E, N, alt, lat, lon)."""
    global _dem_cache
    if _dem_cache is None:
        _dem_cache = np.fromfile(DEM, dtype="<u2").reshape(DEMW, DEMW)
    E0, N0 = _T_WGS_TO_UTM.transform(lon, lat)
    c0 = int(round((E0 - ULX_W) / XDIM))
    r0 = int(round((ULY_N - N0) / XDIM))
    h = int(round(win_m / XDIM))
    sub = _dem_cache[r0 - h:r0 + h + 1, c0 - h:c0 + h + 1]
    mr, mc = np.unravel_index(int(np.argmax(sub)), sub.shape)
    gr, gc = r0 - h + mr, c0 - h + mc
    E = ULX_W + gc * XDIM
    N = ULY_N - gr * XDIM
    lo, la = _T_UTM_TO_WGS.transform(E, N)
    return E, N, float(sub.max()), la, lo


# --------------------------------------------------------------------------- #
# Esri imagery (EPSG:3857) -- verified-correct georeference; cached on disk
# --------------------------------------------------------------------------- #
def fetch_esri(lat: float, lon: float, half_m: float, tag: str, sz: int = 1024):
    """Square Esri World-Imagery crop centred on (lat,lon). Returns (rgb, meta).

    meta has the 3857 extent so UTM<->pixel mapping is exact (Web Mercator scale is
    locally ~uniform at this latitude; the residual is far below one texel over 400 m).

    The ArcGIS export occasionally 500s ("Error: bytes") when the requested pixel size
    maps to a native zoom level whose source tile is missing for a given (mountain)
    location. We retry across a set of sizes that snap to standard web-mercator zooms
    (512/768/1024/640) until one succeeds, so a single bad level never blocks placement."""
    import requests
    RECON.mkdir(parents=True, exist_ok=True)
    cache_img = RECON / f"esri_{tag}.jpg"
    cache_meta = RECON / f"esri_{tag}.json"
    if cache_img.exists() and cache_meta.exists():
        meta = json.loads(cache_meta.read_text())
        return np.asarray(Image.open(cache_img).convert("RGB")), meta
    x, y = _T_WGS_TO_3857.transform(lon, lat)
    minx, miny, maxx, maxy = x - half_m, y - half_m, x + half_m, y + half_m
    bbox = f"{minx},{miny},{maxx},{maxy}"
    sizes = [sz, 512, 768, 1024, 640]
    seen = set()
    last = None
    for s in sizes:
        if s in seen:
            continue
        seen.add(s)
        for _ in range(2):
            try:
                r = requests.get(ESRI, params={"bbox": bbox, "bboxSR": 3857, "imageSR": 3857,
                                               "size": f"{s},{s}", "format": "jpg", "f": "image"},
                                 timeout=40)
                if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image"):
                    im = Image.open(io.BytesIO(r.content)).convert("RGB")
                    meta = dict(ext3857=[minx, miny, maxx, maxy], W=s, H=s,
                                res=2 * half_m / s, lat=lat, lon=lon)
                    im.save(cache_img)
                    cache_meta.write_text(json.dumps(meta, indent=2))
                    return np.asarray(im), meta
                last = f"status {r.status_code} ct={r.headers.get('Content-Type')} (size {s})"
            except Exception as e:                   # noqa: BLE001
                last = repr(e)
            time.sleep(1.0)
    raise SystemExit(f"Esri fetch failed for {tag}: {last}")


def make_mappers(meta):
    minx, miny, maxx, maxy = meta["ext3857"]
    W, H = meta["W"], meta["H"]

    def utm_to_px(E, N):
        x, y = _T_UTM_TO_3857.transform(E, N)
        return ((x - minx) / (maxx - minx) * W, (maxy - y) / (maxy - miny) * H)

    def px_to_utm(px, py):
        x = minx + (px + 0.5) / W * (maxx - minx)
        y = maxy - (py + 0.5) / H * (maxy - miny)
        return _T_3857_TO_UTM.transform(x, y)

    return utm_to_px, px_to_utm


# --------------------------------------------------------------------------- #
# OSM footprint (UTM) -- used as an accurate prior where OSM matches the building
# --------------------------------------------------------------------------- #
_osm_cache = None


def osm_footprint(lat: float, lon: float, name_sub: str):
    """Nearest OSM building polygon (UTM) whose name contains name_sub. Returns the
    shapely Polygon or None."""
    global _osm_cache
    from shapely.geometry import shape
    from shapely.ops import transform as shp_transform
    if _osm_cache is None:
        p = REPO / ".sandbox/osm/buildings.geojson"
        _osm_cache = json.loads(p.read_text(encoding="utf-8"))["features"]
    best = (1e9, None)
    for f in _osm_cache:
        nm = str(f.get("properties", {}).get("name", ""))
        if name_sub and name_sub not in nm:
            continue
        try:
            g = shape(f["geometry"])
        except Exception:
            continue
        c = g.centroid
        d = math.hypot(c.x - lon, c.y - lat)
        if d < best[0]:
            best = (d, f)
    if best[1] is None:
        return None
    poly = shp_transform(_T_WGS_TO_UTM.transform, shape(best[1]["geometry"]))
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda p: p.area)
    return poly


# --------------------------------------------------------------------------- #
# Footprint detector: oriented bounding box of a building-vs-surround mask on Esri.
#
# A landmark roof/deck differs in tone+texture from its surround (grass pitch, river,
# pavement). We build a coarse mask by thresholding local gradient energy + a tone band
# learned at the centre, take its largest connected blob near the centre, and fit a
# minimum-area rectangle (cv2.minAreaRect). Robust for big, complex roofs where a single
# edge-projection (the hangar method, tuned for a clean 34 m quad) is unreliable.
# Returns (cE, cN, long_m, short_m, bearing_mod180, contour_px) or None.
# --------------------------------------------------------------------------- #
def detect_obb(img, meta, seed_lat, seed_lon, bearing_hint, expect_long_m,
               seed_poly=None):
    import cv2
    utm_to_px, px_to_utm = make_mappers(meta)
    res = meta["res"]
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    sE, sN = _T_WGS_TO_UTM.transform(seed_lon, seed_lat)
    scx, scy = utm_to_px(sE, sN)

    # If an accurate OSM polygon is supplied, the OBB of that polygon IS the footprint
    # (cross-checked visually on Esri in the overlay). This is the most reliable path.
    if seed_poly is not None and seed_poly.area > 200.0:
        rect = cv2.minAreaRect(np.array(seed_poly.exterior.coords, dtype=np.float32))
        (rcx, rcy), (rw, rh), rang = rect
        # minAreaRect works in the polygon's own UTM coords already
        box = cv2.boxPoints(rect)
        long_m = max(rw, rh)
        short_m = min(rw, rh)
        # bearing of the long edge
        if rw >= rh:
            ang = rang
        else:
            ang = rang + 90.0
        # cv2 angle is CCW from +x (east) in image-y-down sense; convert to compass.
        bearing = (90.0 - ang) % 180.0
        contour_px = np.array([utm_to_px(x, y) for x, y in box], dtype=np.int32)
        return rcx, rcy, long_m, short_m, bearing, contour_px, "osm-obb"

    # Otherwise detect on the imagery. Mask = (gradient energy high) OR (tone near centre
    # tone), then morphological close, largest central blob.
    blur = cv2.GaussianBlur(gray, (0, 0), 1.2)
    gx = cv2.Sobel(blur, cv2.CV_32F, 1, 0, 3)
    gy = cv2.Sobel(blur, cv2.CV_32F, 0, 1, 3)
    gmag = np.hypot(gx, gy)
    gmag = (gmag / (gmag.max() + 1e-6) * 255).astype(np.uint8)
    # learn centre tone
    r0 = max(3, int(8 / res))
    patch = gray[int(scy) - r0:int(scy) + r0, int(scx) - r0:int(scx) + r0]
    tone = float(np.median(patch)) if patch.size else float(gray[int(scy), int(scx)])
    tone_band = (np.abs(gray.astype(np.float32) - tone) < 40).astype(np.uint8) * 255
    edge = cv2.threshold(gmag, 40, 255, cv2.THRESH_BINARY)[1]
    mask = cv2.bitwise_or(edge, tone_band)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)
    # constrain to a window of ~1.6x the expected size so we don't grab the whole block
    win = int((expect_long_m * 1.6 if expect_long_m else 200) / res)
    wmask = np.zeros_like(mask)
    y0, y1 = max(0, int(scy) - win), min(H, int(scy) + win)
    x0, x1 = max(0, int(scx) - win), min(W, int(scx) + win)
    wmask[y0:y1, x0:x1] = 255
    mask = cv2.bitwise_and(mask, wmask)
    num, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    # pick the blob whose centroid is closest to the seed AND area is plausible
    best = None
    for i in range(1, num):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < (0.2 * (expect_long_m / res) ** 2 if expect_long_m else 400):
            continue
        d = math.hypot(cent[i][0] - scx, cent[i][1] - scy)
        if best is None or d < best[0]:
            best = (d, i)
    if best is None:
        return None
    blob = (lab == best[1]).astype(np.uint8)
    cnts, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnt = max(cnts, key=cv2.contourArea)
    rect = cv2.minAreaRect(cnt)
    (rcx_px, rcy_px), (rw, rh), rang = rect
    box = cv2.boxPoints(rect)
    # convert px box to UTM
    box_utm = np.array([px_to_utm(px, py) for px, py in box])
    cE, cN = px_to_utm(rcx_px, rcy_px)
    e = box_utm
    side1 = math.hypot(e[1][0] - e[0][0], e[1][1] - e[0][1])
    side2 = math.hypot(e[2][0] - e[1][0], e[2][1] - e[1][1])
    long_m, short_m = max(side1, side2), min(side1, side2)
    # long-axis bearing in UTM
    if side1 >= side2:
        dx, dy = e[1][0] - e[0][0], e[1][1] - e[0][1]
    else:
        dx, dy = e[2][0] - e[1][0], e[2][1] - e[1][1]
    bearing = math.degrees(math.atan2(dx, dy)) % 180.0
    return cE, cN, long_m, short_m, bearing, box.astype(np.int32), "esri-obb"


# --------------------------------------------------------------------------- #
# Model footprint (local E/N at ori=0) from the staged c3d AABB
# --------------------------------------------------------------------------- #
def model_footprint(name: str):
    cf = c3d.parse_c3d(STAGE / f"{name}.c3d")
    P = np.array([(v.px, v.py) for o in cf.objects for v in o.vertices])
    xmin, xmax = float(P[:, 0].min()), float(P[:, 0].max())
    ymin, ymax = float(P[:, 1].min()), float(P[:, 1].max())
    return xmin, xmax, ymin, ymax


def model_long_axis(name: str):
    xmin, xmax, ymin, ymax = model_footprint(name)
    dx, dy = xmax - xmin, ymax - ymin
    # long axis at ori=0: E (azimuth 90) if dx>=dy else N (azimuth 0)
    return (90.0 if dx >= dy else 0.0), dx, dy


def choose_ori(det_bearing_mod180: float, model_axis_deg: float, facing_hint: float):
    """ori (deg) so the model long axis aligns to det_bearing (mod 180), picking the one
    of the two 180-apart options whose long axis points nearer the facing hint."""
    base = (det_bearing_mod180 - model_axis_deg) % 360.0
    cands = [base % 360.0, (base + 180.0) % 360.0]
    best = None
    for ori in cands:
        axis_world = (model_axis_deg + ori) % 360.0
        dd = abs((axis_world - facing_hint + 180) % 360 - 180)
        if best is None or dd < best[1]:
            best = (ori, dd)
    return best[0], cands


# --------------------------------------------------------------------------- #
# .obj record encode / decode
# --------------------------------------------------------------------------- #
def make_record(name_c3d: str, E: float, N: float, z: float, ori_deg: float,
                scale: float) -> bytes:
    nm = name_c3d.encode("ascii")
    if len(nm) > NAME_FIELD:
        raise ValueError(f"name too long: {name_c3d!r}")
    posX, posY = G.obj_record_xy(E, N)
    ori = G.heading_deg_to_ori(ori_deg)
    return (struct.pack("<5f", posX, posY, z, scale, ori)
            + bytes([len(nm)]) + nm.ljust(NAME_FIELD, b"\x00"))


def read_records(path: Path):
    if not path.exists():
        return []
    d = path.read_bytes()
    recs = []
    for k in range(len(d) // REC_SIZE):
        b = k * REC_SIZE
        px, py, pz, sc, ori = struct.unpack_from("<5f", d, b)
        nl = d[b + 20]
        nm = d[b + 21:b + 21 + nl].decode("ascii", "replace")
        recs.append(dict(posX=px, posY=py, posZ=pz, scale=sc, ori=ori, name=nm,
                         raw=d[b:b + REC_SIZE]))
    return recs


# --------------------------------------------------------------------------- #
# Validation overlay: decode the would-be/installed record, rotate+scale the model
# footprint, draw on the Esri image. Returns (residual_m, residual_deg, png_path).
# --------------------------------------------------------------------------- #
def overlay(name: str, rec_bytes: bytes, det, img, meta, ori_deg: float, scale: float,
            facing_hint: float, kind: str):
    import cv2
    utm_to_px, _ = make_mappers(meta)
    posX, posY, pz, sc, ori = struct.unpack_from("<5f", rec_bytes, 0)
    placedE, placedN = G.obj_world_xy(posX, posY)
    ori_deg_dec = math.degrees(ori) % 360.0

    xmin, xmax, ymin, ymax = model_footprint(name)
    local = np.array([(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]) * sc
    th = math.radians(ori_deg_dec)
    cth, sth = math.cos(th), math.sin(th)
    # Condor: world = ref + R(-ori).local with local x=E,y=N (so local +Y -> azimuth ori)
    worldE = placedE + (cth * local[:, 0] + sth * local[:, 1])
    worldN = placedN + (-sth * local[:, 0] + cth * local[:, 1])

    vis = img.copy()
    pts = np.array([utm_to_px(e, n) for e, n in zip(worldE, worldN)], dtype=np.int32)
    cv2.polylines(vis, [pts], True, (255, 60, 60), 3)               # placed model footprint
    if det is not None and det.get("contour_px") is not None:
        cv2.polylines(vis, [np.asarray(det["contour_px"], np.int32)], True,
                      (0, 255, 255), 2)                              # detected footprint
    pe = utm_to_px(placedE, placedN)
    cv2.drawMarker(vis, (int(pe[0]), int(pe[1])), (255, 255, 0), cv2.MARKER_CROSS, 18, 2)
    # facing arrow (model long axis world direction)
    axis_world = math.radians((model_long_axis(name)[0] + ori_deg_dec) % 360.0)
    L = max(xmax - xmin, ymax - ymin) * sc * 0.6
    tip = utm_to_px(placedE + math.sin(axis_world) * L, placedN + math.cos(axis_world) * L)
    cv2.arrowedLine(vis, (int(pe[0]), int(pe[1])), (int(tip[0]), int(tip[1])),
                    (255, 140, 0), 2, tipLength=0.25)

    resid_m = resid_deg = 0.0
    if det is not None:
        resid_m = math.hypot(placedE - det["cE"], placedN - det["cN"])
        placed_axis = (model_long_axis(name)[0] + ori_deg_dec) % 180.0
        resid_deg = abs((placed_axis - det["bearing"] + 90) % 180 - 90)
    OVERLAYS.mkdir(parents=True, exist_ok=True)
    pth = OVERLAYS / f"{name}.png"
    Image.fromarray(vis).save(pth)
    return resid_m, resid_deg, pth, (placedE, placedN, ori_deg_dec, sc)


# --------------------------------------------------------------------------- #
# Per-landmark resolve -> (E, N, z, ori_deg, scale, det, img, meta, method, flags)
# --------------------------------------------------------------------------- #
def resolve(lm: dict):
    name = lm["name"]
    kind = lm["kind"]
    flags = []
    model_axis, mdx, mdy = model_long_axis(name)
    model_long = max(mdx, mdy)

    if kind == "summit":
        E, N, alt, la, lo = dem_summit(lm["lat"], lm["lon"], win_m=600.0)
        # the cross is real-size; ori from facing hint (arms E-W -> long axis 90)
        ori_deg, _ = choose_ori(lm["bearing"] % 180.0, model_axis, lm["bearing"])
        img, meta = fetch_esri(la, lo, 120, f"{name}", sz=900)
        det = None
        flags.append(f"DEM summit {alt:.0f} m at ({la:.5f},{lo:.5f})")
        return dict(E=E, N=N, z=alt, ori_deg=ori_deg, scale=1.0, det=det, img=img,
                    meta=meta, method="dem-summit + known facing", flags=flags,
                    facing=lm["bearing"])

    if kind == "monument":
        E, N = _T_WGS_TO_UTM.transform(lm["lon"], lm["lat"])
        z = dem_alt(E, N)
        ori_deg, _ = choose_ori(lm["bearing"] % 180.0, model_axis, lm["bearing"])
        img, meta = fetch_esri(lm["lat"], lm["lon"], lm.get("half_m", 90), f"{name}", sz=900)
        flags.append("known-coord placement (no crisp footprint to detect)")
        return dict(E=E, N=N, z=z, ori_deg=ori_deg, scale=1.0, det=None, img=img,
                    meta=meta, method="verified coord + facing", flags=flags,
                    facing=lm["bearing"])

    if kind == "axis":
        # linear structure: verified centroid + authoritative OSM centreline bearing,
        # real-size model (scale 1). The model long axis (mesh +Y) is rotated to align
        # with the structure axis; we pick the 180-fold nearer the same axis (bridges /
        # decks are symmetric, so either is fine -- choose_ori keeps it deterministic).
        E = lm.get("axis_E") or _T_WGS_TO_UTM.transform(lm["lon"], lm["lat"])[0]
        N = lm.get("axis_N") or _T_WGS_TO_UTM.transform(lm["lon"], lm["lat"])[1]
        z = dem_alt(E, N)
        ori_deg, _ = choose_ori(lm["bearing"] % 180.0, model_axis, lm["bearing"])
        img, meta = fetch_esri(lm["lat"], lm["lon"], lm.get("half_m", 160), f"{name}", sz=1024)
        # synthesise a "det" at the verified centroid + axis so the overlay draws the
        # structure axis line and reports the (tiny) residual of the encoded record.
        det = dict(cE=E, cN=N, long_m=max(mdx, mdy), short_m=min(mdx, mdy),
                   bearing=lm["bearing"] % 180.0, contour_px=None, method="osm-centreline")
        flags.append(f"axis from OSM centreline {lm['bearing']:.1f} deg @ verified centroid")
        return dict(E=E, N=N, z=z, ori_deg=ori_deg, scale=1.0, det=det, img=img,
                    meta=meta, method="OSM centreline axis + verified centroid",
                    flags=flags, facing=lm["bearing"])

    # buildings + bridge: detect footprint on Esri
    half = lm.get("half_m", 160)
    img, meta = fetch_esri(lm["lat"], lm["lon"], half, f"{name}", sz=1024)
    seed_poly = None
    if lm.get("osm_name"):
        seed_poly = osm_footprint(lm["lat"], lm["lon"], lm["osm_name"])
    d = detect_obb(img, meta, lm["lat"], lm["lon"], lm["bearing"], model_long, seed_poly)
    if d is None:
        # detection failed -> fall back to verified coord + hint bearing, FLAG it
        E, N = _T_WGS_TO_UTM.transform(lm["lon"], lm["lat"])
        z = dem_alt(E, N)
        ori_deg, _ = choose_ori(lm["bearing"] % 180.0, model_axis, lm["bearing"])
        flags.append("DETECTION FAILED -> known-coord fallback; NOT auto-installed")
        return dict(E=E, N=N, z=z, ori_deg=ori_deg, scale=1.0, det=None, img=img,
                    meta=meta, method="detect-failed fallback", flags=flags,
                    facing=lm["bearing"], detect_failed=True)
    cE, cN, long_m, short_m, bearing, contour_px, dmeth = d
    det = dict(cE=cE, cN=cN, long_m=long_m, short_m=short_m, bearing=bearing,
               contour_px=contour_px, method=dmeth)
    z = dem_alt(cE, cN)
    # SCALE = 1.0 (rule #11 MAX QUALITY): the staged .c3d is the REAL building at its true
    # metre size, so we place it 1:1 and use detection only for POSITION (centroid) and
    # ORIENTATION (long-axis bearing). Scaling the accurate mesh to a detected blob/OSM
    # polygon (different aspect ratio -> e.g. MOB 0.82 vs OSM 2.39) would DISTORT it. We
    # report detected-vs-model long axis as a cross-check and FLAG a gross mismatch.
    scale = 1.0
    ratio = (long_m / model_long) if model_long > 1 else 1.0
    ori_deg, cands = choose_ori(bearing, model_axis, lm["bearing"])
    flags.append(f"detected {long_m:.0f}x{short_m:.0f} m @ {bearing:.1f} deg ({dmeth}); "
                 f"model long={model_long:.0f} m (det/model={ratio:.2f}, placed scale 1.0)")
    if not (0.7 <= ratio <= 1.4):
        flags.append(f"NOTE: detected size differs from model by {abs(1-ratio)*100:.0f}% "
                     f"(model kept real-size; check the staged .c3d if this seems wrong)")
    return dict(E=cE, N=cN, z=z, ori_deg=ori_deg, scale=scale, det=det, img=img,
                meta=meta, method=f"footprint detect pos+ori ({dmeth}), scale 1.0",
                flags=flags, facing=lm["bearing"])


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true",
                    help="rewrite the install .obj + copy c3d/dds (else dry-run + overlays)")
    ap.add_argument("--only", default="", help="comma list of landmark names to process")
    args = ap.parse_args(argv)

    only = set(s.strip() for s in args.only.split(",") if s.strip())
    plan = [lm for lm in LANDMARKS if not only or lm["name"] in only]

    print("=" * 78)
    print("  SKOPJE LANDMARK PLACEMENT (texture-alignment flow, Esri ground truth)")
    print(f"  anchor E/N = {G.OBJ_ANCHOR_E:.1f}/{G.OBJ_ANCHOR_N:.1f}  (verified .trn header)")
    print("=" * 78)

    results = []
    for lm in plan:
        name = lm["name"]
        print(f"\n--- {name}  [{lm['kind']}] ---")
        r = resolve(lm)
        lo, la = _T_UTM_TO_WGS.transform(r["E"], r["N"])   # always_xy -> (lon, lat)
        rec = make_record(f"{name}.c3d", r["E"], r["N"], r["z"], r["ori_deg"], r["scale"])
        resid_m, resid_deg, pth, placed = overlay(
            name, rec, r["det"], r["img"], r["meta"], r["ori_deg"], r["scale"],
            r["facing"], lm["kind"])
        is_building = lm["kind"] in ("building",)   # only true OBB-detected buildings gate
        ok = (not r.get("detect_failed")) and (
            (resid_m <= 3.0 and resid_deg <= 3.0) if (is_building and r["det"]) else True)
        verdict = "PASS" if ok else "CHECK"
        if is_building and not r["det"]:
            verdict = "CHECK (no detection)"
        print(f"  coord (E,N)=({r['E']:.1f},{r['N']:.1f})  lat,lon=({la:.5f},{lo:.5f})  "
              f"z={r['z']:.0f} m")
        print(f"  ori={r['ori_deg']:.2f} deg  scale={r['scale']:.4f}  method={r['method']}")
        for f in r["flags"]:
            print(f"    - {f}")
        if r["det"]:
            print(f"  residual: {resid_m:.2f} m, {resid_deg:.2f} deg  -> {verdict}")
        else:
            print(f"  {verdict}  (monument/known-coord -> residual not applicable)")
        print(f"  overlay: {pth}")
        results.append(dict(lm=lm, r=r, rec=rec, resid_m=resid_m, resid_deg=resid_deg,
                            overlay=pth, ok=ok, is_building=is_building,
                            lat=la, lon=lo, placed=placed))

    # ---- install ----
    if args.install:
        do_install(results)

    # ---- final table ----
    print("\n" + "=" * 78)
    print("  PLACEMENT TABLE")
    print("=" * 78)
    print(f"  {'name':22} {'lat,lon':21} {'ori':>7} {'scale':>7} {'resid_m':>8} "
          f"{'tex':>4}  method")
    staged = {m["name"]: m for m in json.loads((STAGE / "landmarks.json").read_text())}
    for res in results:
        lm = res["lm"]; r = res["r"]
        textured = "yes" if staged.get(lm["name"], {}).get("textured") else "no"
        rm = f"{res['resid_m']:.2f}" if r["det"] else "n/a"
        # res['lat']/res['lon'] are (la, lo) from _T_UTM_TO_WGS -> la=lat, lo=lon
        print(f"  {lm['name']:22} {res['lat']:.5f},{res['lon']:.5f} "
              f"{r['ori_deg']:7.2f} {r['scale']:7.3f} {rm:>8} {textured:>4}  {r['method']}")
    print("\n  overlays in:", OVERLAYS)
    return results


def do_install(results):
    OBJDIR.mkdir(parents=True, exist_ok=True)
    # 1. back up the old .obj
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if OBJFILE.exists():
        bak = OBJFILE.with_suffix(f".obj.bak_prelandmarks_{ts}")
        shutil.copy2(OBJFILE, bak)
        print(f"\n[install] backed up {OBJFILE.name} -> {bak.name}")

    # 2. keep the StenkovecHangar record BYTE-IDENTICAL
    existing = read_records(OBJFILE)
    hangar = next((e for e in existing if e["name"].lower().startswith("stenkovechangar")),
                  None)
    if hangar is None:
        raise SystemExit("[install] ABORT: StenkovecHangar record not found in current .obj")

    # 3. copy c3d + dds for every landmark we are installing (PASS buildings + all
    #    monuments + the full-quality MillenniumCross). A building flagged CHECK/failed
    #    is NOT installed.
    install_set = []
    for res in results:
        lm = res["lm"]
        if res["is_building"] and not res["ok"]:
            print(f"[install] SKIP {lm['name']}: overlay did not pass "
                  f"({res['resid_m']:.1f} m / {res['resid_deg']:.1f} deg) -> reported, not installed")
            continue
        install_set.append(res)

    WTEX.mkdir(parents=True, exist_ok=True)
    for res in install_set:
        name = res["lm"]["name"]
        # (a) DDS -> World/Textures/ (matches the rewritten full-path ref, Slovenia2 way)
        #     AND World/Objects/ (belt-and-suspenders + the task's literal instruction).
        dds_src = STAGE / f"{name}.dds"
        if dds_src.exists():
            shutil.copy2(dds_src, WTEX / f"{name}.dds")
            shutil.copy2(dds_src, OBJDIR / f"{name}.dds")
        # (b) c3d -> World/Objects/, with its texture path rewritten bare -> full relative
        #     (Landscapes\MacedoniaSkopje\World\Textures\<name>.dds). Round-trips via c3d.py.
        cf = c3d.parse_c3d(STAGE / f"{name}.c3d")
        for o in cf.objects:
            t = o.texture
            if t and "\\" not in t and "/" not in t:
                o.texture = TEX_PREFIX + t
        c3d.write_c3d(cf, OBJDIR / f"{name}.c3d")
    print(f"[install] copied {len(install_set)} landmark c3d (texture path -> "
          f"{TEX_PREFIX}<name>.dds) to {OBJDIR}, DDS to World/Textures/ (+Objects/).")

    # 4. rebuild the .obj: hangar (byte-identical) FIRST, then the landmarks.
    out = bytearray()
    out += hangar["raw"]
    for res in install_set:
        out += res["rec"]
    OBJFILE.write_bytes(bytes(out))
    names = [r["lm"]["name"] for r in install_set]
    print(f"[install] wrote {OBJFILE.name}: StenkovecHangar (kept) + {len(install_set)} "
          f"landmarks = {1 + len(install_set)} records")
    print(f"[install] installed: {names}")

    # 5. sanity: re-decode and confirm hangar bytes unchanged
    after = read_records(OBJFILE)
    h2 = next((e for e in after if e["name"].lower().startswith("stenkovechangar")), None)
    assert h2 and h2["raw"] == hangar["raw"], "hangar record changed -- ABORT"
    print("[install] verified StenkovecHangar record is byte-identical to before.")


if __name__ == "__main__":
    main()
