"""G2 车端执行器单元测试：授权、会话合同、幂等、诊断真实执行、软件测试分级 ACK、路由配置。"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.vehicle_agent_v1 import EXECUTION_PROFILES, command_from_canonical
from services.vehicle_agent_v1_exec.executor import CommandExecutor
from services.vehicle_agent_v1_exec.grant import GrantError, load_grant
from services.vehicle_agent_v1_exec.ros import contract
from services.vehicle_agent_v1_exec.session import DedupStore, SessionValidator

VEHICLE = "firebot-vehicle-01"


def write_grant(tmp_path: Path, **overrides) -> Path:
    data = {
        "vehicle_id": VEHICLE,
        "execution_profile": "ros-software-test",
        "ros_bridge_socket": str(tmp_path / "bridge.sock"),
        "not_after": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    data.update(overrides)
    path = tmp_path / "grant.json"
    path.write_text(json.dumps({k: v for k, v in data.items() if v is not None}))
    path.chmod(0o600)
    return path


def test_grant_fail_closed(tmp_path):
    assert load_grant(write_grant(tmp_path), VEHICLE).execution_profile == "ros-software-test"
    for overrides in (
        {"vehicle_id": "firebot-vehicle-02"},
        {"execution_profile": "telemetry-only"},
        {"execution_profile": "full"},
        {"not_after": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
        {"ros_bridge_socket": None},
        {"extra": 1},
    ):
        with pytest.raises(GrantError):
            load_grant(write_grant(tmp_path, **overrides), VEHICLE)
    path = write_grant(tmp_path)
    path.chmod(0o644)
    with pytest.raises(GrantError):
        load_grant(path, VEHICLE)


def test_profiles_never_grant_physical_commands():
    for name, commands in EXECUTION_PROFILES.items():
        assert "REAL" not in commands.values(), name
        assert not {"homing", "navigate", "resume_patrol", "stop_navigate"} & set(commands)


@pytest.fixture
def harness(tmp_path):
    grant = load_grant(write_grant(tmp_path), VEHICLE)
    validator = SessionValidator(VEHICLE, grant, DedupStore(tmp_path / "dedup.json"))
    validator.new_session()
    published: list[tuple[str, dict]] = []
    bridge_calls: list[dict] = []
    feedback_script: list[dict] = [
        {"state": "ACCEPTED"},
        {"state": "EXECUTING"},
        {"state": "COMPLETED", "phase": "IDLE"},
    ]

    def bridge(request):
        bridge_calls.append(request)
        yield from feedback_script

    executor = CommandExecutor(
        validator,
        lambda kind, data, **kw: published.append((kind, data)) or True,
        program_markers={"chassis": ("never-matching-marker",)},
        bridge_request=bridge,
        execution_log=tmp_path / "exec.jsonl",
    )
    return validator, executor, published, bridge_calls, feedback_script


def wire(validator, command="ping", **overrides) -> dict:
    now = datetime.now(UTC)
    data = {
        "msg_id": str(uuid4()),
        "command": command,
        "params": {"with_navigation": True} if command == "start" else {},
        "ts": now.isoformat(),
        "vehicle_id": VEHICLE,
        "target_boot_id": validator.boot_id,
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "mode": "SOFTWARE_TEST"
        if command in {"start", "close", "patrol", "stop_patrol"}
        else "DIAGNOSTIC",
    }
    data.update(overrides)
    return data


def acks(published):
    return [data for kind, data in published if kind == "ack"]


def events(published, msg_id=None):
    return [
        data
        for kind, data in published
        if kind == "event" and (msg_id is None or data["msg_id"] == msg_id)
    ]


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def settled(executor, timeout=5.0):
    return wait_until(lambda: not executor.active_software(), timeout)


def test_diagnostic_real_execution_and_dedup(harness):
    validator, executor, published, _, _ = harness
    command = wire(validator, "get_telemetry")
    executor.handle(json.dumps(command).encode())
    executor.handle(json.dumps(command).encode())  # QoS1 重投
    first, second = acks(published)
    assert first == second and first["code"] == "OK" and first["success"] is True
    assert first["data"]["host"]["memory_bytes"]["total"] > 0
    assert first["data"]["battery"] is None  # 未接入硬件显式 null
    assert executor.executions == {"get_telemetry": 1}


@pytest.mark.parametrize(
    ("code", "overrides"),
    [
        ("STALE_BOOT", {"target_boot_id": str(uuid4())}),
        ("WRONG_VEHICLE", {"vehicle_id": "firebot-vehicle-02"}),
        ("EXPIRED", {"ts": "2020-01-01T00:00:00+00:00", "expires_at": "2020-01-01T00:00:30+00:00"}),
        ("MODE_MISMATCH", {"mode": "REAL"}),
        ("INVALID_PARAM", {"target_boot_id": None}),
    ],
)
def test_vehicle_rejects_independently(harness, code, overrides):
    validator, executor, published, _, _ = harness
    command = wire(validator, "ping", **overrides)
    command = {k: v for k, v in command.items() if v is not None}
    executor.handle(json.dumps(command).encode())
    (ack,) = acks(published)
    assert ack["code"] == code and ack["success"] is False
    assert executor.executions == {}


def test_future_issued_and_unauthorized_rejected(harness):
    validator, executor, published, _, _ = harness
    future = datetime.now(UTC) + timedelta(minutes=5)
    executor.handle(
        json.dumps(
            wire(
                validator,
                ts=future.isoformat(),
                expires_at=(future + timedelta(seconds=30)).isoformat(),
            )
        ).encode()
    )
    executor.handle(json.dumps(wire(validator, "homing", mode="REAL")).encode())
    assert [a["code"] for a in acks(published)] == ["EXPIRED", "NOT_AUTHORIZED"]
    assert executor.executions == {}


def test_new_session_invalidates_previous_commands(harness):
    validator, executor, published, _, _ = harness
    stale = wire(validator)
    validator.new_session()
    executor.handle(json.dumps(stale).encode())
    assert acks(published)[0]["code"] == "STALE_BOOT"


def test_dedup_survives_process_restart(tmp_path, harness):
    validator, executor, published, _, _ = harness
    command = wire(validator)
    executor.handle(json.dumps(command).encode())
    restored = DedupStore(tmp_path / "dedup.json")
    state, entry = restored.claim(
        command["msg_id"],
        __import__("services.vehicle_agent_v1_exec.session", fromlist=["fingerprint"]).fingerprint(
            command
        ),
    )
    assert state == "replay" and entry["ack"] == acks(published)[0]


def test_software_ack_is_receipt_feedback_is_completion(harness):
    validator, executor, published, bridge_calls, _ = harness
    executor.handle(json.dumps(wire(validator, "start")).encode())
    assert settled(executor)
    (ack,) = acks(published)
    assert ack["code"] == "OK" and ack["data"]["stage"] == "RECEIVED"
    assert ack["data"]["hardware"] == "NO_HARDWARE"
    events = [data for kind, data in published if kind == "event"]
    assert [e["state"] for e in events] == ["ACCEPTED", "EXECUTING", "COMPLETED"]
    assert all(e["type"] == "software_feedback" and e["msg_id"] == ack["msg_id"] for e in events)
    assert bridge_calls[0]["command"] == "start"
    # 重投：原 ACK + 原反馈重放，不再调用 ROS
    executor.handle(json.dumps({**wire(validator, "start"), "msg_id": ack["msg_id"]}).encode())
    assert len(bridge_calls) == 1
    # SoftwareCommand.command_id = V1 msg_id
    assert bridge_calls[0]["command_id"] == ack["msg_id"]


def test_software_bridge_unavailable_reports_failure(harness):
    validator, executor, published, _, _ = harness

    def broken(request):
        raise OSError("no such socket")
        yield  # pragma: no cover

    executor.bridge_request = broken
    executor.handle(json.dumps(wire(validator, "patrol")).encode())
    assert settled(executor)
    last = events(published)[-1]
    assert last["state"] == "FAILED" and last["reason_code"] == "ROS_UNAVAILABLE"


def test_software_missing_terminal_feedback_is_failure(harness):
    validator, executor, published, _, script = harness
    script[:] = [{"state": "ACCEPTED"}]
    executor.handle(json.dumps(wire(validator, "close")).encode())
    assert settled(executor)
    last = events(published)[-1]
    assert last["state"] == "FAILED" and last["reason_code"] == "FEEDBACK_TIMEOUT"


def test_wire_carries_session_contract():
    now = datetime.now(UTC)
    boot = str(uuid4())
    payload = {
        "schema_version": "vehicle-agent-v1",
        "type": "command",
        "message_id": str(uuid4()),
        "vehicle_id": VEHICLE,
        "target_boot_id": boot,
        "command_id": "C1",
        "correlation_id": str(uuid4()),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "ttl_ms": 30000,
        "priority": 50,
        "source": "WEB",
        "operator_id": "u",
        "cmd": "ping",
        "params": {},
        "execution_mode": "DIAGNOSTIC",
    }
    result = command_from_canonical(payload)
    assert result["target_boot_id"] == boot and result["vehicle_id"] == VEHICLE
    assert result["mode"] == "DIAGNOSTIC" and result["expires_at"]


def test_routing_configuration(monkeypatch):
    from app.core import vehicle_routing as routing
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "vehicle_transport_profiles", f"{VEHICLE}=r001-independent-v1")
    monkeypatch.setattr(settings, "transport_profile_adapters", "r001-independent-v1=r001-ad")
    monkeypatch.setattr(settings, "vehicle_execution_profiles", f"{VEHICLE}=ros-software-test")
    assert routing.transport_profile(VEHICLE) == "r001-independent-v1"
    assert routing.transport_profile("firebot-vehicle-02") == "default"
    assert routing.adapter_instance("r001-independent-v1") == "r001-ad"
    assert routing.execution_mode(VEHICLE, "ping") == "DIAGNOSTIC"
    assert routing.execution_mode(VEHICLE, "start") == "SOFTWARE_TEST"
    assert routing.execution_mode("firebot-vehicle-02", "patrol") == "REAL"  # 002 不变
    with pytest.raises(routing.RoutingError):
        routing.execution_mode(VEHICLE, "homing")
    monkeypatch.setattr(settings, "vehicle_transport_profiles", f"{VEHICLE}=unknown")
    with pytest.raises(routing.RoutingError):
        routing.transport_profile(VEHICLE)  # 未知 profile 不回退 default
    monkeypatch.setattr(settings, "vehicle_transport_profiles", "broken-entry")
    with pytest.raises(routing.RoutingError):
        routing.validate_configuration()


# ---------------------------------------------------------------- G2-R1 P0-2：异步长任务
def test_stop_and_diagnostics_not_blocked_by_running_patrol(tmp_path):
    grant = load_grant(write_grant(tmp_path), VEHICLE)
    validator = SessionValidator(VEHICLE, grant, DedupStore(tmp_path / "dedup.json"))
    validator.new_session()
    published: list[tuple[str, dict]] = []
    stop_signal = threading.Event()
    patrol_started = threading.Event()

    def bridge(request):
        if request["command"] == "patrol":
            yield {"state": "ACCEPTED"}
            patrol_started.set()
            for _ in range(500):  # 长巡检：只能被 stop 结束
                if stop_signal.wait(0.01):
                    yield {"state": "STOPPED", "ros_status": "STOPPED"}
                    return
                yield {"state": "EXECUTING", "ros_status": "RUNNING"}
            yield {"state": "COMPLETED"}
        elif request["command"] == "stop_patrol":
            yield {"state": "ACCEPTED"}
            stop_signal.set()
            yield {"state": "COMPLETED", "ros_status": "STOPPED", "stopped_command_id": "p"}

    lock = threading.Lock()

    def publish(kind, data, **kw):
        with lock:
            published.append((kind, data))
        return True

    executor = CommandExecutor(validator, publish, program_markers={}, bridge_request=bridge)
    patrol = wire(validator, "patrol", params={"rounds": 5})
    started = time.monotonic()
    executor.handle(json.dumps(patrol).encode())  # 立即返回（异步）
    assert time.monotonic() - started < 1.0
    assert patrol_started.wait(2)
    ping = wire(validator, "ping")
    executor.handle(json.dumps(ping).encode())  # 诊断即时完成
    assert [a for a in acks(published) if a["msg_id"] == ping["msg_id"]][0]["code"] == "OK"
    # 任务锁：巡检进行中再发启动类命令 → BUSY（确定未执行）
    second = wire(validator, "start")
    executor.handle(json.dumps(second).encode())
    assert [a for a in acks(published) if a["msg_id"] == second["msg_id"]][0]["code"] == "BUSY"
    stop = wire(validator, "stop_patrol")
    executor.handle(json.dumps(stop).encode())
    assert settled(executor)
    patrol_events = events(published, patrol["msg_id"])
    stop_events = events(published, stop["msg_id"])
    assert patrol_events[-1]["state"] == "STOPPED"  # 原巡检保留被停止记录
    assert stop_events[-1]["state"] == "COMPLETED"  # stop 终态关联 stop 自己的 msg_id
    assert stop_events[-1]["ros_status"] == "STOPPED"
    assert not [e for e in patrol_events if e["state"] in {"FAILED", "COMPLETED"}]
    # 重投：不再执行
    executor.handle(json.dumps(stop).encode())
    assert executor.executions["stop_patrol"] == 1
    executor.shutdown()


# ---------------------------------------------------------------- G2-R1 P0-1：001 消息适配
class _Cmd(SimpleNamespace):
    def __init__(self):
        super().__init__(command_id="", action="", params="")


def _fb(status, detail="", software_test=True):
    return SimpleNamespace(
        command_id="m-1",
        action="patrol",
        status=status,
        detail=detail,
        software_test=software_test,
        ts="2026-11-12T00:00:00+00:00",
    )


def test_001_command_mapping_params_json():
    message = contract.build_command(_Cmd, "m-uuid", "patrol", {"rounds": 3})
    assert message.command_id == "m-uuid" and message.action == "patrol"
    assert json.loads(message.params) == {"rounds": 3} and isinstance(message.params, str)
    assert contract.build_command(_Cmd, "m", "close", {}).params == "{}"


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("ACCEPTED", "ACCEPTED"),
        ("RUNNING", "EXECUTING"),
        ("COMPLETED", "COMPLETED"),
        ("REJECTED", "REJECTED"),
        ("STOPPED", "STOPPED"),
        ("WEIRD", "FAILED"),
    ],
)
def test_001_feedback_status_mapping(status, state):
    data = contract.feedback_to_dict(_fb(status, "detail text"))
    assert data["state"] == state and data["ros_status"] == status
    assert data["message"] == "detail text" and data["command_id"] == "m-1"


def test_001_feedback_reject_reason_and_software_test_flag():
    assert contract.feedback_to_dict(_fb("REJECTED", "not ready (state=idle)"))["reason_code"] == (
        "NOT_READY"
    )
    untrusted = contract.feedback_to_dict(_fb("COMPLETED", software_test=False))
    assert untrusted["state"] == "FAILED"
    assert untrusted["reason_code"] == "SOFTWARE_TEST_FLAG_MISSING"


def test_001_status_custom_message_serialized():
    status = SimpleNamespace(
        state="patrol",
        uptime_seconds=12.5,
        pid=42,
        load_1m=0.3,
        mem_available_bytes=1.0e9,
        diagnostics="hardware_executors=NONE",
        ts="t",
    )
    data = contract.status_to_dict(status)
    assert json.loads(json.dumps(data))["state"] == "patrol"
    assert data["patrol"] is True and data["hardware"] == "NO_HARDWARE"
