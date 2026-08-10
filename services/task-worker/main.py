from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.events import append_event, get_redis
from app.core.logging import configure_logging
from app.core.metrics import command_timeout_total
from app.core.serialization import serialize_model
from app.db.models import (
    AuditLog,
    Command,
    ManualControlSession,
    Robot,
    SensorSample,
    TelemetrySample,
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
            redis.set(f"robot:{robot.vehicle_id}:latest", json.dumps(latest, ensure_ascii=False))
            append_event(f"vehicle.{state.lower()}", latest)
        if state in {"STALE", "OFFLINE"}:
            lease_raw = redis.get(f"manual:lease:{robot.id}")
            if lease_raw:
                lease = json.loads(lease_raw)
                redis.delete(f"manual:lease:{robot.id}")
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
        append_event("command.updated", serialize_model(command))


def retention(db, now: datetime) -> None:
    limits = [
        (TelemetrySample, TelemetrySample.server_received_at, settings.telemetry_retention_days),
        (SensorSample, SensorSample.server_received_at, settings.sensor_retention_days),
        (AuditLog, AuditLog.created_at, settings.audit_retention_days),
    ]
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
            with SessionLocal.begin() as db:
                reconcile_robot_states(db, now)
                expire_sessions_and_commands(db, now)
                if now.minute == 0 and now.second < 2:
                    retention(db, now)
            redis.setex("service:task-worker:heartbeat", 5, now.isoformat())
        except Exception:
            logger.exception("task worker cycle failed")
        time.sleep(1)


if __name__ == "__main__":
    main()
