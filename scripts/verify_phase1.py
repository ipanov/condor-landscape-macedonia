"""
Phase 1 Landscape Verification Script
Verifies all files at C:/Condor2/Landscapes/MacedoniaSkopje/
against the Condor 2 file format specification.
"""

import struct
import os
import json
import sys

LANDSCAPE_DIR = "C:/Condor2/Landscapes/MacedoniaSkopje"
NAME = "MacedoniaSkopje"

# Expected grid: 12x12 patches = 144, 3x3 tiles
PATCH_COLS = 12
PATCH_ROWS = 12
NUM_PATCHES = PATCH_COLS * PATCH_ROWS  # 144

# Expected .trn/.bmp/.tdm dimensions: 90 m overview = patches x 64 = 12 x 64 = 768.
# (2305 is the full 30 m DEM .raw used only for .tr3 extraction, NOT the .trn.)
TRN_WIDTH = 768
TRN_HEIGHT = 768

results = []

def report(item, status, detail=""):
    tag = "PASS" if status else "FAIL"
    results.append((item, tag, detail))
    symbol = "[PASS]" if status else "[FAIL]"
    print(f"  {symbol} {item}")
    if detail:
        for line in detail.strip().split("\n"):
            print(f"         {line}")


def section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


# ============================================================
# 1. MacedoniaSkopje.trn
# ============================================================
section("1. MacedoniaSkopje.trn - Source heightmap overview")

trn_path = os.path.join(LANDSCAPE_DIR, f"{NAME}.trn")
expected_trn_size = 36 + TRN_WIDTH * TRN_HEIGHT * 2  # 1,179,684

if not os.path.exists(trn_path):
    report(".trn exists", False, f"File not found: {trn_path}")
