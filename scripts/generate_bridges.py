#!/usr/bin/env python3
r"""
Road/rail BRIDGE autogen for the MacedoniaSkopje 12x12 landscape.

Every significant real bridge (Vardar river bridges in Skopje, highway viaducts,
rail bridges) becomes one textured Condor .c3d whose deck follows the actual OSM
polyline -- position, bearing and length come straight from the OSM geometry, so
the object needs NO placement rotation (ori=0; the mesh itself is world-aligned
in its local frame).

Pipeline (deterministic; with the Overpass cache present a rerun is byte-identical):
  1. FETCH   ways bridge=yes|viaduct with highway=*/railway=* over the 12x12 bbox
             via the osm_io Overpass helper -> cache .sandbox/osm/bridges.geojson
             (LineStrings, properties = tags + osm_id).
  2. MERGE   project to UTM 34N (EPSG:32634); chain touching bridge segments of
             the same road (same mode/class/name-or-ref, endpoints <= 2 m) into
             one polyline per real bridge.
  3. FILTER  keep deck length >= 40 m; ALSO keep every bridge crossing the Vardar
             inside central Skopje (E 528000-545000, N 4645000-4656000) whatever
             its length. EXCLUDE the Stone Bridge (hero landmark handled
             elsewhere), non-physical classes, bridges outside the landscape, and
             foot/cycle decks that just duplicate a kept road deck (z-fighting).
  4. MESH    deck = flat ribbon following the polyline (subdivided so no segment
             > 25 m), slab thickness 1.2 m with visible side faces + end caps;
             solid 0.8 m parapet ribbons along both edges; rectangular piers
             (2 m along x 5 m across) every ~30 m for bridges > 60 m, dropping
             6 m below the deck slab (they sink into terrain/water -- fine).
             LOCAL frame = bridge centroid, +X East +Y North +Z up, real metres,
             deck top at z = DECK_Z (default +7 m so river decks clear the baked
             water). CCW faces point outward/up (Condor front face).
  5. TEXTURE ONE shared 512x512 DXT1 bridges.dds (asphalt deck band with a faint
             dashed lane line / concrete band), baked procedurally with PIL and
             compressed with nvcompress. All bridges reference
             Landscapes\MacedoniaSkopje\World\Textures\bridges.dds.
             UVs: deck U along the length (metres/10, wraps), V across.
  6. QA      deck outlines rendered over the INSTALLED game texture using the
             VERIFIED texture georef from footprints_to_obj.py/validate_bridge.py
             (TEX_ULX_W/TEX_SOUTH0 grid, TRUE UTM, cadastre residual 0.0 m):
             the 8 longest bridges + 4 central-Skopje Vardar crossings.

Outputs (all under .sandbox/bridges/):
  Objects/bridge_<osmid>.c3d      one per bridge (osmid = smallest merged way id)
  placements.json                 {name, c3d, E, N, ori_deg=0, scale=1.0,
                                   length_m, width_m, kind, osm_id, deck_z_m, ...}
                                  E/N = TRUE UTM 34N of the bridge centroid.
  bridges_texture.png / bridges.dds   the shared texture
  report.json                     count, total verts, length histogram, skip list
  qa/bridge_<id>_qa.png           overlay crops for visual inspection

Usage:  python scripts/generate_bridges.py [--no-qa] [--deck-z 7.0] [--refetch]
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from pyproj import Transformer
from shapely.geometry import LineString, Point, box as shp_box
from shapely.ops import unary_union

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import c3d      # noqa: E402  (.c3d reader/writer, byte-exact round-trip)
import osm_io   # noqa: E402  (Overpass fetch + geojson cache helpers)

ROOT = SCRIPTS.parent
SAND = ROOT / ".sandbox"
OUT = SAND / "bridges"
OBJ_DIR = OUT / "Objects"
QA_DIR = OUT / "qa"
CACHE = SAND / "osm" / "bridges.geojson"
WATERWAYS = SAND / "osm" / "waterways.geojson"

# 12x12 landscape bbox (WGS84, S W N E) -- matches the MacedoniaSkopje grid.
BBOX = (41.8276, 21.0829, 42.4537, 21.9242)

# --------------------------------------------------------------------------- #
# VERIFIED texture georef (from scripts/footprints_to_obj.py, reused by
# scripts/validate_bridge.py; validated there: TRUE-UTM cadastre footprints sit
# on the painted buildings with 0.0 m systematic residual). Patch file naming:
# t{11-col_from_west:02d}{row_from_south:02d}.dds, north-up, 2048 px / 5760 m.
# --------------------------------------------------------------------------- #
TEX_ULX_W = 506880.0                 # west edge of texture col 0
TEX_SOUTH0 = 4631040.0               # south edge of texture row 0
PATCH_M = 5760.0
PATCH_PX = 2048
NCOL = NROW = 12
M_PER_PX = PATCH_M / PATCH_PX        # 2.8125 m/px
INSTALL_TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")  # read-only
LS_E0, LS_N0 = TEX_ULX_W, TEX_SOUTH0
LS_E1 = TEX_ULX_W + NCOL * PATCH_M   # 576000
LS_N1 = TEX_SOUTH0 + NROW * PATCH_M  # 4700160

# --------------------------------------------------------------------------- #
# Bridge geometry parameters
# --------------------------------------------------------------------------- #
DECK_Z = 7.0          # deck TOP above the local origin (terrain alt at centroid);
                      # +7 m so river decks clear the baked water. --deck-z overrides.
DECK_T = 1.2          # slab thickness (visible side faces)
PARA_H = 0.8          # solid parapet height above deck top
PARA_W = 0.3          # parapet thickness
MAX_SEG = 25.0        # subdivide the polyline so no segment exceeds this
MIN_LEN = 40.0        # keep decks at least this long (except central Vardar)
MERGE_TOL = 2.0       # endpoint snap distance for merging segments of one road
PIER_SPACING = 30.0   # one pier every ~30 m ...
PIER_MIN_LEN = 60.0   # ... for bridges longer than this
PIER_ALONG = 2.0      # pier section, metres along the deck
PIER_ACROSS = 5.0     # pier section, metres across the deck
PIER_DROP = 6.0       # pier height below the deck slab (sinks into terrain/water)
MITER_LIMIT = 2.5     # clamp offset spikes at sharp polyline bends

# Central-Skopje box: every Vardar crossing here is kept regardless of length.
# "Crossing" = the deck polyline intersects the Vardar CENTRE-LINE (a real
# bank-to-bank crossing always does; a buffer test wrongly kept 8 m quay stubs).
CENTRAL_BOX = (528000.0, 4645000.0, 545000.0, 4656000.0)   # (E0, N0, E1, N1)

# Stone Bridge = custom hero landmark handled elsewhere -- excluded here.
# Exclusion = name match OR within STONE_WAY_DIST_M of the named Stone Bridge WAY
# geometry (catches its unnamed end-ramps without swallowing the separate Bridge
# of Civilizations ~100 m upstream, which is a real crossing and must be kept).
STONE_LATLON = (41.9971, 21.4332)
STONE_RADIUS_M = 120.0        # point-radius FALLBACK if no named way is found
STONE_WAY_DIST_M = 30.0
STONE_NAMES = ("камен мост", "stone bridge")

# Shared texture (Slovenia2-proven full-path reference convention).
TEX_REF = "Landscapes\\MacedoniaSkopje\\World\\Textures\\bridges.dds"
TEX_PNG = OUT / "bridges_texture.png"
TEX_DDS = OUT / "bridges.dds"
NVCOMPRESS = Path("C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe")

# UV layout in bridges.dds (v=0 = top image row, DirectX convention):
#   v [0.02 .. 0.48] asphalt deck band (faint dashed lane line at v~0.25)
#   v [0.52 .. 0.98] concrete band (slab sides/bottom, parapets, piers)
V_DECK0, V_DECK1 = 0.02, 0.48
V_CON0, V_CON1 = 0.52, 0.98
U_PER_M = 0.1                        # deck U = metres / 10 (wraps)

# Default deck widths (metres) when neither width= nor lanes= is tagged.
HW_DEFAULT_WIDTH = {
    "motorway": 22.0, "trunk": 22.0, "motorway_link": 11.0, "trunk_link": 11.0,
    "primary": 14.0, "primary_link": 9.0,
    "secondary": 11.0, "secondary_link": 9.0,
    "tertiary": 9.0, "tertiary_link": 9.0, "residential": 9.0,
    "unclassified": 9.0, "living_street": 9.0, "road": 9.0,
    "service": 7.0, "track": 5.0, "pedestrian": 5.0,
    "footway": 4.0, "path": 4.0, "cycleway": 4.0, "steps": 4.0, "bridleway": 4.0,
}
RAILWAY_WIDTH = 6.0
FOOT_CLASSES = {"footway", "path", "cycleway", "steps", "pedestrian", "bridleway"}
# highway/railway values that are not physical carriageways -> skipped
NONPHYSICAL = {"proposed", "construction", "razed", "dismantled", "abandoned",
               "disused", "platform", "corridor", "elevator", "rest_area",
               "services", "bus_stop", "escape", "emergency_bay", "planned"}

_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)


# --------------------------------------------------------------------------- #
# 1. Fetch (cached) -- reuses osm_io's mirror-rotating Overpass helper
# --------------------------------------------------------------------------- #
def _bridges_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["bridge"~"^(yes|viaduct)$"]["highway"];
  way["bridge"~"^(yes|viaduct)$"]["railway"];
);
out geom;"""


