# ruff: noqa: E501

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "integration/ros2"
SCHEMA = ROOT / "packages/protocol-schemas/firebot-message-1.2.schema.json"
DIST_NAME = "firebot-ros2-integration-1.2.0"
BOOT = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
TEXT_SUFFIXES = {".csv", ".json", ".md", ".txt", ".yaml", ".yml"}

TOPICS = {
    "availability": ("vehicle->platform", 1, True, "connect/LWT"),
    "heartbeat": ("vehicle->platform", 0, False, "1Hz"),
    "capabilities": ("vehicle->platform", 1, True, "boot/config"),
    "location": ("vehicle->platform", 0, False, "5-10Hz; recommended 10Hz"),
    "status": ("vehicle->platform", 1, False, "1Hz"),
    "sensor": ("vehicle->platform", 0, False, "1-2Hz"),
    "alarm": ("vehicle->platform", 1, False, "event"),
    "task_status": ("vehicle->platform", 1, False, "event/progress"),
    "command": ("platform->vehicle", 1, False, "event; manual QoS0"),
    "command_ack": ("vehicle->platform", 1, False, "event"),
}
COMMANDS = {
    "manual_control": (0, 500),
    "stop_motion": (1, 3000),
    "emergency_stop": (1, 5000),
    "reset_estop": (1, 5000),
    "patrol": (1, 30000),
    "extinguish": (1, 30000),
    "return_dock": (1, 30000),
    "cancel_task": (1, 10000),
}


def vehicle(message_type: str, seq: int, **extra) -> dict:
    return {
        "schema_version": "1.2",
        "message_id": f"00000000-0000-4000-8000-{seq:012d}",
        "type": message_type,
        "vehicle_id": "R001",
        "boot_id": BOOT,
        "timestamp": (NOW + timedelta(seconds=seq)).isoformat().replace("+00:00", "Z"),
        "seq": seq,
        **extra,
    }


def command(name: str, index: int, **extra) -> dict:
    qos, ttl = COMMANDS[name]
    issued = NOW + timedelta(minutes=1, seconds=index)
    payload = {
        "schema_version": "1.2",
        "message_id": f"10000000-0000-4000-8000-{index:012d}",
        "type": "command",
        "vehicle_id": "R001",
        "target_boot_id": None if name == "emergency_stop" else BOOT,
        "command_id": f"C-INTEGRATION-{index:03d}",
        "correlation_id": f"20000000-0000-4000-8000-{index:012d}",
        "task_id": f"T-INTEGRATION-{index:03d}"
        if name in {"patrol", "extinguish", "cancel_task"}
        else None,
        "lease_id": None,
        "control_session_id": None,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(milliseconds=ttl)).isoformat().replace("+00:00", "Z"),
        "ttl_ms": ttl,
        "priority": 100 if name == "emergency_stop" else 95 if name == "stop_motion" else 50,
        "source": "WEB",
        "operator_id": "OWNER_PROVIDED_PLATFORM_USER",
        "cmd": name,
        "params": {},
    }
    payload.update(extra)
    return payload


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def examples() -> dict[str, dict]:
    result = {
        "availability.json": vehicle("availability", 1, state="online", reason="CONNECTED"),
        "heartbeat.json": vehicle("heartbeat", 2, uptime_seconds=1.0),
        "capabilities.json": vehicle(
            "capabilities",
            3,
            protocol_version="1.2.0",
            supported_commands=list(COMMANDS),
            sensors=["smoke", "bottom_ir", "top_ir"],
            media=["roof_rgb", "roof_thermal", "bottom_ir"],
        ),
        "location.json": vehicle(
            "location",
            4,
            position={"x": 1.2, "y": 3.4, "theta": 0.5},
            linear_speed=0.2,
            angular_speed=0.0,
            battery=88.0,
            site_code="OWNER_TODO",
            map_code="OWNER_TODO",
            map_version="OWNER_TODO",
            map_checksum="OWNER_TODO_CHECKSUM",
            frame_id="map",
            parking_slot_code=None,
            localization_status="OK",
        ),
        "status.json": vehicle(
            "status", 5, mode="IDLE", battery=88.0, estop_active=False, active_task_id=None
        ),
        "sensor.json": vehicle("sensor", 6, smoke=0.0, bottom_ir=25.0, top_ir_max=26.0, payload={}),
        "fire_alert.json": vehicle(
            "alarm",
            7,
            event_id="FIRE-OWNER-001",
            fire_type="smoke",
            severity="HIGH",
            confidence=0.92,
            parking_slot_code=None,
            position={"x": 1.2, "y": 3.4, "theta": 0.5},
            media={},
        ),
        "task_status.json": vehicle(
            "task_status",
            8,
            task_id="T-INTEGRATION-001",
            status="executing",
            phase="OWNER_DEFINED_PHASE",
            progress=30,
            failure_code=None,
            failure_message=None,
        ),
    }
    manual = command(
        "manual_control",
        1,
        lease_id="30000000-0000-4000-8000-000000000001",
        control_session_id="40000000-0000-4000-8000-000000000001",
        seq=120,
        params={"linear_x": 0.30, "angular_z": 0.0},
    )
    result["manual_control.json"] = manual
    for index, name in enumerate(
        [
            "stop_motion",
            "emergency_stop",
            "reset_estop",
            "patrol",
            "extinguish",
            "return_dock",
            "cancel_task",
        ],
        start=2,
    ):
        result[f"{name}.json"] = command(name, index)
    for index, status in enumerate(("accepted", "rejected", "unsupported"), start=20):
        result[f"ack_{status}.json"] = vehicle(
            "command_ack",
            index,
            command_id="C-INTEGRATION-002",
            task_id=None,
            status=status,
            reason_code=None
            if status == "accepted"
            else "COMMAND_REJECTED"
            if status == "rejected"
            else "COMMAND_UNSUPPORTED",
            reason=None,
        )
    return result


