"""DEV-only Mock session reset.

A fresh DEMO session must start with R001 deterministically at REMOTE_WAITING
(x=1.2, y=1.2, theta=pi/2), IDLE, with no active task and no e-stop, so the very
first "开始巡检" passes the server guards naturally instead of being blocked by
a stale "continue patrol / return waiting" state from a previous demo.

This module is a development tool, not a vehicle or business-logic path:

- It refuses to run unless APP_ENV=dev and MOCK_ENABLED=true, and the target
  robot's RobotIntegrationProfile.source_kind is MOCK.
- It NEVER deletes history: tasks, task_events, commands and audit logs are kept.
  Non-terminal tasks are cancelled in place (phase=DEMO_SESSION_RESET) and the
  previous resumable context is consumed to CONSUMED_BY_DEMO_RESET.
- It only clears the current ephemeral projection for R001 (Redis
  ``robot:R001:latest`` / ``heartbeat:R001`` and the DB current mode/task/estop
  fields). The fresh truth is re-established by the recreated mock-robot's MQTT
  uplink.

Run inside the api container:

    python -m app.dev.reset_mock            # reset
    python -m app.dev.reset_mock wait       # poll until the mock is at waiting
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.events import get_redis
from app.db.models import Robot, RobotIntegrationProfile, Task, TaskEvent
from app.db.session import SessionLocal
from app.modules.navigation.route_builder import REMOTE_WAITING

DEFAULT_VEHICLE_ID = "R001"
NON_TERMINAL_STATUSES = ("CREATED", "QUEUED", "ACCEPTED", "EXECUTING")
RESUME_CONSUMED = "CONSUMED_BY_DEMO_RESET"
RESET_REASON = "DEV_MOCK_SESSION_RESET"
WAITING_TOLERANCE_M = 0.8  # matches the patrol-start waiting guard


def check_reset_allowed(
    settings: Settings, profile: RobotIntegrationProfile | None, vehicle_id: str
) -> str | None:
    """Return a forbidden reason string, or None when the reset is allowed."""
    if settings.app_env != "dev" or not settings.mock_enabled:
        return "MOCK_RESET_FORBIDDEN=YES (APP_ENV != dev or MOCK_ENABLED != true)"
    if not profile or profile.source_kind != "MOCK":
        return f"MOCK_RESET_FORBIDDEN=YES (source_kind is not MOCK, vehicle_id={vehicle_id})"
    return None


def cancel_task_for_reset(task: Task, now: datetime) -> TaskEvent:
    """Cancel a non-terminal task in place and return its terminal TaskEvent."""
    task.status = "CANCELLED"
    task.phase = "DEMO_SESSION_RESET"
    task.completed_at = now
    return TaskEvent(
        task_id=task.id,
        status="CANCELLED",
        phase="DEMO_SESSION_RESET",
        progress=task.progress,
        payload_json={"reason": RESET_REASON},
    )


def consume_resume_context(task: Task) -> bool:
    """Consume a resumable (resume_state=AVAILABLE) task, keeping its cursor."""
    params = dict(task.parameters_json or {})
    if params.get("resume_state") != "AVAILABLE":
        return False
    params["resume_state"] = RESUME_CONSUMED
    task.parameters_json = params
    return True


def _nonterminal_tasks(db, robot_id: str) -> list[Task]:
    return db.scalars(
        select(Task).where(
            Task.robot_id == robot_id,
            Task.status.in_(NON_TERMINAL_STATUSES),
        )
    ).all()


def _resumable_task(db, robot_id: str, task_type: str) -> Task | None:
    return db.scalar(
        select(Task)
        .where(
            Task.robot_id == robot_id,
            Task.type == task_type,
            Task.status == "CANCELLED",
        )
        .order_by(Task.created_at.desc())
    )


def apply_reset(
    *,
    robot: Robot,
    tasks_to_cancel: list[Task],
    resumable_tasks: list[Task],
    redis,
    now: datetime | None = None,
) -> dict:
    """Apply the reset mutations given already-queried tasks (no DB queries)."""
    now = now or datetime.now(UTC)
    events: list[TaskEvent] = []
    for task in tasks_to_cancel:
        events.append(cancel_task_for_reset(task, now))

    consumed_ids: list[str] = []
    for task in resumable_tasks:
        if consume_resume_context(task):
            consumed_ids.append(task.id)

    robot.current_task_id = None
    robot.current_mode = "IDLE"
    robot.estop_active = False

    redis.delete(f"robot:{robot.vehicle_id}:latest")
    redis.delete(f"heartbeat:{robot.vehicle_id}")

    return {"cancelled_events": events, "consumed_task_ids": consumed_ids}


def run_reset(
    db,
    redis,
    *,
    robot: Robot,
    profile: RobotIntegrationProfile | None,
    settings: Settings,
    now: datetime | None = None,
) -> dict:
    """Query current state and apply the reset. History is never deleted."""
    error = check_reset_allowed(settings, profile, robot.vehicle_id)
    if error:
        return {"ok": False, "error": error}

    nonterminal = _nonterminal_tasks(db, robot.id)
    resumable: list[Task] = []
    for task_type in ("PATROL", "RETURN_DOCK"):
        task = _resumable_task(db, robot.id, task_type)
        if task:
            resumable.append(task)

    result = apply_reset(
        robot=robot,
        tasks_to_cancel=nonterminal,
        resumable_tasks=resumable,
        redis=redis,
        now=now,
    )
    for event in result["cancelled_events"]:
        db.add(event)

    return {
        "ok": True,
        "cancelled_task_ids": [t.id for t in nonterminal],
        "consumed_task_ids": result["consumed_task_ids"],
    }


def reset(vehicle_id: str = DEFAULT_VEHICLE_ID) -> int:
    settings = get_settings()
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))
        if not robot:
            print(f"MOCK_RESET_FORBIDDEN=YES (robot not found: {vehicle_id})")
            return 1
        profile = db.get(RobotIntegrationProfile, robot.id)
        result = run_reset(db, get_redis(), robot=robot, profile=profile, settings=settings)
        if not result["ok"]:
            print(result["error"])
            return 1
    print("MOCK_RESET_OK=YES")
    if result["cancelled_task_ids"]:
        print(f"MOCK_RESET_CANCELLED_TASKS={len(result['cancelled_task_ids'])}")
    if result["consumed_task_ids"]:
        print(f"MOCK_RESET_CONSUMED_RESUME={len(result['consumed_task_ids'])}")
    return 0


def wait_ready(vehicle_id: str = DEFAULT_VEHICLE_ID, timeout: int = 45) -> int:
    redis = get_redis()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = redis.get(f"robot:{vehicle_id}:latest")
        if raw:
            latest = json.loads(raw)
            x = latest.get("x")
            y = latest.get("y")
            at_waiting = (
                x is not None
                and y is not None
                and math.hypot(float(x) - REMOTE_WAITING["x"], float(y) - REMOTE_WAITING["y"])
                <= WAITING_TOLERANCE_M
            )
            if (
                at_waiting
                and latest.get("mode") == "IDLE"
                and not latest.get("active_task_id")
                and not latest.get("estop_active")
            ):
                print("MOCK_R001_ONLINE=PASS")
                print("MOCK_R001_WAITING_POSE=PASS")
                print("MOCK_R001_IDLE=PASS")
                print("MOCK_R001_NO_ACTIVE_TASK=PASS")
                print("MOCK_R001_ESTOP_CLEAR=PASS")
                print(f"MOCK_INITIAL_X={float(x):.3f}")
                print(f"MOCK_INITIAL_Y={float(y):.3f}")
                print(f"MOCK_INITIAL_THETA={float(latest.get('theta') or 0):.3f}")
                return 0
        time.sleep(1)
    print("MOCK_R001_WAITING_POSE=FAIL (timeout waiting for fresh mock telemetry)")
    return 1


def main(argv: list[str] | None = None) -> int:
    import sys

    args = argv if argv is not None else sys.argv[1:]
    command = args[0] if args else "reset"
    vehicle_id = args[1] if len(args) > 1 else DEFAULT_VEHICLE_ID
    if command == "wait":
        timeout = int(args[2]) if len(args) > 2 else 45
        return wait_ready(vehicle_id, timeout)
    return reset(vehicle_id)


if __name__ == "__main__":
    raise SystemExit(main())
