#!/usr/bin/env python3
"""Generate SeeYou .cup turnpoint file for MacedoniaSkopje."""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output" / "MacedoniaSkopje.cup"
OUT.parent.mkdir(parents=True, exist_ok=True)


def dd_to_seeyou(lat, lon):
    """Convert decimal degrees to SeeYou lat/lon strings."""
    ns = "N" if lat >= 0 else "S"
    lat_abs = abs(lat)
    lat_deg = int(lat_abs)
    lat_min = (lat_abs - lat_deg) * 60.0
    lat_str = f"{lat_deg:02d}{lat_min:06.3f}{ns}"

    ew = "E" if lon >= 0 else "W"
    lon_abs = abs(lon)
    lon_deg = int(lon_abs)
    lon_min = (lon_abs - lon_deg) * 60.0
    lon_str = f"{lon_deg:03d}{lon_min:06.3f}{ew}"
    return lat_str, lon_str


def elev_m_to_ft(m):
    return int(round(m * 3.28084))


# Additional turnpoints: name, code, lat, lon, elev_m, style, rwdir, rwlen_m, freq, desc
TURNPOINTS = [
    # Major cities / landmarks
    ("Skopje", "SKOPJE", 41.9981, 21.4254, 240, 1, 0, 0, "", "Capital city of North Macedonia"),
    ("Tetovo", "TETOVO", 42.0069, 20.9714, 468, 1, 0, 0, "", "City in Polog valley"),
    ("Veles", "VELES", 41.7154, 21.7758, 206, 1, 0, 0, "", "City on Vardar river"),
    ("Kumanovo", "KUMANOVO", 42.1322, 21.7144, 340, 1, 0, 0, "", "City northeast of Skopje"),
    ("Shtip", "SHTIP", 41.7458, 22.1958, 325, 1, 0, 0, "", "City east of Skopje"),
    ("Gostivar", "GOSTIVAR", 41.7961, 20.9086, 535, 1, 0, 0, "", "City west of Skopje"),
    # Mountains / peaks
    ("Vodno", "VODNO", 41.9639, 21.3933, 1066, 1, 0, 0, "", "Mountain with Millennium Cross"),
    ("Millennium Cross", "CROSS", 41.9653, 21.4000, 1066, 1, 0, 0, "", "66m cross on Vodno"),
    ("Ljuboten", "LJUBOTN", 42.2097, 21.7853, 2498, 1, 0, 0, "", "Peak on Shara range"),
    ("Titov Vrv", "TITOVVR", 42.0000, 20.8750, 2747, 1, 0, 0, "", "Highest peak Shar Planina"),
    ("Karadzica", "KARADZI", 41.7917, 21.5000, 2473, 1, 0, 0, "", "Mountain south of Skopje"),
    ("Skopska Crna Gora", "CRNAGOR", 42.2500, 21.6667, 1651, 1, 0, 0, "", "Range north of Skopje"),
    # Rivers / lakes
    ("Lake Matka", "MATKA", 41.9500, 21.3000, 316, 1, 0, 0, "", "Reservoir on Treska river"),
    ("Kozjak Lake", "KOZJAK", 41.8564, 21.3917, 302, 1, 0, 0, "", "Reservoir on Treska river"),
    ("Stobi", "STOBI", 41.6167, 21.9667, 230, 1, 0, 0, "", "Ancient archaeological site"),
    ("Kale Fortress", "KALE", 42.0008, 21.4314, 325, 1, 0, 0, "", "Fortress overlooking Skopje"),
]


def main():
    with open(DATA / "airports.json") as f:
        data = json.load(f)

    lines = [
        "name,code,country,lat,lon,elev,style,rwdir,rwlen,freq,desc"
    ]

    # Add airports first
    for ap in data["airports"]:
        lat, lon = dd_to_seeyou(ap["lat"], ap["lon"])
        elev_ft = elev_m_to_ft(ap["elevation_m"])
        surface = ap["runways"][0]["surface"].lower()
        style = 4 if "asphalt" in surface or "concrete" in surface else 2
        rwlen_m = int(ap["runways"][0]["length_m"])
        rwdir = int(round(float(ap["runways"][0]["true_heading"])))
        code = ap["icao"]
        name = ap["name"]
        freq = ""
        if code == "LWSK":
            freq = "129.400"
        desc = ap.get("source", "")
        lines.append(
            f'"{name}","{code}",MK,{lat},{lon},{elev_ft}ft,{style},{rwdir},{rwlen_m}m,{freq},"{desc}"'
        )

    # Add turnpoints
    for name, code, lat, lon, elev_m, style, rwdir, rwlen_m, freq, desc in TURNPOINTS:
        lat_s, lon_s = dd_to_seeyou(lat, lon)
        elev_ft = elev_m_to_ft(elev_m)
        lines.append(
            f'"{name}","{code}",MK,{lat_s},{lon_s},{elev_ft}ft,{style},{rwdir},{rwlen_m}m,{freq},"{desc}"'
        )

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(lines)-1} turnpoints to {OUT}")


if __name__ == "__main__":
    main()
