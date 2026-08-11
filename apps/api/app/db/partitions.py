from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

PARTITION_RE = re.compile(r"^(telemetry_samples|sensor_samples)_(\d{4})_(\d{2})$")


def partition_name(parent: str, when: datetime) -> str:
    utc = when.astimezone(UTC)
    if parent not in {"telemetry_samples", "sensor_samples"}:
        raise ValueError("unsupported partition parent")
    return f"{parent}_{utc:%Y_%m}"


def ensure_month_partitions(db: Session, reference: datetime, months_ahead: int = 2) -> None:
    db.execute(
        text("SELECT ensure_firebot_month_partitions(:reference, :months_ahead)"),
        {"reference": reference, "months_ahead": months_ahead},
    )


def default_partition_counts(db: Session) -> dict[str, int]:
    return {
        "telemetry_samples": int(
            db.execute(text("SELECT count(*) FROM telemetry_samples_default")).scalar_one()
        ),
        "sensor_samples": int(
            db.execute(text("SELECT count(*) FROM sensor_samples_default")).scalar_one()
        ),
    }


def drop_expired_month_partitions(db: Session, parent: str, cutoff: datetime) -> list[str]:
    rows = db.execute(
        text(
            """
            SELECT c.relname
            FROM pg_inherits i
            JOIN pg_class p ON p.oid = i.inhparent
            JOIN pg_class c ON c.oid = i.inhrelid
            WHERE p.relname = :parent AND c.relname <> :default_name
            """
        ),
        {"parent": parent, "default_name": f"{parent}_default"},
    ).scalars()
    dropped: list[str] = []
    cutoff_month = datetime(cutoff.year, cutoff.month, 1, tzinfo=UTC)
    for name in rows:
        match = PARTITION_RE.fullmatch(str(name))
        if not match:
            continue
        start = datetime(int(match.group(2)), int(match.group(3)), 1, tzinfo=UTC)
        if start < cutoff_month:
            db.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
            dropped.append(str(name))
    return dropped
