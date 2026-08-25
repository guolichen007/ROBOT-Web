"""Field communication trace（结构化 FBTRACE 事件层）。

业务代码只通过统一接口 emit/transition/throttle/changed/command_received/latency_ms
输出**无 ANSI**、单行、可 grep 的稳定结构化事件；终端渲染由 tools/field_console.py 负责。

约束：
- 禁用（FIREBOT_FIELD_TRACE=false）时全部接口接近 no-op；
- 任何内部异常都被吞掉，绝不 raise 到业务调用方，绝不因 trace 影响通信；
- 绝不出现在 ROS child stdout IPC（那是 ros_adapter 专用通道）；
- 只输出安全摘要字段，绝不 dump 原始 MQTT/ROS JSON，绝不输出 secret。
"""
from __future__ import annotations

import json
import logging
import threading
import time

LOG = logging.getLogger("firebot-bridge")

TRACE_PREFIX = "FBTRACE\t"
_SECRET_KEY_PARTS = ("password", "authorization", "cookie", "token", "secret", "csrf")
_MAX_COMMAND_SEEN = 256
_MISSING = object()


def _is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def sanitize(value):
    """递归把含敏感关键词的 key 值替换为 <redacted>。"""
    if isinstance(value, dict):
        out: dict = {}
        for key, item in value.items():
            out[str(key)] = "<redacted>" if _is_secret_key(str(key)) else sanitize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


class FieldTrace:
    """唯一 trace 入口。保持 last-value / throttle / latency 的内部状态。"""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._last: dict = {}
        self._seen_at: dict = {}

    # ---- 基础 emit ----
    def emit(self, event: str, level: str = "info", **fields) -> None:
        if not self.enabled:
            return
        try:
            record = {"event": event, "level": level, "mono": round(time.monotonic(), 3)}
            record.update(sanitize(fields))
            LOG.info(
                "%s%s",
                TRACE_PREFIX,
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:  # noqa: BLE001 — trace 绝不能影响业务
            pass

    # ---- 状态 transition（只在变化时 emit；首次 emit 初始值）----
    def transition(self, key: str, new_value, event: str, **fields) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                old = self._last.get(key, _MISSING)
                if old is _MISSING:
                    self._last[key] = new_value
                    self.emit(event, **fields)
                elif old != new_value:
                    self._last[key] = new_value
                    self.emit(event, previous=old, **fields)
        except Exception:  # noqa: BLE001
            pass

    # ---- throttle（同一 key 每 interval_s 最多一条）----
    def throttle(self, key: str, interval_s: float, event: str, **fields) -> None:
        if not self.enabled:
            return
        try:
            now = time.monotonic()
            with self._lock:
                last = self._last.get(key)
                if last is not None and now - last < interval_s:
                    return
                self._last[key] = now
            self.emit(event, **fields)
        except Exception:  # noqa: BLE001
            pass

    # ---- changed（首次 emit；数值按 tolerance 判断变化）----
    def changed(self, key: str, value, event: str, *, tolerance=None, **fields) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                old = self._last.get(key, _MISSING)
                if old is _MISSING:
                    self._last[key] = value
                    self.emit(event, **fields)
                    return
                if tolerance is not None:
                    try:
                        should = abs(float(value) - float(old)) >= tolerance
                    except (TypeError, ValueError):
                        should = value != old
                else:
                    should = value != old
                if should:
                    self._last[key] = value
                    self.emit(event, previous=old, **fields)
        except Exception:  # noqa: BLE001
            pass

    # ---- command latency 观察上下文（独立容量有限 dict，绝不进 BridgeState）----
    def command_received(self, command: dict) -> None:
        if not self.enabled:
            return
        try:
            cid = command.get("command_id")
            if cid:
                with self._lock:
                    self._seen_at[cid] = time.monotonic()
                    while len(self._seen_at) > _MAX_COMMAND_SEEN:
                        oldest = min(self._seen_at, key=self._seen_at.get)  # type: ignore[arg-type]
                        self._seen_at.pop(oldest, None)
            self.emit(
                "mqtt.command.rx",
                level="rx",
                cmd=command.get("cmd"),
                command_id=cid,
                task_id=command.get("task_id"),
                vehicle_id=command.get("vehicle_id"),
                target_boot_id=command.get("target_boot_id"),
            )
        except Exception:  # noqa: BLE001
            pass

    def latency_ms(self, command_id) -> float | None:
        try:
            if command_id is None:
                return None
            with self._lock:
                start = self._seen_at.get(command_id)
            if start is None:
                return None
            return round((time.monotonic() - start) * 1000, 1)
        except Exception:  # noqa: BLE001
            return None
