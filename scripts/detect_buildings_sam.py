#!/usr/bin/env python3
r"""
LOCAL SAM building-footprint detection from the installed Condor ortho textures.

Detect building footprints DIRECTLY in the installed Condor patch textures
(``C:/Condor2/Landscapes/MacedoniaSkopje/Textures/tCCRR.dds``, 2048x2048, no
georeference) and emit regularised (orthogonalised) polygons in EPSG:32634
GeoJSON, ready to feed ``scripts/footprints_to_obj.py`` /
``scripts/place_buildings.py``.

  DDS texture  ->  georeferenced GeoTIFF tile  ->  SAM text-prompt 'building'
               ->  raster mask  ->  vectorise  ->  regularise (orthogonalise)
               ->  merge across patches  ->  .sandbox/sam_buildings.geojson

================================================================================
THIS RUNS ON THE USER'S BOX, NOT IN THE SANDBOX
================================================================================
The CI/dev sandbox has NO GPU, so this file is written to be *delivered* and run
on the user's machine (RTX 3070 8 GB, 64 GB RAM, i9-9900K). The heavy imports
(``torch``, ``samgeo``, ``ultralytics``) are deferred into the functions that
need them so the module can at least be imported / ``--help``-ed on a box without
them installed. See ``.sandbox/sam_pipeline_README.md`` for the pinned setup and
the two gated-weights / CLIP-fork gotchas.

--------------------------------------------------------------------------------
GEOREFERENCING THE DDS  (the load-bearing assumption -- verify it once)
--------------------------------------------------------------------------------
A Condor ``.dds`` has NO embedded georeference. We assign one from the patch grid
defined in ``condor_grid.py`` (the single source of truth for this landscape).

  Filename  tCCRR  ->  patch_col = CC,  patch_row = RR
  (Condor convention: col 0 = EAST, row 0 = SOUTH)

  bounds (EPSG:32634, UTM 34N, north-up) = condor_grid.patch_bounds_utm(CC, RR)
        = (min_E, min_N, max_E, max_N)

This was checked to be byte-identical to the task's spelled-out formula
``col_from_west = 11-CC ; row_from_south = RR ;
  E[506880 + col_from_west*5760 .. +5760] ; N[4631040 + row_from_south*5760 .. +5760]``
for all 144 patches (mismatches = 0). Reusing ``condor_grid`` keeps the airport /
forest / mesh / texture pipelines on ONE calibration, so an expansion to full
North Macedonia is a reparameterisation, not a rewrite.

  Pixel size = 5760 m / 2048 px = 2.8125 m/px.  North-up, so the GeoTIFF affine
  transform is  Affine(2.8125, 0, min_E, 0, -2.8125, max_E_north=max_N).

ASSUMPTION FLAGGED: the installed textures were (per CLAUDE.md) built on the
older 29.987 m DEM, NOT the exact-30 m grid. That drifts texture-vs-mesh by up to
~30 m at the SE corner. Footprints detected here are therefore in the *texture*
frame. For placing objects that line up with the *rendered ortho* that is exactly
right (objects and ortho share the texture frame). If you later rebuild textures
on exact-30 m via ``build_patch_textures.py``, re-run this detection so polygons
track the new pixels. Either way the polygons are emitted in true EPSG:32634.

--------------------------------------------------------------------------------
MODELS
--------------------------------------------------------------------------------
  --model sam3      (default) segment-geospatial (samgeo) wrapping SAM 3.1 with a
                    TEXT prompt 'building', FP16 (half=True), batch 1. ~4 GB VRAM
                    at tile 1024. The quality path. Weights are GATED on
                    HuggingFace (request facebook/sam3, then huggingface-cli
                    login) and the Ultralytics text path needs the CLIP fork --
                    see the README.
  --model fastsam   FastSAM-s (Ultralytics) text-prompted 'building'. The OOM /
                    country-wide-speed fallback. Smaller + faster, lower fidelity.
  --model yolo      YOLO11n-seg instance segmentation fallback (no text prompt;
                    detects its trained classes -- use only if you have / fine-tune
                    a building-capable seg model; included for the "speed sweep"
                    path the task asked for).

All three converge on the same downstream contract: a per-tile binary building
mask in the tile's pixel frame, which this script vectorises + regularises +
reprojects identically regardless of model.

--------------------------------------------------------------------------------
OUTPUT  (.sandbox/sam_buildings.geojson, EPSG:32634)
--------------------------------------------------------------------------------
FeatureCollection of Polygons. Per feature:
    properties.area_m2   float   shapely polygon area in m^2
    properties.src       "sam3" | "fastsam" | "yolo"
    properties.patch     "CCRR"
    properties.height    null    <-- intentionally empty; the cadastre/levels
                                      JOIN happens later (see place_buildings.py /
                                      download_cadastre_buildings.py).
This is exactly the schema place_buildings.py already reads (it keys on
num_floors/height/building:levels and defaults height to 3 m when absent), so the
SAM output is a drop-in alternative footprint source.

NEVER writes into C:/Condor2. NEVER opens a GUI. Deterministic given fixed
weights + thresholds.

================================================================================
USAGE (on the user's box, after the README setup)
================================================================================
    # smoke test on ONE patch (recommended first run):
    python scripts/detect_buildings_sam.py --patches 0703 --keep-tiffs

    # a handful of patches:
    python scripts/detect_buildings_sam.py --patches 0703,0704,0803

    # everything installed (all t*.dds in the Textures dir):
    python scripts/detect_buildings_sam.py

    # 3070-tuned knobs (these are the defaults):
    python scripts/detect_buildings_sam.py \
        --model sam3 --tile 1024 --overlap 128 --half \
        --min-area 40 --simplify 1.0 --orthogonalize

    # OOM? drop tile size and/or switch model:
    python scripts/detect_buildings_sam.py --tile 512
    python scripts/detect_buildings_sam.py --model fastsam --tile 1024
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# condor_grid is pure-python (pyproj) and safe to import in the sandbox.
from condor_grid import (  # noqa: E402
    PATCH_SIZE_M,
    PATCHES_X,
    PATCHES_Y,
    UTM_CRS,
    patch_bounds_utm,
)

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
TEXTURE_DIR = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
OUT_DIR = ROOT / ".sandbox"
OUT_GEOJSON = OUT_DIR / "sam_buildings.geojson"
TIFF_CACHE = OUT_DIR / "sam_tiffs"          # georeferenced patch GeoTIFFs
TILE_CACHE = OUT_DIR / "sam_tiles"          # per-tile mask debris (optional keep)

TEX_PX = 2048                                # patch texture side, pixels
PX_SIZE_M = PATCH_SIZE_M / TEX_PX            # 5760 / 2048 = 2.8125 m/px
UTM_EPSG = 32634

# SAM 3.1 text prompt. The whole point of the converged approach.
TEXT_PROMPT = "building"


# ===========================================================================
# 1. DDS -> georeferenced GeoTIFF
# ===========================================================================
def patch_name_to_colrow(name: str):
    """``tCCRR`` (or ``CCRR``) -> (patch_col, patch_row) ints. col 0=E, row 0=S."""
    stem = name
    if stem.lower().endswith(".dds"):
        stem = stem[:-4]
    if stem.lower().startswith("t"):
        stem = stem[1:]
    if len(stem) != 4 or not stem.isdigit():
        raise ValueError(f"bad patch name {name!r}; expected tCCRR / CCRR")
    return int(stem[:2]), int(stem[2:])


def read_dds_rgb(dds_path: Path) -> np.ndarray:
    """Decode a Condor DDS (DXT1 or DXT3) to an HxWx3 uint8 RGB array.

    Pillow 12.x decodes both the 2.8 MB DXT1 and 5.6 MB DXT3 Condor patches to
    RGBA 2048x2048 directly (verified on this repo's installed textures). We drop
    alpha; SAM/ortho care only about RGB. imageio is the documented fallback.
    """
    try:
        from PIL import Image
        im = Image.open(dds_path)
        im.load()
        arr = np.asarray(im)
    except Exception as exc_pil:  # pragma: no cover - fallback path
        try:
            import imageio.v3 as iio
            arr = iio.imread(dds_path)
        except Exception as exc_iio:
            raise RuntimeError(
                f"Could not decode {dds_path.name} with Pillow ({exc_pil}) or "
                f"imageio ({exc_iio}). Install pillow>=10 (DXT support) or "
                f"convert the DDS to PNG first with nvcompress/texconv."
            )
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    if arr.shape[:2] != (TEX_PX, TEX_PX):
        # Don't silently proceed on an unexpected size -- it would mis-georeference.
        raise RuntimeError(
            f"{dds_path.name} decoded to {arr.shape[:2]}, expected "
            f"{(TEX_PX, TEX_PX)}. Refusing to georeference a wrong-size texture."
        )
    return np.ascontiguousarray(arr, dtype=np.uint8)


def dds_to_geotiff(dds_path: Path, out_tif: Path) -> Path:
    """Write a north-up EPSG:32634 GeoTIFF for one patch DDS using grid bounds."""
    import rasterio
    from rasterio.transform import from_bounds

    col, row = patch_name_to_colrow(dds_path.name)
    min_e, min_n, max_e, max_n = patch_bounds_utm(col, row)
    rgb = read_dds_rgb(dds_path)  # HxWx3, north-up (row 0 = north)

    transform = from_bounds(min_e, min_n, max_e, max_n, TEX_PX, TEX_PX)
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": TEX_PX,
        "width": TEX_PX,
        "count": 3,
        "dtype": "uint8",
        "crs": rasterio.crs.CRS.from_epsg(UTM_EPSG),
        "transform": transform,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "photometric": "RGB",
    }
    # rasterio wants band-major (3, H, W).
    with rasterio.open(out_tif, "w", **profile) as ds:
        ds.write(np.transpose(rgb, (2, 0, 1)))
    return out_tif


# ===========================================================================
# 2. SAM / FastSAM / YOLO  ->  per-patch building mask (uint8 0/255, HxW)
# ===========================================================================
def _torch_device_report():
    """Return (device_str, info_str). Imports torch; raises if absent."""
    import torch
    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        vram = torch.cuda.get_device_properties(0).total_memory / 1024**3
        return "cuda", f"CUDA {name} ({vram:.1f} GB), torch {torch.__version__}"
    return "cpu", f"CPU only (no CUDA) -- torch {torch.__version__}; this will be SLOW"


def segment_patch_sam3(tif_path: Path, args) -> np.ndarray:
    """segment-geospatial (samgeo) SAM 3.1, text prompt 'building'.

    Returns an HxW uint8 mask (0/255) in the GeoTIFF's pixel frame (== texture
    frame, north-up). samgeo's text-prompt API has shifted across releases; we
    try the SAM 3 ``LangSAM``/``SamGeo`` entry points and fall back with a clear
    message so the user can pin the matching samgeo per the README.
    """
    device, info = _torch_device_report()
    print(f"    [sam3] {info}; device={device}, half={args.half}, tile={args.tile}")

    out_mask_tif = tif_path.with_name(tif_path.stem + "_buildings.tif")

    # ---- Preferred: samgeo's text-prompt segmenter -------------------------
    # samgeo exposes text-prompted SAM under a couple of names depending on
    # version. We probe them in order. All write a georeferenced mask raster we
    # then read back, so the rest of the pipeline is model-agnostic.
    last_err = None
    try:
        from samgeo import samgeo as _samgeo_mod  # noqa: F401
    except Exception:
        pass

    # (a) Newer samgeo: text_sam / SamGeo3 with .set_image + .predict(text=...)
    for entry in ("_try_samgeo3", "_try_langsam"):
        fn = globals().get(entry)
        try:
            mask = fn(tif_path, out_mask_tif, args, device)
            if mask is not None:
                return mask
        except Exception as exc:  # keep trying the next entry point
            last_err = exc
            print(f"    [sam3] {entry} unavailable: {type(exc).__name__}: {exc}")

    raise RuntimeError(
        "Could not run SAM 3.1 text segmentation via samgeo. Most likely causes:\n"
        "  1. SAM 3 weights are GATED -- request access to facebook/sam3 on "
        "HuggingFace, then `huggingface-cli login`.\n"
        "  2. The Ultralytics text-prompt path needs the CLIP fork: "
        "`pip install git+https://github.com/ultralytics/CLIP.git`.\n"
        "  3. samgeo version mismatch -- pin per .sandbox/sam_pipeline_README.md.\n"
        f"Last error: {last_err!r}\n"
        "Fast unblock: rerun with `--model fastsam`."
    )


def _try_samgeo3(tif_path, out_mask_tif, args, device):
    """samgeo >= the SAM3 release: text_prompt segmentation.

    API (samgeo SAM3): ``from samgeo import SamGeo3`` (or ``text_sam``).
    Method names have varied; we use the documented ``predict``/``text_prompt``
    surface and ask for a binarised raster mask out.
    """
    try:
        from samgeo import SamGeo3 as _Seg  # type: ignore
    except Exception:
        from samgeo.text_sam import LangSAM as _Seg  # type: ignore

    kwargs = {}
    # SAM3 wrappers accept half/device in recent samgeo; pass when supported.
    try:
        seg = _Seg(model_type="sam3", device=device)  # type: ignore
    except TypeError:
        seg = _Seg()  # older signature

    # Half precision where the wrapper exposes the underlying model.
    if args.half:
        _maybe_half(seg)

    # Text-prompted predict. Different samgeo builds name this differently;
    # try the common ones. All produce a georeferenced mask GeoTIFF.
    box_thr = args.box_threshold
    text_thr = args.text_threshold
    for meth in ("predict", "text_prompt", "predict_text"):
        f = getattr(seg, meth, None)
        if f is None:
            continue
        try:
            f(
                str(tif_path),
                text_prompt=TEXT_PROMPT,
                box_threshold=box_thr,
                text_threshold=text_thr,
                output=str(out_mask_tif),
            )
        except TypeError:
            # Positional / minimal signature variant.
            f(str(tif_path), TEXT_PROMPT, box_thr, text_thr, str(out_mask_tif))
        if out_mask_tif.exists():
            return _read_mask_raster(out_mask_tif)
    return None


def _try_langsam(tif_path, out_mask_tif, args, device):
    """Grounded-SAM style text path (samgeo.text_sam.LangSAM) as a second try."""
    from samgeo.text_sam import LangSAM  # type: ignore

    sam = LangSAM(model_type=getattr(args, "langsam_backbone", "sam2-hiera-large"))
    if args.half:
        _maybe_half(sam)
    sam.predict(
        str(tif_path),
        TEXT_PROMPT,
        box_threshold=args.box_threshold,
        text_threshold=args.text_threshold,
    )
    # LangSAM stores the merged mask; persist to raster for a uniform read-back.
    sam.show_anns(
        cmap="binary",
        add_boxes=False,
        alpha=1.0,
        title=None,
        output=str(out_mask_tif),
    )
    if out_mask_tif.exists():
        return _read_mask_raster(out_mask_tif)
    return None


def _maybe_half(seg_obj):
    """Best-effort cast the underlying torch model(s) to FP16 on CUDA."""
    import torch
    if not torch.cuda.is_available():
        return
    for attr in ("model", "sam", "predictor", "sam_model"):
        m = getattr(seg_obj, attr, None)
        if m is None:
            continue
        mdl = getattr(m, "model", m)
        try:
            mdl.half()
        except Exception:
            pass


def segment_patch_fastsam(tif_path: Path, args) -> np.ndarray:
    """FastSAM-s text-prompted 'building' (Ultralytics). OOM/speed fallback.

    Reads the GeoTIFF RGB, runs FastSAM at --tile imgsz, applies the text prompt
    via Ultralytics' FastSAMPrompt, and rasterises the kept instance polygons to
    a full-patch mask. Needs the CLIP fork for the text path (see README).
    """
    import rasterio
    from ultralytics import FastSAM
    from ultralytics.models.fastsam import FastSAMPrompt
    device, info = _torch_device_report()
    print(f"    [fastsam] {info}; imgsz={args.tile}, half={args.half}")

    with rasterio.open(tif_path) as ds:
        rgb = np.transpose(ds.read(), (1, 2, 0))  # HxWx3

    weights = args.fastsam_weights or "FastSAM-s.pt"
    model = FastSAM(weights)
    results = model(
        rgb,
        device=device,
        retina_masks=True,
        imgsz=args.tile,
        conf=args.conf,
        iou=0.9,
        half=args.half,
        verbose=False,
    )
    prompt = FastSAMPrompt(rgb, results, device=device)
    ann = prompt.text_prompt(text=TEXT_PROMPT)

    mask = np.zeros((TEX_PX, TEX_PX), dtype=np.uint8)
    for a in _iter_fastsam_masks(ann):
        if a.shape != (TEX_PX, TEX_PX):
            import cv2
            a = cv2.resize(a.astype(np.uint8), (TEX_PX, TEX_PX),
                           interpolation=cv2.INTER_NEAREST)
        mask[a > 0] = 255
    return mask


def _iter_fastsam_masks(ann):
    """Normalise the several shapes Ultralytics returns into HxW bool arrays."""
    if ann is None:
        return
    # Ultralytics Results object
    data = getattr(getattr(ann, "masks", None), "data", None)
    if data is not None:
        arr = data.cpu().numpy() if hasattr(data, "cpu") else np.asarray(data)
        for m in arr:
            yield m.astype(bool)
        return
    arr = np.asarray(ann)
    if arr.ndim == 3:
        for m in arr:
            yield m.astype(bool)
    elif arr.ndim == 2:
        yield arr.astype(bool)


def segment_patch_yolo(tif_path: Path, args) -> np.ndarray:
    """YOLO11n-seg instance segmentation fallback (no text prompt).

    Included for the country-wide *speed sweep* path. Stock YOLO11n-seg is COCO
    (no 'building' class), so this is only useful with a building-capable / fine-
    tuned seg weight passed via --yolo-weights; otherwise it returns an empty
    mask and warns. Kept so the speed path is wired end-to-end.
    """
    import rasterio
    from ultralytics import YOLO
    device, info = _torch_device_report()
    print(f"    [yolo] {info}; imgsz={args.tile}, weights={args.yolo_weights}")

    with rasterio.open(tif_path) as ds:
        rgb = np.transpose(ds.read(), (1, 2, 0))

    model = YOLO(args.yolo_weights or "yolo11n-seg.pt")
    names = getattr(model, "names", {})
    bld_ids = [i for i, n in names.items() if "build" in str(n).lower()]
    if not bld_ids:
        print("    [yolo] WARNING: loaded weights have no 'building' class "
              f"(classes={list(names.values())[:8]}...). Returning empty mask. "
              "Pass --yolo-weights pointing at a building-seg model.")
        return np.zeros((TEX_PX, TEX_PX), dtype=np.uint8)

    res = model(rgb, device=device, imgsz=args.tile, conf=args.conf,
                half=args.half, verbose=False)[0]
    mask = np.zeros((TEX_PX, TEX_PX), dtype=np.uint8)
    if res.masks is None:
        return mask
    cls = res.boxes.cls.cpu().numpy().astype(int)
    data = res.masks.data.cpu().numpy()
    for m, c in zip(data, cls):
        if c in bld_ids:
            mm = m
            if mm.shape != (TEX_PX, TEX_PX):
                import cv2
                mm = cv2.resize(mm.astype(np.uint8), (TEX_PX, TEX_PX),
                                interpolation=cv2.INTER_NEAREST)
            mask[mm > 0] = 255
    return mask


def _read_mask_raster(path: Path) -> np.ndarray:
    """Read a (possibly multi-band / float) mask raster into HxW uint8 0/255."""
    import rasterio
    with rasterio.open(path) as ds:
        a = ds.read(1)
    a = np.asarray(a)
    if a.dtype != np.uint8:
        a = (a > (a.max() / 2.0 if a.max() > 0 else 0)).astype(np.uint8) * 255
    else:
        a = np.where(a > 0, 255, 0).astype(np.uint8)
    if a.shape != (TEX_PX, TEX_PX):
        import cv2
        a = cv2.resize(a, (TEX_PX, TEX_PX), interpolation=cv2.INTER_NEAREST)
    return a


SEGMENTERS = {
    "sam3": segment_patch_sam3,
    "fastsam": segment_patch_fastsam,
    "yolo": segment_patch_yolo,
}


# ===========================================================================
# 3. mask -> vector polygons (in the tile/texture pixel frame)
# ===========================================================================
def mask_to_polygons(mask: np.ndarray, transform, min_area_m2: float):
    """Vectorise a 0/255 raster mask to UTM shapely polygons via rasterio.

    ``transform`` is the GeoTIFF affine (pixel->UTM), so output polygons are
    already in EPSG:32634. Filters by area in m^2 up front.
    """
    import rasterio.features
    from shapely.geometry import shape as shp_shape

    polys = []
    binmask = (mask > 0).astype(np.uint8)
    for geom, val in rasterio.features.shapes(binmask, mask=binmask.astype(bool),
                                              transform=transform):
        if val != 1:
            continue
        g = shp_shape(geom)
        if g.is_empty:
            continue
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            continue
        if g.area >= min_area_m2:
            polys.append(g)
    return polys


# ===========================================================================
# 4. regularisation / orthogonalisation
# ===========================================================================
def regularize(poly, args):
    """Orthogonalise a footprint to clean right-angled walls.

    Strategy (cheap, dependency-light, deterministic):
      1. simplify (Douglas-Peucker) to drop pixel-staircase vertices;
      2. if --orthogonalize, snap to the minimum-rotated-rectangle when the
         footprint is rectangle-like (the common Condor case -- boxes), else keep
         the simplified ring. This is the "FER/regularization" step from the
         converged approach, kept robust without extra deps. If `buildingregulariser`
         is installed (`pip install buildingregulariser`) we use it for higher
         fidelity (handles L/T/U shapes), falling back to this otherwise.
    """
    g = poly
    if args.simplify > 0:
        g = g.simplify(args.simplify, preserve_topology=True)
        if g.is_empty:
            return None

    if not args.orthogonalize:
        return g

    # Optional high-fidelity regulariser (handles non-rectangular footprints).
    if args.regularizer == "buildingregulariser":
        try:
            from buildingregulariser import regularize_geodataframe  # type: ignore
            import geopandas as gpd  # type: ignore
            gdf = gpd.GeoDataFrame(geometry=[g], crs=f"EPSG:{UTM_EPSG}")
            out = regularize_geodataframe(gdf, parallel_threshold=1.0)
            rg = out.geometry.iloc[0]
            return rg if (rg and not rg.is_empty) else g
        except Exception:
            pass  # fall through to the built-in rectangle snap

    # Built-in: snap to oriented bounding box when rectangle-like.
    try:
        mrr = g.minimum_rotated_rectangle
        if mrr.is_empty:
            return g
        # "Rectangle-like" = footprint fills most of its oriented bbox.
        if g.area >= args.rect_fill * mrr.area:
            return mrr
    except Exception:
        return g
    return g


# ===========================================================================
# 5. cross-patch merge / dedupe
# ===========================================================================
def merge_polygons(features, snap=0.5):
    """Dissolve duplicates created by patch seams.

    Buildings straddling a patch boundary get detected twice (once per patch).
    We union all polygons (buffer(snap)->unary_union->buffer(-snap) closes hair
    gaps at the seam), then re-split into individual footprints. Area is
    recomputed on the merged geometry.
    """
    from shapely.ops import unary_union
    from shapely.geometry import MultiPolygon

    geoms = [f["_geom"] for f in features]
    if not geoms:
        return []
    if snap > 0:
        geoms = [g.buffer(snap, join_style=2) for g in geoms]
    dissolved = unary_union(geoms)
    if snap > 0:
        dissolved = dissolved.buffer(-snap, join_style=2)

    parts = (list(dissolved.geoms) if isinstance(dissolved, MultiPolygon)
             else [dissolved])
    out = []
    for g in parts:
        if g.is_empty or g.area <= 0:
            continue
        out.append(g)
    return out


# ===========================================================================
# Patch selection / driver
# ===========================================================================
def resolve_patches(args):
    """Return a sorted list of (Path, 'CCRR') for the patches to process."""
    if args.patches:
        names = [p.strip() for p in args.patches.replace(" ", ",").split(",") if p.strip()]
        out = []
        for n in names:
            stem = n[1:] if n.lower().startswith("t") else n
            stem = stem[:-4] if stem.lower().endswith(".dds") else stem
            f = args.texture_dir / f"t{stem}.dds"
            if not f.exists():
                print(f"  [warn] requested patch {stem} -> {f} missing; skipping")
                continue
            out.append((f, stem))
        return sorted(out, key=lambda t: t[1])
    # All installed patch textures (skip empty.dds and any non-tCCRR file).
    out = []
    for f in sorted(args.texture_dir.glob("t*.dds")):
        try:
            col, row = patch_name_to_colrow(f.name)
        except ValueError:
            continue
        if 0 <= col < PATCHES_X and 0 <= row < PATCHES_Y:
            out.append((f, f.stem[1:]))
    return out


def build_arg_parser():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--patches", default="",
                   help="comma list of CCRR (e.g. 0703,0704). Default: all t*.dds.")
    p.add_argument("--texture-dir", type=Path, default=TEXTURE_DIR,
                   help=f"Condor Textures dir (default {TEXTURE_DIR})")
    p.add_argument("--out", type=Path, default=OUT_GEOJSON,
                   help=f"output geojson (default {OUT_GEOJSON})")

    p.add_argument("--model", choices=list(SEGMENTERS), default="sam3",
                   help="segmenter: sam3 (quality) | fastsam (fallback) | yolo (speed)")
    p.add_argument("--tile", type=int, default=1024,
                   help="tile / imgsz in px (1024 ~4GB on a 3070; drop to 512 on OOM)")
    p.add_argument("--overlap", type=int, default=128,
                   help="tile overlap px (reserved; samgeo tiles internally)")
    p.add_argument("--half", dest="half", action="store_true", default=True,
                   help="FP16 inference (default on; halves VRAM)")
    p.add_argument("--no-half", dest="half", action="store_false",
                   help="force FP32 (use if FP16 NaNs on your driver)")

    # detection thresholds
    p.add_argument("--box-threshold", type=float, default=0.24,
                   help="SAM3/LangSAM box confidence (lower=more, noisier)")
    p.add_argument("--text-threshold", type=float, default=0.24,
                   help="SAM3/LangSAM text-match threshold")
    p.add_argument("--conf", type=float, default=0.4,
                   help="FastSAM/YOLO confidence")

    # geometry post-processing
    p.add_argument("--min-area", type=float, default=40.0,
                   help="drop footprints < this many m^2 (default 40; matches place_buildings)")
    p.add_argument("--simplify", type=float, default=1.0,
                   help="Douglas-Peucker tolerance in m (0=off; default 1.0 ~= a third of a pixel)")
    p.add_argument("--orthogonalize", dest="orthogonalize", action="store_true",
                   default=True, help="snap rectangle-like footprints to their OBB (default on)")
    p.add_argument("--no-orthogonalize", dest="orthogonalize", action="store_false")
    p.add_argument("--regularizer", choices=["builtin", "buildingregulariser"],
                   default="builtin",
                   help="builtin OBB-snap (no deps) or the buildingregulariser lib if installed")
    p.add_argument("--rect-fill", type=float, default=0.80,
                   help="OBB snap only if footprint area >= this * OBB area (default 0.80)")
    p.add_argument("--merge-snap", type=float, default=0.5,
                   help="seam-dedupe buffer in m for cross-patch union (default 0.5)")

    # model weights overrides
    p.add_argument("--fastsam-weights", default="",
                   help="path/name for FastSAM weights (default FastSAM-s.pt, auto-downloaded)")
    p.add_argument("--yolo-weights", default="",
                   help="path/name for a building-capable YOLO*-seg weight (required for --model yolo)")
    p.add_argument("--langsam-backbone", default="sam2-hiera-large",
                   help="LangSAM fallback backbone if SamGeo3 path is unavailable")

    # io / debug
    p.add_argument("--keep-tiffs", action="store_true",
                   help="keep the intermediate georeferenced patch GeoTIFFs in .sandbox/sam_tiffs/")
    p.add_argument("--keep-masks", action="store_true",
                   help="keep per-patch mask GeoTIFFs for inspection")
    p.add_argument("--limit", type=int, default=0,
                   help="process at most N patches (debug)")
    p.add_argument("--dry-run", action="store_true",
                   help="only georeference DDS->GeoTIFF (no model); verifies the geo path")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TIFF_CACHE.mkdir(parents=True, exist_ok=True)

    patches = resolve_patches(args)
    if args.limit:
        patches = patches[: args.limit]
    if not patches:
        sys.exit(f"No patches to process. Looked in {args.texture_dir}. "
                 f"Pass --patches CCRR or check the path.")

    print(f"detect_buildings_sam: model={args.model} tile={args.tile} half={args.half}")
    print(f"  texture dir : {args.texture_dir}")
    print(f"  patches     : {len(patches)} "
          f"({patches[0][1]} .. {patches[-1][1]})")
    print(f"  px size     : {PX_SIZE_M:.4f} m/px  (5760 m / {TEX_PX} px)")
    print(f"  output      : {args.out}")
    print(f"  min-area    : {args.min_area} m^2  simplify={args.simplify} "
          f"orthogonalize={args.orthogonalize}\n")

    seg_fn = SEGMENTERS[args.model]

    all_features = []     # carry shapely geom in '_geom' for the final merge
    per_patch_counts = {}
    t_start = time.time()

    for i, (dds_path, ccrr) in enumerate(patches, 1):
        t0 = time.time()
        print(f"[{i}/{len(patches)}] patch {ccrr}  ({dds_path.name})")

        # 1. DDS -> GeoTIFF
        tif = TIFF_CACHE / f"t{ccrr}.tif"
        try:
            dds_to_geotiff(dds_path, tif)
        except Exception as exc:
            print(f"    [skip] georeference failed: {exc}")
            continue

        if args.dry_run:
            print(f"    [dry-run] wrote {tif.name} "
                  f"({tif.stat().st_size/1e6:.1f} MB); skipping model.")
            continue

        # 2. segment -> mask
        try:
            mask = seg_fn(tif, args)
        except Exception as exc:
            print(f"    [error] segmentation failed on {ccrr}: {exc}")
            if i == 1:
                # First patch failing usually means setup is wrong -- stop early
                # with the actionable message rather than churning 144 patches.
                raise
            continue

        import rasterio
        with rasterio.open(tif) as ds:
            transform = ds.transform

        if args.keep_masks:
            _save_mask(mask, transform, tif.with_name(f"t{ccrr}_mask.tif"))

        # 3. vectorise (already in UTM via the transform)
        polys = mask_to_polygons(mask, transform, args.min_area)

        # 4. regularise
        kept = 0
        for g in polys:
            rg = regularize(g, args)
            if rg is None or rg.is_empty or rg.area < args.min_area:
                continue
            all_features.append({"_geom": rg, "patch": ccrr})
            kept += 1
        per_patch_counts[ccrr] = kept

        if not args.keep_tiffs:
            try:
                tif.unlink()
            except OSError:
                pass

        dt = time.time() - t0
        n_tiles = math.ceil(TEX_PX / args.tile) ** 2
        print(f"    -> {kept} footprints  ({dt:.1f}s, ~{dt/max(n_tiles,1):.2f}s/tile)")

    if args.dry_run:
        print(f"\n[dry-run] done. GeoTIFFs in {TIFF_CACHE}")
        return

    # 5. cross-patch merge / dedupe at seams
    print(f"\nMerging {len(all_features)} raw footprints across patch seams...")
    merged_geoms = merge_polygons(all_features, snap=args.merge_snap)
    print(f"  -> {len(merged_geoms)} footprints after seam-dedupe")

    # Write EPSG:32634 GeoJSON.
    write_geojson(merged_geoms, args)

    total_dt = time.time() - t_start
    print(f"\nDONE in {total_dt:.1f}s. {len(merged_geoms)} buildings -> {args.out}")
    print("Next: feed this geojson to scripts/footprints_to_obj.py "
          "(or scripts/place_buildings.py --in <this file>) to emit the .obj "
          "placement records. Height is left empty for the cadastre join.")


def _save_mask(mask, transform, path):
    import rasterio
    profile = {
        "driver": "GTiff", "height": TEX_PX, "width": TEX_PX, "count": 1,
        "dtype": "uint8", "crs": rasterio.crs.CRS.from_epsg(UTM_EPSG),
        "transform": transform, "compress": "deflate", "nbits": 1,
    }
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(mask[np.newaxis, :, :])


def write_geojson(geoms, args):
    from shapely.geometry import mapping
    feats = []
    for g in geoms:
        # patch label from centroid (post-merge geometry may span a seam).
        c = g.centroid
        ccrr = _centroid_patch(c.x, c.y)
        feats.append({
            "type": "Feature",
            "properties": {
                "src": args.model,
                "area_m2": round(g.area, 2),
                "patch": ccrr,
                "height": None,   # filled later by the cadastre/levels join
            },
            "geometry": mapping(g),
        })
    fc = {
        "type": "FeatureCollection",
        "name": "sam_buildings",
        "crs": {"type": "name",
                "properties": {"name": f"urn:ogc:def:crs:EPSG::{UTM_EPSG}"}},
        "features": feats,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(fc, f)


def _centroid_patch(e, n):
    """CCRR for a UTM centroid (col 0=E, row 0=S), or '' if outside."""
    from condor_grid import BR_EASTING, BR_NORTHING
    col = int((BR_EASTING - e) // PATCH_SIZE_M)
    row = int((n - BR_NORTHING) // PATCH_SIZE_M)
    if 0 <= col < PATCHES_X and 0 <= row < PATCHES_Y:
        return f"{col:02d}{row:02d}"
    return ""


if __name__ == "__main__":
    main()
