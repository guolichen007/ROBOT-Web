"""G2 车端执行器单元测试：授权、会话合同、幂等、诊断真实执行、软件测试分级 ACK、路由配置。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from services.vehicle_agent_v1 import EXECUTION_PROFILES, command_from_canonical
from services.vehicle_agent_v1_exec.executor import CommandExecutor
from services.vehicle_agent_v1_exec.grant import GrantError, load_grant
from services.vehicle_agent_v1_exec.ros.software_test_node import SoftwareStateMachine
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


def test_software_bridge_unavailable_reports_failure(harness):
    validator, executor, published, _, _ = harness

    def broken(request):
        raise OSError("no such socket")
        yield  # pragma: no cover

    executor.bridge_request = broken
    executor.handle(json.dumps(wire(validator, "patrol")).encode())
    events = [data for kind, data in published if kind == "event"]
    assert events[-1]["state"] == "FAILED" and events[-1]["reason_code"] == "ROS_UNAVAILABLE"


def test_software_missing_terminal_feedback_is_failure(harness):
    validator, executor, published, _, script = harness
    script[:] = [{"state": "ACCEPTED"}]
    executor.handle(json.dumps(wire(validator, "close")).encode())
    events = [data for kind, data in published if kind == "event"]
    assert events[-1]["state"] == "FAILED" and events[-1]["reason_code"] == "FEEDBACK_TIMEOUT"


def test_software_state_machine():
    machine = SoftwareStateMachine()
    assert machine.apply("patrol")[:2] == ("REJECTED", "NOT_READY")
    assert machine.apply("start")[0] == "COMPLETED" and machine.state == "IDLE"
    assert machine.apply("patrol")[0] == "COMPLETED" and machine.state == "PATROLLING"
    assert machine.apply("stop_patrol")[0] == "COMPLETED" and machine.state == "IDLE"
    assert machine.apply("close")[0] == "COMPLETED" and machine.state == "STOPPED"
    assert machine.apply("close")[1] == "ALREADY_CLOSED"
    assert machine.snapshot()["hardware"] == "NO_HARDWARE"


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
