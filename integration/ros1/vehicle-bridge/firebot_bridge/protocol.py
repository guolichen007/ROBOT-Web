"""Firebot schema 1.3 协议工具：消息构造、seq、命令校验。

协议版本兼容：接受 schema_version 1.2 与 1.3（服务器迁移期双版本）。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

# schema 1.3 command 必填字段（server command envelope）
COMMAND_REQUIRED_FIELDS = [
    "schema_version", "message_id", "type", "vehicle_id", "target_boot_id",
    "command_id", "correlation_id", "issued_at", "expires_at", "ttl_ms",
    "priority", "source", "operator_id", "cmd", "params",
]
SUPPORTED_SCHEMA_VERSIONS = ("1.2", "1.3")
COMMAND_ENUM = {
    "manual_control", "stop_motion", "emergency_stop", "reset_estop",
    "return_dock", "patrol", "extinguish", "cancel_task",
}

# 任务类命令：需要非空 task_id 且需独占任务锁
TASK_CMDS = {"patrol", "return_dock", "extinguish"}

# ACK reason_code 枚举白名单（schema 1.3，仅允许这些值）
ACK_REASON_CODES = {
    None,
    "ROBOT_DISABLED", "ROBOT_STALE", "ROBOT_OFFLINE", "ROBOT_BOOT_SESSION_UNKNOWN",
    "ROBOT_CAPABILITY_UNSUPPORTED", "ROBOT_ESTOP_ACTIVE", "MANUAL_LEASE_INVALID",
    "ACTIVE_TASK_CONFLICT", "MAP_VERSION_MISMATCH", "COMMAND_EXPIRED",
    "COMMAND_REJECTED", "COMMAND_UNSUPPORTED", "INVALID_PROTOCOL_MESSAGE",
    "PROTOCOL_VERSION_UNSUPPORTED", "BRIDGE_ADAPTER_NOT_CONNECTED",
}


class Protocol:
    """每消息类型的单调递增 seq + 消息构造。线程安全。"""

    def __init__(self, vehicle_id: str, boot_id: str) -> None:
        self.vehicle_id = vehicle_id
        self.boot_id = boot_id
        self._seq: dict[str, int] = {}
        self._lock = threading.Lock()

    def next_seq(self, msg_type: str) -> int:
        with self._lock:
            self._seq[msg_type] = self._seq.get(msg_type, 0) + 1
            return self._seq[msg_type]

    @staticmethod
    def iso_utc(sec: float | None = None) -> str:
        if sec is not None:
            try:
                return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat()
            except Exception:  # noqa: BLE001
                pass
        return datetime.now(timezone.utc).isoformat()

    def base(self, msg_type: str, stamp_sec: float | None = None) -> dict:
        """schema vehicleBase + seq。"""
        return {
            "schema_version": "1.3",
            "message_id": str(uuid.uuid4()),
            "type": msg_type,
            "vehicle_id": self.vehicle_id,
            "boot_id": self.boot_id,
            "timestamp": self.iso_utc(stamp_sec),
            "seq": self.next_seq(msg_type),
        }

    def topic(self, name: str) -> str:
        return f"robot/{self.vehicle_id}/{name}"


def validate_command(command: dict, current_boot_id: str) -> tuple[bool, str | None]:
    """校验下行 command。

    Returns:
        (ok, reason_code) — ok=False 时 reason_code 为拒绝原因（枚举白名单）。
    """
    if not isinstance(command, dict):
        return False, "INVALID_PROTOCOL_MESSAGE"
    version = str(command.get("schema_version", ""))
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        return False, "PROTOCOL_VERSION_UNSUPPORTED"
    for field in COMMAND_REQUIRED_FIELDS:
        if field not in command:
            return False, "INVALID_PROTOCOL_MESSAGE"
    if command.get("type") != "command":
        return False, "INVALID_PROTOCOL_MESSAGE"
    cmd = command.get("cmd")
    if cmd not in COMMAND_ENUM:
        return False, "COMMAND_UNSUPPORTED"
    # 过期检查
    try:
        from datetime import datetime as _dt

        expires = _dt.fromisoformat(command["expires_at"].replace("Z", "+00:00"))
        if expires <= _dt.now(timezone.utc):
            return False, "COMMAND_EXPIRED"
    except Exception:  # noqa: BLE001
        return False, "INVALID_PROTOCOL_MESSAGE"
    # boot 会话校验：emergency_stop 允许 target_boot_id 为 null 或当前 boot；
    # 显式给了另一个 boot 则拒绝（不默默放行跨会话急停）
    target_boot = command.get("target_boot_id")
    if cmd == "emergency_stop":
        if target_boot not in (None, current_boot_id):
            return False, "ROBOT_BOOT_SESSION_UNKNOWN"
    elif target_boot != current_boot_id:
        return False, "ROBOT_BOOT_SESSION_UNKNOWN"
    return True, None