def fetch_bridges(refetch: bool = False) -> dict:
    """Return the bridges FeatureCollection, fetching + caching if needed."""
    if CACHE.exists() and not refetch:
        fc = osm_io.load_geojson(CACHE)
        print(f"[fetch] cache hit {CACHE} ({len(fc['features'])} ways)")
        return fc
    osm_io._QUERY_BUILDERS["bridges"] = _bridges_query   # plug into the helper
    data = osm_io.query_overpass("bridges", *BBOX)
    feats = []
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry")
        if not geom or len(geom) < 2:
            continue
        props = dict(el.get("tags") or {})
        props["osm_id"] = int(el["id"])
        feats.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "LineString",
                         "coordinates": [[pt["lon"], pt["lat"]] for pt in geom]},
        })
    feats.sort(key=lambda f: f["properties"]["osm_id"])
    fc = {"type": "FeatureCollection", "features": feats}
    osm_io.save_geojson(fc, CACHE)
    return fc


# --------------------------------------------------------------------------- #
# 2. Segments -> merged bridge polylines
# --------------------------------------------------------------------------- #
def load_segments(fc: dict) -> list[dict]:
    segs = []
    for f in fc.get("features", []):
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if g.get("type") != "LineString":
            continue
        pts = []
        for lon, lat in g["coordinates"]:
            e, n = _TO_UTM.transform(lon, lat)
            if not pts or math.hypot(e - pts[-1][0], n - pts[-1][1]) > 0.01:
                pts.append((e, n))
        if len(pts) < 2:
            continue
        if "highway" in p:
            mode, cls = "highway", str(p["highway"])
        elif "railway" in p:
            mode, cls = "railway", str(p["railway"])
        else:
            continue
        segs.append({"id": int(p["osm_id"]), "pts": pts, "tags": p,
                     "mode": mode, "cls": cls})
    segs.sort(key=lambda s: s["id"])
    return segs


