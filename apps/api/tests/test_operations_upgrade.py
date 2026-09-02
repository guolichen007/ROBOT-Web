from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from math import pi
from pathlib import Path
from types import ModuleType

import pytest
from app.core.config import Settings
from app.core.errors import PlatformError
from app.db.models import (
    ParkingSlot,
    Robot,
    RobotCapability,
    RobotIntegrationProfile,
    RobotMotionProfile,
    RobotSensorProfile,
)
from app.modules.commands.readiness import robot_readiness
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
                    control_contract_verified=True,
                    ack_contract_verified=True,
                    map_contract_verified=True,
                    bidirectional_bridge_verified=False,
                    command_path_verified=True,
                    cmd_vel_arbitration_verified=True,
                    ros_control_mode=3,
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
    for action in (
        "manual_control",
        "stop_motion",
        "emergency_stop",
        "patrol",
        "return_dock",
        "extinguish",
    ):
        with pytest.raises(PlatformError) as raised:
            assert_robot_can_execute(FakeDb(), robot, action)  # type: ignore[arg-type]
        assert raised.value.code == "ROS_COMPAT_READ_ONLY"


def test_readiness_is_per_operation_and_safety_does_not_require_map() -> None:
    robot = Robot(
        id="robot-id",
        vehicle_id="R001",
        site_id="site",
        name="R001",
        enabled=True,
        online_state="ONLINE",
        estop_active=False,
    )
    integration = RobotIntegrationProfile(
        robot_id=robot.id,
        source_kind="CANONICAL_MQTT",
        control_contract_verified=True,
        ack_contract_verified=True,
        map_contract_verified=False,
    )
    capability = RobotCapability(
        robot_id=robot.id,
        protocol_version="1.2.0",
        supported_commands_json=["stop_motion", "emergency_stop", "patrol"],
        sensors_json=[],
        media_json=[],
    )

    class FakeDb:
        def get(self, model, _key):
            return {
                RobotIntegrationProfile: integration,
                RobotCapability: capability,
                RobotMotionProfile: None,
            }.get(model)

    readiness = robot_readiness(FakeDb(), robot)  # type: ignore[arg-type]
    assert readiness["safety_command_ready"]["stop_motion"] is True
    assert readiness["safety_command_ready"]["emergency_stop"] is True
    assert readiness["autonomous_task_ready"]["patrol"] is False
    assert readiness["control_enabled"] is False


def test_ros1_actual_payload_mapping_preserves_mecanum_and_unknown_safety(monkeypatch) -> None:
    monkeypatch.setenv("ROS_COMPAT_MODE", "true")
    module = load_service("ros1_actual_mapping", "services/ros-compat-adapter/main.py")
    adapter = module.Adapter()
    monkeypatch.setattr(
        adapter, "map_facts", lambda: ("DEMO_PARKING", "parking_v1", "1", "checksum")
    )
    pose = adapter.normalize(
        "pose",
        {
            "ts": 1_700_000_000,
            "frame_id": "map",
            "x": 1.2,
            "y": 2.3,
            "yaw": 0.4,
            "cov_xx": 0.01,
            "cov_yy": 0.02,
            "cov_yawyaw": 0.03,
        },
    )[0][1]
    odom = adapter.normalize("odom", {"ts": 1_700_000_001, "vx": 0, "vy": 0.25, "wz": 0})[0][1]
    assert pose["position"] == {"x": 1.2, "y": 2.3, "theta": 0.4}
    assert pose["localization_status"] == "VALID_SOURCE"
    assert adapter.latest["amcl_covariance"] == {
        "cov_xx": 0.01,
        "cov_yy": 0.02,
        "cov_yawyaw": 0.03,
    }
    assert odom["linear_x"] == 0
    assert odom["linear_y"] == 0.25
    assert odom["planar_speed"] == pytest.approx(0.25)
    assert adapter.latest["x"] == 1.2  # odom pose never replaces AMCL global pose
    assert "estop_active" not in pose


