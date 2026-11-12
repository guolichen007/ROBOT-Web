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
        from conftest import ensure_isolated_test_admin

        username, password = ensure_isolated_test_admin()
        response = client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
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
def _post_software(api, action: str, params: dict | None = None) -> dict:
    response = post(api, action, params)
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["payload_json"]["execution_mode"] == "SOFTWARE_TEST"
    assert created["task_id"] is None  # 软件测试不创建真实业务任务
    return created


def _software(api, action: str, params: dict | None = None):
    created = _post_software(api, action, params)
    row = wait_terminal(
        created["command_id"], {"SUCCEEDED", "FAILED", "ACK_REJECTED", "CANCELLED"}, 40
    )
    return created, row


def _states(row) -> list[str]:
    return [item["state"] for item in (row.ack_payload_json or {}).get("software_feedback", [])]


def _ros_log(g2) -> str:
    return (g2["workdir"] / "ros-001-command-node.log").read_text(errors="replace")


def test_07_ros_001_custom_messages_start_patrol_close(g2, api):
    """001 正式 command_node.py + firebot_001_software 自定义消息：完整软件闭环。"""
    if not g2["ros"]:
        pytest.skip("rospy 不可用：ROS 软件闭环 NOT_EXECUTED（不伪造通过）")
    created, row = _software(api, "start", {"with_navigation": True})
    assert row.lifecycle_status == "SUCCEEDED", (row.ack_reason, row.ack_payload_json)
    ack = row.ack_payload_json
    assert ack["ack_source"] == "vehicle" and ack["data"]["stage"] == "RECEIVED"
    assert _states(row)[-1] == "COMPLETED" and "ACCEPTED" in _states(row)
    msg_id = created["payload_json"]["message_id"]
    # SoftwareCommand.command_id = V1 msg_id（非云端业务 command_id），节点日志可逐条对应
    assert f"command_id={msg_id}" in _ros_log(g2)
    assert created["command_id"] not in _ros_log(g2)
    # 自然完成的巡检（rounds=2）：RUNNING→EXECUTING，最终 COMPLETED
    created, row = _software(api, "patrol", {"rounds": 2})
    assert row.lifecycle_status == "SUCCEEDED", (row.ack_reason, row.ack_payload_json)
    feedback = row.ack_payload_json["software_feedback"]
    assert [f["ros_status"] for f in feedback] == ["ACCEPTED", "RUNNING", "RUNNING", "COMPLETED"]
    assert _states(row) == ["ACCEPTED", "EXECUTING", "EXECUTING", "COMPLETED"]
    created, row = _software(api, "close")
    assert row.lifecycle_status == "SUCCEEDED"
    # SoftwareStatus 自定义消息 → 可序列化状态（经 status.software 回传云端）
    software = wait_for(
        lambda: (s := (robot_row().agent_state_json or {}).get("status", {}).get("software"))
        and s.get("state") == "idle"
        and s,
        15,
    )
    assert software and software["hardware"] == "NO_HARDWARE" and software["pid"] > 0
    assert "hardware_executors=NONE" in software["diagnostics"]


