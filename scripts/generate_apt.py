#!/usr/bin/env python3
"""
Generate Condor 2 .apt binary airport file directly from data/airports.json.

Record layout (72 bytes, little-endian). VERIFIED by decoding all 8 records of the
shipping Slovenia2.apt on 2026-06-21 (the field meanings at offsets 56/64 differ
from the older docs/condor_landscape_spec.md guess -- the Slovenia2 values prove
offset 56 = WIDTH and offset 64 = FREQUENCY, see below):
  byte   0       name length (uint8)
  bytes  1-31    airport name, null-padded ASCII
  bytes 32-35    float 0.0 (unused)
  bytes 36-39    latitude  (float32, decimal degrees)
  bytes 40-43    longitude (float32, decimal degrees)
  bytes 44-47    elevation (float32, metres)
  bytes 48-51    runway direction (int32, WHOLE degrees -- decimals crash C2)
  bytes 52-55    runway length (int32, metres)
  bytes 56-59    runway WIDTH (int32, metres)   <-- Slovenia2: 25,85,65,80,55,60,95,18
                 Drives the tug's lateral start offset St (AERO p.20). A bogus
                 value here breaks the aerotow ballet -> no towplane spawns.
  bytes 60-63    flags1 (uint32) -- Slovenia2 is 0 for all airports except one
                 (SLOVENJ GRADEC=1); NOT a simple "enabled" flag. Use 0.
  bytes 64-67    frequency MHz (float32)         <-- Slovenia2: 123.5 / 121.0
  bytes 68-71    flags2 (uint32) -- Slovenia2 uses 0x00000100 or 0x00010000
                 (the tow-side / primary-reversed checkbox bits). Use 0x00000100.

For Phase 1 we store the primary runway of each airport.
"""

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from condor_grid import LANDSCAPE_NAME

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# NM has no imagery-aligned airports file; the Skopje pilot prefers its aligned
# centres so the .apt midpoint matches the flattened plateau.
_NM = LANDSCAPE_NAME == "NorthMacedonia"
AIRPORTS_ALIGNED = PROJECT_ROOT / "data" / ("airports_nm_aligned.json" if _NM
                                            else "airports_aligned.json")
AIRPORTS_JSON = PROJECT_ROOT / "data" / ("airports_nm.json" if _NM else "airports.json")
OUT_APT = Path(f"C:/Condor2/Landscapes/{LANDSCAPE_NAME}/{LANDSCAPE_NAME}.apt")


def encode_airport(ap: dict, index: int) -> bytes:
    """Encode one airport record (primary runway only)."""
    name = ap.get("name", "")[:31]
    name_bytes = name.encode("ascii", errors="ignore").ljust(31, b"\x00")

    # Use the first/primary runway
    runways = ap.get("runways", [])
    if not runways:
        raise ValueError(f"Airport {ap.get('icao')} has no runways")
    rwy = runways[0]

    # .apt position must be the runway MIDPOINT (Condor reconstructs the strip as
    # midpoint +/- length/2 and spawns ~170 m in from the end). Use the aligned
    # runway centre so it coincides with the flattened plateau; fall back to the
    # airport reference point.
    lat = float(rwy.get("center_lat", ap["lat"]))
    lon = float(rwy.get("center_lon", ap["lon"]))
    elev = float(ap["elevation_m"])
    # HEADING: WHOLE DEGREES ONLY. Fractional/millidegree headings crash Condor
    # with "Airport is not installed" (verified on this landscape). Slovenia2 stores
    # plain whole-degree int32 (134, 111, 273, ...). Use the UTM-grid azimuth from
    # the runway ENDS -- that is the projection the ortho/painted runway lives in,
    # so the rounded .apt axis overlies the painted strip as closely as a whole
    # degree allows (residual lateral throw at the strip end is only a few metres).
    ends = rwy.get("ends")
    if ends and len(ends) >= 2:
        import math as _m
        from pyproj import Transformer as _T
        _tx = _T.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
        _a = _tx.transform(ends[0]["lon"], ends[0]["lat"])
        _b = _tx.transform(ends[1]["lon"], ends[1]["lat"])
        _az = _m.degrees(_m.atan2(_b[0] - _a[0], _b[1] - _a[1])) % 360.0
    else:
        _az = float(rwy["true_heading"])
    rwdir = int(round(_az))           # WHOLE degrees (Slovenia2 convention)
    # Condor spawns a ground start ~170 m IN from the .apt runway end (into wind),
    # NOT at the threshold. Extend the declared length by ~340 m (2x170) so the
    # spawn lands on the REAL threshold; the flattened plateau (flatten_runways.py)
    # is sized to cover it. Verified: Condor forum t=19413 / t=22592.
    rwlen = int(rwy["length_m"]) + 340
    # WIDTH (offset 56) -- VERIFIED as runway width in metres from Slovenia2
    # (25/85/65/80/55/60/95/18). This sets the tug's lateral start offset St
    # (AERO p.20): 0-25 m -> St 41 m, 50 m -> 50, 75 m -> 60, 100 m -> 72. A wrong
    # value here (the old code wrote a ~120000 frequency-ID into this field) breaks
    # the aerotow ballet so NO towplane spawns. Use the real runway width.
    width = int(round(float(rwy.get("width_m", 50))))
    # FREQUENCY (offset 64), MHz float -- Slovenia2 stores 123.5 / 121.0 here (it is
    # NOT a flatten radius; Condor never flattens from the .apt). Default 123.50.
    freq_mhz = float(ap.get("frequency_mhz", 123.50))

    record = b""
    record += struct.pack("<B", len(name))
    record += name_bytes
    record += struct.pack("<f", 0.0)          # [32] unused, always 0.0
    record += struct.pack("<fff", lat, lon, elev)   # [36][40][44]
    record += struct.pack("<iii", rwdir, rwlen, width)  # [48] dir [52] len [56] WIDTH
    record += struct.pack("<I", 0)            # [60] flags1 (Slovenia2 = 0)
    record += struct.pack("<f", freq_mhz)     # [64] frequency MHz (Slovenia2 = 123.5)
    record += struct.pack("<I", 0x00000100)   # [68] flags2 pattern from Slovenia2
    assert len(record) == 72, f"Record length is {len(record)}"
    return record


def main():
    # Prefer imagery-aligned geometry so the .apt midpoint matches the flattened
    # plateau (flatten_runways.py uses the same aligned centres).
    src = AIRPORTS_ALIGNED if AIRPORTS_ALIGNED.exists() else AIRPORTS_JSON
    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"Source: {src.name}")

    airports = data.get("airports", [])
    OUT_APT.parent.mkdir(parents=True, exist_ok=True)

    with open(OUT_APT, "wb") as f:
        for i, ap in enumerate(airports):
            f.write(encode_airport(ap, i))

    print(f"Wrote {len(airports)} airports to {OUT_APT}")
    print(f"File size: {OUT_APT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
