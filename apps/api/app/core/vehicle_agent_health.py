"""Shared fail-closed adapter heartbeat check (Redis TTL alone is insufficient)."""

from datetime import UTC, datetime

from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.events import get_redis
from app.core.service_identity import heartbeat_key

HEARTBEAT = "service:vehicle-agent-adapter:heartbeat"


def adapter_healthy(instance: str | None = None, now: datetime | None = None) -> bool:
    """instance=None：读兼容键（任意实例存活即健康，供 API 侧命令门/系统健康使用）。

    instance 显式给出：读该实例的命名空间心跳键（专属实例 healthcheck 用，
    双实例共存时避免读到另一实例的心跳）。
    """
    try:
        key = (
            heartbeat_key("vehicle-agent-adapter", instance)
            if instance
            else "service:vehicle-agent-adapter:heartbeat"
        )
        value = get_redis().get(key)
        if not value:
            return False
        age = ((now or datetime.now(UTC)) - datetime.fromisoformat(value)).total_seconds()
        return -2 <= age <= get_settings().vehicle_agent_v1_adapter_max_age_seconds
    except (ValueError, TypeError, OSError, RedisError):
        return False
