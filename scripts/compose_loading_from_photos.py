#!/usr/bin/env python3
"""
Compose Condor 2 loading screens (Loading01/02/03.bmp, 1920x1080, 24-bit BMP)
from REAL Macedonian gliding photographs.

Source photos: .sandbox/loading_src/  (decoded from the user's Google Drive
"GliderflyingatStenkovec" album).

Conventions follow scripts/generate_loading_screens.py:
  - TARGET_SIZE = (1920, 1080)
  - 24-bit RGB BMP saved with PIL
  - Landscape title "MacedoniaSkopje" with a darkened top gradient for legibility
  - Subtle "CONDOR 2" corner brand

This script does NOT install anything into C:/Condor2. Output -> .sandbox/loading_screens/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

TARGET_SIZE: Tuple[int, int] = (1920, 1080)
ASPECT = TARGET_SIZE[0] / TARGET_SIZE[1]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / ".sandbox" / "loading_src"
OUT_DIR = PROJECT_ROOT / ".sandbox" / "loading_screens"

FONT_BOLD = Path("C:/Windows/Fonts/arialbd.ttf")
FONT_REGULAR = Path("C:/Windows/Fonts/arial.ttf")

# Selected photos (Drive file -> stored filename). See selection_notes.md.
#   focus = vertical crop anchor 0..1 (0 top, .5 center, 1 bottom) for the cover crop.
SCREENS = [
    {
        "src": "10201665600912780.jpg",   # Z3-5004 L23 Blanik, front-quarter, dramatic sky
        "out": "Loading01.bmp",
        "subtitle": "Soaring North Macedonia",
        "focus": 0.55,
    },
    {
        "src": "10201665600672774.jpg",   # aerial over Skopje / Vardar valley with lake
        "out": "Loading02.bmp",
        "subtitle": "The Vardar Valley from above",
        "focus": 0.50,
    },
    {
        "src": "10201665600872779.jpg",   # Z3-5004 full wingspan side profile, blue sky
        "out": "Loading03.bmp",
        "subtitle": "Gliding at Stenkovec",
        "focus": 0.55,
    },
]


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    except Exception:
        pass
    return ImageFont.load_default()


def cover_crop(img: Image.Image, focus: float = 0.5) -> Image.Image:
    """Scale + crop so the image fills TARGET_SIZE (16:9) with no distortion."""
    w, h = img.size
    if w / h > ASPECT:
        # too wide -> crop width
        new_w = int(round(h * ASPECT))
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        # too tall -> crop height around focus
        new_h = int(round(w / ASPECT))
        top = int(round((h - new_h) * focus))
        top = max(0, min(top, h - new_h))
        img = img.crop((0, top, w, top + new_h))
    return img.resize(TARGET_SIZE, Image.Resampling.LANCZOS)


def top_gradient(img: Image.Image, height: int, max_alpha: int) -> Image.Image:
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for y in range(min(height, img.height)):
        a = int(max_alpha * (1.0 - y / height))
        d.line([(0, y), (img.width, y)], fill=(0, 0, 0, a))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def bottom_gradient(img: Image.Image, height: int, max_alpha: int) -> Image.Image:
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for y in range(height):
        a = int(max_alpha * (y / height))
        yy = img.height - height + y
        d.line([(0, yy), (img.width, yy)], fill=(0, 0, 0, a))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def centered_text(draw, text, y, size, bold=True, color=(255, 255, 255),
                  shadow=(0, 0, 0), shadow_offset=3):
    font = get_font(size, bold=bold)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (TARGET_SIZE[0] - tw) // 2
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=color)


def corner_text(draw, text, x, y, size, bold=False, color=(255, 255, 255), shadow=(0, 0, 0)):
    font = get_font(size, bold=bold)
    draw.text((x + 2, y + 2), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=color)


def compose(spec: dict) -> Path:
    src = SRC_DIR / spec["src"]
    img = Image.open(src)
    if img.mode != "RGB":
        img = img.convert("RGB")

    img = cover_crop(img, focus=spec.get("focus", 0.5))

    # Gentle, photo-respecting enhancement (don't wreck the originals)
    img = ImageEnhance.Contrast(img).enhance(1.05)
    img = ImageEnhance.Color(img).enhance(1.06)
    img = ImageEnhance.Sharpness(img).enhance(1.08)

    img = top_gradient(img, height=300, max_alpha=150)
    img = bottom_gradient(img, height=130, max_alpha=120)

    draw = ImageDraw.Draw(img)
    centered_text(draw, "MacedoniaSkopje", y=60, size=88, bold=True)
    centered_text(draw, spec["subtitle"], y=168, size=34, bold=False, color=(235, 235, 235))

    corner_text(draw, "CONDOR 2", x=44, y=TARGET_SIZE[1] - 52, size=26, bold=True)
    corner_text(draw, "Real photo - North Macedonia", x=TARGET_SIZE[0] - 360,
                y=TARGET_SIZE[1] - 46, size=20, bold=False, color=(225, 225, 225))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / spec["out"]
    img.save(str(out), "BMP")
    return out


def main() -> int:
    made = []
    for spec in SCREENS:
        p = compose(spec)
        made.append(p)
        print(f"  -> {p}  ({p.stat().st_size / (1024*1024):.2f} MB)")
    # quick verify
    for p in made:
        im = Image.open(p)
        assert im.size == TARGET_SIZE, f"{p.name} wrong size {im.size}"
        assert im.mode == "RGB", f"{p.name} not 24-bit RGB ({im.mode})"
    print(f"\nAll {len(made)} BMPs are {TARGET_SIZE[0]}x{TARGET_SIZE[1]} 24-bit RGB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