def _group_key(s: dict):
    t = s["tags"]
    return (s["mode"], s["cls"], str(t.get("name") or t.get("ref") or ""))


def _close(a, b) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= MERGE_TOL


def _chain(group: list[dict]) -> list[tuple[list, list, dict]]:
    """Greedy deterministic chaining: join segments whose endpoints touch
    (<= MERGE_TOL). Returns [(points, sorted_way_ids, tags_of_first_way)]."""
    unused = sorted(group, key=lambda s: s["id"])
    chains = []
    while unused:
        cur = unused.pop(0)
        pts = list(cur["pts"])
        ids = [cur["id"]]
        tags = cur["tags"]
        grown = True
        while grown:
            grown = False
            for i, s in enumerate(unused):
                sp = s["pts"]
                if _close(pts[-1], sp[0]):
                    pts = pts + sp[1:]
                elif _close(pts[-1], sp[-1]):
                    pts = pts + sp[-2::-1]
                elif _close(pts[0], sp[-1]):
                    pts = sp[:-1] + pts
                elif _close(pts[0], sp[0]):
                    pts = sp[::-1][:-1] + pts
                else:
                    continue
                ids.append(s["id"])
                unused.pop(i)
                grown = True
                break
        chains.append((pts, sorted(ids), tags))
    return chains


