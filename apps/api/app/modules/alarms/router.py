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
    CurrentAuth,
    DbSession,
    request_meta,
    require_permission,
)
from app.core.events import append_event
from app.core.idempotency import lookup, store
from app.core.serialization import serialize_model
from app.db.models import FireEvent, MapVersion, ParkingSlot
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


@router.get("")
def list_alarms(
    db: DbSession, auth: CurrentAuth, state: str | None = None, limit: int = 300
) -> list[dict]:
    query = select(FireEvent).order_by(FireEvent.last_seen_at.desc()).limit(min(limit, 1000))
    if state:
        query = query.where(FireEvent.state == state)
    return [serialize_model(x) for x in db.scalars(query).all()]


@router.get("/{alarm_id}")
def alarm_detail(alarm_id: str, db: DbSession, auth: CurrentAuth) -> dict:
    row = db.get(FireEvent, alarm_id)
    if not row:
        raise HTTPException(404, "火情不存在")
    return serialize_model(row)


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
    if not slot:
        raise HTTPException(404, "车位不存在")
    version = db.get(MapVersion, slot.map_version_id)
    if not version or version.version != payload.map_version:
        raise HTTPException(409, "人工火情地图版本不匹配")
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
        state="CONFIRMED",
        first_seen_at=now,
        last_seen_at=now,
        confirmed_at=now,
        source_position_json=slot.center_pose_json,
        media_snapshot_json=payload.media,
        note=payload.note,
    )
    db.add(row)
    db.flush()
    response = serialize_model(row)
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


def transition(
    alarm_id: str, target: str, permission: str, request: Request, db, auth: AuthContext
) -> dict:
    row = db.get(FireEvent, alarm_id)
    if not row:
        raise HTTPException(404, "火情不存在")
    before = row.state
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
        after={"state": target},
        **request_meta(request),
    )
    db.commit()
    append_event("alarm.updated", serialize_model(row))
    return serialize_model(row)


@router.post("/{alarm_id}/acknowledge")
def acknowledge(
    alarm_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.ack")),
) -> dict:
    return transition(alarm_id, "ACKNOWLEDGED", "alarm.ack", request, db, auth)


@router.post("/{alarm_id}/confirm")
def confirm(
    alarm_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.confirm")),
) -> dict:
    return transition(alarm_id, "CONFIRMED", "alarm.confirm", request, db, auth)


@router.post("/{alarm_id}/dismiss")
def dismiss(
    alarm_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.dismiss")),
) -> dict:
    return transition(alarm_id, "DISMISSED", "alarm.dismiss", request, db, auth)


@router.post("/{alarm_id}/resolve")
def resolve(
    alarm_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("alarm.resolve")),
) -> dict:
    return transition(alarm_id, "RESOLVED", "alarm.resolve", request, db, auth)


@router.post("/{alarm_id}/create-task", status_code=201)
def alarm_create_task(
    alarm_id: str,
    payload: CreateTaskInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("extinguish.create")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    alarm = db.get(FireEvent, alarm_id)
    if not alarm:
        raise HTTPException(404, "火情不存在")
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
