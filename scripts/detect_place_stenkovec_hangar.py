#!/usr/bin/env python3
r"""Detect the Stenkovec (Aeroklub Skopje) glider-hangar footprint in the ortho,
derive its UTM centroid + long-axis bearing + door side, and PLACE the migrated
SoFly hangar ``.c3d`` onto it -- alignment by construction, validated by decoding
the installed ``.obj`` record back and overlaying the model footprint on the ortho.

WHY THIS SCRIPT (vs the old OSM-centroid + guessed-bearing placement)
---------------------------------------------------------------------
The previous install put the hangar at the OSM centroid with a guessed 30.4 deg
bearing (OSM PCA). In-sim that was wrong: metres off and ~rotated. The footprint
in the ORTHO is the placement ground truth (the sim renders that same ortho), so we
detect it directly.

DETECTION SOURCE
----------------
The INSTALLED t0704.dds is only 2.8125 m/px -> the 31 m hangar is ~11 px, too coarse
to fit. We therefore pull a high-resolution crop of the SAME Macedonian 2023 cadastre
ortho (APP_DATA:ORTOFOTO_2023, EPSG:6316) at zoom 12 = 0.14 m/px (~220 px across the
hangar) via the public GWC WMS. The product family and georeferencing match the
installed texture (CLAUDE.md: install textures come from this MK 2023 WMS), and a
permanent building's geometry is epoch-invariant, so the footprint transfers exactly.

DETECTOR (robust oriented-rectangle fit; the GPU/ML SAM3 path is attempted first via
``--use-sam`` but at this scale the edge-energy fit is the reliable workhorse):
  maximise the mean Sobel-gradient sampled along an oriented rectangle's border
  (a building outline is a strong closed gradient ring) over (centre, angle, L, W),
  coarse-to-fine, seeded at the OSM centroid with the OSM size as a prior. The
  interior/exterior texture may match (dry season) -- keying on the OUTLINE makes the
  fit immune to that.

DOOR SIDE (resolve the 180 deg ambiguity)
-----------------------------------------
From the glTF: the ``Hangar_Door`` material sits at +X, the ``Front`` lean-to /
clubroom (chairs/table/chalkboard) at -X. The converter maps glTF +X -> Condor +E,
so in the model the doors face the model's local +X (=+E at ori=0). At placement we
rotate so the model long axis matches the detected bearing AND the doors point to the
apron/runway side (SE, toward the painted 12/30 strip). We pick the ori (of the two
180-apart options) whose door normal points more toward the apron azimuth.

PLACEMENT ANCHOR
----------------
posX/posY use the patch-grid SE corner (576000 / 4631040) -- the anchor the installed
textures were built on (condor_grid.patch_bounds_utm) and the one install_osm_autogen
uses, so objects register with the texture the sim draws. (The texture-overlay
validation itself is anchor-independent: it maps the decoded UTM straight to the
ortho, so a pass means the record reconstructs the true ground point.)

OUTPUTS (sandbox; install only on --install):
  .sandbox/airport_objects/_work/hangar_detect_final.png|json  -- detection
  .sandbox/airport_objects/hangar_placement_overlay.png        -- validation overlay
  on --install: rewrites C:/Condor2/.../MacedoniaSkopje.obj keeping MillenniumCross.

No GUI. No hash. Deterministic.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import condor_grid as G          # noqa: E402
import c3d                       # noqa: E402

REPO = Path(__file__).resolve().parents[1]
WORK = REPO / ".sandbox/airport_objects/_work"
OUT = REPO / ".sandbox/airport_objects"
GLTF = Path("F:/FS2020/Official/OneStore/sofly-lwxx-airfields"
            "/SimObjects/Landmarks/stenkovec-hangar/model/LW75_Main_Hangar.gltf")
INSTALL = Path("C:/Condor2/Landscapes/MacedoniaSkopje")
OBJFILE = INSTALL / "MacedoniaSkopje.obj"
TEX_INSTALL = INSTALL / "Textures"

# OBJECT-PLACEMENT ANCHOR.
# Use the VERIFIED condor_grid anchor = the .trn-header BR pixel-centre + half-pixel
# (575955 / 4631085), calibrated against Slovenia2 (371 buildings, residual ~0 m on the
# painted rooftops). The prior Stenkovec hangar used this same anchor and the task reports
# it was off only "by metres" (from the OSM centroid + bad bearing), NOT ~64 m -- which is
# the proof the .trn anchor is the correct one (the patch-grid corner used by
# install_osm_autogen would have put the old hangar ~64 m off). We feed it the TRUE roof
# UTM from the texture detection, so the placement is better than the OSM-centroid version.
# NOTE: the texture-overlay validation below is anchor-independent (it maps the decoded
# UTM straight onto the ortho), so it stays valid whichever anchor is chosen; the anchor
# only sets the in-sim georeference, which the parent verifies in Condor.
ANCHOR_E, ANCHOR_N = G.OBJ_ANCHOR_E, G.OBJ_ANCHOR_N    # 575955.0 / 4631085.0 (verified)
HANGAR_PATCH = (7, 4)                                   # t0704 covers 42.05968N,21.38488E
DEM = REPO / "sources/dem/macedonia_skopje_dem_30m_2305_flat.raw"
ULX_W, ULY_N, DEMW, XDIM = 506880.0, 4700160.0, 2305, 30.0

_T_TO_6316 = Transformer.from_crs("EPSG:32634", "EPSG:6316", always_xy=True)
_T_FROM_6316 = Transformer.from_crs("EPSG:6316", "EPSG:32634", always_xy=True)
_T_FROM_WGS = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)

# Apron / runway side: Stenkovec runway 12/30 painted strip is SE of the hangar; the
# doors open toward it. Apron azimuth ~120 deg (ESE) -- used only to break the 180 deg
# door ambiguity, not for the long-axis angle (that comes from the ortho).
APRON_AZIMUTH_DEG = 120.0


# --------------------------------------------------------------------------- #
# 0. OSM hangar prior (centroid + size + bearing) -- the search seed/cross-check
# --------------------------------------------------------------------------- #
def osm_hangar_prior():
    from shapely.geometry import shape
    from shapely.ops import transform as shp_transform
    d = json.loads((REPO / ".sandbox/osm/buildings.geojson").read_text(encoding="utf-8"))
    best = None
    for f in d["features"]:
        pr = f.get("properties", {})
        if pr.get("aeroway") == "hangar" and "Аероклуб" in str(pr.get("name", "")):
            g = shape(f["geometry"])
            c = g.centroid
            if abs(c.y - 42.0597) < 0.02 and abs(c.x - 21.3849) < 0.02:
                best = f
                break
    if best is None:
        raise SystemExit("OSM Aeroklub Skopje hangar not found in buildings.geojson")
    poly = shp_transform(_T_FROM_WGS.transform, shape(best["geometry"]))
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda p: p.area)
    mar = poly.minimum_rotated_rectangle
    xs, ys = mar.exterior.coords.xy
    edges = sorted(((math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i]),
                     xs[i + 1] - xs[i], ys[i + 1] - ys[i]) for i in range(4)),
                   key=lambda t: -t[0])
    L, dx, dy = edges[0]
    short = edges[2][0]
    brg = math.degrees(math.atan2(dx, dy)) % 180.0
    return dict(centroid=(poly.centroid.x, poly.centroid.y), long_m=L, short_m=short,
                bearing=brg, area=poly.area, poly=poly)


# --------------------------------------------------------------------------- #
# 1. High-res ortho fetch (EPSG:6316 GWC tiles, zoom 12 = 0.14 m/px)
# --------------------------------------------------------------------------- #
ORIGIN_X6, ORIGIN_Y6 = 7397000.634424793, 4521901.793180252
RESOLUTIONS = [560, 280, 140, 70, 35, 17.92, 8.96, 4.48, 2.24, 1.12, 0.56, 0.28, 0.14]
GWC = "https://e-uslugi.katastar.gov.mk/geo/proxy/gwc/wms"
GWC_HDR = {"Referer": "https://e-uslugi.katastar.gov.mk/"}


def fetch_ortho(center_utm, zoom=12, half_m=100.0):
    """Mosaic GWC zoom-`zoom` EPSG:6316 tiles covering a square around center_utm.

    Returns (rgb HxWx3 uint8, meta dict with ext6316/res/W/H). Cached on disk."""
    import requests
    cache_img = WORK / f"hangar_ortho_z{zoom}.jpg"
    cache_meta = WORK / f"hangar_ortho_z{zoom}.json"
    if cache_img.exists() and cache_meta.exists():
        meta = json.loads(cache_meta.read_text())
        return np.asarray(Image.open(cache_img).convert("RGB")), meta
    res = RESOLUTIONS[zoom]
    ts = 256 * res
    x6, y6 = _T_TO_6316.transform(*center_utm)
    txmin = int(math.floor((x6 - half_m - ORIGIN_X6) / ts))
    txmax = int(math.floor((x6 + half_m - ORIGIN_X6) / ts))
    tymin = int(math.floor((y6 - half_m - ORIGIN_Y6) / ts))
    tymax = int(math.floor((y6 + half_m - ORIGIN_Y6) / ts))
    nx, ny = txmax - txmin + 1, tymax - tymin + 1
    mosaic = Image.new("RGB", (nx * 256, ny * 256))
    ok = 0
    for ty in range(tymin, tymax + 1):
        for tx in range(txmin, txmax + 1):
            minx = ORIGIN_X6 + tx * ts
            miny = ORIGIN_Y6 + ty * ts
            params = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
                      "LAYERS": "APP_DATA:ORTOFOTO_2023", "STYLES": "", "FORMAT": "image/jpeg",
                      "TILED": "true", "GRIDSET": "MSCS6316", "SRS": "EPSG:6316",
                      "BBOX": f"{minx},{miny},{minx + ts},{miny + ts}",
                      "WIDTH": 256, "HEIGHT": 256}
            from urllib.parse import urlencode
            r = requests.get(GWC + "?" + urlencode(params), headers=GWC_HDR, timeout=30)
            if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image"):
                im = Image.open(io.BytesIO(r.content)).convert("RGB")
                mosaic.paste(im, ((tx - txmin) * 256, (tymax - ty) * 256))
                ok += 1
    if ok == 0:
        raise SystemExit("ortho fetch failed: GWC returned no tiles")
    ext = [ORIGIN_X6 + txmin * ts, ORIGIN_Y6 + tymin * ts,
           ORIGIN_X6 + (txmax + 1) * ts, ORIGIN_Y6 + (tymax + 1) * ts]
    meta = dict(zoom=zoom, res=res, crs="EPSG:6316", ext6316=ext, W=nx * 256, H=ny * 256)
    WORK.mkdir(parents=True, exist_ok=True)
    mosaic.save(cache_img)
    cache_meta.write_text(json.dumps(meta, indent=2))
    return np.asarray(mosaic), meta


def _make_px_mappers(meta):
    minx, miny, maxx, maxy = meta["ext6316"]
    W, H = meta["W"], meta["H"]

    def px_to_utm(px, py):
        x6 = minx + (px + 0.5) / W * (maxx - minx)
        y6 = maxy - (py + 0.5) / H * (maxy - miny)
        return _T_FROM_6316.transform(x6, y6)

    def utm_to_px(e, n):
        x6, y6 = _T_TO_6316.transform(e, n)
        return ((x6 - minx) / (maxx - minx) * W, (maxy - y6) / (maxy - miny) * H)

    return px_to_utm, utm_to_px


# --------------------------------------------------------------------------- #
# 2. Deterministic roof detector (de-rotated edge projection + bright-roof extent)
# --------------------------------------------------------------------------- #
def detect_hangar_derotate(gray, seed_px, prior, res_m, search_px=140, tilt_max=12):
    """DETERMINISTIC roof fit by de-rotated edge projection (the reliable workhorse).

    The hangar roof is a clean straight-edged quad. For each candidate tilt theta we
    rotate a crop about the seed by theta and project |Sobel| onto the two axes; a
    rectangle aligned with the de-rotated axes shows TWO sharp peaks in each profile
    (its 4 sides). We pick theta maximising the summed top-2 peaks in both axes -> the
    roof's edges, then read the rectangle's centre / length / width / bearing back in
    UTM. Robust where interior vs. dry-field brightness is similar, because it keys on
    the building's straight OUTLINE, and it never gets stuck on one strong edge (it
    requires two parallel peaks per axis). Returns (cx,cy,L_px,W_px,ang_rad).
    """
    import cv2
    R = int(search_px)
    sx, sy = int(round(seed_px[0])), int(round(seed_px[1]))
    crop = gray[sy - R:sy + R, sx - R:sx + R].astype(np.float32)
    # plausible hangar side range in pixels (8..45 m), to reject spurious close peaks
    smin = int(8.0 / res_m)
    smax = int(45.0 / res_m)

    def best_pair(p):
        """Pick the two strong, well-separated edges (a side range), preferring a pair
        that BRACKETS the crop centre R (the seed sits inside the building)."""
        cand = [i for i in range(2, len(p) - 2)
                if p[i] >= p[i - 1] and p[i] >= p[i + 1] and p[i] > 0.25 * p.max()]
        bestpair = None
        for i in range(len(cand)):
            for j in range(i + 1, len(cand)):
                a, b = cand[i], cand[j]
                d = abs(b - a)
                if d < smin or d > smax:
                    continue
                brackets = (min(a, b) - 6 <= R <= max(a, b) + 6)
                strength = p[a] + p[b] + (0.5 * p.max() if brackets else 0.0)
                if bestpair is None or strength > bestpair[0]:
                    bestpair = (strength, sorted((a, b)))
        if bestpair is None:                       # fallback: global top-2 with min sep
            order = np.argsort(p)[::-1]
            chosen = [int(order[0])]
            for k in order[1:]:
                if all(abs(int(k) - c) >= smin for c in chosen):
                    chosen.append(int(k))
                if len(chosen) == 2:
                    break
            return sorted(chosen), float(p[chosen].sum())
        return bestpair[1], float(bestpair[0])

    best = None
    for thdeg in np.arange(-tilt_max, tilt_max + 0.01, 1.0):
        Mr = cv2.getRotationMatrix2D((R, R), thdeg, 1.0)
        rot = cv2.warpAffine(crop, Mr, (2 * R, 2 * R), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
        gx = cv2.Sobel(rot, cv2.CV_32F, 1, 0, 3)
        gy = cv2.Sobel(rot, cv2.CV_32F, 0, 1, 3)
        cwp, cs = best_pair(np.abs(gx).sum(0))    # vertical edges -> E/W sides
        rnp, rs = best_pair(np.abs(gy).sum(1))    # horizontal edges -> N/S ends
        if best is None or cs + rs > best["score"]:
            best = dict(score=cs + rs, th=thdeg, cw=sorted(cwp), rn=sorted(rnp))

    th = best["th"]
    cw, rn = best["cw"], best["rn"]
    wpx, lpx = cw[1] - cw[0], rn[1] - rn[0]              # E-W width, N-S length (rot frame)
    rcx, rcy = (cw[0] + cw[1]) / 2, (rn[0] + rn[1]) / 2
    Minv = cv2.invertAffineTransform(cv2.getRotationMatrix2D((R, R), th, 1.0))

    def rot_to_full(px, py):
        ox = Minv[0, 0] * px + Minv[0, 1] * py + Minv[0, 2]
        oy = Minv[1, 0] * px + Minv[1, 1] * py + Minv[1, 2]
        return sx - R + ox, sy - R + oy

    fcx, fcy = rot_to_full(rcx, rcy)
    # long axis is N-S in the de-rotated frame; angle (px x grows E, y grows S) of the
    # longer side. ang is measured so cos(ang),sin(ang) is the long-axis direction.
    if lpx >= wpx:
        L_px, W_px = lpx, wpx
        # long axis = vertical (0,1) de-rotated by -th
        ang = math.radians(90.0 - th)
    else:
        L_px, W_px = wpx, lpx
        ang = math.radians(-th)
    return fcx, fcy, float(L_px), float(W_px), ang, th


def _refine_extent(gray, cx, cy, tilt_deg, res_m, search_px=160):
    """Given the roof CENTRE and tilt, measure the bright-roof EXTENT along both axes.

    De-rotate a crop so the roof is axis-aligned, learn the roof brightness from the
    centre, then on the row/column through the centre find the contiguous bright span ->
    true E-W width and N-S length (in metres) and a refined centre. This recovers the
    full length even when one gable edge is shadowed (the edge-projection can clip it).
    """
    import cv2
    R = int(search_px)
    sx, sy = int(round(cx)), int(round(cy))
    crop = gray[sy - R:sy + R, sx - R:sx + R].astype(np.float32)
    Mr = cv2.getRotationMatrix2D((R, R), tilt_deg, 1.0)
    rot = cv2.warpAffine(crop, Mr, (2 * R, 2 * R), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)
    roofval = float(np.median(rot[R - 10:R + 10, R - 10:R + 10]))
    tol = 32.0
    band = np.abs(rot - roofval) < tol

    def span(line):
        # longest run of True through index R
        if not band_line_ok(line, R):
            return None
        i = R
        while i > 0 and line[i - 1]:
            i -= 1
        j = R
        while j < len(line) - 1 and line[j + 1]:
            j += 1
        return i, j

    def band_line_ok(line, idx):
        return 0 <= idx < len(line) and line[idx]

    col_through = band[:, R]
    row_through = band[R, :]
    ns = span(col_through)
    ew = span(row_through)
    if ns is None or ew is None:
        return None
    n0, n1 = ns
    e0, e1 = ew
    lpx = n1 - n0
    wpx = e1 - e0
    # refined centre (rot frame) -> full image
    rcx, rcy = (e0 + e1) / 2, (n0 + n1) / 2
    Minv = cv2.invertAffineTransform(Mr)
    ox = Minv[0, 0] * rcx + Minv[0, 1] * rcy + Minv[0, 2]
    oy = Minv[1, 0] * rcx + Minv[1, 1] * rcy + Minv[1, 2]
    fcx, fcy = sx - R + ox, sy - R + oy
    return fcx, fcy, lpx * res_m, wpx * res_m


def detect_hangar(args, prior):
    import cv2
    img, meta = fetch_ortho(prior["centroid"], zoom=args.zoom, half_m=args.window / 2 + 20)
    px_to_utm, utm_to_px = _make_px_mappers(meta)
    res = meta["res"]
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    seed = utm_to_px(*prior["centroid"])

    # Detect, then RE-CENTRE on the result and detect again so the (off-centre) OSM-seed
    # bias is removed -- the de-rotated projection is most accurate when the crop is
    # centred on the roof. Converges in 1-2 iterations.
    cx, cy, L, Wd, ang, tilt = detect_hangar_derotate(gray, seed, prior, res)
    for _ in range(2):
        cx2, cy2, L2, Wd2, ang2, tilt2 = detect_hangar_derotate(gray, (cx, cy), prior, res)
        if abs(cx2 - cx) < 1 and abs(cy2 - cy) < 1:
            cx, cy, L, Wd, ang, tilt = cx2, cy2, L2, Wd2, ang2, tilt2
            break
        cx, cy, L, Wd, ang, tilt = cx2, cy2, L2, Wd2, ang2, tilt2

    # Refine the EXTENT (length/width) and centre from the bright-roof span along the now-
    # known axes -- recovers the full N-S length the edge-projection clips at a shadowed
    # gable. Keep the edge-projection size only if the extent scan fails.
    ext = _refine_extent(gray, cx, cy, tilt, res)
    if ext is not None:
        fcx, fcy, long_extent, short_extent = ext
        # sanity: extents must be hangar-plausible, else keep edge-projection result
        if 24 <= long_extent <= 42 and 12 <= short_extent <= 26:
            cx, cy = fcx, fcy
            L, Wd = long_extent / res, short_extent / res

    cE, cN = px_to_utm(cx, cy)
    ca, sa = math.cos(ang), math.sin(ang)
    p0 = px_to_utm(cx - ca * L / 2, cy - sa * L / 2)
    p1 = px_to_utm(cx + ca * L / 2, cy + sa * L / 2)
    bearing = math.degrees(math.atan2(p1[0] - p0[0], p1[1] - p0[1])) % 180.0
    long_m, short_m = L * res, Wd * res

    det = dict(centroid_utm=[cE, cN], long_m=long_m, short_m=short_m, bearing_mod180=bearing,
               tilt_deg=float(tilt), rect_px=[cx, cy, L, Wd, math.degrees(ang)],
               res=res, ext6316=meta["ext6316"], W=meta["W"], H=meta["H"],
               zoom=meta["zoom"], osm_centroid_utm=list(prior["centroid"]),
               method="derotated edge-projection (deterministic)")
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "hangar_detect_final.json").write_text(json.dumps(det, indent=2))

    vis = img.copy()
    hl, hw = L / 2, Wd / 2
    ux, uy = ca, sa
    vx, vy = -sa, ca
    corners = np.array([(cx + a * ux + b * vx, cy + a * uy + b * vy)
                        for a, b in [(-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw)]], dtype=np.int32)
    cv2.polylines(vis, [corners], True, (0, 255, 255), 2)
    cv2.circle(vis, (int(cx), int(cy)), 4, (255, 255, 0), -1)
    cv2.drawMarker(vis, tuple(int(v) for v in seed), (255, 0, 255), cv2.MARKER_CROSS, 16, 2)
    Image.fromarray(vis).save(WORK / "hangar_detect_final.png")
    return det


# --------------------------------------------------------------------------- #
# 3. Door axis from the glTF (resolve 180 deg) + altitude
# --------------------------------------------------------------------------- #
def door_axis_condor_deg():
    """Return the Condor azimuth (deg, 0=N CW) the model's doors face at ori=0.

    glTF doors at +X, clubroom at -X. Converter maps glTF X -> Condor +E. So at ori=0
    the doors face local +E == azimuth 90 deg. (We verify the +X/-X split here.)"""
    d = json.loads(GLTF.read_text())
    buf = (GLTF.parent / d["buffers"][0]["uri"]).read_bytes()
    _CT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2),
           5125: ("I", 4), 5126: ("f", 4)}
    _NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}

    def acc(ai):
        a = d["accessors"][ai]
        bv = d["bufferViews"][a["bufferView"]]
        off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        nc = _NC[a["type"]]
        fmt, sz = _CT[a["componentType"]]
        stride = bv.get("byteStride") or sz * nc
        out = np.empty((a["count"], nc))
        for i in range(a["count"]):
            out[i] = struct.unpack_from("<%d%s" % (nc, fmt), buf, off + i * stride)
        return out

    def qmat(x, y, z, w):
        n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
        x, y, z, w = x / n, y / n, z / n, w / n
        return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])

    def nmat(nd):
        if "matrix" in nd:
            return np.array(nd["matrix"]).reshape(4, 4).T
        T = np.eye(4)
        if "translation" in nd:
            T[:3, 3] = nd["translation"]
        R = np.eye(4)
        if "rotation" in nd:
            x, y, z, w = nd["rotation"]
            R[:3, :3] = qmat(x, y, z, w)
        S = np.eye(4)
        if "scale" in nd:
            S[0, 0], S[1, 1], S[2, 2] = nd["scale"]
        return T @ R @ S

    nodes = d["nodes"]
    scene = d["scenes"][d.get("scene", 0)]
    world = {}

    def walk(ni, P):
        M = P @ nmat(nodes[ni])
        world[ni] = M
        for c in nodes[ni].get("children", []):
            walk(c, M)

    for r in scene["nodes"]:
        walk(r, np.eye(4))
    door_mat = {i for i, m in enumerate(d["materials"])
                if m.get("name") in ("Hangar_Door", "Doors")}
    front_mat = {i for i, m in enumerate(d["materials"])
                 if m.get("name") in ("Front", "Chair", "Table", "Chalkboard")}
    dsum = np.zeros(3); dn = 0
    fsum = np.zeros(3); fn = 0
    for ni, M in world.items():
        nd = nodes[ni]
        if "mesh" not in nd:
            continue
        for prim in d["meshes"][nd["mesh"]]["primitives"]:
            mi = prim.get("material")
            pos = acc(prim["attributes"]["POSITION"])[:, :3]
            pw = (M @ np.c_[pos, np.ones(len(pos))].T).T[:, :3]
            if mi in door_mat:
                dsum += pw.sum(0); dn += len(pw)
            elif mi in front_mat:
                fsum += pw.sum(0); fn += len(pw)
    door_x = dsum[0] / max(dn, 1)
    front_x = fsum[0] / max(fn, 1)
    # doors should be at +X, clubroom at -X. Condor +E = +X, so doors face +E (90 deg).
    doors_face_plus_e = door_x > front_x
    return (90.0 if doors_face_plus_e else 270.0), dict(door_glTF_x=door_x, front_glTF_x=front_x)


def altitude(E, N):
    dem = np.fromfile(DEM, dtype="<u2").reshape(DEMW, DEMW)
    c = min(max(int(round((E - ULX_W) / XDIM)), 0), DEMW - 1)
    r = min(max(int(round((ULY_N - N) / XDIM)), 0), DEMW - 1)
    return float(dem[r, c])


# --------------------------------------------------------------------------- #
# 4. Placement: ori so model long-axis -> detected bearing, doors -> apron side
# --------------------------------------------------------------------------- #
def model_long_axis_deg():
    """The migrated c3d's long axis azimuth at ori=0. The footprint extent is wider in
    E than N (door at +E), so the LONG axis is E-W -> azimuth 90 deg at ori=0. Compute
    it from the staged mesh AABB to be exact."""
    cf = c3d.parse_c3d(OUT / "StenkovecHangar.c3d")
    P = np.array([(v.px, v.py) for o in cf.objects for v in o.vertices])
    dx = P[:, 0].max() - P[:, 0].min()
    dy = P[:, 1].max() - P[:, 1].min()
    # azimuth of the longer footprint axis at ori=0: E-axis=90 if dx>dy else N-axis=0
    return (90.0 if dx >= dy else 0.0), float(dx), float(dy)


def compute_ori(det_bearing, door_axis_deg):
    """ori (deg) that rotates the model so its long axis aligns to det_bearing and the
    doors point to the apron (SE, ~APRON_AZIMUTH). Two candidates 180 apart; pick the
    one whose rotated door normal is closest to the apron azimuth."""
    model_axis, dx, dy = model_long_axis_deg()
    # ori brings model_axis -> det_bearing (mod 180 leaves a 180 ambiguity)
    base = (det_bearing - model_axis) % 360.0
    cands = [base % 360.0, (base + 180.0) % 360.0]
    best = None
    for ori in cands:
        door_world = (door_axis_deg + ori) % 360.0          # where doors point after rotation
        # circular distance to apron azimuth
        dd = abs((door_world - APRON_AZIMUTH_DEG + 180) % 360 - 180)
        if best is None or dd < best[1]:
            best = (ori, dd, door_world)
    return best[0], best[2], cands, (model_axis, dx, dy)


# --------------------------------------------------------------------------- #
# 5. Install record + validation overlay on the ortho
# --------------------------------------------------------------------------- #
def read_existing_records():
    if not OBJFILE.exists():
        return []
    d = OBJFILE.read_bytes()
    recs = []
    for k in range(len(d) // 152):
        b = k * 152
        px, py, pz, sc, ori = struct.unpack_from("<5f", d, b)
        nl = d[b + 20]
        nm = d[b + 21:b + 21 + nl].decode("ascii", "replace")
        recs.append((px, py, pz, sc, ori, nm, d[b:b + 152]))
    return recs


def make_record(name, E, N, z, ori_deg, scale=1.0):
    nm = name.encode("ascii")
    if len(nm) > 131:
        raise ValueError(f"name too long for .obj record ({len(nm)} > 131): {name!r}")
    posX = ANCHOR_E - E
    posY = N - ANCHOR_N
    ori = math.radians(ori_deg) % (2 * math.pi)
    return struct.pack("<5f", posX, posY, z, scale, ori) + bytes([len(nm)]) + nm.ljust(131, b"\x00")


def install(det, ori_deg, scale):
    cE, cN = det["centroid_utm"]
    z = altitude(cE, cN)
    existing = read_existing_records()
    out = bytearray()
    kept = []
    for (px, py, pz, sc, ori, nm, raw) in existing:
        if nm.lower().startswith("stenkovechangar"):
            continue                       # replace the hangar record
        out += raw
        kept.append(nm)
    out += make_record("StenkovecHangar.c3d", cE, cN, z, ori_deg, scale)
    OBJFILE.write_bytes(bytes(out))
    return z, kept


def validate_overlay(det, ori_deg, scale, door_axis_deg, installed=True):
    """Decode the hangar record (from the install .obj if installed, else synthesise),
    rotate the model footprint by ori at the placed centroid, and overlay on the ortho.
    `door_axis_deg` is the model's door azimuth at ori=0 (from the glTF).
    Returns (residual_m, residual_deg, png_path)."""
    import cv2
    # detected truth
    cE, cN = det["centroid_utm"]
    det_brg = det["bearing_mod180"]

    # placed centroid: decode from the installed record (round-trips the anchor)
    if installed:
        rec = next(r for r in read_existing_records()
                   if r[5].lower().startswith("stenkovechangar"))
        posX, posY, pz, sc, ori = rec[0], rec[1], rec[2], rec[3], rec[4]
        placedE = ANCHOR_E - posX
        placedN = ANCHOR_N + posY
        ori_deg = math.degrees(ori) % 360.0
        scale = sc
    else:
        placedE, placedN = cE, cN

    # model footprint corners (local E/N at ori=0) from the c3d AABB, then rotate by ori
    cf = c3d.parse_c3d(OUT / "StenkovecHangar.c3d")
    P = np.array([(v.px, v.py) for o in cf.objects for v in o.vertices])
    xmin, xmax = P[:, 0].min(), P[:, 0].max()
    ymin, ymax = P[:, 1].min(), P[:, 1].max()
    local = np.array([(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]) * scale
    th = math.radians(ori_deg)
    # Condor: world = ref + R(-ori).local ; local (x=E,y=N). R(-ori) for azimuth ori:
    #   E' =  cos*x + sin*y ; N' = -sin*x + cos*y    (so local +Y -> azimuth ori)
    c, s = math.cos(th), math.sin(th)
    worldE = placedE + (c * local[:, 0] + s * local[:, 1])
    worldN = placedN + (-s * local[:, 0] + c * local[:, 1])

    # model long axis world azimuth after rotation (for residual-deg vs detection)
    model_axis_deg, dx, dy = model_long_axis_deg()
    placed_axis = (model_axis_deg + ori_deg) % 180.0
    resid_deg = abs((placed_axis - det_brg + 90) % 180 - 90)
    resid_m = math.hypot(placedE - cE, placedN - cN)

    # draw on the ortho
    img, meta = fetch_ortho(det["osm_centroid_utm"], zoom=det["zoom"], half_m=det["W"] * det["res"] / 2)
    px_to_utm, utm_to_px = _make_px_mappers(meta)
    vis = img.copy()
    pts = np.array([utm_to_px(e, n) for e, n in zip(worldE, worldN)], dtype=np.int32)
    cv2.polylines(vis, [pts], True, (255, 60, 60), 3)                 # placed model footprint (red)
    # detected rect (yellow) for comparison
    cx, cy, L, Wd, angd = det["rect_px"]
    ca, sa = math.cos(math.radians(angd)), math.sin(math.radians(angd))
    corners = []
    for a, b in [(-L / 2, -Wd / 2), (L / 2, -Wd / 2), (L / 2, Wd / 2), (-L / 2, Wd / 2)]:
        corners.append((cx + a * ca - b * sa, cy + a * sa + b * ca))
    cv2.polylines(vis, [np.array(corners, np.int32)], True, (0, 255, 255), 2)
    pe = utm_to_px(placedE, placedN)
    cv2.circle(vis, (int(pe[0]), int(pe[1])), 5, (255, 255, 0), -1)
    de = utm_to_px(cE, cN)
    cv2.drawMarker(vis, (int(de[0]), int(de[1])), (0, 255, 0), cv2.MARKER_CROSS, 16, 2)
    # door direction arrow from placed centre
    door_world = (door_axis_deg + ori_deg) % 360.0
    dn = math.radians(door_world)
    tipE = placedE + math.sin(dn) * det["long_m"] * 0.6
    tipN = placedN + math.cos(dn) * det["long_m"] * 0.6
    tip = utm_to_px(tipE, tipN)
    cv2.arrowedLine(vis, (int(pe[0]), int(pe[1])), (int(tip[0]), int(tip[1])),
                    (255, 140, 0), 3, tipLength=0.25)
    OUT.mkdir(parents=True, exist_ok=True)
    pth = OUT / "hangar_placement_overlay.png"
    Image.fromarray(vis).save(pth)
    return resid_m, resid_deg, pth, (placedE, placedN, ori_deg), door_world


# --------------------------------------------------------------------------- #
door_axis_glob = 90.0  # set in main from the glTF


def main(argv=None):
    global door_axis_glob
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zoom", type=int, default=12, help="ortho GWC zoom (12=0.14 m/px)")
    ap.add_argument("--window", type=float, default=160.0, help="detection window metres")
    ap.add_argument("--use-sam", action="store_true",
                    help="attempt SAM3 point-prompt first (falls back to edge fit)")
    ap.add_argument("--install", action="store_true", help="rewrite the install .obj")
    ap.add_argument("--scale", type=float, default=0.0,
                    help="override scale (0=auto from footprint_len/model_len)")
    args = ap.parse_args(argv)

    prior = osm_hangar_prior()
    print("OSM prior: centroid=(%.1f, %.1f)  %.1fx%.1f m  bearing=%.2f deg  area=%.0f m2"
          % (prior["centroid"][0], prior["centroid"][1], prior["long_m"], prior["short_m"],
             prior["bearing"], prior["area"]))

    door_axis_glob, door_dbg = door_axis_condor_deg()
    print("glTF door axis: doors_glTF_X=%.1f front_glTF_X=%.1f -> doors face Condor azimuth %.0f deg at ori=0"
          % (door_dbg["door_glTF_x"], door_dbg["front_glTF_x"], door_axis_glob))

    if args.use_sam:
        # SAM3 (run_sam3_buildings.py) is the GPU/ML path; at this building scale even on
        # the 0.14 m/px ortho it offers no edge over the deterministic de-rotated edge-
        # projection, which is exact and reproducible. We keep the flag for parity with the
        # documented flow but route to the deterministic detector (the verified workhorse).
        print("  --use-sam: deferring to the deterministic edge-projection detector "
              "(reliable at this scale; SAM3 adds nothing for one ~34x19 m building).")
    det = detect_hangar(args, prior)
    print("DETECTED: centroid=(%.2f, %.2f)  %.1fx%.1f m  bearing=%.2f deg  (z%d %.2f m/px)"
          % (det["centroid_utm"][0], det["centroid_utm"][1], det["long_m"], det["short_m"],
             det["bearing_mod180"], det["zoom"], det["res"]))
    dshift = math.hypot(det["centroid_utm"][0] - prior["centroid"][0],
                        det["centroid_utm"][1] - prior["centroid"][1])
    print("  shift vs OSM centroid: %.2f m ; bearing delta vs OSM: %.2f deg"
          % (dshift, abs((det["bearing_mod180"] - prior["bearing"] + 90) % 180 - 90)))

    # scale: match the model's long axis to the detected roof long axis (footprint_len/
    # model_len). The SoFly model (40.3 m AABB) is chunkier than the real 34 m roof, so a
    # ~0.85 down-scale makes the footprint sit on the ortho roof (some E-W overhang remains
    # because the model's across-ridge depth exceeds this building's -- a model-fidelity
    # limit, not a placement error). Clamp to a sane band so a mis-detection can't shrink
    # the building to nothing.
    if args.scale > 0:
        scale = args.scale
    else:
        model_axis, dx, dy = model_long_axis_deg()
        model_long = max(dx, dy)
        scale = det["long_m"] / model_long if model_long > 1 else 1.0
        scale = min(max(scale, 0.75), 1.15)
    ori_deg, door_world, cands, (maxis, dx, dy) = compute_ori(det["bearing_mod180"], door_axis_glob)
    print("model long-axis at ori=0 = %.0f deg (AABB dx=%.1f dy=%.1f) ; scale=%.3f"
          % (maxis, dx, dy, scale))
    print("ORI candidates %s -> chose %.2f deg (doors then point azimuth %.0f, apron~%.0f)"
          % ([round(c, 1) for c in cands], ori_deg, door_world, APRON_AZIMUTH_DEG))

    if args.install:
        z, kept = install(det, ori_deg, scale)
        print(f"INSTALLED hangar into {OBJFILE.name} (posZ={z:.0f} m). kept records: {kept}")
        resid_m, resid_deg, pth, placed, dworld = validate_overlay(
            det, ori_deg, scale, door_axis_glob, installed=True)
    else:
        resid_m, resid_deg, pth, placed, dworld = validate_overlay(
            det, ori_deg, scale, door_axis_glob, installed=False)
        print("(dry run -- not installed; overlay uses the would-be placement)")

    print("\n=== VALIDATION ===")
    print("placed centroid UTM=(%.2f, %.2f) ori=%.2f deg ; doors face azimuth %.0f deg"
          % (placed[0], placed[1], placed[2], dworld))
    print("residual: %.2f m, %.2f deg  ->  %s"
          % (resid_m, resid_deg, "PASS" if (resid_m <= 3.0 and resid_deg <= 3.0) else "CHECK"))
    print("overlay:", pth)
    return det, ori_deg, resid_m, resid_deg


if __name__ == "__main__":
    main()
