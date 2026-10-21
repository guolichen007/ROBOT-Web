"""API/Outbox/adapter/ingress/Redis/WS mock 闭环；不声称 PG 行锁或 migration 验收。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from scripts.vehicle_agent_mock_runtime import MockRuntime


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    old = {k: os.environ.get(k) for k in ("APP_ENV", "FIREBOT_MOCK_E2E")}
    os.environ.update(APP_ENV="test", FIREBOT_MOCK_E2E="1")
    value = MockRuntime(tmp_path_factory.mktemp("v1-mock") / "test.sqlite")
    yield value
    value.close()
    for key, previous in old.items():
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


@pytest.fixture(scope="module")
def client(runtime):
    with TestClient(runtime.app) as value:
        result = value.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "MockOnly-ChangeMe-2026!"}
        )
        assert result.status_code == 200, result.text
        value.headers["Authorization"] = "Bearer " + result.json()["access_token"]
        yield value


def send(runtime, client, action, params=None, *, pump=True):
    response = client.post(
        f"/api/v1/robots/R001/agent/commands/{action}",
        json={"params": params or {}},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    if pump:
        runtime.cycle()
    return response.json()


def command(client, row):
    response = client.get(f"/api/v1/commands/{row['command_id']}")
    assert response.status_code == 200
    return response.json()


def test_all_eight_commands_task_convergence_and_audit(runtime, client):
    from app.db.models import AuditLog, Task

    for action in ["patrol", "homing", "navigate"]:
        response = client.post(
            f"/api/v1/robots/R001/agent/commands/{action}",
            json={"params": {"spot": "A1"} if action == "navigate" else {}},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code != 202
    assert (
        command(client, send(runtime, client, "start", {"with_navigation": True}))[
            "lifecycle_status"
        ]
        == "SUCCEEDED"
    )
    patrol = send(runtime, client, "patrol")
    assert command(client, patrol)["lifecycle_status"] == "EXECUTING"
    assert command(client, send(runtime, client, "stop_patrol"))["lifecycle_status"] == "SUCCEEDED"
    assert runtime.mock.task["patrol_paused"]
    assert (
        command(client, send(runtime, client, "resume_patrol"))["lifecycle_status"] == "EXECUTING"
    )
    send(runtime, client, "stop_patrol")
    assert command(client, send(runtime, client, "close"))["lifecycle_status"] == "SUCCEEDED"
    send(runtime, client, "start", {"with_navigation": True})
    slot = client.get("/api/v1/monitor/snapshot").json()["parking_slots"][0]
    nav = send(runtime, client, "navigate", {"spot": slot["code"]})
    assert command(client, nav)["lifecycle_status"] == "EXECUTING"
    with runtime.SessionLocal() as db:
        task = db.get(Task, nav["task_id"])
        assert task.status == "EXECUTING" and task.phase == "SPOT"
    assert runtime.mock.task["state"] == "spot"
    send(runtime, client, "stop_navigate")
    with runtime.SessionLocal() as db:
        assert db.get(Task, nav["task_id"]).status == "CANCELLED"
    home = send(runtime, client, "homing")
    assert command(client, home)["lifecycle_status"] == "EXECUTING"
    runtime.mock.arrive()
    runtime.send("status", runtime.mock.status())
    runtime.pump()
    assert command(client, home)["lifecycle_status"] == "SUCCEEDED"
    with runtime.SessionLocal() as db:
        actions = set(db.scalars(select(AuditLog.action)).all())
    assert {
        "VEHICLE_AGENT_" + a.upper()
        for a in [
            "start",
            "close",
            "patrol",
            "stop_patrol",
            "resume_patrol",
            "homing",
            "navigate",
            "stop_navigate",
        ]
    } <= actions


def test_http_idempotency_and_pending_double_click(runtime, client):
    key = str(uuid4())
    path = "/api/v1/robots/R001/agent/commands/start"
    body = {"params": {"with_navigation": True}}
    first = client.post(path, json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 202
    again = client.post(path, json=body, headers={"Idempotency-Key": key})
    assert again.json()["command_id"] == first.json()["command_id"]
    repeated = client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
    assert repeated.json()["error"]["code"] == "COMMAND_PENDING"
    runtime.cycle()
    assert command(client, first.json())["ack_reason"] == "ALREADY_RUNNING"


def test_ack_failure_timeout_and_no_fake_success(runtime, client):
    runtime.mock.failure = "BUSY"
    rejected = send(runtime, client, "start", {"with_navigation": True})
    row = command(client, rejected)
    assert row["lifecycle_status"] == "ACK_REJECTED"
    assert row["ack_reason"] == "BUSY" and "mock start: BUSY" == row["ack_payload_json"]["message"]
    runtime.mock.failure = "TIMEOUT"
    pending = send(runtime, client, "start", {"with_navigation": True})
    assert command(client, pending)["lifecycle_status"] == "PUBLISHED"
    correlation = runtime.redis.zrange("vehicle-agent:v1:pending", 0, -1)[0]
    runtime.redis.zadd("vehicle-agent:v1:pending", {correlation: 0})
    runtime.adapter.expire()
    runtime.pump()
    assert command(client, pending)["ack_reason"] == "ACK_MISSING"
    assert command(client, pending)["ack_status"] is None
    assert command(client, pending)["ack_at"] is None
    assert command(client, pending)["lifecycle_status"] == "PUBLISHED_UNCONFIRMED"
    runtime.mock.failure = None


def test_offline_stale_reconnect_and_normalized_telemetry(runtime, client):
    runtime.send("availability", "offline")
    runtime.send("telemetry", runtime.mock.telemetry())
    runtime.pump()
    state = client.get("/api/v1/monitor/snapshot").json()["robots"][0]
    assert state["online_state"] == "OFFLINE"
    assert state["agent_state"]["telemetry"]["battery"]["percentage"] == 73
    assert not any(state["vehicle_agent_ready"].values())
    runtime.send("availability", "online")
    runtime.pump()
    reconnecting = client.get("/api/v1/monitor/snapshot").json()["robots"][0]
    assert reconnecting["online_state"] == "ONLINE"
    assert not any(reconnecting["vehicle_agent_ready"].values())
    runtime.send("status", runtime.mock.status())
    runtime.pump()
    state = client.get("/api/v1/monitor/snapshot").json()["robots"][0]
    assert state["online_state"] == "ONLINE"
    assert state["vehicle_agent_ready"]["start"]
    assert state["agent_state"]["receive_latency_ms"] is not None


def test_backend_navigation_resume_and_protocol_gates(runtime, client):
    from app.db.models import Robot
    from app.modules.commands.vehicle_agent_gate import gate_reason

    with runtime.SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        original = robot.agent_state_json
        for state, paused in [("patrol", False), ("spot", False), ("homing", False)]:
            status = {**original["status"], "task": {"state": state, "patrol_paused": paused}}
            robot.agent_state_json = {**original, "status": status}
            assert gate_reason(db, robot, "navigate") == "BUSY"
        robot.agent_state_json = original
        assert gate_reason(db, robot, "resume_patrol") == "PATROL_NOT_PAUSED"
        assert (
            gate_reason(db, robot, "navigate", now=datetime.now(UTC) + timedelta(seconds=30))
            == "VEHICLE_AGENT_STATUS_STALE"
        )
    response = client.put("/api/v1/robots/R001/agent-protocol", json={"protocol": "CANONICAL_MQTT"})
    assert response.status_code != 200
    assert response.json()["error"]["code"] == "AGENT_BINDING_BUSY"


def test_legacy_canonical_transport_and_v1_mutual_exclusion(runtime):
    from app.db.models import Robot
    from app.modules.commands.service import build_command_payload

    with runtime.SessionLocal() as db:
        v1 = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        legacy = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        assert legacy.agent_protocol == "CANONICAL_MQTT"
        legacy.boot_id = str(uuid4())
        payload = build_command_payload(
            robot=legacy, operator_id="test", cmd="patrol", params={}, ttl_ms=30000, priority=50
        )
        db.commit()
        assert payload["schema_version"] == "1.2"
        runtime.dispatcher.publish_command(payload, qos=1)
        assert runtime.published[-1][0] == "robot/R002/command"
        payload["vehicle_id"] = "R001"
        payload["target_boot_id"] = v1.boot_id
        with pytest.raises(ValueError, match="legacy command forbidden"):
            runtime.dispatcher.publish_command(payload, qos=1)
    runtime.pump()


def test_real_websocket_replays_v1_status(runtime, client):
    ticket = client.post("/api/v1/auth/ws-ticket").json()["ticket"]
    runtime.send("status", runtime.mock.status())
    runtime.pump()
    with client.websocket_connect(f"/ws/v1/monitor?ticket={ticket}") as socket:
        for _ in range(200):
            event = socket.receive_json()
            if event.get("event_type") == "vehicle.agent_state":
                assert event["data"]["agent_protocol"] == "VEHICLE_AGENT_V1"
                break
        else:
            pytest.fail("no v1 websocket status replay")


def test_ack_without_new_status_times_out_as_unconfirmed(runtime, client):
    from app.db.models import Command

    from scripts.vehicle_agent_mock_runtime import load_service

    row = send(runtime, client, "close", pump=False)
    runtime.dispatcher.dispatch_next()
    name, payload, retained = runtime.messages.get_nowait()
    runtime.adapter.process(name, payload)
    name, payload, retained = runtime.messages.get_nowait()
    assert name == "v1/robot/R001/cmd"
    ack = runtime.mock.execute(json.loads(payload))
    runtime.send("ack", ack)
    runtime.pump()
    assert command(client, row)["lifecycle_status"] == "ACK_ACCEPTED"
    worker = load_service("task-worker")
    with runtime.SessionLocal.begin() as db:
        pending = db.scalar(select(Command).where(Command.command_id == row["command_id"]))
        pending.published_at = datetime.now(UTC) - timedelta(seconds=91)
        db.flush()
        worker.expire_sessions_and_commands(db, datetime.now(UTC))
    assert command(client, row)["lifecycle_status"] == "PUBLISHED_UNCONFIRMED"
    assert command(client, row)["ack_reason"] == "STATUS_CONFIRMATION_TIMEOUT"


def test_explicit_offline_binding_resets_verification_and_requires_auth(runtime, client):
    from app.db.models import Robot, RobotIntegrationProfile

    with TestClient(runtime.app) as unauthenticated:
        response = unauthenticated.put(
            "/api/v1/robots/R002/agent-protocol", json={"protocol": "VEHICLE_AGENT_V1"}
        )
        assert response.status_code == 401
    with runtime.SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        robot.online_state = "OFFLINE"
    response = client.put(
        "/api/v1/robots/R002/agent-protocol", json={"protocol": "VEHICLE_AGENT_V1"}
    )
    assert response.status_code == 200
    assert response.json()["agent_protocol"] == "VEHICLE_AGENT_V1"
    assert response.json()["boot_id"] is None
    with runtime.SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        integration = db.get(RobotIntegrationProfile, robot.id)
        assert not integration.control_contract_verified
        assert not integration.ack_contract_verified


def test_stop_remains_available_when_motion_ack_is_lost(runtime, client):
    from app.db.models import Task

    runtime.mock.programs = dict.fromkeys(runtime.mock.programs, True)
    runtime.send("status", runtime.mock.status())
    runtime.pump()
    row = send(runtime, client, "patrol", pump=False)
    runtime.dispatcher.dispatch_next()
    name, payload, _ = runtime.messages.get_nowait()
    runtime.adapter.process(name, payload)
    name, payload, _ = runtime.messages.get_nowait()
    runtime.mock.execute(json.loads(payload))  # executed, deliberately drop its ACK
    runtime.send("status", runtime.mock.status())
    runtime.pump()
    assert command(client, row)["lifecycle_status"] == "PUBLISHED"
    stopped = send(runtime, client, "stop_patrol")
    assert command(client, stopped)["lifecycle_status"] == "SUCCEEDED"
    with runtime.SessionLocal() as db:
        assert db.get(Task, row["task_id"]).status == "CANCELLED"