def test_08_stop_patrol_while_running(g2, api):
    """巡检运行中停止：stop 在巡检自然完成之前收到反馈；patrol 记录为 STOPPED（非 FAILED）。"""
    if not g2["ros"]:
        pytest.skip("rospy 不可用：NOT_EXECUTED")
    _, row = _software(api, "start", {"with_navigation": True})
    assert row.lifecycle_status == "SUCCEEDED"
    rounds = 20  # 自然完成需 ~20s
    patrol = _post_software(api, "patrol", {"rounds": rounds})
    started = time.monotonic()
    wait_for(lambda: "EXECUTING" in _states(command_row(patrol["command_id"])), 15)
    # 巡检进行中：诊断仍可即时执行（工作线程未被长任务阻塞）
    ping = post(api, "ping").json()
    ping_row = wait_terminal(ping["command_id"], {"SUCCEEDED", "ACK_REJECTED"}, 10)
    assert ping_row.lifecycle_status == "SUCCEEDED"
    stop = _post_software(api, "stop_patrol")
    stop_row = wait_terminal(stop["command_id"], {"SUCCEEDED", "FAILED", "ACK_REJECTED"}, 15)
    elapsed = time.monotonic() - started
    assert elapsed < rounds - 5, f"stop took {elapsed:.1f}s — not before natural completion"
    assert stop_row.lifecycle_status == "SUCCEEDED", stop_row.ack_payload_json
    stop_feedback = stop_row.ack_payload_json["software_feedback"]
    # stop 的 ACK 与终态反馈关联 stop 自己的 msg_id；终态来自真实 STOPPED 事件
    assert stop_row.ack_payload_json["msg_id"] == stop["payload_json"]["message_id"]
    assert (
        stop_feedback[-1]["state"] == "COMPLETED" and stop_feedback[-1]["ros_status"] == "STOPPED"
    )
    assert stop_feedback[-1]["stopped_cloud_command_id"] == patrol["command_id"]
    patrol_row = wait_terminal(patrol["command_id"], {"CANCELLED", "SUCCEEDED", "FAILED"}, 10)
    assert patrol_row.lifecycle_status == "CANCELLED" and patrol_row.ack_reason == "STOPPED"
    assert _states(patrol_row)[-1] == "STOPPED"
    assert "COMPLETED" not in _states(patrol_row)
    log = _ros_log(g2)
    assert f"command_id={patrol['payload_json']['message_id']} STOPPED" in log
    g2["evidence"]["stop_while_running_s"] = round(elapsed, 2)
    print(
        "G2_STOP_EVIDENCE "
        + json.dumps(
            {
                "patrol_rounds": rounds,
                "stop_terminal_after_s": round(elapsed, 2),
                "patrol": {
                    "command_id": patrol["command_id"],
                    "msg_id": patrol["payload_json"]["message_id"],
                    "status": patrol_row.lifecycle_status,
                    "states": _states(patrol_row),
                },
                "stop": {
                    "command_id": stop["command_id"],
                    "msg_id": stop["payload_json"]["message_id"],
                    "status": stop_row.lifecycle_status,
                    "final": stop_feedback[-1],
                },
            },
            ensure_ascii=False,
        )
    )
    _, row = _software(api, "close")
    assert row.lifecycle_status == "SUCCEEDED"


def test_08a_patrol_after_stop_completes_naturally(g2, api):
    """P0-A：start → patrol → stop_patrol → start → patrol 自然完成 → close。

    上一次 stop 不得残留影响下一次巡检（001 节点 _patrol_stop 按次重置）。
    """
    if not g2["ros"]:
        pytest.skip("rospy 不可用：NOT_EXECUTED")
    _, row = _software(api, "start", {"with_navigation": True})
    assert row.lifecycle_status == "SUCCEEDED", row.ack_payload_json
    first = _post_software(api, "patrol", {"rounds": 20})
    wait_for(lambda: "EXECUTING" in _states(command_row(first["command_id"])), 15)
    stop = _post_software(api, "stop_patrol")
    stop_row = wait_terminal(stop["command_id"], {"SUCCEEDED", "FAILED", "ACK_REJECTED"}, 15)
    assert stop_row.lifecycle_status == "SUCCEEDED"
    first_row = wait_terminal(first["command_id"], {"CANCELLED", "SUCCEEDED", "FAILED"}, 10)
    assert first_row.lifecycle_status == "CANCELLED"
    # 001 节点停止后回到 idle：需再次 start 才能巡检
    _, row = _software(api, "start", {"with_navigation": True})
    assert row.lifecycle_status == "SUCCEEDED", row.ack_payload_json
    second, second_row = _software(api, "patrol", {"rounds": 3})
    assert second_row.lifecycle_status == "SUCCEEDED", (
        second_row.ack_reason,
        _states(second_row),
    )
    feedback = second_row.ack_payload_json["software_feedback"]
    assert [f["ros_status"] for f in feedback] == [
        "ACCEPTED",
        "RUNNING",
        "RUNNING",
        "RUNNING",
        "COMPLETED",
    ], feedback
    assert "STOPPED" not in _states(second_row)
    _, row = _software(api, "close")
    assert row.lifecycle_status == "SUCCEEDED"
    print(
        "G2_REPATROL_EVIDENCE "
        + json.dumps(
            {
                "first_patrol": [first["command_id"], first_row.lifecycle_status],
                "stop": [stop["command_id"], stop_row.lifecycle_status],
                "second_patrol": [second["command_id"], second_row.lifecycle_status],
                "second_ros_status": [f["ros_status"] for f in feedback],
            },
            ensure_ascii=False,
        )
    )


