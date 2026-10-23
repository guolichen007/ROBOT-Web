"""v1 normalized ingress extension; reuse platform DB, cache, task and WS transactions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.events import get_redis, queue_event, queue_redis_set
from app.core.serialization import serialize_model
from app.db.models import (
    Command,
    Map,
    MapVersion,
    Robot,
    RobotDataChannel,
    SensorSample,
    Task,
    TaskEvent,
)
from app.db.session import SessionLocal
from app.modules.commands.readiness import robot_readiness
from sqlalchemy import select

from services.vehicle_agent_v1 import PROTOCOL, Ack, normalize, status_confirms

INTERNAL = "_platform/vehicle-agent-v1/"


def fresh_status_time(robot: Robot, received: datetime) -> datetime | None:
    if (
        robot.online_state != "ONLINE"
        or (robot.agent_state_json or {}).get("availability") == "offline"
    ):
        return None
    value = (robot.agent_state_json or {}).get("status_received_at")
    if not value:
        return None
    timestamp = datetime.fromisoformat(value)
    age = (received - timestamp).total_seconds()
    return timestamp if -2 <= age <= get_settings().vehicle_agent_v1_status_stale_seconds else None


def converge_commands(db, robot: Robot, received: datetime) -> None:
    state = robot.agent_state_json or {}
    status = state.get("status", {})
    status_at = fresh_status_time(robot, received)
    for command in db.scalars(
        select(Command).where(
            Command.robot_id == robot.id,
            Command.lifecycle_status == "ACK_ACCEPTED",
        )
    ).all():
        if command.payload_json.get("schema_version") != "vehicle-agent-v1":
            continue
        query = command.cmd in {"ping", "get_status", "get_telemetry"}
        if not query and (not status_at or status_at < command.issued_at):
            continue
        if status_confirms(command.cmd, status):
            moving = command.cmd in {"patrol", "resume_patrol", "homing", "navigate"}
            command.lifecycle_status = "EXECUTING" if moving else "SUCCEEDED"
            command.terminal_at = None if moving else received
            queue_event(db, "command.updated", serialize_model(command))


def converge_tasks(db, robot: Robot, received: datetime) -> None:
    status = (robot.agent_state_json or {}).get("status", {})
    status_at = fresh_status_time(robot, received)
    observed = status.get("task", {})
    for task in db.scalars(
        select(Task).where(
            Task.robot_id == robot.id,
            Task.type.like("V1_%"),
            Task.status.in_(["QUEUED", "ACCEPTED", "EXECUTING"]),
        )
    ).all():
        params = dict(task.parameters_json or {})
        expected = {"V1_PATROL": "patrol", "V1_HOMING": "homing", "V1_NAVIGATE": "spot"}.get(
            task.type
        )
        commands = db.scalars(select(Command).where(Command.task_id == task.id)).all()
        initiating = next(
            (c for c in commands if c.cmd in {"patrol", "resume_patrol", "navigate", "homing"}),
            None,
        )
        if not initiating:
            continue
        if (
            not status_at
            or status_at < initiating.issued_at
            or initiating.payload_json.get("target_boot_id") != robot.boot_id
            or not initiating.published_at
        ):
            continue
        stop_confirmed = any(
            c.cmd in {"stop_patrol", "stop_navigate"}
            and c.ack_status == "accepted"
            and status_at >= c.issued_at
            and status_confirms(c.cmd, status)
            for c in commands
        )
        if initiating.ack_status == "rejected" and not stop_confirmed:
            continue
        ack_missing = initiating.ack_status != "accepted"
        if ack_missing:
            params["ack_diagnostic"] = "ACK_MISSING"
            # Preserve transport truth on Command; only Task reflects observation.
            initiating.ack_reason = "ACK_MISSING"
            queue_event(db, "command.updated", serialize_model(initiating))
        # An ACK only accepts a goal. Observing spot/homing/patrol proves execution;
        # only a subsequent idle status after execution can complete the task.
        old_status = task.status
        if stop_confirmed:
            task.status = "CANCELLED"
            task.phase = "PAUSED" if observed.get("patrol_paused") else "STOPPED"
            task.completed_at = received
        elif observed.get("state") == expected:
            params["observed_running"] = True
            task.status = "EXECUTING"
            task.phase = expected.upper()
            task.started_at = task.started_at or received
        elif observed.get("patrol_paused") and task.type == "V1_PATROL":
            task.status = "CANCELLED"
            task.phase = "PAUSED"
            task.completed_at = received
        elif params.get("observed_running") and observed.get("state") == "idle":
            stopped = any(
                c.cmd in {"stop_navigate", "stop_patrol", "close"} and c.ack_status == "accepted"
                for c in commands
            )
            task.status = "CANCELLED" if stopped else "SUCCEEDED"
            task.phase = "STOPPED" if stopped else "COMPLETED"
            task.progress = task.progress if stopped else 100
            task.completed_at = received
        task.parameters_json = params
        if old_status != task.status:
            if task.status in {"SUCCEEDED", "CANCELLED"} and not ack_missing:
                initiating.lifecycle_status = task.status
                initiating.terminal_at = received
                queue_event(db, "command.updated", serialize_model(initiating))
            db.add(
                TaskEvent(
                    task_id=task.id,
                    status=task.status,
                    phase=task.phase,
                    progress=task.progress,
                    payload_json={"v1_status": status},
                )
            )
            queue_event(db, "task.updated", serialize_model(task))


def handle_v1_ack(db, robot: Robot, data: dict, received: datetime, handle_ack) -> None:
    ack = Ack.model_validate({k: v for k, v in data.items() if k in Ack.model_fields})
    command = db.scalar(
        select(Command)
        .where(
            Command.robot_id == robot.id,
            Command.command_id == data["command_id"],
        )
        .with_for_update()
    )
    if (
        not command
        or command.ack_at
        or command.payload_json.get("schema_version") != "vehicle-agent-v1"
    ):
        return
    payload = command.payload_json
    if payload["message_id"] != str(ack.msg_id) or payload["message_id"] != data["message_id"]:
        return
    if (
        payload["target_boot_id"] != data["target_boot_id"]
        or robot.boot_id != data["target_boot_id"]
    ):
        return
    synthetic = data.get("ack_source") == "transport_timeout"
    if synthetic:
        command.ack_payload_json = {
            **ack.model_dump(mode="json"),
            "ack_source": "transport_timeout",
        }
        command.lifecycle_status = "PUBLISHED_UNCONFIRMED"
        command.ack_reason = "ACK_MISSING"
        command.terminal_at = None
        queue_event(db, "command.updated", serialize_model(command))
        converge_tasks(db, robot, received)
        return
    command.ack_payload_json = {**ack.model_dump(mode="json"), "ack_source": "vehicle"}
    handle_ack(
        db,
        robot,
        {
            "command_id": command.command_id,
            "status": "accepted" if ack.accepted else "rejected",
            "reason_code": ack.code,
            "reason": ack.message,
        },
        received,
    )
    if command.task_id:
        task = db.get(Task, command.task_id)
        if (
            task
            and task.status in {"CANCELLED", "SUCCEEDED"}
            and command.cmd in {"patrol", "resume_patrol", "homing", "navigate"}
        ):
            command.lifecycle_status = task.status if ack.accepted else "ACK_REJECTED"
            command.terminal_at = task.completed_at
            queue_event(db, "command.updated", serialize_model(command))
        elif (
            task
            and task.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}
            and command.cmd in {"patrol", "resume_patrol", "homing", "navigate"}
        ):
            task.status = "ACCEPTED" if ack.accepted else "FAILED"
            task.accepted_at = received if ack.accepted else None
            if not ack.accepted:
                task.failure_code, task.failure_message = ack.code, ack.message
                task.completed_at = received
            queue_event(db, "task.updated", serialize_model(task))
    converge_tasks(db, robot, received)
    converge_commands(db, robot, received)


def process(name: str, payload: bytes, handle_ack) -> bool:
    if not name.startswith(INTERNAL):
        return False
    parts = name.split("/")
    if len(parts) != 4 or parts[-1] == "command":
        return True
    envelope = json.loads(payload)
    UUID(envelope["event_id"])
    vehicle, kind = parts[2], parts[3]
    if vehicle != envelope["vehicle_id"] or kind != envelope["kind"]:
        raise ValueError("internal vehicle/topic mismatch")
    if len(payload) > 262144:
        raise ValueError("internal payload too large")
    redis = get_redis()
    dedup = f"vehicle-agent:ingress:{envelope['event_id']}"
    if redis.exists(dedup):
        return True
    received = datetime.now(UTC)
    data = envelope["data"]
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle).with_for_update())
        if not robot or robot.agent_protocol != PROTOCOL:
            return True
        if kind == "ack":
            handle_v1_ack(db, robot, data, received, handle_ack)
        else:
            data = normalize(kind, data)
            state = dict(robot.agent_state_json or {})
            previous_online = robot.online_state
            if not robot.boot_id:
                robot.boot_id = str(uuid4())
            if kind in {"status", "availability"}:
                online = data.get("state") == "online" if kind == "availability" else data["online"]
                # Explicit offline remains authoritative until availability online.
                if kind == "status" and state.get("availability") == "offline":
                    online = False
                if kind == "availability":
                    state["availability"] = data["state"]
                robot.online_state = "ONLINE" if online else "OFFLINE"
                if not online:
                    state["telemetry_online"] = False
                robot.last_seen_at = received if online else None
                if online and previous_online == "OFFLINE":
                    robot.boot_id = str(uuid4())
                    # Availability proves connectivity, never the new session's
                    # chassis/navigation state. Wait for a post-reconnect status.
                    state["status_received_at"] = None
                if kind == "status":
                    state["status"] = data
                    state["status_received_at"] = data.get("ts") or (
                        None if envelope.get("retained") else received.isoformat()
                    )
                    task_state = data["task"]["state"]
                    robot.current_mode = {
                        "idle": "IDLE",
                        "patrol": "PATROL",
                        "homing": "RETURN_DOCK",
                        "spot": "NAVIGATING",
                    }[task_state]
            elif kind == "telemetry":
                source_at = datetime.fromisoformat(data["ts"]) if data.get("ts") else received
                age = (received - source_at).total_seconds()
                if (
                    not envelope.get("retained")
                    and state.get("availability") != "offline"
                    and -2 <= age <= get_settings().vehicle_agent_v1_status_stale_seconds
                ):
                    robot.last_seen_at = received
                    robot.online_state = "ONLINE"
                state["telemetry_online"] = robot.online_state == "ONLINE"
                state["telemetry"] = data
                state["telemetry_received_at"] = received.isoformat()
                timestamp = datetime.fromisoformat(data["ts"]) if data.get("ts") else None
                delay = (received - timestamp).total_seconds() * 1000 if timestamp else None
                state["receive_latency_ms"] = (
                    round(delay, 1) if delay is not None and delay >= 0 else None
                )
                robot.battery = (data.get("battery") or {}).get("percentage")
                if redis.set(f"vehicle-agent:sample:{vehicle}", "1", ex=1, nx=True):
                    db.add(
                        SensorSample(
                            robot_id=robot.id,
                            source_timestamp=timestamp or received,
                            server_received_at=received,
                            smoke=(data.get("smog") or {}).get("percentage"),
                            top_ir_max=(data.get("thermal") or {}).get("max_temp"),
                            payload_json=data,
                            boot_id=robot.boot_id,
                            seq=0,
                        )
                    )
            elif kind == "event":
                write_audit(
                    db,
                    action="VEHICLE_AGENT_EVENT",
                    resource_type="ROBOT",
                    resource_id=robot.id,
                    robot_id=robot.id,
                    actor_type="SYSTEM",
                    after=data,
                )
                queue_event(db, "vehicle.agent_event", {"vehicle_id": vehicle, "event": data})
            robot.agent_state_json = state
            db.flush()
            raw = redis.get(f"robot:{vehicle}:latest")
            latest = json.loads(raw) if raw else {}
            latest.update(
                {
                    "id": robot.id,
                    "robot_id": robot.id,
                    "vehicle_id": vehicle,
                    "agent_protocol": PROTOCOL,
                    "agent_state": state,
                    "online_state": robot.online_state,
                    "boot_id": robot.boot_id,
                    "mode": robot.current_mode,
                }
            )
            if kind == "telemetry":
                latest.update(
                    {
                        "battery": robot.battery,
                        "smoke": (data.get("smog") or {}).get("percentage"),
                        "top_ir": (data.get("thermal") or {}).get("max_temp"),
                        "server_received_at": received.isoformat(),
                        "source_timestamp": data.get("ts"),
                    }
                )
                pose = data.get("pose")
                map_row = db.get(Map, robot.current_map_id) if robot.current_map_id else None
                version = (
                    db.get(MapVersion, map_row.active_version_id)
                    if map_row and map_row.active_version_id
                    else None
                )
                if pose and version and pose["frame_id"] == version.frame_id:
                    latest.update(
                        {
                            "x": pose["x"],
                            "y": pose["y"],
                            "theta": pose["yaw"],
                            "frame_id": pose["frame_id"],
                        }
                    )
                else:
                    for field in ("x", "y", "theta", "frame_id"):
                        latest.pop(field, None)
            channel = db.scalar(
                select(RobotDataChannel).where(
                    RobotDataChannel.robot_id == robot.id, RobotDataChannel.channel == kind
                )
            )
            if not channel:
                channel = RobotDataChannel(robot_id=robot.id, channel=kind)
                db.add(channel)
            channel.support_state = (
                "CONNECTED" if robot.online_state == "ONLINE" else "NOT_CONNECTED"
            )
            channel.source_kind, channel.quality = PROTOCOL, "GOOD"
            channel.last_received_at = received
            latest.update(robot_readiness(db, robot))
            queue_redis_set(db, f"robot:{vehicle}:latest", json.dumps(latest, ensure_ascii=False))
            queue_event(db, "vehicle.agent_state", latest)
            if robot.online_state != previous_online:
                queue_event(db, f"vehicle.{robot.online_state.lower()}", latest)
            if kind == "status":
                converge_tasks(db, robot, received)
                converge_commands(db, robot, received)
        queue_redis_set(db, dedup, "1", ttl_seconds=300)
    return True
