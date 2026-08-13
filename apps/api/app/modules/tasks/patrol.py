from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.errors import PlatformError
from app.db.models import (
    MapVersion,
    NavigationPreset,
    PatrolPlan,
    PatrolPlanPoint,
    Robot,
    Task,
    TaskEvent,
    Trajectory,
)
from app.modules.commands.service import create_durable_command, task_code


def build_patrol_task(
    db,
    *,
    plan: PatrolPlan,
    robot: Robot,
    actor_id: str,
    source: str,
    schedule_id: str | None = None,
    occurrence_id: str | None = None,
) -> tuple[Task, str]:
    """Single patrol domain builder shared by HMI and scheduler entry points."""

    version = db.get(MapVersion, plan.map_version_id)
    if not version or not plan.enabled:
        raise PlatformError("PATROL_PLAN_INVALID", "巡检计划或地图版本无效")
    if robot.current_map_id != version.map_id or robot.current_map_version != version.version:
        raise PlatformError(
            "MAP_VERSION_MISMATCH",
            "机器人地图版本与巡检计划不一致",
            details={
                "robot_map_id": robot.current_map_id,
                "robot_map_version": robot.current_map_version,
                "target_map_id": version.map_id,
                "target_map_version": version.version,
            },
        )
    points = db.scalars(
        select(PatrolPlanPoint)
        .where(PatrolPlanPoint.patrol_plan_id == plan.id)
        .order_by(PatrolPlanPoint.sequence)
    ).all()
    presets = [db.get(NavigationPreset, point.navigation_preset_id) for point in points]
    if not points or any(preset is None or not preset.enabled for preset in presets):
        raise PlatformError("PATROL_PLAN_POINTS_INVALID", "巡检计划缺少有效预设点")
    trajectory = db.get(Trajectory, plan.trajectory_id) if plan.trajectory_id else None
    first = presets[0]
    assert first is not None
    point_snapshots: list[dict[str, Any]] = [
        {
            "navigation_preset_id": preset.id,
            "pose": preset.pose_json,
            "dwell_seconds": point.dwell_seconds,
            "required_observations": point.required_observations_json,
        }
        for point, preset in zip(points, presets, strict=True)
        if preset is not None
    ]
    task = Task(
        task_code=task_code(),
        robot_id=robot.id,
        type="PATROL",
        status="CREATED",
        phase="PATROL_PLAN_TRIGGERED",
        progress=0,
        target_pose_snapshot_json=first.pose_json,
        map_id_snapshot=version.map_id,
        map_version_snapshot=version.version,
        semantic_revision_snapshot=version.semantic_revision,
        trajectory_snapshot_json=trajectory.path_json if trajectory else None,
        parameters_json={
            "source": source,
            "patrol_plan_id": plan.id,
            "schedule_id": schedule_id,
            "occurrence_id": occurrence_id,
            "points": point_snapshots,
        },
        created_by=actor_id,
    )
    db.add(task)
    db.flush()
    db.add(
        TaskEvent(
            task_id=task.id,
            status="CREATED",
            phase="PATROL_PLAN_TRIGGERED",
            progress=0,
        )
    )
    command = create_durable_command(
        db,
        robot=robot,
        operator_id=actor_id,
        cmd="patrol",
        task_id=task.id,
        params={
            "task_id": task.id,
            "patrol_plan_id": plan.id,
            "map_id": version.map_id,
            "map_version": version.version,
            "semantic_revision": version.semantic_revision,
            "trajectory": trajectory.path_json if trajectory else None,
            "points": point_snapshots,
        },
    )
    task.status = "QUEUED"
    task.phase = "COMMAND_QUEUED"
    return task, command.command_id