else:
    actual_size = os.path.getsize(trn_path)
    report(".trn file size", actual_size == expected_trn_size,
           f"Expected {expected_trn_size:,} bytes, got {actual_size:,} bytes")

    with open(trn_path, "rb") as f:
        header = f.read(36)

    if len(header) < 36:
        report(".trn header readable", False, f"Only read {len(header)} bytes of header")
    else:
        # Parse header per spec:
        # int32 width, int32 height, 3 floats (rot), float easting, float northing,
        # uint16 zone, uint16 pad, uint16 hemi, uint16 pad
        # Total: 4+4+4+4+4+4+4+2+2+2+2 = 36 bytes
        fmt = "<iifffffHHHH"
        (width, height,
         rot1, rot2, rot3,
         easting, northing,
         utm_zone, pad1,
         utm_hemi, pad2) = struct.unpack(fmt, header)

        report(".trn width", width == TRN_WIDTH,
               f"Expected {TRN_WIDTH}, got {width}")
        report(".trn height", height == TRN_HEIGHT,
               f"Expected {TRN_HEIGHT}, got {height}")
        report(".trn UTM zone", utm_zone == 34,
               f"Expected 34, got {utm_zone}")
        report(".trn hemisphere", utm_hemi == 78,
               f"Expected 78 (N), got {utm_hemi} ('{chr(utm_hemi) if 32 < utm_hemi < 128 else '?'}')")
        report(".trn rotation angles", True,
               f"rot1={rot1:.1f}, rot2={rot2:.1f}, rot3={rot3:.1f}")
        report(".trn UTM origin", True,
               f"easting={easting:.1f}, northing={northing:.1f}")

        # Sample some elevation data
        with open(trn_path, "rb") as f:
            f.seek(36)
            # Read first 100 values
            sample_data = struct.unpack(f"<{100}H", f.read(200))
            # Read from middle
            mid_offset = 36 + (TRN_HEIGHT // 2 * TRN_WIDTH + TRN_WIDTH // 2) * 2
            f.seek(mid_offset)
            mid_data = struct.unpack(f"<{100}H", f.read(200))
            all_samples = sample_data + mid_data

        min_elev = min(all_samples)
        max_elev = max(all_samples)
        report(".trn elevation sample range", 50 <= max_elev <= 3000,
               f"Sample range: {min_elev}m - {max_elev}m (expect 100-2800m for Macedonia)")


# ============================================================
# 2. HeightMaps/hCCRR.tr3
# ============================================================
section("2. HeightMaps/hCCRR.tr3 - Per-patch heightmaps")

hm_dir = os.path.join(LANDSCAPE_DIR, "HeightMaps")
expected_tr3_size = 193 * 193 * 2  # 74,498

if not os.path.isdir(hm_dir):
    report("HeightMaps/ directory exists", False)
else:
    # Build expected file list: CCRR where CC=0..11, RR=0..11
    expected_files = []
    for cc in range(PATCH_COLS):
        for rr in range(PATCH_ROWS):
            expected_files.append(f"h{cc:02d}{rr:02d}.tr3")

    actual_files = [f for f in os.listdir(hm_dir) if f.endswith(".tr3")]
    report("HeightMaps file count", len(actual_files) == NUM_PATCHES,
           f"Expected {NUM_PATCHES}, found {len(actual_files)}")

    missing = set(expected_files) - set(actual_files)
    extra = set(actual_files) - set(expected_files)
    if missing:
        report("HeightMaps no missing files", False,
               f"Missing: {sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}")
    else:
        report("HeightMaps no missing files", True)

    if extra:
        report("HeightMaps no extra files", False,
               f"Extra: {sorted(extra)[:10]}")
    else:
        report("HeightMaps no extra files", True)

    # Check file sizes
    wrong_sizes = []
    for fn in actual_files:
        fp = os.path.join(hm_dir, fn)
        sz = os.path.getsize(fp)
        if sz != expected_tr3_size:
            wrong_sizes.append((fn, sz))
    report("HeightMaps all files 74,498 bytes", len(wrong_sizes) == 0,
           f"{len(wrong_sizes)} files with wrong size" + (f": {wrong_sizes[:5]}" if wrong_sizes else ""))

    # Sample elevation values from a few .tr3 files
    samples_to_check = ["h0000.tr3", "h0606.tr3", "h1111.tr3", "h0505.tr3"]
    all_mins = []
    all_maxs = []
    for fn in samples_to_check:
        fp = os.path.join(hm_dir, fn)
        if os.path.exists(fp):
            with open(fp, "rb") as f:
                data = struct.unpack(f"<{193*193}H", f.read())
            all_mins.append(min(data))
            all_maxs.append(max(data))

    if all_mins:
        global_min = min(all_mins)
        global_max = max(all_maxs)
        reasonable = global_max > 100 and global_max <= 3000
        report("HeightMaps elevation range reasonable", reasonable,
               f"Sampled min={global_min}m, max={global_max}m across {len(all_mins)} files")
    else:
        report("HeightMaps elevation sampling", False, "Could not read any sample files")


# ============================================================
# 3. MacedoniaSkopje.apt - Airports
# ============================================================
section("3. MacedoniaSkopje.apt - Airports (binary)")

apt_path = os.path.join(LANDSCAPE_DIR, f"{NAME}.apt")
AIRPORT_RECORD_SIZE = 72
EXPECTED_AIRPORTS = 3

if not os.path.exists(apt_path):
    report(".apt exists", False, f"File not found: {apt_path}")
else:
    actual_size = os.path.getsize(apt_path)
    expected_apt_size = EXPECTED_AIRPORTS * AIRPORT_RECORD_SIZE  # 216
    report(".apt file size", actual_size == expected_apt_size,
           f"Expected {expected_apt_size} bytes ({EXPECTED_AIRPORTS} airports * {AIRPORT_RECORD_SIZE}), got {actual_size}")

    num_records = actual_size // AIRPORT_RECORD_SIZE
    report(".apt record count", num_records == EXPECTED_AIRPORTS,
           f"Expected {EXPECTED_AIRPORTS}, got {num_records}")

    # Load reference data
    ref_path = "D:/Repos/condor-landscape/data/airports.json"
    with open(ref_path, "r") as f:
        ref_data = json.load(f)
    ref_airports = {a["icao"]: a for a in ref_data["airports"]}

    with open(apt_path, "rb") as f:
        apt_data = f.read()

    for i in range(min(num_records, 10)):
        offset = i * AIRPORT_RECORD_SIZE
        record = apt_data[offset:offset + AIRPORT_RECORD_SIZE]

        name_len = record[0]
        name = record[1:1+name_len].decode("ascii", errors="replace")

        # Parse fields. Offset 56 is runway WIDTH in metres (verified vs Slovenia2.apt:
        # 25/85/65/80/55/18 ... = real widths), NOT a frequency/id. Offset 64 holds
        # the radio frequency MHz (123.5/121.0). Width drives the aerotow tug offset.
        (unused, lat, lon, elev, rwy_dir, rwy_len, width) = struct.unpack(
            "<f f f f i i i", record[32:60])
        freq_mhz = struct.unpack("<f", record[64:68])[0]

        detail_lines = [
            f"Airport #{i+1}: '{name}'",
            f"  Lat: {lat:.6f}, Lon: {lon:.6f}",
            f"  Elevation: {elev:.1f}m",
            f"  Runway dir: {rwy_dir} deg, length: {rwy_len}m, width: {width}m",
            f"  Frequency: {freq_mhz:.2f} MHz",
        ]

        # Try to match with reference
        matched = None
        for icao, ref in ref_airports.items():
            if abs(ref["lat"] - lat) < 0.05 and abs(ref["lon"] - lon) < 0.05:
                matched = (icao, ref)
                break

        if matched:
            icao, ref = matched
            lat_ok = abs(ref["lat"] - lat) < 0.01
            lon_ok = abs(ref["lon"] - lon) < 0.01
            elev_ok = abs(ref["elevation_m"] - elev) < 20
            detail_lines.append(f"  Matched reference: {icao} ({ref['name']})")
            detail_lines.append(f"  Ref lat={ref['lat']:.6f}, lon={ref['lon']:.6f}, elev={ref['elevation_m']}m")
            report(f".apt airport '{name}' matches {icao}", lat_ok and lon_ok and elev_ok,
                   "\n".join(detail_lines))
        else:
            detail_lines.append("  WARNING: No matching reference airport found!")
            report(f".apt airport '{name}' reference match", False,
                   "\n".join(detail_lines))


# ============================================================
# 4. MacedoniaSkopje.cup - Turnpoints
# ============================================================
section("4. MacedoniaSkopje.cup - Turnpoints/Waypoints")

cup_path = os.path.join(LANDSCAPE_DIR, f"{NAME}.cup")

if not os.path.exists(cup_path):
    report(".cup exists", False, f"File not found: {cup_path}")
else:
    with open(cup_path, "r", encoding="latin-1") as f:
        cup_lines = f.readlines()

    report(".cup file exists", True, f"{len(cup_lines)} lines")

    # Check header
    if cup_lines:
        header = cup_lines[0].strip()
        has_header = "name" in header.lower() and "lat" in header.lower()
        report(".cup has valid header", has_header,
               f"Header: {header[:120]}")

    # Count data lines
    data_lines = [l for l in cup_lines[1:] if l.strip() and not l.startswith("---")]
    report(".cup has data entries", len(data_lines) >= 3,
           f"Found {len(data_lines)} data entries (expect at least 3 airports)")

    # Show first few entries
    for line in data_lines[:5]:
        print(f"         Sample: {line.strip()[:120]}")

    # Check for airport ICAO codes
    cup_text = "".join(cup_lines)
    for icao in ["LWSK", "LWSN", "LW67"]:
        found = icao in cup_text
        report(f".cup contains {icao}", found)


# ============================================================
# 5. MacedoniaSkopje.tdm - Thermal/Albedo map
# ============================================================
section("5. MacedoniaSkopje.tdm - Thermal/Albedo map")

tdm_path = os.path.join(LANDSCAPE_DIR, f"{NAME}.tdm")
expected_tdm_size = 8 + TRN_WIDTH * TRN_HEIGHT  # 5,313,033

if not os.path.exists(tdm_path):
    report(".tdm exists", False, f"File not found: {tdm_path}")
else:
    actual_size = os.path.getsize(tdm_path)
    report(".tdm file size", actual_size == expected_tdm_size,
           f"Expected {expected_tdm_size:,} bytes, got {actual_size:,} bytes")

    with open(tdm_path, "rb") as f:
        tdm_header = f.read(8)
        w, h = struct.unpack("<ii", tdm_header)
        report(".tdm header width", w == TRN_WIDTH,
               f"Expected {TRN_WIDTH}, got {w}")
        report(".tdm header height", h == TRN_HEIGHT,
               f"Expected {TRN_HEIGHT}, got {h}")

        # Sample data to check value range
        # Read a chunk from the middle
        mid_pos = 8 + (TRN_HEIGHT // 2 * TRN_WIDTH)
        f.seek(mid_pos)
        sample = f.read(10000)
        values = list(sample)

        unique_values = set(values)
        all_zeros = all(v == 0 for v in values)
        all_128 = all(v == 128 for v in values)

        report(".tdm data not all zeros", not all_zeros,
               f"Sample of {len(values)} bytes: min={min(values)}, max={max(values)}, unique={len(unique_values)} values")
        report(".tdm data not all 128", not all_128,
               f"Value distribution: most common = {max(set(values), key=values.count)}")


# ============================================================
# 6. ForestMaps/CCRR.for
# ============================================================
section("6. ForestMaps/CCRR.for - Per-patch forest masks")

fm_dir = os.path.join(LANDSCAPE_DIR, "ForestMaps")
expected_for_size = 512 * 512  # 262,144

if not os.path.isdir(fm_dir):
    report("ForestMaps/ directory exists", False)
else:
    expected_files = []
    for cc in range(PATCH_COLS):
        for rr in range(PATCH_ROWS):
            expected_files.append(f"{cc:02d}{rr:02d}.for")

    actual_files = [f for f in os.listdir(fm_dir) if f.endswith(".for")]
    report("ForestMaps file count", len(actual_files) == NUM_PATCHES,
           f"Expected {NUM_PATCHES}, found {len(actual_files)}")

    missing = set(expected_files) - set(actual_files)
    if missing:
        report("ForestMaps no missing files", False,
               f"Missing: {sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}")
    else:
        report("ForestMaps no missing files", True)

    # Check sizes
    wrong_sizes = []
    for fn in actual_files:
        fp = os.path.join(fm_dir, fn)
        sz = os.path.getsize(fp)
        if sz != expected_for_size:
            wrong_sizes.append((fn, sz))
    report("ForestMaps all files 262,144 bytes", len(wrong_sizes) == 0,
           f"{len(wrong_sizes)} files with wrong size" + (f": {wrong_sizes[:5]}" if wrong_sizes else ""))

    # Check values: should contain 0/1/2, not all zeros
    all_zero_count = 0
    has_forest_count = 0
    invalid_values = set()
    sampled = 0
    for fn in actual_files:
        fp = os.path.join(fm_dir, fn)
        with open(fp, "rb") as f:
            data = f.read()
        vals = set(data)
        if vals == {0}:
            all_zero_count += 1
        else:
            has_forest_count += 1
        # Check for values outside 0/1/2
        bad = vals - {0, 1, 2}
        if bad:
            invalid_values.update(bad)
        sampled += 1

    report("ForestMaps not all empty", has_forest_count > 0,
           f"{has_forest_count} files with forest data, {all_zero_count} empty (all zeros)")
    report("ForestMaps valid values (0/1/2)", len(invalid_values) == 0,
           f"Invalid values found: {sorted(invalid_values)[:20]}" if invalid_values else "All values are 0, 1, or 2")


# ============================================================
# 7. .tha and .fha - Hash files
# ============================================================
section("7. Hash files (.tha and .fha)")

for ext, label in [("tha", "terrain hash"), ("fha", "forest hash")]:
    hash_path = os.path.join(LANDSCAPE_DIR, f"{NAME}.{ext}")
    if not os.path.exists(hash_path):
        report(f".{ext} exists", False, f"File not found: {hash_path}")
        continue

    with open(hash_path, "r") as f:
        lines = f.readlines()

    non_empty = [l.strip() for l in lines if l.strip()]
    report(f".{ext} has entries", len(non_empty) >= NUM_PATCHES,
           f"Expected at least {NUM_PATCHES} entries, found {len(non_empty)}")

    # Check for non-zero hash values
    has_nonzero = False
    zero_count = 0
    for line in non_empty:
        parts = line.split()
        if len(parts) >= 2:
            try:
                val = int(parts[1])
                if val != 0:
                    has_nonzero = True
                else:
                    zero_count += 1
            except ValueError:
                pass

    report(f".{ext} has non-zero hash values", has_nonzero,
           f"Found {zero_count} zero-value entries out of {len(non_empty)}")

    # Show a few sample lines
    for line in non_empty[:3]:
        print(f"         Sample: {line}")


# ============================================================
# 8. MacedoniaSkopje.ini
# ============================================================
section("8. MacedoniaSkopje.ini - Landscape options")

ini_path = os.path.join(LANDSCAPE_DIR, f"{NAME}.ini")

if not os.path.exists(ini_path):
    report(".ini exists", False, f"File not found: {ini_path}")
else:
    with open(ini_path, "r") as f:
        ini_content = f.read()

    report(".ini file exists", True, f"{len(ini_content)} bytes")
    report(".ini contains Version", "Version" in ini_content,
           f"Content:\n{ini_content.strip()}")
    report(".ini contains RealtimeShading", "RealtimeShading" in ini_content)


# ============================================================
# 9. Loading screens
# ============================================================
section("9. Loading screens (BMP files)")

loading_files = ["Loading01.bmp", "Loading02.bmp", "Loading03.bmp"]
for fn in loading_files:
    fp = os.path.join(LANDSCAPE_DIR, fn)
    if os.path.exists(fp):
        sz = os.path.getsize(fp)
        report(f"{fn} exists", True, f"Size: {sz:,} bytes")
    else:
        report(f"{fn} exists", False, f"Not found at {fp}")


# ============================================================
# 10. Directory structure
# ============================================================
section("10. Directory structure")

required_dirs = [
    "HeightMaps",
    "Textures",
    "ForestMaps",
    "Images",
    "World/Objects",
    "World/Textures",
    "Airports",
    "Working",
]

for d in required_dirs:
    dp = os.path.join(LANDSCAPE_DIR, d)
    exists = os.path.isdir(dp)
    if exists:
        contents = os.listdir(dp)
        report(f"Directory {d}/", True, f"Contains {len(contents)} items")
    else:
        report(f"Directory {d}/", False, f"Not found: {dp}")


# ============================================================
# Summary
# ============================================================
section("VERIFICATION SUMMARY")

pass_count = sum(1 for _, s, _ in results if s == "PASS")
fail_count = sum(1 for _, s, _ in results if s == "FAIL")
total = len(results)

print(f"\n  Total checks: {total}")
print(f"  Passed: {pass_count}")
print(f"  Failed: {fail_count}")
print()

if fail_count > 0:
    print("  FAILED CHECKS:")
    for item, status, detail in results:
        if status == "FAIL":
            print(f"    [FAIL] {item}")
            if detail:
                for line in detail.strip().split("\n"):
                    print(f"           {line}")
    print()

if fail_count == 0:
    print("  ALL CHECKS PASSED - Phase 1 landscape is correctly structured.")
else:
    print(f"  {fail_count} issue(s) found - review the failures above.")
