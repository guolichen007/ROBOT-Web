from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from math import atan2, cos, hypot, sin
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.events import get_redis, queue_event, queue_redis_delete, queue_redis_set
from app.core.logging import configure_logging
from app.core.metrics import command_timeout_total, partition_default_rows
from app.core.serialization import serialize_model
from app.db.models import (
    AuditLog,
    Command,
    ManualControlSession,
    MapVersion,
    PatrolPlan,
    PatrolSchedule,
    PatrolScheduleOccurrence,
    Robot,
    RobotIntegrationProfile,
    RobotOperationEvent,
    StopOperation,
    Task,
    TaskEvent,
)
from app.db.partitions import (
    default_partition_counts,
    drop_expired_month_partitions,
    ensure_month_partitions,
)
from app.db.session import SessionLocal
from app.modules.commands.readiness import robot_readiness
from app.modules.tasks.patrol import build_patrol_task
from croniter import croniter
from sqlalchemy import delete, select

settings = get_settings()
configure_logging("task-worker")
logger = logging.getLogger("task-worker")
redis = get_redis()

ACTIVE_TASK_STATES = {"CREATED", "QUEUED", "ACCEPTED", "EXECUTING"}


def next_schedule_run(schedule: PatrolSchedule, base: datetime) -> datetime:
    zone = ZoneInfo(schedule.timezone)
    return (
        croniter(schedule.cron_expression, base.astimezone(zone)).get_next(datetime).astimezone(UTC)
    )


def stationary_observation(
    latest: dict,
    now: datetime,
    *,
    freshness_ms: int,
    linear_threshold: float,
    angular_threshold: float,
) -> tuple[bool, bool]:
    """Return (fresh, stationary) using full planar mecanum velocity."""

    received_raw = latest.get("server_received_at")
    source_raw = latest.get("source_timestamp")
    try:
        received = datetime.fromisoformat(received_raw) if received_raw else None
        source = datetime.fromisoformat(source_raw) if source_raw else None
        fresh = bool(
            received
            and source
            and (now - received).total_seconds() * 1000 <= freshness_ms
            and (now - source).total_seconds() * 1000 <= freshness_ms
        )
    except (TypeError, ValueError):
        fresh = False
    linear_x = latest.get("linear_x")
    linear_y = latest.get("linear_y")
    planar_speed = latest.get("planar_speed")
    if planar_speed is None and linear_x is not None:
        planar_speed = hypot(float(linear_x), float(linear_y or 0))
    if planar_speed is None:
        planar_speed = latest.get("linear")
    angular = latest.get("angular_z", latest.get("angular"))
    stationary = bool(
        fresh
        and planar_speed is not None
        and angular is not None
        and abs(float(planar_speed)) < linear_threshold
        and abs(float(angular)) < angular_threshold
    )
    return fresh, stationary


def observation_token(latest: dict) -> datetime | None:
    """返回可用于静止帧去重的观测标识（可比较、单调的时间戳）。

    优先 server_received_at（服务器接收时刻，单调）；缺失时回退 source_timestamp。
    无法解析返回 None（视为无有效观测）。
    """
    for key in ("server_received_at", "source_timestamp"):
        raw = latest.get(key)
        if not raw:
            continue
        try:
            token = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if token.tzinfo is None:
            token = token.replace(tzinfo=UTC)
        return token
    return None


def advance_stationary_frames(
    fresh: bool,
    stationary: bool,
    token: datetime | None,
    last_token: datetime | None,
    current_frames: int,
) -> tuple[int, datetime | None]:
    """仅按「独立新鲜运动观测」推进静止帧计数，绝不按 worker reconcile 次数累计。

    - stale（不新鲜）：帧清零，不确认。
    - 同一观测再次 reconcile（token 不大于 last_token）：帧数不变（去重）。
    - 新观测且静止：帧 +1，更新 last_token。
    - 新观测且非静止（非零速度）：帧清零，更新 last_token。
    """
    if not fresh:
        return 0, last_token
    is_new = token is not None and (last_token is None or token > last_token)
    if not is_new:
        return current_frames, last_token
    return (current_frames + 1 if stationary else 0), token


