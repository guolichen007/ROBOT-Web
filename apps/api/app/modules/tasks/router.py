from __future__ import annotations

import json
import math

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.audit import write_audit
from app.core.dependencies import (
    AuthContext,
    CurrentAuth,
    DbSession,
    request_meta,
    require_permission,
)
from app.core.errors import PlatformError
from app.core.events import append_event, get_redis
from app.core.idempotency import lookup, store
from app.core.serialization import serialize_model
from app.db.models import (
    ExtinguishPoint,
    FireEvent,
    MapVersion,
    ParkingSlot,
    PatrolPlan,
    Robot,
    Task,
    TaskEvent,
    Trajectory,
)
from app.modules.commands.service import create_durable_command, task_code
from app.modules.robots.router import find_robot
from app.modules.tasks.patrol import build_patrol_task, build_resumed_patrol_task
from app.modules.tasks.return_dock import build_return_task

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class TaskInput(BaseModel):
    robot_id: str
    target_parking_slot_id: str
    trajectory_id: str | None = None
    fire_event_id: str | None = None
    parameters: dict = Field(default_factory=dict)


class PatrolPlanTaskInput(BaseModel):
    robot_id: str
    patrol_plan_id: str
    parameters: dict = Field(default_factory=dict)
    resume_task_id: str | None = None


class ReturnDockTaskInput(BaseModel):
    robot_id: str
    parameters: dict = Field(default_factory=dict)


EXTINGUISH_MODES = {
    "DEPLOY_BLANKET": "展开灭火帐",
    "SPRAY_AGENT": "喷射灭火剂",
    "DEPLOY_THEN_SPRAY": "先展开灭火帐，再喷射灭火剂",
}

REMOTE_WAITING_POSE = {"x": 1.2, "y": 1.2}
WAITING_TOLERANCE_M = 0.8


def _robot_at_waiting(db, robot: Robot) -> bool:
    raw = get_redis().get(f"robot:{robot.vehicle_id}:latest")
    latest = json.loads(raw) if raw else {}
    if "x" not in latest or "y" not in latest:
        return False
    return math.hypot(float(latest["x"]) - REMOTE_WAITING_POSE["x"], float(latest["y"]) - REMOTE_WAITING_POSE["y"]) <= WAITING_TOLERANCE_M


def target_snapshot(
    db, robot: Robot, payload: TaskInput, task_type: str
) -> tuple[ParkingSlot, MapVersion, dict, list | None]:
    slot = db.get(ParkingSlot, payload.target_parking_slot_id)
    if not slot:
        raise HTTPException(404, "目标车位不存在")
    version = db.get(MapVersion, slot.map_version_id)
    if not version or version.status != "PUBLISHED":
        raise HTTPException(409, "目标必须属于 Published 地图版本")
    if robot.current_map_id != version.map_id or robot.current_map_version != version.version:
        raise PlatformError(
            "MAP_VERSION_MISMATCH",
            "机器人地图版本与任务目标不一致",
            details={
                "robot_map_version": robot.current_map_version,
                "target_map_version": version.version,
            },
        )
    pose = slot.center_pose_json
    if task_type == "EXTINGUISH":
        point = db.scalar(select(ExtinguishPoint).where(ExtinguishPoint.parking_slot_id == slot.id))
        if not point:
            raise HTTPException(409, "目标车位缺少灭火操作点")
        pose = point.pose_json
    trajectory = db.get(Trajectory, payload.trajectory_id) if payload.trajectory_id else None
    if trajectory and trajectory.map_version_id != version.id:
        raise HTTPException(409, "轨迹与目标地图版本不一致")
    return slot, version, pose, trajectory.path_json if trajectory else None


