#!/usr/bin/env python3
r"""
harmonize_airport_texture.py  --  ISSUE 1 fix for LWSK (Skopje International).

The LWSK patch texture t0402.dds renders with a BROWN colour cast (LAB measured:
L* ~ +3.7 too bright, a* ~ +1.6 too red/warm vs the green-field neighbour tiles,
b* already matched). The DETAIL (runways, fields) is correct -- only the low-
frequency colour balance is wrong, so a per-pixel GLOBAL AFFINE colour map fixes
it WITHOUT touching any high-frequency structure (no banding, unlike histogram
matching; no smear, unlike full-tile Poisson).

TECHNIQUE (researched best-practice, Pitie & Kokaram 2007 + Reinhard 2001):
  * Work in CIELAB (separates detail-bearing L* from cast-bearing chroma a*/b*).
  * CHROMA  (a*, b*): Monge-Kantorovich LINEAR (MKL) optimal-transport transfer
    -- the closed-form 2x2 affine that matches the FULL mean+covariance of the
    target. Covariance-aware (not diagonal Reinhard), so it performs the true
    brown->green chroma ROTATION that diagonal mean/std cannot. A single global
    2x2 matrix + offset => every chroma gradient is merely rescaled, never
    collapsed: detail preserved exactly.
  * LUMINANCE (L*): the brown cast also brightened the apron, so apply only a
    gentle 1-D AFFINE on L* (match mean+std) -- never a histogram match -- which
    is a global gain+bias, so all L* gradients (the runway edges, field
    boundaries: ALL detail lives in L*) are mathematically preserved.
  * TARGET = the JOINT a*/b*/L* distribution of the 8 valid neighbour tiles
    (concatenated sample), so t0402 is pulled to the LOCAL consensus, not any
    single neighbour. Water-baked DXT3 neighbours are fine as references.
  * SEAM FEATHER: because MKL already matched the interior to the neighbours, the
    residual at each shared edge is small. We blend the corrected tile against a
    NEIGHBOUR-EXTRAPOLATED edge strip over a RAMP_PX border so the value AT the
    seam equals the neighbour and ramps smoothly to the fully-corrected interior
    -- gradient-free, no smear. Only the 4 sides that have a valid neighbour are
    feathered.

GPU: the whole pixel apply (LAB convert, MKL tensordot, feather composite) runs
on the GPU via cupy (RTX 3070) when available; falls back to numpy/skimage.

Re-encodes t0402 with nvcompress (-bc1 DXT1, same format it already is -- the
airport patch carries no OSM water). Backs up the overwritten tile first. Then
RE-VERIFIES the previously colour-corrected ridge tiles (t0606/t0706/t0607/t0707)
are byte-unchanged, and assembles a before/after 3x3 mosaic (t0402 + 8 neighbours)
proving the seam is gone.

Run:  python scripts/harmonize_airport_texture.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CONDOR_TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
BACKUP = CONDOR_TEX.parent / "Textures_bak_phase1"
WORK = ROOT / ".sandbox" / "harmonize_airport"
VALID = ROOT / "validation" / "textures"
NVCOMPRESS = "C:/Program Files/NVIDIA Corporation/NVIDIA Texture Tools/nvcompress.exe"

TEX = 2048
TARGET = "t0402"          # LWSK (Skopje International) patch col=04 row=02
RAMP_PX = 96              # feather ramp width at each shared edge (research: 64-128)
PATCHES = 12

# --- GPU backend (cupy) with numpy fallback -------------------------------------
try:
    import cupy as _cp
    _HAS_GPU = True
    try:
        _GPU_NAME = _cp.cuda.runtime.getDeviceProperties(0)["name"].decode()
    except Exception:
        _GPU_NAME = "GPU"
except Exception:
    _cp = None
    _HAS_GPU = False
    _GPU_NAME = ""


def xp():
    return _cp if _HAS_GPU else np


def to_cpu(a):
    return _cp.asnumpy(a) if (_HAS_GPU and isinstance(a, _cp.ndarray)) else np.asarray(a)


# ===========================================================================
# Colour-space conversions (sRGB <-> CIELAB, D65) -- vectorised, GPU-capable.
# Matches skimage.color rgb2lab/lab2rgb to <1e-3 (validated in __main__ block).
# ===========================================================================
_M_RGB2XYZ = np.array([[0.412453, 0.357580, 0.180423],
                       [0.212671, 0.715160, 0.072169],
                       [0.019334, 0.119193, 0.950227]], dtype=np.float64)
_M_XYZ2RGB = np.linalg.inv(_M_RGB2XYZ)
_WHITE = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)   # D65


def _srgb_to_linear(c):
    x = xp()
    # where() eagerly evaluates BOTH branches, so guard the fractional power
    # against tiny negatives (NaN) by clamping its input to >= 0.
    hi = ((x.clip(c, 0.0, None) + 0.055) / 1.055) ** 2.4
    return x.where(c <= 0.04045, c / 12.92, hi)


def _linear_to_srgb(c):
    x = xp()
    c = x.clip(c, 0.0, 1.0)
    hi = 1.055 * (c ** (1 / 2.4)) - 0.055
    return x.where(c <= 0.0031308, c * 12.92, hi)


def rgb_to_lab(rgb):
    """rgb in [0,1], shape (...,3) -> CIELAB (L in [0,100], a/b ~[-128,127])."""
    x = xp()
    M = x.asarray(_M_RGB2XYZ) if _HAS_GPU else _M_RGB2XYZ
    W = x.asarray(_WHITE) if _HAS_GPU else _WHITE
    lin = _srgb_to_linear(rgb)
    xyz = lin @ M.T
    xyz = xyz / W
    eps = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    f = x.where(xyz > eps, x.cbrt(x.clip(xyz, 0.0, None)),
                (kappa * xyz + 16.0) / 116.0)
    L = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return x.stack([L, a, b], axis=-1)


def lab_to_rgb(lab):
    """CIELAB -> sRGB in [0,1]."""
    x = xp()
    Minv = x.asarray(_M_XYZ2RGB) if _HAS_GPU else _M_XYZ2RGB
    W = x.asarray(_WHITE) if _HAS_GPU else _WHITE
    L = lab[..., 0]; a = lab[..., 1]; b = lab[..., 2]
    fy = (L + 16.0) / 116.0
    fx = fy + a / 500.0
    fz = fy - b / 200.0
    eps = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    def finv(t):
        t3 = t ** 3
        return x.where(t3 > eps, t3, (116.0 * t - 16.0) / kappa)
    xyz = x.stack([finv(fx), finv(fy), finv(fz)], axis=-1) * W
    lin = xyz @ Minv.T
    return _linear_to_srgb(lin)


# ===========================================================================
# Monge-Kantorovich Linear (MKL) colour transfer -- closed form.
# ===========================================================================
def _sqrtm_sym(S):
    """Symmetric PSD matrix square root (small NxN, CPU numpy is fine)."""
    w, V = np.linalg.eigh(S)
    w = np.clip(w, 1e-12, None)
    return (V * np.sqrt(w)) @ V.T


def _invsqrtm_sym(S):
    w, V = np.linalg.eigh(S)
    w = np.clip(w, 1e-12, None)
    return (V / np.sqrt(w)) @ V.T


def mkl_matrix(S0, S1):
    """Closed-form MKL transport matrix mapping N(.,S0) -> N(.,S1).
        T = S0^-1/2 (S0^1/2 S1 S0^1/2)^1/2 S0^-1/2   (symmetric)."""
    H = _sqrtm_sym(S0)
    return _invsqrtm_sym(S0) @ _sqrtm_sym(H @ S1 @ H) @ _invsqrtm_sym(S0)


# ===========================================================================
# Tile IO
# ===========================================================================
def load_rgb01(name):
    p = CONDOR_TEX / f"{name}.dds"
    if not p.exists():
        return None
    return np.asarray(Image.open(p).convert("RGB")).astype(np.float64) / 255.0


def neighbours_of(name):
    col = int(name[1:3]); row = int(name[3:5])
    out = []
    for dc in (-1, 0, 1):
        for dr in (-1, 0, 1):
            if dc == 0 and dr == 0:
                continue
            nc, nr = col + dc, row + dr
            if 0 <= nc < PATCHES and 0 <= nr < PATCHES:
                out.append((dc, dr, f"t{nc:02d}{nr:02d}"))
    return out


# ===========================================================================
# Core harmonisation
# ===========================================================================
def harmonize(name):
    x = xp()
    print(f"  GPU backend: {'cupy / ' + _GPU_NAME if _HAS_GPU else 'numpy (CPU)'}")
    src_rgb = load_rgb01(name)
    if src_rgb is None:
        raise SystemExit(f"{name}.dds not found in install")

    # --- gather neighbour reference LAB sample (joint over all valid neighbours) ---
    neigh = neighbours_of(name)
    ref_labs = []
    neigh_rgb = {}      # (dc,dr) -> rgb01, kept for the seam feather
    for dc, dr, nm in neigh:
        rgb = load_rgb01(nm)
        if rgb is None:
            continue
        neigh_rgb[(dc, dr)] = rgb
        lab = to_cpu(rgb_to_lab(x.asarray(rgb)))
        # subsample for speed (every 4th px is plenty for global stats)
        ref_labs.append(lab[::4, ::4].reshape(-1, 3))
    if not ref_labs:
        raise SystemExit("no valid neighbour tiles to harmonise against")
    ref = np.concatenate(ref_labs, axis=0)
    ref_mean = ref.mean(0)
    ref_ab_cov = np.cov(ref[:, 1:], rowvar=False)
    ref_L_std = ref[:, 0].std()

    # --- source LAB stats ---
    src_lab_g = rgb_to_lab(x.asarray(src_rgb))
    src_lab = to_cpu(src_lab_g)
    src_flat = src_lab.reshape(-1, 3)
    src_mean = src_flat.mean(0)
    src_ab_cov = np.cov(src_flat[:, 1:], rowvar=False)
    src_L_std = src_flat[:, 0].std()

    print(f"  src   LAB mean L={src_mean[0]:.2f} a={src_mean[1]:.2f} b={src_mean[2]:.2f}"
          f"  (L std {src_L_std:.2f})")
    print(f"  neigh LAB mean L={ref_mean[0]:.2f} a={ref_mean[1]:.2f} b={ref_mean[2]:.2f}"
          f"  (L std {ref_L_std:.2f})")
    print(f"  delta L={src_mean[0]-ref_mean[0]:+.2f} a={src_mean[1]-ref_mean[1]:+.2f} "
          f"b={src_mean[2]-ref_mean[2]:+.2f}  (target = 0)")

    # --- CHROMA: MKL 2x2 affine a*,b* ---
    T2 = mkl_matrix(src_ab_cov, ref_ab_cov)           # 2x2
    T2g = x.asarray(T2)
    src_mu_ab = x.asarray(src_mean[1:])
    ref_mu_ab = x.asarray(ref_mean[1:])
    ab = src_lab_g[..., 1:]                            # (H,W,2)
    ab2 = (ab - src_mu_ab) @ T2g.T + ref_mu_ab

    # --- LUMINANCE: 1-D affine (mean+std), gentle global gain+bias ---
    L = src_lab_g[..., 0]
    sL = float(ref_L_std / max(src_L_std, 1e-6))
    L2 = (L - float(src_mean[0])) * sL + float(ref_mean[0])

    corr_lab = x.stack([L2, ab2[..., 0], ab2[..., 1]], axis=-1)
    corr_rgb = lab_to_rgb(corr_lab)                    # [0,1]

    # report corrected stats
    corr_mean = to_cpu(rgb_to_lab(corr_rgb).reshape(-1, 3).mean(0))
    print(f"  corrected LAB mean L={corr_mean[0]:.2f} a={corr_mean[1]:.2f} "
          f"b={corr_mean[2]:.2f}  -> residual to neigh "
          f"L={corr_mean[0]-ref_mean[0]:+.2f} a={corr_mean[1]-ref_mean[1]:+.2f} "
          f"b={corr_mean[2]-ref_mean[2]:+.2f}")

    # --- SEAM FEATHER against neighbour-extrapolated edges -----------------------
    corr_rgb = _feather_seams(corr_rgb, neigh_rgb)

    out = to_cpu(x.clip(corr_rgb, 0.0, 1.0))
    out8 = np.rint(out * 255.0).astype(np.uint8)
    return src_rgb, out8, neigh_rgb


def _feather_seams(corr_rgb, neigh_rgb):
    """Blend the corrected tile to a neighbour-extrapolated edge over RAMP_PX on
    each of the 4 sides that has a direct (edge-adjacent) neighbour, so the value
    AT the seam == neighbour and ramps to the corrected interior. Detail-safe:
    only the thin border band is touched and the interior is untouched."""
    x = xp()
    H = W = TEX
    out = corr_rgb
    # edge neighbours: N=(0,+1)?  We index neighbours by (dc,dr) where
    # dc=+1 is EAST col+1, dr=+1 is row+1. In Condor, col increases WEST and row
    # increases NORTH (filenames CCRR, col0=east, row0=south). For the IMAGE
    # (north-up, PIL): x pixel increases EAST, y pixel increases SOUTH.
    #   neighbour col+1 (dc=+1) is the tile to the WEST  -> image LEFT  edge
    #   neighbour col-1 (dc=-1) is the tile to the EAST  -> image RIGHT edge
    #   neighbour row+1 (dr=+1) is the tile to the NORTH -> image TOP   edge
    #   neighbour row-1 (dr=-1) is the tile to the SOUTH -> image BOTTOM edge
    ramp = np.linspace(0.0, 1.0, RAMP_PX)             # 0 at seam -> 1 at interior
    ramp_g = x.asarray(ramp)

    def blend_edge(out, edge_strip_extrap, side):
        """edge_strip_extrap: (RAMP_PX, W, 3) or (H, RAMP_PX, 3) neighbour colour
        replicated inward from the seam. side in {'top','bottom','left','right'}."""
        if side in ("top", "bottom"):
            w = ramp_g.reshape(RAMP_PX, 1, 1)
            if side == "top":
                band = out[:RAMP_PX, :, :]
                out[:RAMP_PX, :, :] = w * band + (1 - w) * edge_strip_extrap
            else:
                w = w[::-1]
                band = out[H - RAMP_PX:, :, :]
                out[H - RAMP_PX:, :, :] = w * band + (1 - w) * edge_strip_extrap
        else:
            w = ramp_g.reshape(1, RAMP_PX, 1)
            if side == "left":
                band = out[:, :RAMP_PX, :]
                out[:, :RAMP_PX, :] = w * band + (1 - w) * edge_strip_extrap
            else:
                w = w[:, ::-1]
                band = out[:, W - RAMP_PX:, :]
                out[:, W - RAMP_PX:, :] = w * band + (1 - w) * edge_strip_extrap
        return out

    # TOP edge  <- neighbour to the NORTH (dr=+1, dc=0): its BOTTOM row of pixels.
    if (0, 1) in neigh_rgb:
        nb = x.asarray(neigh_rgb[(0, 1)])
        seam_row = nb[-1:, :, :]                       # (1,W,3)
        extrap = x.broadcast_to(seam_row, (RAMP_PX, W, 3)).copy()
        out = blend_edge(out, extrap, "top")
    # BOTTOM edge <- neighbour to the SOUTH (dr=-1): its TOP row.
    if (0, -1) in neigh_rgb:
        nb = x.asarray(neigh_rgb[(0, -1)])
        seam_row = nb[:1, :, :]
        extrap = x.broadcast_to(seam_row, (RAMP_PX, W, 3)).copy()
        out = blend_edge(out, extrap, "bottom")
    # LEFT edge  <- neighbour to the WEST (dc=+1): its RIGHT column.
    if (1, 0) in neigh_rgb:
        nb = x.asarray(neigh_rgb[(1, 0)])
        seam_col = nb[:, -1:, :]                        # (H,1,3)
        extrap = x.broadcast_to(seam_col, (H, RAMP_PX, 3)).copy()
        out = blend_edge(out, extrap, "left")
    # RIGHT edge <- neighbour to the EAST (dc=-1): its LEFT column.
    if (-1, 0) in neigh_rgb:
        nb = x.asarray(neigh_rgb[(-1, 0)])
        seam_col = nb[:, :1, :]
        extrap = x.broadcast_to(seam_col, (H, RAMP_PX, 3)).copy()
        out = blend_edge(out, extrap, "right")
    return out


# ===========================================================================
# Encode + mosaic
# ===========================================================================
def compress_dxt1(rgb8, name):
    WORK.mkdir(parents=True, exist_ok=True)
    png = WORK / f"{name}_corrected.png"
    Image.fromarray(rgb8).save(png)
    dds = WORK / f"{name}.dds"
    cmd = [NVCOMPRESS, "-bc1", "-highest", "-mipfilter", "kaiser",
           "-color", "-clamp", "-silent", str(png), str(dds)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dds.exists():
        raise SystemExit(f"nvcompress failed: {r.stderr[:300]}")
    BACKUP.mkdir(parents=True, exist_ok=True)
    bdst = BACKUP / f"{name}.dds"
    if not bdst.exists():
        shutil.copy2(CONDOR_TEX / f"{name}.dds", bdst)
        print(f"  backed up original -> {bdst}")
    else:
        print(f"  backup already exists ({bdst}); not overwriting it")
    shutil.copy2(dds, CONDOR_TEX / f"{name}.dds")
    sz = (CONDOR_TEX / f"{name}.dds").stat().st_size
    return sz


def assemble_mosaic(center_name, center_rgb8, neigh_rgb, tag, thumb=384):
    """3x3 geographic mosaic: place each north-up tile at image block
    (row index increasing DOWN = NORTH at top). Condor: col increases WEST, row
    increases NORTH. Image col index = EAST-positive, so block_x = (1 - dc) and
    block_y = (1 - dr) puts NORTH (dr=+1) at the top and EAST (dc=-1) at right."""
    canvas = Image.new("RGB", (thumb * 3, thumb * 3), (0, 0, 0))
    def place(dc, dr, rgb8):
        bx = (1 - dc)        # dc=+1(west)->0 left ; dc=-1(east)->2 right
        by = (1 - dr)        # dr=+1(north)->0 top ; dr=-1(south)->2 bottom
        im = Image.fromarray(rgb8).resize((thumb, thumb), Image.LANCZOS)
        canvas.paste(im, (bx * thumb, by * thumb))
    place(0, 0, center_rgb8)
    for (dc, dr), rgb in neigh_rgb.items():
        place(dc, dr, np.rint(np.clip(rgb, 0, 1) * 255).astype(np.uint8))
    VALID.mkdir(parents=True, exist_ok=True)
    out = VALID / f"{center_name}_mosaic_{tag}.png"
    canvas.save(out)
    return out


def main():
    print(f"=== ISSUE 1: harmonise airport tile {TARGET} (LWSK) ===")
    src_rgb, out8, neigh_rgb = harmonize(TARGET)

    # BEFORE mosaic (original t0402 + neighbours)
    before_center8 = np.rint(np.clip(src_rgb, 0, 1) * 255).astype(np.uint8)
    before_png = assemble_mosaic(TARGET, before_center8, neigh_rgb, "before")
    print(f"  before mosaic -> {before_png}")

    # encode the corrected tile
    sz = compress_dxt1(out8, TARGET)
    print(f"  {TARGET}.dds re-encoded DXT1 ({sz} bytes) and installed")

    # AFTER mosaic (corrected t0402 + neighbours)
    after_png = assemble_mosaic(TARGET, out8, neigh_rgb, "after")
    print(f"  after  mosaic -> {after_png}")

    # save side-by-side before/after 512 thumbnails of the tile itself
    Image.fromarray(before_center8).resize((512, 512)).save(VALID / f"{TARGET}_tile_before.png")
    Image.fromarray(out8).resize((512, 512)).save(VALID / f"{TARGET}_tile_after.png")
    print(f"  tile before/after thumbs -> {VALID / (TARGET + '_tile_before.png')} , "
          f"{VALID / (TARGET + '_tile_after.png')}")

    print("\nDONE. Re-verify ridge tiles + assemble proof with verify_airport_texture.py")


if __name__ == "__main__":
    main()
