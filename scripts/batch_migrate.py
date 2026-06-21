#!/usr/bin/env python3
r"""
batch_migrate.py — fast, PARALLEL, manifest-driven BULK migration of custom 3D
objects (glTF / GLB / SKP / OBJ) into full-detail, baked-texture Condor 2 ``.c3d``
objects, using ALL CPU cores + the GPU.

WHY THIS EXISTS
---------------
The per-object converters already exist and are excellent, but were driven one at a
time (effectively one LLM agent per object — minutes of wall-clock each). The actual
compute per object is only a few seconds (a headless Blender bake + a GPU DXT
compress + a tiny OBJ->C3D pack). The bottleneck was orchestration, not compute. So
this tool keeps the proven converters UNCHANGED and simply runs N of them at once in
a multiprocessing Pool, with a small GPU semaphore so the parallel Blender-Cycles
bakes + nvcompress calls don't thrash the RTX 3070's VRAM. Converting 8 landmarks
drops from ~minutes-per-object serial to one short parallel batch.

PIPELINE PER OBJECT (obeys CLAUDE.md rule #11 — full detail, baked DDS, no
decimation / approximation; round-trip-verified)::

    source (.glb/.gltf/.skp/.obj)
      -> [Blender 5.1 headless]  baked-texture OBJ + <name>_bake.png (2048 atlas)
                                 (scripts/glb_to_baked.py, or a per-object custom
                                  builder e.g. build_cross.py for the gold cross)
      -> [nvcompress -bc1 GPU]   <name>.dds   (2048x2048 DXT1)
      -> [scripts/obj_to_c3d.py] <name>.c3d   (one g-group, byte-exact round-trip)

The converters themselves are the canonical copies in ``scripts/`` (promoted verbatim
from ``.sandbox/landmarks/_work/``, which keep working unchanged). ``glb_to_baked.py``
writes its outputs next to its own ``__file__``; to make that safe under N-way
parallelism this tool copies the baker into each object's PRIVATE work dir before
launching Blender, so concurrent objects never collide.

MANIFEST  (json — a list, or {"objects": [...]})
------------------------------------------------
Each entry::

    {
      "name":   "PortaMacedonia",            # output basename (<name>.c3d / <name>.dds)
      "source": ".sandbox/.../porta.glb",    # already-LOCAL file (download is OOS)
      "kind":   "glb",                       # gltf | glb | skp | obj
      "lat":    41.99360,                    # WGS84 — carried through for placement
      "lon":    21.43360,
      "target": "landmarks",                 # -> .sandbox/<target>/

      # --- optional conversion tuning (sensible defaults applied) ---
      "scale":  "native",                    # 'native' | a number (height m) | 'len:140'
      "orient": "longY",                     # 'longY' (default) | 'none'
      "material": [1,1,1,1,0,0.9],           # the 6 C3D material floats
      "flip_v": false,                       # flip texture V if mirrored in-sim
      "custom_builder": "build_cross.py"     # bespoke Blender builder beside the source
                                             #   (overrides glb_to_baked.py for this obj)
    }

For OBJ sources that are ALREADY baked (an ``<obj>`` next to an ``<obj-stem>_bake.png``
or an explicit ``"texture_png"``) the Blender step is skipped and the existing PNG is
compressed straight to DDS — so a re-baked OBJ from the work dir converts in well
under a second.

OUTPUT
------
``.sandbox/<target>/<name>.c3d`` + ``.sandbox/<target>/<name>.dds`` and an annotated
manifest (``--out-manifest``, default ``<manifest-stem>.out.json``) where each entry
gains ``verts / tris / textured / elapsed_s / status`` (+ ``error`` on failure).

USAGE
-----
    python scripts/batch_migrate.py <manifest.json> [--workers N] [--gpu-slots K]
                                    [--force] [--out-manifest path]
    python scripts/batch_migrate.py --benchmark        # re-convert the 8 staged landmarks

Idempotent / resumable (skip if the .c3d is newer than the source unless --force);
deterministic (the converters are; bake samples are fixed); robust (a failed object
is logged and the batch continues).
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
SANDBOX = REPO / ".sandbox"
WORKROOT = SANDBOX / "batch_migrate_work"

BLENDER = Path(r"C:/Program Files/Blender Foundation/Blender 5.1/blender.exe")
NVCOMPRESS = Path(r"C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe")
GLB_TO_BAKED = SCRIPTS / "glb_to_baked.py"

# Per-object hard timeout for the Blender bake (seconds). A model that wedges
# Blender must not stall the whole batch.
BLENDER_TIMEOUT = 900

sys.path.insert(0, str(SCRIPTS))


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _resolve(p: str | Path) -> Path:
    """Resolve a manifest path: absolute as-is, else relative to the repo root."""
    p = Path(p)
    return p if p.is_absolute() else (REPO / p)


def _newer(a: Path, b: Path) -> bool:
    """True if a exists and is at least as new as b (a is up to date vs source b)."""
    return a.exists() and b.exists() and a.stat().st_mtime >= b.stat().st_mtime


def _png_for_obj(obj_path: Path, explicit: str | None) -> Path | None:
    """Find an already-baked atlas PNG for an OBJ source (skip the Blender step)."""
    if explicit:
        return _resolve(explicit)
    stem = obj_path.with_suffix("")
    for cand in (Path(str(stem) + "_bake.png"), stem.with_suffix(".png")):
        if cand.exists():
            return cand
    return None


# --------------------------------------------------------------------------- #
# the per-object worker (runs in a Pool process)
# --------------------------------------------------------------------------- #
def convert_one(task: dict) -> dict:
    """Convert a single object. Pure-ish: takes a task dict + the shared GPU
    semaphore via the module global ``_GPU_SEM`` (set by the pool initializer).
    Never raises — returns a result dict with status/error so the batch survives
    any single failure.
    """
    name = task["name"]
    t0 = time.time()
    res = {
        "name": name, "status": "error", "error": "", "elapsed_s": 0.0,
        "verts": None, "tris": None, "textured": None,
        "c3d": None, "dds": None,
    }
    try:
        src = _resolve(task["source"])
        kind = task.get("kind", src.suffix.lstrip(".")).lower()
        target = task.get("target", "objects")
        out_dir = SANDBOX / target
        out_dir.mkdir(parents=True, exist_ok=True)
        out_c3d = out_dir / f"{name}.c3d"
        out_dds = out_dir / f"{name}.dds"

        if not src.exists():
            res["error"] = f"source not found: {src}"
            res["elapsed_s"] = round(time.time() - t0, 2)
            return res

        # ---- idempotent / resumable -------------------------------------- #
        if (not task.get("_force")) and _newer(out_c3d, src) and out_dds.exists():
            from c3d import parse_c3d
            f = parse_c3d(out_c3d)
            o = f.objects[0]
            res.update(status="skipped", elapsed_s=round(time.time() - t0, 2),
                       verts=len(o.vertices), tris=len(o.indices) // 3,
                       textured=bool(o.texture), c3d=str(out_c3d), dds=str(out_dds))
            return res

        # ---- per-object private work dir --------------------------------- #
        work = WORKROOT / name
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

        obj_path = work / f"{name}.obj"
        bake_png = work / f"{name}_bake.png"

        # ---- STAGE 1: source -> baked OBJ + atlas PNG -------------------- #
        if kind == "obj":
            # An OBJ source: copy it in; reuse an existing baked PNG if present,
            # else there's nothing to bake (untextured) -> require a texture_png.
            shutil.copy2(src, obj_path)
            existing_png = _png_for_obj(src, task.get("texture_png"))
            if existing_png and existing_png.exists():
                shutil.copy2(existing_png, bake_png)
            else:
                res["error"] = ("obj source has no baked atlas PNG "
                                "(provide 'texture_png' or a sibling *_bake.png)")
                res["elapsed_s"] = round(time.time() - t0, 2)
                return res
        else:
            # glTF / GLB / SKP -> drive a headless Blender baker in the work dir.
            builder = task.get("custom_builder")
            if builder:
                # bespoke builder (e.g. build_cross.py) — it lives beside the source
                # and may read sibling assets (the .skp), so copy the whole builder +
                # the source's sibling files it needs into the work dir, unchanged.
                bsrc = _resolve(builder)
                if not bsrc.exists():
                    bsrc = src.parent / builder
                if not bsrc.exists():
                    res["error"] = f"custom_builder not found: {builder}"
                    res["elapsed_s"] = round(time.time() - t0, 2)
                    return res
                bdst = work / bsrc.name
                shutil.copy2(bsrc, bdst)
                # copy the source itself under the name the builder expects (its
                # own hardcoded sibling filename == the source filename).
                shutil.copy2(src, work / src.name)
                bake_png, obj_path = _run_custom_builder(bdst, work, name)
            else:
                shutil.copy2(GLB_TO_BAKED, work / GLB_TO_BAKED.name)
                scale = str(task.get("scale", "native"))
                orient = str(task.get("orient", "longY"))
                _run_blender_baker(work / GLB_TO_BAKED.name, src, name, scale, orient)

        if not obj_path.exists():
            res["error"] = f"baker produced no OBJ ({obj_path.name})"
            res["elapsed_s"] = round(time.time() - t0, 2)
            return res

        # ---- STAGE 2: atlas PNG -> Condor DDS (GPU DXT1) ----------------- #
        textured = bake_png.exists()
        if textured:
            _nvcompress(bake_png, out_dds)
        else:
            # No atlas (untextured massing): still emit a c3d, with empty texture.
            if out_dds.exists():
                out_dds.unlink()

        # ---- STAGE 3: OBJ + DDS -> .c3d (round-trip verified) ------------ #
        from obj_to_c3d import write_single
        material = tuple(task.get("material", (1.0, 1.0, 1.0, 1.0, 0.0, 0.9)))
        tex_name = out_dds.name if textured else ""
        nverts, ntris = write_single(
            name, str(obj_path), tex_name, str(out_c3d),
            material=material, flip_v=bool(task.get("flip_v", False)),
        )

        res.update(status="ok", verts=nverts, tris=ntris, textured=textured,
                   c3d=str(out_c3d), dds=str(out_dds) if textured else None,
                   elapsed_s=round(time.time() - t0, 2))
        return res

    except subprocess.TimeoutExpired as e:
        res["error"] = f"blender timed out after {BLENDER_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001 — robustness: never kill the batch
        import traceback
        res["error"] = f"{type(e).__name__}: {e}"
        res["traceback"] = traceback.format_exc()[-1500:]
    res["elapsed_s"] = round(time.time() - t0, 2)
    return res


# --------------------------------------------------------------------------- #
# GPU-guarded external steps
# --------------------------------------------------------------------------- #
_GPU_SEM = None  # set per-process by the pool initializer


def _pool_init(gpu_sem):
    global _GPU_SEM
    _GPU_SEM = gpu_sem


class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _gpu():
    """Context manager that throttles GPU-heavy steps to gpu-slots concurrency."""
    return _GPU_SEM if _GPU_SEM is not None else _NullCtx()


def _run_blender_baker(baker_py: Path, src: Path, name: str, scale: str, orient: str):
    """Headless Blender: GLB/SKP -> baked OBJ + atlas PNG. GPU-throttled (Cycles)."""
    cmd = [str(BLENDER), "-b", "--factory-startup", "--python", str(baker_py), "--",
           str(src), name, scale, orient]
    with _gpu():
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=BLENDER_TIMEOUT)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-700:]
        raise RuntimeError(f"blender baker failed (rc={r.returncode}): {tail}")


def _run_custom_builder(builder_py: Path, work: Path, name: str):
    """Run a bespoke Blender builder (writes its own OBJ + *_bake.png in `work`).
    Returns (bake_png, obj_path) — discovered from what the builder produced."""
    cmd = [str(BLENDER), "-b", "--factory-startup", "--python", str(builder_py)]
    with _gpu():
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=BLENDER_TIMEOUT)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-700:]
        raise RuntimeError(f"custom builder failed (rc={r.returncode}): {tail}")
    # find the produced PNG + OBJ in the work dir (newest of each)
    pngs = sorted(work.glob("*_bake.png"), key=lambda p: p.stat().st_mtime)
    objs = sorted(work.glob("*.obj"), key=lambda p: p.stat().st_mtime)
    if not pngs or not objs:
        raise RuntimeError(f"custom builder produced no obj/png in {work}")
    return pngs[-1], objs[-1]


def _nvcompress(png: Path, dds: Path):
    """GPU DXT1 (bc1) compress a 2048 atlas PNG to a Condor DDS. GPU-throttled."""
    with _gpu():
        r = subprocess.run([str(NVCOMPRESS), "-bc1", "-silent", str(png), str(dds)],
                           capture_output=True, text=True)
    if r.returncode != 0 or not dds.exists():
        raise RuntimeError(f"nvcompress failed: {(r.stderr or r.stdout)[-400:]}")


# --------------------------------------------------------------------------- #
# manifest load / save + the driver
# --------------------------------------------------------------------------- #
def load_manifest(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("objects") or data.get("models") or []
    if not isinstance(data, list):
        raise ValueError("manifest must be a JSON list, or {'objects': [...]}")
    return data


def run_batch(objects: list[dict], workers: int, gpu_slots: int,
              force: bool) -> list[dict]:
    WORKROOT.mkdir(parents=True, exist_ok=True)
    tasks = []
    for o in objects:
        t = dict(o)
        t["_force"] = force
        tasks.append(t)

    print(f"[batch] {len(tasks)} object(s) | workers={workers} | gpu-slots={gpu_slots} "
          f"| blender={'OK' if BLENDER.exists() else 'MISSING'} "
          f"| nvcompress={'OK' if NVCOMPRESS.exists() else 'MISSING'}")
    print(f"[batch] cpu_count={os.cpu_count()}  workroot={WORKROOT}")

    t_start = time.time()
    gpu_sem = mp.BoundedSemaphore(max(1, gpu_slots))
    results: list[dict] = []
    # chunksize=1 so each object is its own scheduling unit (uneven model sizes).
    with mp.Pool(processes=max(1, workers), initializer=_pool_init,
                 initargs=(gpu_sem,)) as pool:
        for r in pool.imap_unordered(convert_one, tasks, chunksize=1):
            tag = {"ok": "OK ", "skipped": "SKIP", "error": "FAIL"}.get(r["status"], "?")
            extra = ""
            if r["status"] in ("ok", "skipped"):
                extra = (f"verts={r['verts']} tris={r['tris']} "
                         f"tex={'Y' if r['textured'] else 'N'}")
            else:
                extra = r.get("error", "")
            print(f"  [{tag}] {r['name']:24} {r['elapsed_s']:6.1f}s  {extra}")
            results.append(r)
    wall = time.time() - t_start

    # ---- merge results back into the object records (preserve order) ----- #
    by_name = {r["name"]: r for r in results}
    annotated = []
    for o in objects:
        r = by_name.get(o["name"], {})
        merged = dict(o)
        for k in ("verts", "tris", "textured", "elapsed_s", "status", "error"):
            if k in r and r[k] is not None:
                merged[k] = r[k]
        annotated.append(merged)

    _print_summary(results, wall, workers, gpu_slots)
    return annotated


def _print_summary(results, wall, workers, gpu_slots):
    ok = [r for r in results if r["status"] == "ok"]
    sk = [r for r in results if r["status"] == "skipped"]
    fa = [r for r in results if r["status"] == "error"]
    conv = ok  # actually-converted (excludes skips) for the speed claim
    print("\n" + "=" * 64)
    print("BENCHMARK / BATCH SUMMARY")
    print("=" * 64)
    print(f"  objects total      : {len(results)}")
    print(f"  converted          : {len(ok)}   skipped(up-to-date): {len(sk)}   failed: {len(fa)}")
    print(f"  workers            : {workers}      gpu-slots: {gpu_slots}      cpu_count: {os.cpu_count()}")
    print(f"  GPU used           : Blender Cycles bake (RTX) + nvcompress DXT, throttled to {gpu_slots} slot(s)")
    print(f"  WALL-CLOCK (batch) : {wall:.1f} s")
    if conv:
        times = [r["elapsed_s"] for r in conv]
        cpu_sum = sum(times)
        print(f"  per-object elapsed : min {min(times):.1f}s  median {sorted(times)[len(times)//2]:.1f}s  "
              f"max {max(times):.1f}s  (sum {cpu_sum:.1f}s)")
        print(f"  serial-equiv (sum) : {cpu_sum:.1f} s  ->  speedup x{cpu_sum / wall:.1f} from parallelism")
        print(f"  per-object times   :")
        for r in sorted(conv, key=lambda x: -x["elapsed_s"]):
            print(f"      {r['name']:24} {r['elapsed_s']:6.1f}s  "
                  f"verts={r['verts']:6} tris={r['tris']:6} tex={'Y' if r['textured'] else 'N'}")
    if fa:
        print("  FAILURES:")
        for r in fa:
            print(f"      {r['name']:24} {r.get('error','')}")
    print("=" * 64)


# --------------------------------------------------------------------------- #
# the built-in benchmark manifest: the 8 already-staged Skopje landmarks.
# Sources + exact per-object scale/orient/material are the verified parameters
# that produced the current .sandbox/landmarks/*.c3d (see git log + BAKE| logs).
# --------------------------------------------------------------------------- #
_LM_WORK = SANDBOX / "landmarks" / "_work"
_LM_MODELS = _LM_WORK / "models"

BENCHMARK_OBJECTS = [
    # The Millennium Cross is the one bespoke case: a .skp built by build_cross.py
    # (custom gold-metal material bake), 66 m, material (1,1,1,1,1,1).
    dict(name="MillenniumCross",
         source=str((_LM_WORK / "MilleniumCross.skp").relative_to(REPO)),
         kind="skp", lat=41.96370, lon=21.40980, target="landmarks",
         custom_builder=str((_LM_WORK / "build_cross.py").relative_to(REPO)),
         material=[1.0, 1.0, 1.0, 1.0, 1.0, 1.0]),
    # The rest are GLB sources baked by the generic glb_to_baked.py.
    dict(name="StoneBridge",
         source=str((_LM_MODELS / "stonebridge_viktormkd.glb").relative_to(REPO)),
         kind="glb", lat=41.99710, lon=21.43323, target="landmarks",
         scale="native", orient="longY"),
    dict(name="WarriorOnHorse",
         source=str((_LM_MODELS / "warrior_fountain.glb").relative_to(REPO)),
         kind="glb", lat=41.99591, lon=21.43147, target="landmarks",
         scale="native", orient="none"),
    dict(name="ToseProeskiArena",
         source=str((_LM_MODELS / "gradski_stadion.glb").relative_to(REPO)),
         kind="glb", lat=42.00571, lon=21.42556, target="landmarks",
         scale="native", orient="longY"),
    dict(name="PortaMacedonia",
         source=str((_LM_MODELS / "porta_macedonia.glb").relative_to(REPO)),
         kind="glb", lat=41.99360, lon=21.43360, target="landmarks",
         scale="native", orient="longY"),
    dict(name="TelecomTowerAEK",
         source=str((_LM_MODELS / "aek_tower.glb").relative_to(REPO)),
         kind="glb", lat=41.96552, lon=21.39765, target="landmarks",
         scale="156.32", orient="none"),
    dict(name="MOB_OperaBallet",
         source=str((_LM_MODELS / "mob.glb").relative_to(REPO)),
         kind="glb", lat=41.99762, lon=21.43696, target="landmarks",
         scale="len:140", orient="longY"),
    dict(name="RailwayStationSkopje",
         source=str((_LM_MODELS / "transport_centre.glb").relative_to(REPO)),
         kind="glb", lat=41.99096, lon=21.44589, target="landmarks",
         scale="native", orient="none"),
]


def write_benchmark_manifest(path: Path) -> Path:
    path.write_text(json.dumps({"objects": BENCHMARK_OBJECTS}, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", nargs="?", help="manifest json (list of objects)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 8,
                    help="parallel worker processes (default = cpu_count)")
    ap.add_argument("--gpu-slots", type=int, default=2,
                    help="max concurrent GPU steps (Cycles bake + nvcompress); "
                         "keeps VRAM from thrashing (default 2)")
    ap.add_argument("--force", action="store_true",
                    help="re-convert even if the .c3d is newer than the source")
    ap.add_argument("--out-manifest", default=None,
                    help="where to write the annotated manifest "
                         "(default <manifest-stem>.out.json)")
    ap.add_argument("--benchmark", action="store_true",
                    help="re-convert the 8 staged Skopje landmarks in one parallel "
                         "batch and report the bulk-speedup numbers")
    args = ap.parse_args(argv)

    if not BLENDER.exists():
        print(f"FATAL: Blender not found at {BLENDER}", file=sys.stderr)
        return 2
    if not NVCOMPRESS.exists():
        print(f"FATAL: nvcompress not found at {NVCOMPRESS}", file=sys.stderr)
        return 2

    if args.benchmark:
        man_path = SANDBOX / "landmarks" / "benchmark_manifest.json"
        write_benchmark_manifest(man_path)
        objects = BENCHMARK_OBJECTS
        out_manifest = _resolve(args.out_manifest) if args.out_manifest else \
            man_path.with_suffix(".out.json")
        print(f"[benchmark] wrote manifest -> {man_path}")
    else:
        if not args.manifest:
            ap.error("a manifest path is required (or use --benchmark)")
        man_path = _resolve(args.manifest)
        objects = load_manifest(man_path)
        out_manifest = _resolve(args.out_manifest) if args.out_manifest else \
            man_path.with_suffix(".out.json")

    annotated = run_batch(objects, workers=args.workers, gpu_slots=args.gpu_slots,
                          force=args.force)
    out_manifest.write_text(json.dumps(annotated, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    print(f"\n[batch] annotated manifest -> {out_manifest}")

    failed = sum(1 for a in annotated if a.get("status") == "error")
    return 1 if failed else 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
