from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.events import EVENT_STREAM, decode_stream_event, get_redis
from app.core.metrics import ws_connection_total, ws_resync_total
from app.db.models import Command, ManualControlSession, Robot
from app.db.session import SessionLocal
from app.modules.commands.service import build_command_payload, enqueue_safety_command

STREAM_BLOCK_MS = 1_000


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
    try:
        while True:
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
                    await websocket.send_json(decode_stream_event(sid, fields))
                    cursor = sid
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ws_connection_total.dec()
        await asyncio.to_thread(release_user_leases, identity["user_id"], "WEBSOCKET_DISCONNECT")
