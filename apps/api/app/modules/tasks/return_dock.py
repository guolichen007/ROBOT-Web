"""Task-backed return-to-waiting workflow.

The frontend no longer fires a bare return-dock command (which had no task_id
and therefore never moved the mock vehicle). Instead a real RETURN_DOCK task is
created whose trajectory is a safe reverse path along the validated cruise
lanes, ending at REMOTE_WAITING.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.core.errors import PlatformError
from app.core.events import get_redis
from app.db.models import NavigationPreset, Robot, Task, TaskEvent, Trajectory
from app.modules.commands.service import create_durable_command, task_code
from app.modules.navigation.route_builder import REMOTE_WAITING, build_return_waypoints


def _latest_pose(robot: Robot) -> dict[str, float] | None:
    raw = get_redis().get(f"robot:{robot.vehicle_id}:latest")
    if not raw:
        return None
    latest = json.loads(raw)
    if not all(key in latest for key in ("x", "y", "theta")):
        return None
    return {"x": float(latest["x"]), "y": float(latest["y"]), "theta": float(latest["theta"])}


def _route_cursor_of(task: Task) -> tuple[int | None, int | None]:
    parameters = task.parameters_json or {}
    cursor = parameters.get("live_route_cursor") or {}
    index = cursor.get("waypoint_index")
    total = cursor.get("waypoint_total")
    return (int(index) if index is not None else None, int(total) if total is not None else None)


def build_return_task(
    db,
    *,
    robot: Robot,
    actor_id: str,
    source: str,
    previous_task: Task | None,
) -> tuple[Task, str]:
    waiting = db.scalar(
        select(NavigationPreset).where(
            NavigationPreset.map_version_id.in_(
                select(Trajectory.map_version_id).where(Trajectory.code == "RIGHT_SIDE_S_CRUISE")
            ),
            NavigationPreset.category == "WAITING_AREA",
            NavigationPreset.code == "REMOTE_WAITING_AREA",
        )
    )
    trajectory = db.scalar(select(Trajectory).where(Trajectory.code == "RIGHT_SIDE_S_CRUISE"))
    if not waiting:
        raise PlatformError("WAITING_AREA_MISSING", "未配置远端待命区")
    full_waypoints: list[dict[str, Any]] = trajectory.path_json if trajectory else []

    pose = _latest_pose(robot)
    cursor_index: int | None = None
    if previous_task:
        cursor_index, _ = _route_cursor_of(previous_task)
    return_path = build_return_waypoints(pose or dict(REMOTE_WAITING), full_waypoints, cursor_index)

    task = Task(
        task_code=task_code(),
        robot_id=robot.id,
        type="RETURN_DOCK",
        status="CREATED",
        phase="RETURN_TRIGGERED",
        progress=0,
        target_parking_slot_id=None,
        target_pose_snapshot_json=waiting.pose_json,
        map_id_snapshot=robot.current_map_id or "",
        map_version_snapshot=robot.current_map_version or "",
        semantic_revision_snapshot=trajectory.semantic_revision if hasattr(trajectory, "semantic_revision") else 1,
        trajectory_snapshot_json=return_path,
        parameters_json={
            "source": source,
            "waiting_preset_id": waiting.id,
            "resume_cleared": True,
        },
        created_by=actor_id,
    )
    db.add(task)
    db.flush()
    db.add(TaskEvent(task_id=task.id, status="CREATED", phase="RETURN_TRIGGERED", progress=0))
    command = create_durable_command(
        db,
        robot=robot,
        operator_id=actor_id,
        cmd="return_dock",
        task_id=task.id,
        params={
            "task_id": task.id,
            "trajectory": return_path,
            "waiting_preset_id": waiting.id,
            "target_pose": waiting.pose_json,
        },
    )
    task.status = "QUEUED"
    task.phase = "COMMAND_QUEUED"
    return task, command.command_id
