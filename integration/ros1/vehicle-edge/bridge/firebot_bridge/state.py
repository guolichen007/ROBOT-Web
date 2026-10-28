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
        # ROS 上报的当前任务（遥测字段；clear_ros_telemetry 会清）
        self.reported_active_task_id: str | None = None
        # Bridge 内部任务互斥锁（仅 CommandProcessor acquire/release，绝不因遥测清空）
        self.task_lock_id: str | None = None
        self.cancel_requested: bool = False
        # 数据缓存（ROS providers 写入，telemetry_loop 读取）
        self.last_battery: float | None = None
        self.last_smoke: float | None = None
        self.last_bottom_ir: float | None = None
        self.last_top_ir_max: float | None = None
        self.last_location: dict | None = None
        # location 只发布「新 revision」：set_location 递增 revision + 刷新更新时间，
        # location_loop 只发布尚未发送过的新 revision，避免旧位置被重新包装成新 MQTT。
        self.location_revision: int = 0
        self.location_updated_monotonic: float | None = None
        # freshness 时间戳（monotonic）：freshness 依据 = 消息是否持续到达，非数值是否变化
        self.battery_updated_monotonic: float | None = None
        self.smoke_updated_monotonic: float | None = None
        # 内部 stale 标记（只用于 recovered 判断，绝不进 MQTT/协议）
        self._battery_was_stale: bool = False
        self._smoke_was_stale: bool = False
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

    # ---- 任务锁（内部互斥，独立于 ROS 遥测）----
    def acquire_task(self, task_id: str) -> bool:
        with self._lock:
            # 空 task_id 不能形成有效锁，拒绝
            if not task_id or self.task_lock_id is not None:
                return False
            self.task_lock_id = task_id
            self.cancel_requested = False
            return True

    def release_task(self) -> None:
        # 只释放内部任务锁；绝不伪造 vehicle mode（mode 只能来自真实车端状态源）
        with self._lock:
            self.task_lock_id = None
            self.cancel_requested = False

    def request_cancel(self) -> None:
        with self._lock:
            self.cancel_requested = True

    # ---- 数据缓存 ----
    def set_battery(self, value: float | None) -> bool:
        """记录 battery 值并刷新 freshness（同值也刷新时间戳）。

        返回 True 表示之前已经 stale（即本次是 recovered）。
        """
        with self._lock:
            was_stale = self._battery_was_stale
            self.last_battery = value
            self.battery_updated_monotonic = time.monotonic()
            self._battery_was_stale = False
            return was_stale

    def set_smoke(self, value: float | None) -> bool:
        """记录 smoke 值并刷新 freshness（同值也刷新时间戳）。返回 True 表示 recovered。"""
        with self._lock:
            was_stale = self._smoke_was_stale
            self.last_smoke = value
            self.smoke_updated_monotonic = time.monotonic()
            self._smoke_was_stale = False
            return was_stale

    def set_location(self, value: dict | None) -> None:
        """记录 ROS location 并递增 revision；仅真实观测产生新的可发布 revision。"""
        with self._lock:
            self.last_location = value
            if value is not None:
                self.location_revision += 1
                self.location_updated_monotonic = time.monotonic()

    def get_location_revision(self) -> int:
        with self._lock:
            return self.location_revision

    def expire_stale_location(self, location_stale_seconds: float, now: float | None = None) -> bool:
        """location provider 断源超过 TTL 即清除，避免旧位置被重新包装成新消息。

        TTL <= 0 视为未启用（不清理）——但 Config 层对 location 的默认值是 fail-closed
        正数，不会落入「永不过期旧 location」的危险默认。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            if (
                self.last_location is not None
                and self.location_updated_monotonic is not None
                and location_stale_seconds > 0
                and now - self.location_updated_monotonic > location_stale_seconds
            ):
                self.last_location = None
                return True
            return False

    def apply_status(self, fields: dict) -> None:
        """只应用真实出现的 status 字段（partial），不伪造缺失字段。"""
        with self._lock:
            if "mode" in fields:
                self.mode = fields["mode"]
            if "estop_active" in fields:
                self.estop_active = fields["estop_active"]
            if "active_task_id" in fields:
                self.reported_active_task_id = fields["active_task_id"]

    def clear_ros_telemetry(self) -> None:
        """ROS 子进程丢失/降级时清空 ROS 来源数据，避免把旧数据包装成新消息上报。

        绝不清 task_lock_id（内部任务互斥锁，只有 CommandProcessor 能 acquire/release）。
        """
        with self._lock:
            self.last_battery = None
            self.last_smoke = None
            self.last_bottom_ir = None
            self.last_top_ir_max = None
            self.last_location = None
            self.location_updated_monotonic = None
            self.battery_updated_monotonic = None
            self.smoke_updated_monotonic = None
            self._battery_was_stale = False
            self._smoke_was_stale = False
            self.mode = None
            self.estop_active = None
            self.reported_active_task_id = None

    def expire_stale_telemetry(
        self,
        battery_stale_seconds: float,
        smoke_stale_seconds: float,
        now: float | None = None,
    ) -> dict:
        """超时清除 stale 数据；返回本次变为 stale 的 channel。

        freshness 依据 = 消息是否持续到达（monotonic 时间），不是数值是否变化。
        同一 stale 周期只触发一次（清除后 last_* 为 None，后续不再匹配）。
        TTL <= 0 表示 freshness guard 未启用，不清理。
        """
        now = time.monotonic() if now is None else now
        result = {"battery_stale": False, "smoke_stale": False}
        with self._lock:
            if (
                self.last_battery is not None
                and self.battery_updated_monotonic is not None
                and battery_stale_seconds > 0
                and now - self.battery_updated_monotonic > battery_stale_seconds
            ):
                self.last_battery = None
                self._battery_was_stale = True
                result["battery_stale"] = True
            if (
                self.last_smoke is not None
                and self.smoke_updated_monotonic is not None
                and smoke_stale_seconds > 0
                and now - self.smoke_updated_monotonic > smoke_stale_seconds
            ):
                self.last_smoke = None
                self._smoke_was_stale = True
                result["smoke_stale"] = True
        return result

    def snapshot_telemetry(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "estop_active": self.estop_active,
                "active_task_id": self.reported_active_task_id,
                "battery": self.last_battery,
                "smoke": self.last_smoke,
                "bottom_ir": self.last_bottom_ir,
                "top_ir_max": self.last_top_ir_max,
                "location": self.last_location,
            }
