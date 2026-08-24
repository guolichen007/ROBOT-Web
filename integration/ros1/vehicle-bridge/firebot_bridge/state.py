"""车端 Bridge 共享状态（线程安全）。"""
from __future__ import annotations

import threading
import time

# command_id 幂等缓存 TTL（QoS1 重复投递窗口）
_DEDUP_TTL_SECONDS = 300


class BridgeState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 车端模式（schema 枚举）。初始 None = 车端未提供，不得伪造（v1.3 partial）
        self.mode: str | None = None
        # 软件急停锁存位。初始 None = 车端未提供，不得伪造
        self.estop_active: bool | None = None
        self.active_task_id: str | None = None
        self.cancel_requested: bool = False
        # 数据缓存（ROS providers 写入，telemetry_loop 读取）
        self.last_battery: float | None = None
        self.last_smoke: float | None = None
        self.last_bottom_ir: float | None = None
        self.last_top_ir_max: float | None = None
        self.last_location: dict | None = None
        # command_id 幂等缓存：command + 最新 ACK + 最新 task_status + 是否终态
        self._processed: dict[str, dict] = {}

    def _cleanup_locked(self, now: float) -> None:
        expired = [
            k for k, rec in self._processed.items() if now - rec["ts"] > _DEDUP_TTL_SECONDS
        ]
        for k in expired:
            self._processed.pop(k, None)

    # ---- 命令幂等（QoS1 重放语义，不重复发布 ROS command）----
    def register_command(self, command_id: str, command: dict) -> None:
        with self._lock:
            self._cleanup_locked(time.time())
            self._processed[command_id] = {
                "ts": time.time(),
                "command": command,
                "ack": None,
                "task_status": None,
                "terminal": False,
            }

    def get_command_record(self, command_id: str) -> dict | None:
        with self._lock:
            self._cleanup_locked(time.time())
            return self._processed.get(command_id)

    def remember_ack(self, command_id: str, ack: dict) -> None:
        with self._lock:
            rec = self._processed.get(command_id)
            if rec:
                rec["ack"] = ack
                rec["ts"] = time.time()

    def remember_task_status(self, command_id: str, task_status: dict) -> None:
        with self._lock:
            rec = self._processed.get(command_id)
            if rec:
                rec["task_status"] = task_status
                rec["ts"] = time.time()

    def mark_terminal(self, command_id: str) -> None:
        with self._lock:
            rec = self._processed.get(command_id)
            if rec:
                rec["terminal"] = True
                rec["ts"] = time.time()

    # ---- 任务锁 ----
    def acquire_task(self, task_id: str) -> bool:
        with self._lock:
            # 空 task_id 不能形成有效锁，拒绝
            if not task_id or self.active_task_id is not None:
                return False
            self.active_task_id = task_id
            self.cancel_requested = False
            return True

    def release_task(self) -> None:
        # 只释放内部任务锁；绝不伪造 vehicle mode（mode 只能来自真实车端状态源）
        with self._lock:
            self.active_task_id = None
            self.cancel_requested = False

    def request_cancel(self) -> None:
        with self._lock:
            self.cancel_requested = True

    # ---- 数据缓存 ----
    def set_battery(self, value: float | None) -> None:
        with self._lock:
            self.last_battery = value

    def set_smoke(self, value: float | None) -> None:
        with self._lock:
            self.last_smoke = value

    def set_location(self, value: dict | None) -> None:
        with self._lock:
            self.last_location = value

    def apply_status(self, fields: dict) -> None:
        """只应用真实出现的 status 字段（partial），不伪造缺失字段。"""
        with self._lock:
            if "mode" in fields:
                self.mode = fields["mode"]
            if "estop_active" in fields:
                self.estop_active = fields["estop_active"]
            if "active_task_id" in fields:
                self.active_task_id = fields["active_task_id"]

    def snapshot_telemetry(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "estop_active": self.estop_active,
                "active_task_id": self.active_task_id,
                "battery": self.last_battery,
                "smoke": self.last_smoke,
                "bottom_ir": self.last_bottom_ir,
                "top_ir_max": self.last_top_ir_max,
                "location": self.last_location,
            }
