"""双向执行 Agent 主循环。

线程模型：paho 网络线程（重连/keepalive）｜命令工作线程（执行，可能阻塞于 ROS 反馈）｜
主线程（availability/status/telemetry 心跳）。命令执行永不阻塞心跳与重连。

会话：每次 MQTT 连接建立生成新的 boot_id，随 live availability（dict 形态）与 status 上报；
云端 Ingress 采纳为 robot.boot_id，命令 target_boot_id 即此值。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from services.vehicle_agent_v1_exec import AGENT_VERSION
from services.vehicle_agent_v1_exec.executor import CommandExecutor
from services.vehicle_agent_v1_exec.session import SessionValidator
from services.vehicle_agent_v1_exec.transport import DuplexTransport
from services.vehicle_agent_v1_readonly.agent import build_status, build_telemetry
from services.vehicle_agent_v1_readonly.collector import collect
from services.vehicle_agent_v1_readonly.guard import legacy_bridge_running

logger = logging.getLogger("vehicle_agent_v1_exec")

STATUS_INTERVAL_SECONDS = 3.0
TELEMETRY_INTERVAL_SECONDS = 30.0
LIVE_ONLINE_INTERVAL_SECONDS = 5.0
OWNER_CHECK_INTERVAL_SECONDS = 5.0
EXIT_OK = 0
EXIT_OWNER_CONFLICT = 3


class DuplexAgent:
    def __init__(
        self,
        transport: DuplexTransport,
        validator: SessionValidator,
        executor: CommandExecutor,
        markers: dict[str, tuple[str, ...]],
        stop_event: threading.Event,
        *,
        proc_root: Path = Path("/proc"),
        status_interval: float = STATUS_INTERVAL_SECONDS,
        telemetry_interval: float = TELEMETRY_INTERVAL_SECONDS,
    ) -> None:
        self.transport = transport
        self.validator = validator
        self.executor = executor
        self.markers = markers
        self.stop_event = stop_event
        self.proc_root = proc_root
        self.status_interval = status_interval
        self.telemetry_interval = telemetry_interval
        self._session_announced: str | None = None

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                payload = self.transport.commands.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.executor.handle(payload)
            except Exception:  # noqa: BLE001 - 单条命令异常不得终止工作线程
                logger.exception("command handling failed")

    def status_payload(self) -> dict:
        snapshot = collect(
            AGENT_VERSION, self.markers, now=datetime.now(UTC), proc_root=self.proc_root
        )
        status = build_status(snapshot.programs, snapshot.collected_at)
        status["boot_id"] = self.validator.boot_id
        software = self.executor.software_status()
        if software is not None:
            status["software"] = software
        return status

    def run(self) -> int:
        worker = threading.Thread(target=self._worker, name="v1-exec-worker", daemon=True)
        worker.start()
        self.transport.start()
        last_status = last_telemetry = last_live = last_owner = float("-inf")
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now - last_owner >= OWNER_CHECK_INTERVAL_SECONDS:
                    last_owner = now
                    if legacy_bridge_running(self.proc_root):
                        logger.critical("legacy vehicle-bridge detected; yielding ownership")
                        self.transport.graceful_offline()
                        return EXIT_OWNER_CONFLICT
                if not self.transport.connected:
                    self._session_announced = None
                    self.stop_event.wait(0.1)
                    continue
                boot = self.validator.boot_id
                if self._session_announced != boot:
                    # 新会话：retained online + live 会话声明（携带 boot_id）
                    self.transport.publish("availability", "online", retain=True)
                    self.transport.publish(
                        "availability", {"state": "online", "boot_id": boot}, retain=False
                    )
                    self._session_announced = boot
                    last_live = last_status = last_telemetry = float("-inf")
                    logger.info("session %s announced", boot)
                if now - last_live >= LIVE_ONLINE_INTERVAL_SECONDS:
                    if self.transport.publish(
                        "availability", {"state": "online", "boot_id": boot}, retain=False
                    ):
                        last_live = now
                if now - last_status >= self.status_interval:
                    self.transport.publish("status", self.status_payload())
                    last_status = now
                if now - last_telemetry >= self.telemetry_interval:
                    snapshot = collect(
                        AGENT_VERSION, self.markers, now=datetime.now(UTC), proc_root=self.proc_root
                    )
                    self.transport.publish("telemetry", build_telemetry(snapshot.collected_at))
                    last_telemetry = now
                self.stop_event.wait(0.1)
        finally:
            self.stop_event.set()
            confirmed = self.transport.graceful_offline()
            logger.info("agent stopped (offline confirmed=%s)", confirmed)
            worker.join(timeout=2)
        return EXIT_OK
