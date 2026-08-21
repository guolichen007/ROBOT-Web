from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from app.core.config import Settings
from app.db.models import Robot, RobotIntegrationProfile, Task
from app.dev import reset_mock
from app.dev.reset_mock import (
    NON_TERMINAL_STATUSES,
    RESUME_CONSUMED,
    apply_reset,
    check_reset_allowed,
    run_reset,
)


def make_robot() -> Robot:
    return Robot(
        id="robot-id",
        vehicle_id="R001",
        site_id="site",
        name="R001",
        enabled=True,
        online_state="ONLINE",
        current_mode="PATROL",
        current_task_id="task-executing",
        estop_active=False,
    )


def make_profile(source_kind: str) -> RobotIntegrationProfile:
    return RobotIntegrationProfile(robot_id="robot-id", source_kind=source_kind)


def make_task(
    task_id: str, status: str, phase: str, *, parameters: dict | None = None
) -> Task:
    return Task(
        id=task_id,
        task_code=f"T-{task_id}",
        robot_id="robot-id",
        type="PATROL",
        status=status,
        phase=phase,
        progress=40.0,
        map_id_snapshot="map",
        map_version_snapshot="1",
        semantic_revision_snapshot=1,
        parameters_json=parameters or {},
        created_by="admin",
    )


class FakeRedis:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, *keys: str) -> None:
        self.deleted.extend(keys)


# TEST A
def test_mock_robot_cold_start_is_at_remote_waiting(monkeypatch) -> None:
    monkeypatch.setenv("MOCK_VEHICLE_ID", "R001")
    from services.mock_robot.main import MockRobot

    robot = MockRobot()
    assert robot.x == pytest.approx(1.2)
    assert robot.y == pytest.approx(1.2)
    assert robot.theta == pytest.approx(math.pi / 2)
    assert robot.mode == "IDLE"
    assert robot.active_task_id is None
    assert robot.estop is False


# TEST B
def test_reset_cancels_executing_patrol_and_keeps_event() -> None:
    robot = make_robot()
    executing = make_task("t-exec", "EXECUTING", "INSPECTING")
    redis = FakeRedis()
    result = apply_reset(
        robot=robot,
        tasks_to_cancel=[executing],
        resumable_tasks=[],
        redis=redis,
        now=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )
    assert executing.status == "CANCELLED"
    assert executing.phase == "DEMO_SESSION_RESET"
    assert executing.completed_at is not None
    events = result["cancelled_events"]
    assert len(events) == 1
    assert events[0].task_id == "t-exec"
    assert events[0].status == "CANCELLED"
    assert events[0].phase == "DEMO_SESSION_RESET"
    assert events[0].payload_json == {"reason": "DEV_MOCK_SESSION_RESET"}
    assert "robot:R001:latest" in redis.deleted


# TEST C
def test_reset_consumes_resume_context_and_keeps_cursor() -> None:
    robot = make_robot()
    cursor = {"waypoint_index": 12, "target_waypoint_index": 13}
    cancelled = make_task(
        "t-cancelled",
        "CANCELLED",
        "ESTOP_INTERRUPTED",
        parameters={"resume_state": "AVAILABLE", "live_route_cursor": cursor},
    )
    redis = FakeRedis()
    result = apply_reset(
        robot=robot, tasks_to_cancel=[], resumable_tasks=[cancelled], redis=redis
    )
    assert cancelled.parameters_json["resume_state"] == RESUME_CONSUMED
    assert cancelled.parameters_json["live_route_cursor"] == cursor
    assert result["consumed_task_ids"] == ["t-cancelled"]


# TEST D
def test_reset_does_not_touch_succeeded_history(monkeypatch) -> None:
    robot = make_robot()
    profile = make_profile("MOCK")
    executing = make_task("t-exec", "EXECUTING", "INSPECTING")
    succeeded = make_task("t-done", "SUCCEEDED", "COMPLETED")
    monkeypatch.setattr(reset_mock, "_nonterminal_tasks", lambda db, rid: [executing])
    monkeypatch.setattr(reset_mock, "_resumable_task", lambda db, rid, tt: None)

    class FakeDb:
        def __init__(self) -> None:
            self.added: list = []

        def add(self, obj) -> None:
            self.added.append(obj)

    db = FakeDb()
    redis = FakeRedis()
    settings = Settings(_env_file=None, app_env="dev", mock_enabled=True)
    result = run_reset(db, redis, robot=robot, profile=profile, settings=settings)
    assert result["ok"] is True
    assert executing.status == "CANCELLED"
    assert executing.phase == "DEMO_SESSION_RESET"
    assert succeeded.status == "SUCCEEDED"
    assert succeeded.phase == "COMPLETED"
    assert len(db.added) == 1  # one TaskEvent, history preserved
    assert "SUCCEEDED" not in NON_TERMINAL_STATUSES
    assert "FAILED" not in NON_TERMINAL_STATUSES
    assert "CANCELLED" not in NON_TERMINAL_STATUSES


# TEST E
def test_reset_rejects_non_mock_robot() -> None:
    settings = Settings(_env_file=None, app_env="dev", mock_enabled=True)
    assert check_reset_allowed(settings, make_profile("CANONICAL_MQTT"), "R001") is not None
    assert check_reset_allowed(settings, None, "R001") is not None


# TEST F
def test_reset_rejects_non_dev_env() -> None:
    settings = Settings(_env_file=None, app_env="server", mock_enabled=True)
    assert check_reset_allowed(settings, make_profile("MOCK"), "R001") is not None
    settings = Settings(_env_file=None, app_env="dev", mock_enabled=False)
    assert check_reset_allowed(settings, make_profile("MOCK"), "R001") is not None


# TEST G
def test_reset_rejects_ros_compat() -> None:
    settings = Settings(_env_file=None, app_env="dev", mock_enabled=True)
    assert check_reset_allowed(settings, make_profile("ROS_COMPAT"), "R001") is not None


def test_reset_allowed_for_dev_mock() -> None:
    settings = Settings(_env_file=None, app_env="dev", mock_enabled=True)
    assert check_reset_allowed(settings, make_profile("MOCK"), "R001") is None
