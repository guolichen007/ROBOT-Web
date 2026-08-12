from __future__ import annotations

import importlib.util
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import jwt
import pytest
from app.core import events
from app.core.config import get_settings
from app.core.events import get_redis, queue_event, queue_redis_set
from app.core.logging import JsonFormatter
from app.core.security import hash_password, verify_password
from app.db.migration import escape_alembic_url
from app.db.models import Map, MapVersion, Robot, RobotBootSession, Site, User
from app.db.partitions import default_partition_counts, partition_name
from app.db.session import SessionLocal
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[3]
BOOT_A = "00000000-0000-4000-8000-000000000002"
BOOT_B = "00000000-0000-4000-8000-000000000003"


def test_json_logs_keep_service_name_and_exception_traceback() -> None:
    formatter = JsonFormatter("task-worker")
    try:
        raise RuntimeError("dependency unavailable")
    except RuntimeError:
        record = logging.LogRecord(
            "worker",
            logging.ERROR,
            __file__,
            1,
            "cycle failed",
            (),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert payload["service"] == "task-worker"
    assert "RuntimeError: dependency unavailable" in payload["exception"]


def test_alembic_url_preserves_percent_encoded_secret() -> None:
    escaped = escape_alembic_url("postgresql+psycopg://firebot:password%21@postgres:5432/firebot")
    assert escaped == "postgresql+psycopg://firebot:password%%21@postgres:5432/firebot"


def load_service(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def stable_hardening_baseline() -> None:
    get_redis().flushdb()
    with SessionLocal.begin() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        active_map = db.scalar(select(Map).where(Map.active_version_id.is_not(None)))
        assert admin and robot and active_map
        version = db.get(MapVersion, active_map.active_version_id)
        assert version
        password = get_settings().effective_admin_password
        if not verify_password(password, admin.password_hash):
            admin.password_hash = hash_password(password)
        admin.status = "ACTIVE"
        admin.must_change_password = False
        robot.online_state = "ONLINE"
        robot.boot_id = BOOT_A
        robot.current_map_id = active_map.id
        robot.current_map_version = version.version


def login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": get_settings().effective_admin_password},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def test_commit_failure_does_not_publish_realtime_success(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        events, "append_event", lambda kind, payload: published.append((kind, payload))
    )
    db = SessionLocal()
    try:
        db.add(Site(code="DEMO_PARKING", name="duplicate"))
        queue_event(db, "site.created", {"code": "DEMO_PARKING"})
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()
    assert published == []


def test_post_commit_cache_failure_does_not_make_database_commit_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenRedis:
        def set(self, *_args, **_kwargs):
            raise ConnectionError("fault injection")

    code = f"COMMIT-{uuid4()}"
    monkeypatch.setattr(events, "get_redis", lambda: BrokenRedis())
    with SessionLocal() as db:
        db.add(Site(code=code, name="commit proof"))
        queue_redis_set(db, f"proof:{code}", "1")
        db.commit()
    with SessionLocal.begin() as db:
        row = db.scalar(select(Site).where(Site.code == code))
        assert row is not None
        db.delete(row)


def test_missing_boot_rejects_stop_but_allows_unconfirmed_estop() -> None:
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        robot.boot_id = None
    with TestClient(app) as client:
        token = login(client)
        stop = client.post(
            "/api/v1/robots/R001/commands/stop-motion",
            json={},
            headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
        )
        assert stop.status_code == 409
        assert stop.json()["error"]["code"] == "ROBOT_BOOT_SESSION_UNKNOWN"
        estop = client.post(
            "/api/v1/robots/R001/commands/emergency-stop",
            json={},
            headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
        )
        assert estop.status_code == 202
        assert estop.json()["payload_json"]["target_boot_id"] is None
        assert estop.json()["lifecycle_status"] != "SUCCEEDED"


def test_boot_session_switch_ends_old_boot_and_prevents_replay() -> None:
    ingress = load_service("firebot_mqtt_ingress_test", "services/mqtt-ingress/main.py")
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        db.query(RobotBootSession).filter(RobotBootSession.robot_id == robot.id).delete()
        db.add(
            RobotBootSession(
                robot_id=robot.id,
                boot_id=BOOT_A,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        assert ingress.accept_boot_session(
            db, robot, {"boot_id": BOOT_B, "type": "heartbeat"}, now + timedelta(seconds=1)
        )
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot and robot.boot_id == BOOT_B
        assert not ingress.accept_boot_session(
            db, robot, {"boot_id": BOOT_A, "type": "heartbeat"}, now + timedelta(seconds=2)
        )


def test_ingress_depth_video_and_rate_protection() -> None:
    ingress = load_service("firebot_mqtt_ingress_protection", "services/mqtt-ingress/main.py")
    assert ingress.json_depth({"a": {"b": [1]}}) == 3
    assert ingress.contains_video_payload({"frame": "data:image/jpeg;base64,AAAA"})
    assert not ingress.contains_video_payload({"value": "ordinary telemetry"})
    allowed = [ingress.rate_allowed("R-RATE", "location") for _ in range(31)]
    assert all(allowed[:30])
    assert allowed[30] is False
    assert ingress.dedup_ttl("location") == 120
    assert ingress.dedup_ttl("heartbeat") == 120
    assert ingress.dedup_ttl("status") == 600
    assert ingress.dedup_ttl("alarm") == 86400


def test_dispatcher_consumer_identity_is_unique() -> None:
    dispatcher = load_service(
        "firebot_command_dispatcher_test", "services/command-dispatcher/main.py"
    )
    first = dispatcher.dispatcher_instance_id()
    second = dispatcher.dispatcher_instance_id()
    assert first != second
    assert first.startswith("command-dispatcher-")


def test_current_and_future_partitions_are_real_and_default_empty() -> None:
    now = datetime.now(UTC)
    future = now + timedelta(days=62)
    with SessionLocal() as db:
        names = set(
            db.execute(
                text(
                    """
                    SELECT c.relname
                    FROM pg_inherits i
                    JOIN pg_class p ON p.oid = i.inhparent
                    JOIN pg_class c ON c.oid = i.inhrelid
                    WHERE p.relname IN ('telemetry_samples', 'sensor_samples')
                    """
                )
            ).scalars()
        )
        assert partition_name("telemetry_samples", now) in names
        assert partition_name("sensor_samples", now) in names
        assert partition_name("telemetry_samples", future) in names
        assert partition_name("sensor_samples", future) in names
        assert default_partition_counts(db) == {
            "telemetry_samples": 0,
            "sensor_samples": 0,
        }


def test_media_ticket_authorization_expiry_binding_and_revocation() -> None:
    with TestClient(app) as client:
        token = login(client)
        streams = client.get("/api/v1/media/streams", headers=bearer(token))
        assert streams.status_code == 200 and streams.json()
        stream = streams.json()[0]
        issued = client.post(
            "/api/v1/media/tickets",
            json={"stream_id": stream["stream_id"]},
            headers=bearer(token),
        )
        assert issued.status_code == 200
        ticket = issued.json()["ticket"]
        valid = client.post(
            "/api/v1/media/authorize",
            json={
                "action": "read",
                "path": stream["stream_id"],
                "token": ticket,
            },
        )
        assert valid.status_code == 200
        query_token = client.post(
            "/api/v1/media/authorize",
            json={
                "action": "read",
                "path": stream["stream_id"],
                "query": f"?token={ticket}",
            },
        )
        assert query_token.status_code == 401
        wrong = client.post(
            "/api/v1/media/authorize",
            json={"action": "read", "path": "another-stream", "token": ticket},
        )
        assert wrong.status_code == 403
        claims = jwt.decode(
            ticket,
            get_settings().effective_jwt_secret,
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        claims["iat"] = datetime.now(UTC) - timedelta(minutes=2)
        claims["exp"] = datetime.now(UTC) - timedelta(minutes=1)
        expired = jwt.encode(claims, get_settings().effective_jwt_secret, algorithm="HS256")
        rejected = client.post(
            "/api/v1/media/authorize",
            json={"action": "read", "path": stream["stream_id"], "token": expired},
        )
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "AUTH_REQUIRED"

        with SessionLocal.begin() as db:
            admin = db.scalar(select(User).where(User.username == "admin"))
            assert admin
            admin.status = "DISABLED"
        revoked = client.post(
            "/api/v1/media/authorize",
            json={"action": "read", "path": stream["stream_id"], "token": ticket},
        )
        assert revoked.status_code == 403


def test_media_publish_requires_registered_path_and_secret() -> None:
    with TestClient(app) as client:
        no_secret = client.post(
            "/api/v1/media/authorize",
            json={"action": "publish", "path": "R001-roof_rgb"},
        )
        assert no_secret.status_code == 401
        unknown = client.post(
            "/api/v1/media/authorize",
            json={
                "action": "publish",
                "path": "unknown",
                "token": get_settings().effective_media_publish_token,
            },
        )
        assert unknown.status_code == 403
