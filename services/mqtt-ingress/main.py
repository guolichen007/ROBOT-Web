from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import paho.mqtt.client as mqtt
from app.core.config import get_settings
from app.core.events import get_redis, queue_event, queue_redis_delete, queue_redis_set
from app.core.logging import configure_logging
from app.core.metrics import (
    clock_skew,
    command_ack_latency,
    mqtt_duplicate_total,
    mqtt_ingress_rate,
    mqtt_invalid_total,
    mqtt_out_of_order_total,
    mqtt_payload_rejected_total,
    robot_boot_rejected_total,
)
from app.core.serialization import serialize_model
from app.db.models import (
    Command,
    FireEvent,
    ManualControlSession,
    Map,
    MapVersion,
    ParkingSlot,
    Robot,
    RobotBootSession,
    RobotCapability,
    RobotConnectionLog,
    RobotDataChannel,
    RobotIntegrationProfile,
    RobotNavigationDiagnostic,
    SensorSample,
    Task,
    TaskEvent,
    TelemetrySample,
)
from app.db.session import SessionLocal
from jsonschema import ValidationError
from sqlalchemy import select

from services.protocol import validate_message

settings = get_settings()
configure_logging("mqtt-ingress")
logger = logging.getLogger("mqtt-ingress")

# `message_id` is useful for event idempotency, but retaining every 10 Hz
# location UUID for a day costs ~864k Redis keys per robot. Sequence tracking
# already rejects high-rate duplicates, so keep only a short retry window for
# telemetry and a longer business window for durable events.
DEDUP_TTL_SECONDS = {
    "location": 120,
    "sensor": 120,
    "heartbeat": 120,
    "status": 600,
    "availability": 86400,
    "capabilities": 86400,
    "alarm": 86400,
    "command_ack": 86400,
    "task_status": 86400,
}


def dedup_ttl(message_type: str) -> int:
    return DEDUP_TTL_SECONDS.get(message_type, 600)


redis = get_redis()
BOOT_ESTABLISH_TYPES = {"availability", "heartbeat", "capabilities"}
BASE64_DATA_RE = re.compile(r"^data:(?:image|video)/[^;]+;base64,", re.IGNORECASE)


def increment_mqtt_metric(field: str) -> None:
    redis.hincrby("metrics:mqtt", field, 1)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def json_depth(value, depth: int = 0) -> int:
    if isinstance(value, dict):
        return max([depth, *(json_depth(item, depth + 1) for item in value.values())])
    if isinstance(value, list):
        return max([depth, *(json_depth(item, depth + 1) for item in value)])
    return depth


def contains_video_payload(value) -> bool:
    if isinstance(value, str):
        return bool(BASE64_DATA_RE.match(value)) or (
            len(value) > 65_536 and "base64" in value[:128].lower()
        )
    if isinstance(value, dict):
        return any(contains_video_payload(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_video_payload(item) for item in value)
    return False


def rate_allowed(vehicle_id: str, message_type: str) -> bool:
    bucket = int(time.time())
    key = f"ratelimit:mqtt:{vehicle_id}:{message_type}:{bucket}"
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, 2)
    limit = (
        settings.mqtt_location_rate_limit_per_second
        if message_type == "location"
        else settings.mqtt_rate_limit_per_second
    )
    return int(count) <= limit


def robot_for(db, vehicle_id: str) -> Robot | None:
    return db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))


def accept_boot_session(db, robot: Robot, msg: dict, received: datetime) -> bool:
    boot_id = msg["boot_id"]
    session = db.scalar(
        select(RobotBootSession).where(
            RobotBootSession.robot_id == robot.id, RobotBootSession.boot_id == boot_id
        )
    )
    if robot.boot_id == boot_id:
        if session:
            session.last_seen_at = received
        else:
            db.add(
                RobotBootSession(
                    robot_id=robot.id,
                    boot_id=boot_id,
                    first_seen_at=received,
                    last_seen_at=received,
                )
            )
        return True
    if session and session.ended_at is not None:
        robot_boot_rejected_total.inc()
        increment_mqtt_metric("invalid:ended_boot_session")
        return False
    if msg["type"] not in BOOT_ESTABLISH_TYPES:
        robot_boot_rejected_total.inc()
        increment_mqtt_metric("invalid:boot_not_established")
        return False
    if robot.boot_id:
        current = db.scalar(
            select(RobotBootSession).where(
                RobotBootSession.robot_id == robot.id,
                RobotBootSession.boot_id == robot.boot_id,
            )
        )
        if current and current.ended_at is None:
            current.ended_at = received
        invalidate_old_boot_leases(db, robot, boot_id)
    if not session:
        db.add(
            RobotBootSession(
                robot_id=robot.id,
                boot_id=boot_id,
                first_seen_at=received,
                last_seen_at=received,
            )
        )
    robot.boot_id = boot_id
    return True


