from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]


def database_url(name: str) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def run_alembic(url: str, *arguments: str) -> None:
    subprocess.run(
        ["alembic", *arguments],
        cwd=ROOT / "apps" / "api",
        env={**os.environ, "DATABASE_URL": url},
        check=True,
    )


def main() -> None:
    admin = create_engine(database_url("postgres"), isolation_level="AUTOCOMMIT")
    suffix = uuid4().hex[:8]
    cases = {
        f"firebot_empty_{suffix}": None,
        f"firebot_rev2_{suffix}": "20260811_0002",
        f"firebot_rev3_{suffix}": "20260812_0003",
    }
    try:
        with admin.connect() as connection:
            for name in cases:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
        for name, starting_revision in cases.items():
            url = database_url(name)
            if starting_revision:
                run_alembic(url, "upgrade", starting_revision)
            run_alembic(url, "upgrade", "head")
            engine = create_engine(url)
            with engine.connect() as connection:
                revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
                assert revision == "20260902_0008", (name, revision)
                for table in (
                    "robot_motion_profiles",
                    "robot_navigation_diagnostics",
                    "stop_operations",
                    "navigation_presets",
                ):
                    exists = connection.scalar(
                        text("SELECT to_regclass(:table_name)"), {"table_name": table}
                    )
                    assert exists == table, (name, table)
            engine.dispose()
            print(f"MIGRATION_CASE=PASS database={name} from={starting_revision or 'empty'}")
    finally:
        with admin.connect() as connection:
            for name in cases:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


if __name__ == "__main__":
    main()