def test_ros1_status_battery_and_nav_remain_truthful_diagnostics(monkeypatch) -> None:
    monkeypatch.setenv("ROS_COMPAT_MODE", "true")
    module = load_service("ros1_status_mapping", "services/ros-compat-adapter/main.py")
    adapter = module.Adapter()
    manual = adapter.normalize("status", {"ts": 1_700_000_000, "control_mode": 1})[0][1]
    ros = adapter.normalize("status", {"ts": 1_700_000_001, "control_mode": 3})[0][1]
    battery = adapter.normalize(
        "battery",
        {
            "ts": 1_700_000_002,
            "battery_percentage": 105,
            "battery_voltage": 51.2,
            "battery_temperature": 31.5,
        },
    )[0][1]
    nav_kind, nav = adapter.normalize(
        "nav_result", {"ts": 1_700_000_003, "goal_id": "native-1", "status": "SUCCEEDED"}
    )[0]
    assert manual["mode"] == "MANUAL" and manual["ros_control_mode"] == 1
    assert ros["mode"] == "IDLE" and ros["ros_control_mode"] == 3
    assert battery["battery"] == 100
    assert battery["diagnostics"]["battery_voltage"] == 51.2
    assert nav_kind == "compat_nav_result"
    assert nav["external_goal_id"] == "native-1"
    assert "task_id" not in nav and "command_id" not in nav


def test_mecanum_lateral_motion_cannot_be_confirmed_stationary() -> None:
    worker = load_service("task_worker_mecanum", "services/task-worker/main.py")
    now = datetime.now(UTC)
    fresh, stationary = worker.stationary_observation(
        {
            "server_received_at": now.isoformat(),
            "source_timestamp": now.isoformat(),
            "linear_x": 0,
            "linear_y": 0.25,
            "angular_z": 0,
        },
        now,
        freshness_ms=1000,
        linear_threshold=0.02,
        angular_threshold=0.03,
    )
    assert fresh is True
    assert stationary is False


def test_stationary_observation_token_and_frame_dedup() -> None:
    worker = load_service("task_worker_dedup", "services/task-worker/main.py")
    base = datetime.now(UTC)

    # observation_token：优先 server_received_at，回退 source_timestamp，缺失/解析失败 → None
    assert worker.observation_token({}) is None
    assert worker.observation_token({"server_received_at": base.isoformat()}) == base
    assert worker.observation_token({"source_timestamp": base.isoformat()}) == base
    assert worker.observation_token({"server_received_at": "not-a-date"}) is None

    # 同一观测去重：token 不大于 last_token → 帧不变
    frames, last = worker.advance_stationary_frames(True, True, base, base, 1)
    assert (frames, last) == (1, base)

    # 新静止观测：帧 +1
    t2 = base + timedelta(seconds=1)
    frames, last = worker.advance_stationary_frames(True, True, t2, last, frames)
    assert (frames, last) == (2, t2)

    # 新非零速度观测：帧清零
    t3 = base + timedelta(seconds=2)
    frames, last = worker.advance_stationary_frames(True, False, t3, last, frames)
    assert (frames, last) == (0, t3)

    # stale：帧清零且 last_token 保持不变
    frames, last = worker.advance_stationary_frames(False, False, base + timedelta(seconds=3), last, 4)
    assert (frames, last) == (0, t3)


def test_submitted_ros1_interface_baseline_matches_platform_adapter() -> None:
    baseline = json.loads((ROOT / "integration/ros1/ROS1实车接口基线.json").read_text("utf-8"))
    assert baseline["platform_contract"] == "1.2.0"
    assert baseline["schema_version"] == "1.2"
    assert baseline["vehicle"]["chassis"] == "MECANUM"
    assert baseline["interfaces"]["global_pose"]["topic"] == "/amcl_pose"
    assert baseline["interfaces"]["odom"]["fields"] == ["vx", "vy", "wz"]
    assert baseline["interfaces"]["motion"]["cmd_vel_mux"] == "NONE"
    assert baseline["interfaces"]["motion"]["chassis_watchdog_ms"] == 3000
    assert baseline["gates"]["read_only"] is True
    assert baseline["gates"]["ready_for_motion_test"] is False


