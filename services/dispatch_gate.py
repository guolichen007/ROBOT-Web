"""Final publish gate for the command-dispatcher (WEB-R1A, R002_OPERATION_CONTRACT §7).

Every command leaving the platform on the CANONICAL_MQTT transport passes through
``publish_command()``, which the three entry points share: Outbox (``dispatch_next``),
Redis manual-control pubsub (``manual_loop``) and the Redis safety Stream
(``process_safety_events``). This gate is the last, API-independent check right before
``client.publish``: anything written to Outbox/Redis without going through the API's
authorisation is refused here.

Scope: CANONICAL_MQTT only. The VEHICLE_AGENT_V1 branch (001 independent access) and the
ROS_COMPAT refusal keep their existing logic unchanged; 001 is never added to the
canonical allowlist.

Default: REAL deny. With an empty ``CANONICAL_DOWNLINK_ALLOWLIST`` nothing is published.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from app.core.config import is_synthetic_namespace
from app.db.models import Command, Robot

logger = logging.getLogger("command-dispatcher.gate")

PRODUCTION_BROKER_HOSTS = frozenset({"firebot.youdaozd.com"})
AUTHORISED_LIFECYCLE = {"outbox": {"QUEUED"}, "safety": {"CREATED"}}
MAX_TTL = timedelta(seconds=240)
CLOCK_SKEW = timedelta(seconds=2)
CLAIM_PREFIX = "dispatch:fg:claim:"
DENY_COUNTER = "dispatch:fg:denied:{path}:{code}"
PATHS = frozenset({"outbox", "safety", "manual"})


class GateDenied(Exception):
    """Terminal refusal: never retried, never published."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _deny(redis, path: str, code: str, detail: str, payload: dict[str, Any]) -> GateDenied:
    try:
        redis.incr(DENY_COUNTER.format(path=path, code=code))
    except Exception:  # counters are observability only; refusal stands regardless
        logger.exception("gate counter failed")
    logger.warning(
        "final publish gate denied",
        extra={
            "gate_code": code,
            "gate_path": path,
            "vehicle_id": payload.get("vehicle_id"),
            "cmd": payload.get("cmd"),
            "command_id": payload.get("command_id"),
        },
    )
    return GateDenied(code, detail)


def final_publish_gate(
    payload: dict[str, Any],
    robot: Robot,
    *,
    db,
    redis,
    settings,
    now: datetime,
    path: str,
) -> str:
    """Raise GateDenied or return the replay-claim key (release it if the publish fails)."""
    if path not in PATHS:
        raise _deny(redis, "outbox", "FG_PATH_UNKNOWN", path, payload)
    vehicle_id = robot.vehicle_id
    cmd = payload.get("cmd")

    if settings.downlink_frozen:
        raise _deny(redis, path, "FG_DOWNLINK_FROZEN", "", payload)

    env = settings.firebot_deployment_env
    if env not in ("REAL", "SIM"):
        raise _deny(redis, path, "FG_ENV_INVALID", env, payload)
    synthetic = is_synthetic_namespace(vehicle_id)
    if (env == "SIM") != synthetic:
        raise _deny(redis, path, "FG_ENV_MISMATCH", f"{env}/{vehicle_id}", payload)
    if env == "SIM" and settings.mqtt_host in PRODUCTION_BROKER_HOSTS:
        raise _deny(redis, path, "FG_ENV_CREDENTIALS", settings.mqtt_host, payload)

    if vehicle_id not in settings.canonical_downlink_allowlist_set:
        raise _deny(redis, path, "FG_NOT_ALLOWLISTED", vehicle_id, payload)

    if payload.get("vehicle_id") != vehicle_id:
        raise _deny(redis, path, "FG_IDENTITY_MISMATCH", str(payload.get("vehicle_id")), payload)

    target_boot = payload.get("target_boot_id")
    if not (cmd == "emergency_stop" and target_boot is None):
        if robot.boot_id is None or target_boot != robot.boot_id:
            raise _deny(redis, path, "FG_BOOT_MISMATCH", str(target_boot), payload)

    issued = datetime.fromisoformat(payload["issued_at"])
    expires = datetime.fromisoformat(payload["expires_at"])
    if expires <= now:
        raise _deny(redis, path, "FG_EXPIRED", payload["expires_at"], payload)
    if issued - now > CLOCK_SKEW or expires - issued > MAX_TTL or expires <= issued:
        raise _deny(redis, path, "FG_TTL_INVALID", f"{issued}->{expires}", payload)

    if cmd == "manual_control":
        if path != "manual":
            raise _deny(
                redis, path, "FG_UNAUTHORIZED", "manual_control off the manual path", payload
            )
        raw = redis.get(f"manual:lease:{robot.id}")
        lease = json.loads(raw) if raw else None
        if (
            not lease
            or lease.get("lease_id") != payload.get("lease_id")
            or lease.get("control_session_id") != payload.get("control_session_id")
            or lease.get("user_id") != payload.get("operator_id")
        ):
            raise _deny(redis, path, "FG_UNAUTHORIZED", "no matching manual lease", payload)
    else:
        if path == "manual":
            raise _deny(redis, path, "FG_UNAUTHORIZED", f"{cmd} on the manual path", payload)
        row = db.query(Command).filter(Command.command_id == payload.get("command_id")).first()
        if (
            row is None
            or row.robot_id != robot.id
            or row.cmd != cmd
            or row.payload_json != payload
            or row.lifecycle_status not in AUTHORISED_LIFECYCLE[path]
        ):
            raise _deny(redis, path, "FG_UNAUTHORIZED", "no matching authorised Command", payload)

    claim = f"{CLAIM_PREFIX}{payload['command_id']}"
    ttl_ms = int((expires - now).total_seconds() * 1000) + 60_000
    if not redis.set(claim, path, nx=True, px=ttl_ms):
        raise _deny(redis, path, "FG_REPLAY", payload["command_id"], payload)
    return claim
