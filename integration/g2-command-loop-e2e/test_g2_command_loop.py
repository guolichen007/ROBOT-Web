"""G2 闭环真实组件测试（顺序执行：同一 session 栈，测试间共享车端进程）。"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import PROFILE, VEHICLE, start_agent, stop, wait_for

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def api(g2):
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        from app.core.config import get_settings

        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": get_settings().effective_admin_password},
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        yield client


def robot_row():
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE))
        db.expunge(robot)
        return robot


def command_row(command_id: str):
    from app.db.models import Command
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        row = db.scalar(select(Command).where(Command.command_id == command_id))
        db.expunge(row)
        return row


def post(api, action: str, params: dict | None = None, key: str | None = None):
    return api.post(
        f"/api/v1/robots/{VEHICLE}/agent/commands/{action}",
        json={"params": params or {}},
        headers={"Idempotency-Key": key or str(uuid4())},
    )


def wait_terminal(command_id: str, statuses: set[str], timeout: float = 25.0):
    row = wait_for(
        lambda: (r := command_row(command_id)) and r.lifecycle_status in statuses and r,
        timeout,
    )
    assert row, f"{command_id} did not reach {statuses}: {command_row(command_id).lifecycle_status}"
    return row


def wait_online_session(previous: str | None = None) -> str:
    def ready():
        robot = robot_row()
        state = robot.agent_state_json or {}
        return (
            robot.online_state == "ONLINE"
            and state.get("boot_source") == "vehicle"
            and robot.boot_id
            and robot.boot_id != previous
            and state.get("status_received_at")
            and robot.boot_id
        )

    boot = wait_for(ready, 30)
    assert boot, f"vehicle session not adopted: {robot_row().agent_state_json}"
    return boot


def exec_log(g2) -> list[dict]:
    path = g2["workdir"] / "agent-run" / "exec-log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def acks_on_b(g2, msg_id: str) -> list[dict]:
    return [
        json.loads(payload)
        for topic, payload, _ in g2["sniff_b"].messages
        if topic == f"v1/robot/{VEHICLE}/ack" and json.loads(payload).get("msg_id") == msg_id
    ]


R002_SNAPSHOT: dict = {}


def r002_state():
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        return {
            "agent_protocol": robot.agent_protocol,
            "boot_id": robot.boot_id,
            "online_state": robot.online_state,
            "enabled": robot.enabled,
            "agent_state_json": robot.agent_state_json,
            "updated_at": robot.updated_at,
        }


# ---------------------------------------------------------------- G2-A 诊断闭环
def test_01_agent_session_adopted_and_gates(g2, api):
    R002_SNAPSHOT.update(r002_state())
    # 车端未连接：OFFLINE → 诊断被门禁拒绝，不产生命令（设备离线不产生虚假 ACK）
    offline = post(api, "ping")
    assert offline.status_code >= 400 and offline.json()["error"]["code"] == "ROBOT_OFFLINE"
    start_agent(g2)
    boot = wait_online_session()
    robot = robot_row()
    # 生产同款基线未被改写：enabled=false / estop NULL / capabilities 空
    assert robot.enabled is False and robot.estop_active is None
    from app.db.models import RobotCapability
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        assert db.get(RobotCapability, robot.id).supported_commands_json == []
    # 真实物理动作仍被拒绝（执行档不含 → EXECUTION_PROFILE_DENIED）
    for action, params in (("homing", {}), ("navigate", {"spot": "A1"}), ("resume_patrol", {})):
        denied = post(api, action, params)
        assert denied.status_code >= 400, denied.text
        assert denied.json()["error"]["code"] == "EXECUTION_PROFILE_DENIED"
    assert boot


def test_02_ping_loop_via_r001_broker(g2, api):
    response = post(api, "ping")
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["payload_json"]["execution_mode"] == "DIAGNOSTIC"
    assert created["lifecycle_status"] == "QUEUED"
    row = wait_terminal(
        created["command_id"], {"SUCCEEDED", "ACK_REJECTED", "PUBLISHED_UNCONFIRMED"}
    )
    assert row.lifecycle_status == "SUCCEEDED", row.ack_payload_json
    ack = row.ack_payload_json
    assert ack["ack_source"] == "vehicle" and ack["code"] == "OK" and ack["success"] is True
    assert ack["data"]["pong"] is True and ack["data"]["boot_id"] == robot_row().boot_id
    assert ack["duration_ms"] >= 0 and row.published_at and row.ack_at
    msg_id = created["payload_json"]["message_id"]
    # 路由证据：命令只出现在 r001 Broker（B），原 Broker（A）零痕迹
    topics_b = g2["sniff_b"].topics()
    assert f"_platform/vehicle-agent-v1/{VEHICLE}/command" in topics_b
    assert f"v1/robot/{VEHICLE}/cmd" in topics_b
    assert not [t for t in g2["sniff_a"].topics() if VEHICLE in t]
    wire = next(
        json.loads(p)
        for t, p, _ in g2["sniff_b"].messages
        if t == f"v1/robot/{VEHICLE}/cmd" and json.loads(p)["msg_id"] == msg_id
    )
    assert wire["vehicle_id"] == VEHICLE and wire["target_boot_id"] == robot_row().boot_id
    assert wire["mode"] == "DIAGNOSTIC" and wire["expires_at"]
    # 车端真实执行记录（correlation：msg_id）
    assert [r for r in exec_log(g2) if r["msg_id"] == msg_id and r["code"] == "OK"]


def test_03_get_status_and_telemetry_real_data(g2, api):
    status = post(api, "get_status").json()
    row = wait_terminal(status["command_id"], {"SUCCEEDED", "ACK_REJECTED"})
    data = row.ack_payload_json["data"]
    assert row.lifecycle_status == "SUCCEEDED"
    assert data["online"] is True and data["programs"]["agent"] is True
    assert data["boot_id"] == robot_row().boot_id
    telemetry = post(api, "get_telemetry").json()
    row = wait_terminal(telemetry["command_id"], {"SUCCEEDED", "ACK_REJECTED"})
    host = row.ack_payload_json["data"]["host"]
    assert row.lifecycle_status == "SUCCEEDED"
    assert host["memory_bytes"]["total"] > 0 and host["uptime_seconds"] > 0
    # 未接入硬件：底盘/电池等显式 null，不生成模拟值
    assert row.ack_payload_json["data"]["battery"] is None


def test_04_duplicate_command_executes_once(g2, api):
    key = str(uuid4())
    first = post(api, "ping", key=key).json()
    again = post(api, "ping", key=key).json()
    assert again["command_id"] == first["command_id"]  # HTTP 幂等：同键同命令
    wait_terminal(first["command_id"], {"SUCCEEDED"})
    msg_id = first["payload_json"]["message_id"]
    wire = next(
        json.loads(p)
        for t, p, _ in g2["sniff_b"].messages
        if t == f"v1/robot/{VEHICLE}/cmd" and json.loads(p)["msg_id"] == msg_id
    )
    # QoS1 重投/ACK 丢失：同一 wire 再投两次 → 车端重放原结果，不重复执行
    for _ in range(2):
        g2["sniff_b"].publish(f"v1/robot/{VEHICLE}/cmd", wire)
    acks = wait_for(lambda: len(a := acks_on_b(g2, msg_id)) >= 3 and a, 10)
    assert acks and len({json.dumps(a, sort_keys=True) for a in acks}) == 1
    executed = [r for r in exec_log(g2) if r.get("kind") == "ack" and r["msg_id"] == msg_id]
    assert len(executed) == 1


def _raw_wire(command: str, **overrides) -> dict:
    now = datetime.now(UTC)
    wire = {
        "msg_id": str(uuid4()),
        "command": command,
        "params": {},
        "ts": now.isoformat(),
        "vehicle_id": VEHICLE,
        "target_boot_id": robot_row().boot_id,
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "mode": "DIAGNOSTIC",
    }
    wire.update(overrides)
    return wire


def test_05_vehicle_side_independent_rejections(g2):
    cases = {
        "STALE_BOOT": _raw_wire("ping", target_boot_id=str(uuid4())),
        "WRONG_VEHICLE": _raw_wire("ping", vehicle_id="firebot-vehicle-02"),
        "EXPIRED": _raw_wire(
            "ping",
            ts=(datetime.now(UTC) - timedelta(seconds=60)).isoformat(),
            expires_at=(datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        ),
        "MODE_MISMATCH": _raw_wire("start", params={"with_navigation": True}, mode="REAL"),
        "NOT_AUTHORIZED": _raw_wire("homing", mode="REAL"),
        "INVALID_PARAM": {k: v for k, v in _raw_wire("ping").items() if k != "target_boot_id"},
    }
    future = _raw_wire("ping")
    future["ts"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    future["expires_at"] = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()
    cases["EXPIRED_FUTURE"] = future
    for wire in cases.values():
        g2["sniff_b"].publish(f"v1/robot/{VEHICLE}/cmd", wire)
    for code, wire in cases.items():
        acks = wait_for(lambda w=wire: acks_on_b(g2, w["msg_id"]), 10)
        assert acks, f"no ACK for {code}"
        assert acks[0]["success"] is False and acks[0]["code"] == code.replace("_FUTURE", "")
    executed = {r["msg_id"] for r in exec_log(g2) if r.get("code") == "OK"}
    assert not executed & {w["msg_id"] for w in cases.values()}


def test_06_restart_new_session_rejects_previous_session_command(g2, api):
    old_boot = robot_row().boot_id
    stale = _raw_wire("ping")  # 针对旧会话签发
    stop(g2["agent"])  # 正常退出（graceful offline）
    wait_for(lambda: robot_row().online_state == "OFFLINE", 20)
    start_agent(g2)
    new_boot = wait_online_session(previous=old_boot)
    assert new_boot != old_boot
    g2["sniff_b"].publish(f"v1/robot/{VEHICLE}/cmd", stale)
    acks = wait_for(lambda: acks_on_b(g2, stale["msg_id"]), 10)
    assert acks and acks[0]["code"] == "STALE_BOOT"
    # 新会话下 API 命令正常
    row = wait_terminal(post(api, "ping").json()["command_id"], {"SUCCEEDED", "ACK_REJECTED"})
    assert row.lifecycle_status == "SUCCEEDED"


# ---------------------------------------------------------------- G2-B ROS 软件闭环
def _software(api, action: str, params: dict | None = None):
    response = post(api, action, params)
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["payload_json"]["execution_mode"] == "SOFTWARE_TEST"
    assert created["task_id"] is None  # 软件测试不创建真实业务任务
    row = wait_terminal(created["command_id"], {"SUCCEEDED", "FAILED", "ACK_REJECTED"}, 30)
    return created, row


def test_07_ros_software_start_patrol_stop_close(g2, api):
    if not g2["ros"]:
        pytest.skip("rospy 不可用：ROS 软件闭环 NOT_EXECUTED（不伪造通过）")
    expected = [
        ("start", {"with_navigation": True}, None),
        ("patrol", {}, None),
        ("stop_patrol", {}, None),
        ("close", {}, None),
        ("close", {}, "ALREADY_CLOSED"),
    ]
    for action, params, reason in expected:
        created, row = _software(api, action, params)
        assert row.lifecycle_status == "SUCCEEDED", (action, row.ack_reason, row.ack_payload_json)
        ack = row.ack_payload_json
        # ACK = 车端接收（RECEIVED）；软件完成以 ROS COMPLETED 反馈为准（两个不同状态）
        assert ack["ack_source"] == "vehicle" and ack["data"]["stage"] == "RECEIVED"
        assert ack["data"]["hardware"] == "NO_HARDWARE"
        states = [item["state"] for item in ack["software_feedback"]]
        assert states[-1] == "COMPLETED" and "ACCEPTED" in states, states
        assert ack["software_feedback"][-1].get("reason_code") == reason
    # ROS 节点状态经 status.software 回传云端（SOFTWARE_TEST / NO_HARDWARE）
    software = wait_for(
        lambda: (robot_row().agent_state_json or {}).get("status", {}).get("software"), 10
    )
    assert software and software["hardware"] == "NO_HARDWARE" and software["state"] == "STOPPED"


def test_08_ros_software_rejection_is_failure_not_success(g2, api):
    if not g2["ros"]:
        pytest.skip("rospy 不可用：NOT_EXECUTED")
    # 软件已关闭时巡检 → ROS 节点 REJECTED(NOT_READY) → FAILED（不得显示成功）
    created, row = _software(api, "patrol")
    assert row.lifecycle_status == "FAILED" and row.ack_reason == "NOT_READY"


# ---------------------------------------------------------------- 隔离与异常
def test_09_cross_broker_misroute_rejected(g2):
    """原 Broker A 上伪造的 001 内部命令：原 Adapter 不属于该 profile → 拒绝，不下发。"""
    from app.modules.commands.service import build_command_payload

    robot = robot_row()
    payload = build_command_payload(
        robot=robot,
        operator_id="g2-test",
        cmd="ping",
        params={},
        ttl_ms=30000,
        priority=50,
        execution_mode="DIAGNOSTIC",
    )
    g2["sniff_a"].publish(f"_platform/vehicle-agent-v1/{VEHICLE}/command", payload)
    time.sleep(2)
    assert f"v1/robot/{VEHICLE}/cmd" not in g2["sniff_a"].topics()
    assert not acks_on_b(g2, payload["message_id"])
    log = (g2["workdir"] / "adapter-A.log").read_text()
    assert "not owned by this adapter" in log


def test_10_unknown_profile_never_falls_back(g2):
    import importlib.util

    from app.core.config import get_settings
    from app.core.vehicle_routing import RoutingError, transport_profile

    settings = get_settings()
    previous = settings.vehicle_transport_profiles
    settings.vehicle_transport_profiles = f"{VEHICLE}=mystery-profile"
    try:
        with pytest.raises(RoutingError):
            transport_profile(VEHICLE)
        spec = importlib.util.spec_from_file_location(
            "dispatcher_probe",
            os.path.join(os.path.dirname(__file__), "../../services/command-dispatcher/main.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with pytest.raises(RoutingError):
            module.client_for("mystery-profile")
    finally:
        settings.vehicle_transport_profiles = previous


def test_11_frozen_vehicle_no_fake_ack_then_expired_rejection(g2, api):
    agent = g2["agent"]
    os.kill(agent.pid, signal.SIGSTOP)  # 车端冻结：仍在线（未超过 offline 阈值）但不处理
    try:
        created = post(api, "ping").json()
        row = wait_terminal(created["command_id"], {"PUBLISHED_UNCONFIRMED", "SUCCEEDED"}, 20)
        # 超时只产生“结果未确认”，绝不产生成功
        assert row.lifecycle_status == "PUBLISHED_UNCONFIRMED"
        assert row.ack_payload_json["ack_source"] == "transport_timeout"
    finally:
        os.kill(agent.pid, signal.SIGCONT)
    # 车端恢复后收到已过期命令 → 独立拒绝 EXPIRED，不执行
    row = wait_terminal(created["command_id"], {"ACK_REJECTED"}, 20)
    assert row.ack_payload_json["code"] == "EXPIRED"
    msg_id = created["payload_json"]["message_id"]
    assert not [r for r in exec_log(g2) if r["msg_id"] == msg_id and r["code"] == "OK"]


def test_12_r002_isolation(g2):
    after = r002_state()
    assert after == R002_SNAPSHOT
    assert after["agent_protocol"] == "CANONICAL_MQTT"
    from app.core.vehicle_routing import execution_mode, transport_profile

    assert transport_profile("R002") == "default" and execution_mode("R002", "patrol") == "REAL"
    assert transport_profile("firebot-vehicle-02") == "default"
    assert not [t for t in g2["sniff_a"].topics() if VEHICLE in t and t.endswith("/cmd")]
    assert PROFILE in transport_profile(VEHICLE)
