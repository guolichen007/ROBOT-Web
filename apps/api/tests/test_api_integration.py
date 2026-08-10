from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.core.config import get_settings
from app.core.events import append_event, get_redis
from app.core.security import hash_password, verify_password
from app.db.models import (
    Command,
    FireEvent,
    Map,
    MapVersion,
    OutboxEvent,
    ParkingSlot,
    Robot,
    Task,
    User,
)
from app.db.session import SessionLocal
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from starlette.websockets import WebSocketDisconnect


@pytest.fixture(autouse=True)
def stable_baseline() -> None:
    get_redis().flushdb()
    with SessionLocal.begin() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin
        test_password = get_settings().effective_admin_password
        if not verify_password(test_password, admin.password_hash):
            admin.password_hash = hash_password(test_password)
        admin.must_change_password = False
        admin.failed_attempts = 0
        admin.locked_until = None
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        active_map = db.scalar(select(Map).where(Map.active_version_id.is_not(None)))
        assert active_map
        version = db.get(MapVersion, active_map.active_version_id)
        assert version
        robot.online_state = "ONLINE"
        robot.last_seen_at = datetime.now(UTC)
        robot.estop_active = False
        robot.current_map_id = active_map.id
        robot.current_map_version = version.version
        db.execute(
            update(Task)
            .where(Task.status.in_({"CREATED", "QUEUED", "ACCEPTED", "EXECUTING"}))
            .values(status="SUCCEEDED", phase="TEST_CLEANUP", completed_at=datetime.now(UTC))
        )


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as value:
        yield value


def login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": get_settings().effective_admin_password},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def auth(token: str, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **headers}


def target() -> tuple[Robot, ParkingSlot, MapVersion]:
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        slot = db.scalar(select(ParkingSlot).where(ParkingSlot.code == "A-12"))
        assert robot and slot
        version = db.get(MapVersion, slot.map_version_id)
        assert version
        db.expunge(robot)
        db.expunge(slot)
        db.expunge(version)
        return robot, slot, version


def test_refresh_rotation_reuse_revokes_family(client: TestClient) -> None:
    login(client)
    old_refresh = client.cookies.get("refresh_token")
    old_csrf = client.cookies.get("csrf_token")
    assert old_refresh and old_csrf

    rotated = client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": old_csrf})
    assert rotated.status_code == 200
    new_refresh = client.cookies.get("refresh_token")
    new_csrf = client.cookies.get("csrf_token")
    assert new_refresh and new_refresh != old_refresh and new_csrf

    with TestClient(app) as replay:
        replay.cookies.set("refresh_token", old_refresh)
        replay.cookies.set("csrf_token", old_csrf)
        rejected = replay.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": old_csrf})
        assert rejected.status_code == 401

    revoked = client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": new_csrf})
    assert revoked.status_code == 401


def test_manual_lease_mutex_sequence_and_release(client: TestClient) -> None:
    token = login(client)
    first = client.post(
        "/api/v1/robots/R001/manual-lease",
        json={"control_session_id": str(uuid4())},
        headers=auth(token),
    )
    assert first.status_code == 201
    lease = first.json()

    second = client.post(
        "/api/v1/robots/R001/manual-lease",
        json={"control_session_id": str(uuid4())},
        headers=auth(token),
    )
    assert second.status_code == 409

    pulse = {
        "lease_id": lease["lease_id"],
        "control_session_id": lease["control_session_id"],
        "seq": 1,
        "linear": 0.2,
        "angular": 0,
    }
    accepted = client.post("/api/v1/robots/R001/commands/manual", json=pulse, headers=auth(token))
    assert accepted.status_code == 200 and accepted.json()["accepted"] is True
    duplicate = client.post("/api/v1/robots/R001/commands/manual", json=pulse, headers=auth(token))
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "accepted": False,
        "reason": "DUPLICATE_OR_OUT_OF_ORDER",
        "last_seq": 1,
    }

    released = client.delete("/api/v1/robots/R001/manual-lease", headers=auth(token))
    assert released.status_code == 204


