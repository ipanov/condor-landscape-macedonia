#!/usr/bin/env python3
r"""
High-voltage transmission-pylon autogen for MacedoniaSkopje (12x12) — deterministic.

SANDBOX ONLY: writes .sandbox/osm/power.json (cache) + .sandbox/pylons/*; reads the
installed textures (C:/Condor2/.../Textures) for QA overlays only, never writes there.

Pipeline
  1. Fetch OSM power=line ways (`out geom`: ordered node refs + voltage tags) and
     power=tower nodes over the landscape WGS84 bbox; cache the raw Overpass JSON at
     .sandbox/osm/power.json (osm_io mirror/User-Agent/retry conventions).
     power=minor_line is never queried; line=busbar/bay stubs are skipped.
  2. Keep towers that are vertices of lines with max(voltage) >= 110 kV
     (semicolon lists -> max part; missing/unparseable voltage -> line dropped).
  3. Per tower record:
       E, N     TRUE UTM 34N (EPSG:32634) of the OSM node. No texture-frame
                correction is baked in — the placer owns frame policy.
       ori_deg  compass azimuth (deg, 0-360) of the LINE at the tower: unit-vector
                mean of the two adjacent segment directions (corridor way-breaks are
                stitched across ways; true corridor ends use the single segment).

                ORI CONVENTION (deliberate, verified): ori_deg is the Condor .obj
                `ori` in degrees under the AUTHORITATIVE condor_grid convention —
                `ori` aims model local +Y; local +X renders at azimuth ori+90
                (condor_grid.py "AUTHORITATIVE" block; make_record precedent does
                ori = radians(ori_deg)). pylon.c3d carries its 18 m cross-arms along
                local +X, so ori_deg = line azimuth  =>  arms at line_az+90 =
                PERPENDICULAR to the wires — the physically correct pose.
                The task brief's literal "ori = line_azimuth + 90" presumed `ori`
                aims the +X arm axis; pushed through the verified +Y convention it
                would swing every pylon's arms PARALLEL to its own wires (90 deg
                error on all instances), so the stated perpendicularity REQUIREMENT
                wins. Each record also ships arm_az_deg = (ori_deg + 90) % 360 so a
                consumer with the other convention can flip trivially.
       scale    1.0 for >= 220 kV, 0.75 for the 110 kV class (per brief).
  4. Clip to the landscape (intersection of the texture/DEM grid and the
     object/.trn grid), then budget: while kept > 900, thin keep-every-2nd per line
     — but towers within 8 km of Stenkovec or inside the Vardar valley core box are
     NEVER thinned (near-field fidelity).
  5. Emit .sandbox/pylons/placements.json + report.json (sorted by osmid, fixed
     rounding -> byte-identical reruns) and QA images under .sandbox/pylons/qa/:
     overview.png (all towers + line polylines over the landscape extent, Agg, no
     window) and two texture-overlay closeups on 400 kV corridors using the SAME
     tCCRR.dds georef as scripts/validate_bridge.py (true-UTM -> DEM-grid patch px,
     verified against painted roads).

Run:  python scripts/generate_pylons.py                       full build
      python scripts/generate_pylons.py --closeup pylon_<id> [...]   extra QA crops
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent))
import osm_io  # noqa: E402  (Overpass mirrors + User-Agent + retry conventions)

ROOT = Path(__file__).resolve().parent.parent
OSM_CACHE = ROOT / ".sandbox" / "osm" / "power.json"
OUT_DIR = ROOT / ".sandbox" / "pylons"
QA_DIR = OUT_DIR / "qa"
INSTALL_TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")  # READ-ONLY

# Landscape WGS84 bbox (S, W, N, E) — the 12x12 grid's geographic bounds.
BBOX = (41.8276, 21.0829, 42.4537, 21.9242)

MIN_V = 110_000            # keep lines with max(voltage) >= this
V_220 = 220_000            # >= this -> scale 1.0, below (i.e. 110 kV class) -> 0.75
BUDGET = 900

# No-thin protection: near-field fidelity zones.
STENKOVEC_EN = (531842.0, 4656466.0)
STENKOVEC_R_M = 8000.0
VARDAR_BOX = (528000.0, 4645000.0, 545000.0, 4656000.0)     # E0, N0, E1, N1

# Landscape clip = intersection of the texture/DEM grid (506880..576000 x
# 4631040..4700160) and the object/.trn grid (506835..575955 x 4631085..4700205),
# so every record is on-mesh AND on-texture.
CLIP = (506880.0, 4631085.0, 575955.0, 4700160.0)            # E0, N0, E1, N1

# Texture-patch georef — IDENTICAL to scripts/validate_bridge.py /
# footprints_to_obj.py (VERIFIED: true-UTM OSM roads align with painted roads).
TEX_ULX_W = 506880.0        # west edge of col 0 (cols counted from WEST here)
TEX_SOUTH0 = 4631040.0      # south edge of row 0
PATCH_M = 5760.0
PATCH_PX = 2048             # north-up DDS
NCOL = 12                   # filename t{11-col:02d}{row:02d}.dds

# The two shipped QA closeups (chosen by visual inspection of candidate crops):
#   pylon_6759804583 — 400 kV line "423" in open NE hills; the tower's dark
#                      base/shadow blob sits inside the 10 m circle and further
#                      dark tower specks are colinear along the drawn line.
#   pylon_2703275140 — 400 kV crossing dark NW forest; visible clear-cut wayleave
#                      seam tracks the line, tower node in the clearing notch.
# Deterministic fallback if absent: first kept >=380 kV records by osmid.
CLOSEUP_TOWERS = ["pylon_6759804583", "pylon_2703275140"]

_QUERY = f"""[out:json][timeout:300][bbox:{BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]}];
(
  way["power"="line"];
  node["power"="tower"];
);
out geom;"""

_TO_UTM = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)


# --------------------------------------------------------------------------- #
# Fetch (cached raw Overpass JSON, osm_io conventions)
# --------------------------------------------------------------------------- #
def fetch_power() -> dict:
    if OSM_CACHE.exists():
        with open(OSM_CACHE, encoding="utf-8") as f:
            return json.load(f)
    import requests
    last_exc = None
    retries = 6
    for attempt in range(retries):
        endpoint = osm_io.OVERPASS_MIRRORS[attempt % len(osm_io.OVERPASS_MIRRORS)]
        url = endpoint + "?data=" + urllib.parse.quote(_QUERY)
        try:
            resp = requests.get(url, headers={"User-Agent": osm_io.USER_AGENT},
                                timeout=600)
            resp.raise_for_status()
            data = resp.json()
            OSM_CACHE.parent.mkdir(parents=True, exist_ok=True)
            with open(OSM_CACHE, "w", encoding="utf-8") as f:
                json.dump(data, f)
            print(f"Saved {OSM_CACHE} ({len(data.get('elements', []))} elements)")
            return data
        except Exception as exc:                                  # noqa: BLE001
            last_exc = exc
            host = endpoint.split("//")[1].split("/")[0]
            print(f"Overpass power @ {host} failed "
                  f"(attempt {attempt + 1}/{retries}): {exc}")
            if attempt < retries - 1:
                time.sleep(min(30, 3 * (attempt + 1)))
    raise last_exc


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_voltage(tag) -> int | None:
    """OSM voltage tag (volts; semicolon list) -> max parseable value, else None."""
    if tag in (None, ""):
        return None
    best = None
    for part in str(tag).split(";"):
        part = part.strip().replace(",", ".")
        try:
            v = float(part)
        except ValueError:
            continue
        if best is None or v > best:
            best = v
    return int(best) if best is not None else None


def load_elements(data):
    """Raw Overpass JSON -> (towers {nid: (lon,lat)}, qualifying ways, stats)."""
    towers: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []
    n_line_ways = 0
    n_no_voltage = 0
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        if el.get("type") == "node":
            if tags.get("power") == "tower" and "lat" in el and "lon" in el:
                towers[el["id"]] = (el["lon"], el["lat"])
        elif el.get("type") == "way":
            if tags.get("power") != "line":
                continue
            n_line_ways += 1
            if tags.get("line") in ("busbar", "bay"):      # substation stubs
                continue
            v = parse_voltage(tags.get("voltage"))
            if v is None:
                n_no_voltage += 1
                continue
            if v < MIN_V:
                continue
            geom = el.get("geometry") or []
            if len(geom) < 2:
                continue
            nodes = el.get("nodes") or []
            pts = [(g["lon"], g["lat"]) for g in geom]
            if len(nodes) != len(pts):
                nodes = [None] * len(pts)                  # repaired below by coord
            ways.append({
                "id": el["id"],
                "v": v,
                "ref": tags.get("ref") or tags.get("name") or f"way/{el['id']}",
                "nodes": nodes,
                "pts_utm": [_TO_UTM.transform(lon, lat) for (lon, lat) in pts],
                "pts_ll": pts,
            })
    ways.sort(key=lambda w: w["id"])
    # Repair node-ref alignment by exact coordinate match if Overpass ever returns
    # mismatched nodes/geometry lengths (defensive; normal `out geom` matches).
    coord_to_nid = {(round(lon, 7), round(lat, 7)): nid
                    for nid, (lon, lat) in towers.items()}
    for w in ways:
        if w["nodes"] and w["nodes"][0] is None:
            w["nodes"] = [coord_to_nid.get((round(lon, 7), round(lat, 7)))
                          for (lon, lat) in w["pts_ll"]]
    return towers, ways, {"line_ways": n_line_ways, "no_voltage": n_no_voltage}


# --------------------------------------------------------------------------- #
# Orientation: mean azimuth of the two adjacent line segments at the tower
# --------------------------------------------------------------------------- #
def _unit(dx, dy):
    L = math.hypot(dx, dy)
    return (dx / L, dy / L) if L > 1e-9 else None


def tower_azimuth(way, idx, occurrences):
    """Compass azimuth (deg, 0-360) of the line at vertex `idx` of `way`.

    Interior vertex: unit-vector mean of the incoming and outgoing segment
    directions. Way endpoint: stitch the corridor continuation from the OTHER
    qualifying ways sharing this node (highest voltage, then lowest way id, is a
    deterministic pick); a true corridor end uses its single segment.
    """
    pts = way["pts_utm"]
    t = pts[idx]
    prev_pt = pts[idx - 1] if idx > 0 else None
    next_pt = pts[idx + 1] if idx < len(pts) - 1 else None
    if prev_pt is None or next_pt is None:
        cont = []
        for (w2, i2) in occurrences:
            if w2 is way and i2 == idx:
                continue
            p2 = w2["pts_utm"]
            for j in (i2 - 1, i2 + 1):
                if 0 <= j < len(p2):
                    cont.append((-w2["v"], w2["id"], j, p2[j]))
        cont.sort(key=lambda c: (c[0], c[1], c[2]))
        if cont:
            if prev_pt is None:
                prev_pt = cont[0][3]
            else:
                next_pt = cont[0][3]
    d_in = _unit(t[0] - prev_pt[0], t[1] - prev_pt[1]) if prev_pt else None
    d_out = _unit(next_pt[0] - t[0], next_pt[1] - t[1]) if next_pt else None
    if d_in and d_out:
        mx, my = d_in[0] + d_out[0], d_in[1] + d_out[1]
        if math.hypot(mx, my) < 1e-9:                      # degenerate U-turn
            mx, my = d_out
    elif d_in:
        mx, my = d_in
    elif d_out:
        mx, my = d_out
    else:
        return None
    return math.degrees(math.atan2(mx, my)) % 360.0


# --------------------------------------------------------------------------- #
# Tower records
# --------------------------------------------------------------------------- #
def is_protected(E, N) -> bool:
    if math.hypot(E - STENKOVEC_EN[0], N - STENKOVEC_EN[1]) <= STENKOVEC_R_M:
        return True
    e0, n0, e1, n1 = VARDAR_BOX
    return e0 <= E <= e1 and n0 <= N <= n1


def build_records(towers, ways):
    """One record per tower node that sits on >=1 qualifying line."""
    occ: dict[int, list] = {}                  # nid -> [(way, idx), ...]
    for w in ways:
        seen = set()
        for i, nid in enumerate(w["nodes"]):
            if nid is None or nid not in towers or nid in seen:
                continue
            seen.add(nid)
            occ.setdefault(nid, []).append((w, i))

    records = []
    n_unoriented = 0
    for nid in sorted(occ):
        lst = sorted(occ[nid], key=lambda o: (-o[0]["v"], o[0]["id"], o[1]))
        way, idx = lst[0]                                    # primary line
        az = tower_azimuth(way, idx, lst)
        if az is None:
            n_unoriented += 1
            continue
        E, N = _TO_UTM.transform(*towers[nid])
        v = max(o[0]["v"] for o in lst)
        records.append({
            "name": f"pylon_{nid}",
            "c3d": "pylon.c3d",
            "E": round(E, 2),
            "N": round(N, 2),
            "ori_deg": round(az, 2),
            "scale": 1.0 if v >= V_220 else 0.75,
            "voltage": int(v),
            "line_ref": way["ref"],
            "arm_az_deg": round((az + 90.0) % 360.0, 2),
            "_way": way["id"],
            "_idx": idx,
            "_prot": is_protected(E, N),
        })
    return records, n_unoriented


def clip_records(records):
    e0, n0, e1, n1 = CLIP
    return [r for r in records if e0 <= r["E"] <= e1 and n0 <= r["N"] <= n1]


def thin_to_budget(records):
    """While > BUDGET: per line (sorted way id, towers in line order), drop every
    2nd NON-protected tower. Returns (kept, dropped, rounds)."""
    kept = list(records)
    dropped = []
    rounds = 0
    while len(kept) > BUDGET:
        by_way: dict[int, list] = {}
        for r in kept:
            by_way.setdefault(r["_way"], []).append(r)
        removed = set()
        for wid in sorted(by_way):
            seq = sorted(by_way[wid], key=lambda r: r["_idx"])
            thinnable = [r for r in seq if not r["_prot"]]
            for r in thinnable[1::2]:
                removed.add(r["name"])
        if not removed:
            print(f"WARNING: cannot thin below {len(kept)} "
                  f"(all remaining towers protected)")
            break
        dropped.extend(r for r in kept if r["name"] in removed)
        kept = [r for r in kept if r["name"] not in removed]
        rounds += 1
    return kept, dropped, rounds


def vclass(v: int) -> str:
    return "400kV" if v >= 380_000 else ("220kV" if v >= V_220 else "110kV")


# --------------------------------------------------------------------------- #
# QA: overview map (matplotlib Agg) + texture-overlay closeups (PIL)
# --------------------------------------------------------------------------- #
_CLS_COLOR = {"400kV": "#d62728", "220kV": "#ff7f0e", "110kV": "#1f77b4"}


def qa_overview(kept, dropped, ways, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Rectangle

    fig, ax = plt.subplots(figsize=(11, 11))
    for w in ways:
        xs = [p[0] for p in w["pts_utm"]]
        ys = [p[1] for p in w["pts_utm"]]
        ax.plot(xs, ys, color="0.65", lw=0.6, zorder=1)
    if dropped:
        ax.scatter([r["E"] for r in dropped], [r["N"] for r in dropped],
                   s=3, c="0.8", marker="x", zorder=2, label=f"thinned ({len(dropped)})")
    for cls in ("110kV", "220kV", "400kV"):
        sel = [r for r in kept if vclass(r["voltage"]) == cls]
        if sel:
            ax.scatter([r["E"] for r in sel], [r["N"] for r in sel], s=6,
                       c=_CLS_COLOR[cls], zorder=3, label=f"{cls} kept ({len(sel)})")
    # landscape extent (texture/DEM grid)
    ax.add_patch(Rectangle((TEX_ULX_W, TEX_SOUTH0), NCOL * PATCH_M, NCOL * PATCH_M,
                           fill=False, ec="k", lw=1.2, zorder=4))
    # protection zones
    ax.add_patch(Circle(STENKOVEC_EN, STENKOVEC_R_M, fill=False, ec="g", lw=1.0,
                        ls="--", zorder=4))
    e0, n0, e1, n1 = VARDAR_BOX
    ax.add_patch(Rectangle((e0, n0), e1 - e0, n1 - n0, fill=False, ec="g", lw=1.0,
                           ls=":", zorder=4))
    ax.plot(*STENKOVEC_EN, marker="*", ms=12, c="g", zorder=5)
    ax.annotate("Stenkovec", STENKOVEC_EN, textcoords="offset points",
                xytext=(6, 6), color="g", fontsize=9)
    ax.set_aspect("equal")
    pad = 3000.0
    ax.set_xlim(TEX_ULX_W - pad, TEX_ULX_W + NCOL * PATCH_M + pad)
    ax.set_ylim(TEX_SOUTH0 - pad, TEX_SOUTH0 + NCOL * PATCH_M + pad)
    ax.set_xlabel("UTM 34N Easting (m)")
    ax.set_ylabel("UTM 34N Northing (m)")
    ax.set_title(f"MacedoniaSkopje pylon autogen — {len(kept)} kept towers "
                 f"(budget {BUDGET}), lines >= 110 kV; dashed = no-thin zones")
    ax.legend(loc="lower right", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f"Saved {out_png}")


def _patch_of(E, N):
    col = int((E - TEX_ULX_W) // PATCH_M)                 # col counted from WEST
    row = int((N - TEX_SOUTH0) // PATCH_M)                # row counted from SOUTH
    return col, row


def render_closeup(center_rec, kept, ways, out_png, window_m=700.0, up=3):
    """Texture-overlay closeup: crop the installed north-up tCCRR.dds around the
    tower (true-UTM -> patch px, validate_bridge.py georef), draw the qualifying
    line polylines + a 10 m-radius tolerance circle per tower, save PNG."""
    from PIL import Image, ImageDraw

    E, N = center_rec["E"], center_rec["N"]
    col, row = _patch_of(E, N)
    dds = INSTALL_TEX / f"t{NCOL - 1 - col:02d}{row:02d}.dds"
    if not dds.exists():
        print(f"closeup: missing texture {dds}")
        return None
    img = Image.open(dds).convert("RGB")
    if img.size != (PATCH_PX, PATCH_PX):
        img = img.resize((PATCH_PX, PATCH_PX))
    west = TEX_ULX_W + col * PATCH_M
    north = TEX_SOUTH0 + (row + 1) * PATCH_M

    def to_px(e, n):
        return ((e - west) / PATCH_M * PATCH_PX, (north - n) / PATCH_M * PATCH_PX)

    m_per_px = PATCH_M / PATCH_PX                          # 2.8125 m/px
    half = window_m / m_per_px / 2.0
    cx, cy = to_px(E, N)
    w = int(round(2 * half))
    x0 = int(round(min(max(cx - half, 0), PATCH_PX - w)))
    y0 = int(round(min(max(cy - half, 0), PATCH_PX - w)))
    crop = img.crop((x0, y0, x0 + w, y0 + w)).resize((w * up, w * up), Image.NEAREST)
    drw = ImageDraw.Draw(crop)

    def to_crop(e, n):
        px, py = to_px(e, n)
        return ((px - x0) * up, (py - y0) * up)

    # qualifying line polylines through the window
    for wy in ways:
        pts = [to_crop(e, n) for (e, n) in wy["pts_utm"]]
        if any(-w * up <= p[0] <= 2 * w * up and -w * up <= p[1] <= 2 * w * up
               for p in pts) and len(pts) >= 2:
            drw.line(pts, fill=(0, 220, 255), width=2)

    # towers: 10 m tolerance circle (the QA acceptance radius) + centre dot
    r10 = 10.0 / m_per_px * up
    for r in kept:
        px, py = to_crop(r["E"], r["N"])
        if not (-r10 <= px <= w * up + r10 and -r10 <= py <= w * up + r10):
            continue
        colr = (255, 240, 0) if r["name"] == center_rec["name"] else (255, 40, 40)
        drw.ellipse([px - r10, py - r10, px + r10, py + r10], outline=colr, width=2)
        drw.ellipse([px - 2, py - 2, px + 2, py + 2], fill=colr)
        drw.text((px + r10 + 3, py - 6), f"{r['voltage'] // 1000}kV", fill=colr)

    # 100 m scale bar
    bar = 100.0 / m_per_px * up
    bx, by = 12, w * up - 18
    drw.line([(bx, by), (bx + bar, by)], fill=(255, 255, 255), width=3)
    drw.text((bx, by - 14), "100 m  (circles = 10 m radius)", fill=(255, 255, 255))
    drw.text((8, 6),
             f"{center_rec['name']}  {center_rec['voltage'] // 1000} kV  "
             f"E={center_rec['E']:.0f} N={center_rec['N']:.0f}  "
             f"line az {center_rec['ori_deg']:.1f} deg  [{dds.name}]",
             fill=(255, 255, 0))
    crop.save(out_png)
    print(f"Saved {out_png}")
    return out_png


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QA_DIR.mkdir(parents=True, exist_ok=True)

    data = fetch_power()
    towers, ways, wstats = load_elements(data)
    records, n_unoriented = build_records(towers, ways)
    in_ls = clip_records(records)
    kept, dropped, rounds = thin_to_budget(in_ls)
    kept.sort(key=lambda r: int(r["name"].split("_")[1]))

    by_name = {r["name"]: r for r in kept}

    # ---- optional ad-hoc closeups mode ------------------------------------ #
    if len(argv) > 1 and argv[1] == "--closeup":
        for nm in argv[2:]:
            r = by_name.get(nm)
            if r is None:
                print(f"closeup: {nm} not in kept set")
                continue
            render_closeup(r, kept, ways, QA_DIR / f"closeup_{nm}.png")
        return 0

    # ---- placements.json --------------------------------------------------- #
    placements = [{k: r[k] for k in ("name", "c3d", "E", "N", "ori_deg", "scale",
                                     "voltage", "line_ref", "arm_az_deg")}
                  for r in kept]
    meta = {
        "generator": "scripts/generate_pylons.py",
        "source": ".sandbox/osm/power.json — Overpass power=line ways (out geom) + "
                  "power=tower nodes, bbox S41.8276 W21.0829 N42.4537 E21.9242",
        "crs": "EPSG:32634 — TRUE UTM 34N metres (no texture-frame correction)",
        "model": ".sandbox/industrial/pylon.c3d — native 18.0 x 9.33 x 40.25 m, "
                 "base z=0, 18 m cross-arms along model local +X",
        "ori_convention": "ori_deg = Condor .obj `ori` in degrees under the "
                          "verified condor_grid convention (aims model local +Y; "
                          "local +X renders at ori_deg+90). ori_deg = line azimuth "
                          "at the tower => cross-arms PERPENDICULAR to the wires. "
                          "arm_az_deg = (ori_deg+90)%360 is the cross-arm axis "
                          "azimuth for consumers using an ori-aims-+X convention.",
        "scale_rule": "1.0 for voltage >= 220000; 0.75 for the 110 kV class",
        "voltage_rule": f"lines with max(voltage) >= {MIN_V} V only; semicolon "
                        "lists -> max; minor_line never queried",
        "budget": BUDGET,
        "count": len(placements),
    }
    pj = OUT_DIR / "placements.json"
    with open(pj, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"_meta": meta, "placements": placements}, f, indent=2)
        f.write("\n")
    print(f"Saved {pj} ({len(placements)} placements)")

    # ---- report.json ------------------------------------------------------- #
    kept_v = Counter(r["voltage"] for r in kept)
    kept_cls = Counter(vclass(r["voltage"]) for r in kept)
    lines_covered = sorted({(r["_way"], r["line_ref"]) for r in kept})
    report = {
        "bbox_wgs84_SWNE": list(BBOX),
        "towers_power_tower_in_bbox": len(towers),
        "power_line_ways_in_bbox": wstats["line_ways"],
        "line_ways_dropped_no_parseable_voltage": wstats["no_voltage"],
        "qualifying_lines_ge_110kV": len(ways),
        "towers_on_qualifying_lines": len(records),
        "towers_unorientable_skipped": n_unoriented,
        "towers_after_landscape_clip": len(in_ls),
        "kept": len(kept),
        "budget": BUDGET,
        "thin_rounds": rounds,
        "thinned_out": len(dropped),
        "protected_kept": sum(1 for r in kept if r["_prot"]),
        "kept_per_voltage_V": {str(v): kept_v[v] for v in sorted(kept_v, reverse=True)},
        "kept_per_class": {c: kept_cls[c] for c in ("400kV", "220kV", "110kV")
                           if kept_cls[c]},
        "lines_covered": {"count": len(lines_covered),
                          "refs": [{"way_id": wid, "ref": ref}
                                   for (wid, ref) in lines_covered]},
    }
    rj = OUT_DIR / "report.json"
    with open(rj, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(f"Saved {rj}")

    # ---- QA ----------------------------------------------------------------- #
    qa_overview(kept, dropped, ways, QA_DIR / "overview.png")
    picks = []
    for nm in CLOSEUP_TOWERS:
        if nm in by_name:
            picks.append(by_name[nm])
    if len(picks) < 2:                                     # deterministic fallback
        for r in kept:
            if r["voltage"] >= 380_000 and r not in picks:
                picks.append(r)
            if len(picks) == 2:
                break
    for i, r in enumerate(picks[:2], 1):
        render_closeup(r, kept, ways, QA_DIR / f"closeup_{i}.png",
                       window_m=400.0, up=4)

    print("\n=== pylon autogen summary ===")
    print(f"towers found (power=tower in bbox): {len(towers)}")
    print(f"on >=110 kV lines: {len(records)}   in-landscape: {len(in_ls)}   "
          f"kept: {len(kept)} (budget {BUDGET}, {rounds} thin round(s))")
    for v in sorted(kept_v, reverse=True):
        print(f"  {v // 1000:>4d} kV : {kept_v[v]:4d} towers  "
              f"(class {vclass(v)}, scale {1.0 if v >= V_220 else 0.75})")
    print(f"lines covered: {len(lines_covered)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
