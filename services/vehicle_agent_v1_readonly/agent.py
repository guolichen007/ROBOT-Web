"""只读 Agent 主循环：周期上报 availability/status/telemetry，断线自动重连。

上行合同完全复用 ``services/vehicle_agent_v1.py``：
- availability: QoS1 retain=True，payload 为 "online"/"offline" 字符串；
- status: QoS1 retain=True，{online, programs, task, ts}；
- telemetry: QoS0 retain=False，{pose, battery, smog, thermal, ts}（未接入硬件显式 null）。
host 指标默认只写本地日志；"status-field" 为实验性扩展（默认关闭，见扩展方案文档）。

时序（G1-R0 A1）：status 默认 3s（云端 stale 门限 8s，留 2.6 倍余量）；
间隔门禁在 CLI 边界统一校验（``validate_interval``，拒绝负数/0/NaN/低于下限）。
确认（G1-R0 A2）：QoS1 发布必须收到 broker PUBACK 才视为成功，未确认只记警告，
绝不宣称已送达；在途/排队上限收紧，重连退避封顶，资源消耗有界。
"""

from __future__ import annotations

import json
import logging
import math
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
from services.vehicle_agent_v1_readonly.guard import legacy_bridge_running
from services.vehicle_agent_v1_readonly.identity import DeviceIdentity
from services.vehicle_agent_v1_readonly.transport import MqttConnectError, MqttTransport

logger = logging.getLogger("vehicle_agent_v1_readonly")

# G1-R0 A1：云端 vehicle_agent_v1_status_stale_seconds=8s；3s 周期留足重传/抖动余量。
STATUS_INTERVAL_SECONDS = 3.0
TELEMETRY_INTERVAL_SECONDS = 30.0
# 周期类参数下限：低于 1s 的配置视为异常（拒绝门禁在 CLI 边界执行）。
MIN_INTERVAL_SECONDS = 1.0
# G1-R1：运行期旧 Bridge 冲突检测周期（启动时由 OwnerGuard 同步检查一次）。
OWNER_CHECK_INTERVAL_SECONDS = 5.0

EXIT_OK = 0
EXIT_OWNER_CONFLICT = 3
EXIT_CONNECT_FAILED = 4


def validate_interval(name: str, value: float) -> float:
    """拒绝负数、0、NaN/Inf 与低于下限的间隔；合法则原样返回。"""
    if not math.isfinite(value) or value < MIN_INTERVAL_SECONDS:
        raise ValueError(f"{name} must be a finite number >= {MIN_INTERVAL_SECONDS}s; got {value}")
    return value


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
    max_reconnect_attempts: int | None = None  # None = 无限重试（退避封顶，资源有界）


class ReadOnlyAgent:
    """G0-A2/G1-R0 只读 Agent：采集真实 OS/进程状态并按 v1 合同上行。"""

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
        last_owner_check = float("-inf")
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
            online_confirmed = self._transport.publish_availability("online")
            last_status = last_telemetry = float("-inf")
            if online_confirmed:
                logger.info(
                    "connected as %s; availability=online published", self._identity.vehicle_id
                )
            else:
                logger.warning(
                    "connected as %s but availability=online publish UNCONFIRMED",
                    self._identity.vehicle_id,
                )
            while not self._stop_event.is_set():
                if not self._transport.connected:
                    logger.warning("connection lost; will reconnect")
                    break
                now = time.monotonic()
                # G1-R1：运行期通信 owner 冲突检测——旧 Bridge 在 Agent 运行期间被
                # 重新启动时，安全处置：发布 offline 并退出（fail-closed），
                # systemd Restart=on-failure 重启后会被启动守卫持续拒绝，直到冲突解除。
                if now - last_owner_check >= OWNER_CHECK_INTERVAL_SECONDS:
                    last_owner_check = now
                    if legacy_bridge_running(self._proc_root):
                        logger.critical(
                            "legacy vehicle-bridge appeared while agent running; "
                            "yielding communication ownership and shutting down"
                        )
                        self._transport.graceful_offline()
                        return EXIT_OWNER_CONFLICT
                if now - last_status >= self._config.status_interval_seconds:
                    self._publish_cycle("status")
                    last_status = now
                if now - last_telemetry >= self._config.telemetry_interval_seconds:
                    self._publish_cycle("telemetry")
                    last_telemetry = now
                self._transport.loop(0.1)
        was_connected = self._transport.connected
        offline_confirmed = self._transport.graceful_offline()
        if not was_connected:
            logger.info("agent stopped; was not connected, no offline publish needed")
        elif offline_confirmed:
            logger.info("graceful offline published and broker-confirmed; agent stopped")
        else:
            # A2：未收到 broker PUBACK 确认时，绝不宣称发布成功。
            logger.warning("graceful offline publish UNCONFIRMED by broker; agent stopped")
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
            confirmed = self._publish_safely(self._transport.publish_status, payload)
            if not confirmed:
                logger.warning("status publish UNCONFIRMED by broker; next cycle will re-send")
        else:
            payload = build_telemetry(snapshot.collected_at)
            self._publish_safely(self._transport.publish_telemetry, payload)

    def _publish_safely(self, publish, *args) -> bool:
        try:
            return bool(publish(*args))
        except (MqttConnectError, OSError, ValueError):
            logger.warning("publish failed; connection loop will handle", exc_info=True)
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
