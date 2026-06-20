#!/usr/bin/env python3
"""
Generate Condor 2 loading-screen BMP images for the Macedonia Skopje landscape.

Condor 2 loading screens are typically 1920x1080 (or 2048x1024) 24-bit BMP files
named Loading.bmp, Loading01.bmp, Loading02.bmp, etc. They are shown randomly
while the simulator loads the landscape.

This script composites project data:
  - orthophoto mosaic tiles (.sandbox/textures/mosaic_*.png) for aerial views
  - the 30 m DEM (sources/dem/macedonia_skopje_dem_2305.bil) for a relief map

It also draws a stylised sailplane silhouette and the landscape title.
"""

from __future__ import annotations

import argparse
import math
import shutil
import struct
import sys
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
TARGET_SIZE: Tuple[int, int] = (1920, 1080)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output" / "loading"

# Default Condor 2 landscape folder (overridable via --condor-dir)
CONDOR_LANDSCAPE_DIR = Path("C:/Condor2/Landscapes/MacedoniaSkopje")

# Source data
TEXTURES_DIR = PROJECT_ROOT / ".sandbox" / "textures"
DEM_FILE = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_2305.bil"
DEM_HDR = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_2305.hdr"

# The centre mosaic tile covers the Skopje area (see mosaic_t0101.json).
ORTHO_CENTRE = TEXTURES_DIR / "mosaic_t0101.png"

# Windows TrueType fonts (fallback to PIL default if unavailable)
FONT_BOLD = Path("C:/Windows/Fonts/arialbd.ttf")
FONT_REGULAR = Path("C:/Windows/Fonts/arial.ttf")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    except Exception:
        pass
    return ImageFont.load_default()


def add_top_gradient(img: Image.Image, height: int = 240, max_alpha: int = 170) -> Image.Image:
    """Darken the top of the image so white text remains readable."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(min(height, img.height)):
        alpha = int(max_alpha * (1.0 - y / height))
        draw.line([(0, y), (img.width, y)], fill=(0, 0, 0, alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def add_centered_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    size: int = 72,
    bold: bool = True,
    color: Tuple[int, int, int] = (255, 255, 255),
    shadow: Tuple[int, int, int] = (0, 0, 0),
    shadow_offset: int = 3,
) -> None:
    font = get_font(size, bold=bold)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (TARGET_SIZE[0] - tw) // 2
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=color)


def add_corner_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    size: int = 24,
    bold: bool = False,
    color: Tuple[int, int, int] = (255, 255, 255),
    shadow: Tuple[int, int, int] = (0, 0, 0),
) -> None:
    font = get_font(size, bold=bold)
    draw.text((x + 2, y + 2), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=color)


def draw_glider_silhouette(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    scale: float = 1.0,
    color: Tuple[int, int, int] = (255, 255, 255),
    shadow: Tuple[int, int, int] = (0, 0, 0),
) -> None:
    """Draw a simple stylised sailplane side-view silhouette."""

    def t(p: Tuple[float, float]) -> Tuple[float, float]:
        return (cx + p[0] * scale, cy + p[1] * scale)

    # Fuselage/teardrop
    fuselage = [(-40, 6), (-35, -6), (30, -7), (48, -2), (52, 5), (48, 12), (30, 16), (-35, 14)]
    # Main wing (swept, high aspect)
    wing = [(-6, -6), (6, -52), (22, -52), (18, -6)]
    # Horizontal stabiliser
    tailplane = [(34, -4), (40, -20), (52, -20), (48, -4)]
    # Fin
    fin = [(36, -4), (42, -24), (50, -24), (46, -4)]

    parts = [fuselage, wing, tailplane, fin]
    # Drop shadow
    for part in parts:
        draw.polygon([t((x + 1.5, y + 1.5)) for x, y in part], fill=shadow)
    # Silhouette
    for part in parts:
        draw.polygon([t(p) for p in part], fill=color)


def crop_to_aspect(
    img: Image.Image, aspect: float = 16.0 / 9.0, anchor: str = "center"
) -> Image.Image:
    """Crop a PIL image to the requested aspect ratio."""
    w, h = img.size
    target_h = int(round(w / aspect))
    if target_h > h:
        target_w = int(round(h * aspect))
        target_h = h
    else:
        target_w = w

    left = (w - target_w) // 2
    top = (h - target_h) // 2

    if anchor == "top":
        top = 0
    elif anchor == "bottom":
        top = h - target_h
    elif anchor == "upper":
        top = int((h - target_h) * 0.25)
    elif anchor == "lower":
        top = int((h - target_h) * 0.75)

    return img.crop((left, top, left + target_w, top + target_h))


def save_bmp_24bit(img: Image.Image, path: Path) -> None:
    """Save a 24-bit RGB BMP file."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(str(path), "BMP")