def reconcile_robot_states(db, now: datetime) -> None:
    for robot in db.scalars(select(Robot)).all():
        integration = db.get(RobotIntegrationProfile, robot.id)
        stale_seconds = integration.stale_seconds if integration else settings.robot_stale_seconds
        offline_seconds = (
            integration.offline_seconds if integration else settings.robot_offline_seconds
        )
        if not robot.last_seen_at:
            state = "OFFLINE"
        else:
            age = (now - robot.last_seen_at).total_seconds()
            state = (
                "OFFLINE"
                if age >= offline_seconds
                else "STALE"
                if age >= stale_seconds
                else "ONLINE"
            )
        if robot.online_state != state:
            robot.online_state = state
            raw = redis.get(f"robot:{robot.vehicle_id}:latest")
            latest = (
                json.loads(raw) if raw else {"vehicle_id": robot.vehicle_id, "robot_id": robot.id}
            )
            latest["online_state"] = state
            queue_redis_set(
                db, f"robot:{robot.vehicle_id}:latest", json.dumps(latest, ensure_ascii=False)
            )
            queue_event(db, f"vehicle.{state.lower()}", latest)
        if state in {"STALE", "OFFLINE"}:
            lease_raw = redis.get(f"manual:lease:{robot.id}")
            if lease_raw:
                lease = json.loads(lease_raw)
                queue_redis_delete(db, f"manual:lease:{robot.id}")
                session = db.scalar(
                    select(ManualControlSession).where(
                        ManualControlSession.lease_id == lease["lease_id"]
                    )
                )
                if session and session.state == "HELD":
                    session.state = "EXPIRED"
                    session.ended_at = now
                    session.end_reason = f"ROBOT_{state}"


def expire_sessions_and_commands(db, now: datetime) -> None:
    sessions = db.scalars(
        select(ManualControlSession).where(
            ManualControlSession.state == "HELD", ManualControlSession.expires_at < now
        )
    ).all()
    for session in sessions:
        if not redis.exists(f"manual:lease:{session.robot_id}"):
            session.state = "EXPIRED"
            session.ended_at = now
            session.end_reason = "LEASE_TTL"
    deadline = now - timedelta(seconds=settings.command_ack_timeout_seconds)
    commands = db.scalars(
        select(Command).where(
            Command.lifecycle_status == "PUBLISHED", Command.published_at < deadline
        )
    ).all()
    for command in commands:
        command.lifecycle_status = "PUBLISHED_UNCONFIRMED"
        command.ack_reason = "ACK_TIMEOUT"
        command_timeout_total.inc()
        queue_event(db, "command.updated", serialize_model(command))