def create_task(
    *,
    task_type: str,
    permission: str,
    payload: TaskInput,
    key: str,
    request: Request,
    db,
    auth: AuthContext,
) -> dict:
    endpoint = f"/tasks/{task_type.lower()}"
    body = payload.model_dump()
    cached = lookup(db, actor_id=auth.user.id, endpoint=endpoint, key=key, payload=body)
    if cached:
        return cached.response_json
    robot = find_robot(db, payload.robot_id)
    if task_type == "EXTINGUISH":
        mode = payload.parameters.get("extinguish_mode")
        if mode not in EXTINGUISH_MODES:
            raise PlatformError(
                "EXTINGUISH_MODE_REQUIRED",
                "灭火任务必须明确选择处理方式",
                details={"allowed": list(EXTINGUISH_MODES)},
            )
    slot, version, pose, trajectory = target_snapshot(db, robot, payload, task_type)
    row = Task(
        task_code=task_code(),
        robot_id=robot.id,
        fire_event_id=payload.fire_event_id,
        type=task_type,
        status="CREATED",
        phase="CREATED",
        progress=0,
        target_parking_slot_id=slot.id,
        target_pose_snapshot_json=pose,
        map_id_snapshot=version.map_id,
        map_version_snapshot=version.version,
        semantic_revision_snapshot=version.semantic_revision,
        trajectory_snapshot_json=trajectory,
        parameters_json=payload.parameters,
        created_by=auth.user.id,
    )
    db.add(row)
    db.flush()
    db.add(TaskEvent(task_id=row.id, status="CREATED", phase="CREATED", progress=0))
    command = create_durable_command(
        db,
        robot=robot,
        operator_id=auth.user.id,
        cmd=task_type.lower(),
        task_id=row.id,
        params={
            "task_id": row.id,
            "task_code": row.task_code,
            "target_pose": pose,
            "map_id": version.map_id,
            "map_version": version.version,
            "semantic_revision": version.semantic_revision,
            "trajectory": trajectory,
            **payload.parameters,
        },
        priority=70 if task_type == "EXTINGUISH" else 50,
    )
    row.status = "QUEUED"
    row.phase = "COMMAND_QUEUED"
    if payload.fire_event_id:
        fire = db.get(FireEvent, payload.fire_event_id)
        if not fire:
            raise HTTPException(404, "火情不存在")
        fire.assigned_task_id = row.id
        fire.state = "DISPATCHED"
    response = serialize_model(row)
    response["command_id"] = command.command_id
    store(db, actor_id=auth.user.id, endpoint=endpoint, key=key, payload=body, response=response)
    write_audit(
        db,
        action=f"TASK_{task_type}_CREATE",
        resource_type="TASK",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=row.id,
        after=response,
        **request_meta(request),
    )
    db.commit()
    append_event("task.created", response)
    return response


@router.get("")
def list_tasks(db: DbSession, auth: CurrentAuth, limit: int = 300) -> list[dict]:
    return [
        serialize_model(x)
        for x in db.scalars(
            select(Task).order_by(Task.created_at.desc()).limit(min(limit, 1000))
        ).all()
    ]


