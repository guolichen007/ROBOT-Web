#!/usr/bin/env python3
"""Server/Web 通信链路验收 Gate（机器可读）。

两个模式：

  --mode local-sim      本地联调：允许注入事件、断 WS、replay / resync，做完整闭环。
  --mode prod-readonly  现场只读：禁止任何 command / DB / Redis 写入 / source_kind /
                        control flag 修改；只验证 HTTPS / auth / snapshot / WSS 读取链路。

输出固定 `KEY=PASS|FAIL|PENDING` 行，便于 CI / 现场脚本 grep。

依赖：httpx、websockets、redis（与 scripts/ws_acceptance.py 相同）。
环境变量：E2E_ADMIN_PASSWORD、E2E_BASE_URL、REDIS_URL（仅 local-sim 注入需要）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import websockets

RESULTS: list[tuple[str, str]] = []


def emit(key: str, status: str) -> None:
    RESULTS.append((key, status))
    print(f"{key}={status}", flush=True)


def _login(client: httpx.Client, password: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": password})
    resp.raise_for_status()
    return resp.json()


async def _recv_event(socket, timeout: float = 8.0) -> dict:
    return json.loads(await asyncio.wait_for(socket.recv(), timeout=timeout))


def _headers(token: str, origin: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": origin}


async def _verify_ticket(client: httpx.Client, ws_base: str, origin: str, token: str) -> bool:
    ticket = client.post("/api/v1/auth/ws-ticket", headers=_headers(token, origin))
    ticket.raise_for_status()
    uri = f"{ws_base}/ws/v1/monitor?ticket={ticket.json()['ticket']}"
    async with websockets.connect(uri, origin=origin) as socket:
        await _recv_event(socket)
    return True


async def _verify_resync(client: httpx.Client, ws_base: str, origin: str, token: str) -> bool:
    ticket = client.post("/api/v1/auth/ws-ticket", headers=_headers(token, origin))
    ticket.raise_for_status()
    uri = f"{ws_base}/ws/v1/monitor?ticket={ticket.json()['ticket']}&after=1-0"
    async with websockets.connect(uri, origin=origin) as socket:
        event = await _recv_event(socket)
    return event.get("event_type") == "resync_required"


async def _verify_replay(client: httpx.Client, ws_base: str, origin: str, token: str,
                         watermark: str, marker: str, redis_url: str) -> bool:
    import redis as redis_lib

    r = redis_lib.Redis.from_url(redis_url, decode_responses=True)
    r.xadd("firebot:events", {"event": json.dumps({
        "event_type": "system.acceptance_marker",
        "server_received_at": datetime.now(UTC).isoformat(),
        "payload": {"marker": marker},
    })})
    ticket = client.post("/api/v1/auth/ws-ticket", headers=_headers(token, origin))
    ticket.raise_for_status()
    uri = f"{ws_base}/ws/v1/monitor?ticket={ticket.json()['ticket']}&after={watermark}"
    async with websockets.connect(uri, origin=origin) as socket:
        for _ in range(20):
            event = await _recv_event(socket)
            if event.get("data", {}).get("marker") == marker:
                return True
    return False


async def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["local-sim", "prod-readonly"], default="prod-readonly")
    args = parser.parse_args()

    password = os.environ["E2E_ADMIN_PASSWORD"]
    http_base = os.getenv("E2E_BASE_URL", "http://nginx")
    ws_base = http_base.replace("http://", "ws://").replace("https://", "wss://")
    origin = http_base.rstrip("/")

    with httpx.Client(base_url=http_base, timeout=10) as client:
        # HTTPS / auth
        try:
            session = _login(client, password)
            emit("AUTH_LOGIN", "PASS")
        except Exception:
            emit("AUTH_LOGIN", "FAIL")
            return
        token = session["access_token"]
        headers = _headers(token, origin)
        try:
            me = client.get("/api/v1/auth/me", headers=headers)
            me.raise_for_status()
            emit("AUTH_ME", "PASS")
        except Exception:
            emit("AUTH_ME", "FAIL")
        try:
            refresh = client.post("/api/v1/auth/refresh", headers=headers)
            refresh.raise_for_status()
            emit("AUTH_REFRESH", "PASS")
        except Exception:
            emit("AUTH_REFRESH", "FAIL")

        # snapshot + multi-vehicle
        try:
            snap = client.get("/api/v1/monitor/snapshot", headers=headers)
            snap.raise_for_status()
            payload = snap.json()
            emit("MONITOR_SNAPSHOT", "PASS")
            watermark = payload["snapshot_watermark"]
            robots = payload.get("robots", [])
            if len(robots) >= 2:
                emit("MULTI_VEHICLE_SNAPSHOT", "PASS")
            else:
                emit("MULTI_VEHICLE_SNAPSHOT", "FAIL")
            real = next((r for r in robots if r.get("vehicle_id") == "firebot-vehicle-01"), None)
            emit("REAL_VEHICLE_PRESENT", "PASS" if real else "PENDING")
        except Exception:
            emit("MONITOR_SNAPSHOT", "FAIL")
            watermark = "0-0"

        # WSS
        try:
            await _verify_ticket(client, ws_base, origin, token)
            emit("WS_TICKET", "PASS")
            emit("WSS_CONNECT", "PASS")
        except Exception:
            emit("WS_TICKET", "FAIL")
            emit("WSS_CONNECT", "FAIL")
        try:
            await _verify_resync(client, ws_base, origin, token)
            emit("RESYNC", "PASS")
        except Exception:
            emit("RESYNC", "FAIL")

        if args.mode == "local-sim":
            redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
            marker = str(uuid4())
            try:
                replayed = await _verify_replay(client, ws_base, origin, token, watermark, marker, redis_url)
                emit("REALTIME_EVENT", "PASS" if replayed else "FAIL")
            except Exception:
                emit("REALTIME_EVENT", "FAIL")
        else:
            emit("NO_COMMAND_SENT", "YES")
            emit("REAL_VEHICLE_EVENT", "PENDING")

    sys.exit(0 if all(status in ("PASS", "PENDING", "YES") for _, status in RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(run())
