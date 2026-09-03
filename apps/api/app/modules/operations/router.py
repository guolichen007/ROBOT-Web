from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.audit import write_audit
from app.core.dependencies import AuthContext, DbSession, request_meta, require_permission
from app.core.errors import PlatformError
from app.core.events import append_event, get_redis
from app.core.idempotency import lookup, store
from app.core.serialization import serialize_model
from app.db.models import (
    Command,
    MapVersion,
    NavigationPreset,
    ParkingSlot,
    PatrolPlan,
    PatrolPlanPoint,
    PatrolSchedule,
    RobotDataChannel,
    RobotExternalAlias,
    RobotIntegrationProfile,
    RobotMotionProfile,
    RobotOperationEvent,
    RobotSensorProfile,
    StopOperation,
    Task,
    TaskEvent,
    Trajectory,
)
from app.modules.commands.readiness import robot_readiness
from app.modules.commands.service import (
    create_durable_command,
    create_safety_command,
    enqueue_safety_command,
    task_code,
)
from app.modules.robots.router import find_robot

router = APIRouter(prefix="/api/v1", tags=["operations"])


class PresetInput(BaseModel):
    map_version_id: str
    parking_slot_id: str | None = None
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str
    category: str = Field(pattern=r"^(INSPECTION|EXTINGUISH|WAITING_AREA|DOCK)$")
    pose_json: dict[str, float]
    position_tolerance_m: float = Field(default=0.2, gt=0, le=2)
    yaw_tolerance_rad: float = Field(default=0.15, gt=0, le=math.pi)
    allowed_approach_json: dict[str, Any] = Field(default_factory=dict)
    requires_reverse: bool = False
    is_default: bool = False
    enabled: bool = True


class SensorProfileInput(BaseModel):
    channel: str = "right_fire_detection"
    support_state: str = Field(
        default="CONNECTED",
        pattern=r"^(CONNECTED|STALE|NOT_CONNECTED|ERROR|UNSUPPORTED)$",
    )
    nominal_side: str = Field(default="RIGHT", pattern=r"^(RIGHT|LEFT|FRONT|REAR)$")
    sensor_mount_x_m: float = 0
    sensor_mount_y_m: float = 0
    sensor_mount_yaw_rad: float = -math.pi / 2
    coverage_range_m: float = Field(default=5, gt=0, le=100)
    coverage_fov_rad: float = Field(default=math.pi / 3, gt=0, le=math.pi * 2)
    config_source: str = "PLATFORM_DEFAULT"


class IntegrationInput(BaseModel):
    source_kind: str = Field(pattern=r"^(CANONICAL_MQTT|ROS_COMPAT|MOCK)$")
    upstream_protocol: str | None = None
    control_contract_verified: bool = False
    ack_contract_verified: bool = False
    map_contract_verified: bool = False
    bidirectional_bridge_verified: bool = False
    command_path_verified: bool = False
    cmd_vel_arbitration_verified: bool = False
    ros_control_mode: int | None = Field(default=None, ge=0, le=255)
    read_only_reason: str | None = None
    stale_seconds: int = Field(default=3, ge=2, le=120)
    offline_seconds: int = Field(default=10, ge=3, le=600)
    forward_only: bool = True
    reverse_precision_navigation: bool = False


class MotionProfileInput(BaseModel):
    max_manual_forward_mps: float | None = Field(default=None, gt=0, le=2)
    max_manual_reverse_mps: float | None = Field(default=None, gt=0, le=2)
    max_manual_angular_radps: float | None = Field(default=None, gt=0, le=3)
    manual_watchdog_verified: bool = False
    reverse_allowed: bool = False
    reverse_precision_verified: bool = False


class AliasInput(BaseModel):
    robot_id: str
    external_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class NavigatePresetInput(BaseModel):
    navigation_preset_id: str


class PatrolPlanPointInput(BaseModel):
    navigation_preset_id: str
    sequence: int = Field(ge=1)
    dwell_seconds: int = Field(default=3, ge=0, le=3600)
    required_observations_json: list[str] = Field(default_factory=list)


