from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.audit import write_audit
from app.core.dependencies import (
    AuthContext,
    DbSession,
    request_meta,
    require_permission,
)
from app.core.errors import PlatformError
from app.core.events import append_event
from app.core.idempotency import lookup, store
from app.core.scope import alarm_scope_clause, alarm_visible, allowed_site_ids, slot_site_id
from app.core.serialization import serialize_model
from app.db.models import (
    Command,
    FireEvent,
    MapVersion,
    ParkingSlot,
    Robot,
    RobotOperationEvent,
    Task,
    TaskEvent,
    User,
)
from app.modules.tasks.router import TaskInput, create_task

router = APIRouter(prefix="/api/v1/alarms", tags=["alarms"])


class ManualAlarmInput(BaseModel):
    parking_slot_id: str
    fire_type: str = Field(pattern="^(smoke|flame|unknown)$")
    note: str = Field(default="", max_length=2000)
    map_version: str
    severity: str = Field(default="HIGH", pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    media: dict = Field(default_factory=dict)


class CreateTaskInput(BaseModel):
    robot_id: str
    trajectory_id: str | None = None
    parameters: dict = Field(default_factory=dict)


ACTIVE_ALARM_STATES = {"NEW", "ACKNOWLEDGED", "CONFIRMED", "DISPATCHED", "IN_PROGRESS"}


def get_visible_alarm(db, auth: AuthContext, alarm_id: str) -> FireEvent:
    row = db.get(FireEvent, alarm_id)
    if not row or not alarm_visible(db, auth.permissions, row):
        raise HTTPException(404, "火情不存在")
    return row


def alarm_view(db, row: FireEvent) -> dict:
    """在 FireEvent 原始字段上补充可读关联（车辆、车位、确认人）。"""
    data = serialize_model(row)
    robot = db.get(Robot, row.robot_id) if row.robot_id else None
    slot = db.get(ParkingSlot, row.parking_slot_id) if row.parking_slot_id else None
    acker = db.get(User, row.ack_by) if row.ack_by else None
    data["robot_vehicle_id"] = robot.vehicle_id if robot else None
    data["parking_slot_code"] = slot.code if slot else None
    data["location_label"] = (
        slot.code if slot else f"位置未匹配（车端上报：{row.reported_slot_code or '无车位编码'}）"
    )
    data["ack_by_name"] = acker.display_name if acker else None
    return data


@router.get("")
def list_alarms(
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.read")),
    state: str | None = None,
    active: bool | None = None,
    severity: str | None = None,
    robot_id: str | None = None,
    location_state: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 300,
) -> list[dict]:
    query = select(FireEvent).order_by(FireEvent.last_seen_at.desc()).limit(min(limit, 1000))
    clause = alarm_scope_clause(db, auth.permissions)
    if clause is not None:
        query = query.where(clause)
    if state:
        query = query.where(FireEvent.state == state)
    if active is True:
        query = query.where(FireEvent.state.in_(ACTIVE_ALARM_STATES))
    if severity:
        query = query.where(FireEvent.severity == severity)
    if robot_id:
        robot = db.scalar(
            select(Robot).where((Robot.id == robot_id) | (Robot.vehicle_id == robot_id))
        )
        query = query.where(FireEvent.robot_id == (robot.id if robot else "__none__"))
    if location_state:
        query = query.where(FireEvent.location_state == location_state)
    if start:
        query = query.where(FireEvent.last_seen_at >= start)
    if end:
        query = query.where(FireEvent.last_seen_at <= end)
    return [alarm_view(db, x) for x in db.scalars(query).all()]


@router.get("/{alarm_id}")
def alarm_detail(
    alarm_id: str,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.read")),
) -> dict:
    return alarm_view(db, get_visible_alarm(db, auth, alarm_id))


@router.get("/{alarm_id}/timeline")
def alarm_timeline(
    alarm_id: str,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.read")),
) -> list[dict]:
    alarm = get_visible_alarm(db, auth, alarm_id)
    items: list[dict] = []

    def add(occurred_at, source_type, state, label, **ids) -> None:
        if occurred_at:
            items.append(
                {
                    "occurred_at": occurred_at,
                    "source_type": source_type,
                    "state": state,
                    "label": label,
                    **ids,
                }
            )

    add(alarm.first_seen_at, "ALARM", "NEW", "发现火情")
    add(alarm.ack_at, "ALARM", "ACKNOWLEDGED", "操作员已确认收到")
    add(alarm.confirmed_at, "ALARM", "CONFIRMED", "火情已确认")
    add(alarm.resolved_at, "ALARM", "RESOLVED", "火情已解决")
    add(alarm.closed_at, "ALARM", "CLOSED", "火情已关闭")
    tasks = db.scalars(select(Task).where(Task.fire_event_id == alarm.id)).all()
    for task in tasks:
        add(task.created_at, "TASK", "CREATED", "已创建灭火任务", task_id=task.id)
        for event in db.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.created_at)
        ).all():
            add(
                event.created_at,
                "TASK",
                event.status,
                event.phase,
                task_id=task.id,
            )
        for command in db.scalars(
            select(Command).where(Command.task_id == task.id).order_by(Command.issued_at)
        ).all():
            add(
                command.issued_at,
                "COMMAND",
                "CREATED",
                f"已创建 {command.cmd} 命令",
                task_id=task.id,
                command_id=command.command_id,
                correlation_id=command.correlation_id,
            )
            add(
                command.published_at,
                "COMMAND",
                "PUBLISHED",
                "命令已发布到车辆通道",
                task_id=task.id,
                command_id=command.command_id,
                correlation_id=command.correlation_id,
            )
            add(
                command.ack_at,
                "COMMAND",
                f"ACK_{str(command.ack_status or 'UNKNOWN').upper()}",
                f"车端 ACK：{command.ack_status or '未知'}",
                task_id=task.id,
                command_id=command.command_id,
                correlation_id=command.correlation_id,
            )
            add(
                command.terminal_at,
                "COMMAND",
                command.lifecycle_status,
                f"命令终态：{command.lifecycle_status}",
                task_id=task.id,
                command_id=command.command_id,
                correlation_id=command.correlation_id,
            )
        for operation in db.scalars(
            select(RobotOperationEvent)
            .where(RobotOperationEvent.task_id == task.id)
            .order_by(RobotOperationEvent.occurred_at)
        ).all():
            add(
                operation.occurred_at,
                "OPERATION",
                operation.state,
                operation.operation_type,
                task_id=task.id,
            )
    items.sort(key=lambda item: item["occurred_at"])
    return items


