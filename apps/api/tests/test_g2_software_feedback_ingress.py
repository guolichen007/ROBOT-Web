"""G2 封板反证：Ingress 软件反馈收敛（真实 handle_software_feedback；SQLite+FakeRedis）。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from scripts.vehicle_agent_mock_runtime import MockRuntime


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    old = {k: os.environ.get(k) for k in ("APP_ENV", "FIREBOT_MOCK_E2E")}
    os.environ.update(APP_ENV="test", FIREBOT_MOCK_E2E="1")
    value = MockRuntime(tmp_path_factory.mktemp("g2-ingress") / "test.sqlite")
    yield value
    value.close()
    for key, previous in old.items():
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def make_command(db, robot, cmd, *, boot=None, mode="SOFTWARE_TEST", status="ACK_ACCEPTED"):
    from app.db.models import Command, User

    now = datetime.now(UTC)
    message_id = str(uuid4())
    admin = db.scalar(select(User).where(User.username == "admin"))
    row = Command(
        command_id=f"C-{uuid4().hex[:12]}",
        correlation_id=str(uuid4()),
        robot_id=robot.id,
        cmd=cmd,
        payload_json={
            "schema_version": "vehicle-agent-v1",
            "message_id": message_id,
            "target_boot_id": boot or robot.boot_id,
            "execution_mode": mode,
        },
        lifecycle_status=status,
        issued_by=admin.id,
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
        ack_at=now,
        ack_status="accepted",
        ack_payload_json={"code": "OK", "ack_source": "vehicle"},
    )
    db.add(row)
    db.flush()
    return row


def feedback(row, state, **extra):
    return {
        "type": "software_feedback",
        "command_id": row.command_id,
        "message_id": row.payload_json["message_id"],
        "msg_id": row.payload_json["message_id"],
        "target_boot_id": row.payload_json["target_boot_id"],
        "state": state,
        "software_test": True,
        "ros_ts": str(uuid4()),
        **extra,
    }


def robot(db):
    from app.db.models import Robot

    value = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
    value.boot_id = value.boot_id or str(uuid4())
    return value


def test_stop_success_requires_exact_same_session_patrol(runtime):
    from services.vehicle_agent_ingress import handle_software_feedback

    now = datetime.now(UTC)
    with runtime.SessionLocal.begin() as db:
        r = robot(db)
        patrol = make_command(db, r, "patrol", status="EXECUTING")
        good = make_command(db, r, "stop_patrol")
        handle_software_feedback(
            db,
            r,
            feedback(
                good,
                "COMPLETED",
                ros_status="STOPPED",
                stopped_command_id=patrol.payload_json["message_id"],
            ),
            now,
        )
        assert good.lifecycle_status == "SUCCEEDED"
        # 被停止目标不存在 / 不是本会话 / 不是 SOFTWARE_TEST：stop 不得 SUCCEEDED
        other_session = make_command(db, r, "patrol", boot=str(uuid4()), status="EXECUTING")
        real_patrol = make_command(db, r, "patrol", mode="REAL", status="EXECUTING")
        for target in (
            str(uuid4()),
            other_session.payload_json["message_id"],
            real_patrol.payload_json["message_id"],
            None,
        ):
            stop = make_command(db, r, "stop_patrol")
            handle_software_feedback(
                db,
                r,
                feedback(stop, "COMPLETED", ros_status="STOPPED", stopped_command_id=target),
                now,
            )
            assert stop.lifecycle_status == "FAILED", target
            assert stop.ack_reason == "STOP_TARGET_MISMATCH"


def test_untrusted_or_untagged_feedback_never_terminal_success(runtime):
    from services.vehicle_agent_ingress import handle_software_feedback

    now = datetime.now(UTC)
    with runtime.SessionLocal.begin() as db:
        r = robot(db)
        for flag in (False, None):
            patrol = make_command(db, r, "patrol", status="EXECUTING")
            data = feedback(patrol, "STOPPED", ros_status="STOPPED")
            if flag is None:
                data.pop("software_test")
            else:
                data["software_test"] = flag
            handle_software_feedback(db, r, data, now)
            assert patrol.lifecycle_status == "FAILED"
            assert patrol.ack_reason == "UNTRUSTED_SOFTWARE_FEEDBACK"


def test_terminal_state_never_rewritten(runtime):
    from services.vehicle_agent_ingress import handle_software_feedback

    now = datetime.now(UTC)
    with runtime.SessionLocal.begin() as db:
        r = robot(db)
        patrol = make_command(db, r, "patrol", status="EXECUTING")
        handle_software_feedback(db, r, feedback(patrol, "STOPPED", ros_status="STOPPED"), now)
        assert patrol.lifecycle_status == "CANCELLED" and patrol.ack_reason == "STOPPED"
        handle_software_feedback(db, r, feedback(patrol, "COMPLETED"), now)
        assert patrol.lifecycle_status == "CANCELLED"


def test_foreign_correlation_ignored(runtime):
    from services.vehicle_agent_ingress import handle_software_feedback

    now = datetime.now(UTC)
    with runtime.SessionLocal.begin() as db:
        r = robot(db)
        patrol = make_command(db, r, "patrol", status="EXECUTING")
        forged = feedback(patrol, "STOPPED", ros_status="STOPPED")
        forged["message_id"] = forged["msg_id"] = str(uuid4())  # 他车/他命令 msg_id
        handle_software_feedback(db, r, forged, now)
        assert patrol.lifecycle_status == "EXECUTING"
        wrong_boot = feedback(patrol, "STOPPED", ros_status="STOPPED")
        wrong_boot["target_boot_id"] = str(uuid4())
        handle_software_feedback(db, r, wrong_boot, now)
        assert patrol.lifecycle_status == "EXECUTING"
