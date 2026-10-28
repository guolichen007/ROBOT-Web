"""Field communication trace（结构化 FBTRACE 事件层）。

业务代码只通过统一接口 emit/transition/throttle/changed/command_received/latency_ms
输出**无 ANSI**、单行、可 grep 的稳定结构化事件；终端渲染由 tools/field_console.py 负责。

约束：
- 关键控制事件（critical/important）恒记录，不依赖 FIREBOT_FIELD_TRACE 开关；
- 遥测事件（telemetry）持久化受 FIREBOT_TELEMETRY_LOG_ENABLED 控制，journal 刷屏受
  FIREBOT_FIELD_TRACE 控制——「是否记录 / 是否刷屏 / 是否 verbose」三件事彼此独立；
- 任何内部异常都被吞掉，绝不 raise 到业务调用方，绝不因 trace 影响通信；
- 绝不出现在 ROS child stdout IPC（那是 ros_adapter 专用通道）；
- 只输出白名单内的安全摘要字段，绝不 dump 原始 MQTT/ROS JSON，绝不输出 secret。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

LOG = logging.getLogger("firebot-bridge")

TRACE_PREFIX = "FBTRACE\t"
_SECRET_KEY_PARTS = ("password", "authorization", "cookie", "token", "secret", "csrf")
_MAX_COMMAND_SEEN = 256
_MISSING = object()

# ---- 事件重要性分级（决定「是否落盘」与「是否受 FIELD_TRACE 门控」）----
# critical / important：控制审计，恒记录（events.jsonl + journal FBTRACE）。
# telemetry：遥测，落盘受 FIREBOT_TELEMETRY_LOG_ENABLED，journal 受 FIREBOT_FIELD_TRACE。
_IMPORTANCE = {
    "bridge.started": "critical",
    "bridge.stopping": "critical",
    "mqtt.connected": "critical",
    "mqtt.connect_failed": "critical",
    "mqtt.disconnected": "critical",
    "mqtt.command.rx": "critical",
    "ros.command.tx": "critical",
    "ros.command.tx_failed": "critical",
    "ros.feedback.rx": "critical",
    "mqtt.command_ack.tx": "critical",
    "mqtt.task_status.tx": "critical",
    "ros.master.changed": "critical",
    "ros.adapter.changed": "critical",
    "mqtt.subscribed": "important",
    "mqtt.availability.tx": "important",
    "mqtt.capabilities.tx": "important",
    "ros.child.spawned": "important",
    "ros.child.exited": "important",
    "ros.child.ready_timeout": "important",
    "ros.child.backoff": "important",
    "mqtt.command.ignored": "important",
    "mqtt.heartbeat.tx": "telemetry",
    "mqtt.status.tx": "telemetry",
    "mqtt.sensor.tx": "telemetry",
    "mqtt.location.tx": "telemetry",
    "ros.battery.rx": "telemetry",
    "ros.smoke.rx": "telemetry",
    "ros.status.rx": "telemetry",
    "ros.location.rx": "telemetry",
    "ros.battery.stale": "important",
    "ros.battery.recovered": "important",
    "ros.smoke.stale": "important",
    "ros.smoke.recovered": "important",
}

# ---- 字段白名单：每个事件只允许这些业务键进入结构化日志 ----
# 目的：防御未来误加 `**payload` dump 完整 MQTT/ROS 消息；sanitize 仍作第二层。
_BASE_KEYS = frozenset({
    "trace_schema_version", "timestamp_utc", "monotonic",
    "vehicle_id", "boot_id", "event_seq", "event", "level",
})
_ALWAYS_ALLOWED = frozenset({"previous", "latency_ms"})

_EVENT_ALLOWED_KEYS = {
    "bridge.started": {"vehicle", "boot", "protocol", "pid", "stub", "commands"},
    "bridge.stopping": {"boot"},
    "mqtt.connected": {"broker", "boot"},
    "mqtt.connect_failed": {"broker"},
    "mqtt.disconnected": {"rc"},
    "mqtt.subscribed": {"topic", "qos"},
    "mqtt.command.rx": {"cmd", "command_id", "task_id", "vehicle_id", "target_boot_id"},
    "mqtt.command.ignored": {"reason"},
    "mqtt.availability.tx": {"state", "reason"},
    "mqtt.capabilities.tx": {"commands", "sensors"},
    "mqtt.heartbeat.tx": {"seq", "uptime"},
    "mqtt.status.tx": {"battery", "mode", "estop_active", "active_task_id"},
    "mqtt.sensor.tx": {"smoke"},
    "mqtt.location.tx": set(),
    "mqtt.command_ack.tx": {"cmd", "status", "reason_code", "command_id", "task_id"},
    "mqtt.task_status.tx": {"task_id", "status", "phase", "progress", "failure_code"},
    "ros.master.changed": {"state"},
    "ros.child.spawned": {"pid", "generation"},
    "ros.child.exited": {"pid", "generation", "returncode"},
    "ros.child.ready_timeout": {"pid", "generation", "timeout_s"},
    "ros.child.backoff": {"seconds"},
    "ros.adapter.changed": {"state", "node", "publisher", "feedback"},
    "ros.command.tx": {"cmd", "command_id", "task_id"},
    "ros.command.tx_failed": {"reason", "cmd", "command_id", "task_id"},
    "ros.feedback.rx": {"cmd", "state", "command_id", "task_id", "reason_code", "message", "phase", "progress"},
    "ros.battery.rx": {"battery", "source"},
    "ros.smoke.rx": {"smoke", "source"},
    "ros.status.rx": {"mode", "estop_active", "active_task_id"},
    "ros.location.rx": {"x", "y", "theta", "localization_status", "enabled"},
    "ros.battery.stale": {"source", "age_seconds"},
    "ros.battery.recovered": {"source", "age_seconds"},
    "ros.smoke.stale": {"source", "age_seconds"},
    "ros.smoke.recovered": {"source", "age_seconds"},
}


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


def _apply_key_whitelist(event: str, record: dict) -> dict:
    """只保留 base schema 键 + 通用键 + 该事件白名单业务键，其余丢弃。"""
    allowed = _BASE_KEYS | _ALWAYS_ALLOWED | _EVENT_ALLOWED_KEYS.get(event, set())
    return {k: v for k, v in record.items() if k in allowed}


def _utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S") + f".{now.microsecond // 1000:03d}Z"


class FieldTrace:
    """唯一 trace 入口。保持 last-value / throttle / latency 的内部状态。"""

    def __init__(
        self,
        enabled: bool,
        telemetry_log_enabled: bool = True,
        vehicle_id: str = "",
        boot_id: str = "",
        recorder=None,
    ) -> None:
        self.enabled = enabled  # 只门控 telemetry 是否刷 journal（critical/important 恒刷）
        self.telemetry_log_enabled = telemetry_log_enabled  # 只门控 telemetry 是否落盘
        self.vehicle_id = vehicle_id
        self.boot_id = boot_id
        self.recorder = recorder
        self._lock = threading.Lock()
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._last: dict = {}
        self._seen_at: dict = {}

    # ---- 分级判定 ----
    def _importance_of(self, event: str, importance) -> str:
        if importance is not None:
            return importance
        return _IMPORTANCE.get(event, "telemetry")

    def _should_journal(self, imp: str) -> bool:
        return imp in ("critical", "important") or self.enabled

    def _should_persist(self, imp: str) -> bool:
        return imp in ("critical", "important") or self.telemetry_log_enabled

    # ---- 基础 emit ----
    def emit(self, event: str, level: str = "info", importance=None, **fields) -> None:
        imp = self._importance_of(event, importance)
        journal = self._should_journal(imp)
        persist = self._should_persist(imp)
        if not (journal or persist):
            return
        try:
            with self._seq_lock:
                self._seq += 1
                seq = self._seq
            record = {
                "trace_schema_version": 1,
                "timestamp_utc": _utc_now_iso(),
                "monotonic": round(time.monotonic(), 6),
                "vehicle_id": self.vehicle_id,
                "boot_id": self.boot_id,
                "event_seq": seq,
                "event": event,
                "level": level,
            }
            record.update(sanitize(fields))
            record = _apply_key_whitelist(event, record)
            if journal:
                LOG.info(
                    "%s%s",
                    TRACE_PREFIX,
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                )
            if persist and self.recorder is not None:
                self.recorder.enqueue(record, imp)
        except Exception:  # noqa: BLE001 — trace 绝不能影响业务
            pass

    # ---- 状态 transition（只在变化时 emit；首次 emit 初始值）----
    def transition(self, key: str, new_value, event: str, importance=None, **fields) -> None:
        imp = self._importance_of(event, importance)
        if not (self._should_journal(imp) or self._should_persist(imp)):
            return
        try:
            with self._lock:
                old = self._last.get(key, _MISSING)
                if old is _MISSING:
                    self._last[key] = new_value
                    self.emit(event, importance=imp, **fields)
                elif old != new_value:
                    self._last[key] = new_value
                    self.emit(event, importance=imp, previous=old, **fields)
        except Exception:  # noqa: BLE001
            pass

    # ---- throttle（同一 key 每 interval_s 最多一条）----
    def throttle(self, key: str, interval_s: float, event: str, importance=None, **fields) -> None:
        imp = self._importance_of(event, importance)
        if not (self._should_journal(imp) or self._should_persist(imp)):
            return
        try:
            now = time.monotonic()
            with self._lock:
                last = self._last.get(key)
                if last is not None and now - last < interval_s:
                    return
                self._last[key] = now
            self.emit(event, importance=imp, **fields)
        except Exception:  # noqa: BLE001
            pass

    # ---- changed（首次 emit；数值按 tolerance 判断变化）----
    def changed(self, key: str, value, event: str, *, tolerance=None, importance=None, **fields) -> None:
        imp = self._importance_of(event, importance)
        if not (self._should_journal(imp) or self._should_persist(imp)):
            return
        try:
            with self._lock:
                old = self._last.get(key, _MISSING)
                if old is _MISSING:
                    self._last[key] = value
                    self.emit(event, importance=imp, **fields)
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
                    self.emit(event, importance=imp, previous=old, **fields)
        except Exception:  # noqa: BLE001
            pass

    # ---- command latency 观察上下文（独立容量有限 dict，绝不进 BridgeState）----
    def command_received(self, command: dict) -> None:
        # 记账恒定执行（不受 enabled 门控），否则 latency_ms 在开关关时拿不到起始点
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
                importance="critical",
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
