#!/usr/bin/env python3
r"""
Extract the real Skopje-area INDUSTRIAL sites from OpenStreetMap (Overpass) for
the MacedoniaSkopje landscape's industrial-object pass.

This is the *identification* step (task 2): it pulls every industrial feature
worth a placed Condor object -- power plants/generators, chimneys, cooling
towers, storage/gas tanks, works, industrial landuse, and the big NAMED sites
(OKTA refinery, TE-TO CHP, Zelezara steelworks, Skopski Leguri ferroalloys, the
cement works, the brewery, large warehouses) -- with real coords, footprint
extent (m) and any tagged height/levels.

It writes a human-curated-friendly JSON to .sandbox/industrial/osm_industrial.json
(raw, every feature) and a deduped, ranked site list to
.sandbox/industrial/sites.json (one entry per real-world site, the named ones
first). SANDBOX ONLY -- touches no install, no .obj, no .apt.

Tags queried (Overpass QL):
  power      = plant | generator | substation
  man_made   = chimney | works | storage_tank | gasometer | cooling_tower
             | silo | tank | pipeline (pipeline excluded from objects, kept FYI)
  landuse    = industrial            (the site polygons that frame everything)
  building   = industrial | warehouse | factory   (big sheds)

Footprint extent + long-edge azimuth are computed in UTM 34N so the numbers are
metric and directly feed condor_grid.footprint_to_local / heading_deg_to_ori.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pyproj
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
import osm_io  # noqa: E402
from condor_grid import UTM_CRS, WGS84_CRS  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / ".sandbox" / "industrial"
OUT.mkdir(parents=True, exist_ok=True)

# Skopje pilot bbox (WGS84), derived from condor_grid (12x12 @ 30 m UTM 34N).
SOUTH, WEST, NORTH, EAST = 41.8276, 21.0829, 42.4537, 21.9242

_TO_UTM = pyproj.Transformer.from_crs(WGS84_CRS, UTM_CRS, always_xy=True)


def _industrial_query(south, west, north, east):
    return f"""[out:json][timeout:300][bbox:{south},{west},{north},{east}];
(
  way["power"~"^(plant|generator|substation)$"];
  relation["power"~"^(plant|generator|substation)$"];
  node["power"~"^(plant|generator)$"];

  way["man_made"~"^(chimney|works|storage_tank|gasometer|cooling_tower|silo|tank)$"];
  relation["man_made"~"^(chimney|works|storage_tank|gasometer|cooling_tower|silo|tank)$"];
  node["man_made"~"^(chimney|works|storage_tank|gasometer|cooling_tower|silo|tank)$"];

  way["landuse"="industrial"];
  relation["landuse"="industrial"];

  way["building"~"^(industrial|warehouse|factory)$"];
  relation["building"~"^(industrial|warehouse|factory)$"];

  way["man_made"="pipeline"];
);
out geom;"""


def fetch():
    """Run the Overpass industrial query; cache the raw JSON so reruns are free."""
    raw_path = OUT / "overpass_raw.json"
    if raw_path.exists():
        print(f"using cached {raw_path}")
        return json.loads(raw_path.read_text())
    query = _industrial_query(SOUTH, WEST, NORTH, EAST)
    last = None
    for attempt in range(6):
        ep = osm_io.OVERPASS_MIRRORS[attempt % len(osm_io.OVERPASS_MIRRORS)]
        import urllib.parse, requests, time
        url = ep + "?data=" + urllib.parse.quote(query)
        try:
            r = requests.get(url, headers={"User-Agent": osm_io.USER_AGENT}, timeout=600)
            r.raise_for_status()
            data = r.json()
            raw_path.write_text(json.dumps(data))
            print(f"fetched {len(data.get('elements', []))} elements -> {raw_path}")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"overpass attempt {attempt+1} @ {ep.split('//')[1].split('/')[0]} failed: {exc}")
            time.sleep(min(20, 4 * (attempt + 1)))
    raise last


def _num(props, *keys):
    """Parse a numeric tag (height / building:levels) -> float or None."""
    for k in keys:
        v = props.get(k)
        if v is None:
            continue
        try:
            return float(str(v).split()[0].replace(",", "."))
        except (ValueError, IndexError):
            continue
    return None


def _height_m(props):
    """Best height estimate from tags: height -> levels*3.0 -> None."""
    h = _num(props, "height", "est_height")
    if h is not None:
        return h, "height tag"
    lv = _num(props, "building:levels", "levels")
    if lv is not None:
        return lv * 3.0, f"{lv:g} levels x3 m"
    return None, "untagged"


def _footprint_metrics(geom_wgs):
    """Return (lat, lon, foot_m=[long,short], long_axis_az_deg, area_m2) in UTM."""
    g_utm = shp_transform(lambda x, y, z=None: _TO_UTM.transform(x, y), geom_wgs)
    c = g_utm.centroid
    lon, lat = pyproj.Transformer.from_crs(UTM_CRS, WGS84_CRS, always_xy=True).transform(c.x, c.y)
    # min-area rotated rectangle gives the true long/short edge + orientation
    try:
        mrr = g_utm.minimum_rotated_rectangle
        xs, ys = mrr.exterior.coords.xy
        edges = []
        for i in range(4):
            dx = xs[i + 1] - xs[i]
            dy = ys[i + 1] - ys[i]
            edges.append((math.hypot(dx, dy), math.atan2(dx, dy)))  # az = atan2(E,N)
        edges.sort(reverse=True)
        long_len = edges[0][0]
        short_len = edges[2][0] if edges[2][0] <= edges[0][0] else edges[0][0]
        short_len = min(e[0] for e in edges)
        az = math.degrees(edges[0][1]) % 180.0
    except Exception:  # noqa: BLE001
        b = g_utm.bounds
        long_len, short_len, az = b[2] - b[0], b[3] - b[1], 0.0
    return (round(lat, 6), round(lon, 6),
            [round(long_len, 1), round(short_len, 1)],
            round(az, 1), round(g_utm.area, 1))


# Object-worthy man_made point/area features -> a coarse "kind" for model choice.
KIND_BY_MANMADE = {
    "chimney": "chimney",
    "cooling_tower": "cooling_tower",
    "storage_tank": "storage_tank",
    "tank": "storage_tank",
    "gasometer": "gas_holder",
    "silo": "silo",
    "works": "works",
}


def classify(props):
    """Map a feature's tags to (kind, object_worthy:bool)."""
    mm = props.get("man_made")
    if mm in KIND_BY_MANMADE:
        return KIND_BY_MANMADE[mm], True
    if mm == "pipeline":
        return "pipeline", False
    p = props.get("power")
    if p == "plant":
        return "power_plant", True   # the site polygon; halls/chimneys are separate features
    if p == "generator":
        return "generator", True
    if p == "substation":
        return "substation", False
    b = props.get("building")
    if b in ("industrial", "warehouse", "factory"):
        return "industrial_building", True
    if props.get("landuse") == "industrial":
        return "industrial_landuse", False  # framing polygon, not an object
    return "other", False


