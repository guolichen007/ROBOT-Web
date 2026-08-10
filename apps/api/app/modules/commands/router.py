from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.core.audit import write_audit
from app.core.dependencies import (
    AuthContext,
    CurrentAuth,
    DbSession,
    request_meta,
    require_permission,
)
from app.core.events import append_event, get_redis
from app.core.idempotency import lookup, store
from app.core.serialization import serialize_model
from app.db.models import Command, Robot
from app.modules.commands.service import (
    assert_robot_can_execute,
    build_command_payload,
    create_durable_command,
    enqueue_safety_command,
)
from app.modules.robots.router import active_lease, end_lease, find_robot

router = APIRouter(prefix="/api/v1", tags=["commands"])


class ManualCommand(BaseModel):
    lease_id: str
    control_session_id: str
    seq: int = Field(ge=0)
    linear: float = Field(ge=-0.4, le=0.4)
    angular: float = Field(ge=-1.0, le=1.0)


class CommandRequest(BaseModel):
    params: dict = Field(default_factory=dict)


@router.post("/robots/{robot_id}/commands/manual")
def manual(
    robot_id: str,
    payload: ManualCommand,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.manual")),
) -> dict:
    robot = find_robot(db, robot_id)
    assert_robot_can_execute(db, robot, "manual_control")
    redis = get_redis()
    lease = active_lease(redis, robot)
    if not lease or lease["lease_id"] != payload.lease_id or lease["user_id"] != auth.user.id:
        raise HTTPException(409, "manual lease 无效或已过期")
    if lease["control_session_id"] != payload.control_session_id:
        raise HTTPException(409, "manual control session 不匹配")
    last_seq = int(redis.get(f"manual:lastseq:{payload.lease_id}") or -1)
    if payload.seq <= last_seq:
        return {"accepted": False, "reason": "DUPLICATE_OR_OUT_OF_ORDER", "last_seq": last_seq}
    command = build_command_payload(
        robot=robot,
        operator_id=auth.user.id,
        cmd="manual_control",
        params={"linear": payload.linear, "angular": payload.angular},
        ttl_ms=500,
        priority=80,
        lease_id=payload.lease_id,
        control_session_id=payload.control_session_id,
        seq=payload.seq,
    )
    redis.setex(f"manual:lastseq:{payload.lease_id}", 30, payload.seq)
    redis.expire(f"manual:lease:{robot.id}", 5)
    redis.publish("firebot:manual_commands", json.dumps(command, ensure_ascii=False))
    return {"accepted": True, "expires_at": command["expires_at"], "seq": payload.seq}


def safety_command(
    *,
    robot: Robot,
    cmd: str,
    auth: AuthContext,
    db,
    request: Request,
    ttl_ms: int,
    priority: int,
    idempotency_key: str,
    endpoint: str,
) -> dict:
    request_body: dict = {}
    cached = lookup(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=request_body,
    )
    if cached:
        return cached.response_json
    command_payload = build_command_payload(
        robot=robot,
        operator_id=auth.user.id,
        cmd=cmd,
        params={},
        ttl_ms=ttl_ms,
        priority=priority,
    )
    row = Command(
        command_id=command_payload["command_id"],
        correlation_id=command_payload["correlation_id"],
        robot_id=robot.id,
        cmd=cmd,
        priority=priority,
        payload_json=command_payload,
        lifecycle_status="CREATED",
        issued_by=auth.user.id,
        issued_at=datetime.fromisoformat(command_payload["issued_at"]),
        expires_at=datetime.fromisoformat(command_payload["expires_at"]),
    )
    db.add(row)
    if robot.online_state in {"STALE", "OFFLINE"}:
        row.lifecycle_status = "PUBLISHED_UNCONFIRMED"
        row.ack_reason = "OFFLINE_NOT_DELIVERED"
    else:
        enqueue_safety_command(command_payload)
    if cmd == "emergency_stop":
        lease = active_lease(get_redis(), robot)
        if lease:
            get_redis().delete(f"manual:lease:{robot.id}")
            end_lease(db, robot, lease, "EMERGENCY_STOP", "FORCE_RELEASED")
    write_audit(
        db,
        action=cmd.upper(),
        resource_type="COMMAND",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=row.command_id,
        after={"lifecycle_status": row.lifecycle_status},
        **request_meta(request),
    )
    db.flush()
    response = serialize_model(row)
    store(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=request_body,
        response=response,
        status_code=202,
    )
    db.commit()
    append_event("command.updated", serialize_model(row))
    return response


@router.post("/robots/{robot_id}/commands/stop-motion", status_code=202)
def stop_motion(
    robot_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.stop")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    robot = find_robot(db, robot_id)
    return safety_command(
        robot=robot,
        cmd="stop_motion",
        auth=auth,
        db=db,
        request=request,
        ttl_ms=3000,
        priority=95,
        idempotency_key=idempotency_key,
        endpoint=f"/robots/{robot.id}/commands/stop-motion",
    )


@router.post("/robots/{robot_id}/commands/emergency-stop", status_code=202)
def emergency_stop(
    robot_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.estop")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    robot = find_robot(db, robot_id)
    return safety_command(
        robot=robot,
        cmd="emergency_stop",
        auth=auth,
        db=db,
        request=request,
        ttl_ms=5000,
        priority=100,
        idempotency_key=idempotency_key,
        endpoint=f"/robots/{robot.id}/commands/emergency-stop",
    )


@router.post("/robots/{robot_id}/commands/reset-estop", status_code=202)
def reset_estop(
    robot_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.reset_estop")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    robot = find_robot(db, robot_id)
    if robot.online_state in {"STALE", "OFFLINE"}:
        raise HTTPException(409, "离线机器人不能复位软件急停")
    return safety_command(
        robot=robot,
        cmd="reset_estop",
        auth=auth,
        db=db,
        request=request,
        ttl_ms=5000,
        priority=90,
        idempotency_key=idempotency_key,
        endpoint=f"/robots/{robot.id}/commands/reset-estop",
    )


@router.post("/robots/{robot_id}/commands/return-dock", status_code=202)
def return_dock(
    robot_id: str,
    payload: CommandRequest,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.task")),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    robot = find_robot(db, robot_id)
    endpoint = f"/robots/{robot.id}/commands/return-dock"
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
    command = create_durable_command(
        db,
        robot=robot,
        operator_id=auth.user.id,
        cmd="return_dock",
        params=payload.params,
        priority=60,
    )
    db.flush()
    response = serialize_model(command)
    store(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=body,
        response=response,
        status_code=202,
    )
    write_audit(
        db,
        action="RETURN_DOCK",
        resource_type="COMMAND",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=command.command_id,
        **request_meta(request),
    )
    db.commit()
    return response


@router.get("/commands")
def list_commands(db: DbSession, auth: CurrentAuth, limit: int = 200) -> list[dict]:
    rows = db.scalars(
        select(Command).order_by(Command.issued_at.desc()).limit(min(limit, 1000))
    ).all()
    return [serialize_model(row) for row in rows]


@router.get("/commands/{command_id}")
def get_command(command_id: str, db: DbSession, auth: CurrentAuth) -> dict:
    row = db.scalar(
        select(Command).where(or_(Command.command_id == command_id, Command.id == command_id))
    )
    if not row:
        raise HTTPException(404, "命令不存在")
    return serialize_model(row)
