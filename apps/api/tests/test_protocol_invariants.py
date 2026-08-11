import json
from datetime import datetime

import jwt
import pytest
from app.core.events import decode_stream_event
from app.core.idempotency import request_hash
from app.core.security import (
    create_access_token,
    create_refresh_token,
    csrf_digest,
    decode_access_token,
    decode_refresh_token,
)
from app.db.models import Robot
from app.modules.commands.service import build_command_payload


def test_idempotency_hash_is_canonical() -> None:
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})
    assert request_hash({"a": 1}) != request_hash({"a": 2})


def test_stream_event_exposes_watermark_and_data() -> None:
    fields = {
        "event": json.dumps(
            {
                "event_type": "robot.location",
                "server_received_at": "2026-08-10T00:00:00Z",
                "payload": {"vehicle_id": "R001", "x": 1.5},
            }
        )
    }
    event = decode_stream_event("100-7", fields)
    assert event["stream_id"] == "100-7"
    assert event["data"]["x"] == 1.5
    assert "payload" not in event


def test_access_and_refresh_tokens_are_not_interchangeable() -> None:
    access = create_access_token("user-1", ["robot.read"])
    refresh = create_refresh_token("user-1", "family-1")
    assert decode_access_token(access)["type"] == "access"
    assert decode_refresh_token(refresh)["family_id"] == "family-1"
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(refresh)
    with pytest.raises(jwt.InvalidTokenError):
        decode_refresh_token(access)


def test_csrf_digest_is_keyed_and_stable() -> None:
    assert csrf_digest("token") == csrf_digest("token")
    assert csrf_digest("token") != csrf_digest("other")


def test_command_identity_and_expiry_are_explicit() -> None:
    robot = Robot(
        id="robot-id",
        vehicle_id="R001",
        site_id="site-id",
        name="R001",
        boot_id="00000000-0000-4000-8000-000000000002",
    )
    payload = build_command_payload(
        robot=robot,
        operator_id="user-id",
        cmd="stop_motion",
        params={},
        ttl_ms=3000,
        priority=95,
    )
    assert payload["schema_version"] == "1.2"
    assert payload["target_boot_id"] == robot.boot_id
    assert "boot_id" not in payload
    assert payload["command_id"].startswith("C")
    assert datetime.fromisoformat(payload["expires_at"]) > datetime.fromisoformat(
        payload["issued_at"]
    )