def test_manual_lease_blocks_autonomous_task(client: TestClient) -> None:
    token = login(client)
    lease = client.post(
        "/api/v1/robots/R001/manual-lease",
        json={"control_session_id": str(uuid4())},
        headers=auth(token),
    )
    assert lease.status_code == 201
    _, slot, _ = target()
    response = client.post(
        "/api/v1/tasks/patrol",
        json={"robot_id": "R001", "target_parking_slot_id": slot.id},
        headers=auth(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert response.status_code == 409
    client.delete("/api/v1/robots/R001/manual-lease", headers=auth(token))


def test_logout_releases_lease_and_queues_stop(client: TestClient) -> None:
    token = login(client)
    robot, _, _ = target()
    lease = client.post(
        "/api/v1/robots/R001/manual-lease",
        json={"control_session_id": str(uuid4())},
        headers=auth(token),
    )
    assert lease.status_code == 201
    csrf = client.cookies.get("csrf_token")
    assert csrf
    logged_out = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logged_out.status_code == 204
    assert not get_redis().exists(f"manual:lease:{robot.id}")
    with SessionLocal() as db:
        stop = db.scalar(
            select(Command)
            .where(Command.robot_id == robot.id, Command.cmd == "stop_motion")
            .order_by(Command.issued_at.desc())
        )
        assert stop and stop.payload_json["params"]["reason"] == "USER_LOGOUT"


def test_task_idempotency_outbox_and_map_mismatch(client: TestClient) -> None:
    token = login(client)
    robot, slot, _ = target()
    key = str(uuid4())
    payload = {"robot_id": "R001", "target_parking_slot_id": slot.id}
    first = client.post(
        "/api/v1/tasks/patrol",
        json=payload,
        headers=auth(token, **{"Idempotency-Key": key}),
    )
    assert first.status_code == 201
    replay = client.post(
        "/api/v1/tasks/patrol",
        json=payload,
        headers=auth(token, **{"Idempotency-Key": key}),
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    with SessionLocal() as db:
        command = db.scalar(select(Command).where(Command.command_id == first.json()["command_id"]))
        assert command and command.lifecycle_status == "QUEUED"
        assert db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == command.command_id))

    with SessionLocal.begin() as db:
        current = db.get(Robot, robot.id)
        assert current
        current.current_map_version = "MISMATCH"
        db.execute(update(Task).where(Task.id == first.json()["id"]).values(status="SUCCEEDED"))
    mismatch = client.post(
        "/api/v1/tasks/patrol",
        json=payload,
        headers=auth(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["robot_map_version"] == "MISMATCH"


def test_manual_alarm_idempotency_lifecycle_and_audit(client: TestClient) -> None:
    token = login(client)
    _, slot, version = target()
    key = str(uuid4())
    payload = {
        "parking_slot_id": slot.id,
        "fire_type": "smoke",
        "note": "integration test",
        "map_version": version.version,
    }
    created = client.post(
        "/api/v1/alarms/manual",
        json=payload,
        headers=auth(token, **{"Idempotency-Key": key}),
    )
    assert created.status_code == 201
    replay = client.post(
        "/api/v1/alarms/manual",
        json=payload,
        headers=auth(token, **{"Idempotency-Key": key}),
    )
    assert replay.status_code == 201 and replay.json()["id"] == created.json()["id"]
    alarm_id = created.json()["id"]
    assert created.json()["state"] == "NEW"
    for action, state in (
        ("acknowledge", "ACKNOWLEDGED"),
        ("confirm", "CONFIRMED"),
        ("resolve", "RESOLVED"),
    ):
        transitioned = client.post(f"/api/v1/alarms/{alarm_id}/{action}", headers=auth(token))
        assert transitioned.status_code == 200 and transitioned.json()["state"] == state
    with SessionLocal() as db:
        assert db.get(FireEvent, alarm_id).state == "RESOLVED"  # type: ignore[union-attr]


def test_snapshot_watermark_replay_and_one_time_ws_ticket(client: TestClient) -> None:
    token = login(client)
    snapshot = client.get("/api/v1/monitor/snapshot", headers=auth(token))
    assert snapshot.status_code == 200
    watermark = snapshot.json()["snapshot_watermark"]
    emitted = append_event("vehicle.location", {"vehicle_id": "R001", "x": 9, "y": 8})
    ticket = client.post("/api/v1/auth/ws-ticket", headers=auth(token)).json()["ticket"]
    with client.websocket_connect(
        f"/ws/v1/monitor?ticket={ticket}&after={watermark}",
    ) as websocket:
        replayed = []
        for _ in range(20):
            event = websocket.receive_json()
            replayed.append(event)
            if event["stream_id"] == emitted:
                break
        assert any(item["stream_id"] == emitted for item in replayed)
        assert next(item for item in replayed if item["stream_id"] == emitted)["data"]["x"] == 9
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/ws/v1/monitor?ticket={ticket}&after={watermark}",
        ):
            pass


def test_health_ready_and_offline_estop_semantics(client: TestClient) -> None:
    token = login(client)
    ready = client.get("/health/ready")
    assert ready.status_code == 503 and ready.json()["ok"] is False
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        robot.online_state = "OFFLINE"
    command = client.post(
        "/api/v1/robots/R001/commands/emergency-stop",
        json={},
        headers=auth(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert command.status_code == 202
    assert command.json()["lifecycle_status"] == "PUBLISHED_UNCONFIRMED"
    assert command.json()["ack_reason"] == "OFFLINE_NOT_DELIVERED"
