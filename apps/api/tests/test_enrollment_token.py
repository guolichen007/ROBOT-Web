"""enrollment token 生命周期单测。

单次消费 / 重放拒绝 / 过期拒绝 / DEVICE_ID 绑定 / 并发单次成功。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from app.db.session import SessionLocal
from app.modules.enrollment.token_store import consume_token, issue_token


def _issue(device_id: str, ttl_seconds: int = 3600) -> str:
    with SessionLocal.begin() as db:
        token = issue_token(db, device_id, {"mqtt_password": f"pw-{device_id}"}, ttl_seconds)
    return token


def test_token_single_use_and_replay_rejected() -> None:
    device = "firebot-vehicle-11"
    token = _issue(device)
    with SessionLocal.begin() as db:
        assert consume_token(db, device, token) is not None
    # 重放：第二次使用同一 token 必须拒绝
    with SessionLocal.begin() as db:
        assert consume_token(db, device, token) is None


def test_token_bound_to_device_id() -> None:
    device = "firebot-vehicle-12"
    token = _issue(device)
    with SessionLocal.begin() as db:
        # 不同 DEVICE_ID 使用同一 token 必须拒绝
        assert consume_token(db, "firebot-vehicle-13", token) is None
    with SessionLocal.begin() as db:
        assert consume_token(db, device, token) is not None


def test_token_expiry_rejected() -> None:
    device = "firebot-vehicle-14"
    token = _issue(device, ttl_seconds=1)
    # 直接改过期时间模拟过期（不走真实 sleep，避免 CI 变慢）
    from app.db.models import EnrollmentToken
    from sqlalchemy import select

    with SessionLocal.begin() as db:
        row = db.scalar(select(EnrollmentToken).where(EnrollmentToken.device_id == device))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with SessionLocal.begin() as db:
        assert consume_token(db, device, token) is None


def test_wrong_token_rejected() -> None:
    device = "firebot-vehicle-15"
    _issue(device)
    with SessionLocal.begin() as db:
        assert consume_token(db, device, "deadbeef" * 8) is None


def test_concurrent_consume_single_success() -> None:
    device = "firebot-vehicle-16"
    token = _issue(device)
    results: list[bool] = []

    def worker() -> None:
        with SessionLocal.begin() as db:
            results.append(consume_token(db, device, token) is not None)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 1, f"并发下成功次数应为 1，实际 {sum(results)}"