# -----------------------------------------------------------------------------
# DEM / relief helpers
# -----------------------------------------------------------------------------
def read_envi_hdr(hdr_path: Path) -> dict:
    meta: dict = {}
    with open(hdr_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            meta[key.strip()] = value.strip().strip("{}").strip()
    return meta


def read_dem(dem_path: Path, hdr_path: Path) -> np.ndarray:
    hdr = read_envi_hdr(hdr_path)
    ncols = int(hdr.get("samples", hdr.get("NCOLS", 0)))
    nrows = int(hdr.get("lines", hdr.get("NROWS", 0)))
    data_type = int(hdr.get("data type", hdr.get("NBITS", 2)))

    if data_type == 2:
        dtype = np.int16
    elif data_type == 4:
        dtype = np.float32
    else:
        dtype = np.int16

    arr = np.fromfile(dem_path, dtype=dtype).reshape(nrows, ncols).astype(np.float32)
    nodata = float(hdr.get("data ignore value", hdr.get("NODATA", -32768)))
    arr[arr == nodata] = np.nan
    return arr


def hillshade(
    dem: np.ndarray, azimuth: float = 315.0, altitude: float = 45.0, cellsize: float = 30.0
) -> np.ndarray:
    """Compute a standard hillshade (0-255 uint8)."""
    dx, dy = np.gradient(dem, cellsize)
    slope = np.pi / 2.0 - np.arctan(np.sqrt(dx * dx + dy * dy))
    aspect = np.arctan2(-dx, dy)

    az_rad = math.radians(azimuth)
    alt_rad = math.radians(altitude)

    hs = (
        math.sin(alt_rad) * np.sin(slope)
        + math.cos(alt_rad) * np.cos(slope) * np.cos(az_rad - aspect)
    )
    hs = np.clip(hs, 0.0, 1.0)
    hs[np.isnan(dem)] = 0.0
    return (hs * 255.0).astype(np.uint8)


def colorize_dem(dem: np.ndarray) -> np.ndarray:
    """Apply a simple terrain colour ramp to the DEM (returns uint8 RGB)."""
    valid = dem[~np.isnan(dem)]
    if valid.size == 0:
        zmin, zmax = 0.0, 1.0
    else:
        zmin, zmax = float(valid.min()), float(valid.max())
    zrange = max(zmax - zmin, 1.0)

    idx = np.clip(((dem - zmin) / zrange * 255.0), 0, 255).astype(np.uint8)

    # Build a terrain-ish LUT
    lut = np.zeros((256, 3), dtype=np.uint8)
    for i in range(256):
        t = i / 255.0
        if t < 0.20:
            # low farmland / valley
            r = int(80 + t * 5.0 * 60)
            g = int(120 + t * 5.0 * 80)
            b = int(60 + t * 5.0 * 20)
        elif t < 0.45:
            # forested hills
            r = int(140 + (t - 0.20) * 4.0 * 60)
            g = int(200 - (t - 0.20) * 4.0 * 30)
            b = int(80 - (t - 0.20) * 4.0 * 40)
        elif t < 0.70:
            # rocky highlands
            r = int(200 + (t - 0.45) * 4.0 * 35)
            g = int(170 - (t - 0.45) * 4.0 * 70)
            b = int(80 - (t - 0.45) * 4.0 * 50)
        elif t < 0.90:
            # bare rock / scree
            v = int(235 - (t - 0.70) * 5.0 * 60)
            r = g = b = v
        else:
            # snow caps
            v = int(205 + (t - 0.90) * 10.0 * 50)
            r = g = b = v
        lut[i] = (r, g, b)

    rgb = lut[idx]
    rgb[np.isnan(dem)] = [160, 190, 220]  # nodata = light blue
    return rgb


# -----------------------------------------------------------------------------
# Image generators
# -----------------------------------------------------------------------------
def generate_ortho_loading(
    ortho: Image.Image,
    name: str,
    anchor: str = "center",
    subtitle: str = "Condor 2 Soaring Simulator",
) -> Image.Image:
    """Create an aerial-photo loading screen from the orthophoto mosaic."""
    cropped = crop_to_aspect(ortho, anchor=anchor)
    img = cropped.resize(TARGET_SIZE, Image.Resampling.LANCZOS)

    # Slight contrast / sharpness boost
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Sharpness(img).enhance(1.15)

    img = add_top_gradient(img, height=240, max_alpha=165)

    draw = ImageDraw.Draw(img)
    add_centered_text(draw, "Macedonia Skopje", y=55, size=92, bold=True)
    add_centered_text(draw, subtitle, y=160, size=34, bold=False)

    # Condor-style corner branding + glider silhouette
    add_corner_text(draw, "CONDOR 2", x=40, y=TARGET_SIZE[1] - 55, size=26, bold=True)
    draw_glider_silhouette(draw, TARGET_SIZE[0] - 130, TARGET_SIZE[1] - 90, scale=1.1)

    return img


def generate_relief_loading(dem: np.ndarray, name: str) -> Image.Image:
    """Create a shaded-relief loading screen from the DEM (full map view)."""
    colored = colorize_dem(dem)
    hs = hillshade(dem, azimuth=315, altitude=50, cellsize=30.0)

    # Multiply hillshade into colour
    shaded = (colored.astype(np.float32) * hs[:, :, None].astype(np.float32) / 255.0).astype(
        np.uint8
    )
    img = Image.fromarray(shaded)
    img = crop_to_aspect(img, anchor="center")
    img = img.resize(TARGET_SIZE, Image.Resampling.LANCZOS)

    # Boost contrast so relief pops
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Sharpness(img).enhance(1.20)

    img = add_top_gradient(img, height=250, max_alpha=180)

    # Bottom gradient for lower text readability
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    bot_h = 120
    for y in range(bot_h):
        alpha = int(140 * (y / bot_h))
        yy = img.height - bot_h + y
        odraw.line([(0, yy), (img.width, yy)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    add_centered_text(draw, "Macedonia Skopje", y=55, size=92, bold=True)
    add_centered_text(draw, "Condor 2 Soaring Simulator", y=160, size=34, bold=False)

    add_corner_text(draw, "CONDOR 2", x=40, y=TARGET_SIZE[1] - 55, size=26, bold=True)
    draw_glider_silhouette(draw, TARGET_SIZE[0] - 130, TARGET_SIZE[1] - 90, scale=1.1)

    return img


def generate_relief_zoom_loading(dem: np.ndarray, name: str) -> Image.Image:
    """Create a zoomed DEM relief screen focusing on a sub-region with warm tones."""
    rows, cols = dem.shape
    # Crop to the upper-centre portion (mountainous area north of Skopje)
    r0 = int(rows * 0.05)
    r1 = int(rows * 0.55)
    c0 = int(cols * 0.20)
    c1 = int(cols * 0.80)
    sub = dem[r0:r1, c0:c1]

    colored = colorize_dem(sub)
    hs = hillshade(sub, azimuth=280, altitude=40, cellsize=30.0)

    shaded = (colored.astype(np.float32) * hs[:, :, None].astype(np.float32) / 255.0).astype(
        np.uint8
    )
    img = Image.fromarray(shaded)
    img = crop_to_aspect(img, anchor="center")
    img = img.resize(TARGET_SIZE, Image.Resampling.LANCZOS)

    # Warm colour cast (evening light feeling)
    r, g, b = img.split()
    r = ImageEnhance.Brightness(r.convert("L").convert("RGB")).enhance(1.12).split()[0]
    img = Image.merge("RGB", (r, g, b))

    img = ImageEnhance.Contrast(img).enhance(1.20)
    img = ImageEnhance.Sharpness(img).enhance(1.15)
    img = ImageEnhance.Color(img).enhance(1.10)

    img = add_top_gradient(img, height=260, max_alpha=185)

    # Bottom gradient
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    bot_h = 120
    for y in range(bot_h):
        alpha = int(140 * (y / bot_h))
        yy = img.height - bot_h + y
        odraw.line([(0, yy), (img.width, yy)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    add_centered_text(draw, "Macedonia Skopje", y=55, size=92, bold=True)
    add_centered_text(draw, "Vardar Valley & Skopje", y=160, size=34, bold=False)

    add_corner_text(draw, "CONDOR 2", x=40, y=TARGET_SIZE[1] - 55, size=26, bold=True)
    draw_glider_silhouette(draw, TARGET_SIZE[0] - 130, TARGET_SIZE[1] - 90, scale=1.1)

    return img


def generate_dramatic_mountain_loading(dem: np.ndarray, name: str) -> Image.Image:
    """Create a dramatic mountain perspective screen with strong shadows and cool tones."""
    rows, cols = dem.shape
    # Crop to the SW mountain ranges (avoid the flat Skopje valley)
    r0 = int(rows * 0.40)
    r1 = int(rows * 0.95)
    c0 = int(cols * 0.0)
    c1 = int(cols * 0.55)
    sub = dem[r0:r1, c0:c1]

    colored = colorize_dem(sub)
    # Low-ish sun for dramatic shadows, but not so low that valleys go black
    hs = hillshade(sub, azimuth=225, altitude=35, cellsize=30.0)

    shaded = (colored.astype(np.float32) * hs[:, :, None].astype(np.float32) / 255.0)
    # Lift the shadows slightly so dark valleys aren't pure black
    shaded = np.clip(shaded * 0.85 + 30.0, 0, 255).astype(np.uint8)

    img = Image.fromarray(shaded)
    img = crop_to_aspect(img, anchor="center")
    img = img.resize(TARGET_SIZE, Image.Resampling.LANCZOS)

    # Cool blue-grey tone (dramatic mountain atmosphere)
    r, g, b = img.split()
    b = ImageEnhance.Brightness(b.convert("L").convert("RGB")).enhance(1.15).split()[0]
    img = Image.merge("RGB", (r, g, b))

    img = ImageEnhance.Contrast(img).enhance(1.25)
    img = ImageEnhance.Sharpness(img).enhance(1.25)

    img = add_top_gradient(img, height=280, max_alpha=195)

    # Bottom gradient
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    bot_h = 140
    for y in range(bot_h):
        alpha = int(160 * (y / bot_h))
        yy = img.height - bot_h + y
        odraw.line([(0, yy), (img.width, yy)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    add_centered_text(draw, "Macedonia Skopje", y=55, size=92, bold=True)
    add_centered_text(draw, "Soaring the Balkan Mountains", y=160, size=34, bold=False)

    add_corner_text(draw, "CONDOR 2", x=40, y=TARGET_SIZE[1] - 55, size=26, bold=True)
    draw_glider_silhouette(draw, TARGET_SIZE[0] - 130, TARGET_SIZE[1] - 90, scale=1.1)

    return img


def generate_fallback_loading(index: int) -> Image.Image:
    """Generate a plain gradient loading screen when no project data is available."""
    img = Image.new("RGB", TARGET_SIZE, (25, 60, 100))
    draw = ImageDraw.Draw(img)
    # Simple gradient
    for y in range(img.height):
        r = int(25 + (y / img.height) * 60)
        g = int(60 + (y / img.height) * 80)
        b = int(100 + (y / img.height) * 80)
        draw.line([(0, y), (img.width, y)], fill=(r, g, b))

    img = add_top_gradient(img, height=250, max_alpha=160)
    draw = ImageDraw.Draw(img)
    add_centered_text(draw, "Macedonia Skopje", y=60, size=92, bold=True)
    add_centered_text(draw, "Condor 2 Soaring Simulator", y=165, size=34, bold=False)
    add_corner_text(draw, "CONDOR 2", x=40, y=TARGET_SIZE[1] - 55, size=26, bold=True)
    draw_glider_silhouette(draw, TARGET_SIZE[0] - 130, TARGET_SIZE[1] - 90, scale=1.1)
    return img


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Condor 2 loading screens")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Output directory for BMP files (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--condor-dir",
        type=Path,
        default=CONDOR_LANDSCAPE_DIR,
        help=f"Condor landscape directory to copy files into (default: {CONDOR_LANDSCAPE_DIR})",
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="Do not copy generated files to the Condor landscape folder",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = ensure_dir(args.output_dir)

    generated: List[Path] = []

    # --- Load DEM (used by all DEM-based screens) ---
    dem: np.ndarray | None = None
    if DEM_FILE.exists() and DEM_HDR.exists():
        print(f"Loading DEM: {DEM_FILE}")
        dem = read_dem(DEM_FILE, DEM_HDR)
        print(f"  DEM shape: {dem.shape}, "
              f"elevation range: {np.nanmin(dem):.0f} - {np.nanmax(dem):.0f} m")
    else:
        print(f"DEM not found: {DEM_FILE}")

    # --- Orthophoto-based screens (if available) ---
    if ORTHO_CENTRE.exists():
        print(f"Loading orthophoto: {ORTHO_CENTRE}")
        Image.MAX_IMAGE_PIXELS = None
        ortho = Image.open(ORTHO_CENTRE)
        if ortho.mode != "RGB":
            ortho = ortho.convert("RGB")

        img1 = generate_ortho_loading(
            ortho, "Loading01", anchor="center", subtitle="Condor 2 Soaring Simulator"
        )
        p1 = out_dir / "Loading01.bmp"
        save_bmp_24bit(img1, p1)
        generated.append(p1)
        print(f"  -> {p1}")

        img2 = generate_ortho_loading(
            ortho, "Loading02", anchor="upper", subtitle="Vardar Valley & Skopje"
        )
        p2 = out_dir / "Loading02.bmp"
        save_bmp_24bit(img2, p2)
        generated.append(p2)
        print(f"  -> {p2}")

        # DEM relief as third screen
        if dem is not None:
            img3 = generate_relief_loading(dem, "Loading03")
        else:
            img3 = generate_fallback_loading(3)
        p3 = out_dir / "Loading03.bmp"
        save_bmp_24bit(img3, p3)
        generated.append(p3)
        print(f"  -> {p3}")

    elif dem is not None:
        # --- All screens from DEM data (no orthophoto available) ---
        print("Orthophoto not found; generating all screens from DEM relief data")

        # Loading01: Full relief/hillshade map view with title
        print("  Generating Loading01: full relief map view ...")
        img1 = generate_relief_loading(dem, "Loading01")
        p1 = out_dir / "Loading01.bmp"
        save_bmp_24bit(img1, p1)
        generated.append(p1)
        print(f"  -> {p1}")

        # Loading02: Zoomed warm-toned view of northern region
        print("  Generating Loading02: zoomed terrain view ...")
        img2 = generate_relief_zoom_loading(dem, "Loading02")
        p2 = out_dir / "Loading02.bmp"
        save_bmp_24bit(img2, p2)
        generated.append(p2)
        print(f"  -> {p2}")

        # Loading03: Dramatic mountain perspective with deep shadows
        print("  Generating Loading03: dramatic mountain perspective ...")
        img3 = generate_dramatic_mountain_loading(dem, "Loading03")
        p3 = out_dir / "Loading03.bmp"
        save_bmp_24bit(img3, p3)
        generated.append(p3)
        print(f"  -> {p3}")

    else:
        # --- No DEM, no ortho: plain gradient fallback ---
        print("No source data available; generating gradient fallback screens")
        for idx in range(1, 4):
            img = generate_fallback_loading(idx)
            p = out_dir / f"Loading{idx:02d}.bmp"
            save_bmp_24bit(img, p)
            generated.append(p)
            print(f"  -> {p}")

    # --- Copy to Condor 2 landscape folder ---
    condor_dir = args.condor_dir
    if not args.no_copy and condor_dir:
        if condor_dir.exists():
            print(f"Copying to Condor landscape folder: {condor_dir}")
            for src in generated:
                dst = condor_dir / src.name
                shutil.copy2(src, dst)
                print(f"  copied {src.name}")
        else:
            print(f"Condor landscape folder not found: {condor_dir} (skipped copy)")

    print("\nGenerated loading screens:")
    for p in generated:
        print(f"  {p}  ({p.stat().st_size / (1024 * 1024):.2f} MB)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