def main():
    data = fetch()
    gj = osm_io.osm_json_to_geojson(data)
    # also keep nodes (chimneys/tanks are often a single node with height)
    node_pts = osm_io.osm_json_to_points(data)

    feats = []
    for f in gj["features"]:
        props = f["properties"]
        kind, worthy = classify(props)
        try:
            geom = shape(f["geometry"])
            if geom.is_empty:
                continue
            lat, lon, foot, az, area = _footprint_metrics(geom)
        except Exception:  # noqa: BLE001
            continue
        h, hsrc = _height_m(props)
        feats.append(dict(
            kind=kind, object_worthy=worthy,
            name=props.get("name") or props.get("operator") or "",
            lat=lat, lon=lon, footprint_m=foot, long_axis_deg=az, area_m2=area,
            height_m=h, height_src=hsrc,
            osm=dict((k, props[k]) for k in (
                "power", "man_made", "building", "landuse", "plant:source",
                "plant:output:electricity", "generator:source", "product",
                "operator", "name:en", "name:mk") if k in props),
        ))

    # nodes that the polygon pass missed (man_made point chimneys/tanks)
    seen = {(round(x["lat"], 5), round(x["lon"], 5)) for x in feats}
    for p in node_pts:
        props = p["tags"]
        kind, worthy = classify(props)
        if not worthy:
            continue
        key = (round(p["lat"], 5), round(p["lon"], 5))
        if key in seen:
            continue
        h, hsrc = _height_m(props)
        feats.append(dict(
            kind=kind, object_worthy=worthy, name=props.get("name") or "",
            lat=round(p["lat"], 6), lon=round(p["lon"], 6),
            footprint_m=None, long_axis_deg=0.0, area_m2=None,
            height_m=h, height_src=hsrc,
            osm=dict((k, props[k]) for k in (
                "power", "man_made", "building", "height", "operator") if k in props),
        ))

    feats.sort(key=lambda f: (-(f["area_m2"] or 0), f["kind"]))
    (OUT / "osm_industrial.json").write_text(json.dumps(feats, indent=2, ensure_ascii=False))

    worthy = [f for f in feats if f["object_worthy"]]
    landuse = [f for f in feats if f["kind"] == "industrial_landuse"]
    print(f"\n=== {len(feats)} industrial features ({len(worthy)} object-worthy, "
          f"{len(landuse)} industrial-landuse polygons) ===")
    from collections import Counter
    for k, n in Counter(f["kind"] for f in feats).most_common():
        print(f"  {k:22} {n}")

    print("\n--- NAMED object-worthy sites (largest first) ---")
    named = [f for f in worthy if f["name"]]
    for f in named[:40]:
        fm = f["footprint_m"]
        fms = f"{fm[0]:.0f}x{fm[1]:.0f}m" if fm else "node"
        hm = f"{f['height_m']:.0f}m" if f["height_m"] else "h?"
        print(f"  {f['kind']:18} {fms:>12} {hm:>5}  {f['lat']:.4f},{f['lon']:.4f}  {f['name']}")

    print("\n--- biggest industrial-landuse polygons (the named industrial zones) ---")
    for f in sorted(landuse, key=lambda x: -(x["area_m2"] or 0))[:15]:
        nm = f["name"] or "(unnamed zone)"
        print(f"  {f['area_m2']/1e4:7.1f} ha  {f['lat']:.4f},{f['lon']:.4f}  {nm}")

    return feats


if __name__ == "__main__":
    main()