def manifest() -> dict:
    return {
        "contract_version": "1.2.0",
        "schema_version": "1.2",
        "status": "FROZEN_FOR_ROS2_INTEGRATION",
        "broker": {
            "dev": "mqtt://PLATFORM_LAN_IP:1883",
            "server": "mqtts://OWNER_PRODUCTION_HOST:8883",
            "anonymous_server": False,
        },
        "identity": {
            "vehicle_id": "owner-assigned",
            "boot_id": "new UUID per vehicle process boot",
            "seq_scope": "boot_id + topic",
        },
        "coordinates": {
            "frame_id": "map",
            "x_y_unit": "m",
            "theta_unit": "rad",
            "theta_zero": "+X",
            "positive_rotation": "CCW",
        },
        "topics": [
            {
                "topic": f"robot/{{vehicle_id}}/{name}",
                "direction": direction,
                "qos": qos,
                "retain": retain,
                "nominal": nominal,
                "schema": "schemas/firebot-message-1.2.schema.json",
            }
            for name, (direction, qos, retain, nominal) in TOPICS.items()
        ],
        "commands": [
            {"name": name, "qos": qos, "retain": False, "ttl_ms": ttl, "idempotency": "command_id"}
            for name, (qos, ttl) in COMMANDS.items()
        ],
        "ack": {
            "status": ["accepted", "rejected", "unsupported"],
            "accepted_semantics": "vehicle application validation completed and execution accepted",
            "completion_source": "task_status",
        },
        "map_rules": {
            "required_location_fields": [
                "site_code",
                "map_code",
                "map_version",
                "map_checksum",
                "frame_id",
            ],
            "mismatch": "reject navigation/extinguish task",
        },
        "time_rules": {
            "timestamp": "UTC ISO-8601",
            "ttl_watchdog": "vehicle local monotonic receive time",
            "online_source": "platform receive time + heartbeat/LWT",
        },
        "safety_rules": [
            "manual TTL expiry stops locally",
            "network loss never continues manual indefinitely",
            "expired commands are rejected",
            "command_id is end-to-end idempotent",
            "old boot commands never execute",
            "software e-stop latches after acceptance",
            "reset_estop is explicit",
            "physical e-stop overrides platform",
        ],
    }


