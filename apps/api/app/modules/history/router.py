from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.audit import write_audit
from app.core.dependencies import AuthContext, CurrentAuth, DbSession, request_meta
from app.core.scope import alarm_scope_clause, scoped_robot_ids
from app.core.serialization import serialize_model
from app.db.models import (
    AuditLog,
    Command,
    FireEvent,
    PatrolPlan,
    PatrolSchedule,
    PatrolScheduleOccurrence,
    Robot,
    SensorSample,
    Task,
    TelemetrySample,
    User,
)
from app.modules.robots.router import find_robot

router = APIRouter(prefix="/api/v1/history", tags=["history"])

EXPORT_MAX_ROWS = 10_000
# WEB-R1-MIN 导出授权：每类记录导出所需权限（与页面查看权限一致或更严）。
EXPORT_PERMISSIONS = {
    "tasks": "robot.read",
    "plan-previews": "robot.read",
    "alarms": "alarm.read",
    "operations": "audit.read",
    "audit": "audit.read",
}
# 导出额外要求：普通 viewer 只能查看，不能导出（导出需具备业务操作或审计权限）。
EXPORT_EXTRA = {"patrol.create", "alarm.ack", "audit.read"}


def range_filter(query, column, start: datetime | None, end: datetime | None):
    if start:
        query = query.where(column >= start)
    if end:
        query = query.where(column <= end)
    return query


def _require(auth: AuthContext, permission: str) -> None:
    if permission not in auth.permissions:
        raise HTTPException(403, f"缺少 {permission}")


def _names(db) -> tuple[dict[str, str], dict[str, str]]:
    robots = {r.id: r.vehicle_id for r in db.scalars(select(Robot)).all()}
    users = {u.id: u.display_name for u in db.scalars(select(User)).all()}
    return robots, users


@router.get("/telemetry")
def telemetry(
    db: DbSession,
    auth: CurrentAuth,
    robot_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 5000,
) -> list[dict]:
    _require(auth, "robot.read")
    robot = find_robot(db, robot_id, auth)
    query = select(TelemetrySample).where(TelemetrySample.robot_id == robot.id)
    query = (
        range_filter(query, TelemetrySample.server_received_at, start, end)
        .order_by(TelemetrySample.server_received_at)
        .limit(min(limit, 20000))
    )
    return [serialize_model(x) for x in db.scalars(query).all()]


@router.get("/sensors")
def sensors(
    db: DbSession,
    auth: CurrentAuth,
    robot_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 5000,
) -> list[dict]:
    _require(auth, "robot.read")
    robot = find_robot(db, robot_id, auth)
    query = select(SensorSample).where(SensorSample.robot_id == robot.id)
    query = (
        range_filter(query, SensorSample.server_received_at, start, end)
        .order_by(SensorSample.server_received_at)
        .limit(min(limit, 20000))
    )
    return [serialize_model(x) for x in db.scalars(query).all()]


