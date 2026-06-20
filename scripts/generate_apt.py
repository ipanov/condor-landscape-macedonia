#!/usr/bin/env python3
"""
Generate Condor 2 .apt binary airport file directly from data/airports.json.

Record layout (72 bytes, little-endian), verified against Slovenia2.apt:
  byte   0       name length (uint8)
  bytes  1-31    airport name, null-padded ASCII
  bytes 32-35    float 0.0 (unused)
  bytes 36-39    latitude  (float32, decimal degrees)
  bytes 40-43    longitude (float32, decimal degrees)
  bytes 44-47    elevation (float32, metres)
  bytes 48-51    runway direction (int32, degrees true)
  bytes 52-55    runway length (int32, metres)
  bytes 56-59    frequency / airport ID (int32)
  bytes 60-63    flags / has_aviation (int32, Slovenia2 uses 0 or 1)
  bytes 64-67    flatten radius (float32, Slovenia2 uses ~120 m)
  bytes 68-71    unknown flags (int32, Slovenia2 uses 0x00000100 or 0x00010000)

For Phase 1 we store the primary runway of each airport.
"""

import json
import struct
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AIRPORTS_JSON = PROJECT_ROOT / "data" / "airports.json"
OUT_APT = Path("C:/Condor2/Landscapes/MacedoniaSkopje/MacedoniaSkopje.apt")


def encode_airport(ap: dict, index: int) -> bytes:
    """Encode one airport record (primary runway only)."""
    name = ap.get("name", "")[:31]
    name_bytes = name.encode("ascii", errors="ignore").ljust(31, b"\x00")

    # Use the first/primary runway
    runways = ap.get("runways", [])
    if not runways:
        raise ValueError(f"Airport {ap.get('icao')} has no runways")
    rwy = runways[0]

    lat = float(ap["lat"])
    lon = float(ap["lon"])
    elev = float(ap["elevation_m"])
    rwdir = int(round(rwy["true_heading"]))
    rwlen = int(rwy["length_m"])
    # Use a synthetic airport ID / frequency
    freq = int(ap.get("frequency_khz", 0)) or (120000 + index * 250)

    record = b""
    record += struct.pack("<B", len(name))
    record += name_bytes
    record += struct.pack("<f", 0.0)          # unused
    record += struct.pack("<fff", lat, lon, elev)
    record += struct.pack("<iii", rwdir, rwlen, freq)
    record += struct.pack("<I", 1)            # has_aviation / enabled
    record += struct.pack("<f", 150.0)        # flatten radius metres
    record += struct.pack("<I", 0x00000100)   # flags pattern from Slovenia2
    assert len(record) == 72, f"Record length is {len(record)}"
    return record


def main():
    with open(AIRPORTS_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    airports = data.get("airports", [])
    OUT_APT.parent.mkdir(parents=True, exist_ok=True)

    with open(OUT_APT, "wb") as f:
        for i, ap in enumerate(airports):
            f.write(encode_airport(ap, i))

    print(f"Wrote {len(airports)} airports to {OUT_APT}")
    print(f"File size: {OUT_APT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