def reconcile_stop_operations(db, now: datetime) -> None:
    operations = db.scalars(
        select(StopOperation).where(
            StopOperation.state.not_in(
                {"VEHICLE_STATIONARY_CONFIRMED", "PARTIAL_UNCONFIRMED", "FAILED", "UNCONFIRMED"}
            )
        )
    ).all()
    for operation in operations:
        stop = db.scalar(select(Command).where(Command.command_id == operation.stop_command_id))
        cancel = (
            db.scalar(select(Command).where(Command.command_id == operation.cancel_command_id))
            if operation.cancel_command_id
            else None
        )
        task = db.get(Task, operation.task_id) if operation.task_id else None

        # --- mission cancel truth (independent of motion stop) ---
        if operation.mission_cancel_state not in {"NOT_REQUIRED", "CANCELLED_CONFIRMED", "UNCONFIRMED", "UNAVAILABLE"}:
            if task and task.status == "CANCELLED":
                operation.mission_cancel_state = "CANCELLED_CONFIRMED"
            elif cancel and cancel.ack_status == "accepted":
                operation.mission_cancel_state = "ACK_ACCEPTED"
            if (
                operation.mission_cancel_state not in {"CANCELLED_CONFIRMED", "UNCONFIRMED", "UNAVAILABLE"}
                and operation.cancel_deadline_at
                and now >= operation.cancel_deadline_at
            ):
                operation.mission_cancel_state = "UNCONFIRMED"
                operation.failure_reason = "TASK_CANCEL_TIMEOUT"

        cancel_terminal = operation.mission_cancel_state in {
            "NOT_REQUIRED",
            "UNAVAILABLE",
            "CANCELLED_CONFIRMED",
            "UNCONFIRMED",
        }

        # --- stop ACK truth (independent of physical stationary) ---
        stop_ack_confirmed = bool(stop and stop.ack_status == "accepted")
        stop_ack_unconfirmed = bool(
            stop and stop.lifecycle_status in {"PUBLISHED_UNCONFIRMED", "FAILED", "EXPIRED"}
        ) or (now >= operation.stop_ack_deadline_at and not stop_ack_confirmed)

        # --- physical stationary truth: always verify telemetry, even when
        # the stop ACK is lost. A missing ACK must never suppress the
        # independent velocity check. ---
        # 物理静止确认是单调事实：一旦 STATIONARY_CONFIRMED，后续 reconcile 不得因
        # cancel/ACK timeout、telemetry 缺失/陈旧、帧数不足等原因重新降级。
        if operation.motion_stop_state == "STATIONARY_CONFIRMED":
            physically_stationary = True
        else:
            robot = db.get(Robot, operation.robot_id)
            raw = redis.get(f"robot:{robot.vehicle_id}:latest") if robot else None
            latest = json.loads(raw) if raw else {}
            fresh, stationary = stationary_observation(
                latest,
                now,
                freshness_ms=operation.telemetry_freshness_ms,
                linear_threshold=operation.linear_threshold,
                angular_threshold=operation.angular_threshold,
            )
            operation.stationary_frames, operation.last_stationary_observation_at = (
                advance_stationary_frames(
                    fresh,
                    stationary,
                    observation_token(latest),
                    operation.last_stationary_observation_at,
                    operation.stationary_frames,
                )
            )
            physically_stationary = operation.stationary_frames >= 5

        def terminal(state: str, motion_state: str, failure_reason: str | None = None) -> None:
            operation.state = state
            operation.motion_stop_state = motion_state
            operation.terminal_at = now
            if failure_reason:
                operation.failure_reason = failure_reason
            db.add(
                RobotOperationEvent(
                    robot_id=operation.robot_id,
                    task_id=operation.task_id,
                    operation_type="STOP_PATROL",
                    state=motion_state,
                    payload_json={
                        "stationary_frames": operation.stationary_frames,
                        "mission_cancel_state": operation.mission_cancel_state,
                    },
                )
            )
            queue_event(db, "operation.stop.updated", serialize_model(operation))

        if physically_stationary:
            if operation.mission_cancel_state in {"NOT_REQUIRED", "CANCELLED_CONFIRMED"}:
                if stop_ack_confirmed:
                    terminal("VEHICLE_STATIONARY_CONFIRMED", "STATIONARY_CONFIRMED")
                else:
                    terminal("PARTIAL_UNCONFIRMED", "STATIONARY_CONFIRMED", "STOP_ACK_UNCONFIRMED")
            elif operation.mission_cancel_state in {"UNAVAILABLE", "UNCONFIRMED"}:
                # 保留已设置的 failure_reason（如 TASK_CANCEL_TIMEOUT），不要用统一值覆盖
                reason = operation.failure_reason or "TASK_CANCEL_UNCONFIRMED"
                terminal("PARTIAL_UNCONFIRMED", "STATIONARY_CONFIRMED", reason)
            else:
                operation.motion_stop_state = "STATIONARY_CONFIRMED"
                operation.state = "STATIONARY_CONFIRMED_CANCEL_PENDING"
                queue_event(db, "operation.stop.updated", serialize_model(operation))
            continue

        # Not yet physically stationary.
        if not fresh and (stop_ack_unconfirmed or now >= operation.stationary_verify_deadline_at):
            terminal("UNCONFIRMED", "UNCONFIRMED", "TELEMETRY_STALE")
            continue
        if now >= operation.stationary_verify_deadline_at:
            terminal("UNCONFIRMED", "UNCONFIRMED", "VELOCITY_NOT_ZERO")
            continue

        # Still verifying: keep a descriptive motion state for the UI.
        if stop_ack_confirmed:
            operation.motion_stop_state = "VERIFYING_STATIONARY"
        elif stop_ack_unconfirmed:
            operation.motion_stop_state = "UNCONFIRMED"
        else:
            operation.motion_stop_state = "WAITING_ACK"
        operation.state = "VERIFYING_STATIONARY"
        queue_event(db, "operation.stop.updated", serialize_model(operation))


def _angle_error(actual: float, expected: float) -> float:
    return abs(atan2(sin(actual - expected), cos(actual - expected)))


