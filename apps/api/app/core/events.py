from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from redis import Redis
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.config import get_settings

EVENT_STREAM = "firebot:events"
logger = logging.getLogger(__name__)


@lru_cache
def get_redis() -> Any:
    return Redis.from_url(
        get_settings().effective_redis_url,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=2.0,
        retry_on_timeout=False,
    )


def append_event(event_type: str, payload: dict[str, Any]) -> str:
    event = {
        "event_type": event_type,
        "server_received_at": datetime.now(UTC).isoformat(),
        "payload": payload,
    }
    return str(
        get_redis().xadd(
            EVENT_STREAM,
            {"event": json.dumps(event, ensure_ascii=False, default=str)},
            maxlen=get_settings().event_stream_maxlen,
            approximate=True,
        )
    )


def queue_event(session: Session, event_type: str, payload: dict[str, Any]) -> None:
    """Publish a business-fact delta only after its PostgreSQL transaction commits."""
    session.info.setdefault("firebot_realtime_events", []).append((event_type, payload))


def queue_redis_set(
    session: Session, key: str, value: str, *, ttl_seconds: int | None = None
) -> None:
    session.info.setdefault("firebot_redis_after_commit", []).append(
        ("set", key, value, ttl_seconds)
    )


def queue_redis_delete(session: Session, key: str) -> None:
    session.info.setdefault("firebot_redis_after_commit", []).append(("delete", key, None, None))


@event.listens_for(Session, "after_commit")
def _publish_committed_events(session: Session) -> None:
    operations = session.info.pop("firebot_redis_after_commit", [])
    try:
        redis = get_redis()
        for operation, key, value, ttl_seconds in operations:
            if operation == "delete":
                redis.delete(key)
            elif ttl_seconds:
                redis.setex(key, ttl_seconds, value)
            else:
                redis.set(key, value)
    except Exception:
        # PostgreSQL has committed already. Do not turn a successful business
        # transaction into an ambiguous API failure because the cache is down.
        logger.exception("post-commit Redis operation failed")
    queued = session.info.pop("firebot_realtime_events", [])
    for event_type, payload in queued:
        try:
            append_event(event_type, payload)
        except Exception:
            logger.exception(
                "post-commit realtime event publish failed", extra={"event_type": event_type}
            )


@event.listens_for(Session, "after_rollback")
def _discard_rolled_back_events(session: Session) -> None:
    session.info.pop("firebot_realtime_events", None)
    session.info.pop("firebot_redis_after_commit", None)


def current_watermark() -> str:
    rows = get_redis().xrevrange(EVENT_STREAM, count=1)
    return str(rows[0][0]) if rows else "0-0"


def decode_stream_event(stream_id: str, fields: dict[str, str]) -> dict[str, Any]:
    event = json.loads(fields["event"])
    event["data"] = event.pop("payload")
    event["stream_id"] = stream_id
    return event