def invalidate_old_boot_leases(db, robot: Robot, boot_id: str) -> None:
    if robot.boot_id and robot.boot_id != boot_id:
        raw = redis.get(f"manual:lease:{robot.id}")
        if raw:
            lease = json.loads(raw)
            queue_redis_delete(db, f"manual:lease:{robot.id}")
            session = db.scalar(
                select(ManualControlSession).where(
                    ManualControlSession.lease_id == lease["lease_id"]
                )
            )
            if session and session.state == "HELD":
                session.state = "FORCE_RELEASED"
                session.ended_at = datetime.now(UTC)
                session.end_reason = "ROBOT_REBOOT"
        queue_event(
            db,
            "vehicle.rebooted",
            {"vehicle_id": robot.vehicle_id, "old_boot_id": robot.boot_id, "boot_id": boot_id},
        )


def update_online(db, robot: Robot, state: str, message: dict, reason: str | None = None) -> None:
    previous = robot.online_state
    robot.online_state = state
    robot.last_seen_at = datetime.now(UTC)
    if previous != state:
        db.add(
            RobotConnectionLog(
                robot_id=robot.id, state=state, boot_id=message["boot_id"], reason=reason
            )
        )
        queue_event(
            db,
            f"vehicle.{state.lower()}",
            {"vehicle_id": robot.vehicle_id, "state": state, "reason": reason},
        )


def handle_location(
    db,
    robot: Robot,
    msg: dict,
    source_ts: datetime,
    received: datetime,
    *,
    estop_unknown: bool = False,
    map_contract_verified: bool = True,
) -> None:
    latest_raw = redis.get(f"robot:{robot.vehicle_id}:latest")
    latest = json.loads(latest_raw) if latest_raw else {}
    if estop_unknown:
        # A ROS1 read-only source has no software e-stop topic.  Never retain a
        # stale Mock/canonical false value after the source is switched.
        latest.pop("estop_active", None)
    pose = msg["position"]
    pose_projection = (
        {
            "position": msg["position"],
            "x": pose["x"],
            "y": pose["y"],
            "theta": pose["theta"],
        }
        if map_contract_verified
        else {"diagnostic_pose": msg["position"]}
    )
    if not map_contract_verified:
        for key in ("position", "x", "y", "theta"):
            latest.pop(key, None)
    latest.update(
        {
            "vehicle_id": robot.vehicle_id,
            "robot_id": robot.id,
            "online_state": robot.online_state,
            **pose_projection,
            "linear": msg.get("linear_speed"),
            "angular": msg.get("angular_speed"),
            "linear_speed": msg.get("linear_speed"),
            "angular_speed": msg.get("angular_speed"),
            "battery": msg.get("battery", robot.battery),
            "site_code": msg["site_code"],
            "map_code": msg["map_code"],
            "map_version": msg["map_version"],
            "frame_id": msg["frame_id"],
            "parking_slot_code": msg.get("parking_slot_code"),
            "localization_status": (
                msg.get("localization_status", "UNKNOWN")
                if map_contract_verified
                else "DEGRADED_MAP_UNVERIFIED"
            ),
            "map_contract_verified": map_contract_verified,
            "boot_id": msg["boot_id"],
            "seq": msg["seq"],
            "source_timestamp": source_ts.isoformat(),
            "server_received_at": received.isoformat(),
        }
    )
    queue_redis_set(db, f"robot:{robot.vehicle_id}:latest", json.dumps(latest, ensure_ascii=False))
    if msg.get("battery") is not None:
        robot.battery = float(msg["battery"])
    robot.current_map_version = msg["map_version"]
    reported_map = db.scalar(
        select(Map).where(Map.site_id == robot.site_id, Map.code == msg["map_code"])
    )
    robot.current_map_id = reported_map.id if reported_map else None
    if redis.set(f"downsample:location:{robot.id}", "1", ex=1, nx=True):
        db.add(
            TelemetrySample(
                robot_id=robot.id,
                source_timestamp=source_ts,
                server_received_at=received,
                x=pose["x"],
                y=pose["y"],
                theta=pose["theta"],
                linear_speed=msg.get("linear_speed"),
                angular_speed=msg.get("angular_speed"),
                battery=msg.get("battery", robot.battery),
                localization_status=msg.get("localization_status", "UNKNOWN"),
                map_version=msg["map_version"],
                boot_id=msg["boot_id"],
                seq=msg["seq"],
            )
        )
    queue_event(db, "vehicle.location", latest)


