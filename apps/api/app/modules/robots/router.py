from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.dependencies import (
    AuthContext,
    CurrentAuth,
    DbSession,
    request_meta,
    require_permission,
)
from app.core.errors import PlatformError
from app.core.events import current_watermark, get_redis, queue_redis_delete
from app.core.serialization import serialize_model
from app.db.models import (
    ManualControlSession,
    Robot,
    RobotCapability,
    RobotConnectionLog,
    TelemetrySample,
    User,
)
from app.modules.commands.service import assert_robot_can_execute

router = APIRouter(prefix="/api/v1/robots", tags=["robots"])


def find_robot(db, robot_id: str) -> Robot:
    robot = db.scalar(select(Robot).where(or_(Robot.id == robot_id, Robot.vehicle_id == robot_id)))
    if not robot:
        raise HTTPException(404, "机器人不存在")
    return robot


@router.get("")
def list_robots(
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> list[dict]:
    return [
        serialize_model(row) for row in db.scalars(select(Robot).order_by(Robot.vehicle_id)).all()
    ]


@router.get("/{robot_id}")
def robot_detail(
    robot_id: str,
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> dict:
    return serialize_model(find_robot(db, robot_id))


@router.get("/{robot_id}/latest")
def robot_latest(
    robot_id: str,
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> dict:
    robot = find_robot(db, robot_id)
    raw = get_redis().get(f"robot:{robot.vehicle_id}:latest")
    latest = (
        json.loads(raw)
        if raw
        else {
            "vehicle_id": robot.vehicle_id,
            "online_state": robot.online_state,
            "battery": robot.battery,
            "mode": robot.current_mode,
            "estop_active": robot.estop_active,
            "map_version": robot.current_map_version,
        }
    )
    latest["snapshot_watermark"] = current_watermark()
    return latest


@router.get("/{robot_id}/trajectory")
def robot_trajectory(
    robot_id: str,
    db: DbSession,
    limit: int = 600,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> list[dict]:
    robot = find_robot(db, robot_id)
    rows = db.scalars(
        select(TelemetrySample)
        .where(TelemetrySample.robot_id == robot.id)
        .order_by(TelemetrySample.server_received_at.desc())
        .limit(min(limit, 5000))
    ).all()
    return [serialize_model(row) for row in reversed(rows)]


@router.get("/{robot_id}/connection-history")
def connection_history(
    robot_id: str,
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> list[dict]:
    robot = find_robot(db, robot_id)
    rows = db.scalars(
        select(RobotConnectionLog)
        .where(RobotConnectionLog.robot_id == robot.id)
        .order_by(RobotConnectionLog.server_received_at.desc())
        .limit(200)
    ).all()
    return [serialize_model(row) for row in rows]


@router.get("/{robot_id}/capabilities")
def capabilities(
    robot_id: str,
    db: DbSession,
    _: AuthContext = Depends(require_permission("robot.read")),
) -> dict:
    robot = find_robot(db, robot_id)
    row = db.get(RobotCapability, robot.id)
    if not row:
        return {"robot_id": robot.id, "protocol_version": None, "supported_commands_json": []}
    return serialize_model(row)


class LeaseRequest(BaseModel):
    control_session_id: str | None = None


def active_lease(redis, robot: Robot) -> dict | None:
    raw = redis.get(f"manual:lease:{robot.id}")
    return json.loads(raw) if raw else None


@router.post("/{robot_id}/manual-lease", status_code=201)
def acquire_lease(
    robot_id: str,
    payload: LeaseRequest,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.manual")),
) -> dict:
    robot = find_robot(db, robot_id)
    assert_robot_can_execute(db, robot, "manual_control")
    redis = get_redis()
    lease_id = str(uuid4())
    control_session_id = payload.control_session_id or str(uuid4())
    ttl = get_settings().manual_lease_ttl_seconds
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
    value = json.dumps(
        {
            "lease_id": lease_id,
            "control_session_id": control_session_id,
            "robot_id": robot.id,
            "vehicle_id": robot.vehicle_id,
            "user_id": auth.user.id,
            "expires_at": expires_at.isoformat(),
        }
    )
    if not redis.set(f"manual:lease:{robot.id}", value, ex=ttl, nx=True):
        held = active_lease(redis, robot) or {}
        holder = db.get(User, held.get("user_id")) if held.get("user_id") else None
        raise PlatformError(
            "MANUAL_LEASE_CONFLICT",
            "机器人已被其他会话控制",
            details={
                "holder": holder.display_name if holder else "未知用户",
                "expires_at": held.get("expires_at"),
            },
        )
    session = ManualControlSession(
        lease_id=lease_id,
        robot_id=robot.id,
        user_id=auth.user.id,
        acquired_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    db.add(session)
    write_audit(
        db,
        action="MANUAL_LEASE_ACQUIRE",
        resource_type="MANUAL_SESSION",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=lease_id,
        after={"control_session_id": control_session_id, "expires_at": expires_at.isoformat()},
        **request_meta(request),
    )
    try:
        db.commit()
    except Exception:
        redis.delete(f"manual:lease:{robot.id}")
        raise
    return {
        "lease_id": lease_id,
        "control_session_id": control_session_id,
        "expires_at": expires_at.isoformat(),
        "ttl_seconds": ttl,
    }


def end_lease(db, robot: Robot, lease: dict, reason: str, state: str = "RELEASED") -> None:
    session = db.scalar(
        select(ManualControlSession).where(ManualControlSession.lease_id == lease["lease_id"])
    )
    if session and session.state == "HELD":
        session.state = state
        session.ended_at = datetime.now(UTC)
        session.end_reason = reason
        session.last_seq = int(get_redis().get(f"manual:lastseq:{lease['lease_id']}") or 0)


@router.delete("/{robot_id}/manual-lease", status_code=204)
def release_lease(
    robot_id: str,
    request: Request,
    db: DbSession,
    auth: CurrentAuth,
) -> Response:
    robot = find_robot(db, robot_id)
    redis = get_redis()
    lease = active_lease(redis, robot)
    if not lease:
        return Response(status_code=204)
    if lease["user_id"] != auth.user.id:
        raise PlatformError("PERMISSION_DENIED", "只有租约持有人可以释放", status_code=403)
    queue_redis_delete(db, f"manual:lease:{robot.id}")
    end_lease(db, robot, lease, "USER_RELEASE")
    write_audit(
        db,
        action="MANUAL_LEASE_RELEASE",
        resource_type="MANUAL_SESSION",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=lease["lease_id"],
        **request_meta(request),
    )
    db.commit()
    return Response(status_code=204)


@router.post("/{robot_id}/manual-lease/force-release")
def force_release_lease(
    robot_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.control.force_release")),
) -> dict:
    robot = find_robot(db, robot_id)
    redis = get_redis()
    lease = active_lease(redis, robot)
    if not lease:
        return {"released": False, "reason": "NO_ACTIVE_LEASE"}
    queue_redis_delete(db, f"manual:lease:{robot.id}")
    end_lease(db, robot, lease, "ADMIN_FORCE_RELEASE", "FORCE_RELEASED")
    write_audit(
        db,
        action="MANUAL_LEASE_FORCE_RELEASE",
        resource_type="MANUAL_SESSION",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=lease["lease_id"],
        **request_meta(request),
    )
    db.commit()
    return {"released": True, "lease_id": lease["lease_id"]}