class PatrolPlanInput(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str
    robot_id: str
    map_version_id: str
    trajectory_id: str | None = None
    enabled: bool = True
    points: list[PatrolPlanPointInput] = Field(default_factory=list)


class ScheduleInput(BaseModel):
    patrol_plan_id: str
    cron_expression: str = Field(pattern=r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")
    timezone: str = "Asia/Shanghai"
    enabled: bool = False
    misfire_policy: str = Field(default="SKIP", pattern=r"^(SKIP|RUN_IF_WITHIN_WINDOW)$")
    misfire_grace_seconds: int = Field(default=0, ge=0, le=86400)
    overlap_policy: str = Field(default="SKIP", pattern=r"^(SKIP|QUEUE|REJECT)$")
    queue_expiry_seconds: int = Field(default=300, ge=1, le=86400)
    require_robot_online: bool = True
    require_control_contract_verified: bool = True
    require_map_contract_verified: bool = True


def next_schedule_run(
    expression: str, timezone_name: str, base: datetime | None = None
) -> datetime:
    """Calculate the next five-field cron occurrence in the declared IANA timezone."""

    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise PlatformError("INVALID_TIMEZONE", "时区必须是有效的 IANA 时区") from exc
    if not croniter.is_valid(expression):
        raise PlatformError("INVALID_CRON", "定时表达式必须是有效的五段 cron 表达式")
    local_base = (base or datetime.now(UTC)).astimezone(zone)
    return croniter(expression, local_base).get_next(datetime).astimezone(UTC)


def _draft_version(db, version_id: str) -> MapVersion:
    row = db.get(MapVersion, version_id)
    if not row:
        raise PlatformError("RESOURCE_NOT_FOUND", "地图版本不存在", status_code=404)
    if row.status != "DRAFT":
        raise PlatformError("MAP_VERSION_IMMUTABLE", "已发布地图不可原地修改", status_code=409)
    return row


@router.get("/navigation-presets")
def list_presets(
    db: DbSession,
    map_version_id: str | None = None,
    _: AuthContext = Depends(require_permission("map.read")),
) -> list[dict]:
    query = select(NavigationPreset).order_by(NavigationPreset.category, NavigationPreset.code)
    if map_version_id:
        query = query.where(NavigationPreset.map_version_id == map_version_id)
    return [serialize_model(row) for row in db.scalars(query).all()]


@router.post("/navigation-presets", status_code=201)
def create_preset(
    payload: PresetInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    version = _draft_version(db, payload.map_version_id)
    if payload.requires_reverse:
        raise PlatformError(
            "REVERSE_PRECISION_UNSUPPORTED",
            "当前 AGV 能力边界不承诺需要倒车的精准到位任务",
            status_code=409,
        )
    row = NavigationPreset(**payload.model_dump(), semantic_revision=version.semantic_revision)
    db.add(row)
    db.flush()
    write_audit(
        db,
        action="NAVIGATION_PRESET_CREATE",
        resource_type="NAVIGATION_PRESET",
        user_id=auth.user.id,
        resource_id=row.id,
        after=serialize_model(row),
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)


@router.post("/robots/{robot_id}/navigate-preset", status_code=202)
def navigate_to_preset(
    robot_id: str,
    payload: NavigatePresetInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.task")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    """Navigate through canonical 1.2 patrol semantics and verify final pose server-side."""

    robot = find_robot(db, robot_id)
    endpoint = f"/robots/{robot.id}/navigate-preset"
    body = payload.model_dump()
    cached = lookup(db, actor_id=auth.user.id, endpoint=endpoint, key=idempotency_key, payload=body)
    if cached:
        return cached.response_json
    preset = db.get(NavigationPreset, payload.navigation_preset_id)
    if not preset or not preset.enabled:
        raise PlatformError("RESOURCE_NOT_FOUND", "预设位置不存在或已停用", status_code=404)
    version = db.get(MapVersion, preset.map_version_id)
    if not version or version.status != "PUBLISHED":
        raise PlatformError("MAP_VERSION_NOT_PUBLISHED", "预设位置必须属于已发布地图")
    if robot.current_map_id != version.map_id or robot.current_map_version != version.version:
        raise PlatformError("MAP_VERSION_MISMATCH", "机器人与预设位置的地图版本不一致")
    integration = db.get(RobotIntegrationProfile, robot.id)
    if preset.requires_reverse and not (integration and integration.reverse_precision_navigation):
        raise PlatformError(
            "REVERSE_PRECISION_UNSUPPORTED",
            "当前 AGV 能力边界不承诺需要倒车的精确到位任务",
            status_code=409,
        )
    task = Task(
        task_code=task_code("N"),
        robot_id=robot.id,
        type="NAVIGATE_TO_PRESET",
        status="CREATED",
        phase="CREATED",
        progress=0,
        target_pose_snapshot_json=preset.pose_json,
        map_id_snapshot=version.map_id,
        map_version_snapshot=version.version,
        semantic_revision_snapshot=preset.semantic_revision,
        parameters_json={
            "mission_kind": "NAVIGATE_TO_PRESET",
            "navigation_preset_id": preset.id,
            "position_tolerance_m": preset.position_tolerance_m,
            "yaw_tolerance_rad": preset.yaw_tolerance_rad,
            "pose_freshness_ms": 1000,
            "required_verification_frames": 3,
            "verification_timeout_seconds": 30,
        },
        created_by=auth.user.id,
    )
    db.add(task)
    db.flush()
    db.add(TaskEvent(task_id=task.id, status="CREATED", phase="CREATED", progress=0))
    command = create_durable_command(
        db,
        robot=robot,
        operator_id=auth.user.id,
        cmd="patrol",
        task_id=task.id,
        params={
            "task_id": task.id,
            "mission_kind": "NAVIGATE_TO_PRESET",
            "navigation_preset_id": preset.id,
            "target_pose": preset.pose_json,
            "map_id": version.map_id,
            "map_version": version.version,
            "semantic_revision": preset.semantic_revision,
        },
        priority=55,
    )
    task.status = "QUEUED"
    task.phase = "COMMAND_QUEUED"
    result = serialize_model(task)
    result["command_id"] = command.command_id
    store(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=body,
        response=result,
    )
    write_audit(
        db,
        action="NAVIGATE_PRESET_CREATE",
        resource_type="TASK",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=task.id,
        after=result,
        **request_meta(request),
    )
    db.commit()
    append_event("task.created", result)
    return result


@router.get("/robots/{robot_id}/sensor-profiles")
def sensor_profiles(
    robot_id: str,
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> list[dict]:
    robot = find_robot(db, robot_id)
    return [
        serialize_model(row)
        for row in db.scalars(
            select(RobotSensorProfile).where(RobotSensorProfile.robot_id == robot.id)
        ).all()
    ]


@router.put("/robots/{robot_id}/sensor-profiles/{channel}")
def update_sensor_profile(
    robot_id: str,
    channel: str,
    payload: SensorProfileInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("settings.manage")),
) -> dict:
    robot = find_robot(db, robot_id)
    if payload.channel != channel:
        raise PlatformError("CHANNEL_MISMATCH", "URL 与请求体通道不一致")
    row = db.scalar(
        select(RobotSensorProfile).where(
            RobotSensorProfile.robot_id == robot.id, RobotSensorProfile.channel == channel
        )
    )
    before = serialize_model(row) if row else None
    if not row:
        row = RobotSensorProfile(robot_id=robot.id, **payload.model_dump())
        db.add(row)
    else:
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
    db.flush()
    write_audit(
        db,
        action="ROBOT_SENSOR_PROFILE_UPDATE",
        resource_type="ROBOT_SENSOR_PROFILE",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=row.id,
        before=before,
        after=serialize_model(row),
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)


def _rotate(x: float, y: float, yaw: float) -> tuple[float, float]:
    return (x * math.cos(yaw) - y * math.sin(yaw), x * math.sin(yaw) + y * math.cos(yaw))


def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        if (y1 > y) != (y2 > y):
            boundary_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < boundary_x:
                inside = not inside
    return inside


def _orientation(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(a, b, c, d) -> bool:
    return (
        _orientation(a, b, c) * _orientation(a, b, d) <= 0
        and _orientation(c, d, a) * _orientation(c, d, b) <= 0
    )


def _polygons_intersect(left: list[tuple[float, float]], right: list[tuple[float, float]]) -> bool:
    if any(_point_in_polygon(point, right) for point in left):
        return True
    if any(_point_in_polygon(point, left) for point in right):
        return True
    return any(
        _segments_intersect(
            left[i], left[(i + 1) % len(left)], right[j], right[(j + 1) % len(right)]
        )
        for i in range(len(left))
        for j in range(len(right))
    )


def calculate_detection_coverage(
    *,
    robot_pose: dict[str, float],
    profile: RobotSensorProfile,
    slots: list[ParkingSlot],
) -> dict:
    theta = float(robot_pose["theta"])
    mount_dx, mount_dy = _rotate(profile.sensor_mount_x_m, profile.sensor_mount_y_m, theta)
    origin = (float(robot_pose["x"]) + mount_dx, float(robot_pose["y"]) + mount_dy)
    center_yaw = theta + profile.sensor_mount_yaw_rad
    start_yaw = center_yaw - profile.coverage_fov_rad / 2
    sector = [origin]
    for index in range(25):
        yaw = start_yaw + profile.coverage_fov_rad * index / 24
        sector.append(
            (
                origin[0] + profile.coverage_range_m * math.cos(yaw),
                origin[1] + profile.coverage_range_m * math.sin(yaw),
            )
        )

    # The vehicle only detects its right side. Never mirror a mis-configured
    # mount into a fake "correct" sector; surface the invariant violation.
    if not _sector_is_on_vehicle_right(robot_pose, sector):
        return {
            "state": "ERROR",
            "reason": "RIGHT_SENSOR_ORIENTATION_INVALID",
            "polygon": [],
            "covered_parking_slot_ids": [],
        }

    covered: list[str] = []
    for slot in slots:
        source = (
            slot.polygon_json.get("points", [])
            if isinstance(slot.polygon_json, dict)
            else slot.polygon_json
        )
        polygon = [(float(item["x"]), float(item["y"])) for item in source]
        if polygon and _polygons_intersect(sector, polygon):
            covered.append(slot.id)
    return {
        "state": "CONNECTED",
        "channel": profile.channel,
        "nominal_side": profile.nominal_side,
        "sensor_origin": {"x": origin[0], "y": origin[1], "yaw": center_yaw},
        "polygon": [{"x": x, "y": y} for x, y in sector],
        "covered_parking_slot_ids": covered,
        "configuration": serialize_model(profile),
    }


def _sector_is_on_vehicle_right(
    robot_pose: dict[str, float], sector: list[tuple[float, float]]
) -> bool:
    theta = float(robot_pose["theta"])
    arc = sector[1:]
    centroid = (
        sum(point[0] for point in arc) / len(arc),
        sum(point[1] for point in arc) / len(arc),
    )
    rel = (centroid[0] - float(robot_pose["x"]), centroid[1] - float(robot_pose["y"]))
    right = (math.sin(theta), -math.cos(theta))
    return rel[0] * right[0] + rel[1] * right[1] > 0


@router.get("/robots/{robot_id}/detection-coverage")
def detection_coverage(
    robot_id: str,
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> dict:
    robot = find_robot(db, robot_id)
    profile = db.scalar(
        select(RobotSensorProfile).where(
            RobotSensorProfile.robot_id == robot.id,
            RobotSensorProfile.channel == "right_fire_detection",
        )
    )
    if not profile or profile.support_state != "CONNECTED":
        return {
            "state": profile.support_state if profile else "NOT_CONNECTED",
            "polygon": [],
            "covered_parking_slot_ids": [],
        }
    raw = get_redis().get(f"robot:{robot.vehicle_id}:latest")
    latest = json.loads(raw) if raw else {}
    received_raw = latest.get("server_received_at")
    try:
        received = datetime.fromisoformat(received_raw) if received_raw else None
        pose_fresh = bool(received and (datetime.now(UTC) - received).total_seconds() <= 2)
    except (TypeError, ValueError):
        pose_fresh = False
    localization_ok = latest.get("localization_status") in {
        "OK",
        "GOOD",
        "VALID",
        "VALID_SOURCE",
    }
    if (
        robot.online_state != "ONLINE"
        or not pose_fresh
        or not localization_ok
        or not all(key in latest for key in ("x", "y", "theta"))
    ):
        return {
            "state": "STALE",
            "polygon": [],
            "covered_parking_slot_ids": [],
            "reason": "ROBOT_OR_LOCALIZATION_NOT_FRESH",
        }
    slots = db.scalars(
        select(ParkingSlot).where(
            ParkingSlot.map_version_id.in_(
                select(MapVersion.id).where(
                    MapVersion.map_id == robot.current_map_id,
                    MapVersion.version == robot.current_map_version,
                )
            )
        )
    ).all()
    return calculate_detection_coverage(robot_pose=latest, profile=profile, slots=list(slots))


@router.get("/robots/{robot_id}/integration")
def integration_status(
    robot_id: str,
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> dict:
    robot = find_robot(db, robot_id)
    profile = db.get(RobotIntegrationProfile, robot.id)
    channels = db.scalars(
        select(RobotDataChannel).where(RobotDataChannel.robot_id == robot.id)
    ).all()
    return {
        "profile": serialize_model(profile) if profile else None,
        **robot_readiness(db, robot),
        "disabled_reason": None
        if profile and profile.control_contract_verified
        else (profile.read_only_reason if profile else "未建立集成配置"),
        "channels": [serialize_model(row) for row in channels],
    }


@router.put("/robots/{robot_id}/integration")
def update_integration(
    robot_id: str,
    payload: IntegrationInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("settings.manage")),
) -> dict:
    robot = find_robot(db, robot_id)
    row = db.get(RobotIntegrationProfile, robot.id)
    before = serialize_model(row) if row else None
    values = payload.model_dump()
    values["verified_at"] = (
        datetime.now(UTC)
        if all(
            values[key]
            for key in (
                "control_contract_verified",
                "ack_contract_verified",
                "map_contract_verified",
            )
        )
        else None
    )
    if values["source_kind"] == "ROS_COMPAT" and not values["bidirectional_bridge_verified"]:
        values["read_only_reason"] = (
            values["read_only_reason"]
            or "ROS1 兼容链路仅完成只读上行；下行 bridge、仲裁与 watchdog 尚未验证"
        )
    if row:
        for key, value in values.items():
            setattr(row, key, value)
    else:
        row = RobotIntegrationProfile(robot_id=robot.id, **values)
        db.add(row)
    db.flush()
    write_audit(
        db,
        action="ROBOT_INTEGRATION_UPDATE",
        resource_type="ROBOT_INTEGRATION_PROFILE",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=robot.id,
        before=before,
        after=serialize_model(row),
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)


@router.get("/robots/{robot_id}/motion-profile")
def motion_profile(
    robot_id: str,
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> dict | None:
    robot = find_robot(db, robot_id)
    row = db.get(RobotMotionProfile, robot.id)
    return serialize_model(row) if row else None


@router.put("/robots/{robot_id}/motion-profile")
def update_motion_profile(
    robot_id: str,
    payload: MotionProfileInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("settings.manage")),
) -> dict:
    robot = find_robot(db, robot_id)
    row = db.get(RobotMotionProfile, robot.id)
    before = serialize_model(row) if row else None
    values = payload.model_dump()
    if payload.manual_watchdog_verified and (
        payload.max_manual_forward_mps is None or payload.max_manual_angular_radps is None
    ):
        raise PlatformError(
            "MOTION_PROFILE_INCOMPLETE",
            "验证 manual watchdog 前必须提供前进与角速度上限",
        )
    if payload.reverse_allowed and payload.max_manual_reverse_mps is None:
        raise PlatformError("MOTION_PROFILE_INCOMPLETE", "允许倒车时必须提供倒车速度上限")
    if row:
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = datetime.now(UTC)
    else:
        row = RobotMotionProfile(robot_id=robot.id, **values)
        db.add(row)
    db.flush()
    write_audit(
        db,
        action="ROBOT_MOTION_PROFILE_UPDATE",
        resource_type="ROBOT_MOTION_PROFILE",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=robot.id,
        before=before,
        after=serialize_model(row),
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)


@router.post("/integration/ros-native/aliases", status_code=201)
def confirm_alias(
    payload: AliasInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("settings.manage")),
) -> dict:
    robot = find_robot(db, payload.robot_id)
    existing = db.scalar(
        select(RobotExternalAlias).where(RobotExternalAlias.external_id == payload.external_id)
    )
    if existing and existing.robot_id != robot.id:
        raise PlatformError("EXTERNAL_ID_CONFLICT", "该外部设备已绑定其他机器人", status_code=409)
    row = existing or RobotExternalAlias(
        robot_id=robot.id,
        source_kind="ROS_NATIVE",
        external_id=payload.external_id,
        state="CONFIRMED",
        confirmed_by=auth.user.id,
        confirmed_at=datetime.now(UTC),
    )
    db.add(row)
    get_redis().delete(f"ros_compat:discovery:{payload.external_id}")
    write_audit(
        db,
        action="ROS_NATIVE_ALIAS_CONFIRM",
        resource_type="ROBOT_EXTERNAL_ALIAS",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=row.id,
        after={"external_id": payload.external_id},
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)


@router.get("/integration/ros-native/discoveries")
def discoveries(
    _: AuthContext = Depends(require_permission("settings.manage")),
) -> list[dict]:
    rows: list[dict] = []
    for key in get_redis().scan_iter("ros_compat:discovery:*"):
        raw = get_redis().get(key)
        if raw:
            rows.append(json.loads(raw))
    return sorted(rows, key=lambda item: item.get("last_seen_at", ""), reverse=True)


@router.get("/patrol-plans")
def list_patrol_plans(
    db: DbSession, _: AuthContext = Depends(require_permission("robot.read"))
) -> list[dict]:
    rows = []
    for plan in db.scalars(select(PatrolPlan).order_by(PatrolPlan.code)).all():
        item = serialize_model(plan)
        item["points"] = [
            serialize_model(point)
            for point in db.scalars(
                select(PatrolPlanPoint)
                .where(PatrolPlanPoint.patrol_plan_id == plan.id)
                .order_by(PatrolPlanPoint.sequence)
            ).all()
        ]
        rows.append(item)
    return rows


@router.post("/patrol-plans", status_code=201)
def create_patrol_plan(
    payload: PatrolPlanInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    robot = find_robot(db, payload.robot_id)
    version = db.get(MapVersion, payload.map_version_id)
    if not version:
        raise PlatformError("RESOURCE_NOT_FOUND", "地图版本不存在", status_code=404)
    if payload.trajectory_id and not db.get(Trajectory, payload.trajectory_id):
        raise PlatformError("RESOURCE_NOT_FOUND", "轨迹不存在", status_code=404)
    row = PatrolPlan(
        code=payload.code,
        name=payload.name,
        robot_id=robot.id,
        map_version_id=version.id,
        trajectory_id=payload.trajectory_id,
        enabled=payload.enabled,
        created_by=auth.user.id,
    )
    db.add(row)
    db.flush()
    for point in payload.points:
        preset = db.get(NavigationPreset, point.navigation_preset_id)
        if not preset or preset.map_version_id != version.id:
            raise PlatformError("MAP_VERSION_MISMATCH", "巡检点与计划地图版本不一致")
        db.add(PatrolPlanPoint(patrol_plan_id=row.id, **point.model_dump()))
    write_audit(
        db,
        action="PATROL_PLAN_CREATE",
        resource_type="PATROL_PLAN",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=row.id,
        after=payload.model_dump(),
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)


@router.post("/patrol-schedules", status_code=201)
def create_schedule(
    payload: ScheduleInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
) -> dict:
    if not db.get(PatrolPlan, payload.patrol_plan_id):
        raise PlatformError("RESOURCE_NOT_FOUND", "巡检计划不存在", status_code=404)
    values = payload.model_dump()
    values["next_run_at"] = next_schedule_run(payload.cron_expression, payload.timezone)
    row = PatrolSchedule(**values, created_by=auth.user.id)
    db.add(row)
    db.flush()
    write_audit(
        db,
        action="PATROL_SCHEDULE_CREATE",
        resource_type="PATROL_SCHEDULE",
        user_id=auth.user.id,
        resource_id=row.id,
        after=payload.model_dump(),
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)


@router.post("/robots/{robot_id}/stop-operation", status_code=202)
@router.post("/robots/{robot_id}/stop-patrol", status_code=202)
def stop_current_operation(
    robot_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.stop")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    robot = find_robot(db, robot_id)
    endpoint = f"/robots/{robot.id}/stop-operation"
    body: dict[str, Any] = {}
    cached = lookup(db, actor_id=auth.user.id, endpoint=endpoint, key=idempotency_key, payload=body)
    if cached:
        return cached.response_json
    active = db.scalar(
        select(Task)
        .where(
            Task.robot_id == robot.id,
            Task.type.in_({"PATROL", "RETURN_DOCK", "NAVIGATE_TO_PRESET", "EXTINGUISH"}),
            Task.status.in_({"CREATED", "QUEUED", "ACCEPTED", "EXECUTING"}),
        )
        .order_by(Task.created_at.desc())
    )
    # Safety stop is authorized independently and first. A missing map
    # contract is therefore never allowed to block a verified stop path.
    stop, stop_payload = create_safety_command(
        db,
        robot=robot,
        operator_id=auth.user.id,
        cmd="stop_motion",
        params={"reason": "OPERATOR_STOP"},
        task_id=active.id if active else None,
        ttl_ms=3000,
        priority=99,
    )
    cancel = None
    mission_cancel_state = "NOT_REQUIRED"
    if active:
        try:
            cancel = create_durable_command(
                db,
                robot=robot,
                operator_id=auth.user.id,
                cmd="cancel_task",
                task_id=active.id,
                params={"task_id": active.id, "reason": "OPERATOR_STOP"},
                priority=96,
            )
            active.phase = "STOP_REQUESTED"
            mission_cancel_state = "WAITING_ACK"
        except PlatformError:
            # A mission-cancel limitation never suppresses an independently
            # authorized motion stop. The operation records that partial truth.
            mission_cancel_state = "UNAVAILABLE"
    operation = StopOperation(
        robot_id=robot.id,
        boot_id_snapshot=robot.boot_id,
        task_id=active.id if active else None,
        cancel_command_id=cancel.command_id if cancel else None,
        stop_command_id=stop.command_id,
        state="STOP_REQUESTED",
        motion_stop_state="WAITING_ACK",
        mission_cancel_state=mission_cancel_state,
        requested_by=auth.user.id,
        stop_ack_deadline_at=datetime.now(UTC) + timedelta(seconds=5),
        cancel_deadline_at=(datetime.now(UTC) + timedelta(seconds=10)) if cancel else None,
        stationary_verify_deadline_at=datetime.now(UTC) + timedelta(seconds=15),
    )
    db.add(operation)
    db.add(
        RobotOperationEvent(
            robot_id=robot.id,
            task_id=active.id if active else None,
            operation_type="STOP_OPERATION",
            state="STOP_REQUESTED",
            payload_json={
                "cancel_command_id": cancel.command_id if cancel else None,
                "stop_command_id": stop.command_id,
            },
        )
    )
    db.flush()
    result = serialize_model(operation)
    store(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=body,
        response=result,
        status_code=202,
    )
    write_audit(
        db,
        action="STOP_OPERATION_REQUEST",
        resource_type="STOP_OPERATION",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=operation.id,
        after=result,
        **request_meta(request),
    )
    db.commit()
    if robot.online_state == "ONLINE":
        try:
            enqueue_safety_command(stop_payload)
        except Exception:
            persisted = db.get(Command, stop.id)
            if persisted:
                persisted.lifecycle_status = "PUBLISHED_UNCONFIRMED"
                persisted.ack_reason = "SAFETY_QUEUE_UNAVAILABLE"
                db.commit()
    append_event("operation.stop.updated", result)
    return result


@router.get("/stop-operations/{operation_id}")
def stop_operation_detail(
    operation_id: str,
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> dict:
    row = db.get(StopOperation, operation_id)
    if not row:
        raise PlatformError("RESOURCE_NOT_FOUND", "停止操作不存在", status_code=404)
    return serialize_model(row)