def generate_source() -> None:
    (SOURCE / "examples").mkdir(parents=True, exist_ok=True)
    (SOURCE / "schemas").mkdir(parents=True, exist_ok=True)
    (SOURCE / "test-vectors").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCHEMA, SOURCE / "schemas/firebot-message-1.2.schema.json")
    write_json(SOURCE / "ROBOT_INTEGRATION_MANIFEST.json", manifest())
    for name, payload in examples().items():
        write_json(SOURCE / "examples" / name, payload)
    expired = command("stop_motion", 90)
    expired["expires_at"] = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    write_json(SOURCE / "test-vectors/expired_command.json", expired)
    wrong_boot = command("patrol", 91)
    wrong_boot["target_boot_id"] = "99999999-9999-4999-8999-999999999999"
    write_json(SOURCE / "test-vectors/wrong_target_boot.json", wrong_boot)
    duplicate = command("extinguish", 92)
    write_json(SOURCE / "test-vectors/duplicate_command_first.json", duplicate)
    write_json(SOURCE / "test-vectors/duplicate_command_retry.json", duplicate)
    invalid = vehicle("heartbeat", 99, uptime_seconds=1)
    invalid["schema_version"] = "9.9"
    write_json(SOURCE / "test-vectors/invalid_schema.json", invalid)

    readme = """# ROS2 现场对接交付包\n\n本包冻结 `contract_version=1.2.0`、`schema_version=1.2`。它只定义 MQTT + Media Protocol，不包含任何 ROS2 节点、SLAM、Nav2、驱动、底盘、执行机构或车端 watchdog 实现。\n\n现场严格按验收清单 Gate 顺序推进；未知 ROS topic、速度、量程、地图、视频和网络值只填写参数模板，平台不会代替现场猜测。\n\n机器可读总合同见 `ROBOT_INTEGRATION_MANIFEST.json`，canonical JSON Schema 见 `schemas/`，所有命令与上报示例见 `examples/`。\n"""
    (SOURCE / "README_现场对接说明.md").write_text(readme, encoding="utf-8", newline="\n")
    contract = """# ROS2 MQTT 接口合同 1.2.0\n\n- Vehicle 消息携带 `boot_id`；平台命令只携带 `target_boot_id`。\n- 非 emergency-stop 命令没有当前 boot session 时必须拒绝。software e-stop 可以使用 `target_boot_id=null`，但 UI 必须等待 ACK。\n- `command_id` 端到端幂等；所有 command `retain=false`。\n- manual_control QoS0、TTL 500ms；stop/e-stop/业务命令 QoS1。\n- ACK 仅允许 accepted/rejected/unsupported；accepted 表示车端应用层校验通过并接受执行。\n- task_status 仅允许 accepted/executing/completed/failed/cancelled；未知 phase 必须安全透传。\n- TTL 使用车端本地 monotonic receive time，不依赖源 UTC 时钟。\n"""
    (SOURCE / "ROS2_MQTT接口合同.md").write_text(contract, encoding="utf-8", newline="\n")
    map_contract = """# MAP 坐标系合同\n\n`frame_id=map`；x/y 单位米；theta 单位弧度；theta=0 指向 +X；正方向逆时针。location 必须携带 site_code、map_code、map_version、map_checksum。数据库与 MQTT 保存世界坐标，像素转换仅由 Web MapAdapter 完成。\n"""
    (SOURCE / "MAP坐标系合同.md").write_text(map_contract, encoding="utf-8", newline="\n")
    safety = """# VEHICLE 安全责任合同\n\n云平台不承担网络失联后的最终运动安全闭环。车端必须实现 manual 500ms TTL watchdog、断网停止、过期命令拒绝、command_id 幂等、旧 boot 防重放、software e-stop 锁存与显式 reset；硬件急停始终优先。平台显示“已发送”不等于车辆已经停止。\n"""
    (SOURCE / "VEHICLE安全责任合同.md").write_text(safety, encoding="utf-8", newline="\n")
    checklist = """# ROS2 首次接入验收清单\n\n- [ ] Gate 1 Broker/TLS/identity\n- [ ] Gate 2 availability/heartbeat/capabilities\n- [ ] Gate 3 location/map/version/checksum/x/y/theta\n- [ ] Gate 4 status/sensor/time sync\n- [ ] Gate 5 command_id/ACK/idempotency\n- [ ] Gate 6 stop_motion（安全区域）\n- [ ] Gate 7 低速 manual + 500ms TTL + 断网停止\n- [ ] Gate 8 software e-stop latch/reset\n- [ ] Gate 9 patrol/Nav\n- [ ] Gate 10 fire alert\n- [ ] Gate 11 extinguish task（不启用真实机构）\n- [ ] Gate 12 最后才开放真实灭火执行机构\n\n前一 Gate 未签字通过，下一 Gate 不启用。\n"""
    (SOURCE / "ROS2_验收清单.md").write_text(checklist, encoding="utf-8", newline="\n")
    params = """vehicle:\n  vehicle_id: TODO\n  site_code: TODO\n  map_code: TODO\n  map_version: TODO\n  map_checksum: TODO\n\nmqtt:\n  host: TODO\n  port: 8883\n  tls: true\n  username: TODO\n  ca_file: TODO\n\nframes:\n  map: map\n  base: base_link\n\nmotion:\n  max_linear_x_mps: TODO\n  max_angular_z_radps: TODO\n\nros_mapping:\n  localization_source: TODO\n  battery_source: TODO\n  smoke_source: TODO\n  bottom_ir_source: TODO\n  top_ir_source: TODO\n  command_target: TODO\n  estop_target: TODO\n\nvideo:\n  roof_rgb: TODO\n  roof_thermal: TODO\n  bottom_ir: TODO\n\ntime_sync:\n  method: TODO_NTP_CHRONY_PTP\n"""
    (SOURCE / "ROS2_对接参数模板.yaml").write_text(params, encoding="utf-8", newline="\n")

    fields = [
        ("VehicleEnvelope", "schema_version", "string", "required", "1.2"),
        ("VehicleEnvelope", "message_id", "UUID", "required", "消息幂等标识"),
        ("VehicleEnvelope", "vehicle_id", "string", "required", "现场分配"),
        ("VehicleEnvelope", "boot_id", "UUID", "required", "每次车端进程启动新建"),
        ("VehicleEnvelope", "timestamp", "UTC ISO8601", "required", "源时间"),
        ("VehicleEnvelope", "seq", "integer", "required", "boot_id+topic 内单调递增"),
        ("Location", "x/y/theta", "number", "required", "m/m/rad, +X, CCW"),
        ("Location", "map_checksum", "string", "required", "发布地图 checksum"),
        ("Command", "target_boot_id", "UUID|null", "required", "仅 emergency_stop 可 null"),
        ("Command", "command_id", "string", "required", "端到端幂等"),
        ("Command", "ttl_ms", "integer", "required", "车端 monotonic watchdog"),
        ("CommandAck", "status", "enum", "required", "accepted/rejected/unsupported"),
        ("CommandAck", "reason_code", "enum|null", "required", "稳定 machine-readable code"),
        (
            "TaskStatus",
            "status",
            "enum",
            "required",
            "accepted/executing/completed/failed/cancelled",
        ),
    ]
    with (SOURCE / "ROS2_字段字典.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["消息", "字段", "类型", "必填", "说明"])
        writer.writerows(fields)


def build_dist() -> tuple[Path, Path]:
    dist = ROOT / "dist"
    target = dist / DIST_NAME
    archive = dist / f"{DIST_NAME}.zip"
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, target)
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(dist).as_posix(), (2026, 8, 11, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                payload = path.read_bytes()
                if path.suffix.lower() in TEXT_SUFFIXES:
                    payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                output.writestr(info, payload)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = dist / f"{DIST_NAME}.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii", newline="\n")
    return archive, checksum


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", action="store_true")
    args = parser.parse_args()
    generate_source()
    if args.dist:
        archive, checksum = build_dist()
        print(f"ROS2_HANDOFF_ZIP={archive}")
        print(f"ROS2_HANDOFF_SHA256={checksum.read_text(encoding='ascii').split()[0]}")
    else:
        print(f"ROS2_HANDOFF_SOURCE={SOURCE}")


if __name__ == "__main__":
    main()