def reconcile_navigation_verification(db, now: datetime) -> None:
    tasks = db.scalars(
        select(Task).where(
            Task.type == "NAVIGATE_TO_PRESET",
            Task.status == "EXECUTING",
            Task.phase == "VERIFYING_FINAL_POSE",
        )
    ).all()
    for task in tasks:
        robot = db.get(Robot, task.robot_id)
        parameters = dict(task.parameters_json or {})
        raw = redis.get(f"robot:{robot.vehicle_id}:latest") if robot else None
        latest = json.loads(raw) if raw else {}
        reported_raw = parameters.get("vehicle_completed_reported_at")
        received_raw = latest.get("server_received_at")
        try:
            reported = datetime.fromisoformat(reported_raw) if reported_raw else task.created_at
            received = datetime.fromisoformat(received_raw) if received_raw else None
            fresh = bool(
                received
                and (now - received).total_seconds() * 1000
                <= int(parameters.get("pose_freshness_ms", 1000))
            )
        except (TypeError, ValueError):
            reported = task.created_at
            fresh = False
        pose = task.target_pose_snapshot_json or {}
        localization_ok = latest.get("localization_status") in {
            "OK",
            "GOOD",
            "VALID",
            "VALID_SOURCE",
        }
        values_present = all(latest.get(key) is not None for key in ("x", "y", "theta"))
        within = False
        if values_present:
            within = hypot(
                float(latest["x"]) - float(pose.get("x", 0)),
                float(latest["y"]) - float(pose.get("y", 0)),
            ) <= float(parameters.get("position_tolerance_m", 0.2)) and _angle_error(
                float(latest["theta"]), float(pose.get("theta", 0))
            ) <= float(parameters.get("yaw_tolerance_rad", 0.15))
        frames = int(parameters.get("pose_verification_frames", 0))
        frames = frames + 1 if fresh and localization_ok and within else 0
        parameters["pose_verification_frames"] = frames
        task.parameters_json = parameters
        if frames >= int(parameters.get("required_verification_frames", 3)):
            task.status = "SUCCEEDED"
            task.phase = "FINAL_POSE_VERIFIED"
            task.progress = 100
            task.completed_at = now
            db.add(
                TaskEvent(
                    task_id=task.id,
                    status=task.status,
                    phase=task.phase,
                    progress=task.progress,
                    payload_json={
                        "fresh": fresh,
                        "localization_ok": localization_ok,
                        "frames": frames,
                    },
                )
            )
            command = db.scalar(
                select(Command).where(Command.task_id == task.id).order_by(Command.issued_at.desc())
            )
            if command:
                command.lifecycle_status = "SUCCEEDED"
                command.terminal_at = now
        elif (now - reported).total_seconds() >= int(
            parameters.get("verification_timeout_seconds", 30)
        ):
            task.status = "FAILED"
            task.phase = "FINAL_POSE_UNVERIFIED"
            task.failure_code = "FINAL_POSE_UNVERIFIED"
            task.failure_message = "车辆报告完成，但新鲜定位数据未能连续证明到位"
            task.completed_at = now
            db.add(
                TaskEvent(
                    task_id=task.id,
                    status=task.status,
                    phase=task.phase,
                    progress=task.progress,
                    payload_json={
                        "fresh": fresh,
                        "localization_ok": localization_ok,
                        "frames": frames,
                    },
                )
            )
        queue_event(db, "task.updated", serialize_model(task))


def _dispatch_schedule_occurrence(db, occurrence: PatrolScheduleOccurrence) -> bool:
    schedule = db.get(PatrolSchedule, occurrence.schedule_id)
    plan = db.get(PatrolPlan, schedule.patrol_plan_id) if schedule else None
    robot = db.get(Robot, plan.robot_id) if plan else None
    version = db.get(MapVersion, plan.map_version_id) if plan else None
    if not schedule or not plan or not robot or not version or not plan.enabled:
        occurrence.state = "REJECTED"
        occurrence.reason_code = "CONFIGURATION_INVALID"
        return False
    if (
        occurrence.state == "QUEUED"
        and (datetime.now(UTC) - occurrence.scheduled_for).total_seconds()
        > schedule.queue_expiry_seconds
    ):
        occurrence.state = "SKIPPED"
        occurrence.reason_code = "QUEUE_WINDOW_EXPIRED"
        return False
    integration = db.get(RobotIntegrationProfile, robot.id)
    if schedule.require_robot_online and robot.online_state != "ONLINE":
        occurrence.state = "SKIPPED"
        occurrence.reason_code = f"ROBOT_{robot.online_state}"
        return False
    if schedule.require_control_contract_verified and not (
        integration and integration.control_contract_verified and integration.ack_contract_verified
    ):
        occurrence.state = "SKIPPED"
        occurrence.reason_code = "CONTROL_CONTRACT_NOT_VERIFIED"
        return False
    if schedule.require_map_contract_verified and not (
        integration and integration.map_contract_verified
    ):
        occurrence.state = "SKIPPED"
        occurrence.reason_code = "MAP_CONTRACT_NOT_VERIFIED"
        return False
    if not robot_readiness(db, robot)["autonomous_task_ready"]["patrol"]:
        occurrence.state = "SKIPPED"
        occurrence.reason_code = "AUTONOMOUS_TASK_NOT_READY"
        return False
    if robot.current_map_id != version.map_id or robot.current_map_version != version.version:
        occurrence.state = "SKIPPED"
        occurrence.reason_code = "MAP_VERSION_MISMATCH"
        return False
    active = db.scalar(
        select(Task).where(Task.robot_id == robot.id, Task.status.in_(ACTIVE_TASK_STATES))
    )
    if active:
        if schedule.overlap_policy == "QUEUE":
            occurrence.state = "QUEUED"
            occurrence.reason_code = "WAITING_FOR_ACTIVE_TASK"
        else:
            occurrence.state = "REJECTED" if schedule.overlap_policy == "REJECT" else "SKIPPED"
            occurrence.reason_code = "ACTIVE_TASK_OVERLAP"
        return False
    try:
        task, command_id = build_patrol_task(
            db,
            plan=plan,
            robot=robot,
            actor_id=schedule.created_by,
            source="SCHEDULER",
            schedule_id=schedule.id,
            occurrence_id=occurrence.id,
        )
    except Exception as exc:
        occurrence.state = "REJECTED"
        occurrence.reason_code = getattr(exc, "code", "PATROL_BUILD_FAILED")
        return False
    occurrence.state = "DISPATCHED"
    occurrence.reason_code = None
    occurrence.task_id = task.id
    queue_event(db, "task.created", {**serialize_model(task), "command_id": command_id})
    return True


