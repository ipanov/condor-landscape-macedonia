#!/usr/bin/env python3
"""TEMPORARY airborne DEBUG airport at the centre of the placed Skopje city
landmarks, so we can airborne-start right over them and debug object placement.

Objects only render within ~5 km and Condor only lets you start at an airport, so
to inspect the city landmarks we drop a *virtual* airport (an .apt entry with NO
runway .c3d) in the middle of them. Virtual => airborne start works (Landscape
Guide rev2 p.18); ground/tow start is intentionally unavailable (no c3d).

Centre = centroid of the central-Skopje (Macedonia Square / Vardar valley) static
landmarks in data/placement_manifest.json. The Vodno cross/telecom tower sit ~3.5 km
south, well inside render range from altitude.

REMOVE later (restore the 3 real airports): python scripts/generate_apt.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_apt                      # noqa: E402  (encode_airport, 72-byte record)
import condor_grid as G                  # noqa: E402
from pyproj import Transformer           # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(f"C:/Condor2/Landscapes/{G.LANDSCAPE_NAME}/{G.LANDSCAPE_NAME}.apt")
_to_wgs = Transformer.from_crs("EPSG:32634", "EPSG:4326", always_xy=True).transform

# --- centre on the central-Skopje landmark cluster (valley floor, the square) ----
man = json.loads((ROOT / "data/placement_manifest.json").read_text(encoding="utf-8"))
pts = []
for o in man["objects"]:
    fp = o.get("footprint", {})
    if fp.get("source") == "static" and "E" in fp:
        E, N = float(fp["E"]), float(fp["N"])
        # the Macedonia-Square / Vardar valley cluster (exclude Stenkovec airfield
        # ~4656 kN and the Vodno mountain monuments ~4646 kN so we spawn over downtown)
        if 4648500 < N < 4651500 and 534500 < E < 537500:
            pts.append((E, N, o["id"]))
if not pts:
    sys.exit("no central-Skopje static landmarks found in the manifest")
cE = sum(p[0] for p in pts) / len(pts)
cN = sum(p[1] for p in pts) / len(pts)
lon, lat = _to_wgs(cE, cN)

# --- append the virtual debug airport to the 3 real ones -------------------------
src = ROOT / "data/airports_aligned.json"
airports = list(json.loads(src.read_text(encoding="utf-8"))["airports"])
debug = {
    "icao": "ZZDB", "name": "ZDebugSkopjeCity", "lat": lat, "lon": lon,
    "elevation_m": 245.0,                     # Vardar valley floor
    "runways": [{"designation": "09/27", "length_m": 1000, "width_m": 50,
                 "surface": "grass", "true_heading": 90,
                 "center_lat": lat, "center_lon": lon}],
}
blob = b"".join(generate_apt.encode_airport(a, i) for i, a in enumerate(airports + [debug]))
OUT.write_bytes(blob)

print(f"centred on {len(pts)} downtown landmarks: {[p[2] for p in pts]}")
print(f"debug airport 'ZDebugSkopjeCity' @ lat={lat:.5f} lon={lon:.5f} (UTM {cE:.0f},{cN:.0f}, elev 245 m)")
print(f"wrote {len(airports)+1} airports = {len(blob)} bytes ({len(blob)//72} records) -> {OUT}")
print("Select it in the flight planner, choose AIRBORNE start, fly over the objects.")
print("REMOVE later: python scripts/generate_apt.py")
