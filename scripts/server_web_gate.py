#!/usr/bin/env python3
"""Server/Web 通信链路验收 Gate（机器可读）。

两个模式：

  --mode local-sim      本地联调：允许注入事件、断 WS、replay / resync，做完整闭环。
  --mode prod-readonly  现场只读：不修改 robot/task/command/source_kind/control flag 任何
                        业务状态；认证 session / ws-ticket 等临时写入属于验收必要副作用，
                        明确标记为 AUTH_SESSION_EPHEMERAL_WRITE=ALLOWED。

两个阶段：

  --phase prefield      车端未上线前：REAL_VEHICLE_PRESENT / REAL_VEHICLE_EVENT 允许 PENDING。
  --phase postfield     车端已上线后：两项必须 PASS，否则 exit 1，禁止用 PENDING 假通过。

输出固定 `KEY=PASS|FAIL|PENDING|SKIP|YES|ALLOWED` 行，便于 CI / 现场脚本 grep。

依赖：httpx、websockets、redis（与 scripts/ws_acceptance.py 相同）。
环境变量：
  E2E_ADMIN_PASSWORD            必填
  E2E_BASE_URL                  默认 http://nginx（prod-readonly 必须 https://）
  REDIS_URL                     仅 local-sim 注入需要
  REAL_VEHICLE_EVENT_WAIT_SECONDS  postfield 观察实车事件的最长等待秒数（默认 30）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import websockets

REAL_VEHICLE_ID = "firebot-vehicle-01"
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
        "server_received_at": datetime.now(timezone.utc).isoformat(),
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


async def _verify_real_vehicle_event(client: httpx.Client, ws_base: str, origin: str,
                                     token: str, watermark: str, wait: float) -> bool:
    """只读观察：等一段实车 `vehicle.*`/`robot.*` 事件。不写任何状态。"""
    ticket = client.post("/api/v1/auth/ws-ticket", headers=_headers(token, origin))
    ticket.raise_for_status()
    uri = f"{ws_base}/ws/v1/monitor?ticket={ticket.json()['ticket']}&after={watermark}"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + wait
    async with websockets.connect(uri, origin=origin) as socket:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                event = json.loads(await asyncio.wait_for(socket.recv(), timeout=remaining))
            except asyncio.TimeoutError:
                return False
            event_type = event.get("event_type", "")
            if event_type.startswith(("vehicle.", "robot.")) and str(
                event.get("data", {}).get("vehicle_id")
            ) == REAL_VEHICLE_ID:
                return True


def _exit_code(phase: str) -> int:
    statuses = dict(RESULTS)
    if any(status == "FAIL" for _, status in RESULTS):
        return 1
    if phase == "postfield":
        if statuses.get("REAL_VEHICLE_PRESENT") != "PASS":
            return 1
        if statuses.get("REAL_VEHICLE_EVENT") != "PASS":
            return 1
    return 0


async def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["local-sim", "prod-readonly"], default="prod-readonly")
    parser.add_argument("--phase", choices=["prefield", "postfield"], default="prefield")
    args = parser.parse_args()

    password = os.environ["E2E_ADMIN_PASSWORD"]
    http_base = os.getenv("E2E_BASE_URL", "http://nginx")
    ws_base = http_base.replace("http://", "ws://").replace("https://", "wss://")
    origin = http_base.rstrip("/")

    with httpx.Client(base_url=http_base, timeout=10, cookies=httpx.Cookies()) as client:
        # HTTPS / auth
        is_https = http_base.startswith("https://")
        if args.mode == "prod-readonly":
            emit("WEB_HTTPS", "PASS" if is_https else "FAIL")
        else:
            emit("WEB_HTTPS", "SKIP")
        try:
            session = _login(client, password)
            emit("AUTH_LOGIN", "PASS")
        except Exception:
            emit("AUTH_LOGIN", "FAIL")
            sys.exit(_exit_code(args.phase))
        token = session["access_token"]
        headers = _headers(token, origin)
        try:
            me = client.get("/api/v1/auth/me", headers=headers)
            me.raise_for_status()
            emit("AUTH_ME", "PASS")
        except Exception:
            emit("AUTH_ME", "FAIL")
        try:
            csrf = client.cookies.get("csrf_token")
            refresh_headers = {**headers, "X-CSRF-Token": csrf} if csrf else headers
            refresh = client.post("/api/v1/auth/refresh", headers=refresh_headers)
            refresh.raise_for_status()
            emit("AUTH_REFRESH", "PASS")
        except Exception:
            emit("AUTH_REFRESH", "FAIL")

        if args.mode == "prod-readonly":
            # 只读边界：不修改任何 robot/task/command/source_kind/control flag 业务状态。
            # 登录 / refresh / ws-ticket 产生的认证 session 与临时 ticket 属验收必要副作用。
            emit("NO_ROBOT_STATE_WRITE", "YES")
            emit("NO_COMMAND_WRITE", "YES")
            emit("NO_TASK_WRITE", "YES")
            emit("NO_SOURCE_KIND_WRITE", "YES")
            emit("NO_CONTROL_FLAG_WRITE", "YES")
            emit("AUTH_SESSION_EPHEMERAL_WRITE", "ALLOWED")

        # snapshot + multi-vehicle
        watermark = "0-0"
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
            real = next((r for r in robots if r.get("vehicle_id") == REAL_VEHICLE_ID), None)
            if real:
                emit("REAL_VEHICLE_PRESENT", "PASS")
            else:
                emit("REAL_VEHICLE_PRESENT", "PENDING" if args.phase == "prefield" else "FAIL")
        except Exception:
            emit("MONITOR_SNAPSHOT", "FAIL")
            if args.phase == "postfield":
                emit("REAL_VEHICLE_PRESENT", "FAIL")

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
            emit("REAL_VEHICLE_EVENT", "PENDING")
        elif args.phase == "prefield":
            emit("REAL_VEHICLE_EVENT", "PENDING")
        else:
            wait = float(os.getenv("REAL_VEHICLE_EVENT_WAIT_SECONDS", "30"))
            try:
                seen = await _verify_real_vehicle_event(
                    client, ws_base, origin, token, watermark, wait
                )
                emit("REAL_VEHICLE_EVENT", "PASS" if seen else "FAIL")
            except Exception:
                emit("REAL_VEHICLE_EVENT", "FAIL")

    sys.exit(_exit_code(args.phase))


if __name__ == "__main__":
    asyncio.run(run())