def test_ros1_handoff_examples_are_accepted_without_protocol_upgrade(monkeypatch) -> None:
    monkeypatch.setenv("ROS_COMPAT_MODE", "true")
    module = load_service("ros1_handoff_examples", "services/ros-compat-adapter/main.py")
    adapter = module.Adapter()
    monkeypatch.setattr(adapter, "map_facts", lambda: ("DEMO", "mymap", "1", "checksum"))
    handoff = json.loads((ROOT / "integration/ros1/ROS1上行MQTT接口示例.json").read_text("utf-8"))
    for example in handoff["examples"]:
        parts = example["topic"].split("/")
        suffix = f"nav_{parts[-1]}" if len(parts) == 4 else parts[-1]
        source = example["payload"]["ts"]
        received = (
            datetime.fromtimestamp(source, UTC)
            if isinstance(source, int | float)
            else datetime.fromisoformat(source.replace("Z", "+00:00")).astimezone(UTC)
        )
        normalized = adapter.normalize(
            suffix,
            example["payload"],
            external_id="firerobot-01",
            received=received,
        )
        assert normalized, example["topic"]
        for kind, payload in normalized:
            if not kind.startswith("compat_"):
                assert payload["schema_version"] == "1.2"


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


def compat_payload(adapter, *, seq: int, ts: datetime, **values):
    return {
        "compat_schema_version": "1.1",
        "external_id": "firerobot-01",
        "bridge_boot_id": adapter.boot_id,
        "seq": seq,
        "ts": ts.isoformat(),
        **values,
    }


def test_ros_compat_rejects_missing_stale_future_and_out_of_order_envelopes() -> None:
    module = load_service("ros_compat_envelope", "services/ros-compat-adapter/main.py")
    adapter = module.Adapter()
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="compat_schema_version"):
        adapter.normalize("odom", {"vx": 0, "wz": 0}, external_id="firerobot-01", received=now)
    stale = compat_payload(
        adapter,
        seq=1,
        ts=now.replace(year=now.year - 1),
        vx=0,
        wz=0,
    )
    with pytest.raises(ValueError, match="stale"):
        adapter.normalize("odom", stale, external_id="firerobot-01", received=now)
    future = compat_payload(
        adapter,
        seq=1,
        ts=datetime.fromtimestamp(now.timestamp() + 10, UTC),
        vx=0,
        wz=0,
    )
    with pytest.raises(ValueError, match="future"):
        adapter.normalize("odom", future, external_id="firerobot-01", received=now)
    accepted = compat_payload(adapter, seq=1, ts=now, vx=0, wz=0)
    adapter.normalize("odom", accepted, external_id="firerobot-01", received=now)
    with pytest.raises(ValueError, match="out-of-order"):
        adapter.normalize("odom", accepted, external_id="firerobot-01", received=now)


def test_ros_compat_boot_change_allows_sequence_reset_and_requires_vehicle_map() -> None:
    module = load_service("ros_compat_boot_map", "services/ros-compat-adapter/main.py")
    adapter = module.Adapter()
    now = datetime.now(UTC)
    first = compat_payload(adapter, seq=8, ts=now, vx=0, wz=0)
    adapter.normalize("odom", first, external_id="firerobot-01", received=now)
    adapter.boot_id = "00000000-0000-4000-8000-000000000099"
    reset = compat_payload(adapter, seq=0, ts=now, vx=0, wz=0)
    adapter.normalize("odom", reset, external_id="firerobot-01", received=now)
    pose = compat_payload(adapter, seq=1, ts=now, frame_id="map", x=1, y=2, yaw=0)
    location = adapter.normalize("pose", pose, external_id="firerobot-01", received=now)[0][1]
    assert location["map_code"] == "UNVERIFIED"
    assert location["localization_status"] == "DEGRADED_MAP_UNVERIFIED"
    reported = compat_payload(
        adapter,
        seq=1,
        ts=now,
        site_code="SITE-01",
        map_code="parking_v1",
        map_version="1",
        map_checksum="abc123",
    )
    adapter.normalize("map", reported, external_id="firerobot-01", received=now)
    pose["seq"] = 2
    location = adapter.normalize("pose", pose, external_id="firerobot-01", received=now)[0][1]
    assert location["map_code"] == "parking_v1"
