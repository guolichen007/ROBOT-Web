from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "bb7a12b8"


def database_url(name: str) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def run(*args: str, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def main() -> None:
    checkout_value = os.environ.get("RELEASED_CHECKOUT")
    if not checkout_value:
        raise SystemExit("RELEASED_CHECKOUT must point to the exact bb7a12b8 worktree")
    checkout = Path(checkout_value)
    if not (checkout / "apps" / "api" / "alembic.ini").exists():
        raise SystemExit("RELEASED_CHECKOUT is not a Firebot released worktree")
    name = f"firebot_released_0002_{uuid4().hex[:8]}"
    admin = create_engine(database_url("postgres"), isolation_level="AUTOCOMMIT")
    url = database_url(name)
    env = {**os.environ, "DATABASE_URL": url, "SEED_DEMO": "true"}
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        run("alembic", "upgrade", "20260811_0002", cwd=checkout / "apps" / "api", env=env)
        run("python", "-m", "app.db.seed", cwd=checkout / "apps" / "api", env=env)
        engine = create_engine(url)
        with engine.begin() as connection:
            tables = (
                "users",
                "maps",
                "robots",
                "telemetry_samples",
                "sensor_samples",
                "tasks",
                "fire_events",
                "audit_logs",
                "assets",
            )
            before = {
                table: connection.scalar(text(f"SELECT count(*) FROM {table}")) for table in tables
            }
            fingerprint = hashlib.sha256(
                str(
                    connection.execute(text("SELECT username FROM users ORDER BY username")).all()
                ).encode()
            ).hexdigest()
        run("alembic", "upgrade", "head", cwd=ROOT / "apps" / "api", env=env)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20260813_0005"
            )
            after = {
                table: connection.scalar(text(f"SELECT count(*) FROM {table}")) for table in tables
            }
            after_fingerprint = hashlib.sha256(
                str(
                    connection.execute(text("SELECT username FROM users ORDER BY username")).all()
                ).encode()
            ).hexdigest()
            assert after == before
            assert after_fingerprint == fingerprint
            assert connection.scalar(text("SELECT to_regclass('telemetry_samples_default')"))
            assert connection.scalar(text("SELECT to_regclass('sensor_samples_default')"))
        engine.dispose()
        print(f"RELEASED_0002_UPGRADE=PASS baseline={RELEASE} revision=20260813_0005")
    finally:
        with admin.connect() as connection:
            connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name"),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


if __name__ == "__main__":
    main()
