#!/usr/bin/env python3
"""DEV-only simulated data cleanup (ROBOT-Web).

Identifies demo/simulated data by reliable discriminators only:
- robots whose RobotIntegrationProfile.source_kind == "MOCK"
- seed telemetry/sensor samples with boot_id == "seed-history"
- fire events carrying explicit test markers in their note

It NEVER deletes audit logs (immutable/retention contract).

Usage (from repo root, inside the api venv):
    python scripts/reset_demo_data.py                # dry-run: counts only
    python scripts/reset_demo_data.py --export out/  # dry-run + JSON backup
    python scripts/reset_demo_data.py --execute      # delete (dev profile only)
    python scripts/reset_demo_data.py --execute --force-server  # override guard

Safety:
- destructive delete is refused unless app_env == "dev" (or --force-server).
- old data without a reliable discriminator is intentionally left in place;
  do NOT guess by date or task code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "api"))

from app.core.config import get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    AuditLog,
    FireEvent,
    Robot,
    RobotIntegrationProfile,
    SensorSample,
    Task,
    TelemetrySample,
)
from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

TEST_NOTE_MARKERS = ("integration test", "ui-geometry probe", "direct extinguish")


def mock_robot_ids(db: Session) -> list[str]:
    return list(
        db.execute(
            select(Robot.id)
            .join(RobotIntegrationProfile, RobotIntegrationProfile.robot_id == Robot.id)
            .where(RobotIntegrationProfile.source_kind == "MOCK")
        ).scalars()
    )


def scalar_count(db: Session, stmt) -> int:
    return len(db.execute(stmt).scalars().all())


def dry_run(db: Session, mock_ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {"mock_robots": len(mock_ids)}
    counts["mock_tasks"] = scalar_count(db, select(Task.id).where(Task.robot_id.in_(mock_ids)))
    counts["mock_fire_events"] = scalar_count(
        db, select(FireEvent.id).where(FireEvent.robot_id.in_(mock_ids))
    )
    counts["mock_telemetry"] = scalar_count(
        db, select(TelemetrySample.id).where(TelemetrySample.robot_id.in_(mock_ids))
    )
    counts["mock_sensor_samples"] = scalar_count(
        db, select(SensorSample.id).where(SensorSample.robot_id.in_(mock_ids))
    )
    counts["test_marker_fire_events"] = sum(
        scalar_count(db, select(FireEvent.id).where(FireEvent.note.ilike(f"%{marker}%")))
        for marker in TEST_NOTE_MARKERS
    )
    counts["seed_telemetry"] = scalar_count(
        db, select(TelemetrySample.id).where(TelemetrySample.boot_id == "seed-history")
    )
    counts["seed_sensor_samples"] = scalar_count(
        db, select(SensorSample.id).where(SensorSample.boot_id == "seed-history")
    )
    counts["audit_logs_not_deleted"] = scalar_count(db, select(AuditLog.id))
    return counts


def dump(db: Session, stmt, payload: dict[str, list[dict]], name: str) -> None:
    rows = db.execute(stmt).scalars().all()
    payload[name] = [
        {column.name: getattr(row, column.name) for column in row.__table__.columns} for row in rows
    ]


def export_rows(db: Session, out_dir: str, mock_ids: list[str]) -> str:
    os.makedirs(out_dir, exist_ok=True)
    payload: dict[str, list[dict]] = {}
    dump(db, select(FireEvent).where(FireEvent.robot_id.in_(mock_ids)), payload, "mock_fire_events")
    dump(db, select(Task).where(Task.robot_id.in_(mock_ids)), payload, "mock_tasks")
    dump(
        db,
        select(TelemetrySample).where(TelemetrySample.robot_id.in_(mock_ids)),
        payload,
        "mock_telemetry",
    )
    dump(
        db,
        select(SensorSample).where(SensorSample.robot_id.in_(mock_ids)),
        payload,
        "mock_sensor_samples",
    )
    dump(
        db,
        select(TelemetrySample).where(TelemetrySample.boot_id == "seed-history"),
        payload,
        "seed_telemetry",
    )
    dump(
        db,
        select(SensorSample).where(SensorSample.boot_id == "seed-history"),
        payload,
        "seed_sensor_samples",
    )
    for marker in TEST_NOTE_MARKERS:
        dump(
            db,
            select(FireEvent).where(FireEvent.note.ilike(f"%{marker}%")),
            payload,
            f"test_fire_{marker.replace(' ', '_')}",
        )

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, f"reset-demo-data-{stamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, default=str, indent=2)
    return path


def execute_cleanup(db: Session, mock_ids: list[str]) -> dict[str, int]:
    deleted: dict[str, int] = {}

    def remove(stmt, name: str) -> None:
        deleted[name] = db.execute(stmt).rowcount or 0

    remove(delete(FireEvent).where(FireEvent.robot_id.in_(mock_ids)), "mock_fire_events")
    remove(delete(Task).where(Task.robot_id.in_(mock_ids)), "mock_tasks")
    remove(delete(TelemetrySample).where(TelemetrySample.robot_id.in_(mock_ids)), "mock_telemetry")
    remove(delete(SensorSample).where(SensorSample.robot_id.in_(mock_ids)), "mock_sensor_samples")
    remove(
        delete(TelemetrySample).where(TelemetrySample.boot_id == "seed-history"), "seed_telemetry"
    )
    remove(
        delete(SensorSample).where(SensorSample.boot_id == "seed-history"), "seed_sensor_samples"
    )
    for marker in TEST_NOTE_MARKERS:
        remove(
            delete(FireEvent).where(FireEvent.note.ilike(f"%{marker}%")),
            f"test_fire_{marker.replace(' ', '_')}",
        )
    db.commit()
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="ROBOT-Web simulated data cleanup")
    parser.add_argument("--execute", action="store_true", help="actually delete (dev profile only)")
    parser.add_argument(
        "--force-server", action="store_true", help="override the non-dev guard (danger)"
    )
    parser.add_argument(
        "--export", metavar="DIR", help="write a JSON backup of rows that would be deleted"
    )
    args = parser.parse_args()

    settings = get_settings()
    print(f"app_env = {settings.app_env}")

    with SessionLocal.begin() as db:
        mock_ids = mock_robot_ids(db)
        print("dry-run counts:", json.dumps(dry_run(db, mock_ids), ensure_ascii=False, indent=2))

        if args.export:
            print("backup written:", export_rows(db, args.export, mock_ids))

        if args.execute:
            if settings.app_env != "dev" and not args.force_server:
                print("REFUSED: destructive delete requires app_env == 'dev' (or --force-server).")
                return 2
            print(
                "deleted:", json.dumps(execute_cleanup(db, mock_ids), ensure_ascii=False, indent=2)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
