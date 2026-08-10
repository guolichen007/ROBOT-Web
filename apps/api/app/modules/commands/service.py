from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.events import get_redis
from app.db.models import Command, OutboxEvent, Robot, Task

ACTIVE_TASK_STATES = {"CREATED", "QUEUED", "ACCEPTED", "EXECUTING"}


def command_code() -> str:
    return f"C{datetime.now(UTC):%Y%m%d%H%M%S}-{str(uuid4())[:8]}"


def task_code(prefix: str = "T") -> str:
    return f"{prefix}{datetime.now(UTC):%Y%m%d%H%M%S}-{str(uuid4())[:8]}"


def assert_robot_can_execute(
    db: Session, robot: Robot, action: str, ignore_task_id: str | None = None
) -> None:
    if not robot.enabled:
        raise HTTPException(409, "机器人已禁用")
    if robot.online_state in {"STALE", "OFFLINE"} and action != "emergency_stop":
        raise HTTPException(409, f"机器人状态为 {robot.online_state}，拒绝执行 {action}")
    if robot.estop_active and action not in {"emergency_stop", "reset_estop"}:
        raise HTTPException(409, "软件急停已锁存")
    active_query = select(Task).where(
        Task.robot_id == robot.id, Task.status.in_(ACTIVE_TASK_STATES)
    )
    if ignore_task_id:
        active_query = active_query.where(Task.id != ignore_task_id)
    active_task = db.scalar(active_query)
    if not active_task:
        return
    if action == "manual_control":
        raise HTTPException(409, "存在自动任务，请先显式取消任务再进入手动模式")
    if action == "patrol":
        raise HTTPException(409, "机器人已有活动业务任务")
    if action == "return_dock" and active_task.type == "EXTINGUISH":
        raise HTTPException(409, "灭火任务活动时不能回充")
    if action == "extinguish":
        raise HTTPException(409, "灭火任务不得静默覆盖现有任务，请先显式取消")


def build_command_payload(
    *,
    robot: Robot,
    operator_id: str,
    cmd: str,
    params: dict[str, Any],
    ttl_ms: int,
    priority: int,
    task_id: str | None = None,
    command_id: str | None = None,
    lease_id: str | None = None,
    control_session_id: str | None = None,
    seq: int = 0,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "schema_version": "1.1",
        "message_id": str(uuid4()),
        "type": "command",
        "vehicle_id": robot.vehicle_id,
        "boot_id": robot.boot_id or str(uuid4()),
        "timestamp": now.isoformat(),
        "seq": seq,
        "command_id": command_id or command_code(),
        "correlation_id": str(uuid4()),
        "task_id": task_id,
        "lease_id": lease_id,
        "control_session_id": control_session_id,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(milliseconds=ttl_ms)).isoformat(),
        "ttl_ms": ttl_ms,
        "priority": priority,
        "source": "WEB",
        "operator_id": operator_id,
        "cmd": cmd,
        "params": params,
    }


def create_durable_command(
    db: Session,
    *,
    robot: Robot,
    operator_id: str,
    cmd: str,
    params: dict[str, Any],
    task_id: str | None = None,
    ttl_ms: int = 30_000,
    priority: int = 50,
) -> Command:
    assert_robot_can_execute(db, robot, cmd, ignore_task_id=task_id)
    payload = build_command_payload(
        robot=robot,
        operator_id=operator_id,
        cmd=cmd,
        params=params,
        ttl_ms=ttl_ms,
        priority=priority,
        task_id=task_id,
    )
    command = Command(
        command_id=payload["command_id"],
        correlation_id=payload["correlation_id"],
        robot_id=robot.id,
        task_id=task_id,
        cmd=cmd,
        priority=priority,
        payload_json=payload,
        lifecycle_status="QUEUED",
        issued_by=operator_id,
        issued_at=datetime.fromisoformat(payload["issued_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )
    db.add(command)
    db.flush()
    db.add(
        OutboxEvent(
            aggregate_type="COMMAND",
            aggregate_id=command.command_id,
            event_type="COMMAND_PUBLISH",
            payload_json=payload,
        )
    )
    return command


def enqueue_safety_command(payload: dict[str, Any]) -> str:
    return str(
        get_redis().xadd(
            "firebot:safety_commands",
            {"command": json.dumps(payload, ensure_ascii=False)},
            maxlen=1000,
            approximate=True,
        )
    )
