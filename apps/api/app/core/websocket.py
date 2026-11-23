from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.errors import PlatformError
from app.core.events import EVENT_STREAM, decode_stream_event, get_redis
from app.core.metrics import ws_connection_total, ws_resync_total
from app.core.scope import allowed_site_ids
from app.db.models import (
    Command,
    ManualControlSession,
    Map,
    MapVersion,
    ParkingSlot,
    Robot,
    User,
)
from app.db.session import SessionLocal
from app.modules.commands.service import build_command_payload, enqueue_safety_command

STREAM_BLOCK_MS = 1_000
# WEB-R1-MIN：已建立的 WS 连接按此间隔复核账号状态与 token_version；
# 账号停用 / 角色变更 / 重置密码后旧 WS 最迟在该间隔内被关闭（4401）。
SESSION_RECHECK_SECONDS = 2.0


class WsScope:
    """单个 WS 连接的站点可见范围（None = 不限）。"""

    def __init__(self, robot_ids: set[str] | None, slot_ids: set[str] | None) -> None:
        self.robot_ids = robot_ids
        self.slot_ids = slot_ids
        self.vehicle_ids: set[str] = set()

    def allows(self, event: dict) -> bool:
        if self.robot_ids is None:
            return True
        data = event.get("data")
        if not isinstance(data, dict):
            return True
        robot_id = data.get("robot_id")
        vehicle_id = data.get("vehicle_id")
        if robot_id:
            return robot_id in self.robot_ids or robot_id in self.vehicle_ids
        if vehicle_id:
            return vehicle_id in self.vehicle_ids
        slot_id = data.get("parking_slot_id")
        if slot_id and self.slot_ids is not None:
            return slot_id in self.slot_ids
        return True


def session_identity_valid(user_id: str, token_version: int) -> tuple[bool, WsScope | None]:
    """复核账号仍 ACTIVE、未改密码/权限；返回最新权限范围。"""
    from app.core.dependencies import load_permissions

    with SessionLocal() as db:
        user = db.get(User, user_id)
        if (
            not user
            or user.status != "ACTIVE"
            or int(user.token_version or 0) != int(token_version)
            or user.must_change_password
        ):
            return False, None
        permissions = load_permissions(db, user.id)
        if "robot.read" not in permissions and "alarm.read" not in permissions:
            return False, None
        try:
            sites = allowed_site_ids(db, permissions)
        except PlatformError:
            return False, None  # 运营站点配置错误：普通账号实时连接 fail-closed
        if sites is None:
            return True, WsScope(None, None)
        robots = db.execute(
            select(Robot.id, Robot.vehicle_id).where(Robot.site_id.in_(sites))
        ).all()
        slot_ids = set(
            db.scalars(
                select(ParkingSlot.id)
                .join(MapVersion, MapVersion.id == ParkingSlot.map_version_id)
                .join(Map, Map.id == MapVersion.map_id)
                .where(Map.site_id.in_(sites))
            ).all()
        )
        scope = WsScope({row.id for row in robots}, slot_ids)
        scope.vehicle_ids = {row.vehicle_id for row in robots}
        return True, scope


def stream_tuple(value: str) -> tuple[int, int]:
    left, right = value.split("-", 1)
    return int(left), int(right)


def release_user_leases(user_id: str, reason: str) -> None:
    redis = get_redis()
    with SessionLocal.begin() as db:
        for key in redis.scan_iter("manual:lease:*"):
            raw = redis.get(key)
            if not raw:
                continue
            lease = json.loads(raw)
            if lease.get("user_id") != user_id:
                continue
            redis.delete(key)
            session = db.scalar(
                select(ManualControlSession).where(
                    ManualControlSession.lease_id == lease["lease_id"]
                )
            )
            if session and session.state == "HELD":
                session.state = "FORCE_RELEASED"
                session.ended_at = datetime.now(UTC)
                session.end_reason = reason
            robot = db.get(Robot, lease["robot_id"])
            if robot:
                payload = build_command_payload(
                    robot=robot,
                    operator_id=user_id,
                    cmd="stop_motion",
                    params={"reason": reason},
                    ttl_ms=5_000,
                    priority=90,
                )
                command = Command(
                    command_id=payload["command_id"],
                    correlation_id=payload["correlation_id"],
                    robot_id=robot.id,
                    cmd="stop_motion",
                    priority=90,
                    payload_json=payload,
                    lifecycle_status="CREATED",
                    issued_by=user_id,
                    issued_at=datetime.fromisoformat(payload["issued_at"]),
                    expires_at=datetime.fromisoformat(payload["expires_at"]),
                )
                db.add(command)
                db.flush()
                if robot.online_state in {"STALE", "OFFLINE"}:
                    command.lifecycle_status = "PUBLISHED_UNCONFIRMED"
                    command.ack_reason = "OFFLINE_NOT_DELIVERED"
                else:
                    enqueue_safety_command(payload)
                write_audit(
                    db,
                    action="STOP_MOTION_ON_WS_DISCONNECT",
                    resource_type="COMMAND",
                    user_id=user_id,
                    robot_id=robot.id,
                    resource_id=command.command_id,
                    after={"reason": reason, "lifecycle_status": command.lifecycle_status},
                    actor_type="SYSTEM",
                )


async def monitor_socket(websocket: WebSocket, ticket: str, after: str = "0-0") -> None:
    settings = get_settings()
    origin = websocket.headers.get("origin")
    if origin and origin not in settings.origins:
        await websocket.close(code=4403)
        return
    raw_ticket = get_redis().getdel(f"ws-ticket:{ticket}")
    if not raw_ticket:
        await websocket.close(code=4401)
        return
    identity = json.loads(raw_ticket)
    token_version = int(identity.get("token_version", 0))
    valid, scope = await asyncio.to_thread(
        session_identity_valid, identity["user_id"], token_version
    )
    if not valid or scope is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    ws_connection_total.inc()
    redis = get_redis()
    first = redis.xrange(EVENT_STREAM, count=1)
    if after != "0-0" and first and stream_tuple(after) < stream_tuple(str(first[0][0])):
        ws_resync_total.inc()
        await websocket.send_json(
            {"event_type": "resync_required", "reason": "REPLAY_WINDOW_EXPIRED"}
        )
        await websocket.close(code=4009)
        ws_connection_total.dec()
        return
    cursor = after
    loop = asyncio.get_running_loop()
    last_check = loop.time()
    try:
        while True:
            if loop.time() - last_check >= SESSION_RECHECK_SECONDS:
                last_check = loop.time()
                valid, refreshed = await asyncio.to_thread(
                    session_identity_valid, identity["user_id"], token_version
                )
                if not valid or refreshed is None:
                    await websocket.send_json(
                        {"event_type": "session_revoked", "reason": "SESSION_INVALIDATED"}
                    )
                    await websocket.close(code=4401)
                    break
                scope = refreshed
            # The shared Redis client has a two-second socket timeout. Keep the
            # blocking read below that limit so an idle event stream emits a
            # heartbeat instead of looking like a broken WebSocket.
            rows = await asyncio.to_thread(
                redis.xread, {EVENT_STREAM: cursor}, 100, STREAM_BLOCK_MS
            )
            if not rows:
                await websocket.send_json({"event_type": "heartbeat", "stream_id": cursor})
                continue
            for _, events in rows:
                for stream_id, fields in events:
                    sid = str(stream_id)
                    event = decode_stream_event(sid, fields)
                    cursor = sid
                    if not scope.allows(event):
                        continue
                    await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ws_connection_total.dec()
        await asyncio.to_thread(release_user_leases, identity["user_id"], "WEBSOCKET_DISCONNECT")
