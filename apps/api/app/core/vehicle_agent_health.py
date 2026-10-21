"""Shared fail-closed adapter heartbeat check (Redis TTL alone is insufficient)."""

from datetime import UTC, datetime

from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.events import get_redis

HEARTBEAT = "service:vehicle-agent-adapter:heartbeat"


def adapter_healthy(now: datetime | None = None) -> bool:
    try:
        value = get_redis().get(HEARTBEAT)
        if not value:
            return False
        age = ((now or datetime.now(UTC)) - datetime.fromisoformat(value)).total_seconds()
        return -2 <= age <= get_settings().vehicle_agent_v1_adapter_max_age_seconds
    except (ValueError, TypeError, OSError, RedisError):
        return False
