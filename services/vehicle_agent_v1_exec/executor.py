"""命令执行：会话校验 → 幂等 → 诊断（真实本机采集）/ 软件测试（ROS 软件桥）→ ACK/反馈。

ACK 语义（v1/robot/{id}/ack，QoS1，retain=false）：
- 诊断：执行完成后回 ACK（OK + 真实数据 + 耗时；失败带明确 code）。
- 软件测试：校验通过即回 ACK（data.stage=RECEIVED，表示车端已收到并接受投递），之后 ROS
  软件反馈以 event{type:software_feedback} 逐级上报（ACCEPTED/EXECUTING/COMPLETED/FAILED）。
  ACK 只代表车端接收，不代表软件完成；软件完成以 COMPLETED 反馈为准。
- 拒绝：EXPIRED / STALE_BOOT / WRONG_VEHICLE / NOT_AUTHORIZED / MODE_MISMATCH / INVALID_PARAM，
  确定未执行。REAL 模式在本执行器中不存在实现路径 → NO_HARDWARE。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.vehicle_agent_v1_exec import AGENT_VERSION
from services.vehicle_agent_v1_exec.session import SessionValidator, Verdict, fingerprint
from services.vehicle_agent_v1_readonly.agent import build_status, build_telemetry
from services.vehicle_agent_v1_readonly.collector import collect

logger = logging.getLogger("vehicle_agent_v1_exec")
DIAGNOSTIC_TIMEOUT_SECONDS = 10.0
BRIDGE_CONNECT_TIMEOUT_SECONDS = 3.0
# G2-R1：ROS 软件长任务异步执行（有界并发）；诊断/停止命令不被长任务阻塞
SOFTWARE_WORKERS = 4
# 软件任务锁：同一时刻最多一个“启动类”任务；停止类命令不受此锁阻塞
LONG_RUNNING = {"start", "close", "patrol"}
STOP_COMMANDS = {"stop_patrol"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CommandExecutor:
    def __init__(
        self,
        validator: SessionValidator,
        publish: Callable[..., bool],
        *,
        program_markers: dict[str, tuple[str, ...]],
        proc_root: Path = Path("/proc"),
        software_status: Callable[[], dict | None] | None = None,
        bridge_request: Callable[[dict], Any] | None = None,
        execution_log: Path | None = None,
    ) -> None:
        self.validator = validator
        self.publish = publish
        self.markers = program_markers
        self.proc_root = proc_root
        self.software_status = software_status or (lambda: None)
        self.bridge_request = bridge_request
        self.execution_log = execution_log
        self.executions: dict[str, int] = {}
        self._log_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=SOFTWARE_WORKERS, thread_name_prefix="v1-software"
        )
        self._slots = threading.BoundedSemaphore(SOFTWARE_WORKERS)
        self._task_lock = threading.Lock()
        self._active: dict[str, str] = {}  # msg_id -> command（软件任务进行中）

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def active_software(self) -> dict[str, str]:
        with self._task_lock:
            return dict(self._active)

    # ---------- 入口 ----------
    def handle(self, payload: bytes) -> None:
        received = time.monotonic()
        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict):
                raise ValueError("command must be an object")
        except ValueError:
            logger.warning("non-JSON command dropped")
            return
        verdict = self.validator.check(raw)
        if verdict.msg_id is None:
            logger.warning("command without valid msg_id dropped")
            return
        if verdict.replay is not None:
            self._replay(verdict.msg_id, verdict.replay)
            return
        if verdict.in_progress:
            logger.info("duplicate delivery of in-flight %s ignored", verdict.msg_id)
            return
        fp = fingerprint(raw)
        handed_off = False
        try:
            if verdict.reject_code:
                if verdict.reject_message == "msg_id reused":
                    self._send_ack(verdict, False, "INVALID_PARAM", verdict.reject_message, {}, 0)
                    return
                self._finish(
                    verdict, fp, False, verdict.reject_code, verdict.reject_message, {}, received
                )
                return
            if verdict.mode == "DIAGNOSTIC":
                self._diagnostic(verdict, fp, received)
            elif verdict.mode == "SOFTWARE_TEST":
                handed_off = self._software(verdict, fp, received)
            else:
                self._finish(
                    verdict, fp, False, "NO_HARDWARE", "本车未接入硬件执行路径", {}, received
                )
        finally:
            if not handed_off:
                self.validator.dedup.release(verdict.msg_id)

    # ---------- 诊断 ----------
    def _diagnostic(self, verdict: Verdict, fp: str, received: float) -> None:
        command = verdict.wire.command
        try:
            snapshot = collect(
                AGENT_VERSION, self.markers, now=datetime.now(UTC), proc_root=self.proc_root
            )
            if command == "ping":
                data = {
                    "pong": True,
                    "boot_id": self.validator.boot_id,
                    "agent_version": AGENT_VERSION,
                    "uptime_seconds": snapshot.as_dict().get("uptime_seconds"),
                }
            elif command == "get_status":
                data = build_status(snapshot.programs, snapshot.collected_at)
                data["boot_id"] = self.validator.boot_id
                software = self.software_status()
                if software is not None:
                    data["software"] = software
            else:
                data = build_telemetry(snapshot.collected_at)
                data["host"] = snapshot.as_dict()
            self.executions[command] = self.executions.get(command, 0) + 1
            self._finish(verdict, fp, True, "OK", f"{command} executed", data, received)
        except Exception as exc:  # noqa: BLE001 - 采集失败须明确回报，不得静默
            logger.exception("diagnostic %s failed", command)
            self._finish(verdict, fp, False, "INTERNAL", f"{command} failed: {exc}", {}, received)

    # ---------- 软件测试 ----------
    def _software(self, verdict: Verdict, fp: str, received: float) -> bool:
        """回执 ACK 同步发布；ROS 执行交给有界线程池。返回 True 表示已移交（由 worker 释放）。"""
        wire = verdict.wire
        if self.bridge_request is None:
            self._finish(verdict, fp, False, "ROS_UNAVAILABLE", "ROS 软件桥未配置", {}, received)
            return False
        with self._task_lock:
            busy = wire.command in LONG_RUNNING and any(
                command in LONG_RUNNING for command in self._active.values()
            )
            if not busy and not self._slots.acquire(blocking=False):
                busy = True
            if not busy:
                self._active[verdict.msg_id] = wire.command
        if busy:
            # 任务锁：已有启动类软件任务进行中 / 并发已满 → 明确 BUSY（确定未执行）
            self._finish(verdict, fp, False, "BUSY", "已有软件任务执行中", {}, received)
            return False
        self._finish(
            verdict,
            fp,
            True,
            "OK",
            "车端已接收，已提交 ROS 软件执行（SOFTWARE_TEST，非真实底盘）",
            {"stage": "RECEIVED", "mode": "SOFTWARE_TEST", "hardware": "NO_HARDWARE"},
            received,
        )
        self.executions[wire.command] = self.executions.get(wire.command, 0) + 1
        try:
            self._pool.submit(self._run_software, verdict, received)
        except RuntimeError:  # 执行器关闭中
            self._release_software(verdict.msg_id)
            self._feedback(
                verdict,
                {"state": "FAILED", "reason_code": "AGENT_STOPPING", "message": "agent stopping"},
                received,
            )
            return False
        return True

    def _release_software(self, msg_id: str) -> None:
        with self._task_lock:
            self._active.pop(msg_id, None)
        self._slots.release()
        self.validator.dedup.release(msg_id)

    def _run_software(self, verdict: Verdict, received: float) -> None:
        wire = verdict.wire
        request = {
            "op": "command",
            # SoftwareCommand.command_id = V1 msg_id（云端业务 command_id 只在云端关联表）
            "command_id": verdict.msg_id,
            "command": wire.command,
            "params": wire.params,
            "issued_at": wire.ts.isoformat(),
            "expires_at": wire.expires_at.isoformat(),
        }
        terminal = False
        try:
            for feedback in self.bridge_request(request):
                terminal = feedback.get("state") in {"COMPLETED", "REJECTED", "FAILED", "STOPPED"}
                self._feedback(verdict, feedback, received)
                if terminal:
                    break
        except OSError as exc:
            logger.warning("ROS software bridge unavailable: %s", exc)
            self._feedback(
                verdict,
                {"state": "FAILED", "reason_code": "ROS_UNAVAILABLE", "message": str(exc)},
                received,
            )
            terminal = True
        except Exception as exc:  # noqa: BLE001 - 终态必须收敛
            logger.exception("software task %s failed", verdict.msg_id)
            self._feedback(
                verdict,
                {"state": "FAILED", "reason_code": "INTERNAL", "message": str(exc)},
                received,
            )
            terminal = True
        finally:
            if not terminal:
                self._feedback(
                    verdict,
                    {"state": "FAILED", "reason_code": "FEEDBACK_TIMEOUT", "message": "无终态反馈"},
                    received,
                )
            self._release_software(verdict.msg_id)

    def _feedback(self, verdict: Verdict, feedback: dict, received: float) -> None:
        event = {
            "type": "software_feedback",
            "msg_id": verdict.msg_id,
            "command": verdict.wire.command,
            "mode": "SOFTWARE_TEST",
            "hardware": "NO_HARDWARE",
            # 车端可信标记：仅 ROS 反馈 software_test=true（或桥/执行器自身生成）才为 true
            "software_test": feedback.get("software_test", True) is True,
            "state": feedback.get("state"),
            "reason_code": feedback.get("reason_code"),
            "message": feedback.get("message"),
            "phase": feedback.get("phase"),
            "ros_status": feedback.get("ros_status"),
            "stopped_command_id": feedback.get("stopped_command_id"),
            "ros_ts": feedback.get("ts"),
            "duration_ms": int((time.monotonic() - received) * 1000),
            "ts": now_iso(),
        }
        self.validator.dedup.record_event(verdict.msg_id, event)
        self._log({"kind": "software_feedback", **event})
        if not self.publish("event", event):
            logger.warning("software feedback publish UNCONFIRMED for %s", verdict.msg_id)

    # ---------- ACK ----------
    def _finish(self, verdict, fp, success, code, message, data, received) -> None:
        duration = int((time.monotonic() - received) * 1000)
        ack = self._ack(verdict.msg_id, success, code, message, data, duration)
        self.validator.dedup.record_ack(verdict.msg_id, fp, ack)
        self._log(
            {
                "kind": "ack",
                "msg_id": verdict.msg_id,
                "command": verdict.wire.command if verdict.wire else None,
                "mode": verdict.mode,
                "code": code,
                "duration_ms": duration,
                "boot_id": self.validator.boot_id,
                "ts": ack["ts"],
            }
        )
        if not self.publish("ack", ack):
            logger.warning(
                "ACK publish UNCONFIRMED for %s (replayed on redelivery)", verdict.msg_id
            )

    def _send_ack(self, verdict, success, code, message, data, duration) -> None:
        self.publish("ack", self._ack(verdict.msg_id, success, code, message, data, duration))

    @staticmethod
    def _ack(msg_id, success, code, message, data, duration) -> dict:
        return {
            "msg_id": msg_id,
            "success": success,
            "code": code,
            "message": message,
            "data": data,
            "duration_ms": duration,
            "ts": now_iso(),
        }

    def _replay(self, msg_id: str, entry: dict) -> None:
        logger.info("duplicate %s: replaying original result without re-execution", msg_id)
        self.publish("ack", entry["ack"])
        for event in entry.get("events", []):
            self.publish("event", event)

    def _log(self, record: dict) -> None:
        if self.execution_log is None:
            return
        with self._log_lock:
            try:
                self.execution_log.parent.mkdir(parents=True, exist_ok=True)
                with self.execution_log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                logger.warning("execution log write failed", exc_info=True)
