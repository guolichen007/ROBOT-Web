from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.events import get_redis, queue_event, queue_redis_delete, queue_redis_set
from app.core.logging import configure_logging
from app.core.metrics import command_timeout_total, partition_default_rows
from app.core.serialization import serialize_model
from app.db.models import (
    AuditLog,
    Command,
    ManualControlSession,
    Robot,
)
from app.db.partitions import (
    default_partition_counts,
    drop_expired_month_partitions,
    ensure_month_partitions,
)
from app.db.session import SessionLocal
from sqlalchemy import delete, select

settings = get_settings()
configure_logging("task-worker")
logger = logging.getLogger("task-worker")
redis = get_redis()


def reconcile_robot_states(db, now: datetime) -> None:
    for robot in db.scalars(select(Robot)).all():
        if not robot.last_seen_at:
            state = "OFFLINE"
        else:
            age = (now - robot.last_seen_at).total_seconds()
            state = (
                "OFFLINE"
                if age >= settings.robot_offline_seconds
                else "STALE"
                if age >= settings.robot_stale_seconds
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
                if now.minute == 0 and now.second < 2:
                    retention(db, now)
        except Exception:
            logger.exception("task worker cycle failed")
        time.sleep(1)


if __name__ == "__main__":
    main()
