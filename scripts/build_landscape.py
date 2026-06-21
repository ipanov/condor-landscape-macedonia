#!/usr/bin/env python3
r"""
GENERIC Condor-2 landscape build orchestrator -- ONE entry point for ANY landscape.

    CONDOR_LANDSCAPE=nm python scripts/build_landscape.py            # full NM, metadata
    python scripts/build_landscape.py                               # skopje, metadata
    CONDOR_LANDSCAPE=nm python scripts/build_landscape.py --with-textures --with-forest

It runs EVERY pipeline step in the correct dependency order for the landscape
selected by CONDOR_LANDSCAPE, so a human or AI gets a COMPLETE landscape with
nothing silently missed. Each step is:
  * idempotent + resumable  -- skipped when its output already exists and is FRESH
    (newer than its inputs); re-run with --force or target it with --only/--from.
  * clearly logged          -- every step prints RUN / SKIP (fresh) / GATED.

Dependency order (Condor needs all of these to load+fly):

    dem -> trn -> tr3 -> flatten-runways -> re-tr3 -> apt -> cup -> tdm -> bmp
        -> textures -> forest -> water-bake -> hash -> verify

HEAVY stages take hours and are GATED behind explicit flags, so the DEFAULT run
is the FAST "metadata" subset (trn, tr3, apt, cup, tdm, bmp, hash, verify):

    --with-dem        rebuild the 30 m DEM from Copernicus GLO-30 (download, slow)
    --with-textures   build per-patch ortho DDS + water-bake (hours; needs ortho)
    --with-forest     download forest rasters + build per-patch .for (slow)
    --with-all        = --with-dem --with-textures --with-forest

flatten-runways + the re-tr3 it requires run by default (cheap, and tow/winch
starts need the flattened mesh). The hash step (LandscapeEditor.exe -hash, the
one safe headless CLT CLI) re-stamps .tha/.fha after any .tr3/.for change.

Resumability / selection:
    --only STEP[,STEP...]   run only these steps
    --from STEP             run from this step to the end
    --skip STEP[,STEP...]   skip these steps
    --force                 ignore freshness; re-run selected steps
    --list                  print the resolved plan and exit (no work)
    --dry-run               print the commands that WOULD run, then exit

The orchestrator shells each step out as a subprocess with CONDOR_LANDSCAPE in the
environment, so every grid-driven child script targets the right landscape and
the build stays deterministic. Texture/water-bake route to the per-landscape
script (skopje build_patch_textures/bake_water; nm nm_build_textures/nm_bake_water).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import condor_grid as g  # honours CONDOR_LANDSCAPE

NAME = g.LANDSCAPE_NAME
_NM = NAME == "NorthMacedonia"
SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
DEM_DIR = ROOT / "sources" / "dem"
LS_DIR = Path(f"C:/Condor2/Landscapes/{NAME}")
PY = sys.executable

PATCHES = g.PATCHES_X * g.PATCHES_Y

# --------------------------------------------------------------------------- #
# Per-landscape file handles used for freshness decisions.
# --------------------------------------------------------------------------- #
if _NM:
    DEM_RAW = DEM_DIR / f"northmacedonia_dem_30m_{g.WIDTH}x{g.HEIGHT}.raw"
    DEM_FLAT = DEM_DIR / f"northmacedonia_dem_30m_{g.WIDTH}x{g.HEIGHT}_flat.raw"
    TEXTURE_SCRIPT = "nm_build_textures.py"
    WATER_SCRIPT = "nm_bake_water.py"
else:
    DEM_RAW = DEM_DIR / "macedonia_skopje_dem_30m_2305.raw"
    DEM_FLAT = DEM_DIR / "macedonia_skopje_dem_30m_2305_flat.raw"
    TEXTURE_SCRIPT = "build_patch_textures.py"
    WATER_SCRIPT = "bake_water.py"

TRN = LS_DIR / f"{NAME}.trn"
APT = LS_DIR / f"{NAME}.apt"
CUP = LS_DIR / f"{NAME}.cup"
TDM = LS_DIR / f"{NAME}.tdm"
BMP = LS_DIR / f"{NAME}.bmp"
THA = LS_DIR / f"{NAME}.tha"
FHA = LS_DIR / f"{NAME}.fha"
HM_DIR = LS_DIR / "HeightMaps"
TEX_DIR = LS_DIR / "Textures"
FOR_DIR = LS_DIR / "ForestMaps"
HASH_EXE = ROOT / "tools" / "CLT2.7" / "LandscapeEditor.exe"


# --------------------------------------------------------------------------- #
# Freshness helpers
# --------------------------------------------------------------------------- #
def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else -1.0


def _newest(paths) -> float:
    return max((_mtime(p) for p in paths), default=-1.0)


def _count(dir_: Path, suffix: str) -> int:
    return len(list(dir_.glob(f"*{suffix}"))) if dir_.is_dir() else 0


def _outputs_fresh(outs, deps) -> bool:
    """True if every output exists and is at least as new as every dep."""
    outs = [Path(o) for o in outs]
    if not outs or any(not o.exists() for o in outs):
        return False
    dep_t = _newest([Path(d) for d in deps]) if deps else -1.0
    return min(_mtime(o) for o in outs) >= dep_t - 1.0


# --------------------------------------------------------------------------- #
# Step definitions
# --------------------------------------------------------------------------- #
class Step:
    def __init__(self, name, argv, is_fresh, heavy=False, gate=None, desc=""):
        self.name = name
        self.argv = argv                 # list[str] OR callable -> list[str]
        self.is_fresh = is_fresh         # callable -> bool
        self.heavy = heavy               # gated unless its gate flag is set
        self.gate = gate                 # attribute name on args, e.g. "with_textures"
        self.desc = desc

    def resolved_argv(self):
        return self.argv() if callable(self.argv) else self.argv


def _hash_fresh() -> bool:
    """.tha/.fha exist and are newer than the newest .tr3/.for."""
    if not (THA.exists() and FHA.exists()):
        return False
    newest_src = max(_newest(HM_DIR.glob("*.tr3")), _newest(FOR_DIR.glob("*.for")))
    return min(_mtime(THA), _mtime(FHA)) >= newest_src - 1.0


def build_steps():
    """Return the ordered list of Steps for the selected landscape."""
    return [
        Step("dem",
             [PY, str(SCRIPTS / "build_dem.py")],
             lambda: DEM_RAW.exists(),
             heavy=True, gate="with_dem",
             desc="Copernicus GLO-30 -> exactly-30 m raw"),

        Step("trn",
             [PY, str(SCRIPTS / "generate_trn.py")],
             lambda: _outputs_fresh([TRN], [DEM_RAW]),
             desc="90 m overview .trn (patches*64)"),

        Step("tr3",
             [PY, str(SCRIPTS / "generate_tr3.py")],
             lambda: _count(HM_DIR, ".tr3") == PATCHES and _outputs_fresh(
                 [max(HM_DIR.glob("*.tr3"), key=_mtime)] if _count(HM_DIR, ".tr3") else [],
                 [DEM_RAW]),
             desc="per-patch 30 m .tr3 (base mesh)"),

        Step("flatten-runways",
             [PY, str(SCRIPTS / "flatten_runways.py")],
             lambda: _outputs_fresh([DEM_FLAT], [DEM_RAW]),
             desc="flatten runway plateaus in the DEM"),

        Step("re-tr3",
             [PY, str(SCRIPTS / "generate_tr3.py"), "--source", str(DEM_FLAT)],
             lambda: _count(HM_DIR, ".tr3") == PATCHES and _outputs_fresh(
                 [max(HM_DIR.glob("*.tr3"), key=_mtime)] if _count(HM_DIR, ".tr3") else [],
                 [DEM_FLAT]),
             desc="re-extract .tr3 from the FLATTENED DEM (tow/winch starts)"),

        Step("apt",
             [PY, str(SCRIPTS / "generate_apt.py")],
             lambda: _outputs_fresh([APT], []),  # cheap; rebuild if absent
             desc="binary .apt airports"),

        Step("cup",
             [PY, str(SCRIPTS / "generate_cup.py")],
             lambda: _outputs_fresh([CUP], [APT, TRN]),
             desc=".cup turnpoints (FRESH vs .apt/.trn)"),

        Step("tdm",
             [PY, str(SCRIPTS / "generate_tdm.py")],
             lambda: _outputs_fresh([TDM], [DEM_FLAT, DEM_RAW]),
             desc="thermal map .tdm (dims == .trn)"),

        Step("bmp",
             [PY, str(SCRIPTS / "generate_flight_planner_map.py")],
             lambda: _outputs_fresh([BMP], [TRN, APT]),
             desc="flight-planner .bmp (dims == .trn; FRESH vs .trn/.apt)"),

        Step("textures",
             [PY, str(SCRIPTS / TEXTURE_SCRIPT)],
             lambda: _count(TEX_DIR, ".dds") >= PATCHES,
             heavy=True, gate="with_textures",
             desc=f"per-patch ortho DDS ({TEXTURE_SCRIPT})"),

        Step("forest",
             [PY, str(SCRIPTS / "generate_forest_maps.py")],
             lambda: _count(FOR_DIR, ".for") == PATCHES,
             heavy=True, gate="with_forest",
             desc="per-patch forest .for (satellite canopy)"),

        Step("water-bake",
             [PY, str(SCRIPTS / WATER_SCRIPT)],
             # No standalone output; treated as part of the textures stage. Only
             # runs when textures are being (re)built, else skipped as fresh.
             lambda: _count(TEX_DIR, ".dds") >= PATCHES,
             heavy=True, gate="with_textures",
             desc=f"bake OSM water into shoreline DDS ({WATER_SCRIPT})"),

        Step("hash",
             [str(HASH_EXE), "-hash", NAME],
             _hash_fresh,
             desc="re-stamp .tha/.fha (LandscapeEditor -hash; headless)"),

        Step("verify",
             [PY, str(SCRIPTS / "verify_landscape.py")],
             lambda: False,  # always run the gate at the end
             desc="HARD completeness+freshness gate"),
    ]


# --------------------------------------------------------------------------- #
# Plan resolution + execution
# --------------------------------------------------------------------------- #
def resolve_plan(steps, args):
    names = [s.name for s in steps]

    def idx(n):
        if n not in names:
            sys.exit(f"unknown step '{n}'. valid: {', '.join(names)}")
        return names.index(n)

    selected = list(steps)
    if args.only:
        want = {x.strip() for x in args.only.split(",")}
        for w in want:
            idx(w)
        selected = [s for s in steps if s.name in want]
    elif getattr(args, "from_"):
        selected = steps[idx(args.from_):]
    if args.skip:
        drop = {x.strip() for x in args.skip.split(",")}
        for d in drop:
            idx(d)
        selected = [s for s in selected if s.name not in drop]
    return selected


def run(args) -> int:
    steps = build_steps()
    plan = resolve_plan(steps, args)

    print("=" * 74)
    print(f"  BUILD LANDSCAPE: {NAME}   ({g.PATCHES_X}x{g.PATCHES_Y} = {PATCHES} patches)")
    print(f"  Install: {LS_DIR}")
    gates = [g_ for g_ in ("with_dem", "with_textures", "with_forest") if getattr(args, g_)]
    print(f"  Heavy stages enabled: {', '.join(gates) if gates else '(none -- FAST metadata run)'}")
    print(f"  Steps: {' -> '.join(s.name for s in plan)}")
    print("=" * 74)

    if args.list:
        print("\n  RESOLVED PLAN:")
        for s in plan:
            tag = "HEAVY" if s.heavy else "     "
            print(f"    [{tag}] {s.name:16s} {s.desc}")
        return 0

    env = dict(os.environ)
    env["CONDOR_LANDSCAPE"] = os.environ.get("CONDOR_LANDSCAPE", "skopje")
    env["PYTHONPATH"] = str(SCRIPTS) + os.pathsep + env.get("PYTHONPATH", "")

    ran, skipped, gated = [], [], []
    for s in plan:
        # Gate heavy stages behind their flag.
        if s.heavy and s.gate and not getattr(args, s.gate):
            print(f"\n[GATED] {s.name:16s} -- needs --{s.gate.replace('_','-')}  ({s.desc})")
            gated.append(s.name)
            continue
        # Idempotent skip (unless forced or it's the always-run verify).
        if not args.force and s.name != "verify":
            try:
                if s.is_fresh():
                    print(f"\n[SKIP ] {s.name:16s} -- output fresh  ({s.desc})")
                    skipped.append(s.name)
                    continue
            except Exception as exc:  # freshness probe must never abort the build
                print(f"\n[WARN ] {s.name}: freshness check error ({exc}); running anyway")

        argv = s.resolved_argv()
        if s.name == "verify" and args.with_textures and args.with_forest:
            pass  # full gate
        elif s.name == "verify" and not (args.with_textures and args.with_forest):
            argv = argv + ["--metadata-only"]  # don't demand un-built heavy artifacts

        print(f"\n[RUN  ] {s.name:16s} {' '.join(str(a) for a in argv)}")
        if args.dry_run:
            ran.append(s.name)
            continue
        if s.name == "hash" and not HASH_EXE.exists():
            print(f"[FAIL ] hash: {HASH_EXE} not found")
            return 1
        t0 = time.time()
        proc = subprocess.run(argv, env=env, cwd=str(ROOT))
        dt = time.time() - t0
        if proc.returncode != 0:
            print(f"[FAIL ] {s.name} exited {proc.returncode} after {dt:.0f}s -- STOPPING")
            print(f"\n  ran: {ran}\n  skipped: {skipped}\n  gated: {gated}")
            return proc.returncode
        print(f"[DONE ] {s.name} in {dt:.0f}s")
        ran.append(s.name)

    print("\n" + "=" * 74)
    print(f"  BUILD COMPLETE: {NAME}")
    print(f"  ran:     {ran}")
    print(f"  skipped: {skipped} (already fresh)")
    print(f"  gated:   {gated} (heavy; enable with --with-*)")
    print("=" * 74)
    if gated:
        print("  NOTE: gated heavy stages were NOT built. For a landscape that is "
              "complete enough to FLY with textures+forest, re-run with "
              "--with-textures --with-forest (hours).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--with-dem", action="store_true", help="rebuild the 30 m DEM (slow download)")
    ap.add_argument("--with-textures", action="store_true", help="build ortho DDS + water-bake (hours)")
    ap.add_argument("--with-forest", action="store_true", help="build forest .for (slow)")
    ap.add_argument("--with-all", action="store_true", help="= --with-dem --with-textures --with-forest")
    ap.add_argument("--only", help="run only these comma-separated steps")
    ap.add_argument("--from", dest="from_", metavar="STEP", help="run from this step to the end")
    ap.add_argument("--skip", help="skip these comma-separated steps")
    ap.add_argument("--force", action="store_true", help="ignore freshness; re-run selected steps")
    ap.add_argument("--list", action="store_true", help="print the resolved plan and exit")
    ap.add_argument("--dry-run", action="store_true", help="print commands that would run, then exit")
    args = ap.parse_args()
    if args.with_all:
        args.with_dem = args.with_textures = args.with_forest = True
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
