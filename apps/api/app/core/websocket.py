from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.config import get_settings
from app.core.events import EVENT_STREAM, decode_stream_event, get_redis
from app.core.metrics import ws_connection_total, ws_resync_total
from app.db.models import ManualControlSession
from app.db.session import SessionLocal


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
            rows = await asyncio.to_thread(redis.xread, {EVENT_STREAM: cursor}, 100, 5000)
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