def run_patrol_scheduler(db, now: datetime) -> None:
    queued = db.scalars(
        select(PatrolScheduleOccurrence).where(PatrolScheduleOccurrence.state == "QUEUED")
    ).all()
    for occurrence in queued:
        _dispatch_schedule_occurrence(db, occurrence)

    schedules = db.scalars(
        select(PatrolSchedule).where(
            PatrolSchedule.enabled.is_(True),
            PatrolSchedule.next_run_at.is_not(None),
            PatrolSchedule.next_run_at <= now,
        )
    ).all()
    for schedule in schedules:
        scheduled_for = schedule.next_run_at
        if scheduled_for is None:
            continue
        schedule.next_run_at = next_schedule_run(schedule, scheduled_for)
        schedule.last_run_at = now
        occurrence = PatrolScheduleOccurrence(
            schedule_id=schedule.id,
            scheduled_for=scheduled_for,
            state="PENDING",
        )
        db.add(occurrence)
        db.flush()
        delay = (now - scheduled_for).total_seconds()
        misfired = delay > 5
        allowed_late = (
            schedule.misfire_policy == "RUN_IF_WITHIN_WINDOW"
            and delay <= schedule.misfire_grace_seconds
        )
        if misfired and not allowed_late:
            occurrence.state = "SKIPPED"
            occurrence.reason_code = "MISFIRE_EXPIRED"
        else:
            _dispatch_schedule_occurrence(db, occurrence)


def retention(db, now: datetime) -> None:
    ensure_month_partitions(db, now, 2)
    drop_expired_month_partitions(
        db, "telemetry_samples", now - timedelta(days=settings.telemetry_retention_days)
    )
    drop_expired_month_partitions(
        db, "sensor_samples", now - timedelta(days=settings.sensor_retention_days)
    )
    counts = default_partition_counts(db)
    for parent, count in counts.items():
        partition_default_rows.labels(table=parent).set(count)
        if count:
            logger.warning(
                "default partition contains rows", extra={"table": parent, "rows": count}
            )
    limits = [(AuditLog, AuditLog.created_at, settings.audit_retention_days)]
    for model, column, days in limits:
        ids = (
            db.execute(select(model.id).where(column < now - timedelta(days=days)).limit(1000))
            .scalars()
            .all()
        )
        if ids:
            db.execute(delete(model).where(model.id.in_(ids)))


def main() -> None:
    while True:
        now = datetime.now(UTC)
        try:
            redis.setex("service:task-worker:heartbeat", 5, now.isoformat())
            with SessionLocal.begin() as db:
                reconcile_robot_states(db, now)
                expire_sessions_and_commands(db, now)
                reconcile_stop_operations(db, now)
                reconcile_navigation_verification(db, now)
                run_patrol_scheduler(db, now)
                if now.minute == 0 and now.second < 2:
                    retention(db, now)
        except Exception:
            logger.exception("task worker cycle failed")
        time.sleep(1)


if __name__ == "__main__":
    main()
