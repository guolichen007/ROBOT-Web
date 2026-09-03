"""enrollment token 生命周期单测：单次消费 / 重放拒绝 / 过期拒绝 / DEVICE_ID 绑定。"""
from __future__ import annotations

import time
from pathlib import Path

from app.modules.enrollment.token_store import TokenStore


def test_token_single_use_and_replay_rejected(tmp_path: Path) -> None:
    store = TokenStore(str(tmp_path), ttl_seconds=3600)
    token = store.issue("firebot-vehicle-01")
    assert store.consume("firebot-vehicle-01", token) is True
    # 重放：第二次使用同一 token 必须拒绝
    assert store.consume("firebot-vehicle-01", token) is False


def test_token_bound_to_device_id(tmp_path: Path) -> None:
    store = TokenStore(str(tmp_path), ttl_seconds=3600)
    token = store.issue("firebot-vehicle-01")
    # 不同 DEVICE_ID 使用同一 token 必须拒绝
    assert store.consume("firebot-vehicle-02", token) is False
    # 正确 DEVICE_ID 仍可用
    assert store.consume("firebot-vehicle-01", token) is True


def test_token_expiry_rejected(tmp_path: Path) -> None:
    store = TokenStore(str(tmp_path), ttl_seconds=1)
    token = store.issue("firebot-vehicle-01")
    time.sleep(1.1)
    assert store.consume("firebot-vehicle-01", token) is False


def test_wrong_token_rejected(tmp_path: Path) -> None:
    store = TokenStore(str(tmp_path), ttl_seconds=3600)
    store.issue("firebot-vehicle-01")
    assert store.consume("firebot-vehicle-01", "deadbeef" * 8) is False
