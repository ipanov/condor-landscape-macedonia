#!/usr/bin/env python3
r"""Offscreen textured renderer for a Condor ``.c3d`` -- a self-contained numpy
z-buffer triangle rasteriser (no GPU, no GUI, no extra deps beyond numpy/PIL).

Purpose: VISUALLY VERIFY a converted object (e.g. the Stenkovec hangar) so we can
confirm asymmetric features (door side, lean-to) are on the correct side and NOT
mirrored, before the model is installed. The c3d frame is X=East, Y=North, Z=up.

Two cameras:
  * ``top``     -- orthographic plan view looking straight DOWN (-Z). North is up,
                   East is right. This is the view to compare against a north-up
                   satellite crop.
  * ``oblique`` -- orthographic view from the SE and above (azimuth/elevation
                   configurable) for a 3D read of the massing.

Textures are sampled per-triangle from the object's Condor DDS (decoded via PIL,
which reads DXT1/3/5). Flat-coloured objects (no texture) use their material RGB.
Lambertian shading from a fixed sun gives depth; back-of-roof faces still show
because we do not cull (we z-buffer), so winding mistakes do not hide geometry --
they only flip which side a normal-lit face appears, which is itself diagnostic.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3d  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _load_texture(tex_name: str, search_dirs) -> np.ndarray | None:
    if not tex_name:
        return None
    base = Path(tex_name).name
    for d in search_dirs:
        p = Path(d) / base
        if p.exists():
            im = Image.open(p)
            im.load()
            return np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    return None


def _gather(cf: "c3d.C3DFile"):
    """Flatten all objects into (verts Nx8, tris list of (i0,i1,i2,obj_index))."""
    verts = []
    tris = []
    objs = []
    base = 0
    for oi, o in enumerate(cf.objects):
        objs.append(o)
        for v in o.vertices:
            verts.append(v.as_tuple())
        idx = o.indices
        for t in range(0, len(idx) - 2, 3):
            tris.append((base + idx[t], base + idx[t + 1], base + idx[t + 2], oi))
        base += len(o.vertices)
    return np.asarray(verts, dtype=np.float64), tris, objs


def _project(P, cam, W, H, pad=0.06):
    """Orthographic projection. cam='top' or ('oblique',az,el).

    Returns screen XY (float, pixels), depth (larger = nearer camera), and the
    world->screen scale (m/px) so callers can annotate."""
    X, Y, Z = P[:, 0], P[:, 1], P[:, 2]
    if cam == "top":
        # look down -Z: screen x = East, screen y = North (up). depth = Z (up = nearer)
        sx, sy, depth = X.copy(), Y.copy(), Z.copy()
        up_world = np.array([0, 0, 1.0])
    else:
        _, az, el = cam
        a = math.radians(az)
        e = math.radians(el)
        # view direction (from camera toward scene)
        vd = np.array([-math.cos(e) * math.sin(a),
                       -math.cos(e) * math.cos(a),
                       -math.sin(e)])
        right = np.array([math.cos(a), -math.sin(a), 0.0])
        up = np.cross(right, vd)
        up /= np.linalg.norm(up)
        sx = X * right[0] + Y * right[1] + Z * right[2]
        sy = X * up[0] + Y * up[1] + Z * up[2]
        depth = -(X * vd[0] + Y * vd[1] + Z * vd[2])
    # fit to frame, equal aspect, Y up
    x0, x1 = sx.min(), sx.max()
    y0, y1 = sy.min(), sy.max()
    span = max(x1 - x0, y1 - y0) * (1 + 2 * pad) or 1.0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    scale = min(W, H) / span
    px = (sx - cx) * scale + W / 2
    py = H / 2 - (sy - cy) * scale  # invert: screen row grows downward
    return px, py, depth, scale


def _sun_shade(n, lo=0.45):
    sun = np.array([0.4, -0.5, 0.85])
    sun /= np.linalg.norm(sun)
    s = n @ sun
    return lo + (1 - lo) * np.clip(np.abs(s), 0, 1)  # abs: lit regardless of facing


def render(cf, cam, W=900, H=900, search_dirs=(), cull="none"):
    """cull: 'none' (z-buffer only), or 'back' to drop CCW-back triangles (like Condor's
    front=CCW culling) -- a culled render goes full of holes if the mesh winding is wrong,
    so it is the visual proof that the winding fix worked (solid => CCW-front/outward)."""
    V, tris, objs = _gather(cf)
    P = V[:, :3]
    N = V[:, 3:6]
    UV = V[:, 6:8]
    px, py, depth, scale = _project(P, cam, W, H)

    img = np.zeros((H, W, 3), dtype=np.float32)
    img[:] = (0.16, 0.18, 0.20)  # dark slate background
    zbuf = np.full((H, W), -1e18, dtype=np.float64)

    texcache = {}
    for o in objs:
        if o.texture and o.texture not in texcache:
            texcache[o.texture] = _load_texture(o.texture, search_dirs)

    for (i0, i1, i2, oi) in tris:
        x0, y0 = px[i0], py[i0]
        x1, y1 = px[i1], py[i1]
        x2, y2 = px[i2], py[i2]
        minx = max(int(math.floor(min(x0, x1, x2))), 0)
        maxx = min(int(math.ceil(max(x0, x1, x2))), W - 1)
        miny = max(int(math.floor(min(y0, y1, y2))), 0)
        maxy = min(int(math.ceil(max(y0, y1, y2))), H - 1)
        if maxx < minx or maxy < miny:
            continue
        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(denom) < 1e-9:
            continue
        if cull == "back":
            # keep faces whose authored normal points toward the camera (front-facing);
            # a solid result is the visual proof that winding agrees with the normals.
            fn = N[i0] + N[i1] + N[i2]
            if cam == "top":
                if fn[2] <= 0:          # normal points down => back face, cull
                    continue
            else:
                _, az, el = cam
                a = math.radians(az); e = math.radians(el)
                vd = np.array([-math.cos(e) * math.sin(a),
                               -math.cos(e) * math.cos(a), -math.sin(e)])
                if fn @ (-vd) <= 0:     # normal away from camera => cull
                    continue
        o = objs[oi]
        tex = texcache.get(o.texture)
        face_n = N[i0] + N[i1] + N[i2]
        nn = np.linalg.norm(face_n)
        if nn > 1e-6:
            face_n = face_n / nn
        else:
            face_n = np.array([0, 0, 1.0])
        shade = _sun_shade(face_n)
        if tex is None:
            base = np.array(o.material[:3], dtype=np.float32)
        th, tw = (tex.shape[0], tex.shape[1]) if tex is not None else (0, 0)

        ys, xs = np.mgrid[miny:maxy + 1, minx:maxx + 1]
        xs = xs.astype(np.float64) + 0.5
        ys = ys.astype(np.float64) + 0.5
        l0 = ((y1 - y2) * (xs - x2) + (x2 - x1) * (ys - y2)) / denom
        l1 = ((y2 - y0) * (xs - x2) + (x0 - x2) * (ys - y2)) / denom
        l2 = 1.0 - l0 - l1
        inside = (l0 >= -1e-4) & (l1 >= -1e-4) & (l2 >= -1e-4)
        if not inside.any():
            continue
        dep = l0 * depth[i0] + l1 * depth[i1] + l2 * depth[i2]
        sub_z = zbuf[miny:maxy + 1, minx:maxx + 1]
        win = inside & (dep > sub_z)
        if not win.any():
            continue
        if tex is not None:
            u = l0 * UV[i0, 0] + l1 * UV[i1, 0] + l2 * UV[i2, 0]
            v = l0 * UV[i0, 1] + l1 * UV[i1, 1] + l2 * UV[i2, 1]
            tx = np.clip((u % 1.0) * (tw - 1), 0, tw - 1).astype(np.int32)
            ty = np.clip((v % 1.0) * (th - 1), 0, th - 1).astype(np.int32)
            col = tex[ty, tx]
        else:
            col = np.broadcast_to(base, win.shape + (3,)).copy()
        col = col * shade
        sub_img = img[miny:maxy + 1, minx:maxx + 1]
        sub_img[win] = col[win]
        sub_z[win] = dep[win]

    return (np.clip(img, 0, 1) * 255).astype(np.uint8)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("c3d", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--view", choices=["top", "oblique", "both"], default="both")
    ap.add_argument("--az", type=float, default=135.0, help="oblique azimuth (deg, from N, CW)")
    ap.add_argument("--el", type=float, default=35.0, help="oblique elevation (deg)")
    ap.add_argument("--size", type=int, default=900)
    ap.add_argument("--texdir", action="append", default=[],
                    help="extra directory to resolve textures from (repeatable)")
    ap.add_argument("--cull", choices=["none", "back"], default="none",
                    help="'back' renders only front-facing (outward-normal) tris")
    args = ap.parse_args(argv)

    cf = c3d.parse_c3d(args.c3d)
    search = list(args.texdir) + [args.c3d.parent, args.c3d.parent / "_work",
                                  REPO / ".sandbox/airport_objects"]
    panels = []
    if args.view in ("top", "both"):
        panels.append(("TOP (north up, east right)",
                       render(cf, "top", args.size, args.size, search, cull=args.cull)))
    if args.view in ("oblique", "both"):
        panels.append((f"OBLIQUE az{args.az:.0f} el{args.el:.0f}",
                       render(cf, ("oblique", args.az, args.el), args.size, args.size, search, cull=args.cull)))

    from PIL import ImageDraw, ImageFont
    gap = 8
    tot_w = sum(p[1].shape[1] for p in panels) + gap * (len(panels) - 1)
    h = max(p[1].shape[0] for p in panels) + 22
    canvas = Image.new("RGB", (tot_w, h), (10, 10, 12))
    x = 0
    dr = ImageDraw.Draw(canvas)
    for title, im in panels:
        pim = Image.fromarray(im)
        canvas.paste(pim, (x, 22))
        dr.text((x + 4, 4), title, fill=(230, 230, 230))
        x += im.shape[1] + gap
    args.out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.out)
    print(f"wrote {args.out}  ({canvas.size[0]}x{canvas.size[1]})")


if __name__ == "__main__":
    main()