@router.get("/{task_id}")
def task_detail(task_id: str, db: DbSession, auth: CurrentAuth) -> dict:
    row = db.get(Task, task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    result = serialize_model(row)
    result["events"] = [
        serialize_model(x)
        for x in db.scalars(
            select(TaskEvent).where(TaskEvent.task_id == row.id).order_by(TaskEvent.created_at)
        ).all()
    ]
    return result


@router.post("/patrol", status_code=201)
def patrol(
    payload: TaskInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    endpoint = "/tasks/patrol"
    body = payload.model_dump()
    cached = lookup(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=body,
    )
    if cached:
        return cached.response_json
    robot = find_robot(db, payload.robot_id)
    plan = db.scalar(
        select(PatrolPlan)
        .where(PatrolPlan.robot_id == robot.id, PatrolPlan.enabled.is_(True))
        .order_by(PatrolPlan.code)
    )
    if not plan:
        raise PlatformError("PATROL_PLAN_INVALID", "当前机器人没有启用的巡检计划")
    task, command_id = build_patrol_task(
        db,
        plan=plan,
        robot=robot,
        actor_id=auth.user.id,
        source=str(payload.parameters.get("source", "EXTERNAL_API")),
    )
    response = {**serialize_model(task), "command_id": command_id}
    store(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=body,
        response=response,
    )
    write_audit(
        db,
        action="TASK_PATROL_CREATE",
        resource_type="TASK",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=task.id,
        after=response,
        **request_meta(request),
    )
    db.commit()
    append_event("task.created", response)
    return response


@router.post("/patrol-plan", status_code=201)
def patrol_plan_task(
    payload: PatrolPlanTaskInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    endpoint = "/tasks/patrol-plan"
    body = payload.model_dump()
    cached = lookup(db, actor_id=auth.user.id, endpoint=endpoint, key=idempotency_key, payload=body)
    if cached:
        return cached.response_json
    robot = find_robot(db, payload.robot_id)
    plan = db.get(PatrolPlan, payload.patrol_plan_id)
    if not plan or plan.robot_id != robot.id:
        raise PlatformError("PATROL_PLAN_INVALID", "巡检计划不存在或不属于当前机器人")

    # Server-side hard guards: never allow a fresh full patrol from an
    # arbitrary pose, and never allow it while a patrol is resumable.
    resume_available = db.scalar(
        select(Task)
        .where(
            Task.robot_id == robot.id,
            Task.type == "PATROL",
            Task.status == "CANCELLED",
        )
        .order_by(Task.created_at.desc())
    )
    resume_available = (
        resume_available
        if resume_available and (resume_available.parameters_json or {}).get("resume_state") == "AVAILABLE"
        else None
    )
    if payload.resume_task_id:
        previous = db.get(Task, payload.resume_task_id)
        if (
            not previous
            or previous.robot_id != robot.id
            or previous.type != "PATROL"
            or previous.status != "CANCELLED"
        ):
            raise PlatformError(
                "PATROL_RESUME_INVALID", "上次巡检状态无法安全恢复，请先返回等待区"
            )
        task, command_id = build_resumed_patrol_task(
            db,
            plan=plan,
            robot=robot,
            actor_id=auth.user.id,
            source=str(payload.parameters.get("source", "OPERATIONS_HMI")),
            previous_task=previous,
        )
    else:
        if resume_available:
            raise PlatformError(
                "PATROL_RESUME_REQUIRED", "存在未完成巡检，请选择继续巡检或返回等待区"
            )
        if not _robot_at_waiting(db, robot):
            raise PlatformError(
                "PATROL_START_REQUIRES_WAITING_AREA", "机器人当前不在等待区，请先返回等待区"
            )
        task, command_id = build_patrol_task(
            db,
            plan=plan,
            robot=robot,
            actor_id=auth.user.id,
            source=str(payload.parameters.get("source", "OPERATIONS_HMI")),
        )
    response = {**serialize_model(task), "command_id": command_id}
    store(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=body,
        response=response,
    )
    write_audit(
        db,
        action="TASK_PATROL_CREATE",
        resource_type="TASK",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=task.id,
        after=response,
        **request_meta(request),
    )
    db.commit()
    append_event("task.created", response)
    return response


@router.post("/return-dock", status_code=201)
def return_dock(
    payload: ReturnDockTaskInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    endpoint = "/tasks/return-dock"
    body = payload.model_dump()
    cached = lookup(db, actor_id=auth.user.id, endpoint=endpoint, key=idempotency_key, payload=body)
    if cached:
        return cached.response_json
    robot = find_robot(db, payload.robot_id)
    # Resume context is cleared by a successful return: find the latest
    # cancelled patrol so we can reverse its route cursor back home.
    previous = db.scalar(
        select(Task)
        .where(
            Task.robot_id == robot.id,
            Task.type == "PATROL",
            Task.status == "CANCELLED",
        )
        .order_by(Task.created_at.desc())
    )
    task, command_id = build_return_task(
        db,
        robot=robot,
        actor_id=auth.user.id,
        source=str(payload.parameters.get("source", "OPERATIONS_HMI")),
        previous_task=previous,
    )
    response = {**serialize_model(task), "command_id": command_id}
    store(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=body,
        response=response,
    )
    write_audit(
        db,
        action="RETURN_DOCK",
        resource_type="TASK",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=task.id,
        after=response,
        **request_meta(request),
    )
    db.commit()
    append_event("task.created", response)
    return response


@router.post("/extinguish", status_code=201)
def extinguish(
    payload: TaskInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("extinguish.create")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    return create_task(
        task_type="EXTINGUISH",
        permission="extinguish.create",
        payload=payload,
        key=idempotency_key,
        request=request,
        db=db,
        auth=auth,
    )


@router.post("/{task_id}/cancel", status_code=202)
def cancel_task(
    task_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.task")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    endpoint = f"/tasks/{task_id}/cancel"
    body = {"task_id": task_id}
    cached = lookup(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=body,
    )
    if cached:
        return cached.response_json
    row = db.get(Task, task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if row.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        raise HTTPException(409, "任务已处于终态")
    robot = db.get(Robot, row.robot_id)
    if not robot:
        raise HTTPException(409, "任务所属机器人不存在")
    command = create_durable_command(
        db,
        robot=robot,
        operator_id=auth.user.id,
        cmd="cancel_task",
        task_id=row.id,
        params={"task_id": row.id},
        priority=80,
    )
    row.phase = "CANCEL_QUEUED"
    db.add(TaskEvent(task_id=row.id, status=row.status, phase=row.phase, progress=row.progress))
    db.flush()
    result = serialize_model(row)
    result["command_id"] = command.command_id
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
        action="TASK_CANCEL",
        resource_type="TASK",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=row.id,
        after={"command_id": command.command_id},
        **request_meta(request),
    )
    db.commit()
    append_event("task.updated", serialize_model(row))
    return result