def merge_segments(segs: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for s in segs:
        groups[_group_key(s)].append(s)
    bridges = []
    for key in sorted(groups):
        mode, cls, _name = key
        for pts, ids, tags in _chain(groups[key]):
            bridges.append({"osm_id": ids[0], "merged_osm_ids": ids, "pts": pts,
                            "tags": tags, "mode": mode, "cls": cls})
    bridges.sort(key=lambda b: b["osm_id"])
    return bridges


# --------------------------------------------------------------------------- #
# 3. Filtering
# --------------------------------------------------------------------------- #
def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None


def deck_width(mode: str, cls: str, tags: dict) -> float:
    w = _num(tags.get("width"))
    if w is not None and 1.5 <= w <= 45.0:
        return round(float(w), 2)
    if mode == "highway":
        lanes = _num(tags.get("lanes"))
        if lanes is not None and lanes >= 1:
            return round(min(45.0, lanes * 3.5 + 2.0), 2)
        return HW_DEFAULT_WIDTH.get(cls, 9.0)
    return RAILWAY_WIDTH


def load_vardar():
    """Union of the Vardar river centre-lines (UTM) from the cached OSM
    waterways layer. None if the cache is unavailable."""
    if not WATERWAYS.exists():
        return None
    fc = osm_io.load_geojson(WATERWAYS)
    lines = []
    for f in fc.get("features", []):
        pr = f.get("properties") or {}
        if pr.get("waterway") != "river":
            continue
        names = " ".join(str(pr.get(k, "")) for k in
                         ("name", "name:mk", "name:en", "int_name", "alt_name"))
        if ("Вардар" not in names) and ("Vardar" not in names):
            continue
        g = f.get("geometry") or {}
        if g.get("type") == "LineString":
            coords = [_TO_UTM.transform(x, y) for x, y in g["coordinates"]]
            if len(coords) >= 2:
                lines.append(LineString(coords))
    if not lines:
        return None
    return unary_union(lines)


def _stone_names_match(tags: dict) -> bool:
    names = " ".join(str(tags.get(k, "")) for k in
                     ("name", "name:mk", "name:en", "int_name", "alt_name",
                      "old_name", "wikipedia")).lower()
    return any(s in names for s in STONE_NAMES)


def _stone_bridge_geom(bridges: list[dict]):
    """Union of the polylines whose name says Stone Bridge; None if unnamed."""
    lines = [LineString(b["pts"]) for b in bridges if _stone_names_match(b["tags"])]
    return unary_union(lines) if lines else None


def _is_stone_bridge(b: dict, line, stone_geom, stone_en) -> bool:
    if _stone_names_match(b["tags"]):
        return True
    if stone_geom is not None:
        # unnamed fragments (stairs/end ramps) that are part of the structure
        return line.distance(stone_geom) <= STONE_WAY_DIST_M
    cE, cN = line.centroid.x, line.centroid.y
    return math.hypot(cE - stone_en[0], cN - stone_en[1]) <= STONE_RADIUS_M


def filter_bridges(bridges: list[dict], vardar) -> tuple[list[dict], list[dict]]:
    stone_en = _TO_UTM.transform(STONE_LATLON[1], STONE_LATLON[0])
    stone_geom = _stone_bridge_geom(bridges)
    central = shp_box(CENTRAL_BOX[0], CENTRAL_BOX[1], CENTRAL_BOX[2], CENTRAL_BOX[3])
    kept, skipped = [], []

    def skip(b, line, reason):
        skipped.append({"osm_id": b["osm_id"], "merged_osm_ids": b["merged_osm_ids"],
                        "kind": f"{b['mode']}:{b['cls']}",
                        "name": str(b["tags"].get("name", "")),
                        "length_m": round(line.length, 1), "reason": reason})

    for b in bridges:
        line = LineString(b["pts"])
        cE, cN = line.centroid.x, line.centroid.y
        b["line"] = line
        b["cE"], b["cN"] = cE, cN
        b["length_m"] = line.length
        if b["cls"] in NONPHYSICAL:
            skip(b, line, f"nonphysical_class:{b['cls']}")
            continue
        if not (LS_E0 <= cE <= LS_E1 and LS_N0 <= cN <= LS_N1):
            skip(b, line, "outside_landscape")
            continue
        if _is_stone_bridge(b, line, stone_geom, stone_en):
            skip(b, line, "stone_bridge_hero_landmark")
            continue
        central_vardar = bool(vardar is not None and central.contains(Point(cE, cN))
                              and line.intersects(vardar))
        b["central_vardar"] = central_vardar
        if line.length < MIN_LEN and not central_vardar:
            skip(b, line, "short_lt_40m")
            continue
        b["width_m"] = deck_width(b["mode"], b["cls"], b["tags"])
        kept.append(b)

    # Drop foot/cycle decks that merely duplicate a kept road/rail deck (they are
    # the sidewalk of the same physical bridge; a second coplanar deck z-fights).
    road_polys = [k["line"].buffer(k["width_m"] / 2.0 + 1.0, cap_style=2)
                  for k in kept if k["cls"] not in FOOT_CLASSES]
    road_union = unary_union(road_polys) if road_polys else None
    deduped = []
    for k in kept:
        if k["cls"] in FOOT_CLASSES and road_union is not None and k["length_m"] > 0:
            frac = k["line"].intersection(road_union).length / k["length_m"]
            if frac > 0.6:
                skip(k, k["line"], "duplicate_of_road_deck")
                continue
        deduped.append(k)
    skipped.sort(key=lambda s: s["osm_id"])
    return deduped, skipped


# --------------------------------------------------------------------------- #
# 4. Mesh building
# --------------------------------------------------------------------------- #
def densify(pts: list, max_seg: float = MAX_SEG) -> list:
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(math.ceil(d / max_seg)))
        for k in range(1, n + 1):
            out.append((a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))
    return out


