"""G1-P2 双 Adapter/Ingress 共存隔离（共享 PG/Redis）单元与真实 Redis 测试。

- 心跳键命名空间：两个实例的心跳互不覆盖；healthcheck 显式 instance 只读自己的键。
- 兼容键语义：adapter_healthy() 不带 instance 时读兼容键（"任一实例存活"），
  API 侧命令门/系统健康不受实例拆分影响。
- 范围白名单：专属实例只处理白名单 vehicle_id，非目标车辆消息直接丢弃。
"""

from __future__ import annotations

import pytest
from app.core.config import get_settings
from app.core.service_identity import heartbeat_key, in_v1_scope, v1_scope_set


def test_heartbeat_key_namespaced_per_instance(monkeypatch):
    monkeypatch.setenv("FIREBOT_SERVICE_INSTANCE_ID", "mqtt-ingress-prod")
    assert heartbeat_key("mqtt-ingress") == "service:mqtt-ingress:heartbeat:mqtt-ingress-prod"
    monkeypatch.setenv("FIREBOT_SERVICE_INSTANCE_ID", "mqtt-ingress-r001")
    assert heartbeat_key("mqtt-ingress") == "service:mqtt-ingress:heartbeat:mqtt-ingress-r001"
    # 两实例键不同 → 不会互相覆盖
    assert (
        heartbeat_key("mqtt-ingress", "mqtt-ingress-prod")
        != heartbeat_key("mqtt-ingress", "mqtt-ingress-r001")
    )


def test_scope_set_and_filter(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vehicle_agent_scope_vehicle_ids", "firebot-vehicle-01")
    assert v1_scope_set() == {"firebot-vehicle-01"}
    assert in_v1_scope("firebot-vehicle-01") is True
    assert in_v1_scope("firebot-vehicle-02") is False
    assert in_v1_scope("R001") is False
    monkeypatch.setattr(settings, "vehicle_agent_scope_vehicle_ids", "")
    assert v1_scope_set() is None
    assert in_v1_scope("firebot-vehicle-02") is True  # 空范围 = 不限制


def test_adapter_healthy_plain_compat_key(monkeypatch):
    """兼容键语义：不传 instance 读兼容键；传 instance 读命名空间键。"""
    from datetime import UTC, datetime

    import app.core.vehicle_agent_health as vh
    import fakeredis

    store = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(vh, "get_redis", lambda: store)
    stamp = datetime.now(UTC).isoformat()
    store.set("service:vehicle-agent-adapter:heartbeat", stamp)
    store.set(heartbeat_key("vehicle-agent-adapter", "dedicated-r001"), stamp)
    assert vh.adapter_healthy() is True
    assert vh.adapter_healthy(instance="dedicated-r001") is True
    assert vh.adapter_healthy(instance="missing-instance") is False
    # 实例键删除不影响兼容键判定（命令门/系统健康"任一实例存活"语义）
    store.delete(heartbeat_key("vehicle-agent-adapter", "dedicated-r001"))
    assert vh.adapter_healthy() is True


def test_adapter_healthy_instance_not_confused(monkeypatch):
    """双实例共存：另一实例心跳新鲜不能救活本实例 healthcheck。"""
    from datetime import UTC, datetime

    import app.core.vehicle_agent_health as vh
    import fakeredis

    store = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(vh, "get_redis", lambda: store)
    store.set(
        heartbeat_key("vehicle-agent-adapter", "mqtt-ingress-other"),
        datetime.now(UTC).isoformat(),
    )
    assert vh.adapter_healthy(instance="mqtt-ingress-this") is False


@pytest.mark.skipif(
    not __import__("os").environ.get("REDIS_URL"), reason="真实 Redis 共存验证需 REDIS_URL"
)
def test_dual_instance_heartbeat_keys_live(monkeypatch):
    """真实 Redis：双实例键并存互不覆盖（CI 提供 redis service 时执行）。"""
    from app.core.events import get_redis

    redis = get_redis()
    key_a = heartbeat_key("mqtt-ingress", "instance-a")
    key_b = heartbeat_key("mqtt-ingress", "instance-b")
    redis.set(key_a, "ts-a", ex=30)
    redis.set(key_b, "ts-b", ex=30)
    assert redis.get(key_a).decode() == "ts-a"
    assert redis.get(key_b).decode() == "ts-b"