@router.get("/{alarm_id}/actions")
def alarm_actions(
    alarm_id: str,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.read")),
) -> list[dict]:
    """处置记录：谁在何时对该火警做了什么（含处置备注），来源为审计日志。"""
    from app.db.models import AuditLog

    alarm = get_visible_alarm(db, auth, alarm_id)
    rows = []
    for log in db.scalars(
        select(AuditLog)
        .where(AuditLog.resource_type == "FIRE_EVENT", AuditLog.resource_id == alarm.id)
        .order_by(AuditLog.created_at)
    ).all():
        actor = db.get(User, log.user_id) if log.user_id else None
        rows.append(
            {
                "id": log.id,
                "created_at": log.created_at.isoformat() if log.created_at else None,
                "action": log.action,
                "result": log.result,
                "actor": actor.display_name if actor else None,
                "before_state": (log.before_json or {}).get("state"),
                "after_state": (log.after_json or {}).get("state"),
                "note": (log.after_json or {}).get("note"),
            }
        )
    return rows


@router.post("/manual", status_code=201)
def manual_alarm(
    payload: ManualAlarmInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.confirm")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    body = payload.model_dump()
    cached = lookup(
        db, actor_id=auth.user.id, endpoint="/alarms/manual", key=idempotency_key, payload=body
    )
    if cached:
        return cached.response_json
    slot = db.get(ParkingSlot, payload.parking_slot_id)
    sites = allowed_site_ids(db, auth.permissions)
    if not slot or (sites is not None and slot_site_id(db, slot.id) not in sites):
        raise HTTPException(404, "车位不存在")
    version = db.get(MapVersion, slot.map_version_id)
    if not version or version.version != payload.map_version:
        raise PlatformError(
            "MAP_VERSION_MISMATCH",
            "人工火情地图版本不匹配",
            details={"requested_map_version": payload.map_version},
        )
    now = datetime.now(UTC)
    fingerprint = hashlib.sha256(
        f"MANUAL:{slot.id}:{payload.fire_type}:{now:%Y%m%d%H%M}".encode()
    ).hexdigest()
    row = FireEvent(
        event_code=f"FE-MANUAL-{now:%Y%m%d%H%M%S}-{str(uuid4())[:6]}",
        parking_slot_id=slot.id,
        detection_method="MANUAL",
        fire_type=payload.fire_type,
        severity=payload.severity,
        fingerprint=fingerprint,
        state="NEW",
        first_seen_at=now,
        last_seen_at=now,
        source_position_json=slot.center_pose_json,
        media_snapshot_json=payload.media,
        note=payload.note,
    )
    db.add(row)
    db.flush()
    response = alarm_view(db, row)
    store(
        db,
        actor_id=auth.user.id,
        endpoint="/alarms/manual",
        key=idempotency_key,
        payload=body,
        response=response,
    )
    write_audit(
        db,
        action="ALARM_MANUAL_CREATE",
        resource_type="FIRE_EVENT",
        user_id=auth.user.id,
        resource_id=row.id,
        after=response,
        **request_meta(request),
    )
    db.commit()
    append_event("alarm.created", response)
    return response


class AlarmActionInput(BaseModel):
    note: str = Field(default="", max_length=2000)


def transition(
    alarm_id: str,
    target: str,
    permission: str,
    request: Request,
    db,
    auth: AuthContext,
    note: str = "",
) -> dict:
    row = get_visible_alarm(db, auth, alarm_id)
    before = row.state
    allowed = {
        "NEW": {"ACKNOWLEDGED", "CONFIRMED", "DISMISSED"},
        "ACKNOWLEDGED": {"CONFIRMED", "DISMISSED"},
        "CONFIRMED": {"RESOLVED", "DISMISSED"},
        "DISPATCHED": {"IN_PROGRESS", "RESOLVED"},
        "IN_PROGRESS": {"RESOLVED"},
        "RESOLVED": {"CLOSED"},
    }
    if target not in allowed.get(before, set()):
        raise HTTPException(409, f"不允许从 {before} 转换为 {target}")
    now = datetime.now(UTC)
    row.state = target
    if target == "ACKNOWLEDGED":
        row.ack_by = auth.user.id
        row.ack_at = now
    if target == "CONFIRMED":
        row.confirmed_at = now
    if target == "RESOLVED":
        row.resolved_at = now
    if target == "CLOSED":
        row.closed_at = now
    write_audit(
        db,
        action=f"ALARM_{target}",
        resource_type="FIRE_EVENT",
        user_id=auth.user.id,
        robot_id=row.robot_id,
        resource_id=row.id,
        before={"state": before},
        after={"state": target, "note": note or None},
        **request_meta(request),
    )
    if note:
        row.note = (f"{row.note}\n" if row.note else "") + f"[{target}] {note}"
    db.commit()
    append_event("alarm.updated", serialize_model(row))
    return alarm_view(db, row)


@router.post("/{alarm_id}/acknowledge")
def acknowledge(
    alarm_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.ack")),
    payload: AlarmActionInput | None = None,
) -> dict:
    return transition(
        alarm_id, "ACKNOWLEDGED", "alarm.ack", request, db, auth, (payload.note if payload else "")
    )


@router.post("/{alarm_id}/confirm")
def confirm(
    alarm_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.confirm")),
    payload: AlarmActionInput | None = None,
) -> dict:
    return transition(
        alarm_id, "CONFIRMED", "alarm.confirm", request, db, auth, (payload.note if payload else "")
    )


@router.post("/{alarm_id}/dismiss")
def dismiss(
    alarm_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.dismiss")),
    payload: AlarmActionInput | None = None,
) -> dict:
    return transition(
        alarm_id, "DISMISSED", "alarm.dismiss", request, db, auth, (payload.note if payload else "")
    )


@router.post("/{alarm_id}/resolve")
def resolve(
    alarm_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.resolve")),
    payload: AlarmActionInput | None = None,
) -> dict:
    return transition(
        alarm_id, "RESOLVED", "alarm.resolve", request, db, auth, (payload.note if payload else "")
    )


@router.post("/{alarm_id}/create-task", status_code=201)
def alarm_create_task(
    alarm_id: str,
    payload: CreateTaskInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("extinguish.create")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    alarm = get_visible_alarm(db, auth, alarm_id)
    if alarm.state not in {"NEW", "ACKNOWLEDGED", "CONFIRMED"}:
        raise HTTPException(409, "该火情状态不允许创建灭火任务")
    if not alarm.parking_slot_id:
        raise PlatformError(
            "ALARM_LOCATION_UNMATCHED",
            "火情位置未匹配车位，不能自动派发灭火任务，请现场核实位置",
        )
    task_payload = TaskInput(
        robot_id=payload.robot_id,
        target_parking_slot_id=alarm.parking_slot_id,
        trajectory_id=payload.trajectory_id,
        fire_event_id=alarm.id,
        parameters=payload.parameters,
    )
    return create_task(
        task_type="EXTINGUISH",
        permission="extinguish.create",
        payload=task_payload,
        key=idempotency_key,
        request=request,
        db=db,
        auth=auth,
    )
