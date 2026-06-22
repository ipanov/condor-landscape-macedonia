#!/usr/bin/env python3
"""Validate Condor object placement against real-world texture/vector footprints.

The validator reads a Condor ``.obj`` placement record and the referenced ``.c3d``
mesh, projects the actual mesh footprint into UTM, then checks it against one or
more target footprints. It intentionally rejects centroid-only matches: position,
axis, dimensions, and full footprint overlap all have to pass.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from shapely.geometry import MultiPoint, Polygon, shape
from shapely.ops import transform as shp_transform

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import c3d as C3  # noqa: E402
import condor_grid as G  # noqa: E402

REC_SIZE = 152
DEFAULT_MAX_POSITION_ERROR_M = 3.0
DEFAULT_MAX_ANGLE_ERROR_DEG = 3.0
DEFAULT_MAX_SIZE_ERROR_M = 3.0
DEFAULT_MIN_IOU = 0.85


@dataclass(frozen=True)
class ObjRecord:
    name: str
    pos_x: float
    pos_y: float
    pos_z: float
    scale: float
    ori_rad: float
    world_e: float
    world_n: float
    offset: int


@dataclass(frozen=True)
class TargetFootprint:
    source: str
    polygon: Polygon


def angle_diff_deg(a: float, b: float, period: float = 360.0) -> float:
    """Smallest absolute angular difference in degrees."""
    return abs((a - b + period / 2.0) % period - period / 2.0)


def _as_polygon(poly) -> Polygon:
    if poly.geom_type == "Polygon":
        out = poly
    else:
        out = poly.convex_hull
    if not out.is_valid:
        out = out.buffer(0)
    if out.is_empty or out.area <= 0:
        raise ValueError("footprint polygon is empty or has zero area")
    return out


def rectangle_polygon(
    centroid: tuple[float, float],
    long_m: float,
    short_m: float,
    bearing_deg: float,
) -> Polygon:
    """Build a UTM rectangle from a compass long-axis bearing."""
    ce, cn = float(centroid[0]), float(centroid[1])
    az = math.radians(float(bearing_deg))
    ux, uy = math.sin(az), math.cos(az)
    vx, vy = math.cos(az), -math.sin(az)
    hl, hs = float(long_m) / 2.0, float(short_m) / 2.0
    pts = [
        (ce - ux * hl - vx * hs, cn - uy * hl - vy * hs),
        (ce + ux * hl - vx * hs, cn + uy * hl - vy * hs),
        (ce + ux * hl + vx * hs, cn + uy * hl + vy * hs),
        (ce - ux * hl + vx * hs, cn - uy * hl + vy * hs),
    ]
    return Polygon(pts)


def mrr_metrics(poly: Polygon) -> dict:
    """Return long/short side lengths and undirected long-axis bearing."""
    rect = _as_polygon(poly).minimum_rotated_rectangle
    coords = list(rect.exterior.coords)[:4]
    edges: list[tuple[float, float, float]] = []
    for i in range(4):
        x0, y0 = coords[i]
        x1, y1 = coords[(i + 1) % 4]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        az = math.degrees(math.atan2(dx, dy)) % 180.0
        edges.append((length, az, i))
    long_len, long_az, _ = max(edges, key=lambda item: item[0])
    short_len = min(edge[0] for edge in edges)
    return {
        "long_m": float(long_len),
        "short_m": float(short_len),
        "bearing_mod180_deg": float(long_az),
    }


def read_obj_records(obj_path: str | Path, anchor: tuple[float, float] | None = None) -> list[ObjRecord]:
    data = Path(obj_path).read_bytes()
    if len(data) % REC_SIZE:
        raise ValueError(f"{obj_path}: size {len(data)} is not a multiple of {REC_SIZE}")
    records: list[ObjRecord] = []
    for off in range(0, len(data), REC_SIZE):
        pos_x, pos_y, pos_z, scale, ori = struct.unpack_from("<5f", data, off)
        name_len = data[off + 20]
        if name_len > 131:
            raise ValueError(f"{obj_path}: invalid object name length {name_len} at offset {off}")
        name = data[off + 21:off + 21 + name_len].decode("latin1")
        world_e, world_n = G.obj_world_xy(pos_x, pos_y, anchor=anchor)
        records.append(
            ObjRecord(
                name=name,
                pos_x=pos_x,
                pos_y=pos_y,
                pos_z=pos_z,
                scale=scale,
                ori_rad=ori,
                world_e=world_e,
                world_n=world_n,
                offset=off,
            )
        )
    return records


def select_record(records: Sequence[ObjRecord], name: str) -> ObjRecord:
    exact = [r for r in records if r.name == name]
    if exact:
        return exact[0]
    loose = [r for r in records if Path(r.name).stem.lower() == Path(name).stem.lower()]
    if loose:
        return loose[0]
    names = ", ".join(r.name for r in records)
    raise ValueError(f"object {name!r} not found in .obj records: {names}")


def c3d_footprint_polygon(c3d_path: str | Path, base_z_window_m: float = 0.75) -> Polygon:
    """Return the projected base footprint of a C3D mesh in local metres.

    The base slice is used first because it is the physical ground footprint.
    If a model has too few low vertices, the full horizontal silhouette is used
    as a conservative fallback.
    """
    parsed = C3.parse_c3d(c3d_path)
    all_vertices = [v for obj in parsed.objects for v in obj.vertices]
    if not all_vertices:
        raise ValueError(f"{c3d_path}: no vertices")
    min_z = min(v.pz for v in all_vertices)
    base_vertices = [v for v in all_vertices if v.pz <= min_z + base_z_window_m]
    chosen = base_vertices if len(base_vertices) >= 3 else all_vertices
    hull = MultiPoint([(v.px, v.py) for v in chosen]).convex_hull
    return _as_polygon(hull)


def transform_local_polygon(local_poly: Polygon, record: ObjRecord) -> Polygon:
    """Apply the Condor object transform to a local C3D footprint polygon."""
    co, so = math.cos(record.ori_rad), math.sin(record.ori_rad)
    scale = record.scale

    def _xf(x: float, y: float, z: float | None = None) -> tuple[float, float]:
        sx, sy = x * scale, y * scale
        return (
            record.world_e + co * sx + so * sy,
            record.world_n - so * sx + co * sy,
        )

    return _as_polygon(shp_transform(_xf, local_poly))


def _iou(poly_a: Polygon, poly_b: Polygon) -> float:
    if poly_a.is_empty or poly_b.is_empty:
        return 0.0
    union = poly_a.union(poly_b)
    if union.is_empty:
        return 0.0
    return float(poly_a.intersection(poly_b).area / union.area)


def validate_polygons(
    placed_poly: Polygon,
    target_poly: Polygon,
    *,
    max_position_error_m: float = DEFAULT_MAX_POSITION_ERROR_M,
    max_angle_error_deg: float = DEFAULT_MAX_ANGLE_ERROR_DEG,
    max_size_error_m: float = DEFAULT_MAX_SIZE_ERROR_M,
    min_iou: float = DEFAULT_MIN_IOU,
) -> dict:
    """Validate a placed mesh polygon against a target footprint polygon."""
    placed = _as_polygon(placed_poly)
    target = _as_polygon(target_poly)
    placed_mrr = mrr_metrics(placed)
    target_mrr = mrr_metrics(target)
    pos_err = math.hypot(placed.centroid.x - target.centroid.x, placed.centroid.y - target.centroid.y)
    angle_err = angle_diff_deg(
        placed_mrr["bearing_mod180_deg"],
        target_mrr["bearing_mod180_deg"],
        period=180.0,
    )
    long_err = abs(placed_mrr["long_m"] - target_mrr["long_m"])
    short_err = abs(placed_mrr["short_m"] - target_mrr["short_m"])
    iou = _iou(placed, target)

    failures: list[dict] = []
    if pos_err > max_position_error_m:
        failures.append({"code": "position_error", "value": pos_err, "limit": max_position_error_m})
    if angle_err > max_angle_error_deg:
        failures.append({"code": "angle_error", "value": angle_err, "limit": max_angle_error_deg})
    if long_err > max_size_error_m:
        failures.append({"code": "long_size_error", "value": long_err, "limit": max_size_error_m})
    if short_err > max_size_error_m:
        failures.append({"code": "short_size_error", "value": short_err, "limit": max_size_error_m})
    if iou < min_iou:
        failures.append({"code": "iou_below_threshold", "value": iou, "limit": min_iou})

    return {
        "ok": not failures,
        "position_error_m": float(pos_err),
        "angle_error_deg": float(angle_err),
        "long_size_error_m": float(long_err),
        "short_size_error_m": float(short_err),
        "iou": float(iou),
        "placed": placed_mrr,
        "target": target_mrr,
        "failures": failures,
    }


def compare_target_sources(
    targets: Sequence[TargetFootprint],
    *,
    max_position_error_m: float = DEFAULT_MAX_POSITION_ERROR_M,
    max_angle_error_deg: float = DEFAULT_MAX_ANGLE_ERROR_DEG,
    max_size_error_m: float = DEFAULT_MAX_SIZE_ERROR_M,
) -> list[dict]:
    """Return conflicts between independent target footprint sources."""
    conflicts: list[dict] = []
    for i, left in enumerate(targets):
        for right in targets[i + 1:]:
            lm, rm = mrr_metrics(left.polygon), mrr_metrics(right.polygon)
            pos_err = math.hypot(
                left.polygon.centroid.x - right.polygon.centroid.x,
                left.polygon.centroid.y - right.polygon.centroid.y,
            )
            angle_err = angle_diff_deg(lm["bearing_mod180_deg"], rm["bearing_mod180_deg"], period=180.0)
            long_err = abs(lm["long_m"] - rm["long_m"])
            short_err = abs(lm["short_m"] - rm["short_m"])
            pair = f"{left.source} vs {right.source}"
            if pos_err > max_position_error_m:
                conflicts.append({
                    "code": "source_position_conflict",
                    "pair": pair,
                    "value": float(pos_err),
                    "limit": max_position_error_m,
                })
            if angle_err > max_angle_error_deg:
                conflicts.append({
                    "code": "source_angle_conflict",
                    "pair": pair,
                    "value": float(angle_err),
                    "limit": max_angle_error_deg,
                })
            if long_err > max_size_error_m or short_err > max_size_error_m:
                conflicts.append({
                    "code": "source_size_conflict",
                    "pair": pair,
                    "long_error_m": float(long_err),
                    "short_error_m": float(short_err),
                    "limit": max_size_error_m,
                })
    return conflicts


def _coords_look_wgs84(poly: Polygon) -> bool:
    minx, miny, maxx, maxy = poly.bounds
    return -180.0 <= minx <= 180.0 and -180.0 <= maxx <= 180.0 and -90.0 <= miny <= 90.0 and -90.0 <= maxy <= 90.0


def _project_if_wgs84(poly: Polygon) -> Polygon:
    if not _coords_look_wgs84(poly):
        return _as_polygon(poly)
    transformer = G.pyproj.Transformer.from_crs(G.WGS84_CRS, G.UTM_CRS, always_xy=True)
    return _as_polygon(shp_transform(transformer.transform, poly))


def _target_from_mapping(data: dict, source: str) -> TargetFootprint:
    if "centroid_utm" in data:
        return TargetFootprint(
            source=source,
            polygon=rectangle_polygon(
                centroid=tuple(data["centroid_utm"]),
                long_m=float(data["long_m"]),
                short_m=float(data["short_m"]),
                bearing_deg=float(data.get("bearing_mod180", data.get("bearing_mod180_deg", data.get("bearing_deg", 0.0)))),
            ),
        )
    placement = data.get("placement") if isinstance(data.get("placement"), dict) else None
    if placement and "detected_centroid_utm34" in placement:
        return TargetFootprint(
            source=source,
            polygon=rectangle_polygon(
                centroid=tuple(placement["detected_centroid_utm34"]),
                long_m=float(placement["detected_long_m"]),
                short_m=float(placement["detected_short_m"]),
                bearing_deg=float(placement["detected_bearing_deg_mod180"]),
            ),
        )
    if data.get("type") == "Feature":
        return TargetFootprint(source=source, polygon=_project_if_wgs84(shape(data["geometry"])))
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []
        if not features:
            raise ValueError(f"{source}: FeatureCollection has no features")
        polys = [_project_if_wgs84(shape(feature["geometry"])) for feature in features]
        best = max(polys, key=lambda poly: poly.area)
        return TargetFootprint(source=source, polygon=best)
    raise ValueError(f"{source}: unsupported target footprint JSON")


def load_target_footprints(paths: Iterable[str | Path]) -> list[TargetFootprint]:
    targets: list[TargetFootprint] = []
    for path_like in paths:
        path = Path(path_like)
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for idx, item in enumerate(data):
                if isinstance(item, dict):
                    targets.append(_target_from_mapping(item, f"{path.name}[{idx}]"))
            continue
        if not isinstance(data, dict):
            raise ValueError(f"{path}: JSON root must be an object or list of objects")
        targets.append(_target_from_mapping(data, path.name))
    return targets


def validate_record_against_targets(
    record: ObjRecord,
    c3d_path: str | Path,
    targets: Sequence[TargetFootprint],
    **thresholds,
) -> dict:
    if not targets:
        raise ValueError("at least one target footprint is required")
    local_poly = c3d_footprint_polygon(c3d_path)
    placed_poly = transform_local_polygon(local_poly, record)
    conflicts = compare_target_sources(targets, **{
        key: value for key, value in thresholds.items()
        if key in {"max_position_error_m", "max_angle_error_deg", "max_size_error_m"}
    })
    report = validate_polygons(placed_poly, targets[0].polygon, **thresholds)
    if conflicts:
        report["ok"] = False
        report["failures"] = [{"code": "target_sources_conflict", "conflicts": conflicts}] + report["failures"]
    report["record"] = {
        "name": record.name,
        "world_e": record.world_e,
        "world_n": record.world_n,
        "pos_z": record.pos_z,
        "scale": record.scale,
        "ori_deg": math.degrees(record.ori_rad) % 360.0,
        "offset": record.offset,
    }
    report["target_source"] = targets[0].source
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--obj", required=True, help="Condor landscape .obj placement file")
    parser.add_argument("--objects-dir", required=True, help="Directory containing referenced .c3d files")
    parser.add_argument("--name", required=True, help="Object record/model name to validate")
    parser.add_argument("--target", action="append", required=True, help="Target footprint JSON/GeoJSON path; may repeat")
    parser.add_argument("--trn", help="Optional .trn path for deriving the object anchor")
    parser.add_argument("--max-position", type=float, default=DEFAULT_MAX_POSITION_ERROR_M)
    parser.add_argument("--max-angle", type=float, default=DEFAULT_MAX_ANGLE_ERROR_DEG)
    parser.add_argument("--max-size", type=float, default=DEFAULT_MAX_SIZE_ERROR_M)
    parser.add_argument("--min-iou", type=float, default=DEFAULT_MIN_IOU)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    anchor = G.obj_anchor_from_trn(args.trn) if args.trn else None
    records = read_obj_records(args.obj, anchor=anchor)
    record = select_record(records, args.name)
    c3d_path = Path(args.objects_dir) / record.name
    if not c3d_path.exists():
        raise FileNotFoundError(c3d_path)
    targets = load_target_footprints(args.target)
    report = validate_record_against_targets(
        record,
        c3d_path,
        targets,
        max_position_error_m=args.max_position,
        max_angle_error_deg=args.max_angle,
        max_size_error_m=args.max_size,
        min_iou=args.min_iou,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
