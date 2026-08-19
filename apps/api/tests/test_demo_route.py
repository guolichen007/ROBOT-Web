"""Pure geometry tests for the DEMO right-side-coverage S-cruise route.

These do not require a database; they validate the single source of truth
used by both fresh-seed and existing-dev sync.
"""

from __future__ import annotations

import math

import pytest

from app.db.models import ParkingSlot, RobotSensorProfile
from app.modules.navigation.route_builder import (
    REMOTE_WAITING,
    SlotRef,
    build_cruise_trajectory,
    inspection_pose,
    ordered_codes,
    slot_is_on_vehicle_right,
)
from app.modules.operations.router import calculate_detection_coverage


def demo_slots() -> list[SlotRef]:
    slots: list[SlotRef] = []
    index = 0
    for group_start in (2.5, 27.0):
        for col in range(9):
            index += 1
            slots.append(SlotRef(code=f"A-{index:02d}", x=group_start + col * 2.15, y=31.5))
    for x in (5.0, 17.0, 29.0, 43.0):
        for row in range(9):
            index += 1
            slots.append(SlotRef(code=f"A-{index:02d}", x=x, y=3.0 + row * 2.8))
    return slots


def test_all_54_slots_have_unique_right_side_inspection_pose() -> None:
    slots = demo_slots()
    assert len(slots) == 54
    by_code = {s.code: s for s in slots}
    poses: dict[str, dict] = {}
    for slot in slots:
        pose = inspection_pose(slot)
        poses[slot.code] = pose
        assert slot_is_on_vehicle_right(pose, {"x": slot.x, "y": slot.y}), slot.code
    assert len(poses) == 54
    assert poses["A-27"]["theta"] == pytest.approx(-math.pi / 2)
    assert poses["A-28"]["theta"] == pytest.approx(math.pi / 2)
    assert poses["A-45"]["theta"] == pytest.approx(-math.pi / 2)
    assert poses["A-46"]["theta"] == pytest.approx(math.pi / 2)
    assert poses["A-18"]["theta"] == pytest.approx(math.pi)


def test_ordered_codes_follow_the_s_cruise_sequence() -> None:
    codes = ordered_codes(demo_slots())
    assert len(codes) == 54
    assert len(set(codes)) == 54
    assert codes[:9] == [f"A-{n:02d}" for n in range(27, 18, -1)]
    assert codes[9:18] == [f"A-{n:02d}" for n in range(28, 37)]
    assert codes[18:27] == [f"A-{n:02d}" for n in range(45, 36, -1)]
    assert codes[27:36] == [f"A-{n:02d}" for n in range(46, 55)]
    assert codes[36:] == [f"A-{n:02d}" for n in range(18, 0, -1)]


def test_trajectory_starts_and_ends_at_remote_waiting() -> None:
    path = build_cruise_trajectory(demo_slots())
    assert len(path) > 20
    assert (path[0]["x"], path[0]["y"]) == (REMOTE_WAITING["x"], REMOTE_WAITING["y"])
    assert (path[-1]["x"], path[-1]["y"]) == (REMOTE_WAITING["x"], REMOTE_WAITING["y"])


def test_trajectory_segments_are_axis_aligned_lanes() -> None:
    path = build_cruise_trajectory(demo_slots())
    for a, b in zip(path, path[1:]):
        dx = abs(b["x"] - a["x"])
        dy = abs(b["y"] - a["y"])
        # No diagonal cuts: every segment is a straight horizontal/vertical lane.
        assert dx < 1e-6 or dy < 1e-6, (a, b)
        # Every lane stays within the demo map bounds (48 x 34).
        assert dx <= 48.1, (a, b)
        assert dy <= 34.1, (a, b)


def _demo_profile(mount_yaw: float = -math.pi / 2) -> RobotSensorProfile:
    return RobotSensorProfile(
        robot_id="robot-id",
        channel="right_fire_detection",
        support_state="CONNECTED",
        nominal_side="RIGHT",
        sensor_mount_x_m=0.35,
        sensor_mount_y_m=-0.32,
        sensor_mount_yaw_rad=mount_yaw,
        coverage_range_m=5.5,
        coverage_fov_rad=math.pi / 3,
    )


def _demo_slot_models() -> list[ParkingSlot]:
    slots: list[ParkingSlot] = []
    index = 0
    for group_start in (2.5, 27.0):
        for col in range(9):
            index += 1
            x, y = group_start + col * 2.15, 31.5
            width, height = 1.95, 4.0
            slots.append(_slot(index, x, y, width, height))
    for x in (5.0, 17.0, 29.0, 43.0):
        for row in range(9):
            index += 1
            y = 3.0 + row * 2.8
            width, height = 4.0, 2.45
            slots.append(_slot(index, x, y, width, height))
    return slots


def _slot(index: int, x: float, y: float, width: float, height: float) -> ParkingSlot:
    return ParkingSlot(
        id=f"slot-{index}",
        map_version_id="version-1",
        code=f"A-{index:02d}",
        polygon_json={
            "points": [
                {"x": x - width / 2, "y": y - height / 2},
                {"x": x + width / 2, "y": y - height / 2},
                {"x": x + width / 2, "y": y + height / 2},
                {"x": x - width / 2, "y": y + height / 2},
            ]
        },
        center_pose_json={"x": x, "y": y, "theta": 0},
        enabled=True,
    )


def test_detection_coverage_covers_all_54_inspection_slots() -> None:
    refs = demo_slots()
    models = _demo_slot_models()
    by_code = {m.code: m for m in models}
    profile = _demo_profile()
    for ref in refs:
        pose = inspection_pose(ref)
        result = calculate_detection_coverage(
            robot_pose={"x": pose["x"], "y": pose["y"], "theta": pose["theta"]},
            profile=profile,
            slots=models,
        )
        assert result["state"] == "CONNECTED", ref.code
        assert by_code[ref.code].id in result["covered_parking_slot_ids"], ref.code


def test_detection_coverage_rejects_mis_mounted_sensor_instead_of_mirroring() -> None:
    models = _demo_slot_models()
    bad_profile = _demo_profile(mount_yaw=math.pi / 2)
    pose = inspection_pose(demo_slots()[0])
    result = calculate_detection_coverage(
        robot_pose={"x": pose["x"], "y": pose["y"], "theta": pose["theta"]},
        profile=bad_profile,
        slots=models,
    )
    assert result["state"] == "ERROR"
    assert result["reason"] == "RIGHT_SENSOR_ORIENTATION_INVALID"
    assert result["covered_parking_slot_ids"] == []
