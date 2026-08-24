"""轻量本地 runtime status（供 verify.sh 读取；不含任何 secret）。"""
from __future__ import annotations

import json
import os
import threading


def status_path() -> str:
    path = os.environ.get("FIREBOT_BRIDGE_STATUS_FILE")
    if path:
        return path
    for candidate in ("/var/run/firebot-bridge/status.json", "/tmp/firebot-bridge-status.json"):
        parent = os.path.dirname(candidate)
        if os.path.isdir(parent) and os.access(parent, os.W_OK):
            return candidate
    return "/tmp/firebot-bridge-status.json"


class RuntimeStatus:
    """线程安全的运行时状态，落盘为 JSON 供 verify.sh 读取。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = {
            "boot_id": "",
            "mqtt_connected": False,
            "ros_master_available": False,
            "ros_adapter_ready": False,
        }

    def set(self, **fields) -> None:
        with self._lock:
            self._data.update(fields)
            self._flush()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def _flush(self) -> None:
        try:
            with open(status_path(), "w", encoding="utf-8") as handle:
                json.dump(self._data, handle)
        except Exception:  # noqa: BLE001 — 状态文件写入失败不影响 Bridge 运行
            pass
