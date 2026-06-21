#!/usr/bin/env python3
r"""Standalone SAM3 (samgeo, transformers backend) building-footprint runner.

WHY THIS EXISTS (vs detect_buildings_sam.py):
    detect_buildings_sam.py's --model sam3 path calls ``SamGeo3(model_type=...)``
    which is NOT the samgeo 1.3.3 API. In samgeo 1.3.3, SamGeo3 takes
    ``backend=`` ('meta' | 'transformers'). The default 'meta' backend needs the
    ``sam3`` pip package (Python 3.12 only) -- it is NOT installed here -- so the
    scripted path raises "No module named 'sam3'" and never runs the model.

    THIS box: Python 3.10 + transformers 5.12.1 (which ships Sam3Model/Sam3Processor)
    + facebook/sam3 gated access granted (huggingface-cli whoami = ipanov).
    The samgeo TRANSFORMERS backend therefore runs genuine SAM3 with a text prompt
    on 3.10. We reuse detect_buildings_sam.py's verified DDS->GeoTIFF georeferencing
    (EPSG:32634 from condor_grid.patch_bounds_utm) and its vectorise+regularise+
    write-GeoJSON helpers, so output is identical-contract to that script.

PIPELINE:
    DDS (2048, DXT) -> georef GeoTIFF (EPSG:32634)
        -> SamGeo3(backend='transformers').generate_masks_tiled(prompt='building',
               tile_size=T, overlap=128)   [writes a labelled GeoTIFF mask]
        -> vectorise (rasterio.features.shapes, already UTM)
        -> regularise (simplify + OBB-snap, from detect_buildings_sam)
        -> EPSG:32634 GeoJSON + preview PNG.

8GB-VRAM tuning: FP16 is the transformers default for Sam3 on CUDA; tiling keeps
peak VRAM low (tile 1024 -> ~tile-sized forward passes). Drop --tile to 512 on OOM.

NEVER writes into C:/Condor2. NEVER opens a GUI.

USAGE:
    python scripts/run_sam3_buildings.py --patches 0703 --tile 1024
    python scripts/run_sam3_buildings.py --patches 0703 --tile 512   # OOM fallback
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import detect_buildings_sam as dbs  # reuse georef + vectorise + regularise + io
from condor_grid import patch_bounds_utm  # noqa: E402

OUT_DIR = ROOT / ".sandbox"
TIFF_CACHE = OUT_DIR / "sam_tiffs"
MASK_CACHE = OUT_DIR / "sam3_masks"
TEX_PX = dbs.TEX_PX
UTM_EPSG = dbs.UTM_EPSG
TEXT_PROMPT = "building"


def gpu_mem_report(tag: str):
    import torch
    if not torch.cuda.is_available():
        return f"[{tag}] no CUDA"
    alloc = torch.cuda.memory_allocated() / 1024**3
    reserved = torch.cuda.memory_reserved() / 1024**3
    peak = torch.cuda.max_memory_allocated() / 1024**3
    return (f"[{tag}] VRAM alloc={alloc:.2f}GB reserved={reserved:.2f}GB "
            f"peak_alloc={peak:.2f}GB")


def build_sam3(args):
    """Construct SamGeo3 with the transformers backend (the only one that works
    on Python 3.10). FP16 on CUDA, confidence threshold passed through."""
    from samgeo import SamGeo3
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"  building SamGeo3(backend='transformers', model_id='facebook/sam3') "
          f"on {device} ...")
    sam = SamGeo3(
        backend="transformers",
        model_id="facebook/sam3",
        device=device,
        confidence_threshold=args.confidence,
        mask_threshold=args.mask_threshold,
    )
    # Best-effort FP16 to fit 8GB. The transformers Sam3 wrapper keeps the model
    # under .model; cast it if exposed and we're on CUDA.
    if args.half and device == "cuda":
        for attr in ("model", "sam3_model", "_model"):
            m = getattr(sam, attr, None)
            if m is not None and hasattr(m, "half"):
                try:
                    m.half()
                    print(f"    cast SamGeo3.{attr} -> FP16")
                except Exception as e:
                    print(f"    FP16 cast on {attr} skipped: {e}")
                break
    return sam


def segment_patch_sam3_transformers(sam, tif_path: Path, mask_tif: Path, args):
    """Run text-prompted SAM3 on a georef GeoTIFF -> labelled mask GeoTIFF.

    Returns HxW uint8 0/255 mask read back from the written raster (so the
    geotransform-aware vectorise path is identical to detect_buildings_sam)."""
    mask_tif.parent.mkdir(parents=True, exist_ok=True)
    sam.generate_masks_tiled(
        source=str(tif_path),
        prompt=TEXT_PROMPT,
        output=str(mask_tif),
        tile_size=args.tile,
        overlap=args.overlap,
        min_size=int(args.min_area / (dbs.PX_SIZE_M ** 2)),  # m^2 -> px
        unique=False,   # binary mask; we vectorise connected components ourselves
        dtype="uint8",
        batch_size=1,
        verbose=True,
    )
    if not mask_tif.exists():
        raise RuntimeError(f"SAM3 produced no mask raster at {mask_tif}")
    return dbs._read_mask_raster(mask_tif)


def render_preview(tif_path: Path, geoms, out_png: Path):
    """Overlay detected footprints (red outlines) on the RGB texture -> PNG."""
    import rasterio
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon as MplPoly
    from shapely.geometry import MultiPolygon

    with rasterio.open(tif_path) as ds:
        rgb = np.transpose(ds.read(), (1, 2, 0))
        b = ds.bounds  # left, bottom, right, top
        T = ds.transform

    fig, ax = plt.subplots(figsize=(12, 12), dpi=110)
    ax.imshow(rgb, extent=[b.left, b.right, b.bottom, b.top])

    def add(poly):
        xs, ys = poly.exterior.xy
        ax.add_patch(MplPoly(np.column_stack([xs, ys]), closed=True,
                             fill=False, edgecolor="red", linewidth=0.8))

    n = 0
    for g in geoms:
        if g.is_empty:
            continue
        if isinstance(g, MultiPolygon):
            for p in g.geoms:
                add(p); n += 1
        else:
            add(g); n += 1
    ax.set_title(f"SAM3 'building' footprints on t{tif_path.stem[-4:]}  "
                 f"(n={n}, EPSG:{UTM_EPSG})")
    ax.set_xlabel("Easting (m)"); ax.set_ylabel("Northing (m)")
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patches", default="0703",
                    help="comma list of CCRR (default 0703)")
    ap.add_argument("--texture-dir", type=Path, default=dbs.TEXTURE_DIR)
    ap.add_argument("--out", type=Path, default=OUT_DIR / "sam_buildings_t0703.geojson")
    ap.add_argument("--preview", type=Path, default=OUT_DIR / "sam_buildings_t0703_preview.png")
    ap.add_argument("--tile", type=int, default=1024)
    ap.add_argument("--overlap", type=int, default=128)
    ap.add_argument("--half", action="store_true", default=True)
    ap.add_argument("--no-half", dest="half", action="store_false")
    ap.add_argument("--confidence", type=float, default=0.35,
                    help="SAM3 detection confidence threshold (lower=more boxes)")
    ap.add_argument("--mask-threshold", type=float, default=0.5)
    # geometry post-proc (mirror detect_buildings_sam defaults)
    ap.add_argument("--min-area", type=float, default=40.0)
    ap.add_argument("--max-area", type=float, default=0.0,
                    help="drop footprints > this m^2 (0=off). Sane buildings are "
                         "<~8000 m^2; large values are dense-block over-merges.")
    ap.add_argument("--simplify", type=float, default=1.0)
    ap.add_argument("--orthogonalize", dest="orthogonalize", action="store_true", default=True)
    ap.add_argument("--no-orthogonalize", dest="orthogonalize", action="store_false")
    ap.add_argument("--regularizer", choices=["builtin", "buildingregulariser"], default="builtin")
    ap.add_argument("--rect-fill", type=float, default=0.80)
    ap.add_argument("--merge-snap", type=float, default=0.5)
    ap.add_argument("--keep-tiffs", action="store_true", default=True)
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TIFF_CACHE.mkdir(parents=True, exist_ok=True)

    names = [p.strip() for p in args.patches.replace(" ", ",").split(",") if p.strip()]
    print(f"run_sam3_buildings: patches={names} tile={args.tile} half={args.half} "
          f"conf={args.confidence}")
    print(gpu_mem_report("startup"))

    sam = build_sam3(args)
    print(gpu_mem_report("after model load"))

    all_features = []
    t_start = time.time()
    last_tif = None
    for ccrr in names:
        f = args.texture_dir / f"t{ccrr}.dds"
        if not f.exists():
            print(f"  [skip] {f} missing"); continue
        col, row = dbs.patch_name_to_colrow(f.name)
        print(f"\n[patch {ccrr}] bounds UTM = {patch_bounds_utm(col, row)}")

        tif = TIFF_CACHE / f"t{ccrr}.tif"
        dbs.dds_to_geotiff(f, tif)
        last_tif = tif
        print(f"  georef -> {tif.name}")

        import torch
        torch.cuda.reset_peak_memory_stats() if torch.cuda.is_available() else None
        t0 = time.time()
        mask_tif = MASK_CACHE / f"t{ccrr}_buildings.tif"
        mask = segment_patch_sam3_transformers(sam, tif, mask_tif, args)
        dt = time.time() - t0
        print(f"  SAM3 mask done in {dt:.1f}s; {gpu_mem_report('after segment')}")
        print(f"  mask coverage: {100.0*(mask>0).mean():.2f}% of patch px")

        import rasterio
        with rasterio.open(tif) as ds:
            transform = ds.transform
        polys = dbs.mask_to_polygons(mask, transform, args.min_area)
        kept = 0
        dropped_big = 0
        for g in polys:
            rg = dbs.regularize(g, args)
            if rg is None or rg.is_empty or rg.area < args.min_area:
                continue
            if args.max_area > 0 and rg.area > args.max_area:
                dropped_big += 1
                continue
            all_features.append({"_geom": rg, "patch": ccrr})
            kept += 1
        print(f"  vectorise+regularise -> {kept} footprints (raw {len(polys)}, "
              f"dropped {dropped_big} over max-area)")

    print(f"\nMerging {len(all_features)} footprints across seams ...")
    merged = dbs.merge_polygons(all_features, snap=args.merge_snap)
    print(f"  -> {len(merged)} after seam-dedupe")
    if args.max_area > 0:
        before = len(merged)
        merged = [g for g in merged if g.area <= args.max_area]
        print(f"  -> {len(merged)} after max-area cap "
              f"({before - len(merged)} block-merge blobs dropped)")

    # write GeoJSON via detect_buildings_sam's writer (it expects args.model)
    args.model = "sam3"
    dbs.write_geojson(merged, args)
    print(f"WROTE {args.out}  ({len(merged)} features, EPSG:{UTM_EPSG})")

    if last_tif is not None:
        npv = render_preview(last_tif, merged, args.preview)
        print(f"WROTE preview {args.preview}  ({npv} polygons drawn)")

    print(f"\nTOTAL {time.time()-t_start:.1f}s | {gpu_mem_report('final')}")
    print(f"buildings detected: {len(merged)}")


if __name__ == "__main__":
    main()
