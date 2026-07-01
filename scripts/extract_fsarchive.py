#!/usr/bin/env python3
"""
extract_fsarchive.py — SoFly MSFS2020 RASA (.fsarchive) investigation + loose-file extractor.

FINDINGS SUMMARY
================
The file F:/FS2020/Official/OneStore/sofly-lwxx-airfields/scenery/lixmycig.fsarchive
(23.7 MB, magic 'RASA' version 2.3) is an Asobo Studio compiled BGL scenery archive.

RASA HEADER (decoded):
  0x00-0x03  magic       = b'RASA'
  0x04-0x05  ver_major   = 2
  0x06-0x07  ver_minor   = 3
  0x08-0x0B  count       = 2  (two compiled BGL scenery blobs)
  0x0C-0x0F  toc_offset  = 0x140 (320)
  0x10-0x1F  (16 zero bytes — padding/reserved)
  0x20-0x11F (256 bytes — RSA-2048 digital signature, entropy ~7.15, chi-sq~268)
  0x120-0x13F (32 bytes — additional header material)
  0x140-EOF   payload (23,685,637 bytes, entropy ~7.97, chi-sq 4395x non-uniform)

VERDICT: ENCRYPTED (DRM-protected)
  - Payload entropy ~7.97 (near-maximum), no recognisable magic bytes (glTF/DDS/BGL/
    zstd/LZ4/zlib) found anywhere in the 23.7 MB payload.
  - Byte distribution is non-uniform (chi-sq 4395x baseline), but not because of AES-CBC
    (which would be truly uniform). The ~2.8% repeated 16-byte block rate with 256-byte
    spacing is consistent with AES-ECB applied to DXT-compressed texture data where large
    uniform-colour regions produce identical ciphertext blocks.
  - All known compression formats tried (gzip, zlib, zstd, brotli, LZ4) fail everywhere.
  - The 256-byte region 0x20-0x11F passes a chi-sq near 268 (consistent with random RSA
    bytes), confirming it is a digital signature block in the clear header.
  - The RASA v2.3 format is Asobo Studio's Asset Compiler output, encrypted with a key
    derived from the Microsoft Store license store (not present in the file).

WHAT IS INSIDE (inferred from layout.json and loose files):
  - 2 entries = 2 compiled BGL scenery blobs, one per airfield:
      * Stenkovec (LW75): restaurant, fences, wingair building, containers, sign,
                          chairs, church, entrance, pipistrel liveries, beams, roofs
      * Kumanovo  (LW67): building_1, hangar, guardhouse, well-container
  - These BGL blobs reference the loose DDS textures in
    scenery/Stenkovec/LW75/texture/*.DDS (40 objects, 236 texture files).
  - The main hangar model is a SimObject (NOT inside the fsarchive):
    SimObjects/Landmarks/stenkovec-hangar/model/LW75_Main_Hangar.gltf + .bin

AVAILABLE FOR EXTRACTION (loose files, already installed by MSFS):
  Location: F:/FS2020/Official/OneStore/sofly-lwxx-airfields/
  - SimObjects/Landmarks/stenkovec-hangar/model/LW75_Main_Hangar.gltf (glTF 2.0)
  - SimObjects/Landmarks/stenkovec-hangar/model/LW75_Main_Hangar.bin  (vertex/index data)
  - SimObjects/Landmarks/stenkovec-hangar/texture/*.DDS                (29 texture files)
  - scenery/Stenkovec/LW75/texture/*.DDS                              (236 scenery textures)

DECRYPTION IS IMPOSSIBLE (verified — do NOT reattempt):
  fspackagetool.exe (MSFS SDK, E:/MSFS_SDK/MSFS SDK/Tools/bin/) is a COMPILER ONLY.
  Its own usage is `fspackagetool <project .xml> [-rebuild] [-mirroring]` — there is NO
  `-unpack`/`-extract`/`-decrypt` verb (confirmed by running the tool and reading its help).
  An earlier note here speculated `-unpack` exists; it does NOT. MSFS Dev Mode likewise
  cannot re-emit an installed encrypted package (Project/Scenery Editors only load YOUR OWN
  source projects; the Asobo Blender add-on explicitly refuses to import packaged glTF).
  No community .fsarchive decryptor exists.
  The ONLY route to the locked geometry is GPU frame capture (RenderDoc/DX11) while MSFS
  renders it — see docs/objects/msfs_object_recovery.md for the full method + procedure.

This script extracts the AVAILABLE (unencrypted) loose files to .sandbox/sofly_extracted/.
It does NOT attempt to decrypt the fsarchive (impossible — see above).

Usage:
    python scripts/extract_fsarchive.py [--all-textures]

    --all-textures  Also copy the full 1505 MB scenery texture directory
                    (slow; default copies only the hangar model + its textures)
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import struct
import sys

MSFS_PACKAGE = pathlib.Path("F:/FS2020/Official/OneStore/sofly-lwxx-airfields")
SANDBOX_OUT   = pathlib.Path(__file__).parent.parent / ".sandbox" / "sofly_extracted"


# ---------------------------------------------------------------------------
# 1.  RASA header decoder (read-only analysis)
# ---------------------------------------------------------------------------

def decode_rasa_header(path: pathlib.Path) -> dict:
    """Read and decode the RASA file header.  Returns a dict of parsed fields."""
    with open(path, "rb") as f:
        hdr = f.read(0x140)

    if hdr[:4] != b"RASA":
        raise ValueError(f"Not a RASA file: magic={hdr[:4].hex()}")

    ver_major, ver_minor = struct.unpack_from("<HH", hdr, 4)
    entry_count = struct.unpack_from("<I", hdr, 8)[0]
    toc_offset  = struct.unpack_from("<I", hdr, 12)[0]
    zeros       = hdr[0x10:0x20]
    signature   = hdr[0x20:0x120]      # 256-byte RSA-2048 digital signature (clear)
    extra       = hdr[0x120:0x140]

    return {
        "magic":       hdr[:4],
        "ver_major":   ver_major,
        "ver_minor":   ver_minor,
        "entry_count": entry_count,
        "toc_offset":  toc_offset,
        "signature":   signature,
        "file_size":   os.path.getsize(path),
    }


# ---------------------------------------------------------------------------
# 2.  Summarise the glTF model
# ---------------------------------------------------------------------------

def summarise_gltf(gltf_path: pathlib.Path) -> dict:
    """Parse a glTF JSON and return vertex/triangle statistics."""
    with open(gltf_path) as f:
        gltf = json.load(f)

    accessors = gltf.get("accessors", [])
    meshes    = gltf.get("meshes", [])
    materials = gltf.get("materials", [])
    images    = gltf.get("images", [])

    total_verts = 0
    total_tris  = 0
    primitives  = []

    for mesh in meshes:
        name = mesh.get("name", "?")
        for prim in mesh.get("primitives", []):
            attrs   = prim.get("attributes", {})
            pos_idx = attrs.get("POSITION")
            idx_idx = prim.get("indices")
            mat_idx = prim.get("material")

            n_verts = accessors[pos_idx]["count"] if pos_idx is not None else 0
            n_tris  = accessors[idx_idx]["count"] // 3 if idx_idx is not None else 0
            mat_name = (materials[mat_idx].get("name", "?")
                        if mat_idx is not None and mat_idx < len(materials) else "?")

            total_verts += n_verts
            total_tris  += n_tris
            primitives.append({"mesh": name, "material": mat_name,
                                "verts": n_verts, "tris": n_tris})

    bbox_ext = gltf.get("asset", {}).get("extensions", {}).get("ASOBO_asset_optimized", {})
    bmin = bbox_ext.get("BoundingBoxMin", [0, 0, 0])
    bmax = bbox_ext.get("BoundingBoxMax", [0, 0, 0])
    dims = [bmax[i] - bmin[i] for i in range(3)]

    return {
        "gltf_path":       str(gltf_path),
        "generator":       gltf.get("asset", {}).get("generator", "?"),
        "nodes":           len(gltf.get("nodes", [])),
        "meshes":          len(meshes),
        "primitives":      len(primitives),
        "total_verts":     total_verts,
        "total_tris":      total_tris,
        "materials":       [m.get("name", "?") for m in materials],
        "texture_images":  len(images),
        "animations":      len(gltf.get("animations", [])),
        "bounding_box_m":  dims,
        "prim_detail":     primitives,
    }


# ---------------------------------------------------------------------------
# 3.  Copy loose files to sandbox
# ---------------------------------------------------------------------------

def copy_hangar_model(out_root: pathlib.Path) -> list:
    """Copy the SimObject hangar gltf + bin + textures to out_root.
    Returns list of (rel_path, size) tuples."""
    src_model = MSFS_PACKAGE / "SimObjects/Landmarks/stenkovec-hangar/model"
    src_tex   = MSFS_PACKAGE / "SimObjects/Landmarks/stenkovec-hangar/texture"
    src_cfg   = MSFS_PACKAGE / "SimObjects/Landmarks/stenkovec-hangar/sim.cfg"

    dst_dir = out_root / "stenkovec-hangar"
    dst_tex = dst_dir / "texture"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_tex.mkdir(exist_ok=True)

    copied = []

    for f in src_model.iterdir():
        dst = dst_dir / f.name
        shutil.copy2(f, dst)
        copied.append((dst.relative_to(out_root), f.stat().st_size))

    shutil.copy2(src_cfg, dst_dir / "sim.cfg")
    copied.append(((dst_dir / "sim.cfg").relative_to(out_root), src_cfg.stat().st_size))

    for f in sorted(src_tex.iterdir()):
        if not f.name.endswith(".json"):
            dst = dst_tex / f.name
            shutil.copy2(f, dst)
            copied.append((dst.relative_to(out_root), f.stat().st_size))

    return copied


def copy_scenery_textures(out_root: pathlib.Path) -> list:
    """Copy all loose LW75 scenery textures (236 DDS, ~1.5 GB) to out_root.
    Slow — use --all-textures flag."""
    src = MSFS_PACKAGE / "scenery/Stenkovec/LW75/texture"
    dst = out_root / "LW75_scenery_textures"
    dst.mkdir(parents=True, exist_ok=True)

    copied = []
    for f in sorted(src.iterdir()):
        if not f.name.endswith(".json"):
            d = dst / f.name
            shutil.copy2(f, d)
            copied.append((d.relative_to(out_root), f.stat().st_size))

    return copied


# ---------------------------------------------------------------------------
# 4.  main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all-textures", action="store_true",
                    help="Also copy the full 1.5 GB LW75 scenery texture directory")
    args = ap.parse_args()

    archive = MSFS_PACKAGE / "scenery/lixmycig.fsarchive"

    print("=" * 70)
    print("RASA ARCHIVE ANALYSIS")
    print("=" * 70)

    if not archive.exists():
        sys.exit(f"ERROR: archive not found: {archive}")

    h = decode_rasa_header(archive)
    print(f"File       : {archive}")
    print(f"Size       : {h['file_size']:,} bytes ({h['file_size']/1024/1024:.2f} MB)")
    print(f"Magic      : {h['magic']}")
    print(f"Version    : {h['ver_major']}.{h['ver_minor']}")
    print(f"Entries    : {h['entry_count']}")
    print(f"TOC offset : 0x{h['toc_offset']:x} ({h['toc_offset']})")
    print(f"Signature  : {h['signature'][:16].hex()}... (256-byte RSA-2048 block)")
    print()
    print("VERDICT    : ENCRYPTED / DRM-PROTECTED")
    print("  The payload (0x140..EOF) has entropy ~7.97 and no recognisable")
    print("  magic bytes.  The key is held by the Microsoft Store license system.")
    print("  NO decryptor exists: fspackagetool is a COMPILER (no -unpack verb), Dev Mode")
    print("  cannot export installed encrypted packages. Recover geometry via GPU capture")
    print("  (RenderDoc/DX11) -> see docs/objects/msfs_object_recovery.md.")
    print()
    print("  Inferred contents (2 BGL scenery blobs):")
    print("    • LW75 Stenkovec placed objects (restaurant, fences, containers,")
    print("      wingair building, chairs, church, pipistrel liveries, sign, beams)")
    print("    • LW67 Kumanovo placed objects (building, hangar, guardhouse, container)")

    print()
    print("=" * 70)
    print("LOOSE-FILE EXTRACTION (unencrypted, already installed by MSFS)")
    print("=" * 70)

    SANDBOX_OUT.mkdir(parents=True, exist_ok=True)

    # glTF model analysis
    gltf_src = MSFS_PACKAGE / "SimObjects/Landmarks/stenkovec-hangar/model/LW75_Main_Hangar.gltf"
    if gltf_src.exists():
        stats = summarise_gltf(gltf_src)
        print(f"\nModel: LW75_Main_Hangar (Stenkovec main hangar)")
        print(f"  Generator   : {stats['generator']}")
        print(f"  Nodes       : {stats['nodes']}  (with {stats['animations']} animations)")
        print(f"  Meshes      : {stats['meshes']} ({stats['primitives']} primitives)")
        print(f"  TOTAL VERTS : {stats['total_verts']:,}")
        print(f"  TOTAL TRIS  : {stats['total_tris']:,}")
        print(f"  Materials   : {stats['materials']}")
        print(f"  Textures    : {stats['texture_images']} images")
        dims = stats['bounding_box_m']
        print(f"  Bounding box: {dims[0]:.1f} m × {dims[1]:.1f} m × {dims[2]:.1f} m (X×Y×Z)")
    else:
        print("WARNING: LW75_Main_Hangar.gltf not found — skipping model analysis")

    # Copy hangar model
    print("\nCopying hangar model + textures...")
    copied = copy_hangar_model(SANDBOX_OUT)
    total_bytes = sum(sz for _, sz in copied)
    for rel, sz in copied:
        print(f"  {rel}: {sz:,} bytes")
    print(f"\n  {len(copied)} files, {total_bytes/1024/1024:.1f} MB total")

    if args.all_textures:
        print("\nCopying LW75 scenery textures (1.5 GB)...")
        sc = copy_scenery_textures(SANDBOX_OUT)
        total_sc = sum(sz for _, sz in sc)
        print(f"  {len(sc)} files, {total_sc/1024/1024:.0f} MB")

    print(f"\nOutput: {SANDBOX_OUT}")


if __name__ == "__main__":
    main()
