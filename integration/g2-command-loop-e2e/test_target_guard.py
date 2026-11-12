"""P0-C 反证：生产样式目标必须在首次写入之前被拒绝（纯解析，不连接任何目标）。"""

from __future__ import annotations

import pytest
from target_guard import TargetGuardError, check_static

OK = {
    "APP_ENV": "test",
    "REAL_E2E": "1",
    "DATABASE_URL": "postgresql+psycopg://firebot_ci:x@localhost:5432/firebot_ci",
    "REDIS_URL": "redis://localhost:6379/15",
}


def test_ci_target_accepted():
    assert check_static(OK)["db"] == ("localhost", 5432, "firebot_ci")


@pytest.mark.parametrize(
    "override",
    [
        {"APP_ENV": "server"},
        {"REAL_E2E": "0"},
        {"DATABASE_URL_FILE": "/run/secrets/database_url"},
        {"REDIS_URL_FILE": "/run/secrets/redis_url"},
        {"DATABASE_URL": "postgresql+psycopg://firebot:pw@postgres:5432/firebot"},
        {"DATABASE_URL": "postgresql+psycopg://firebot:pw@127.0.0.1:5432/firebot"},
        {"DATABASE_URL": "postgresql+psycopg://u:pw@10.0.0.8:5432/firebot_ci"},
        {"DATABASE_URL": "postgresql+psycopg://u:pw@db.youdaozd.com:5432/firebot_e2e"},
        {"DATABASE_URL": "postgresql+psycopg://u:pw@localhost:5432/other_db"},
        {"DATABASE_URL": "postgresql+psycopg://u:pw@/firebot_ci?host=/run/pg"},
        {"REDIS_URL": "redis://redis:6379/15"},
        {"REDIS_URL": "redis://localhost:6379/0"},
        {"REDIS_URL": "redis://localhost:6379"},
        {"REDIS_URL": "redis://10.0.0.9:6379/15"},
    ],
)
def test_production_style_targets_refused(override):
    with pytest.raises(TargetGuardError):
        check_static({**OK, **override})


def test_guard_runs_before_first_write(monkeypatch):
    """fixture 中守卫位于 flushdb / 资产写入之前（源码顺序反证）。"""
    from pathlib import Path

    source = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    guard = source.index("assert_isolated_target()")
    for write in ("redis.flushdb()", "_prepare_vehicle_asset()", "ensure_isolated_test_admin"):
        position = source.find(write, source.index("def g2("))
        assert position == -1 or guard < position, write
