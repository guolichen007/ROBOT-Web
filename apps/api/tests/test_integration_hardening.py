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
from app.db.models import (
    Command,
    Map,
    MapVersion,
    PatrolPlan,
    PatrolSchedule,
    PatrolScheduleOccurrence,
    Robot,
    RobotBootSession,
    RobotCapability,
    RobotDataChannel,
    RobotIntegrationProfile,
    RobotMotionProfile,
    Site,
    StopOperation,
    Task,
    User,
)
from app.db.partitions import default_partition_counts, partition_name
from app.db.session import SessionLocal
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
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
        robot.estop_active = False
        robot.current_map_id = active_map.id
        robot.current_map_version = version.version
        integration = db.get(RobotIntegrationProfile, robot.id)
        if integration:
            integration.source_kind = "MOCK"
            integration.control_contract_verified = True
            integration.ack_contract_verified = True
            integration.map_contract_verified = True
            integration.bidirectional_bridge_verified = True
            integration.command_path_verified = True
            integration.cmd_vel_arbitration_verified = True
        capability = db.get(RobotCapability, robot.id)
        if capability:
            capability.supported_commands_json = [
                "manual_control",
                "stop_motion",
                "emergency_stop",
                "reset_estop",
                "return_dock",
                "patrol",
                "extinguish",
                "cancel_task",
            ]
        motion = db.get(RobotMotionProfile, robot.id)
        if motion:
            motion.manual_watchdog_verified = True
            motion.max_manual_forward_mps = 0.22
            motion.max_manual_reverse_mps = 0.16
            motion.max_manual_angular_radps = 0.65
            motion.reverse_allowed = True
        db.execute(
            update(Task)
            .where(Task.status.in_({"CREATED", "QUEUED", "ACCEPTED", "EXECUTING"}))
            .values(status="SUCCEEDED", phase="TEST_CLEANUP", completed_at=datetime.now(UTC))
        )