ROGUE_PUBLISHER = """
import sys, time, rospy
from firebot_001_software.msg import SoftwareFeedback
rospy.init_node("g2_rogue_feedback", anonymous=True, disable_signals=True)
pub = rospy.Publisher("/firebot_001/software/feedback", SoftwareFeedback, queue_size=5)
deadline = time.time() + 10
while pub.get_num_connections() == 0 and time.time() < deadline:
    time.sleep(0.05)
time.sleep(0.5)
msg = SoftwareFeedback(command_id=sys.argv[1], action="patrol", status="STOPPED",
                       detail="forged stop", software_test=False, ts="forged")
pub.publish(msg)
time.sleep(1.0)
"""


def test_08c_untrusted_ros_feedback_rejected(g2, api):
    """不可信反馈（software_test=false）不得驱动 STOPPED/成功；stop 只关联当前活动巡检。"""
    if not g2["ros"]:
        pytest.skip("rospy 不可用：NOT_EXECUTED")
    import subprocess

    _, row = _software(api, "start", {"with_navigation": True})
    assert row.lifecycle_status == "SUCCEEDED"
    patrol = _post_software(api, "patrol", {"rounds": 20})
    wait_for(lambda: "EXECUTING" in _states(command_row(patrol["command_id"])), 15)
    python3, env = g2["ros_env"]
    forged = subprocess.run(
        [python3, "-c", ROGUE_PUBLISHER, patrol["payload_json"]["message_id"]],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert forged.returncode == 0, forged.stderr.decode()[-500:]
    row = wait_terminal(patrol["command_id"], {"CANCELLED", "SUCCEEDED", "FAILED"}, 10)
    assert row.lifecycle_status == "FAILED", (row.ack_reason, _states(row))
    assert row.ack_reason == "SOFTWARE_TEST_FLAG_MISSING"
    assert "STOPPED" not in _states(row)
    # 该巡检已被桥判定终止（不再追踪）→ 新 stop 无可关联活动巡检：结果未确认，不得成功
    stop = _post_software(api, "stop_patrol")
    stop_row = wait_terminal(stop["command_id"], {"SUCCEEDED", "FAILED", "ACK_REJECTED"}, 15)
    assert stop_row.lifecycle_status == "FAILED"
    assert stop_row.ack_reason == "STOP_TARGET_UNTRACKED"
    # 真实节点确实已停（state=idle），云端 patrol 仍保持 FAILED（不被后到的 STOPPED 改写）
    assert wait_for(
        lambda: (s := (robot_row().agent_state_json or {}).get("status", {}).get("software"))
        and s.get("state") == "idle",
        15,
    )
    assert command_row(patrol["command_id"]).lifecycle_status == "FAILED"


def test_08b_ros_rejection_is_failure_not_success(g2, api):
    if not g2["ros"]:
        pytest.skip("rospy 不可用：NOT_EXECUTED")
    # 软件已关闭（idle）时巡检 → 001 节点 REJECTED(not ready) → FAILED（不得显示成功）
    _, row = _software(api, "patrol")
    assert row.lifecycle_status == "FAILED" and row.ack_reason == "NOT_READY"
    assert row.ack_payload_json["software_feedback"][-1]["ros_status"] == "REJECTED"


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
