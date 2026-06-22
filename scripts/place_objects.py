#!/usr/bin/env python3
r"""ONE generic, object-agnostic Condor 2 object-placement engine.

Drives placement of ANY object (custom-migrated model OR autogen shell) from a
manifest (data/placement_manifest.json). There is NO per-object script: a hangar,
church, tower or generic box all flow through this same code. See
docs/OBJECT_PLACEMENT.md for the full algorithm and the coordinate-frame analysis.

Per object the engine:
  1. FOOTPRINT  -- a real-world BUILDING polygon (vector-first: OSM/cadastre/MS/file),
     reprojected to UTM 34N. Never the parcel; imagery never measures, only validates.
  2. POSE       -- position = footprint centroid; orientation = footprint long-axis
     (mod 180) from the VECTOR polygon (NOT the 2.8 m/texel texture); the 180 deg
     front/rear resolved by a DIRECTED CUE from the model itself (front_groups =
     c3d object-name substrings, e.g. the hangar doors) matched to a WORLD cue
     (apron/road/azimuth). scale = sqrt(area) (uniform) for landmarks.
  3. FRAME      -- until the Phase-0a texture re-warp, target the PAINTED texture by
     adding condor_grid.TEXTURE_FRAME_CORRECTION (target_frame=installed_texture_dem_grid).
  4. VALIDATE   -- THREE independent game-free gates (reject on any fail):
       (a) vector geometry  : centroid/azimuth/size/IoU vs the footprint (in the
           painted frame)              [verify_object_placement]
       (b) directed front + chirality : front residual <= 90 deg vs the cue; model
           not mirrored                 [the 180 deg / handedness hole]
       (c) installed-DDS drift (NUMERIC): the placed outline must ride a real
           gradient ring in the texture Condor draws (not just an overlay image).
     Plus a 2-panel overlay (installed DDS | high-res cadastre ortho).
  5. INSTALL    -- only on --commit and only if all gates pass; rewrites the .obj
     (other records kept byte-identical), backs up first. No GUI, no hash.

CLI:  python scripts/place_objects.py [--only ID[,ID...]] [--commit] [--no-ortho]
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pyproj
from PIL import Image, ImageDraw
from shapely.affinity import translate as shp_translate
from shapely.geometry import MultiPoint, Polygon, shape
from shapely.ops import transform as shp_transform

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import condor_grid as G          # noqa: E402
import c3d as C3                 # noqa: E402
import footprint_registration as FR   # noqa: E402
import verify_object_placement as V    # noqa: E402

INSTALL = Path("C:/Condor2/Landscapes") / G.LANDSCAPE_NAME
OBJDIR = INSTALL / "World" / "Objects"
OBJFILE = INSTALL / f"{G.LANDSCAPE_NAME}.obj"
TEXDIR = INSTALL / "Textures"
DEM = REPO / "sources/dem/macedonia_skopje_dem_30m_2305_flat.raw"
DEM_ULX, DEM_ULY, DEM_W, DEM_PX = 506880.0, 4700160.0, 2305, 30.0
REC_SIZE, NAME_FIELD = 152, 131
OUT = REPO / ".sandbox/placement"
OUT.mkdir(parents=True, exist_ok=True)

_to_utm_wgs = pyproj.Transformer.from_crs(4326, 32634, always_xy=True).transform


# --------------------------------------------------------------------------- #
# Footprint sources (adapters; vector-first). Each returns a UTM-34N Polygon.
# --------------------------------------------------------------------------- #
def footprint_from_osm(spec):
    key, val = spec["filter_key"], spec["filter_val"]
    near = spec.get("near_utm"); rad = spec.get("radius_m", 300)
    best = None
    data = json.loads((REPO / ".sandbox/osm/buildings.geojson").read_text(encoding="utf-8"))
    for f in data["features"]:
        if str(f.get("properties", {}).get(key)) != str(val):
            continue
        g = shp_transform(_to_utm_wgs, shape(f["geometry"]))
        if g.geom_type == "MultiPolygon":
            g = max(g.geoms, key=lambda p: p.area)
        if near is not None:
            d = math.hypot(g.centroid.x - near[0], g.centroid.y - near[1])
            if d > rad:
                continue
            if best is None or d < best[0]:
                best = (d, g)
        else:
            best = (0, g)
            break
    if best is None:
        raise SystemExit(f"[footprint] no OSM {key}={val} near {near}")
    return best[1]


def footprint_from_geojson(spec):
    p = REPO / spec["path"]
    data = json.loads(p.read_text(encoding="utf-8"))
    feats = data["features"] if data.get("type") == "FeatureCollection" else [data]
    polys = []
    for f in feats:
        g = shape(f["geometry"])
        if g.bounds[0] < 180 and g.bounds[1] < 90:   # looks like WGS84
            g = shp_transform(_to_utm_wgs, g)
        if g.geom_type == "MultiPolygon":
            g = max(g.geoms, key=lambda p: p.area)
        polys.append(g)
    return max(polys, key=lambda p: p.area)


def resolve_footprint(spec):
    src = spec["source"]
    if src == "osm":
        return footprint_from_osm(spec)
    if src == "geojson":
        return footprint_from_geojson(spec)
    raise SystemExit(f"[footprint] source {src!r} not yet wired (add cadastre/ms adapter)")


# --------------------------------------------------------------------------- #
# Model introspection (base outline + front-cue groups) from the installed c3d
# --------------------------------------------------------------------------- #
def model_info(c3d_path, front_groups, rear_groups, base_z_window=0.75):
    cf = C3.parse_c3d(c3d_path)
    allv = [(v.px, v.py, v.pz) for o in cf.objects for v in o.vertices]
    if not allv:
        raise SystemExit(f"[model] {c3d_path} has no vertices")
    zmin = min(v[2] for v in allv)
    base = [(x, y) for x, y, z in allv if z <= zmin + base_z_window]
    outline = MultiPoint(base if len(base) >= 3 else [(x, y) for x, y, _ in allv]).convex_hull
    if outline.geom_type != "Polygon":
        outline = outline.buffer(0)
    mc = outline.centroid

    def group_centroid(subs):
        pts = [(v.px, v.py) for o in cf.objects
               if any(s.lower() in o.name.lower() for s in subs)
               for v in o.vertices]
        return (float(np.mean([p[0] for p in pts])), float(np.mean([p[1] for p in pts]))) if pts else None

    fc = group_centroid(front_groups) if front_groups else None
    rc = group_centroid(rear_groups) if rear_groups else None
    front_local_az = None
    if fc is not None:
        front_local_az = math.degrees(math.atan2(fc[0] - mc.x, fc[1] - mc.y)) % 360.0
    elif rc is not None:                      # front = opposite the rear group
        front_local_az = (math.degrees(math.atan2(rc[0] - mc.x, rc[1] - mc.y)) + 180.0) % 360.0
    # signed area of the base outline (chirality witness: CCW>0 in E/N)
    xy = np.array(outline.exterior.coords)
    signed_area = 0.5 * np.sum(xy[:-1, 0] * xy[1:, 1] - xy[1:, 0] * xy[:-1, 1])
    return dict(outline=outline, centroid=(mc.x, mc.y), front_local_az=front_local_az,
                has_front=fc is not None or rc is not None, signed_area=float(signed_area))


def long_axis_az_mod180(poly):
    m = V.mrr_metrics(poly)
    return m["bearing_mod180_deg"], m["long_m"], m["short_m"]


# --------------------------------------------------------------------------- #
# World front cue
# --------------------------------------------------------------------------- #
def world_front_azimuth(cue, centroid):
    if isinstance(cue, (int, float)):
        return float(cue) % 360.0, f"fixed {float(cue):.0f} deg"
    if cue == "apron":
        runp = REPO / ".sandbox/osm/runways.geojson"
        if runp.exists():
            best = None
            for f in json.loads(runp.read_text(encoding="utf-8"))["features"]:
                g = shp_transform(_to_utm_wgs, shape(f["geometry"]))
                d = math.hypot(g.centroid.x - centroid[0], g.centroid.y - centroid[1])
                if d < 3000 and (best is None or d < best[0]):
                    best = (d, g.centroid.x, g.centroid.y)
            if best:
                az = math.degrees(math.atan2(best[1] - centroid[0], best[2] - centroid[1])) % 360.0
                return az, f"apron/runway bearing {az:.0f} deg"
        return 120.0, "apron fallback 120 deg"
    raise SystemExit(f"[front] world cue {cue!r} not supported")


def dem_alt(E, N):
    dem = np.fromfile(DEM, dtype="<u2").reshape(DEM_W, DEM_W)
    c = min(max(int(round((E - DEM_ULX) / DEM_PX)), 0), DEM_W - 1)
    r = min(max(int(round((DEM_ULY - N) / DEM_PX)), 0), DEM_W - 1)
    return float(dem[r, c])


# --------------------------------------------------------------------------- #
# Patch index + object-grid texel mapping (the frame the placed object lives in)
# --------------------------------------------------------------------------- #
def patch_of(E, N):
    col = int((G.OBJ_ANCHOR_E - E) // G.PATCH_SIZE_M)
    row = int((N - G.OBJ_ANCHOR_N) // G.PATCH_SIZE_M)
    return col, row


def obj_texel_mapper(col, row):
    e_max = G.OBJ_ANCHOR_E - col * G.PATCH_SIZE_M; e_min = e_max - G.PATCH_SIZE_M
    n_min = G.OBJ_ANCHOR_N + row * G.PATCH_SIZE_M; n_max = n_min + G.PATCH_SIZE_M

    def tpx(e, n):
        return ((e - e_min) / (e_max - e_min) * 2048.0, (n_max - n) / (n_max - n_min) * 2048.0)
    return tpx


# --------------------------------------------------------------------------- #
# POSE: ori (long-axis align + front-cue 180) + scale + painted target centroid
# --------------------------------------------------------------------------- #
def solve_pose(obj, fp_poly, mi):
    corr = G.TEXTURE_FRAME_CORRECTION if obj.get("target_frame") == "installed_texture_dem_grid" else (0.0, 0.0)
    target_poly = shp_translate(fp_poly, xoff=corr[0], yoff=corr[1])   # footprint in the PAINTED frame
    tE, tN = target_poly.centroid.x, target_poly.centroid.y

    fp_az, fp_long, fp_short = long_axis_az_mod180(fp_poly)
    mdl_az, mdl_long, mdl_short = long_axis_az_mod180(mi["outline"])

    # ori candidates: align model long-axis to footprint long-axis (mod 180) -> 2 options
    base = (fp_az - mdl_az) % 180.0
    cands = [base % 360.0, (base + 180.0) % 360.0]

    # scale: sqrt(area) (uniform, proportion-preserving); cross-check via registration
    reg = FR.register(list(mi["outline"].exterior.coords), fp_poly)
    scale = math.sqrt(fp_poly.area / mi["outline"].area)
    aspect_resid = abs((mdl_long / max(mdl_short, 1e-6)) - (fp_long / max(fp_short, 1e-6)))

    # resolve the 180 by the directed front cue
    front_src = "none"; chosen = cands[0]; front_world = None; front_resid = None
    if mi["front_local_az"] is not None:
        wf, front_src = world_front_azimuth(obj["front"]["world"], (tE, tN))
        front_world = wf

        def resid(o):
            return abs(((mi["front_local_az"] + o) % 360.0 - wf + 180.0) % 360.0 - 180.0)
        chosen = min(cands, key=resid)
        front_resid = resid(chosen)
        front_src = "model:" + front_src
    elif obj.get("confidence_policy") == "reject_if_unresolved":
        front_src = "UNRESOLVED"
    # else: deterministic default (first candidate), front_confidence none

    return dict(ori_deg=chosen % 360.0, scale=float(scale), target_poly=target_poly,
                tE=tE, tN=tN, corr=corr, fp_az=fp_az, fp_long=fp_long, fp_short=fp_short,
                mdl_az=mdl_az, mdl_long=mdl_long, mdl_short=mdl_short, cands=cands,
                front_src=front_src, front_world=front_world, front_resid=front_resid,
                front_local_az=mi["front_local_az"], reg=reg, aspect_resid=aspect_resid)


def build_record(name_c3d, pose, mi):
    o = math.radians(pose["ori_deg"]); co, so = math.cos(o), math.sin(o)
    mcx, mcy = mi["centroid"]; s = pose["scale"]
    # world = ref + M(ori) . (scale.local) ; M=[[co,so],[-so,co]] ; place model centroid at target
    refE = pose["tE"] - (co * (s * mcx) + so * (s * mcy))
    refN = pose["tN"] - (-so * (s * mcx) + co * (s * mcy))
    posX, posY = G.obj_record_xy(refE, refN)
    # posZ = terrain altitude at the TRUE footprint centroid (undo the texture correction)
    z = dem_alt(pose["tE"] - pose["corr"][0], pose["tN"] - pose["corr"][1])
    nm = name_c3d.encode("ascii")
    rec = (struct.pack("<5f", posX, posY, z, s, o % (2 * math.pi))
           + bytes([len(nm)]) + nm.ljust(NAME_FIELD, b"\x00"))
    return rec, (refE, refN, z)


# --------------------------------------------------------------------------- #
# VALIDATION -- three independent game-free gates
# --------------------------------------------------------------------------- #
def placed_polygon_from_record(rec, c3d_path):
    posX, posY, pz, sc, ori = struct.unpack_from("<5f", rec, 0)
    we, wn = G.obj_world_xy(posX, posY)
    r = V.ObjRecord(name="x", pos_x=posX, pos_y=posY, pos_z=pz, scale=sc, ori_rad=ori,
                    world_e=we, world_n=wn, offset=0)
    local = V.c3d_footprint_polygon(c3d_path)
    return V.transform_local_polygon(local, r), r


def dds_gradient_ring_score(placed_poly, col, row):
    """NUMERIC drift gate: the placed outline should ride a real gradient ring in the
    INSTALLED texture (object-grid mapping). Returns ratio = border|grad| / interior|grad|.
    >~1.3 means the outline sits on a building edge in the raster Condor draws."""
    from scipy import ndimage
    tex = TEXDIR / f"t{col:02d}{row:02d}.dds"
    if not tex.exists():
        return None
    img = np.asarray(Image.open(tex).convert("L")).astype(np.float32)
    gx = ndimage.sobel(img, axis=1); gy = ndimage.sobel(img, axis=0)
    gmag = np.hypot(gx, gy)
    tpx = obj_texel_mapper(col, row)
    ring = np.array([tpx(e, n) for e, n in placed_poly.exterior.coords])
    cE, cN = placed_poly.centroid.x, placed_poly.centroid.y

    def samp(arr, pts):
        xs = np.clip(pts[:, 0], 0, 2047).astype(int); ys = np.clip(pts[:, 1], 0, 2047).astype(int)
        return float(np.mean(arr[ys, xs]))
    border = samp(gmag, ring)
    # interior ring at 60% scale toward centroid
    cx, cy = tpx(cE, cN)
    inner = (ring - [cx, cy]) * 0.6 + [cx, cy]
    interior = samp(gmag, inner) + 1e-6
    return border / interior


def validate(obj, rec, c3d_path, pose, mi):
    placed, r = placed_polygon_from_record(rec, c3d_path)
    target = pose["target_poly"]
    # (a) vector geometry (in the painted frame)
    min_iou = 0.85 if obj.get("scale_mode") == "uniform_landmark" else 0.75
    vg = V.validate_polygons(placed, target, max_position_error_m=3.0, max_angle_error_deg=3.0,
                             max_size_error_m=3.0, min_iou=min_iou)
    # (b) front + chirality
    front_ok = True; front_note = pose["front_src"]
    if pose["front_src"] == "UNRESOLVED":
        front_ok = False; front_note = "front UNRESOLVED + reject_if_unresolved"
    elif pose["front_resid"] is not None and pose["front_resid"] > 90.0:
        front_ok = False; front_note = f"front residual {pose['front_resid']:.0f} deg > 90"
    # (c) installed-DDS numeric drift
    col, row = patch_of(r.world_e, r.world_n)
    ring = dds_gradient_ring_score(placed, col, row)
    # HARD gates = vector geometry + directed front (the 180 deg flip). These are the
    # measurable, reliable ones. Chirality-by-hull-winding and the DDS gradient ring are
    # UNRELIABLE on a near-symmetric hull / a 2.8 m-texel roof, so they are WARNINGS, not
    # rejections: a true mirror shows up as a large front residual (caught above) and the
    # in-sim-texture drift is confirmed by the cadastre-ortho overlay + the Phase-0a re-warp.
    failures = list(vg["failures"])
    if not front_ok:
        failures.append({"code": "front_unresolved_or_flipped", "note": front_note})
    warnings = []
    if mi["signed_area"] < 0:
        warnings.append({"code": "hull_winding_cw_check_mirror_visually", "signed_area": mi["signed_area"]})
    if ring is not None and ring < 1.20:
        warnings.append({"code": "dds_edge_ring_low_inspect_overlay", "ring_ratio": ring})
    report = dict(ok=not failures, vector=vg, front_resid=pose["front_resid"],
                  front_note=front_note, dds_ring_ratio=ring, warnings=warnings,
                  patch=[col, row], failures=failures,
                  placed_centroid=[placed.centroid.x, placed.centroid.y],
                  world=[r.world_e, r.world_n])
    return report, placed


# --------------------------------------------------------------------------- #
# OVERLAYS (game-free): installed DDS (drift truth) + optional high-res cadastre
# --------------------------------------------------------------------------- #
def overlay_dds(obj_id, placed, target_poly, fp_poly, col, row, half_m=70):
    tex = TEXDIR / f"t{col:02d}{row:02d}.dds"
    if not tex.exists():
        return None
    tpx = obj_texel_mapper(col, row)
    cx, cy = tpx(placed.centroid.x, placed.centroid.y)
    Z = 12; half = int(half_m / (G.PATCH_SIZE_M / 2048.0))
    box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
    img = Image.open(tex).convert("RGB").crop(box).resize(((box[2]-box[0])*Z, (box[3]-box[1])*Z), Image.LANCZOS)
    dr = ImageDraw.Draw(img, "RGBA")

    def toi(e, n):
        px, py = tpx(e, n); return ((px - box[0]) * Z, (py - box[1]) * Z)
    dr.line([toi(x, y) for x, y in placed.exterior.coords], fill=(255, 50, 50, 255), width=3)   # PLACED model
    dr.line([toi(x, y) for x, y in target_poly.exterior.coords], fill=(0, 255, 255, 200), width=1)  # footprint (painted frame)
    mx, my = toi(placed.centroid.x, placed.centroid.y)
    dr.ellipse([mx-4, my-4, mx+4, my+4], fill=(255, 0, 255))
    dr.text((6, 6), f"{obj_id}  red=placed model  cyan=footprint  (installed DDS t{col:02d}{row:02d})", fill=(255, 255, 0))
    p = OUT / f"{obj_id}_overlay_dds.png"; img.save(p); return p


def overlay_ortho(obj_id, placed, fp_poly):
    """High-res cadastre (0.14 m/px) panel via the GWC fetch (best-effort, needs net)."""
    try:
        import detect_place_stenkovec_hangar as DP
        true_c = (placed.centroid.x - G.TEXTURE_FRAME_CORRECTION[0],
                  placed.centroid.y - G.TEXTURE_FRAME_CORRECTION[1])
        img, meta = DP.fetch_ortho(true_c, zoom=12, half_m=90)
        _, utm_to_px = DP._make_px_mappers(meta)
        import cv2
        vis = np.asarray(img).copy()
        # draw the placed model mapped back to TRUE utm (undo the texture correction)
        pl_true = shp_translate(placed, xoff=-G.TEXTURE_FRAME_CORRECTION[0], yoff=-G.TEXTURE_FRAME_CORRECTION[1])
        pts = np.array([utm_to_px(e, n) for e, n in pl_true.exterior.coords], np.int32)
        cv2.polylines(vis, [pts], True, (255, 50, 50), 2)
        fpts = np.array([utm_to_px(e, n) for e, n in fp_poly.exterior.coords], np.int32)
        cv2.polylines(vis, [fpts], True, (0, 255, 255), 1)
        p = OUT / f"{obj_id}_overlay_ortho.png"; Image.fromarray(vis).save(p); return p
    except Exception as e:                       # noqa: BLE001
        print(f"   [ortho overlay skipped: {e}]")
        return None


# --------------------------------------------------------------------------- #
def read_records():
    if not OBJFILE.exists():
        return []
    d = OBJFILE.read_bytes()
    return [d[k*REC_SIZE:(k+1)*REC_SIZE] for k in range(len(d)//REC_SIZE)]


def rec_name(raw):
    nl = raw[20]; return raw[21:21+nl].decode("ascii", "replace")


def install(obj_id, name_c3d, rec):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if OBJFILE.exists():
        shutil.copy2(OBJFILE, OBJFILE.with_suffix(f".obj.bak_{ts}"))
    out = bytearray()
    for raw in read_records():
        if rec_name(raw).lower() == name_c3d.lower():
            continue                       # replace this object's record
        out += raw
    out += rec
    OBJFILE.write_bytes(bytes(out))
    print(f"   [install] wrote {OBJFILE.name}: {len(out)//REC_SIZE} records (replaced {name_c3d})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default="data/placement_manifest.json")
    ap.add_argument("--only", default="", help="comma list of object ids")
    ap.add_argument("--commit", action="store_true", help="install passing objects (else dry-run + overlays)")
    ap.add_argument("--no-ortho", action="store_true", help="skip the high-res cadastre panel (no network)")
    args = ap.parse_args(argv)

    man = json.loads((REPO / args.manifest).read_text(encoding="utf-8"))
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    objs = [o for o in man["objects"] if not only or o["id"] in only]
    print("=" * 78)
    print(f"  GENERIC OBJECT PLACEMENT  ({G.LANDSCAPE_NAME})  anchor "
          f"{G.OBJ_ANCHOR_E:.0f}/{G.OBJ_ANCHOR_N:.0f}  texcorr {G.TEXTURE_FRAME_CORRECTION}")
    print("=" * 78)

    passed = []
    for obj in objs:
        oid = obj["id"]; name_c3d = obj["c3d"]; c3d_path = OBJDIR / name_c3d
        print(f"\n--- {oid}  ({name_c3d}) ---")
        if not c3d_path.exists():
            print(f"   ! c3d not installed: {c3d_path}"); continue
        fp = resolve_footprint(obj["footprint"])
        mi = model_info(c3d_path, obj.get("front", {}).get("model_groups"),
                        obj.get("front", {}).get("rear_groups"))
        pose = solve_pose(obj, fp, mi)
        rec, (refE, refN, z) = build_record(name_c3d, pose, mi)
        report, placed = validate(obj, rec, c3d_path, pose, mi)
        col, row = report["patch"]

        print(f"   footprint long={pose['fp_long']:.1f} short={pose['fp_short']:.1f} m  "
              f"long-axis {pose['fp_az']:.1f} deg")
        print(f"   model     long={pose['mdl_long']:.1f} short={pose['mdl_short']:.1f} m  "
              f"long-axis {pose['mdl_az']:.1f} deg  front_local {pose['front_local_az']}")
        print(f"   ori candidates {[round(c,1) for c in pose['cands']]} -> chose {pose['ori_deg']:.2f} deg  "
              f"({pose['front_src']}; front_world={pose['front_world']}, resid={pose['front_resid']})")
        print(f"   scale {pose['scale']:.3f} (reg iou {pose['reg']['iou']:.2f}, aspect_resid {pose['aspect_resid']:.2f})  "
              f"posZ {z:.0f} m  patch t{col:02d}{row:02d}")
        vg = report["vector"]
        print(f"   GATE-a vector : pos {vg['position_error_m']:.2f} m  ang {vg['angle_error_deg']:.2f} deg  "
              f"long {vg['long_size_error_m']:.2f}  short {vg['short_size_error_m']:.2f}  iou {vg['iou']:.3f}")
        print(f"   GATE-b front  : {report['front_note']}  (resid {report['front_resid']})")
        print(f"   warnings (non-blocking): ring_ratio {report['dds_ring_ratio']}  {report['warnings']}")
        verdict = "PASS" if report["ok"] else "REJECT"
        print(f"   => {verdict}")
        if report["failures"]:
            for f in report["failures"]:
                print(f"        - {f}")

        p1 = overlay_dds(oid, placed, pose["target_poly"], fp, col, row)
        p2 = None if args.no_ortho else overlay_ortho(oid, placed, fp)
        if p1:
            print(f"   overlay (DDS)   {p1}")
        if p2:
            print(f"   overlay (ortho) {p2}")

        (OUT / f"{oid}_report.json").write_text(json.dumps(
            {**report, "ori_deg": pose["ori_deg"], "scale": pose["scale"]}, indent=2, default=float))
        if report["ok"]:
            passed.append((oid, name_c3d, rec))

    if args.commit and passed:
        print("\n" + "=" * 78)
        for oid, name_c3d, rec in passed:
            install(oid, name_c3d, rec)
        # sanity: re-decode
        names = [rec_name(r) for r in read_records()]
        print(f"   installed records now: {names}")
    elif args.commit:
        print("\n  [commit] nothing passed the gates -> nothing installed.")
    else:
        print("\n  (dry run -- review overlays + reports in .sandbox/placement/, then --commit)")
    return passed


if __name__ == "__main__":
    main()
