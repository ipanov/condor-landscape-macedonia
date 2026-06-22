#!/usr/bin/env python3
"""Tests for the object placement validator.

These are deliberately synthetic so they do not depend on the user's installed
Condor or MSFS packages. They protect the failure mode from the Stenkovec work:
a centroid/axis-only check can pass while the actual mesh footprint is the wrong
object for the roof painted in the texture.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from verify_object_placement import (  # noqa: E402
    ObjRecord,
    TargetFootprint,
    compare_target_sources,
    rectangle_polygon,
    transform_local_polygon,
    validate_polygons,
)


def _assert_has_failure(report: dict, code: str) -> None:
    assert any(f["code"] == code for f in report["failures"]), report


def test_centroid_and_axis_are_not_enough_when_size_is_wrong():
    target = rectangle_polygon(
        centroid=(1000.0, 2000.0),
        long_m=34.3,
        short_m=19.2,
        bearing_deg=9.0,
    )
    wrong_mesh_same_center_and_axis = rectangle_polygon(
        centroid=(1000.0, 2000.0),
        long_m=40.3,
        short_m=35.2,
        bearing_deg=9.0,
    )

    report = validate_polygons(wrong_mesh_same_center_and_axis, target)

    assert report["position_error_m"] < 0.001
    assert report["angle_error_deg"] < 0.001
    assert report["ok"] is False
    _assert_has_failure(report, "long_size_error")
    _assert_has_failure(report, "short_size_error")
    _assert_has_failure(report, "iou_below_threshold")


def test_record_origin_is_not_assumed_to_be_mesh_centroid():
    local = Polygon([(8.0, -7.0), (12.0, -7.0), (12.0, -3.0), (8.0, -3.0)])
    record = ObjRecord(
        name="OffsetMesh.c3d",
        pos_x=0.0,
        pos_y=0.0,
        pos_z=0.0,
        scale=1.0,
        ori_rad=0.0,
        world_e=100.0,
        world_n=200.0,
        offset=0,
    )

    placed = transform_local_polygon(local, record)

    assert math.isclose(placed.centroid.x, 110.0, abs_tol=1e-9)
    assert math.isclose(placed.centroid.y, 195.0, abs_tol=1e-9)


def test_conflicting_target_sources_block_validation():
    texture = TargetFootprint(
        source="texture",
        polygon=rectangle_polygon((1000.0, 2000.0), 34.0, 19.0, 9.0),
    )
    vector_shifted_and_rotated = TargetFootprint(
        source="microsoft",
        polygon=rectangle_polygon((1005.0, 2000.0), 34.0, 19.0, 15.5),
    )

    conflicts = compare_target_sources([texture, vector_shifted_and_rotated])

    assert len(conflicts) == 2
    assert any(c["code"] == "source_position_conflict" for c in conflicts)
    assert any(c["code"] == "source_angle_conflict" for c in conflicts)


def main() -> int:
    tests = [
        test_centroid_and_axis_are_not_enough_when_size_is_wrong,
        test_record_origin_is_not_assumed_to_be_mesh_centroid,
        test_conflicting_target_sources_block_validation,
    ]
    failures = 0
    print("object placement validation tests")
    for test in tests:
        try:
            test()
            print(f"  [PASS] {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  [FAIL] {test.__name__}: {exc}")
    print(f"RESULTS: {len(tests) - failures} PASS / {failures} FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