def polyline_frame(P: list):
    """Per-vertex (arc s, unit tangent, miter-scaled left vector) of a polyline."""
    n = len(P)
    seg = []
    for a, b in zip(P, P[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        seg.append((dx / L, dy / L, L))
    s = [0.0]
    for d in seg:
        s.append(s[-1] + d[2])
    tang, left = [], []
    for i in range(n):
        if i == 0:
            tx, ty = seg[0][0], seg[0][1]
            m = 1.0
        elif i == n - 1:
            tx, ty = seg[-1][0], seg[-1][1]
            m = 1.0
        else:
            tx, ty = seg[i - 1][0] + seg[i][0], seg[i - 1][1] + seg[i][1]
            L = math.hypot(tx, ty)
            if L < 1e-9:
                tx, ty = seg[i][0], seg[i][1]
            else:
                tx, ty = tx / L, ty / L
            dot = max(-1.0, min(1.0, seg[i - 1][0] * seg[i][0] + seg[i - 1][1] * seg[i][1]))
            cos_half = math.sqrt(max(1e-9, (1.0 + dot) / 2.0))
            m = min(1.0 / cos_half, MITER_LIMIT)
        tang.append((tx, ty))
        left.append((-ty * m, tx * m))
    return s, tang, left


class Mesh:
    """Quad collector -> c3d vertices/indices. Winding is auto-corrected per quad
    so the CCW front face always points along the requested outward normal."""

    def __init__(self):
        self.verts: list[c3d.Vertex] = []
        self.indices: list[int] = []

    def quad(self, p0, p1, p2, p3, nrm, uv0, uv1, uv2, uv3):
        ax, ay, az = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        bx, by, bz = p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2]
        cx, cy, cz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
        pts = [(p0, uv0), (p1, uv1), (p2, uv2), (p3, uv3)]
        if cx * nrm[0] + cy * nrm[1] + cz * nrm[2] < 0.0:
            pts = [(p0, uv0), (p3, uv3), (p2, uv2), (p1, uv1)]
        L = math.sqrt(nrm[0] ** 2 + nrm[1] ** 2 + nrm[2] ** 2) or 1.0
        nx, ny, nz = nrm[0] / L, nrm[1] / L, nrm[2] / L
        base = len(self.verts)
        for (p, (u, v)) in pts:
            self.verts.append(c3d.Vertex(float(p[0]), float(p[1]), float(p[2]),
                                         nx, ny, nz, float(u), float(v)))
        self.indices += [base, base + 1, base + 2, base, base + 2, base + 3]


def build_bridge_mesh(pts_local: list, width: float, deck_z: float):
    """Deck slab + parapets + piers, local to the bridge centroid.

    Returns (Mesh, length_m, left_edge_pts, right_edge_pts) -- edge polylines in
    the same local frame (add the centroid back for UTM overlays)."""
    hw = width / 2.0
    P = densify(pts_local)
    s, tang, left = polyline_frame(P)
    n = len(P)
    Ld = [(P[i][0] + left[i][0] * hw, P[i][1] + left[i][1] * hw) for i in range(n)]
    Rd = [(P[i][0] - left[i][0] * hw, P[i][1] - left[i][1] * hw) for i in range(n)]
    Li = [(P[i][0] + left[i][0] * (hw - PARA_W), P[i][1] + left[i][1] * (hw - PARA_W))
          for i in range(n)]
    Ri = [(P[i][0] - left[i][0] * (hw - PARA_W), P[i][1] - left[i][1] * (hw - PARA_W))
          for i in range(n)]
    zt, zb = deck_z, deck_z - DECK_T
    pz0, pz1 = zt, zt + PARA_H
    u = [si * U_PER_M for si in s]
    vs0, vs1 = V_CON0, V_CON0 + 0.12          # slab side band
    m = Mesh()

    for i in range(n - 1):
        l0, l1, r0, r1 = Ld[i], Ld[i + 1], Rd[i], Rd[i + 1]
        li0, li1, ri0, ri1 = Li[i], Li[i + 1], Ri[i], Ri[i + 1]
        u0, u1 = u[i], u[i + 1]
        nl = (left[i][0] + left[i + 1][0], left[i][1] + left[i + 1][1], 0.0)
        nr = (-nl[0], -nl[1], 0.0)
        # deck top (asphalt band, V across the width)
        m.quad((l0[0], l0[1], zt), (l1[0], l1[1], zt),
               (r1[0], r1[1], zt), (r0[0], r0[1], zt), (0.0, 0.0, 1.0),
               (u0, V_DECK0), (u1, V_DECK0), (u1, V_DECK1), (u0, V_DECK1))
        # deck bottom (concrete)
        m.quad((l0[0], l0[1], zb), (l1[0], l1[1], zb),
               (r1[0], r1[1], zb), (r0[0], r0[1], zb), (0.0, 0.0, -1.0),
               (u0, V_CON0), (u1, V_CON0), (u1, V_CON1), (u0, V_CON1))
        # slab side faces
        m.quad((l0[0], l0[1], zt), (l1[0], l1[1], zt),
               (l1[0], l1[1], zb), (l0[0], l0[1], zb), nl,
               (u0, vs0), (u1, vs0), (u1, vs1), (u0, vs1))
        m.quad((r0[0], r0[1], zt), (r1[0], r1[1], zt),
               (r1[0], r1[1], zb), (r0[0], r0[1], zb), nr,
               (u0, vs0), (u1, vs0), (u1, vs1), (u0, vs1))
        # parapets: outer face, inner face, top -- both edges
        for O0, O1, I0, I1, no in (
            (l0, l1, li0, li1, nl),
            (r0, r1, ri0, ri1, nr),
        ):
            ni = (-no[0], -no[1], 0.0)
            m.quad((O0[0], O0[1], pz1), (O1[0], O1[1], pz1),
                   (O1[0], O1[1], pz0), (O0[0], O0[1], pz0), no,
                   (u0, V_CON0 + 0.14), (u1, V_CON0 + 0.14),
                   (u1, V_CON0 + 0.26), (u0, V_CON0 + 0.26))
            m.quad((I0[0], I0[1], pz1), (I1[0], I1[1], pz1),
                   (I1[0], I1[1], pz0), (I0[0], I0[1], pz0), ni,
                   (u0, V_CON0 + 0.14), (u1, V_CON0 + 0.14),
                   (u1, V_CON0 + 0.26), (u0, V_CON0 + 0.26))
            m.quad((I0[0], I0[1], pz1), (I1[0], I1[1], pz1),
                   (O1[0], O1[1], pz1), (O0[0], O0[1], pz1), (0.0, 0.0, 1.0),
                   (u0, V_CON0 + 0.28), (u1, V_CON0 + 0.28),
                   (u1, V_CON0 + 0.32), (u0, V_CON0 + 0.32))

    # end caps (slab cross-section + parapet ends), outward along -t0 / +t_end
    for idx, sign in ((0, -1.0), (n - 1, +1.0)):
        tx, ty = tang[idx][0] * sign, tang[idx][1] * sign
        Lp, Rp, Lip, Rip = Ld[idx], Rd[idx], Li[idx], Ri[idx]
        m.quad((Lp[0], Lp[1], zt), (Rp[0], Rp[1], zt),
               (Rp[0], Rp[1], zb), (Lp[0], Lp[1], zb), (tx, ty, 0.0),
               (0.0, vs0), (width * U_PER_M, vs0),
               (width * U_PER_M, vs1), (0.0, vs1))
        for Op, Ip in ((Lp, Lip), (Rp, Rip)):
            m.quad((Op[0], Op[1], pz1), (Ip[0], Ip[1], pz1),
                   (Ip[0], Ip[1], pz0), (Op[0], Op[1], pz0), (tx, ty, 0.0),
                   (0.0, V_CON0 + 0.14), (PARA_W * U_PER_M, V_CON0 + 0.14),
                   (PARA_W * U_PER_M, V_CON0 + 0.26), (0.0, V_CON0 + 0.26))

    # piers every ~PIER_SPACING for long bridges
    length = s[-1]
    if length > PIER_MIN_LEN:
        n_p = max(1, int(round(length / PIER_SPACING)) - 1)
        zp0, zp1 = zb - PIER_DROP, zb
        for k in range(1, n_p + 1):
            sp = length * k / (n_p + 1)
            i = max(0, min(n - 2, next(j for j in range(n - 1) if s[j + 1] >= sp - 1e-9)))
            f = (sp - s[i]) / max(1e-9, s[i + 1] - s[i])
            cx = P[i][0] + (P[i + 1][0] - P[i][0]) * f
            cy = P[i][1] + (P[i + 1][1] - P[i][1]) * f
            dx, dy = P[i + 1][0] - P[i][0], P[i + 1][1] - P[i][1]
            L = math.hypot(dx, dy) or 1.0
            ax, ay = dx / L, dy / L                     # along the deck
            qx, qy = -ay, ax                            # across the deck
            ha, hq = PIER_ALONG / 2.0, min(PIER_ACROSS, width) / 2.0
            c00 = (cx - ax * ha - qx * hq, cy - ay * ha - qy * hq)
            c10 = (cx + ax * ha - qx * hq, cy + ay * ha - qy * hq)
            c11 = (cx + ax * ha + qx * hq, cy + ay * ha + qy * hq)
            c01 = (cx - ax * ha + qx * hq, cy - ay * ha + qy * hq)
            for A, B, nq in (
                (c00, c10, (-qx, -qy, 0.0)),
                (c01, c11, (qx, qy, 0.0)),
                (c00, c01, (-ax, -ay, 0.0)),
                (c10, c11, (ax, ay, 0.0)),
            ):
                w_face = math.hypot(B[0] - A[0], B[1] - A[1])
                m.quad((A[0], A[1], zp1), (B[0], B[1], zp1),
                       (B[0], B[1], zp0), (A[0], A[1], zp0), nq,
                       (0.0, V_CON0 + 0.34), (w_face * U_PER_M, V_CON0 + 0.34),
                       (w_face * U_PER_M, V_CON1), (0.0, V_CON1))
    return m, length, Ld, Rd


# --------------------------------------------------------------------------- #
# 5. Shared texture
# --------------------------------------------------------------------------- #
def bake_texture() -> None:
    """Deterministic 512x512 texture: asphalt deck band (top half, faint dashed
    centre line at v~0.25) + concrete band (bottom half). PNG -> DXT1 DDS."""
    W = 512
    rng = np.random.RandomState(20260702)
    img = np.zeros((W, W, 3), np.float32)

    # asphalt band, rows 0..255  (v 0..0.5)
    img[:256] = np.array([74.0, 74.0, 77.0]) + rng.normal(0.0, 4.5, (256, W, 1))
    rows = np.arange(256, dtype=np.float32)
    wear = 5.0 * np.exp(-((rows - 64.0) / 30.0) ** 2) \
         + 5.0 * np.exp(-((rows - 192.0) / 30.0) ** 2)      # lighter wheel tracks
    img[:256] += wear[:, None, None]
    dash = np.zeros((W,), np.float32)
    dash[:256] = 1.0                                        # 5 m dash / 5 m gap at 10 m wrap
    lane = np.array([198.0, 198.0, 186.0])
    for r in range(124, 132):                               # v ~0.242..0.258
        a = 0.38 * dash[:, None]
        img[r] = img[r] * (1.0 - a) + lane * a

    # concrete band, rows 256..511  (v 0.5..1.0)
    img[256:] = np.array([151.0, 149.0, 143.0]) + rng.normal(0.0, 5.0, (256, W, 1))
    img[256:] += rng.normal(0.0, 3.0, (1, W, 1))            # vertical weather streaks
    for r in range(256, 512, 32):                           # shutter-board joints
        img[r:r + 2] -= 12.0

    arr = np.clip(img, 0.0, 255.0).astype(np.uint8)
    png_bytes_old = TEX_PNG.read_bytes() if TEX_PNG.exists() else None
    Image.fromarray(arr).save(TEX_PNG)
    if TEX_DDS.exists() and TEX_PNG.read_bytes() == png_bytes_old:
        print(f"[texture] unchanged, kept {TEX_DDS}")
        return
    r = subprocess.run([str(NVCOMPRESS), "-bc1", "-silent", str(TEX_PNG), str(TEX_DDS)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not TEX_DDS.exists():
        raise RuntimeError(f"nvcompress failed: {(r.stderr or r.stdout)[-300:]}")
    print(f"[texture] baked {TEX_PNG.name} -> {TEX_DDS} ({TEX_DDS.stat().st_size} B)")


# --------------------------------------------------------------------------- #
# 6. QA overlays on the installed game texture (verified georef, TRUE UTM)
# --------------------------------------------------------------------------- #
_PATCCH_CACHE: dict = {}


def _load_patch_rgb(col: int, row: int):
    """Installed patch DDS (col from west, row from south) as an RGB array."""
    key = (col, row)
    if key not in _PATCCH_CACHE:
        p = INSTALL_TEX / f"t{NCOL - 1 - col:02d}{row:02d}.dds"
        if p.exists():
            im = Image.open(p).convert("RGB")
            if im.size != (PATCH_PX, PATCH_PX):
                im = im.resize((PATCH_PX, PATCH_PX))
            _PATCCH_CACHE[key] = np.asarray(im)
        else:
            _PATCCH_CACHE[key] = None
    return _PATCCH_CACHE[key]


def qa_overlay(b: dict, out_png: Path) -> bool:
    """Deck outline (red) + centreline (yellow) over the installed texture crop."""
    ring = b["outline_utm"]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    pad = 60.0
    e0, e1 = min(xs) - pad, max(xs) + pad
    n0, n1 = min(ys) - pad, max(ys) + pad
    c0 = max(0, min(NCOL - 1, int((e0 - TEX_ULX_W) // PATCH_M)))
    c1 = max(0, min(NCOL - 1, int((e1 - TEX_ULX_W) // PATCH_M)))
    r0 = max(0, min(NROW - 1, int((n0 - TEX_SOUTH0) // PATCH_M)))
    r1 = max(0, min(NROW - 1, int((n1 - TEX_SOUTH0) // PATCH_M)))
    mosaic = np.zeros(((r1 - r0 + 1) * PATCH_PX, (c1 - c0 + 1) * PATCH_PX, 3), np.uint8)
    any_tex = False
    for col in range(c0, c1 + 1):
        for row in range(r0, r1 + 1):
            arr = _load_patch_rgb(col, row)
            if arr is None:
                continue
            any_tex = True
            mosaic[(r1 - row) * PATCH_PX:(r1 - row + 1) * PATCH_PX,
                   (col - c0) * PATCH_PX:(col - c0 + 1) * PATCH_PX] = arr
    if not any_tex:
        return False
    west = TEX_ULX_W + c0 * PATCH_M
    north = TEX_SOUTH0 + (r1 + 1) * PATCH_M

    def to_px(E, N):
        return (E - west) / M_PER_PX, (north - N) / M_PER_PX

    x0, y1 = to_px(e0, n0)
    x1, y0 = to_px(e1, n1)
    ix0, iy0 = max(0, int(x0)), max(0, int(y0))
    ix1 = min(mosaic.shape[1], int(math.ceil(x1)))
    iy1 = min(mosaic.shape[0], int(math.ceil(y1)))
    crop = mosaic[iy0:iy1, ix0:ix1]
    if crop.shape[0] < 8 or crop.shape[1] < 8:
        return False
    scale = max(2, min(6, int(900 / max(crop.shape[0], crop.shape[1])) or 2))
    im = Image.fromarray(crop).resize((crop.shape[1] * scale, crop.shape[0] * scale),
                                      Image.NEAREST)
    drw = ImageDraw.Draw(im)

    def draw_line(pts_utm, colour, width):
        px = []
        for (E, N) in pts_utm:
            fx, fy = to_px(E, N)
            px.append(((fx - ix0) * scale, (fy - iy0) * scale))
        drw.line(px, fill=colour, width=width)

    draw_line(ring + [ring[0]], (255, 40, 40), 2)
    draw_line(b["centerline_utm"], (255, 235, 60), 1)
    label = (f"bridge_{b['osm_id']}  {b['kind']}  L={b['length_m']:.0f}m "
             f"w={b['width_m']:.1f}m")
    drw.rectangle([0, 0, 7 * len(label) + 8, 14], fill=(0, 0, 0))
    drw.text((4, 2), label, fill=(255, 255, 255))
    im.save(out_png)
    return True


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--deck-z", type=float, default=DECK_Z,
                    help=f"deck-top height above the local origin (default {DECK_Z})")
    ap.add_argument("--no-qa", action="store_true", help="skip the QA overlays")
    ap.add_argument("--refetch", action="store_true",
                    help="ignore the Overpass cache and refetch")
    args = ap.parse_args()
    deck_z = float(args.deck_z)

    OBJ_DIR.mkdir(parents=True, exist_ok=True)
    QA_DIR.mkdir(parents=True, exist_ok=True)

    fc = fetch_bridges(refetch=args.refetch)
    segs = load_segments(fc)
    print(f"[merge] {len(segs)} bridge way segments")
    bridges = merge_segments(segs)
    print(f"[merge] -> {len(bridges)} merged bridge polylines")

    vardar = load_vardar()
    if vardar is None:
        print("[filter] WARNING: Vardar not found in waterways cache -- "
              "central-Skopje short-bridge keeps disabled")
    kept, skipped = filter_bridges(bridges, vardar)
    n_central = sum(1 for b in kept if b.get("central_vardar"))
    print(f"[filter] kept {len(kept)} bridges ({n_central} central-Skopje Vardar "
          f"crossings), skipped {len(skipped)}")

    bake_texture()

    # stale outputs from earlier runs must not survive (deterministic output set)
    for old in OBJ_DIR.glob("bridge_*.c3d"):
        old.unlink()

    placements = []
    total_verts = total_tris = 0
    for b in sorted(kept, key=lambda x: x["osm_id"]):
        cE, cN = b["cE"], b["cN"]
        local = [(x - cE, y - cN) for (x, y) in b["pts"]]
        mesh, length, Ld, Rd = build_bridge_mesh(local, b["width_m"], deck_z)
        b["length_m"] = length
        b["kind"] = f"{b['mode']}:{b['cls']}"
        b["outline_utm"] = ([(x + cE, y + cN) for (x, y) in Ld]
                            + [(x + cE, y + cN) for (x, y) in Rd[::-1]])
        b["centerline_utm"] = list(b["pts"])
        name = f"bridge_{b['osm_id']}"
        obj = c3d.C3DObject(name=name, texture=TEX_REF,
                            material=c3d.WHITE_MATERIAL,
                            vertices=mesh.verts, indices=mesh.indices)
        path = OBJ_DIR / f"{name}.c3d"
        blob = c3d.write_c3d(c3d.C3DFile(objects=[obj]), path)
        # round-trip gate: parse what we wrote, re-write, must be byte-identical
        if c3d.write_c3d(c3d.parse_c3d(blob)) != blob:
            raise RuntimeError(f"c3d round-trip FAILED for {path}")
        total_verts += len(mesh.verts)
        total_tris += len(mesh.indices) // 3
        placements.append({
            "name": name,
            "c3d": f"{name}.c3d",
            "E": round(cE, 2),
            "N": round(cN, 2),
            "ori_deg": 0.0,
            "scale": 1.0,
            "length_m": round(length, 1),
            "width_m": round(b["width_m"], 2),
            "kind": b["kind"],
            "osm_id": b["osm_id"],
            "deck_z_m": deck_z,
            "osm_name": str(b["tags"].get("name", "")),
            "merged_osm_ids": b["merged_osm_ids"],
            "central_vardar": bool(b.get("central_vardar")),
            "verts": len(mesh.verts),
            "tris": len(mesh.indices) // 3,
        })

    placements_doc = {
        "note": ("ori_deg=0 BY DESIGN: the mesh is world-aligned in its local "
                 "frame (local x=E, y=N about the TRUE-UTM centroid), so the "
                 "deck bearing is baked into the geometry. E/N are TRUE UTM 34N "
                 "(EPSG:32634). posZ at placement = terrain altitude at (E,N); "
                 "the deck top sits deck_z_m above that."),
        "texture": TEX_REF,
        "count": len(placements),
        "placements": placements,
    }
    (OUT / "placements.json").write_text(json.dumps(placements_doc, indent=2),
                                         encoding="utf-8")
    print(f"[out] {len(placements)} c3d in {OBJ_DIR}")
    print(f"[out] {OUT / 'placements.json'}")

    # length histogram
    bins = [(0, 40), (40, 60), (60, 100), (100, 150), (150, 250), (250, 400),
            (400, 10000)]
    hist = {}
    for lo, hi in bins:
        label = f"{lo}-{hi}m" if hi < 10000 else f"{lo}m+"
        hist[label] = sum(1 for p in placements if lo <= p["length_m"] < hi)
    report = {
        "bridge_count": len(placements),
        "central_vardar_crossings": n_central,
        "total_verts": total_verts,
        "total_tris": total_tris,
        "length_histogram_m": hist,
        "deck_z_m": deck_z,
        "raw_way_segments": len(segs),
        "merged_polylines": len(bridges),
        "skipped_count": len(skipped),
        "skipped_reasons": {},
        "skipped": skipped,
    }
    for sk in skipped:
        r = sk["reason"]
        report["skipped_reasons"][r] = report["skipped_reasons"].get(r, 0) + 1
    report["skipped_reasons"] = dict(sorted(report["skipped_reasons"].items()))
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[out] {OUT / 'report.json'}  verts={total_verts} tris={total_tris}")
    print(f"[out] histogram: {hist}")

    # QA: 8 longest + 4 longest central-Skopje Vardar crossings (distinct)
    if not args.no_qa:
        by_len = sorted(kept, key=lambda x: (-x["length_m"], x["osm_id"]))
        sel = list(by_len[:8])
        sel_ids = {b["osm_id"] for b in sel}
        central_sorted = [b for b in by_len if b.get("central_vardar")
                          and b["osm_id"] not in sel_ids]
        sel += central_sorted[:4]
        n_ok = 0
        for b in sel:
            png = QA_DIR / f"bridge_{b['osm_id']}_qa.png"
            if qa_overlay(b, png):
                n_ok += 1
                print(f"[qa] {png.name}  {b['kind']}  L={b['length_m']:.0f}m  "
                      f"'{b['tags'].get('name', '')}'")
            else:
                print(f"[qa] SKIP {png.name} (no installed texture under bridge)")
        print(f"[qa] wrote {n_ok}/{len(sel)} overlays -> {QA_DIR}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
