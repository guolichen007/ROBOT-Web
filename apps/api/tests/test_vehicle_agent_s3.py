"""Deterministic cadence, ACK truth, readiness and dispatcher counterexamples."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from scripts.vehicle_agent_mock_runtime import MockRuntime, load_service


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("FIREBOT_MOCK_E2E", "1")
    value = MockRuntime(tmp_path / "s3.sqlite")
    yield value
    value.close()


def robot_in(db):
    from app.db.models import Robot

    return db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))


def create(runtime, action="start"):
    from app.db.models import Map, MapVersion, Task, User
    from app.modules.commands.service import create_durable_command

    with runtime.SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.username == "admin"))
        task_id = None
        if action == "patrol":
            map_row = db.get(Map, robot_in(db).current_map_id)
            version = db.get(MapVersion, map_row.active_version_id)
            task = Task(
                robot_id=robot_in(db).id,
                type="V1_PATROL",
                task_code="S3-" + str(datetime.now(UTC).timestamp()),
                status="QUEUED",
                map_id_snapshot=map_row.id,
                map_version_snapshot=version.version,
                semantic_revision_snapshot=version.semantic_revision,
                created_by=user.id,
            )
            db.add(task)
            db.flush()
            task_id = task.id
        row = create_durable_command(
            db,
            robot=robot_in(db),
            operator_id=user.id,
            cmd=action,
            params={"with_navigation": True} if action == "start" else {},
            ttl_ms=90000,
            priority=90 if action.startswith("stop_") else 50,
            task_id=task_id,
        )
        db.flush()
        payload = dict(row.payload_json)
    return payload


def test_real_5s_status_1s_telemetry_cadence_and_explicit_offline(runtime):
    from app.modules.commands.vehicle_agent_gate import gate_reason

    from services import vehicle_agent_ingress as ingress

    worker = load_service("task-worker")
    start = datetime.now(UTC)
    clock = start

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    with patch.object(ingress, "datetime", Clock):
        for second in range(26):
            clock = start + timedelta(seconds=second)
            if second % 5 == 0:
                runtime.send("status", {**runtime.mock.status(), "ts": clock.isoformat()})
            runtime.send("telemetry", {**runtime.mock.telemetry(), "ts": clock.isoformat()})
            runtime.pump()
            with runtime.SessionLocal.begin() as db:
                worker.reconcile_robot_states(db, clock)
                robot = robot_in(db)
                assert robot.online_state == "ONLINE"
                assert gate_reason(db, robot, "start", now=clock) == ""
        # Continuous telemetry proves liveness, never control-state freshness.
        for second in range(26, 36):
            clock = start + timedelta(seconds=second)
            runtime.send("telemetry", {**runtime.mock.telemetry(), "ts": clock.isoformat()})
            runtime.pump()
        with runtime.SessionLocal.begin() as db:
            worker.reconcile_robot_states(db, clock)
            assert robot_in(db).online_state == "ONLINE"
            assert gate_reason(db, robot_in(db), "start", now=clock) == "VEHICLE_AGENT_STATUS_STALE"
        runtime.send("status", {**runtime.mock.status(), "ts": clock.isoformat()})
        runtime.pump()
        with runtime.SessionLocal() as db:
            assert gate_reason(db, robot_in(db), "start", now=clock) == ""
        runtime.send("availability", "offline")
        runtime.send("telemetry", {**runtime.mock.telemetry(), "ts": start.isoformat()})
        runtime.send("status", {**runtime.mock.status(), "ts": start.isoformat()})
        runtime.pump()
        with runtime.SessionLocal() as db:
            assert robot_in(db).online_state == "OFFLINE"
        runtime.send("availability", "online")
        runtime.pump()
        with runtime.SessionLocal.begin() as db:
            worker.reconcile_robot_states(db, clock + timedelta(seconds=16))
            assert robot_in(db).online_state == "OFFLINE"


@pytest.mark.parametrize("late_code", ["OK", "BUSY"])
@pytest.mark.parametrize("seconds", [89.999, 90, 90.001])
def test_ack_timeout_boundary_and_late_real_ack(runtime, late_code, seconds):
    from app.db.models import Command

    payload = create(runtime)
    runtime.dispatcher.dispatch_next()
    name, wire, _ = runtime.messages.get_nowait()
    runtime.adapter.process(name, wire)
    runtime.messages.get_nowait()  # drop vehicle delivery; inject exact ACK below
    pending = runtime.redis.zrange("vehicle-agent:v1:pending", 0, -1)[0]
    entry = json.loads(runtime.redis.get(pending))
    with patch("time.time", return_value=entry["deadline"] - 90 + seconds):
        runtime.adapter.expire()
    runtime.pump()
    with runtime.SessionLocal() as db:
        command = db.scalar(select(Command).where(Command.command_id == payload["command_id"]))
        assert command.ack_at is None and command.ack_status is None
        assert command.lifecycle_status == (
            "PUBLISHED" if seconds < 90 else "PUBLISHED_UNCONFIRMED"
        )
    ack = {
        "msg_id": payload["message_id"],
        "success": late_code == "OK",
        "code": late_code,
        "message": "real late ACK",
        "ts": datetime.now(UTC).isoformat(),
    }
    runtime.send("ack", ack)
    runtime.pump()
    with runtime.SessionLocal() as db:
        command = db.scalar(select(Command).where(Command.command_id == payload["command_id"]))
        assert command.ack_status == ("accepted" if late_code == "OK" else "rejected")
        assert command.ack_payload_json["ack_source"] == "vehicle"
        assert command.lifecycle_status == ("ACK_ACCEPTED" if late_code == "OK" else "ACK_REJECTED")


def test_ack_missing_status_observation_and_reboot_rejects_old_ack(runtime):
    from app.db.models import Command, Task

    runtime.mock.programs = dict.fromkeys(runtime.mock.programs, True)
    runtime.send("status", runtime.mock.status())
    runtime.pump()
    payload = create(runtime, "patrol")
    runtime.dispatcher.dispatch_next()
    name, wire, _ = runtime.messages.get_nowait()
    runtime.adapter.process(name, wire)
    _, wire, _ = runtime.messages.get_nowait()
    ack = runtime.mock.execute(json.loads(wire))
    # An old retained status must not confirm a newly published command.
    old = {**runtime.mock.status(), "ts": (datetime.now(UTC) - timedelta(seconds=30)).isoformat()}
    runtime.messages.put(("v1/robot/R001/status", json.dumps(old).encode(), True))
    runtime.pump()
    with runtime.SessionLocal() as db:
        row = db.scalar(select(Command).where(Command.command_id == payload["command_id"]))
        assert db.get(Task, row.task_id).status == "QUEUED"
    runtime.send("status", runtime.mock.status())
    runtime.pump()
    with runtime.SessionLocal() as db:
        row = db.scalar(select(Command).where(Command.command_id == payload["command_id"]))
        task = db.get(Task, row.task_id)
        assert (
            task.status == "EXECUTING" and task.parameters_json["ack_diagnostic"] == "ACK_MISSING"
        )
        assert row.ack_status is None and row.ack_at is None and row.ack_reason == "ACK_MISSING"
    runtime.send("availability", "offline")
    runtime.send("availability", "online")
    runtime.send("ack", ack)
    runtime.pump()
    with runtime.SessionLocal() as db:
        row = db.scalar(select(Command).where(Command.command_id == payload["command_id"]))
        assert row.ack_at is None


def test_adapter_health_gates_control_and_readiness(runtime):
    from app.modules.commands.vehicle_agent_gate import gate_reason
    from app.modules.system import router as system

    for stamp, healthy in [
        (datetime.now(UTC).isoformat(), True),
        (None, False),
        ((datetime.now(UTC) - timedelta(seconds=30)).isoformat(), False),
    ]:
        runtime.redis.delete("service:vehicle-agent-adapter:heartbeat")
        if stamp:
            runtime.redis.set("service:vehicle-agent-adapter:heartbeat", stamp)
        for name in ("mqtt-ingress", "task-worker"):
            runtime.redis.set(f"service:{name}:heartbeat", "alive")
        for name in ("outbox", "safety"):
            runtime.redis.set(f"service:command-dispatcher:{name}-heartbeat", "alive")
        with (
            runtime.SessionLocal() as db,
            patch.object(system, "bounded_tcp_probe", return_value=(True, None)),
        ):
            reason = gate_reason(db, robot_in(db), "start")
            assert reason == ("" if healthy else "VEHICLE_AGENT_ADAPTER_UNAVAILABLE")
            result = system.readiness_payload(db)
            assert result["checks"]["vehicle_agent_adapter"] == {"ok": healthy, "required": True}
            assert result["ok"] is healthy


@pytest.mark.parametrize("stop", ["stop_patrol", "stop_navigate"])
def test_stop_overtakes_older_business_backlog_and_same_priority_fifo(runtime, stop):
    normal = [create(runtime, "get_status") for _ in range(3)]
    urgent = create(runtime, stop)
    runtime.published.clear()
    for expected in [urgent, *normal]:
        assert runtime.dispatcher.dispatch_next()
        sent = json.loads(runtime.published[-1][1])
        assert sent["command_id"] == expected["command_id"]
        assert sent["cmd"] == expected["cmd"]
