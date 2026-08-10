from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from redis import Redis

from app.core.config import get_settings

EVENT_STREAM = "firebot:events"


@lru_cache
def get_redis() -> Any:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


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


def current_watermark() -> str:
    rows = get_redis().xrevrange(EVENT_STREAM, count=1)
    return str(rows[0][0]) if rows else "0-0"


def decode_stream_event(stream_id: str, fields: dict[str, str]) -> dict[str, Any]:
    event = json.loads(fields["event"])
    event["data"] = event.pop("payload")
    event["stream_id"] = stream_id
    return event
