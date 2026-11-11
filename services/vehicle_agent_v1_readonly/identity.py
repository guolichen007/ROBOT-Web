"""设备身份：来自经过验证的单一配置源（device_identity.json）。

安全边界：
- 绝不读取旧 bridge.env；即使被误传为配置文件路径也拒绝。
- 若环境中存在旧身份变量（FIREBOT_VEHICLE_ID / BRIDGE_VEHICLE_ID），直接拒绝启动，
  以可验证的方式证明未使用旧身份来源。
- MQTT username 必须等于 canonical vehicle_id（broker ACL ``%u`` pattern 合同）。
- 冻结身份（历史 synthetic R001、R002）在配置层直接拒绝。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from services.vehicle_agent_v1 import VEHICLE_ID

# 历史 synthetic R001 保持冻结；R002 firebot-vehicle-02 完全禁止触碰。
FORBIDDEN_VEHICLE_IDS = {"R001", "R002", "firebot-vehicle-02", "r001", "r002"}
LEGACY_ENV_VARS = ("FIREBOT_VEHICLE_ID", "BRIDGE_VEHICLE_ID")
# 占位凭证标记：仅本地隔离验收允许（--allow-placeholder-credentials）；G1 正式凭证不得含此标记。
PLACEHOLDER_PASSWORD_MARKER = "CHANGE-ME"
IDENTITY_CONFIG_KEYS = {
    "vehicle_id",
    "display_name",
    "mqtt_host",
    "mqtt_port",
    "mqtt_username",
    "mqtt_password",
    "ca_cert_path",
}


class IdentityError(ValueError):
    """身份配置无效或来源不合法。"""


@dataclass(frozen=True)
class DeviceIdentity:
    vehicle_id: str
    display_name: str
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    ca_cert_path: Path

    @property
    def client_id(self) -> str:
        return f"{self.vehicle_id}-readonly-v1"


def assert_no_legacy_identity_env() -> None:
    for name in LEGACY_ENV_VARS:
        if os.environ.get(name):
            raise IdentityError(
                f"legacy identity env {name} is set; refusing to start — "
                "identity must come only from device_identity.json"
            )


def load_identity(config_path: Path, *, allow_placeholder_password: bool = False) -> DeviceIdentity:
    assert_no_legacy_identity_env()
    if config_path.name == "bridge.env":
        raise IdentityError("bridge.env is a legacy identity source and must never be read")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityError(f"cannot read identity config {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise IdentityError("identity config must be a JSON object")
    unknown = set(raw) - IDENTITY_CONFIG_KEYS
    if unknown:
        raise IdentityError(f"unknown identity config keys: {sorted(unknown)}")
    missing = IDENTITY_CONFIG_KEYS - set(raw)
    if missing:
        raise IdentityError(f"missing identity config keys: {sorted(missing)}")
    vehicle_id = str(raw["vehicle_id"])
    if not VEHICLE_ID.fullmatch(vehicle_id):
        raise IdentityError("vehicle_id does not match vehicle_agent_v1 contract pattern")
    if vehicle_id in FORBIDDEN_VEHICLE_IDS:
        raise IdentityError(
            f"vehicle_id {vehicle_id!r} is frozen/forbidden (synthetic R001 / R002)"
        )
    username = str(raw["mqtt_username"])
    if username != vehicle_id:
        raise IdentityError("mqtt_username must equal vehicle_id (broker ACL %u contract)")
    password = str(raw["mqtt_password"])
    if not password:
        raise IdentityError("mqtt_password must not be empty")
    if PLACEHOLDER_PASSWORD_MARKER in password and not allow_placeholder_password:
        raise IdentityError(
            "placeholder credential detected; provision per-device credentials before "
            "production use (local acceptance may pass --allow-placeholder-credentials)"
        )
    ca_cert_path = Path(str(raw["ca_cert_path"])).expanduser()
    if not ca_cert_path.is_file():
        raise IdentityError(f"ca_cert_path not readable: {ca_cert_path}")
    try:
        port = int(raw["mqtt_port"])
    except (TypeError, ValueError) as exc:
        raise IdentityError("mqtt_port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise IdentityError("mqtt_port out of range")
    return DeviceIdentity(
        vehicle_id=vehicle_id,
        display_name=str(raw["display_name"]),
        mqtt_host=str(raw["mqtt_host"]),
        mqtt_port=port,
        mqtt_username=username,
        mqtt_password=password,
        ca_cert_path=ca_cert_path,
    )