def task_rows(
    db,
    auth: AuthContext,
    *,
    robot_id: str | None,
    status: str | None,
    type_: str | None,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    _require(auth, "robot.read")
    query = select(Task).order_by(Task.created_at.desc())
    visible = scoped_robot_ids(db, auth.permissions)
    if visible is not None:
        query = query.where(Task.robot_id.in_(visible or {""}))
    if robot_id:
        query = query.where(Task.robot_id == find_robot(db, robot_id, auth).id)
    if status:
        query = query.where(Task.status == status)
    if type_:
        query = query.where(Task.type == type_)
    query = range_filter(query, Task.created_at, start, end).limit(min(limit, EXPORT_MAX_ROWS))
    robots, users = _names(db)
    rows = []
    for task in db.scalars(query).all():
        item = serialize_model(task)
        item["robot_vehicle_id"] = robots.get(task.robot_id)
        item["created_by_name"] = users.get(task.created_by)
        item["source"] = (task.parameters_json or {}).get("source")
        rows.append(item)
    return rows


def plan_preview_rows(
    db,
    auth: AuthContext,
    *,
    robot_id: str | None,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    _require(auth, "robot.read")
    query = (
        select(PatrolScheduleOccurrence, PatrolSchedule, PatrolPlan)
        .join(PatrolSchedule, PatrolSchedule.id == PatrolScheduleOccurrence.schedule_id)
        .join(PatrolPlan, PatrolPlan.id == PatrolSchedule.patrol_plan_id)
        .order_by(PatrolScheduleOccurrence.created_at.desc())
    )
    visible = scoped_robot_ids(db, auth.permissions)
    if visible is not None:
        query = query.where(PatrolPlan.robot_id.in_(visible or {""}))
    if robot_id:
        query = query.where(PatrolPlan.robot_id == find_robot(db, robot_id, auth).id)
    query = range_filter(query, PatrolScheduleOccurrence.scheduled_for, start, end).limit(
        min(limit, EXPORT_MAX_ROWS)
    )
    robots, _ = _names(db)
    return [
        {
            "id": occurrence.id,
            "plan_id": plan.id,
            "plan_code": plan.code,
            "plan_name": plan.name,
            "execution_mode": plan.execution_mode,
            "robot_vehicle_id": robots.get(plan.robot_id),
            "scheduled_for": occurrence.scheduled_for.isoformat(),
            "state": occurrence.state,
            "reason_code": occurrence.reason_code,
            "task_id": occurrence.task_id,
            "created_at": occurrence.created_at.isoformat() if occurrence.created_at else None,
        }
        for occurrence, _schedule, plan in db.execute(query).all()
    ]


def alarm_rows(
    db,
    auth: AuthContext,
    *,
    robot_id: str | None,
    state: str | None,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    _require(auth, "alarm.read")
    query = select(FireEvent).order_by(FireEvent.last_seen_at.desc())
    clause = alarm_scope_clause(db, auth.permissions)
    if clause is not None:
        query = query.where(clause)
    if robot_id:
        query = query.where(FireEvent.robot_id == find_robot(db, robot_id, auth).id)
    if state:
        query = query.where(FireEvent.state == state)
    query = range_filter(query, FireEvent.last_seen_at, start, end).limit(
        min(limit, EXPORT_MAX_ROWS)
    )
    robots, users = _names(db)
    rows = []
    for alarm in db.scalars(query).all():
        item = serialize_model(alarm)
        item["robot_vehicle_id"] = robots.get(alarm.robot_id or "")
        item["ack_by_name"] = users.get(alarm.ack_by or "")
        rows.append(item)
    return rows


def audit_scope_filters(db, auth: AuthContext) -> list:
    """P1-02 审计范围：持 settings.manage 的管理员看全部；其他 audit.read 账号只看
    (a) 本站点车辆关联的记录，(b) 自己本人的操作记录。
    robot_id 为空的全局人员 / 认证 / 导出审计（他人的 USER_* / AUTH_* 等）不再放行。"""
    visible = scoped_robot_ids(db, auth.permissions)
    if visible is None:
        return []
    return [AuditLog.robot_id.in_(visible or {""}) | (AuditLog.user_id == auth.user.id)]


OPERATION_PREFIXES = ("USER_", "AUTH_", "PATROL_PLAN", "ALARM_", "TASK_", "EXPORT_")


def audit_rows(
    db,
    auth: AuthContext,
    *,
    operations_only: bool,
    user_id: str | None,
    robot_id: str | None,
    action: str | None,
    result: str | None,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    _require(auth, "audit.read")
    query = select(AuditLog).order_by(AuditLog.created_at.desc())
    if operations_only:
        query = query.where(AuditLog.actor_type == "USER")
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if robot_id:
        query = query.where(AuditLog.robot_id == find_robot(db, robot_id, auth).id)
    if action:
        query = query.where(AuditLog.action.ilike(f"%{action}%"))
    if result:
        query = query.where(AuditLog.result == result)
    query = query.where(*audit_scope_filters(db, auth))
    query = range_filter(query, AuditLog.created_at, start, end).limit(min(limit, EXPORT_MAX_ROWS))
    robots, users = _names(db)
    rows = []
    for log in db.scalars(query).all():
        item = serialize_model(log)
        item["user_name"] = users.get(log.user_id or "")
        item["robot_vehicle_id"] = robots.get(log.robot_id or "")
        rows.append(item)
    return rows


@router.get("/tasks")
def tasks(
    db: DbSession,
    auth: CurrentAuth,
    robot_id: str | None = None,
    status: str | None = None,
    type: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict]:
    return task_rows(
        db, auth, robot_id=robot_id, status=status, type_=type, start=start, end=end, limit=limit
    )


@router.get("/plan-previews")
def plan_previews(
    db: DbSession,
    auth: CurrentAuth,
    robot_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict]:
    return plan_preview_rows(db, auth, robot_id=robot_id, start=start, end=end, limit=limit)


@router.get("/commands")
def commands(db: DbSession, auth: CurrentAuth, limit: int = 1000) -> list[dict]:
    _require(auth, "robot.read")
    query = select(Command).order_by(Command.issued_at.desc()).limit(min(limit, EXPORT_MAX_ROWS))
    visible = scoped_robot_ids(db, auth.permissions)
    if visible is not None:
        query = query.where(Command.robot_id.in_(visible or {""}))
    return [serialize_model(x) for x in db.scalars(query).all()]


@router.get("/alarms")
def alarms(
    db: DbSession,
    auth: CurrentAuth,
    robot_id: str | None = None,
    state: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict]:
    return alarm_rows(db, auth, robot_id=robot_id, state=state, start=start, end=end, limit=limit)


@router.get("/operations")
def operations(
    db: DbSession,
    auth: CurrentAuth,
    user_id: str | None = None,
    robot_id: str | None = None,
    action: str | None = None,
    result: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict]:
    return audit_rows(
        db,
        auth,
        operations_only=True,
        user_id=user_id,
        robot_id=robot_id,
        action=action,
        result=result,
        start=start,
        end=end,
        limit=limit,
    )


@router.get("/audit")
def audit(
    db: DbSession,
    auth: CurrentAuth,
    user_id: str | None = None,
    robot_id: str | None = None,
    action: str | None = None,
    result: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[dict]:
    return audit_rows(
        db,
        auth,
        operations_only=False,
        user_id=user_id,
        robot_id=robot_id,
        action=action,
        result=result,
        start=start,
        end=end,
        limit=limit,
    )


EXPORT_COLUMNS: dict[str, list[str]] = {
    "tasks": [
        "task_code",
        "robot_vehicle_id",
        "type",
        "status",
        "phase",
        "source",
        "created_by_name",
        "created_at",
        "completed_at",
        "failure_code",
        "failure_message",
    ],
    "plan-previews": [
        "plan_code",
        "plan_name",
        "execution_mode",
        "robot_vehicle_id",
        "scheduled_for",
        "state",
        "reason_code",
        "created_at",
    ],
    "alarms": [
        "event_code",
        "robot_vehicle_id",
        "location_state",
        "reported_slot_code",
        "fire_type",
        "severity",
        "state",
        "occurrence_count",
        "first_seen_at",
        "last_seen_at",
        "ack_by_name",
        "ack_at",
        "resolved_at",
    ],
    "operations": [
        "created_at",
        "user_name",
        "action",
        "resource_type",
        "resource_id",
        "robot_vehicle_id",
        "result",
        "ip",
        "request_id",
    ],
    "audit": [
        "created_at",
        "actor_type",
        "user_name",
        "action",
        "resource_type",
        "resource_id",
        "robot_vehicle_id",
        "result",
        "ip",
        "request_id",
    ],
}


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # CSV 公式注入防护：以 = + - @ 开头的单元格前置单引号。
    if text[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + text
    return text


@router.get("/export/{kind}")
def export(
    kind: str,
    request: Request,
    db: DbSession,
    auth: CurrentAuth,
    robot_id: str | None = None,
    user_id: str | None = None,
    status: str | None = None,
    state: str | None = None,
    type: str | None = None,
    action: str | None = None,
    result: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> StreamingResponse:
    """授权导出：与页面查询使用同一过滤函数，导出内容 = 页面筛选结果。"""
    if kind not in EXPORT_PERMISSIONS:
        raise HTTPException(404, "不支持的导出类型")
    required = EXPORT_PERMISSIONS[kind]
    if required not in auth.permissions or not (auth.permissions & EXPORT_EXTRA):
        write_audit(
            db,
            action="EXPORT_DENIED",
            resource_type="EXPORT",
            user_id=auth.user.id,
            resource_id=kind,
            result="DENIED",
            **request_meta(request),
        )
        db.commit()
        raise HTTPException(403, "当前账号没有导出该类记录的权限")
    limit = EXPORT_MAX_ROWS
    if kind == "tasks":
        rows = task_rows(
            db,
            auth,
            robot_id=robot_id,
            status=status,
            type_=type,
            start=start,
            end=end,
            limit=limit,
        )
    elif kind == "plan-previews":
        rows = plan_preview_rows(db, auth, robot_id=robot_id, start=start, end=end, limit=limit)
    elif kind == "alarms":
        rows = alarm_rows(
            db, auth, robot_id=robot_id, state=state, start=start, end=end, limit=limit
        )
    else:
        rows = audit_rows(
            db,
            auth,
            operations_only=kind == "operations",
            user_id=user_id,
            robot_id=robot_id,
            action=action,
            result=result,
            start=start,
            end=end,
            limit=limit,
        )
    columns = EXPORT_COLUMNS[kind]
    buffer = io.StringIO()
    buffer.write("﻿")  # Excel 识别 UTF-8
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    write_audit(
        db,
        action="EXPORT_RECORDS",
        resource_type="EXPORT",
        user_id=auth.user.id,
        resource_id=kind,
        after={
            "kind": kind,
            "rows": len(rows),
            "filters": {
                k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in {
                    "robot_id": robot_id,
                    "user_id": user_id,
                    "status": status,
                    "state": state,
                    "type": type,
                    "action": action,
                    "result": result,
                    "start": start,
                    "end": end,
                }.items()
                if v
            },
        },
        **request_meta(request),
    )
    db.commit()
    filename = f"firebot-{kind}-{datetime.now():%Y%m%d%H%M%S}.csv"
    return StreamingResponse(
        iter([buffer.getvalue().encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
