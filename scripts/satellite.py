#!/usr/bin/env python3
r"""Reusable, UTM-georeferenced satellite imagery for Condor object placement.

Cross-check source: Esri World Imagery (free, no API key). Equivalent ground
resolution to Google/Bing (~0.22 m/px at zoom 19 for lat ~42). We fetch XYZ PNG
tiles, composite them, and build an *exact* pixel<->UTM affine transform so any
UTM polygon (OSM/cadastre/MS footprint or a placed object footprint) can be
overlaid with sub-metre accuracy -- the independent ground truth the placement
algorithm validates against.

This is a BUILDING BLOCK of the generic placement engine, not a per-object
script: `fetch_window(E, N, half_m, zoom)` returns a georeferenced image usable
for ANY object on the map.

Refs:
  Web Mercator (EPSG:3857) tile math is exact; the only approximation is the
  3857->32634 transform, which is locally affine to <0.1 m over a <1 km window.
"""
from __future__ import annotations
import io, math, urllib.request
from pathlib import Path
import numpy as np
import pyproj
from PIL import Image

TILE = 256
_ESRI = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
         "World_Imagery/MapServer/tile/{z}/{y}/{x}")
_HDR = {"User-Agent": "Mozilla/5.0 condor-landscape/satellite-xcheck"}
_M2W = pyproj.Transformer.from_crs(32634, 3857, always_xy=True)   # UTM -> WebMerc
_W2U = pyproj.Transformer.from_crs(3857, 32634, always_xy=True)   # WebMerc -> UTM


def _lonlat(lat, lon):
    """UTM-free web-mercator tile coords for a lat/lon (standard Slippy map)."""
    n = 2.0 ** 22  # placeholder, real z handled below
    la, lo = math.radians(lat), math.radians(lon)
    return lo, la


def _tile_xy(lon, lat, z):
    n = 2 ** z
    xt = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    yt = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return xt, yt


def _tile_wm_extent(xtile, ytile, z):
    """Web-Mercator (metres) extent [xmin,ymin,xmax,ymax] of a tile."""
    n = 2 ** z
    def wm(x, y):  # tile coords -> web mercator metres
        x_m = (x / n) * 2 * math.pi * 6378137.0 - math.pi * 6378137.0
        y_m = math.pi * 6378137.0 - (y / n) * 2 * math.pi * 6378137.0
        return x_m, y_m
    xmin, ymax = wm(xtile, ytile)
    xmax, ymin = wm(xtile + 1, ytile + 1)
    return xmin, ymin, xmax, ymax


def _fetch_tile(z, x, y, retries=2, cache=None):
    if cache is not None:
        p = cache / f"esri_{z}_{x}_{y}.png"
        if p.exists():
            return Image.open(p).convert("RGB")
    url = _ESRI.format(z=z, y=y, x=x)
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=_HDR)
            data = urllib.request.urlopen(req, timeout=40).read()
            img = Image.open(io.BytesIO(data)).convert("RGB")
            if cache is not None:
                cache.mkdir(parents=True, exist_ok=True)
                img.save(p)
            return img
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"tile {z}/{y}/{x} failed: {last}")


class GeoImage:
    """A satellite window with an exact pixel<->UTM affine transform."""

    def __init__(self, img, e_min, n_min, e_max, n_max):
        self.img = img
        self.W, self.H = img.size
        # UTM extent (note: image y grows DOWN, northing grows UP)
        self.e_min, self.n_max = e_min, n_max   # top-left pixel
        self.e_max, self.n_min = e_max, n_min   # bottom-right pixel

    def to_px(self, E, N):
        u = (E - self.e_min) / (self.e_max - self.e_min)
        v = 1.0 - (N - self.n_min) / (self.n_max - self.n_min)
        return u * self.W, v * self.H

    def to_utm(self, px, py):
        E = self.e_min + (px / self.W) * (self.e_max - self.e_min)
        N = self.n_max - (py / self.H) * (self.n_max - self.n_min)
        return E, N

    @property
    def mpp(self):
        return ((self.e_max - self.e_min) / self.W
                + (self.n_max - self.n_min) / self.H) / 2.0


