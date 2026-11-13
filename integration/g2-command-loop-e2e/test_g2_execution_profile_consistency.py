"""G2-R3：执行档在 API 初始快照与专属 Ingress 实时增量之间保持一致（真实进程）。

r001-ingress 每条上行都会执行 robot_readiness 并把 vehicle_agent_execution 写入 Redis latest 与
`vehicle.agent_state` 事件；若 Ingress 未拿到与 API 相同的 G2 键，前端会在首个增量后把执行档覆盖
为“未配置”，诊断面板闪现后消失。本文件直接读取真实事件流（Redis firebot:events）断言。
"""

from __future__ import annotations

import json
import time

import pytest
from conftest import VEHICLE, start_agent, stop, wait_for

PROFILE_NAME = "ros-software-test"


@pytest.fixture(scope="module")
def api(g2):
    from app.main import app
    from conftest import ensure_isolated_test_admin
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        username, password = ensure_isolated_test_admin()
        response = client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        yield client


def agent_running(g2) -> bool:
    agent = g2.get("agent")
    return agent is not None and agent.poll() is None


def events_since(g2, last_id: str, vehicle: str) -> tuple[list[dict], str]:
    ms, seq = last_id.split("-")
    # 排他起点（兼容 Redis < 6.2：不使用 "(" 语法）
    rows = g2["redis"].xrange("firebot:events", min=f"{ms}-{int(seq) + 1}", max="+")
    out = []
    for stream_id, fields in rows:
        last_id = stream_id
        event = json.loads(fields["event"])
        if event["event_type"].startswith("vehicle.") and (
            event["payload"].get("vehicle_id") == vehicle
        ):
            out.append(event)
    return out, last_id


def collect(g2, vehicle: str, count: int, timeout: float, start: str | None = None) -> list[dict]:
    last = start or g2["redis"].xinfo_stream("firebot:events")["last-generated-id"]
    seen: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(seen) < count:
        batch, last = events_since(g2, last, vehicle)
        seen.extend(e for e in batch if "vehicle_agent_execution" in e["payload"])
        time.sleep(0.3)
    return seen


def profile_of(payload: dict) -> str | None:
    return (payload.get("vehicle_agent_execution") or {}).get("profile")


def snapshot_robot(api, vehicle: str) -> dict:
    robots = api.get("/api/v1/monitor/snapshot").json()["robots"]
    return next(r for r in robots if r["vehicle_id"] == vehicle)


def test_snapshot_and_live_events_keep_execution_profile(g2, api):
    if not agent_running(g2):
        start_agent(g2)
    assert wait_for(lambda: snapshot_robot(api, VEHICLE)["online_state"] == "ONLINE", 30)
    first = snapshot_robot(api, VEHICLE)
    assert profile_of(first) == PROFILE_NAME, first.get("vehicle_agent_execution")
    events = collect(g2, VEHICLE, count=6, timeout=25)
    assert len(events) >= 6, f"too few live vehicle events: {len(events)}"
    profiles = [profile_of(e["payload"]) for e in events]
    g2["evidence"]["live_event_profiles"] = profiles
    assert set(profiles) == {PROFILE_NAME}, profiles
    # Ingress 写入的 Redis latest 与 API 快照同档
    latest = json.loads(g2["redis"].get(f"robot:{VEHICLE}:latest"))
    assert profile_of(latest) == PROFILE_NAME
    assert (
        snapshot_robot(api, VEHICLE)["vehicle_agent_execution"] == first["vehicle_agent_execution"]
        or profile_of(snapshot_robot(api, VEHICLE)) == PROFILE_NAME
    )


def test_offline_and_reconnect_keep_profile_and_disable_buttons_when_offline(g2, api):
    if not agent_running(g2):
        start_agent(g2)
        assert wait_for(lambda: snapshot_robot(api, VEHICLE)["online_state"] == "ONLINE", 30)
    start_id = g2["redis"].xinfo_stream("firebot:events")["last-generated-id"]
    stop(g2["agent"])  # 正常离线（retained offline + LWT 语义）
    assert wait_for(lambda: snapshot_robot(api, VEHICLE)["online_state"] == "OFFLINE", 25)
    offline_events = collect(g2, VEHICLE, count=1, timeout=10, start=start_id)
    assert offline_events, "no offline vehicle event"
    for event in offline_events:
        assert profile_of(event["payload"]) == PROFILE_NAME
    offline = snapshot_robot(api, VEHICLE)
    assert profile_of(offline) == PROFILE_NAME
    # 离线：执行档保留，但所有命令 ready=false（按钮 disabled），真实控制就绪全部 false
    assert not any(c["ready"] for c in offline["vehicle_agent_execution"]["commands"].values())
    assert not any(offline["vehicle_agent_ready"].values())
    start_agent(g2)
    assert wait_for(lambda: snapshot_robot(api, VEHICLE)["online_state"] == "ONLINE", 30)
    reconnect_events = collect(g2, VEHICLE, count=4, timeout=20)
    assert {profile_of(e["payload"]) for e in reconnect_events} == {PROFILE_NAME}
    online = snapshot_robot(api, VEHICLE)
    assert online["vehicle_agent_execution"]["commands"]["ping"]["ready"] is True


def test_downlink_disabled_means_disabled_not_real_control(g2, api):
    """下行关闭（API 侧）：执行档仍显示，但全部命令 ready=false，真实控制就绪不被误判为可用。"""
    from app.core.config import get_settings

    settings = get_settings()
    previous = settings.vehicle_agent_v1_downlink_enabled
    settings.vehicle_agent_v1_downlink_enabled = False
    try:
        robot = snapshot_robot(api, VEHICLE)
        commands = robot["vehicle_agent_execution"]["commands"]
        assert profile_of(robot) == PROFILE_NAME
        assert commands and not any(c["ready"] for c in commands.values())
        assert not any(robot["vehicle_agent_ready"].values())
        assert robot["vehicle_agent_execution"]["hardware"] == "NO_HARDWARE"
    finally:
        settings.vehicle_agent_v1_downlink_enabled = previous


def test_r002_unaffected(g2, api):
    r002 = snapshot_robot(api, "R002")
    assert (r002.get("vehicle_agent_execution") or {}).get("profile") is None
    assert not (r002.get("vehicle_agent_execution") or {}).get("commands")
    print("G2R3_EVIDENCE " + json.dumps(g2["evidence"].get("live_event_profiles")))
