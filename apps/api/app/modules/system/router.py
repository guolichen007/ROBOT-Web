from __future__ import annotations

import json
import socket
import threading
import time

from fastapi import APIRouter, Depends
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import case, func, select, text
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.dependencies import AuthContext, CurrentAuth, DbSession, require_permission
from app.core.events import current_watermark, get_redis
from app.core.metrics import (
    mqtt_duplicate_total,
    mqtt_ingress_rate,
    mqtt_invalid_total,
    mqtt_out_of_order_total,
    robot_online_total,
)
from app.core.serialization import serialize_model
from app.db.models import (
    ExtinguishPoint,
    FireEvent,
    InspectionPoint,
    Map,
    MapVersion,
    NavigationPreset,
    ParkingSlot,
    Robot,
    RobotCapability,
    RobotDataChannel,
    RobotIntegrationProfile,
    RobotSensorProfile,
    Site,
    StreamRegistry,
    Task,
    Trajectory,
)
from app.modules.commands.readiness import robot_readiness

router = APIRouter(tags=["system"])


def bounded_tcp_probe(host: str, port: int, timeout: float = 1.5) -> tuple[bool, str | None]:
    result: dict[str, object] = {}

    def probe() -> None:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                result["ok"] = True
        except Exception as exc:
            result["error"] = type(exc).__name__

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, "TimeoutError"
    return bool(result.get("ok")), str(result.get("error")) if result.get("error") else None


@router.get("/health/live")
def live() -> dict:
    return {"status": "live"}


