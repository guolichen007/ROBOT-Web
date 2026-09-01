"""轻量本地 runtime status（供 verify.sh 读取；不含任何 secret）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading

_DEFAULT_FIELDS = {
    "boot_id": "",
    "vehicle_id": "",
    "protocol_version": "",
    "pid": None,
    "stub_mode": False,
    "supported_commands": [],
    "sensors": [],
    "location_enabled": False,
    "field_trace_enabled": False,
    "mqtt_connected": False,
    "ros_master_available": False,
    "ros_node_ready": False,
    "ros_command_publisher_ready": False,
    "ros_feedback_ready": False,
    "ros_provider_ready": False,
    "ros_adapter_ready": False,
    "battery_provider_seen": False,
    "battery_last_update": None,
    "battery_fresh": False,
    "smoke_provider_seen": False,
    "smoke_last_update": None,
    "smoke_fresh": False,
    "smoke_source": "UNKNOWN",
    # 事件日志健康（只进本地 status，不进 MQTT 协议）
    "event_logger_ready": False,
    "event_queue_depth": 0,
    "event_dropped": 0,
    "telemetry_dropped": 0,
    "write_errors": 0,
    "disk_log_available": True,
    "battery_source": "UNKNOWN",
}


def status_path() -> str:
    path = os.environ.get("FIREBOT_BRIDGE_STATUS_FILE")
    if path:
        return path
    # systemd RuntimeDirectory=/run/firebot-bridge
    run_dir = "/run/firebot-bridge"
    if os.path.isdir(run_dir) and os.access(run_dir, os.W_OK):
        return os.path.join(run_dir, "status.json")
    return "/tmp/firebot-bridge-status.json"


class RuntimeStatus:
    """线程安全的运行时状态；原子写盘（临时文件 + os.replace）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = dict(_DEFAULT_FIELDS)

    def set(self, **fields) -> None:
        with self._lock:
            self._data.update(fields)
            self._flush()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def _flush(self) -> None:
        try:
            path = status_path()
            directory = os.path.dirname(path) or "."
            fd, tmp = tempfile.mkstemp(prefix=".status-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._data, handle)
                os.replace(tmp, path)
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 — 状态文件写入失败不影响 Bridge 运行
            pass