def handle_status(db, robot: Robot, msg: dict) -> None:
    """Update robot snapshot from a status message.

    v1.3 allows partial status (only the fields the vehicle really has), so
    missing fields are left untouched / NULL instead of being fabricated.
    """
    if "mode" in msg:
        robot.current_mode = msg["mode"]
    if "battery" in msg:
        robot.battery = msg["battery"]
    if "estop_active" in msg:
        robot.estop_active = bool(msg["estop_active"])
    if "active_task_id" in msg:
        robot.current_task_id = msg.get("active_task_id")
    raw = redis.get(f"robot:{robot.vehicle_id}:latest")
    latest = json.loads(raw) if raw else {"vehicle_id": robot.vehicle_id, "robot_id": robot.id}
    updates: dict = {}
    if "mode" in msg:
        updates["mode"] = robot.current_mode
    if "battery" in msg:
        updates["battery"] = robot.battery
    if "estop_active" in msg:
        updates["estop_active"] = robot.estop_active
    if "active_task_id" in msg:
        updates["active_task_id"] = robot.current_task_id
    latest.update(updates)
    queue_redis_set(db, f"robot:{robot.vehicle_id}:latest", json.dumps(latest, ensure_ascii=False))
    queue_event(db, "vehicle.status", latest)


def handle_sensor(db, robot: Robot, msg: dict, source_ts: datetime, received: datetime) -> None:
    """Store a sensor sample.

    v1.3 sensor is capability-driven: ``smoke`` is required, bottom_ir/top_ir_max
    are optional and only present when the vehicle actually has that sensor.
    """
    smoke = msg.get("smoke")
    bottom_ir = msg.get("bottom_ir")
    top_ir_max = msg.get("top_ir_max")
    db.add(
        SensorSample(
            robot_id=robot.id,
            source_timestamp=source_ts,
            server_received_at=received,
            smoke=smoke,
            bottom_ir=bottom_ir,
            top_ir_max=top_ir_max,
            payload_json=msg.get("payload", {}),
            boot_id=msg["boot_id"],
            seq=msg["seq"],
        )
    )
    raw = redis.get(f"robot:{robot.vehicle_id}:latest")
    latest = json.loads(raw) if raw else {"vehicle_id": robot.vehicle_id, "robot_id": robot.id}
    updates: dict = {"server_received_at": received.isoformat()}
    if smoke is not None:
        updates["smoke"] = smoke
    if bottom_ir is not None:
        updates["bottom_ir"] = bottom_ir
    if top_ir_max is not None:
        updates["top_ir"] = top_ir_max
        updates["top_ir_max"] = top_ir_max
    latest.update(updates)
    queue_redis_set(db, f"robot:{robot.vehicle_id}:latest", json.dumps(latest, ensure_ascii=False))
    queue_event(db, "vehicle.sensor", latest)