def fetch_window(e_utm, n_utm, half_m=150.0, zoom=19, cache=None):
    """Georeferenced Esri World Imagery window centred on UTM (E,N).

    Builds a composite of the tiles covering [E-half, E+half]x[N-half, N+half],
    then transforms the composite corners (Web Mercator -> UTM) and fits the
    affine pixel->UTM map. Returns a GeoImage.
    """
    # UTM window -> Web Mercator corners
    w_corners = [_M2W.transform(e, n) for e, n in
                 [(e_utm - half_m, n_utm - half_m), (e_utm + half_m, n_utm + half_m)]]
    xm0, ym0 = w_corners[0]
    xm1, ym1 = w_corners[1]
    xm_min, xm_max = min(xm0, xm1), max(xm0, xm1)
    ym_min, ym_max = min(ym0, ym1), max(ym0, ym1)
    # Web Mercator centre -> lat/lon for tile math
    inv = pyproj.Transformer.from_crs(3857, 4326, always_xy=True)
    lon_c, lat_c = inv.transform((xm_min + xm_max) / 2, (ym_min + ym_max) / 2)
    x0, y0 = _tile_xy(lon_c - 0.001, lat_c + 0.001, zoom)   # top-left-ish
    # Walk the tile grid to cover the mercator window
    def tile_for_lonlat(lo, la):
        xt, yt = _tile_xy(lo, la, zoom)
        ex = _tile_wm_extent(xt, yt, zoom)
        return xt, yt
    # find tile range covering the window
    lo_w, la_n = inv.transform(xm_min, ym_max)
    lo_e, la_s = inv.transform(xm_max, ym_min)
    xt0, yt0 = _tile_xy(lo_w, la_n, zoom)
    xt1, yt1 = _tile_xy(lo_e, la_s, zoom)
    if xt1 < xt0:
        xt0, xt1 = xt1, xt0
    if yt1 < yt0:
        yt0, yt1 = yt1, yt0
    cdir = Path(cache) if cache else (Path(".sandbox/sat_cache"))
    cols = list(range(xt0, xt1 + 1))
    rows = list(range(yt0, yt1 + 1))
    tiles = {}
    for x in cols:
        for y in rows:
            tiles[(x, y)] = _fetch_tile(zoom, x, y, cache=cdir)
    # composite
    W = len(cols) * TILE
    H = len(rows) * TILE
    canvas = Image.new("RGB", (W, H))
    for i, x in enumerate(cols):
        for j, y in enumerate(rows):
            canvas.paste(tiles[(x, y)], (i * TILE, j * TILE))
    # composite Web Mercator extent
    ex0 = _tile_wm_extent(xt0, yt0, zoom)        # top-left tile
    ex1 = _tile_wm_extent(xt1, yt1, zoom)        # bottom-right tile
    xm_a, ym_a = ex0[0], ex0[3]                   # xmin of TL, ymax of TL
    xm_b, ym_b = ex1[2], ex1[1]                   # xmax of BR, ymin of BR
    # composite corners -> UTM (corners), then affine pixel->UTM is exact
    utm_tl = _W2U.transform(xm_a, ym_a)
    utm_br = _W2U.transform(xm_b, ym_b)
    # For a window <1 km the 3857->32634 is affine to <0.1 m, BUT to be rigorous
    # we map all four corners and the centre to UTM and fit an affine by least
    # squares (handles the residual nonlinearity + convergence).
    pts_px = [(0, 0), (W, 0), (0, H), (W, H), (W / 2, H / 2)]
    pts_wm = [(xm_a, ym_a), (xm_b, ym_a), (xm_a, ym_b), (xm_b, ym_b),
              ((xm_a + xm_b) / 2, (ym_a + ym_b) / 2)]
    pts_utm = [_W2U.transform(x, y) for x, y in pts_wm]
    # Build a GeoImage but override mapping with the least-squares affine.
    gi = GeoImage(canvas, utm_tl[0], utm_br[1], utm_br[0], utm_tl[1])
    # least-squares affine px->UTM (E = a*px+b*py+c ; N = d*px+e*py+f)
    A = []
    Ex, Nx = [], []
    for (px, py), (E, N) in zip(pts_px, pts_utm):
        A.append([px, py, 1.0]); Ex.append(E); Nx.append(N)
    A = np.array(A); Ex = np.array(Ex); Nx = np.array(Nx)
    (a, b, c), *_ = np.linalg.lstsq(A, Ex, rcond=None)
    (d, e, f), *_ = np.linalg.lstsq(A, Nx, rcond=None)
    gi._aff = (a, b, c, d, e, f)

    def to_utm(px, py, _a=a, _b=b, _c=c, _d=d, _e=e, _f=f):
        return _a * px + _b * py + _c, _d * px + _e * py + _f
    gi.to_utm = to_utm

    def to_px(E, N):
        # invert the affine: [px py 1]^T = M^-1 [E N 1]^T
        M = np.array([[a, b, c], [d, e, f], [0, 0, 1]])
        Minv = np.linalg.inv(M)
        r = Minv @ np.array([E, N, 1.0])
        return float(r[0]), float(r[1])
    gi.to_px = to_px
    gi.zoom = zoom
    gi.tile_range = (xt0, yt0, xt1, yt1)
    return gi


def save_overlay(gi, polys_utm, labels=None, scale=2, out=None):
    """Render the GeoImage with UTM polygons overlaid. polys_utm = list of
    [(E,N),...] closed or open rings; labels parallel to polys_utm."""
    img = gi.img.resize((gi.W * scale, gi.H * scale), Image.LANCZOS)
    from PIL import ImageDraw, ImageFont
    dr = ImageDraw.Draw(img, "RGBA")
    colors = [(255, 40, 40, 255), (0, 255, 0, 255), (0, 180, 255, 255),
              (255, 255, 0, 255)]
    for i, poly in enumerate(polys_utm):
        col = colors[i % len(colors)]
        pts = [tuple(round(c) for c in (x * scale, y * scale))
               for x, y in (gi.to_px(E, N) for E, N in poly)]
        if len(pts) >= 2:
            dr.line(pts + ([pts[0]] if pts[0] != pts[-1] else []), fill=col, width=3)
            if labels:
                cx = sum(p[0] for p in pts) / len(pts)
                cy = sum(p[1] for p in pts) / len(pts)
                dr.text((cx + 4, cy + 4), labels[i], fill=col)
    if out:
        img.save(out)
    return img


if __name__ == "__main__":
    import sys
    e, n = float(sys.argv[1]), float(sys.argv[2])
    half = float(sys.argv[3]) if len(sys.argv) > 3 else 150.0
    gi = fetch_window(e, n, half_m=half, zoom=19)
    print(f"window {gi.W}x{gi.H} px  ~{gi.mpp:.3f} m/px  tiles {gi.tile_range}")
    print(f"UTM TL {gi.to_utm(0,0)}  BR {gi.to_utm(gi.W, gi.H)}")
