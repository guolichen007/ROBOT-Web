from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from math import pi
from pathlib import Path
from types import ModuleType

import pytest
from app.core.config import Settings
from app.core.errors import PlatformError
from app.db.models import ParkingSlot, Robot, RobotIntegrationProfile, RobotSensorProfile
from app.modules.commands.service import assert_robot_can_execute
from app.modules.operations.router import calculate_detection_coverage, next_schedule_run

ROOT = Path(__file__).resolve().parents[3]


def load_service(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_right_sensor_uses_mount_extrinsic_and_slot_polygon() -> None:
    profile = RobotSensorProfile(
        robot_id="R001",
        channel="right_fire_detection",
        support_state="CONNECTED",
        nominal_side="RIGHT",
        sensor_mount_x_m=0.5,
        sensor_mount_y_m=-0.3,
        sensor_mount_yaw_rad=-pi / 2,
        coverage_range_m=5,
        coverage_fov_rad=pi / 3,
    )
    east = ParkingSlot(
        id="east",
        map_version_id="map",
        code="EAST",
        polygon_json={
            "points": [
                {"x": 1.5, "y": -1},
                {"x": 3.5, "y": -1},
                {"x": 3.5, "y": 1},
                {"x": 1.5, "y": 1},
            ]
        },
        center_pose_json={"x": 2.5, "y": 0, "theta": 0},
        enabled=True,
    )
    west = ParkingSlot(
        id="west",
        map_version_id="map",
        code="WEST",
        polygon_json={
            "points": [
                {"x": -3.5, "y": -1},
                {"x": -1.5, "y": -1},
                {"x": -1.5, "y": 1},
                {"x": -3.5, "y": 1},
            ]
        },
        center_pose_json={"x": -2.5, "y": 0, "theta": 0},
        enabled=True,
    )
    # Driving north: vehicle right is east.
    result = calculate_detection_coverage(
        robot_pose={"x": 0, "y": 0, "theta": pi / 2}, profile=profile, slots=[east, west]
    )
    assert result["covered_parking_slot_ids"] == ["east"]
    assert result["sensor_origin"]["x"] > 0


def test_ros_compat_ignores_canonical_and_never_fakes_unknown_sensors(monkeypatch) -> None:
    monkeypatch.setenv("ROS_COMPAT_MODE", "true")
    module = load_service("ros_compat_test", "services/ros-compat-adapter/main.py")
    adapter = module.Adapter()
    monkeypatch.setattr(
        adapter, "map_facts", lambda: ("DEMO_PARKING", "parking_v1", "1", "demo-map-v1")
    )
    adapter.latest = {}
    battery = adapter.normalize("battery", {"ts": 1_700_000_000, "percentage": 68})
    assert battery[0][0] == "compat_battery"
    pose = adapter.normalize("pose", {"ts": 1_700_000_001, "x": 5.12, "y": 3.45, "yaw": 1.57})
    location = pose[0][1]
    assert location["schema_version"] == "1.2"
    assert location["vehicle_id"] == "R001"
    assert location["battery"] == 68
    assert "smoke" not in location
    assert "bottom_ir" not in location
    assert "estop_active" not in location
    assert adapter.is_canonical({"schema_version": "1.2", "type": "status"})
    assert not adapter.is_canonical({"control_mode_str": "AUTO"})


def test_unverified_ros_compat_is_read_only() -> None:
    class FakeDb:
        def get(self, model, key):
            if model is RobotIntegrationProfile:
                return RobotIntegrationProfile(
                    robot_id=str(key),
                    source_kind="ROS_COMPAT",
                    control_contract_verified=False,
                    ack_contract_verified=False,
                    map_contract_verified=False,
                    read_only_reason="现场控制合同未验证",
                )
            return None

    robot = Robot(
        id="robot-id",
        vehicle_id="R001",
        site_id="site",
        name="R001",
        enabled=True,
        online_state="ONLINE",
        estop_active=False,
    )
    with pytest.raises(PlatformError) as raised:
        assert_robot_can_execute(FakeDb(), robot, "patrol")  # type: ignore[arg-type]
    assert raised.value.code == "CONTROL_CONTRACT_NOT_VERIFIED"


def test_patrol_schedule_uses_declared_timezone_and_next_occurrence() -> None:
    base = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    # 09:30 Asia/Shanghai is 01:30 UTC.
    assert next_schedule_run("30 9 * * *", "Asia/Shanghai", base) == datetime(
        2026, 8, 12, 1, 30, tzinfo=UTC
    )


def test_canonical_contract_remains_12() -> None:
    module = load_service("ros_compat_contract_test", "services/ros-compat-adapter/main.py")
    adapter = module.Adapter()
    payload = {"schema_version": "1.2", "type": "status", "vehicle_id": "R001"}
    assert adapter.is_canonical(payload) is True


def test_ros_compat_freshness_thresholds_are_deployment_configurable(monkeypatch) -> None:
    monkeypatch.setenv("ROS_COMPAT_STALE_SECONDS", "9")
    monkeypatch.setenv("ROS_COMPAT_OFFLINE_SECONDS", "21")
    configured = Settings(_env_file=None)
    assert configured.ros_compat_stale_seconds == 9
    assert configured.ros_compat_offline_seconds == 21
