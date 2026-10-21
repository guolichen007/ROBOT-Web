from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fakeredis import FakeRedis

from scripts.generate_vehicle_agent_contract import contract
from scripts.vehicle_agent_mock_runtime import load_service
from services.vehicle_agent_v1 import (
    ACK_CODES,
    BUSINESS_COMMANDS,
    PROTOCOL,
    Ack,
    WireCommand,
    command_from_canonical,
    normalize,
    parse_topic,
    status_confirms,
    topic,
)

Adapter = load_service("vehicle-agent-adapter").Adapter
MockAgent = load_service("mock-vehicle-agent").MockAgent


def canonical(action="start", **overrides):
    now = datetime.now(UTC)
    return {
        "schema_version": "vehicle-agent-v1",
        "type": "command",
        "message_id": str(uuid4()),
        "vehicle_id": "TEST_V1",
        "target_boot_id": str(uuid4()),
        "command_id": "C-" + str(uuid4()),
        "correlation_id": str(uuid4()),
        "operator_id": "test-operator",
        "source": "WEB",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=90)).isoformat(),
        "ttl_ms": 90000,
        "priority": 50,
        "cmd": action,
        "params": {"with_navigation": True}
        if action == "start"
        else {"spot": "1号车位"}
        if action == "navigate"
        else {},
        **overrides,
    }


def adapter_for(payload):
    redis = FakeRedis(decode_responses=True)
    published = []
    redis.set(
        f"vehicle-agent:binding:{payload['vehicle_id']}", f"{PROTOCOL}:{payload['target_boot_id']}"
    )
    return Adapter(redis, lambda *args: published.append(args), downlink=True), redis, published


@pytest.mark.parametrize(
    "kind,qos,retain",
    [
        ("cmd", 1, False),
        ("ack", 1, False),
        ("status", 1, True),
        ("telemetry", 0, False),
        ("availability", 1, True),
        ("event", 1, False),
    ],
)
def test_topic_contract(kind, qos, retain):
    from services.vehicle_agent_v1 import TOPIC_CONTRACT

    assert parse_topic(topic("TEST_V1", kind)) == ("TEST_V1", kind)
    assert TOPIC_CONTRACT[kind] == (qos, retain)


@pytest.mark.parametrize("action", BUSINESS_COMMANDS)
def test_each_wire_command(action):
    payload = canonical(action)
    wire = command_from_canonical(payload)
    assert set(wire) == {"msg_id", "command", "params", "ts"}
    assert wire["msg_id"] == payload["message_id"]
    assert wire["command"] == action


def test_generated_contract_is_current():
    root = Path(__file__).resolve().parents[3]
    assert (
        json.loads((root / "packages/protocol-schemas/vehicle-agent-v1.contract.json").read_text())
        == contract()
    )


def test_bad_params_topics_and_missing_data():
    for action, params in [
        ("start", {}),
        ("start", {"with_navigation": False}),
        ("navigate", {"spot": ""}),
        ("patrol", {"route": []}),
    ]:
        with pytest.raises(ValueError):
            WireCommand.model_validate(
                {
                    "msg_id": str(uuid4()),
                    "command": action,
                    "params": params,
                    "ts": datetime.now(UTC).isoformat(),
                }
            )
    for name in ["robot/TEST_V1/cmd", "v1/robot/../../cmd", "v1/robot/TEST_V1/video"]:
        with pytest.raises(ValueError):
            parse_topic(name)
    telemetry = normalize("telemetry", {})
    assert telemetry["battery"] is None and telemetry["thermal"] is None


def test_status_telemetry_and_availability():
    mock = MockAgent()
    assert normalize("availability", "offline") == {"state": "offline"}
    assert normalize("availability", {"state": "online"}) == {"state": "online"}
    assert normalize("status", mock.status())["task"]["state"] == "idle"
    assert normalize("telemetry", mock.telemetry())["pose"]["yaw"] == 0
    with pytest.raises(ValueError):
        normalize("telemetry", {"battery": {"percentage": 101}})


