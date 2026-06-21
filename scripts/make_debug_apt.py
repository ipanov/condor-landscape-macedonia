#!/usr/bin/env python3
"""Add a TEMPORARY virtual debug airport at the centre of the cadastre autogen so we
can airborne-start right over the buildings (objects only render within ~5 km, and
Condor only lets you start at an airport). Virtual = .apt entry with no runway c3d;
airborne start works. To REMOVE it, just rerun: python scripts/generate_apt.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_apt
from pyproj import Transformer
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
OUT = Path("C:/Condor2/Landscapes/MacedoniaSkopje/MacedoniaSkopje.apt")

feats = json.load(open(ROOT / ".sandbox" / "buildings" / "cadastre_buildings.geojson",
                      encoding="utf-8"))["features"]
xs, ys = [], []
for f in feats:
    c = shape(f["geometry"]).centroid
    xs.append(c.x)
    ys.append(c.y)
cE, cN = sum(xs) / len(xs), sum(ys) / len(ys)
lon, lat = Transformer.from_crs("EPSG:32634", "EPSG:4326", always_xy=True).transform(cE, cN)

data = json.load(open(ROOT / "data" / "airports_aligned.json", encoding="utf-8"))
airports = list(data["airports"])
debug = {
    "icao": "ZZDB", "name": "ZDebugSkopjeAutogen", "lat": lat, "lon": lon,
    "elevation_m": 245.0,
    "runways": [{"designation": "09/27", "length_m": 1000, "width_m": 50,
                 "surface": "grass", "true_heading": 90,
                 "center_lat": lat, "center_lon": lon}],
}
allap = airports + [debug]
blob = b"".join(generate_apt.encode_airport(a, i) for i, a in enumerate(allap))
OUT.write_bytes(blob)
print(f"debug airport 'ZDebugSkopjeAutogen' @ {lat:.5f},{lon:.5f} (UTM {cE:.0f},{cN:.0f})")
print(f"wrote {len(allap)} airports = {len(blob)} bytes ({len(blob)//72} records)")
print("REMOVE later with: python scripts/generate_apt.py")
