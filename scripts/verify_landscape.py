#!/usr/bin/env python3
"""
HARD completeness + freshness gate for a Condor 2 landscape.

This is the single definition of "done": for the landscape selected by
CONDOR_LANDSCAPE (skopje default, nm = NorthMacedonia) it REQUIRES every file
Condor needs to LOAD and FLY the landscape, and FAILS (non-zero exit) -- never
silently skips -- on any file that is MISSING or STALE.

Checked artifacts (all mandatory unless --metadata-only):
  .ini                     landscape options
  .trn                     90 m overview heightmap (patches*64 per side)
  HeightMaps/h*.tr3        per-patch 30 m mesh, count == patches^2, 74,498 B each
  .apt                     airports (72 B/record, count == data airports)
  .cup                     turnpoints (>= #airports rows; FRESH vs .apt/.trn)
  .tdm                     thermal map, dims == .trn         (FRESH vs .trn)
  .bmp                     flight-planner map, dims == .trn  (FRESH vs .trn/.apt)
  .obj                     object placements (0 B allowed = no objects yet)
  Textures/t*.dds          per-patch textures, count == patches^2, 2048^2, DXT1/3
  Textures/empty.dds       2048^2
  ForestMaps/*.for         per-patch forest, count == patches^2, 262,144 B each
  .tha / .fha              terrain/forest hashes, entries >= patches^2 (FRESH vs
                           .tr3 / .for)
  Images/*.jpg             >= 1 loading-screen image

FRESHNESS (the bug this gate exists to catch): a map-area or airport change that
isn't followed by regenerating the dependent files leaves a landscape that loads
the OLD map. So:
  * .bmp / .tdm  must be >= as new as the .trn  (map area changed -> regen)
  * .bmp / .cup  must be >= as new as the .apt  (airports changed -> regen)
  * .tha         must be >= as new as the newest .tr3
  * .fha         must be >= as new as the newest .for
A stale file is a FAIL with a clear "regenerate X" message.

Usage:
  python scripts/verify_landscape.py                 # skopje, full gate
  CONDOR_LANDSCAPE=nm python scripts/verify_landscape.py
  CONDOR_LANDSCAPE=nm python scripts/verify_landscape.py --metadata-only
      # metadata subset only (ini/trn/tr3/apt/cup/tdm/bmp/obj/hashes/images);
      # skips the heavy .dds/.for so a fast 'metadata' build can still be gated.

Exit code 0 = every required artifact present, correctly sized, and fresh.
Exit code 1 = at least one missing / wrong / stale -> NOT done.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import condor_grid as g  # honours CONDOR_LANDSCAPE

NAME = g.LANDSCAPE_NAME
LS_DIR = Path(f"C:/Condor2/Landscapes/{NAME}")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

PATCH_COLS = g.PATCHES_X
PATCH_ROWS = g.PATCHES_Y
NUM_PATCHES = PATCH_COLS * PATCH_ROWS
TRN_W = PATCH_COLS * 64
TRN_H = PATCH_ROWS * 64

_NM = NAME == "NorthMacedonia"
AIRPORTS_JSON = PROJECT_ROOT / "data" / ("airports_nm.json" if _NM else "airports.json")
with open(AIRPORTS_JSON, encoding="utf-8") as _f:
    NUM_AIRPORTS = len(json.load(_f)["airports"])

# Expected exact byte sizes for fixed-format per-patch files.
TR3_BYTES = 193 * 193 * 2          # 74,498
FOR_BYTES = 512 * 512              # 262,144
TRN_BYTES = 36 + TRN_W * TRN_H * 2
TDM_BYTES = 8 + TRN_W * TRN_H
APT_BYTES = NUM_AIRPORTS * 72


# --------------------------------------------------------------------------- #
# Result tracking
# --------------------------------------------------------------------------- #
_FAILS: list[str] = []
_OKS: list[str] = []


def ok(item: str, detail: str = "") -> None:
    _OKS.append(item)
    line = f"  [ OK ] {item}"
    if detail:
        line += f"  ({detail})"
    print(line)


def fail(item: str, detail: str = "") -> None:
    _FAILS.append(item)
    line = f"  [FAIL] {item}"
    if detail:
        line += f"  -> {detail}"
    print(line)


def check(item: str, condition: bool, detail: str = "") -> bool:
    (ok if condition else fail)(item, detail)
    return condition


def section(title: str) -> None:
    print(f"\n{'-'*72}\n  {title}\n{'-'*72}")


def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else -1.0


def fresh(item: str, target: Path, *deps: Path) -> None:
    """FAIL if `target` is older than any existing dependency in `deps`."""
    if not target.exists():
        return  # existence is checked separately; don't double-report
    t = _mtime(target)
    stale_against = [d for d in deps if d.exists() and _mtime(d) > t + 1.0]
    if stale_against:
        names = ", ".join(d.name for d in stale_against)
        fail(f"{item} is FRESH (newer than {names})",
             f"{target.name} is STALE -> regenerate it (older than {names})")
    else:
        dep_names = ", ".join(d.name for d in deps if d.exists()) or "(no deps present)"
        ok(f"{item} is FRESH", f"newer than {dep_names}")


def _dds_dims_fourcc(path: Path):
    with open(path, "rb") as f:
        hdr = f.read(128)
    if hdr[:4] != b"DDS ":
        return None, None, "no-magic"
    h, w = struct.unpack_from("<ii", hdr, 12)
    fcc = hdr[84:88].decode("latin-1", "replace")
    return w, h, fcc


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_ini() -> None:
    section(".ini  (landscape options)")
    p = LS_DIR / f"{NAME}.ini"
    if not check(".ini exists", p.exists(), str(p)):
        return
    txt = p.read_text(errors="replace")
    check(".ini has Version", "Version" in txt)


def check_trn() -> None:
    section(".trn  (90 m overview heightmap)")
    p = LS_DIR / f"{NAME}.trn"
    if not check(".trn exists", p.exists(), str(p)):
        return
    sz = p.stat().st_size
    check(".trn size", sz == TRN_BYTES, f"{sz:,} B (expected {TRN_BYTES:,})")
    hdr = p.read_bytes()[:36]
    w, h = struct.unpack_from("<ii", hdr, 0)
    sx, sy, sz_ = struct.unpack_from("<fff", hdr, 8)
    check(".trn dims == patches*64", (w, h) == (TRN_W, TRN_H),
          f"{w}x{h} (expected {TRN_W}x{TRN_H})")
    check(".trn spacing floats (90,-90,90)", (sx, sy, sz_) == (90.0, -90.0, 90.0),
          f"({sx},{sy},{sz_})")


def check_tr3() -> None:
    section("HeightMaps/h*.tr3  (per-patch 30 m mesh)")
    d = LS_DIR / "HeightMaps"
    if not check("HeightMaps/ exists", d.is_dir(), str(d)):
        return
    files = [f for f in os.listdir(d) if f.endswith(".tr3")]
    check(f".tr3 count == {NUM_PATCHES}", len(files) == NUM_PATCHES,
          f"found {len(files)}")
    expected = {f"h{c:02d}{r:02d}.tr3" for c in range(PATCH_COLS) for r in range(PATCH_ROWS)}
    missing = expected - set(files)
    check(".tr3 no missing patches", not missing,
          f"{len(missing)} missing e.g. {sorted(missing)[:5]}" if missing else "")
    bad = [f for f in files if (d / f).stat().st_size != TR3_BYTES]
    check(f".tr3 all {TR3_BYTES:,} B", not bad,
          f"{len(bad)} wrong-size e.g. {bad[:5]}" if bad else "")


def check_apt() -> None:
    section(".apt  (airports)")
    p = LS_DIR / f"{NAME}.apt"
    if not check(".apt exists", p.exists(), str(p)):
        return
    sz = p.stat().st_size
    # 72 B/record. Require a clean multiple and at least the declared airports;
    # an EXTRA record (e.g. a ZDebug autogen airport) is tolerated, a MISSING one
    # is not (Condor needs every real airport's strip).
    check(".apt size is N*72", sz % 72 == 0, f"{sz} B (not a multiple of 72)")
    records = sz // 72
    check(f".apt has >= {NUM_AIRPORTS} airports", records >= NUM_AIRPORTS,
          f"{records} records (expected >= {NUM_AIRPORTS} = data airports; "
          f"{APT_BYTES} B exact, extras allowed)")


def check_cup() -> None:
    section(".cup  (turnpoints)")
    p = LS_DIR / f"{NAME}.cup"
    if not check(".cup exists", p.exists(), str(p)):
        return
    lines = [l for l in p.read_text(encoding="latin-1").splitlines()
             if l.strip() and not l.startswith("---")]
    rows = lines[1:] if lines else []
    check(".cup has airport rows", len(rows) >= NUM_AIRPORTS,
          f"{len(rows)} data rows (expected >= {NUM_AIRPORTS} airports)")
    fresh(".cup", p, LS_DIR / f"{NAME}.apt", LS_DIR / f"{NAME}.trn")


def check_tdm() -> None:
    section(".tdm  (thermal map)")
    p = LS_DIR / f"{NAME}.tdm"
    if not check(".tdm exists", p.exists(), str(p)):
        return
    sz = p.stat().st_size
    check(".tdm size (dims == .trn)", sz == TDM_BYTES,
          f"{sz:,} B (expected {TDM_BYTES:,} for {TRN_W}x{TRN_H})")
    w, h = struct.unpack("<ii", p.read_bytes()[:8])
    check(".tdm header dims == .trn", (w, h) == (TRN_W, TRN_H),
          f"{w}x{h} (expected {TRN_W}x{TRN_H})")
    fresh(".tdm", p, LS_DIR / f"{NAME}.trn")


def check_bmp() -> None:
    section(".bmp  (flight-planner map)")
    p = LS_DIR / f"{NAME}.bmp"
    if not check(".bmp exists", p.exists(), str(p)):
        return
    hdr = p.read_bytes()[:54]
    w, h = struct.unpack_from("<ii", hdr, 18)
    check(".bmp dims == .trn", (w, abs(h)) == (TRN_W, TRN_H),
          f"{w}x{abs(h)} (expected {TRN_W}x{TRN_H})")
    fresh(".bmp", p, LS_DIR / f"{NAME}.trn", LS_DIR / f"{NAME}.apt")


def check_obj() -> None:
    section(".obj  (object placements)")
    p = LS_DIR / f"{NAME}.obj"
    # 0-byte .obj is valid (no objects yet) -- matches the shipping MacedoniaSkopje
    # reference which Condor loads fine. Only the FILE must exist.
    check(".obj exists (0 B = no objects yet, allowed)", p.exists(),
          f"{p.stat().st_size} B" if p.exists() else str(p))


def check_textures() -> None:
    section("Textures/t*.dds  (per-patch textures)")
    d = LS_DIR / "Textures"
    if not check("Textures/ exists", d.is_dir(), str(d)):
        return
    files = [f for f in os.listdir(d) if f.startswith("t") and f.endswith(".dds")]
    check(f".dds count == {NUM_PATCHES}", len(files) == NUM_PATCHES,
          f"found {len(files)}")
    expected = {f"t{c:02d}{r:02d}.dds" for c in range(PATCH_COLS) for r in range(PATCH_ROWS)}
    missing = expected - set(files)
    check(".dds no missing patches", not missing,
          f"{len(missing)} missing e.g. {sorted(missing)[:5]}" if missing else "")
    bad_dim, fcc_hist = [], {}
    for f in files:
        w, h, fcc = _dds_dims_fourcc(d / f)
        if (w, h) != (2048, 2048):
            bad_dim.append((f, f"{w}x{h}"))
        fcc_hist[fcc] = fcc_hist.get(fcc, 0) + 1
    check(".dds all 2048x2048", not bad_dim,
          f"{len(bad_dim)} wrong e.g. {bad_dim[:3]}" if bad_dim else "")
    check(".dds DXT1/DXT3 only", set(fcc_hist) <= {"DXT1", "DXT3"}, f"{fcc_hist}")
    empty = d / "empty.dds"
    if check("empty.dds exists", empty.exists()):
        w, h, _ = _dds_dims_fourcc(empty)
        check("empty.dds 2048x2048", (w, h) == (2048, 2048), f"{w}x{h}")


def check_forest() -> None:
    section("ForestMaps/*.for  (per-patch forest masks)")
    d = LS_DIR / "ForestMaps"
    if not check("ForestMaps/ exists", d.is_dir(), str(d)):
        return
    files = [f for f in os.listdir(d) if f.endswith(".for")]
    check(f".for count == {NUM_PATCHES}", len(files) == NUM_PATCHES,
          f"found {len(files)}")
    expected = {f"{c:02d}{r:02d}.for" for c in range(PATCH_COLS) for r in range(PATCH_ROWS)}
    missing = expected - set(files)
    check(".for no missing patches", not missing,
          f"{len(missing)} missing e.g. {sorted(missing)[:5]}" if missing else "")
    bad = [f for f in files if (d / f).stat().st_size != FOR_BYTES]
    check(f".for all {FOR_BYTES:,} B", not bad,
          f"{len(bad)} wrong-size e.g. {bad[:5]}" if bad else "")


def check_hashes(metadata_only: bool) -> None:
    section(".tha / .fha  (terrain / forest hashes)")
    hm = LS_DIR / "HeightMaps"
    fm = LS_DIR / "ForestMaps"
    newest_tr3 = max((hm.glob("*.tr3")), key=lambda p: p.stat().st_mtime, default=None)
    newest_for = max((fm.glob("*.for")), key=lambda p: p.stat().st_mtime, default=None)

    for ext, label, dep in (("tha", "terrain", newest_tr3), ("fha", "forest", newest_for)):
        p = LS_DIR / f"{NAME}.{ext}"
        if not check(f".{ext} exists", p.exists(), str(p)):
            continue
        entries = [l for l in p.read_text(errors="replace").splitlines() if l.strip()]
        check(f".{ext} entries >= {NUM_PATCHES}", len(entries) >= NUM_PATCHES,
              f"{len(entries)} entries")
        # Hash must be at least as new as the newest mesh/forest file it covers.
        if dep is not None:
            fresh(f".{ext}", p, dep)


def check_images() -> None:
    section("Images/  (loading screens)")
    d = LS_DIR / "Images"
    if not check("Images/ exists", d.is_dir(), str(d)):
        return
    jpgs = [f for f in os.listdir(d) if f.lower().endswith((".jpg", ".jpeg"))]
    check("Images/ has >= 1 jpg", len(jpgs) >= 1, f"{len(jpgs)} jpg(s): {jpgs[:5]}")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metadata-only", action="store_true",
                    help="check only the metadata subset (skip heavy .dds/.for); "
                         "use to gate a fast metadata-only build")
    args = ap.parse_args()

    print("=" * 72)
    print(f"  VERIFY LANDSCAPE: {NAME}   ({PATCH_COLS}x{PATCH_ROWS} = {NUM_PATCHES} "
          f"patches, .trn {TRN_W}x{TRN_H}, {NUM_AIRPORTS} airports)")
    print(f"  Install dir: {LS_DIR}")
    print(f"  Mode: {'METADATA-ONLY' if args.metadata_only else 'FULL (load+fly)'}")
    print("=" * 72)

    if not LS_DIR.is_dir():
        print(f"\n  [FAIL] install dir not found: {LS_DIR}")
        print("\n  RESULT: FAIL -- landscape not installed.")
        return 1

    check_ini()
    check_trn()
    check_tr3()
    check_apt()
    check_cup()
    check_tdm()
    check_bmp()
    check_obj()
    if not args.metadata_only:
        check_textures()
        check_forest()
    else:
        section("Textures/ + ForestMaps/  (SKIPPED: --metadata-only)")
        print("  (heavy .dds/.for not required in metadata-only mode)")
    check_hashes(args.metadata_only)
    check_images()

    # ---- summary / checklist ----
    print("\n" + "=" * 72)
    print(f"  COMPLETENESS CHECKLIST -- {NAME}")
    print("=" * 72)
    print(f"  Passed: {len(_OKS)}    Failed: {len(_FAILS)}")
    if _FAILS:
        print("\n  MISSING / WRONG / STALE (landscape is NOT done):")
        for item in _FAILS:
            print(f"    [FAIL] {item}")
        print("\n  RESULT: FAIL -- regenerate the failed artifacts "
              "(CONDOR_LANDSCAPE=" + os.environ.get("CONDOR_LANDSCAPE", "skopje") +
              " python scripts/build_landscape.py ...), then re-verify.")
        return 1

    print("\n  RESULT: PASS -- every file Condor needs to load+fly is present, "
          "correctly sized, and fresh.")
    if not args.metadata_only:
        print("  NOTE: 'done' also requires opening the flight planner in-sim and "
              "confirming the map renders (see CLAUDE.md / docs/PIPELINES.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