@pytest.mark.parametrize("code", ACK_CODES)
def test_ack_codes_preserve_failure_and_idempotent_success(code):
    ack = Ack.model_validate(
        {
            "msg_id": str(uuid4()),
            "success": code == "OK",
            "code": code,
            "message": "车端原始诊断",
            "data": {},
            "duration_ms": 10,
            "ts": datetime.now(UTC).isoformat(),
        }
    )
    assert ack.accepted == (code in {"OK", "ALREADY_RUNNING", "ALREADY_CLOSED", "ALREADY_STOPPED"})
    assert ack.message == "车端原始诊断"


def test_exact_ack_correlation_and_duplicate_vehicle_execution():
    payload = canonical()
    adapter, redis, published = adapter_for(payload)
    name = "_platform/vehicle-agent-v1/TEST_V1/command"
    for _ in range(2):
        adapter.process(name, json.dumps(payload).encode())
    wires = [json.loads(p[1]) for p in published if p[0].endswith("/cmd")]
    mock = MockAgent()
    ack = mock.execute(wires[0])
    assert mock.execute(wires[1]) == ack
    assert mock.executions["start"] == 1
    adapter.process(topic("TEST_V1", "ack"), json.dumps(ack).encode())
    normalized = json.loads(published[-1][1])["data"]
    assert normalized["command_id"] == payload["command_id"]
    assert normalized["message_id"] == payload["message_id"]
    assert normalized["target_boot_id"] == payload["target_boot_id"]
    with pytest.raises(ValueError):
        adapter.process(topic("OTHER", "ack"), json.dumps(ack).encode())
    with pytest.raises(ValueError):
        adapter.process(topic("TEST_V1", "ack"), json.dumps(ack).encode(), retained=True)
    count = len(wires)
    adapter.process(name, json.dumps(payload).encode())
    assert len([p for p in published if p[0].endswith("/cmd")]) == count


def test_five_minute_vehicle_dedup_window():
    mock = MockAgent()
    wire = command_from_canonical(canonical())
    mock.execute(wire, now=0)
    mock.execute(wire, now=299)
    assert mock.executions["start"] == 1
    mock.execute(wire, now=301)
    assert mock.executions["start"] == 2


def test_adapter_timeout_and_late_ack_cannot_replace_timeout():
    payload = canonical()
    adapter, redis, published = adapter_for(payload)
    adapter.down("TEST_V1", payload)
    pending_key = next(iter(redis.zrange("vehicle-agent:v1:pending", 0, -1)))
    redis.zadd("vehicle-agent:v1:pending", {pending_key: time.time() - 1})
    adapter.expire()
    assert json.loads(published[-1][1])["data"]["code"] == "TIMEOUT"
    ack = MockAgent().execute(command_from_canonical(payload))
    adapter.process(topic("TEST_V1", "ack"), json.dumps(ack).encode())
    assert json.loads(published[-1][1])["data"]["code"] == "TIMEOUT"


def test_downlink_disabled_binding_changed_and_payload_reuse_fail_closed():
    payload = canonical()
    adapter, redis, published = adapter_for(payload)
    adapter.downlink = False
    with pytest.raises(ValueError):
        adapter.down("TEST_V1", payload)
    adapter.downlink = True
    redis.delete("vehicle-agent:binding:TEST_V1")
    with pytest.raises(ValueError):
        adapter.down("TEST_V1", payload)
    redis.set("vehicle-agent:binding:TEST_V1", f"{PROTOCOL}:{payload['target_boot_id']}")
    adapter.down("TEST_V1", payload)
    with pytest.raises(ValueError):
        adapter.down("TEST_V1", {**payload, "command_id": "C-OTHER"})
    assert len([p for p in published if p[0].endswith("/cmd")]) == 1


def test_navigate_ack_does_not_mean_arrival_and_mock_faults():
    mock = MockAgent()
    assert mock.execute(command_from_canonical(canonical("navigate")))["code"] == "NOT_READY"
    mock.execute(command_from_canonical(canonical()))
    assert mock.execute(command_from_canonical(canonical("navigate")))["success"]
    assert mock.task["state"] == "spot"
    assert mock.execute(command_from_canonical(canonical("patrol")))["code"] == "BUSY"
    assert status_confirms("navigate", mock.status())
    mock.arrive()
    assert mock.task["state"] == "idle"
    mock.failure = "TIMEOUT"
    assert mock.execute(command_from_canonical(canonical("homing"))) is None