def readiness_payload(db: DbSession) -> dict:
    settings = get_settings()
    checks: dict[str, dict] = {}
    postgres_ok, postgres_error = bounded_tcp_probe("postgres", 5432)
    if postgres_ok:
        try:
            db.execute(text("SELECT 1"))
            checks["postgresql"] = {"ok": True}
        except Exception as exc:
            checks["postgresql"] = {"ok": False, "error": type(exc).__name__}
    else:
        checks["postgresql"] = {"ok": False, "error": postgres_error}
    started = time.perf_counter()
    redis_ok, redis_error = bounded_tcp_probe("redis", 6379)
    checks["redis"] = (
        {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        if redis_ok
        else {"ok": False, "error": redis_error}
    )
    mqtt_heartbeat = (
        get_redis().get("service:mqtt-ingress:heartbeat")
        if checks.get("redis", {}).get("ok")
        else None
    )
    checks["mqtt_ingress"] = {"ok": bool(mqtt_heartbeat), "last_heartbeat": mqtt_heartbeat}
    for service_name in ("ros-compat-adapter", "task-worker"):
        heartbeat = (
            get_redis().get(f"service:{service_name}:heartbeat")
            if checks.get("redis", {}).get("ok")
            else None
        )
        checks[service_name.replace("-", "_")] = {
            "ok": bool(heartbeat),
            "last_heartbeat": heartbeat,
        }
    dispatcher_outbox = (
        get_redis().get("service:command-dispatcher:outbox-heartbeat")
        if checks.get("redis", {}).get("ok")
        else None
    )
    dispatcher_safety = (
        get_redis().get("service:command-dispatcher:safety-heartbeat")
        if checks.get("redis", {}).get("ok")
        else None
    )
    checks["command_dispatcher"] = {
        "ok": bool(dispatcher_outbox and dispatcher_safety),
        "outbox_heartbeat": dispatcher_outbox,
        "safety_heartbeat": dispatcher_safety,
    }
    mqtt_ok, mqtt_error = bounded_tcp_probe(settings.mqtt_host, settings.mqtt_port)
    checks["mqtt_broker"] = (
        {"ok": True, "endpoint": f"{settings.mqtt_host}:{settings.mqtt_port}"}
        if mqtt_ok
        else {"ok": False, "error": mqtt_error}
    )
    ok = all(item["ok"] for item in checks.values())
    return {"status": "ready" if ok else "degraded", "ok": ok, "checks": checks}


@router.get("/health/ready")
def ready(db: DbSession) -> Response:
    payload = readiness_payload(db)
    return JSONResponse(payload, status_code=200 if payload["ok"] else 503)


@router.get("/metrics")
def metrics(db: DbSession) -> Response:
    try:
        mqtt_values = get_redis().hgetall("metrics:mqtt")
    except Exception:
        mqtt_values = {}
    for field, raw_value in mqtt_values.items():
        value = float(raw_value)
        if field.startswith("ingress:"):
            mqtt_ingress_rate.labels(type=field.split(":", 1)[1]).set(value)
        elif field.startswith("invalid:"):
            mqtt_invalid_total.labels(reason=field.split(":", 1)[1]).set(value)
        elif field == "duplicate":
            mqtt_duplicate_total.set(value)
        elif field == "out_of_order":
            mqtt_out_of_order_total.set(value)
    online_count = db.scalar(
        select(func.count()).select_from(Robot).where(Robot.online_state == "ONLINE")
    )
    robot_online_total.set(online_count or 0)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/api/v1/system/status")
def system_status(db: DbSession, auth: CurrentAuth) -> dict:
    status = readiness_payload(db)
    media: dict[str, object]
    try:
        with socket.create_connection(("mediamtx", 9997), timeout=1.5):
            media = {"ok": True, "endpoint": "mediamtx:9997"}
    except Exception as exc:
        media = {"ok": False, "error": type(exc).__name__}
    status["checks"]["mediamtx"] = media
    status["server_deployed"] = False
    status["server_deployment_ready"] = True
    return status


def build_operation_context(db, robot: Robot) -> dict:
    """Server-authoritative mission interruption context for one robot.

    The frontend must read this instead of scanning historical tasks. At any
    moment a robot has either a running mission, an interrupted mission, or
    nothing.
    """
    active_states = {"CREATED", "QUEUED", "ACCEPTED", "EXECUTING"}

    def cursor_of(task: Task) -> dict:
        return (task.parameters_json or {}).get("live_route_cursor") or {}

    def checkpoint_of(task: Task) -> dict:
        return (task.parameters_json or {}).get("live_checkpoint") or {}

    base = {
        "state": "IDLE",
        "kind": None,
        "task_id": None,
        "patrol_plan_id": None,
        "last_completed_waypoint_index": None,
        "target_waypoint_index": None,
        "waypoint_total": None,
        "checkpoint_index": None,
        "checkpoint_total": None,
        "current_slot_code": None,
        "next_slot_code": None,
        "interrupted_reason": None,
        "can_continue": False,
        "can_return": True,
    }

    if robot.estop_active:
        base["state"] = "ESTOPPED"

    active = db.scalar(
        select(Task)
        .where(Task.robot_id == robot.id, Task.type.in_({"PATROL", "RETURN_DOCK"}), Task.status.in_(active_states))
        .order_by(Task.created_at.desc())
    )
    if active:
        base["state"] = "RUNNING"
        base["kind"] = "PATROL" if active.type == "PATROL" else "RETURN"
        base["task_id"] = active.id
        base["patrol_plan_id"] = (active.parameters_json or {}).get("patrol_plan_id")
        return base

    patrol = db.scalar(
        select(Task)
        .where(
            Task.robot_id == robot.id,
            Task.type == "PATROL",
            Task.status == "CANCELLED",
        )
        .order_by(Task.created_at.desc())
    )
    if patrol and (patrol.parameters_json or {}).get("resume_state") == "AVAILABLE":
        cursor = cursor_of(patrol)
        checkpoint = checkpoint_of(patrol)
        base.update(
            {
                "state": "PAUSED" if base["state"] != "ESTOPPED" else base["state"],
                "kind": "PATROL",
                "task_id": patrol.id,
                "patrol_plan_id": (patrol.parameters_json or {}).get("patrol_plan_id"),
                "last_completed_waypoint_index": cursor.get("last_completed_waypoint_index", cursor.get("waypoint_index")),
                "target_waypoint_index": cursor.get("target_waypoint_index"),
                "waypoint_total": cursor.get("waypoint_total"),
                "checkpoint_index": checkpoint.get("index"),
                "checkpoint_total": checkpoint.get("total"),
                "current_slot_code": checkpoint.get("current_slot_code"),
                "next_slot_code": checkpoint.get("next_slot_code"),
                "interrupted_reason": (patrol.parameters_json or {}).get("interruption_reason"),
                "can_continue": True,
                "can_return": True,
            }
        )
        return base

    ret = db.scalar(
        select(Task)
        .where(
            Task.robot_id == robot.id,
            Task.type == "RETURN_DOCK",
            Task.status == "CANCELLED",
        )
        .order_by(Task.created_at.desc())
    )
    if ret and (ret.parameters_json or {}).get("resume_state") == "AVAILABLE":
        base["state"] = "PAUSED" if base["state"] != "ESTOPPED" else base["state"]
        base["kind"] = "RETURN"
        base["task_id"] = ret.id
        base["can_continue"] = False
        base["can_return"] = True
        return base

    return base


@router.get("/api/v1/monitor/snapshot")
def monitor_snapshot(
    db: DbSession, _: AuthContext = Depends(require_permission("robot.read"))
) -> dict:
    site = db.scalar(select(Site).where(Site.active.is_(True)).order_by(Site.code))
    map_row = (
        db.scalar(select(Map).where(Map.site_id == site.id).order_by(Map.code)) if site else None
    )
    version = (
        db.get(MapVersion, map_row.active_version_id)
        if map_row and map_row.active_version_id
        else None
    )
    version_id = version.id if version else None
    robots = db.scalars(select(Robot).order_by(Robot.vehicle_id)).all()
    latest = []
    for robot in robots:
        raw = get_redis().get(f"robot:{robot.vehicle_id}:latest")
        state = json.loads(raw) if raw else serialize_model(robot)
        # Redis latest is an intentionally compact realtime projection.  The
        # monitor contract still needs stable database identity and display
        # fields, including when the snapshot was assembled from that cache.
        state.update(
            {
                "id": robot.id,
                "robot_id": robot.id,
                "vehicle_id": robot.vehicle_id,
                "name": robot.name,
                "model": robot.model,
            }
        )
        capability = db.get(RobotCapability, robot.id)
        integration = db.get(RobotIntegrationProfile, robot.id)
        state["supported_commands"] = capability.supported_commands_json if capability else []
        state["sensors"] = capability.sensors_json if capability else []
        state["media"] = capability.media_json if capability else []
        state["integration"] = serialize_model(integration) if integration else None
        readiness = robot_readiness(db, robot)
        state.update(readiness)
        if (
            integration
            and integration.source_kind == "ROS_COMPAT"
            and not (integration.bidirectional_bridge_verified)
        ):
            state["supported_commands"] = []
        state["control_disabled_reason"] = (
            None
            if state["control_enabled"]
            else (integration.read_only_reason if integration else "未建立集成配置")
        )
        state["data_channels"] = {
            row.channel: serialize_model(row)
            for row in db.scalars(
                select(RobotDataChannel).where(RobotDataChannel.robot_id == robot.id)
            ).all()
        }
        state["sensor_profiles"] = [
            serialize_model(row)
            for row in db.scalars(
                select(RobotSensorProfile).where(RobotSensorProfile.robot_id == robot.id)
            ).all()
        ]
        latest.append(state)
    severity_order = case(
        (FireEvent.severity == "CRITICAL", 4),
        (FireEvent.severity == "HIGH", 3),
        (FireEvent.severity == "MEDIUM", 2),
        (FireEvent.severity == "LOW", 1),
        else_=0,
    )
    state_order = case(
        (FireEvent.state == "NEW", 4),
        (FireEvent.state == "ACKNOWLEDGED", 3),
        (FireEvent.state == "CONFIRMED", 2),
        else_=1,
    )
    alarms = db.scalars(
        select(FireEvent)
        .where(FireEvent.state.not_in({"RESOLVED", "CLOSED", "DISMISSED"}))
        .order_by(severity_order.desc(), state_order.desc(), FireEvent.last_seen_at.desc())
    ).all()
    tasks = db.scalars(
        select(Task)
        .where(Task.status.not_in({"SUCCEEDED", "FAILED", "CANCELLED"}))
        .order_by(Task.created_at.desc())
    ).all()

    def version_rows(model):
        if not version_id:
            return []
        return [
            serialize_model(x)
            for x in db.scalars(select(model).where(model.map_version_id == version_id)).all()
        ]

    return {
        "snapshot_watermark": current_watermark(),
        "site": serialize_model(site) if site else None,
        "map": serialize_model(map_row) if map_row else None,
        "map_version": serialize_model(version) if version else None,
        "parking_slots": version_rows(ParkingSlot),
        "inspection_points": version_rows(InspectionPoint),
        "extinguish_points": version_rows(ExtinguishPoint),
        "trajectories": version_rows(Trajectory),
        "navigation_presets": version_rows(NavigationPreset),
        "robots": latest,
        "alarms": [serialize_model(x) for x in alarms],
        "tasks": [serialize_model(x) for x in tasks],
        "operation_context": build_operation_context(db, robots[0]) if robots else None,
        "streams": [
            serialize_model(x)
            for x in db.scalars(select(StreamRegistry).order_by(StreamRegistry.camera_type)).all()
        ],
    }
