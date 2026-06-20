#!/usr/bin/env python3
"""
Generate Condor 2 PDA / flight-planner map (.bmp) from the DEM.

Format: Windows 32-bit BMP (BGRA), width x height pixels.
Uses a shaded-relief elevation colour map with airport markers.
"""

import struct
import numpy as np
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_RAW = PROJECT_ROOT / "sources" / "dem" / "macedonia_skopje_dem_2305_flat.raw"
AIRPORTS_JSON = PROJECT_ROOT / "data" / "airports.json"
OUT_BMP = Path("C:/Condor2/Landscapes/MacedoniaSkopje/MacedoniaSkopje.bmp")

WIDTH = 2305
HEIGHT = 2305


def elevation_colormap(dem: np.ndarray) -> np.ndarray:
    """Return BGRA colour image from elevation values."""
    z = dem.astype(np.float32)
    zmin, zmax = z.min(), z.max()
    t = (z - zmin) / (zmax - zmin + 1e-6)

    # Simple hypsometric tint (BGR order)
    # Low: dark green, Mid: light green/brown, High: grey/white
    b = np.clip(60 + 120 * t + 80 * t**2, 0, 255).astype(np.uint8)
    g = np.clip(100 + 140 * t - 60 * t**2, 0, 255).astype(np.uint8)
    r = np.clip(50 + 180 * t**1.5, 0, 255).astype(np.uint8)

    img = np.stack([b, g, r, np.full_like(b, 255)], axis=-1)
    return img


def hillshade(dem: np.ndarray, azimuth: float = 315, altitude: float = 45) -> np.ndarray:
    """Simple hillshade multiplier."""
    dx = np.gradient(dem, axis=1)
    dy = np.gradient(dem, axis=0)
    az = np.radians(azimuth)
    alt = np.radians(altitude)
    slope = np.pi / 2.0 - np.arctan(np.sqrt(dx**2 + dy**2) / 30.0)
    aspect = np.arctan2(-dx, dy)
    shade = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    return (shade * 0.5 + 0.5).clip(0.3, 1.3)


def draw_marker(img: np.ndarray, px: int, py: int, color: tuple, size: int = 6):
    """Draw a simple cross/marker on the image."""
    h, w = img.shape[:2]
    for dy in range(-size, size + 1):
        y = py + dy
        if 0 <= y < h:
            for dx in range(-size, size + 1):
                x = px + dx
                if 0 <= x < w and (abs(dx) == size or abs(dy) == size):
                    img[y, x] = color


def main():
    dem = np.fromfile(SOURCE_RAW, dtype=np.int16).reshape(HEIGHT, WIDTH)
    dem = np.where(dem < 0, 0, dem)

    img = elevation_colormap(dem)
    shade = hillshade(dem.astype(np.float32))
    img[..., :3] = (img[..., :3].astype(np.float32) * shade[..., None]).clip(0, 255).astype(np.uint8)

    # Add airport markers from data/airports.json
    import json
    import pyproj
    with open(AIRPORTS_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    transformer = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
    # Pixel size and top-left calibration for the 2305 source
    xdim = 29.9869848156182
    ydim = 29.9869848156182
    ulxmap = 506880.0
    ulymap = 4700160.0

    for ap in data.get("airports", []):
        e, n = transformer.transform(ap["lon"], ap["lat"])
        px = int(round((e - ulxmap) / xdim))
        py = int(round((ulymap - n) / ydim))
        draw_marker(img, px, py, (0, 0, 255, 255), size=6)  # Red BGRA

    # BMP rows are stored bottom-to-top
    img = np.flipud(img)

    # BMP header
    row_size = WIDTH * 4
    row_size = (row_size + 3) & ~3  # align to 4 bytes (already aligned for 32-bit)
    pixel_data_size = row_size * HEIGHT
    header_size = 54
    file_size = header_size + pixel_data_size

    header = b"BM"
    header += struct.pack("<I", file_size)
    header += struct.pack("<HH", 0, 0)  # reserved
    header += struct.pack("<I", header_size)
    header += struct.pack("<I", 40)     # DIB header size
    header += struct.pack("<ii", WIDTH, HEIGHT)
    header += struct.pack("<HH", 1, 32)  # planes, bpp
    header += struct.pack("<I", 0)      # compression (BI_RGB)
    header += struct.pack("<I", pixel_data_size)
    header += struct.pack("<i", 2835)   # X ppm
    header += struct.pack("<i", 2835)   # Y ppm
    header += struct.pack("<I", 0)      # colors used
    header += struct.pack("<I", 0)      # important colors

    OUT_BMP.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_BMP, "wb") as f:
        f.write(header)
        f.write(img.tobytes())

    print(f"Wrote {OUT_BMP}")
    print(f"  size: {OUT_BMP.stat().st_size} bytes")


if __name__ == "__main__":
    main()