def process_internal_compat(topic: str, payload: bytes, received: datetime) -> bool:
    expected = topic.rsplit("/", 1)[-1]
    if not topic.startswith("_platform/compat/") or expected not in {
        "compat_availability",
        "compat_map",
        "compat_status",
        "compat_battery",
        "compat_odom",
        "compat_nav_status",
        "compat_nav_result",
    }:
        return False
    try:
        msg = json.loads(payload.decode("utf-8"))
        if msg.get("internal_contract") != "ros-compat-v1":
            raise ValueError("unknown internal contract")
        vehicle_id = msg["vehicle_id"]
        source_ts = parse_time(msg["timestamp"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        mqtt_invalid_total.labels(reason="invalid_internal_compat").inc()
        return True
    if expected == "compat_odom":
        # 50 Hz fast path: Redis latest + a bounded 10 Hz replayable delta.
        # SQL integration/capability/channel facts are refreshed separately.
        raw = redis.get(f"robot:{vehicle_id}:latest")
        latest = json.loads(raw) if raw else {"vehicle_id": vehicle_id}
        latest.update(
            {
                "linear": msg.get("planar_speed"),
                "angular": msg.get("angular_z"),
                "linear_x": msg.get("linear_x"),
                "linear_y": msg.get("linear_y"),
                "angular_z": msg.get("angular_z"),
                "planar_speed": msg.get("planar_speed"),
                "linear_speed": msg.get("planar_speed"),
                "angular_speed": msg.get("angular_z"),
                "source_timestamp": source_ts.isoformat(),
                "server_received_at": received.isoformat(),
                "bridge_boot_id": msg.get("bridge_boot_id"),
                "seq": msg.get("seq"),
            }
        )
        redis.set(f"robot:{vehicle_id}:latest", json.dumps(latest, ensure_ascii=False))
        interval_ms = max(1, int(1000 / settings.ros_compat_ws_max_hz))
        if redis.set(f"ros_compat:ws_gate:{vehicle_id}", "1", px=interval_ms, nx=True):
            from app.core.events import append_event

            append_event("vehicle.odom", latest)
        if not redis.set(
            f"ros_compat:metadata_gate:{vehicle_id}:odom",
            "1",
            ex=settings.ros_compat_metadata_refresh_seconds,
            nx=True,
        ):
            return True
    with SessionLocal.begin() as db:
        robot = robot_for(db, vehicle_id)
        if not robot:
            mqtt_invalid_total.labels(reason="unknown_vehicle").inc()
            return True
        profile = db.get(RobotIntegrationProfile, robot.id)
        if not profile:
            profile = RobotIntegrationProfile(robot_id=robot.id)
            db.add(profile)
        profile.source_kind = "ROS_COMPAT"
        profile.upstream_protocol = "ros-native-compat"
        profile.control_contract_verified = False
        profile.ack_contract_verified = False
        if expected != "compat_map":
            profile.map_contract_verified = False
        profile.bidirectional_bridge_verified = False
        profile.command_path_verified = False
        profile.cmd_vel_arbitration_verified = False
        profile.read_only_reason = "ROS 原生上行已接入；command/ACK 与地图合同尚未现场验证"
        profile.stale_seconds = settings.ros_compat_stale_seconds
        profile.offline_seconds = settings.ros_compat_offline_seconds
        profile.external_id = msg.get("external_id", profile.external_id)
        profile.bridge_boot_id = msg.get("bridge_boot_id", profile.bridge_boot_id)
        profile.last_source_timestamp = source_ts
        profile.last_received_at = received
        sequence_state = dict(profile.compat_sequence_state_json or {})
        sequence_state[expected.removeprefix("compat_")] = {
            "bridge_boot_id": msg.get("bridge_boot_id"),
            "seq": msg.get("seq"),
            "source_timestamp": source_ts.isoformat(),
        }
        profile.compat_sequence_state_json = sequence_state
        raw = redis.get(f"robot:{vehicle_id}:latest")
        robot.estop_active = None
        latest = json.loads(raw) if raw else {"vehicle_id": vehicle_id, "robot_id": robot.id}
        latest.pop("estop_active", None)
        capability = db.get(RobotCapability, robot.id)
        if not capability:
            capability = RobotCapability(
                robot_id=robot.id,
                protocol_version="ros1-readonly",
                supported_commands_json=[],
                sensors_json=[],
                media_json=[],
            )
            db.add(capability)
        capability.protocol_version = "ros1-readonly"
        capability.supported_commands_json = []
        capability.sensors_json = []
        capability.media_json = []
        capability.received_at = received
        channel_name = expected.removeprefix("compat_")
        if expected == "compat_availability":
            state = str(msg["state"]).lower()
            profile.availability_state = state
            accept_boot_session(
                db,
                robot,
                {"boot_id": msg["bridge_boot_id"], "type": "availability"},
                received,
            )
            robot.online_state = "ONLINE" if state == "online" else "OFFLINE"
            robot.last_seen_at = received
            latest["online_state"] = robot.online_state
        elif expected == "compat_map":
            profile.reported_site_code = msg["site_code"]
            profile.reported_map_code = msg["map_code"]
            profile.reported_map_version = msg["map_version"]
            profile.reported_map_checksum = msg["map_checksum"]
            map_row = db.scalar(
                select(Map).where(Map.site_id == robot.site_id, Map.code == msg["map_code"])
            )
            version = (
                db.get(MapVersion, map_row.active_version_id)
                if map_row and map_row.active_version_id
                else None
            )
            profile.map_contract_verified = bool(
                version
                and version.version == msg["map_version"]
                and version.checksum == msg["map_checksum"]
            )
            latest["map_contract_verified"] = profile.map_contract_verified
        elif expected == "compat_status":
            robot.current_mode = str(msg["mode"])
            latest["mode"] = robot.current_mode
            profile.ros_control_mode = msg.get("ros_control_mode")
            latest["ros_control_mode"] = profile.ros_control_mode
        elif expected == "compat_battery":
            robot.battery = float(msg["battery"])
            latest["battery"] = robot.battery
            latest["battery_diagnostics"] = msg.get("diagnostics", {})
        elif expected == "compat_odom":
            latest["linear"] = msg.get("planar_speed")
            latest["angular"] = msg.get("angular_z")
            latest["linear_x"] = msg.get("linear_x")
            latest["linear_y"] = msg.get("linear_y")
            latest["angular_z"] = msg.get("angular_z")
            latest["planar_speed"] = msg.get("planar_speed")
            latest["linear_speed"] = msg.get("planar_speed")
            latest["angular_speed"] = msg.get("angular_z")
        else:
            db.add(
                RobotNavigationDiagnostic(
                    robot_id=robot.id,
                    external_goal_id=msg.get("external_goal_id"),
                    diagnostic_type=expected.removeprefix("compat_"),
                    status=str(msg.get("status", "UNKNOWN")),
                    payload_json=msg.get("payload", {}),
                    source_timestamp=source_ts,
                    server_received_at=received,
                )
            )
        latest["server_received_at"] = received.isoformat()
        latest["source_timestamp"] = source_ts.isoformat()
        queue_redis_set(db, f"robot:{vehicle_id}:latest", json.dumps(latest, ensure_ascii=False))
        channel = db.scalar(
            select(RobotDataChannel).where(
                RobotDataChannel.robot_id == robot.id,
                RobotDataChannel.channel == channel_name,
            )
        )
        if not channel:
            channel = RobotDataChannel(robot_id=robot.id, channel=channel_name)
            db.add(channel)
        channel.support_state = "CONNECTED"
        channel.quality = "GOOD"
        channel.source_kind = "ROS_COMPAT"
        channel.last_source_timestamp = source_ts
        channel.last_received_at = received
        if expected == "compat_battery":
            channel.metadata_json = {"diagnostics": msg.get("diagnostics", {})}
        elif expected in {"compat_nav_status", "compat_nav_result"}:
            channel.metadata_json = {
                "external_goal_id": msg.get("external_goal_id"),
                "diagnostic_only": True,
            }
        for unsupported_channel in (
            "estop",
            "roof_rgb",
            "roof_thermal",
            "bottom_ir_video",
        ):
            unsupported = db.scalar(
                select(RobotDataChannel).where(
                    RobotDataChannel.robot_id == robot.id,
                    RobotDataChannel.channel == unsupported_channel,
                )
            )
            if not unsupported:
                unsupported = RobotDataChannel(robot_id=robot.id, channel=unsupported_channel)
                db.add(unsupported)
            unsupported.support_state = "UNSUPPORTED"
            unsupported.quality = "NOT_AVAILABLE"
            unsupported.source_kind = "ROS_COMPAT"
        for unknown_channel in ("smoke", "top_ir", "bottom_ir"):
            unknown = db.scalar(
                select(RobotDataChannel).where(
                    RobotDataChannel.robot_id == robot.id,
                    RobotDataChannel.channel == unknown_channel,
                )
            )
            if not unknown:
                unknown = RobotDataChannel(robot_id=robot.id, channel=unknown_channel)
                db.add(unknown)
            unknown.support_state = "NOT_CONNECTED"
            unknown.quality = "NOT_AVAILABLE"
            unknown.source_kind = "ROS_COMPAT"
        queue_event(db, f"vehicle.{channel_name}", latest)
    return True


def handle_alarm(db, robot: Robot, msg: dict, received: datetime) -> None:
    by_message = db.scalar(
        select(FireEvent).where(FireEvent.source_message_id == msg["message_id"])
    )
    if by_message:
        return
    slot = db.scalar(select(ParkingSlot).where(ParkingSlot.code == msg.get("parking_slot_code")))
    if not slot:
        logger.warning(
            "alarm refers to unknown parking slot", extra={"vehicle_id": robot.vehicle_id}
        )
        return
    by_event = db.scalar(
        select(FireEvent).where(
            FireEvent.robot_id == robot.id, FireEvent.source_event_id == msg.get("event_id")
        )
    )
    fingerprint = hashlib.sha256(f"{robot.id}:{slot.id}:{msg['fire_type']}".encode()).hexdigest()
    if by_event:
        by_event.last_seen_at = received
        by_event.occurrence_count += 1
        by_event.confidence = msg.get("confidence")
        queue_event(db, "alarm.updated", serialize_model(by_event))
        return
    row = db.scalar(
        select(FireEvent).where(
            FireEvent.fingerprint == fingerprint,
            FireEvent.last_seen_at >= received - timedelta(minutes=5),
        )
    )
    if row:
        row.last_seen_at = received
        row.occurrence_count += 1
        row.confidence = msg.get("confidence")
        queue_event(db, "alarm.updated", serialize_model(row))
        return
    row = FireEvent(
        event_code=f"FE-{received:%Y%m%d%H%M%S}-{str(uuid4())[:6]}",
        robot_id=robot.id,
        parking_slot_id=slot.id,
        detection_method="AUTO",
        fire_type=msg["fire_type"],
        confidence=msg.get("confidence"),
        severity=msg["severity"],
        fingerprint=fingerprint,
        source_message_id=msg["message_id"],
        source_event_id=msg["event_id"],
        state="NEW",
        first_seen_at=received,
        last_seen_at=received,
        source_position_json=msg["position"],
        media_snapshot_json=msg.get("media", {}),
    )
    db.add(row)
    db.flush()
    queue_event(db, "alarm.created", serialize_model(row))


def handle_capabilities(db, robot: Robot, msg: dict, received: datetime) -> None:
    row = db.get(RobotCapability, robot.id)
    values = {
        "protocol_version": msg["protocol_version"],
        "supported_commands_json": msg["supported_commands"],
        "sensors_json": msg["sensors"],
        "media_json": msg["media"],
        "received_at": received,
    }
    if not row:
        row = RobotCapability(robot_id=robot.id, **values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    queue_event(db, "vehicle.capabilities", {"vehicle_id": robot.vehicle_id, **values})


def handle_ack(db, robot: Robot, msg: dict, received: datetime) -> None:
    command = db.scalar(
        select(Command).where(Command.command_id == msg["command_id"], Command.robot_id == robot.id)
    )
    if not command:
        logger.warning(
            "ACK for unknown command",
            extra={"vehicle_id": robot.vehicle_id, "command_id": msg["command_id"]},
        )
        return
    if command.ack_at:
        return
    command.ack_at = received
    command.ack_status = msg["status"]
    command.ack_reason = msg.get("reason_code") or msg.get("reason")
    if msg["status"] == "accepted":
        command.lifecycle_status = "ACK_ACCEPTED"
    elif msg["status"] == "unsupported":
        command.lifecycle_status = "ACK_UNSUPPORTED"
        command.terminal_at = received
    else:
        command.lifecycle_status = "ACK_REJECTED"
        command.terminal_at = received
    if command.published_at:
        command_ack_latency.observe((received - command.published_at).total_seconds())
    queue_event(db, "command.updated", serialize_model(command))


def handle_task_status(db, robot: Robot, msg: dict, received: datetime) -> None:
    task = db.get(Task, msg["task_id"])
    if not task or task.robot_id != robot.id:
        return
    internal_status = {
        "accepted": "ACCEPTED",
        "executing": "EXECUTING",
        "completed": "SUCCEEDED",
        "failed": "FAILED",
        "cancelled": "CANCELLED",
    }[msg["status"]]
    reported_completed = internal_status == "SUCCEEDED"
    verify_navigation = task.type == "NAVIGATE_TO_PRESET" and reported_completed
    if verify_navigation:
        # A vehicle-reported completion is not proof of final pose. The worker
        # requires fresh, well-localized telemetry for consecutive frames.
        task.status = "EXECUTING"
        task.phase = "VERIFYING_FINAL_POSE"
        task.progress = min(float(msg["progress"]), 99.0)
        parameters = dict(task.parameters_json or {})
        parameters["vehicle_completed_reported_at"] = received.isoformat()
        parameters["pose_verification_frames"] = 0
        task.parameters_json = parameters
    else:
        task.status = internal_status
        task.phase = msg["phase"]
        task.progress = msg["progress"]
    if internal_status == "ACCEPTED":
        task.accepted_at = received
    if internal_status == "EXECUTING" and not task.started_at:
        task.started_at = received
    if internal_status in {"SUCCEEDED", "FAILED", "CANCELLED"} and not verify_navigation:
        task.completed_at = received
    task.failure_code = msg.get("failure_code")
    task.failure_message = msg.get("failure_message")
    if msg.get("checkpoint_index") is not None:
        parameters = dict(task.parameters_json or {})
        parameters["live_checkpoint"] = {
            "index": msg.get("checkpoint_index"),
            "total": msg.get("checkpoint_total"),
            "current_slot_code": msg.get("current_slot_code"),
            "next_slot_code": msg.get("next_slot_code"),
        }
        task.parameters_json = parameters
    if msg.get("waypoint_index") is not None or msg.get("target_waypoint_index") is not None:
        parameters = dict(task.parameters_json or {})
        parameters["live_route_cursor"] = {
            "waypoint_index": msg.get("waypoint_index"),
            "waypoint_total": msg.get("waypoint_total"),
            "target_waypoint_index": msg.get("target_waypoint_index"),
            "last_completed_waypoint_index": msg.get("last_completed_waypoint_index"),
        }
        task.parameters_json = parameters

    # Interrupted missions become resumable; a completed return consumes them.
    parameters = dict(task.parameters_json or {})
    if internal_status == "CANCELLED" and task.type == "PATROL" and parameters.get("live_route_cursor"):
        parameters["resume_state"] = "AVAILABLE"
        task.parameters_json = parameters
    elif internal_status == "CANCELLED" and task.type == "RETURN_DOCK":
        # A stopped return is resumable: the next return re-plans from the
        # current pose, no route cursor required.
        parameters["resume_state"] = "AVAILABLE"
        task.parameters_json = parameters
    elif internal_status == "SUCCEEDED" and task.type == "RETURN_DOCK":
        task.parameters_json = parameters
        previous = db.scalar(
            select(Task)
            .where(
                Task.robot_id == task.robot_id,
                Task.type == "PATROL",
                Task.status == "CANCELLED",
            )
            .order_by(Task.created_at.desc())
        )
        if previous:
            prev_params = dict(previous.parameters_json or {})
            if prev_params.get("resume_state") == "AVAILABLE":
                prev_params["resume_state"] = "CONSUMED_BY_RETURN"
                previous.parameters_json = prev_params
    db.add(
        TaskEvent(
            task_id=task.id,
            status=task.status,
            phase=task.phase,
            progress=task.progress,
            payload_json=msg,
        )
    )
    # task_status advances the initiating business command only. A later
    # stop_motion/cancel_task sharing task_id has its own ACK lifecycle and
    # must never be overwritten with the task terminal status.
    command = db.scalar(
        select(Command)
        .where(
            Command.task_id == task.id,
            Command.cmd.in_({"patrol", "extinguish", "return_dock"}),
        )
        .order_by(Command.issued_at.desc())
    )
    if command:
        command.lifecycle_status = "EXECUTING" if task.status == "EXECUTING" else task.status
        if task.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            command.terminal_at = received
    if task.fire_event_id:
        fire = db.get(FireEvent, task.fire_event_id)
        if fire:
            if task.status in {"ACCEPTED", "EXECUTING"}:
                fire.state = "IN_PROGRESS" if task.status == "EXECUTING" else "DISPATCHED"
            elif task.status == "SUCCEEDED":
                fire.state = "RESOLVED"
                fire.resolved_at = received
            elif task.status in {"FAILED", "CANCELLED"}:
                fire.state = "CONFIRMED"
            queue_event(db, "alarm.updated", serialize_model(fire))
    queue_event(db, "task.updated", serialize_model(task))


def process(topic: str, payload: bytes) -> None:
    received = datetime.now(UTC)
    if len(payload) > settings.max_mqtt_payload_bytes:
        mqtt_payload_rejected_total.labels(reason="payload_too_large").inc()
        increment_mqtt_metric("invalid:payload_too_large")
        return
    if process_internal_compat(topic, payload, received):
        return
    try:
        msg = json.loads(payload.decode("utf-8"))
        if json_depth(msg) > settings.max_json_depth:
            mqtt_payload_rejected_total.labels(reason="json_too_deep").inc()
            increment_mqtt_metric("invalid:json_too_deep")
            return
        if contains_video_payload(msg):
            mqtt_payload_rejected_total.labels(reason="video_payload_forbidden").inc()
            increment_mqtt_metric("invalid:video_payload_forbidden")
            return
        validate_message(msg)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        mqtt_invalid_total.labels(reason=type(exc).__name__).inc()
        increment_mqtt_metric(f"invalid:{type(exc).__name__}")
        logger.warning("invalid MQTT payload")
        return
    expected = topic.rsplit("/", 1)[-1]
    source_kind = "ROS_COMPAT" if topic.startswith("_platform/compat/") else "CANONICAL_MQTT"
    if msg["type"] != expected:
        mqtt_invalid_total.labels(reason="topic_type_mismatch").inc()
        increment_mqtt_metric("invalid:topic_type_mismatch")
        return
    if not rate_allowed(msg["vehicle_id"], msg["type"]):
        mqtt_payload_rejected_total.labels(reason="rate_limit").inc()
        increment_mqtt_metric("invalid:rate_limit")
        return
    # availability（尤其 LWT offline）的 message_id 在 bridge 进程生命周期内固定，
    # 不能按 message_id 做长期去重（否则同一 boot 第二次相同 LWT offline 会被丢弃）。
    # 只保留 30s 的 in-flight pending 去重。
    dedup_key = None if msg["type"] == "availability" else f"dedup:message:{msg['message_id']}"
    pending_key = f"dedup:pending:{msg['message_id']}"
    if (dedup_key and redis.exists(dedup_key)) or not redis.set(pending_key, "1", ex=30, nx=True):
        mqtt_duplicate_total.inc()
        increment_mqtt_metric("duplicate")
        return
    last_key = f"seq:{msg['vehicle_id']}:{msg['boot_id']}:{expected}"
    last_seq = redis.get(last_key)
    # availability (online/offline) is not subject to the monotonic seq check:
    # the LWT offline payload is fixed before connect and cannot know the latest
    # seq; use message_id / server receive time for ordering (v1.3 constraint).
    if expected != "availability" and last_seq is not None and msg["seq"] <= int(last_seq):
        mqtt_out_of_order_total.inc()
        increment_mqtt_metric("out_of_order")
        redis.setex(dedup_key, dedup_ttl(msg["type"]), "1")
        redis.delete(pending_key)
        return
    source_ts = parse_time(msg["timestamp"])
    skew_ms = (received - source_ts).total_seconds() * 1000
    clock_skew.observe(abs(skew_ms))
    mqtt_ingress_rate.labels(type=msg["type"]).inc()
    increment_mqtt_metric(f"ingress:{msg['type']}")
    accepted = False
    try:
        with SessionLocal.begin() as db:
            robot = robot_for(db, msg["vehicle_id"])
            if not robot:
                mqtt_invalid_total.labels(reason="unknown_vehicle").inc()
                increment_mqtt_metric("invalid:unknown_vehicle")
            elif not accept_boot_session(db, robot, msg, received):
                mqtt_invalid_total.labels(reason="ended_or_unknown_boot").inc()
            else:
                accepted = True
                # The payload stays canonical schema 1.2; provenance is carried by
                # the broker namespace and recorded platform-side, not invented as
                # an extra vehicle field.
                from app.db.models import RobotDataChannel, RobotIntegrationProfile

                profile = db.get(RobotIntegrationProfile, robot.id)
                if source_kind == "ROS_COMPAT" and (
                    not profile or profile.source_kind != source_kind
                ):
                    if not profile:
                        profile = RobotIntegrationProfile(robot_id=robot.id)
                        db.add(profile)
                    profile.source_kind = "ROS_COMPAT"
                    profile.upstream_protocol = "ros-native-compat"
                    profile.control_contract_verified = False
                    profile.ack_contract_verified = False
                    profile.map_contract_verified = False
                    profile.bidirectional_bridge_verified = False
                    profile.command_path_verified = False
                    profile.cmd_vel_arbitration_verified = False
                    profile.read_only_reason = (
                        "ROS 原生上行已接入；command/ACK 与地图合同尚未现场验证"
                    )
                    profile.stale_seconds = settings.ros_compat_stale_seconds
                    profile.offline_seconds = settings.ros_compat_offline_seconds
                if source_kind == "ROS_COMPAT":
                    robot.estop_active = None
                    capability = db.get(RobotCapability, robot.id)
                    if not capability:
                        capability = RobotCapability(
                            robot_id=robot.id,
                            protocol_version="ros1-readonly",
                            supported_commands_json=[],
                            sensors_json=[],
                            media_json=[],
                        )
                        db.add(capability)
                    capability.protocol_version = "ros1-readonly"
                    capability.supported_commands_json = []
                    capability.sensors_json = []
                    capability.media_json = []
                    capability.received_at = received
                channel_name = (
                    "pose"
                    if source_kind == "ROS_COMPAT" and msg["type"] == "location"
                    else msg["type"]
                )
                channel = db.scalar(
                    select(RobotDataChannel).where(
                        RobotDataChannel.robot_id == robot.id,
                        RobotDataChannel.channel == channel_name,
                    )
                )
                if not channel:
                    channel = RobotDataChannel(robot_id=robot.id, channel=channel_name)
                    db.add(channel)
                channel.support_state = "CONNECTED"
                channel.quality = "GOOD"
                channel.source_kind = source_kind
                channel.last_source_timestamp = source_ts
                channel.last_received_at = received
                if msg["type"] == "availability" and msg["state"] == "offline":
                    update_online(db, robot, "OFFLINE", msg, msg.get("reason"))
                    queue_redis_delete(db, f"heartbeat:{robot.vehicle_id}")
                elif msg["type"] in {"heartbeat", "availability"}:
                    update_online(db, robot, "ONLINE", msg)
                    queue_redis_set(
                        db,
                        f"heartbeat:{robot.vehicle_id}",
                        received.isoformat(),
                        ttl_seconds=settings.robot_offline_seconds,
                    )
                handlers = {
                    "location": lambda: handle_location(
                        db,
                        robot,
                        msg,
                        source_ts,
                        received,
                        estop_unknown=source_kind == "ROS_COMPAT",
                        map_contract_verified=(
                            profile.map_contract_verified if source_kind == "ROS_COMPAT" else True
                        ),
                    ),
                    "status": lambda: handle_status(db, robot, msg),
                    "sensor": lambda: handle_sensor(db, robot, msg, source_ts, received),
                    "alarm": lambda: handle_alarm(db, robot, msg, received),
                    "capabilities": lambda: handle_capabilities(db, robot, msg, received),
                    "command_ack": lambda: handle_ack(db, robot, msg, received),
                    "task_status": lambda: handle_task_status(db, robot, msg, received),
                }
                if msg["type"] in handlers:
                    handlers[msg["type"]]()
    except Exception:
        redis.delete(pending_key)
        logger.exception("MQTT database transaction failed; message remains retryable")
        return
    if dedup_key:
        redis.setex(dedup_key, dedup_ttl(msg["type"]), "1")
    if accepted:
        redis.setex(last_key, 86400, msg["seq"])
    redis.delete(pending_key)


def service_heartbeat() -> None:
    while True:
        try:
            redis.setex("service:mqtt-ingress:heartbeat", 5, datetime.now(UTC).isoformat())
        except Exception:
            logger.warning("MQTT ingress heartbeat store unavailable", exc_info=True)
        time.sleep(1)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code != 0:
        logger.error("MQTT connect failed: %s", reason_code)
        return
    for topic in (
        "availability",
        "heartbeat",
        "capabilities",
        "location",
        "status",
        "sensor",
        "alarm",
        "task_status",
        "command_ack",
    ):
        client.subscribe(f"robot/+/{topic}", qos=1)
        client.subscribe(f"_platform/compat/+/{topic}", qos=1)
    for topic in (
        "compat_status",
        "compat_battery",
        "compat_odom",
        "compat_nav_status",
        "compat_nav_result",
    ):
        client.subscribe(f"_platform/compat/+/{topic}", qos=1)
    logger.info("MQTT ingress connected")


def on_message(client, userdata, message):
    process(message.topic, message.payload)


def main() -> None:
    threading.Thread(target=service_heartbeat, daemon=True).start()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id="firebot-mqtt-ingress", protocol=mqtt.MQTTv5
    )
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.effective_mqtt_password)
    settings.configure_mqtt_client(client)
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30)
            client.loop_forever(retry_first_connection=True)
        except Exception:
            logger.exception("MQTT ingress loop failed")
            time.sleep(3)


if __name__ == "__main__":
    main()
