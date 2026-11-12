"""逐车执行授权（车端独立授权源，与云端执行档互为复核）。

授权文件（root:firebot 0640）示例::

    {"vehicle_id": "firebot-vehicle-01",
     "execution_profile": "ros-software-test",
     "ros_bridge_socket": "/run/firebot-ros-bridge/bridge.sock",
     "not_after": "2026-12-31T00:00:00+08:00"}

缺失/畸形/过期/与设备身份不一致 → 执行器拒绝启动（readonly 默认模式不受影响）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from services.vehicle_agent_v1 import EXECUTION_PROFILES

GRANT_KEYS = {"vehicle_id", "execution_profile", "ros_bridge_socket", "not_after"}
REQUIRED_KEYS = {"vehicle_id", "execution_profile", "not_after"}


class GrantError(ValueError):
    """授权无效：执行器不得启动。"""


@dataclass(frozen=True)
class ExecutionGrant:
    vehicle_id: str
    execution_profile: str
    not_after: datetime
    ros_bridge_socket: Path | None = None

    def mode_for(self, command: str) -> str | None:
        return EXECUTION_PROFILES[self.execution_profile].get(command)

    def valid(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) < self.not_after


def load_grant(path: Path, vehicle_id: str, now: datetime | None = None) -> ExecutionGrant:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise GrantError(f"execution grant not readable: {exc}") from exc
    if mode & 0o027:
        raise GrantError(f"execution grant {path} permissions too open: {oct(mode & 0o777)}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GrantError(f"cannot read execution grant: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) - GRANT_KEYS or REQUIRED_KEYS - set(raw):
        raise GrantError("execution grant keys invalid")
    if raw["vehicle_id"] != vehicle_id:
        raise GrantError("execution grant vehicle_id does not match device identity")
    profile = raw["execution_profile"]
    if profile not in EXECUTION_PROFILES or profile == "telemetry-only":
        raise GrantError(f"execution_profile {profile!r} grants no command execution")
    not_after = datetime.fromisoformat(raw["not_after"])
    if not_after.tzinfo is None:
        raise GrantError("not_after must carry a timezone")
    socket_path = raw.get("ros_bridge_socket")
    grant = ExecutionGrant(
        vehicle_id=vehicle_id,
        execution_profile=profile,
        not_after=not_after,
        ros_bridge_socket=Path(socket_path) if socket_path else None,
    )
    if not grant.valid(now):
        raise GrantError("execution grant expired")
    if "SOFTWARE_TEST" in EXECUTION_PROFILES[profile].values() and grant.ros_bridge_socket is None:
        raise GrantError("software-test profile requires ros_bridge_socket")
    return grant
