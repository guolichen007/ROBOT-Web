from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.core.dependencies import CurrentAuth, DbSession
from app.core.serialization import serialize_model
from app.db.models import AuditLog, Command, FireEvent, SensorSample, Task, TelemetrySample
from app.modules.robots.router import find_robot

router = APIRouter(prefix="/api/v1/history", tags=["history"])


def range_filter(query, column, start: datetime | None, end: datetime | None):
    if start:
        query = query.where(column >= start)
    if end:
        query = query.where(column <= end)
    return query


@router.get("/telemetry")
def telemetry(
    db: DbSession,
    auth: CurrentAuth,
    robot_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 5000,
) -> list[dict]:
    robot = find_robot(db, robot_id)
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
    robot = find_robot(db, robot_id)
    query = select(SensorSample).where(SensorSample.robot_id == robot.id)
    query = (
        range_filter(query, SensorSample.server_received_at, start, end)
        .order_by(SensorSample.server_received_at)
        .limit(min(limit, 20000))
    )
    return [serialize_model(x) for x in db.scalars(query).all()]


@router.get("/tasks")
def tasks(db: DbSession, auth: CurrentAuth, limit: int = 1000) -> list[dict]:
    return [
        serialize_model(x)
        for x in db.scalars(select(Task).order_by(Task.created_at.desc()).limit(limit)).all()
    ]


@router.get("/commands")
def commands(db: DbSession, auth: CurrentAuth, limit: int = 1000) -> list[dict]:
    return [
        serialize_model(x)
        for x in db.scalars(select(Command).order_by(Command.issued_at.desc()).limit(limit)).all()
    ]


@router.get("/alarms")
def alarms(db: DbSession, auth: CurrentAuth, limit: int = 1000) -> list[dict]:
    return [
        serialize_model(x)
        for x in db.scalars(
            select(FireEvent).order_by(FireEvent.last_seen_at.desc()).limit(limit)
        ).all()
    ]


@router.get("/audit")
def audit(db: DbSession, auth: CurrentAuth, limit: int = 1000) -> list[dict]:
    if "audit.read" not in auth.permissions:
        raise HTTPException(403, "缺少 audit.read")
    return [
        serialize_model(x)
        for x in db.scalars(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        ).all()
    ]
