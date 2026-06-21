#!/usr/bin/env python3
"""Validate building-placement precision BEFORE extruding anything.

Draws the cadastre footprint outlines on the *installed* ortho texture (the exact
pixels the sim shows) for the densest central patch, cropped to the buildings, so
position + orientation can be eyeballed against the painted rooftops. The cadastre
footprints and the ortho share source + CRS (MK cadastre, EPSG:32634 on the Condor
grid), so a correct pipeline lands them sub-metre. Any global shift/rotation here is
a georeferencing bug to fix before placing 3D objects. No GUI.
"""
import json
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw
from shapely.geometry import shape

CAD = Path(".sandbox/buildings/cadastre_buildings.geojson")
TEX = Path("C:/Condor2/Landscapes/MacedoniaSkopje/Textures")
OUT = Path(".sandbox/buildings/validate_overlay.png")
ULX, SOUTH0 = 506880.0, 4631040.0        # NW easting, SW/SE northing (grid corners)
PATCH, PX, NCOL = 5760.0, 2048, 12


def rings(g):
    if g.geom_type == "Polygon":
        return [g.exterior]
    if g.geom_type == "MultiPolygon":
        return [p.exterior for p in g.geoms]
    return []


def patch_of(c):
    return int((c.x - ULX) // PATCH), int((c.y - SOUTH0) // PATCH)


def main():
    feats = json.load(open(CAD, encoding="utf-8"))["features"]
    cnt = Counter(patch_of(shape(f["geometry"]).centroid) for f in feats)
    (col, row), n = cnt.most_common(1)[0]
    CC, RR = NCOL - 1 - col, row
    dds = TEX / f"t{CC:02d}{RR:02d}.dds"
    if not dds.exists():
        dds = TEX / f"t{col:02d}{row:02d}.dds"
    print(f"densest patch col_w={col} row_s={row} -> {dds.name} "
          f"(exists={dds.exists()}, {n} buildings)")
    img = Image.open(dds).convert("RGB").resize((PX, PX))
    draw = ImageDraw.Draw(img)
    west, north = ULX + col * PATCH, SOUTH0 + (row + 1) * PATCH

    def topx(x, y):
        return ((x - west) / PATCH * PX, (north - y) / PATCH * PX)

    minx = miny = 1e9
    maxx = maxy = -1e9
    for f in feats:
        g = shape(f["geometry"])
        if patch_of(g.centroid) != (col, row):
            continue
        for ring in rings(g):
            xy = [topx(x, y) for x, y in ring.coords]
            draw.line(xy + [xy[0]], fill=(255, 40, 40), width=2)
            for px, py in xy:
                minx, maxx = min(minx, px), max(maxx, px)
                miny, maxy = min(miny, py), max(maxy, py)
    m = 100
    box = (max(0, int(minx - m)), max(0, int(miny - m)),
           min(PX, int(maxx + m)), min(PX, int(maxy + m)))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.crop(box).save(OUT)
    print(f"crop {box} -> {OUT}  ({box[2]-box[0]}x{box[3]-box[1]} px, "
          f"~{(box[2]-box[0])/PX*PATCH:.0f}x{(box[3]-box[1])/PX*PATCH:.0f} m)")


if __name__ == "__main__":
    main()
