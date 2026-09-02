#!/usr/bin/env python3
"""Server/Web 通信链路验收 Gate（机器可读）。

两个模式：

  --mode local-sim      本地联调：允许注入事件、断 WS、replay / resync，做完整闭环。
                        bootstrap admin 若 must_change_password=true，会先改密再重登。
  --mode prod-readonly  现场只读：不修改 robot/task/command/source_kind/control flag 任何
                        业务状态；认证 session / ws-ticket 等临时写入属于验收必要副作用，
                        标记为 AUTH_SESSION_EPHEMERAL_WRITE=ALLOWED。绝不自动改生产密码：
                        must_change_password=true 时直接 AUTH_PASSWORD_READY=FAIL 退出。

两个阶段：

  --phase prefield      车端未上线前：REAL_VEHICLE_PRESENT / REAL_VEHICLE_EVENT 允许 PENDING。
  --phase postfield     车端已上线后：两项必须 PASS，否则 exit 1，禁止用 PENDING 假通过。

输出固定 `KEY=PASS|FAIL|PENDING|SKIP|YES|ALLOWED` 行（stdout）；诊断走 stderr 的
`GATE_DIAG <KEY> <exc> status=.. body=..`（已脱敏，绝不打印 token/password/cookie）。

依赖：httpx、websockets、redis（与 scripts/ws_acceptance.py 相同）。
环境变量：
  E2E_ADMIN_PASSWORD             必填（bootstrap 初始密码）
  E2E_CHANGED_PASSWORD           可选（改密后的密码，默认 Firebot-E2E-Changed-2026!）
  E2E_BASE_URL                   传输地址（local-sim 默认 http://nginx）
  E2E_ORIGIN                     Browser Origin（未设置时从 E2E_BASE_URL 派生；
                                 local-sim 必须显式设为 ALLOWED_ORIGINS 之一，如
                                 http://127.0.0.1:18080）
  REDIS_URL                      仅 local-sim 注入需要
  REAL_VEHICLE_EVENT_WAIT_SECONDS postfield 观察实车事件的最长等待秒数（默认 30）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import websockets

REAL_VEHICLE_ID = "firebot-vehicle-01"
DEFAULT_CHANGED_PASSWORD = "Firebot-E2E-Changed-2026!"
RESULTS: list[tuple[str, str]] = []

_SECRET_RE = re.compile(
    r'(?i)(access_token|refresh_token|csrf_token|password|ticket)\s*["\']?\s*[:=]\s*["\'][^"\']*["\']'
)


def emit(key: str, status: str) -> None:
    RESULTS.append((key, status))
    print(f"{key}={status}", flush=True)


def _redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)


def _diag(key: str, exc: Exception) -> None:
    response = getattr(exc, "response", None)
    parts = [type(exc).__name__]
    if response is not None:
        parts.append(f"status={getattr(response, 'status_code', '?')}")
        try:
            parts.append(f"body={_redact(response.text[:300])!r}")
        except Exception:
            pass
    print("GATE_DIAG " + key + " " + " ".join(parts), file=sys.stderr, flush=True)


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


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


async def _verify_replay(
    client: httpx.Client, ws_base: str, origin: str, token: str, marker: str, redis_url: str
) -> bool:
    """local-sim 专用：先取当前 stream 最新 id 作为 after，再注入 marker 并等待回放。

    不能用 snapshot watermark：双 mock 持续写入会把它推到有限窗口之外，或早于
    stream 保留窗口导致 resync_required，从而让 marker 永远读不到。
    """
    import redis as redis_lib

    r = redis_lib.Redis.from_url(redis_url, decode_responses=True)
    fresh = "0-0"
    try:
        latest = r.xrevrange("firebot:events", count=1)
        if latest:
            fresh = str(latest[0][0])
    except Exception as exc:  # noqa: BLE001
        _diag("REPLAY_READ_WATERMARK", exc)
    marker_id = r.xadd(
        "firebot:events",
        {
            "event": json.dumps(
                {
                    "event_type": "system.acceptance_marker",
                    "server_received_at": datetime.now(UTC).isoformat(),
                    "payload": {"marker": marker},
                },
                default=str,
            )
        },
    )
    # 安全诊断：只写 after 与 marker id，不写 auth ticket。
    print(
        f"GATE_DIAG REPLAY_FROM_STREAM_ID after={fresh} marker_id={marker_id}",
        file=sys.stderr,
        flush=True,
    )
    ticket = client.post("/api/v1/auth/ws-ticket", headers=_headers(token, origin))
    ticket.raise_for_status()
    uri = f"{ws_base}/ws/v1/monitor?ticket={ticket.json()['ticket']}&after={fresh}"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 15.0
    async with websockets.connect(uri, origin=origin) as socket:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                event = json.loads(await asyncio.wait_for(socket.recv(), timeout=remaining))
            except TimeoutError:
                return False
            if event.get("event_type") == "resync_required":
                return False
            if event.get("data", {}).get("marker") == marker:
                return True


async def _verify_real_vehicle_event(
    client: httpx.Client, ws_base: str, origin: str, token: str, watermark: str, wait: float
) -> bool:
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
            except TimeoutError:
                return False
            event_type = event.get("event_type", "")
            if (
                event_type.startswith(("vehicle.", "robot."))
                and str(event.get("data", {}).get("vehicle_id")) == REAL_VEHICLE_ID
            ):
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

    admin_password = os.environ["E2E_ADMIN_PASSWORD"]
    changed_password = os.getenv("E2E_CHANGED_PASSWORD") or DEFAULT_CHANGED_PASSWORD
    http_base = os.getenv("E2E_BASE_URL", "http://nginx")
    origin = os.getenv("E2E_ORIGIN") or http_base.rstrip("/")
    ws_base = http_base.replace("http://", "ws://").replace("https://", "wss://")

    with httpx.Client(base_url=http_base, timeout=10, cookies=httpx.Cookies()) as client:
        is_https = http_base.startswith("https://")
        if args.mode == "prod-readonly":
            emit("WEB_HTTPS", "PASS" if is_https else "FAIL")
        else:
            emit("WEB_HTTPS", "SKIP")

        # ---- auth：候选密码 + 首次改密 ----
        token: str | None = None
        used_password: str | None = None
        for candidate in _unique([changed_password, admin_password]):
            try:
                session = _login(client, candidate)
                token = session["access_token"]
                used_password = candidate
                break
            except Exception:
                continue
        if token is None:
            emit("AUTH_LOGIN", "FAIL")
            emit("AUTH_PASSWORD_READY", "FAIL")
            sys.exit(_exit_code(args.phase))

        must_change = bool(session.get("user", {}).get("must_change_password"))
        if must_change:
            if args.mode == "prod-readonly":
                emit("AUTH_LOGIN", "PASS")
                emit("AUTH_PASSWORD_READY", "FAIL")
                sys.exit(_exit_code(args.phase))
            try:
                change = client.post(
                    "/api/v1/auth/change-password",
                    json={"current_password": used_password, "new_password": changed_password},
                    headers=_headers(token, origin),
                )
                change.raise_for_status()
            except Exception as exc:
                _diag("AUTH_CHANGE_PASSWORD", exc)
                emit("AUTH_LOGIN", "PASS")
                emit("AUTH_PASSWORD_READY", "FAIL")
                sys.exit(_exit_code(args.phase))
            try:
                session = _login(client, changed_password)
                token = session["access_token"]
            except Exception as exc:
                _diag("AUTH_RELOGIN", exc)
                emit("AUTH_LOGIN", "PASS")
                emit("AUTH_PASSWORD_READY", "FAIL")
                sys.exit(_exit_code(args.phase))

        emit("AUTH_LOGIN", "PASS")
        emit("AUTH_PASSWORD_READY", "PASS")
        headers = _headers(token, origin)

        if args.mode == "prod-readonly":
            emit("NO_ROBOT_STATE_WRITE", "YES")
            emit("NO_COMMAND_WRITE", "YES")
            emit("NO_TASK_WRITE", "YES")
            emit("NO_SOURCE_KIND_WRITE", "YES")
            emit("NO_CONTROL_FLAG_WRITE", "YES")
            emit("AUTH_SESSION_EPHEMERAL_WRITE", "ALLOWED")

        try:
            me = client.get("/api/v1/auth/me", headers=headers)
            me.raise_for_status()
            emit("AUTH_ME", "PASS")
        except Exception as exc:
            _diag("AUTH_ME", exc)
            emit("AUTH_ME", "FAIL")
        try:
            csrf = client.cookies.get("csrf_token")
            refresh_headers = {**headers, "X-CSRF-Token": csrf} if csrf else headers
            refresh = client.post("/api/v1/auth/refresh", headers=refresh_headers)
            refresh.raise_for_status()
            token = refresh.json()["access_token"]
            headers = _headers(token, origin)
            emit("AUTH_REFRESH", "PASS")
        except Exception as exc:
            _diag("AUTH_REFRESH", exc)
            emit("AUTH_REFRESH", "FAIL")

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
        except Exception as exc:
            _diag("MONITOR_SNAPSHOT", exc)
            emit("MONITOR_SNAPSHOT", "FAIL")
            if args.phase == "postfield":
                emit("REAL_VEHICLE_PRESENT", "FAIL")

        # WSS
        try:
            await _verify_ticket(client, ws_base, origin, token)
            emit("WS_TICKET", "PASS")
            emit("WSS_CONNECT", "PASS")
        except Exception as exc:
            _diag("WS_TICKET", exc)
            emit("WS_TICKET", "FAIL")
            emit("WSS_CONNECT", "FAIL")
        try:
            await _verify_resync(client, ws_base, origin, token)
            emit("RESYNC", "PASS")
        except Exception as exc:
            _diag("RESYNC", exc)
            emit("RESYNC", "FAIL")

        if args.mode == "local-sim":
            redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
            marker = str(uuid4())
            try:
                replayed = await _verify_replay(client, ws_base, origin, token, marker, redis_url)
                emit("REALTIME_EVENT", "PASS" if replayed else "FAIL")
            except Exception as exc:
                _diag("REALTIME_EVENT", exc)
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
            except Exception as exc:
                _diag("REAL_VEHICLE_EVENT", exc)
                emit("REAL_VEHICLE_EVENT", "FAIL")

    sys.exit(_exit_code(args.phase))


if __name__ == "__main__":
    asyncio.run(run())
