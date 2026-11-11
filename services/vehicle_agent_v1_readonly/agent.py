"""只读 Agent 主循环：周期上报 availability/status/telemetry，断线自动重连。

上行合同完全复用 ``services/vehicle_agent_v1.py``：
- availability: QoS1 retain=True，payload 为 "online"/"offline" 字符串；
- status: QoS1 retain=True，{online, programs, task, ts}；
- telemetry: QoS0 retain=False，{pose, battery, smog, thermal, ts}（未接入硬件显式 null）。
host 指标默认只写本地日志；"status-field" 为实验性扩展（默认关闭，见扩展方案文档）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from services.vehicle_agent_v1_readonly import AGENT_VERSION
from services.vehicle_agent_v1_readonly.collector import (
    ROBOT_PROGRAM_NAMES,
    HostSnapshot,
    collect,
)
from services.vehicle_agent_v1_readonly.identity import DeviceIdentity
from services.vehicle_agent_v1_readonly.transport import MqttConnectError, MqttTransport

logger = logging.getLogger("vehicle_agent_v1_readonly")

STATUS_INTERVAL_SECONDS = 10.0
TELEMETRY_INTERVAL_SECONDS = 30.0

EXIT_OK = 0
EXIT_CONNECT_FAILED = 4


def idle_task() -> dict:
    """只读 Agent 不执行任何任务：固定 idle 语义，不生成任务状态。"""
    return {"state": "idle", "patrol_next": None, "patrol_count": 0, "patrol_paused": False}


def build_status(programs: dict[str, bool], ts: str, *, host: dict | None = None) -> dict:
    """构造 v1 status 上行；programs 是现有 Schema 的开放地图（dict[str, bool|str|None]）。"""
    status = {
        "online": True,
        "programs": {
            "agent": True,
            **{name: programs.get(name, False) for name in ROBOT_PROGRAM_NAMES},
        },
        "task": idle_task(),
        "ts": ts,
    }
    if host is not None:
        # 实验性扩展（默认关闭）：平台 UplinkModel extra=ignore 保证兼容（见扩展方案）。
        status["host"] = host
    return status


def build_telemetry(ts: str) -> dict:
    """未接入的底盘/导航/电池/定位/烟雾/热成像：显式 null，绝不生成模拟数值。"""
    return {"pose": None, "battery": None, "smog": None, "thermal": None, "ts": ts}


@dataclass
class AgentConfig:
    status_interval_seconds: float = STATUS_INTERVAL_SECONDS
    telemetry_interval_seconds: float = TELEMETRY_INTERVAL_SECONDS
    # host 指标模式：log-only（默认，只写本地日志）| status-field（实验性扩展）。
    host_metrics_mode: str = "log-only"
    host_metrics_path: Path | None = None
    max_reconnect_attempts: int | None = None  # None = 无限重试


class ReadOnlyAgent:
    """G0-A2 只读 Agent：采集真实 OS/进程状态并按 v1 合同上行。"""

    def __init__(
        self,
        identity: DeviceIdentity,
        transport: MqttTransport,
        config: AgentConfig,
        program_markers: dict[str, tuple[str, ...]],
        stop_event: Event,
        *,
        proc_root: Path = Path("/proc"),
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._identity = identity
        self._transport = transport
        self._config = config
        self._program_markers = program_markers
        self._stop_event = stop_event
        self._proc_root = proc_root
        self._sleep_fn = sleep_fn

    def run(self) -> int:
        reconnect_attempts = 0
        last_status = last_telemetry = float("-inf")
        while not self._stop_event.is_set():
            try:
                self._transport.connect()
                if not self._transport.wait_for_connect():
                    raise MqttConnectError("connect not acknowledged (rejected or timeout)")
            except MqttConnectError as exc:
                reconnect_attempts += 1
                if (
                    self._config.max_reconnect_attempts is not None
                    and reconnect_attempts > self._config.max_reconnect_attempts
                ):
                    logger.error(
                        "giving up after %d failed connection attempts", reconnect_attempts
                    )
                    return EXIT_CONNECT_FAILED
                logger.warning(
                    "connect failed (attempt %d): %s; retrying with backoff",
                    reconnect_attempts,
                    exc,
                )
                self._sleep_interruptible(
                    self._transport.config.backoff_delay(reconnect_attempts - 1)
                )
                continue
            reconnect_attempts = 0
            self._publish_safely(
                "availability online", self._transport.publish_availability, "online"
            )
            last_status = last_telemetry = float("-inf")
            logger.info("connected as %s; availability=online published", self._identity.vehicle_id)
            while not self._stop_event.is_set():
                if not self._transport.connected:
                    logger.warning("connection lost; will reconnect")
                    break
                now = time.monotonic()
                if now - last_status >= self._config.status_interval_seconds:
                    self._publish_cycle("status")
                    last_status = now
                if now - last_telemetry >= self._config.telemetry_interval_seconds:
                    self._publish_cycle("telemetry")
                    last_telemetry = now
                self._transport.loop(0.1)
        self._transport.graceful_offline()
        logger.info("graceful offline published; agent stopped")
        return EXIT_OK

    def _publish_cycle(self, kind: str) -> None:
        snapshot = collect(
            AGENT_VERSION,
            self._program_markers,
            now=datetime.now(UTC),
            proc_root=self._proc_root,
        )
        if self._config.host_metrics_mode == "log-only":
            self._log_host_metrics(snapshot)
        if kind == "status":
            host = snapshot.as_dict() if self._config.host_metrics_mode == "status-field" else None
            payload = build_status(snapshot.programs, snapshot.collected_at, host=host)
            self._publish_safely("status", self._transport.publish_status, payload)
        else:
            payload = build_telemetry(snapshot.collected_at)
            self._publish_safely("telemetry", self._transport.publish_telemetry, payload)

    def _publish_safely(self, label: str, publish, *args) -> bool:
        try:
            publish(*args)
            return True
        except (MqttConnectError, OSError):
            logger.warning("%s publish failed; connection loop will handle", label)
            return False

    def _log_host_metrics(self, snapshot: HostSnapshot) -> None:
        path = self._config.host_metrics_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(snapshot.as_dict(), ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("host metrics log write failed for %s", path, exc_info=True)

    def _sleep_interruptible(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop_event.is_set():
            self._sleep_fn(min(0.1, max(0.0, deadline - time.monotonic())))