def login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": get_settings().effective_admin_password},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def test_stop_patrol_ros_compat_has_zero_side_effects_with_and_without_task() -> None:
    with TestClient(app) as client:
        token = login(client)
        for active_task in (False, True):
            with SessionLocal.begin() as db:
                robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
                profile = db.get(RobotIntegrationProfile, robot.id) if robot else None
                assert robot and profile
                profile.source_kind = "ROS_COMPAT"
                profile.bidirectional_bridge_verified = False
                if active_task:
                    admin = db.scalar(select(User).where(User.username == "admin"))
                    assert admin
                    task = Task(
                        task_code=f"ROS-STOP-{uuid4()}",
                        robot_id=robot.id,
                        type="PATROL",
                        status="EXECUTING",
                        phase="EXECUTING",
                        progress=20,
                        target_pose_snapshot_json={},
                        map_id_snapshot=robot.current_map_id or "map",
                        map_version_snapshot=robot.current_map_version or "1",
                        semantic_revision_snapshot=1,
                        parameters_json={},
                        created_by=admin.id,
                    )
                    db.add(task)
            before_commands = 0
            with SessionLocal() as db:
                before_commands = len(db.scalars(select(Command)).all())
            response = client.post(
                "/api/v1/robots/R001/stop-patrol",
                headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "ROS_COMPAT_READ_ONLY"
            with SessionLocal() as db:
                assert len(db.scalars(select(Command)).all()) == before_commands
            assert get_redis().xlen("firebot:safety_commands") == 0
            with SessionLocal.begin() as db:
                db.execute(
                    update(Task)
                    .where(Task.status.in_({"CREATED", "QUEUED", "ACCEPTED", "EXECUTING"}))
                    .values(status="SUCCEEDED", phase="TEST_CLEANUP")
                )


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


def _task_for_stop(db, *, robot: Robot, user: User, version: MapVersion) -> Task:
    task = Task(
        task_code=f"STOP-TEST-{uuid4()}",
        robot_id=robot.id,
        type="PATROL",
        status="EXECUTING",
        phase="PATROL_RUNNING",
        progress=20,
        target_pose_snapshot_json={"x": 1, "y": 1, "theta": 0},
        map_id_snapshot=version.map_id,
        map_version_snapshot=version.version,
        semantic_revision_snapshot=version.semantic_revision,
        parameters_json={},
        created_by=user.id,
    )
    db.add(task)
    db.flush()
    return task


def _command_for_stop(
    db,
    *,
    robot: Robot,
    user: User,
    cmd: str,
    task: Task | None,
    ack_status: str | None,
) -> Command:
    now = datetime.now(UTC)
    row = Command(
        command_id=f"C-{uuid4()}",
        correlation_id=str(uuid4()),
        robot_id=robot.id,
        task_id=task.id if task else None,
        cmd=cmd,
        priority=99,
        payload_json={"cmd": cmd},
        lifecycle_status="ACK_ACCEPTED" if ack_status else "PUBLISHED",
        issued_by=user.id,
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        published_at=now,
        ack_status=ack_status,
    )
    db.add(row)
    db.flush()
    return row


def test_stop_operation_keeps_physical_stop_separate_from_cancel_timeout() -> None:
    worker = load_service("firebot_stop_split_test", "services/task-worker/main.py")
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        user = db.scalar(select(User).where(User.username == "admin"))
        active_map = db.scalar(select(Map).where(Map.active_version_id.is_not(None)))
        assert robot and user and active_map and active_map.active_version_id
        version = db.get(MapVersion, active_map.active_version_id)
        assert version
        task = _task_for_stop(db, robot=robot, user=user, version=version)
        stop = _command_for_stop(
            db, robot=robot, user=user, cmd="stop_motion", task=task, ack_status="accepted"
        )
        cancel = _command_for_stop(
            db, robot=robot, user=user, cmd="cancel_task", task=task, ack_status="accepted"
        )
        operation = StopOperation(
            robot_id=robot.id,
            task_id=task.id,
            stop_command_id=stop.command_id,
            cancel_command_id=cancel.command_id,
            state="STATIONARY_CONFIRMED_CANCEL_PENDING",
            motion_stop_state="STATIONARY_CONFIRMED",
            mission_cancel_state="ACK_ACCEPTED",
            requested_by=user.id,
            stop_ack_deadline_at=now + timedelta(seconds=5),
            cancel_deadline_at=now - timedelta(milliseconds=1),
            stationary_verify_deadline_at=now + timedelta(seconds=5),
        )
        db.add(operation)
        db.flush()
        operation_id = operation.id
    with SessionLocal.begin() as db:
        worker.reconcile_stop_operations(db, now)
    with SessionLocal() as db:
        operation = db.get(StopOperation, operation_id)
        assert operation
        assert operation.motion_stop_state == "STATIONARY_CONFIRMED"
        assert operation.mission_cancel_state == "UNCONFIRMED"
        assert operation.state == "PARTIAL_UNCONFIRMED"
        assert operation.failure_reason == "TASK_CANCEL_TIMEOUT"


def test_stop_operation_stale_telemetry_terminates_unconfirmed() -> None:
    worker = load_service("firebot_stop_stale_test", "services/task-worker/main.py")
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        user = db.scalar(select(User).where(User.username == "admin"))
        assert robot and user
        stop = _command_for_stop(
            db, robot=robot, user=user, cmd="stop_motion", task=None, ack_status="accepted"
        )
        operation = StopOperation(
            robot_id=robot.id,
            stop_command_id=stop.command_id,
            state="VERIFYING_STATIONARY",
            motion_stop_state="VERIFYING_STATIONARY",
            mission_cancel_state="NOT_REQUIRED",
            requested_by=user.id,
            stop_ack_deadline_at=now + timedelta(seconds=5),
            stationary_verify_deadline_at=now - timedelta(milliseconds=1),
        )
        db.add(operation)
        db.flush()
        operation_id = operation.id
        vehicle_id = robot.vehicle_id
    get_redis().set(
        f"robot:{vehicle_id}:latest",
        json.dumps(
            {
                "server_received_at": (now - timedelta(seconds=5)).isoformat(),
                "linear_x": 0,
                "linear_y": 0,
                "angular_z": 0,
            }
        ),
    )
    with SessionLocal.begin() as db:
        worker.reconcile_stop_operations(db, now)
    with SessionLocal() as db:
        operation = db.get(StopOperation, operation_id)
        assert operation
        assert operation.state == "UNCONFIRMED"
        assert operation.failure_reason == "TELEMETRY_STALE"


def test_stop_operation_ack_deadline_terminates_unconfirmed() -> None:
    worker = load_service("firebot_stop_ack_timeout_test", "services/task-worker/main.py")
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        user = db.scalar(select(User).where(User.username == "admin"))
        assert robot and user
        stop = _command_for_stop(
            db, robot=robot, user=user, cmd="stop_motion", task=None, ack_status=None
        )
        operation = StopOperation(
            robot_id=robot.id,
            stop_command_id=stop.command_id,
            state="STOP_REQUESTED",
            motion_stop_state="WAITING_ACK",
            mission_cancel_state="NOT_REQUIRED",
            requested_by=user.id,
            stop_ack_deadline_at=now - timedelta(milliseconds=1),
            stationary_verify_deadline_at=now + timedelta(seconds=5),
        )
        db.add(operation)
        db.flush()
        operation_id = operation.id
    with SessionLocal.begin() as db:
        worker.reconcile_stop_operations(db, now)
    with SessionLocal() as db:
        operation = db.get(StopOperation, operation_id)
        assert operation and operation.state == "UNCONFIRMED"
        assert operation.failure_reason == "STOP_ACK_TIMEOUT"


def test_expired_queued_patrol_occurrence_is_never_dispatched() -> None:
    worker = load_service("firebot_queue_expiry_test", "services/task-worker/main.py")
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        plan = db.scalar(select(PatrolPlan).where(PatrolPlan.enabled.is_(True)))
        user = db.scalar(select(User).where(User.username == "admin"))
        assert plan and user
        schedule = PatrolSchedule(
            patrol_plan_id=plan.id,
            cron_expression="0 9 * * *",
            enabled=True,
            overlap_policy="QUEUE",
            queue_expiry_seconds=1,
            created_by=user.id,
        )
        db.add(schedule)
        db.flush()
        occurrence = PatrolScheduleOccurrence(
            schedule_id=schedule.id,
            scheduled_for=now - timedelta(minutes=1),
            state="QUEUED",
        )
        db.add(occurrence)
        db.flush()
        occurrence_id = occurrence.id
    with SessionLocal.begin() as db:
        occurrence = db.get(PatrolScheduleOccurrence, occurrence_id)
        assert occurrence
        assert worker._dispatch_schedule_occurrence(db, occurrence) is False
    with SessionLocal() as db:
        occurrence = db.get(PatrolScheduleOccurrence, occurrence_id)
        assert occurrence and occurrence.state == "SKIPPED"
        assert occurrence.reason_code == "QUEUE_WINDOW_EXPIRED"


def test_ros1_internal_status_removes_stale_estop_and_marks_missing_channels() -> None:
    ingress = load_service("firebot_ros1_unknown_safety_test", "services/mqtt-ingress/main.py")
    now = datetime.now(UTC)
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        robot_id = robot.id
    get_redis().set(
        "robot:R001:latest",
        json.dumps({"vehicle_id": "R001", "estop_active": False}),
    )
    payload = {
        "internal_contract": "ros-compat-v1",
        "vehicle_id": "R001",
        "timestamp": now.isoformat(),
        "mode": "IDLE",
        "ros_control_mode": 3,
    }
    assert ingress.process_internal_compat(
        "_platform/compat/R001/compat_status", json.dumps(payload).encode(), now
    )
    latest = json.loads(get_redis().get("robot:R001:latest"))
    assert "estop_active" not in latest
    with SessionLocal() as db:
        robot = db.get(Robot, robot_id)
        estop = db.scalar(
            select(RobotDataChannel).where(
                RobotDataChannel.robot_id == robot_id,
                RobotDataChannel.channel == "estop",
            )
        )
        capability = db.get(RobotCapability, robot_id)
        assert robot and robot.estop_active is None
        assert estop and estop.support_state == "UNSUPPORTED"
        assert capability and capability.supported_commands_json == []


def test_monitor_snapshot_online_state_is_db_authoritative() -> None:
    """DB online_state 是权威：Redis projection STALE 不得覆盖 DB ONLINE。"""
    client = TestClient(app)
    token = login(client)
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        robot.online_state = "ONLINE"
        get_redis().set(
            f"robot:{robot.vehicle_id}:latest",
            json.dumps(
                {
                    "vehicle_id": robot.vehicle_id,
                    "robot_id": robot.id,
                    "online_state": "STALE",
                    "battery": 63.1,
                }
            ),
        )
    response = client.get("/api/v1/monitor/snapshot", headers=bearer(token))
    assert response.status_code == 200
    row = next(r for r in response.json()["robots"] if r["vehicle_id"] == "R001")
    assert row["online_state"] == "ONLINE"


def test_update_online_syncs_redis_latest() -> None:
    """update_online 更新 DB 后同步 Redis latest online_state（DB/Redis/API 一致）。"""
    ingress = load_service("firebot_mqtt_ingress_online_sync", "services/mqtt-ingress/main.py")
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        ingress.update_online(db, robot, "ONLINE", {"boot_id": robot.boot_id}, None)
        ops = db.info.get("firebot_redis_after_commit", [])
        latest_sets = [
            value
            for (op, key, value, _ttl) in ops
            if op == "set" and key == f"robot:{robot.vehicle_id}:latest"
        ]
        assert latest_sets
        assert json.loads(latest_sets[-1])["online_state"] == "ONLINE"


def test_ingress_field_channel_realtime_delta() -> None:
    """U2B：vehicle.status/sensor realtime event 携带字段级 data_channels freshness delta。

    合同：status 含 battery 才携带 battery delta；sensor 含 smoke 才携带 smoke delta；
    其它消息类型（heartbeat/status 其它字段）不得刷新 battery/smoke freshness。
    """
    ingress = load_service("firebot_mqtt_ingress_field_delta", "services/mqtt-ingress/main.py")
    received = datetime.now(UTC)
    source_ts = received - timedelta(milliseconds=200)

    def _payloads(db, event_type: str) -> list[dict]:
        return [p for (t, p) in db.info.get("firebot_realtime_events", []) if t == event_type]

    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot

        # CASE 1: status 含 battery → event 含 data_channels.battery
        ingress.handle_status(
            db, robot, {"battery": 63.1}, source_ts, received, "CANONICAL_MQTT"
        )
        battery = _payloads(db, "vehicle.status")[-1].get("data_channels", {}).get("battery")
        assert battery and battery["support_state"] == "CONNECTED"
        assert battery["source_kind"] == "CANONICAL_MQTT"
        assert battery["last_received_at"] == received.isoformat()

        # CASE 2: status 不含 battery → event 不得伪造 battery freshness
        ingress.handle_status(
            db, robot, {"mode": "IDLE"}, source_ts, received, "CANONICAL_MQTT"
        )
        assert "battery" not in _payloads(db, "vehicle.status")[-1].get("data_channels", {})

        # CASE 3: sensor 含 smoke → event 含 data_channels.smoke
        ingress.handle_sensor(
            db,
            robot,
            {"smoke": 0.345, "boot_id": robot.boot_id, "seq": 1},
            source_ts,
            received,
            "CANONICAL_MQTT",
        )
        smoke = _payloads(db, "vehicle.sensor")[-1].get("data_channels", {}).get("smoke")
        assert smoke and smoke["support_state"] == "CONNECTED"
        assert smoke["last_received_at"] == received.isoformat()

        # CASE 4: sensor 不含 smoke → 不得伪造 smoke freshness
        ingress.handle_sensor(
            db,
            robot,
            {"bottom_ir": 1.0, "boot_id": robot.boot_id, "seq": 2},
            source_ts,
            received,
            "CANONICAL_MQTT",
        )
        assert "smoke" not in _payloads(db, "vehicle.sensor")[-1].get("data_channels", {})
