from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import IdempotencyRecord


def request_hash(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(body).hexdigest()


def lookup(
    db: Session, *, actor_id: str, endpoint: str, key: str, payload: dict[str, Any]
) -> IdempotencyRecord | None:
    row = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.endpoint == endpoint,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if row and row.request_hash != request_hash(payload):
        raise HTTPException(status_code=409, detail="相同 Idempotency-Key 对应不同请求内容")
    return row


def store(
    db: Session,
    *,
    actor_id: str,
    endpoint: str,
    key: str,
    payload: dict[str, Any],
    response: dict[str, Any],
    status_code: int = 201,
) -> None:
    db.add(
        IdempotencyRecord(
            actor_id=actor_id,
            endpoint=endpoint,
            idempotency_key=key,
            request_hash=request_hash(payload),
            response_status=status_code,
            response_json=response,
        )
    )
