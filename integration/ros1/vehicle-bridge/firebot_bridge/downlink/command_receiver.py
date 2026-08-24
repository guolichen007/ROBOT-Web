"""命令处理编排：MQTT command → 校验/去重 → ROS placeholder → feedback → MQTT ACK/task_status。

Bridge 不做任何执行。只有 ROS /firebot_bridge/command_feedback 明确回 ACCEPTED
后 Bridge 才回 MQTT accepted；无 feedback（ROS 未接）→ rejected + BRIDGE_ADAPTER_NOT_CONNECTED。
"""
from __future__ import annotations

import json
import logging
import threading

from ..protocol import Protocol, TASK_CMDS
from ..state import BridgeState
from .command_dedup import CommandDedup
from .command_validator import validate_received_command

LOG = logging.getLogger("firebot-bridge")


class _Pending:
    def __init__(self, command: dict, timer: threading.Timer) -> None:
        self.command = command
        self.timer = timer
        self.acked = False

    def cancel_timer(self) -> None:
        if self.timer:
            self.timer.cancel()


class CommandProcessor:
    def __init__(
        self,
        config,
        state: BridgeState,
        identity,
        proto: Protocol,
        mqtt_client,
        placeholder,
    ) -> None:
        self.config = config
        self.state = state
        self.identity = identity
        self.proto = proto
        self.client = mqtt_client
        self.placeholder = placeholder
        self.dedup = CommandDedup(state)
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    # ---- 入口（MQTT on_message 调用）----
    def on_command(self, command: dict) -> None:
        command_id = command.get("command_id")
        ok, reason = validate_received_command(
            command, self.identity.boot_id, self.config
        )
        if not ok:
            self._ack(command, "rejected", reason)
            return
        existing = self.dedup.lookup(command_id)
        if existing:
            # QoS1 重复投递：不重复发布 ROS command；重放之前 ACK / 最新 task_status
            if existing.get("ack"):
                self._publish("command_ack", existing["ack"])
            if existing.get("task_status"):
                self._publish("task_status", existing["task_status"])
            LOG.info("重复命令（幂等重放）: %s", command_id)
            return
        self.dedup.register(command_id, command)

        cmd = command.get("cmd")
        # 任务锁：任务类命令需独占（task_id 已由 validator 保证非空）
        if cmd in TASK_CMDS and not self.state.acquire_task(command.get("task_id")):
            self._ack(command, "rejected", "ACTIVE_TASK_CONFLICT")
            return

        # 转发到 ROS placeholder（不做任何执行）
        delivered = self.placeholder.publish_command(command)
        if not delivered:
            self._release_if_needed(cmd)
            self._ack(command, "rejected", "BRIDGE_ADAPTER_NOT_CONNECTED")
            return

        # 注册 pending + 反馈超时
        timer = threading.Timer(
            self.config.feedback_timeout_seconds, self._on_timeout, args=(command_id,)
        )
        timer.daemon = True
        with self._lock:
            self._pending[command_id] = _Pending(command=command, timer=timer)
        timer.start()

        # stub 联调：模拟 feedback（默认回 rejected，证明消息闭环，不假装执行）
        if self.config.bridge_stub_mode and self.config.stub_simulate_feedback:
            self._simulate_stub_feedback(command)

    # ---- ROS feedback 回调 ----
    def on_feedback(self, feedback: dict) -> None:
        command_id = feedback.get("command_id")
        with self._lock:
            # ACCEPTED 后必须保留 pending 以关联后续 EXECUTING/COMPLETED/FAILED
            pending = self._pending.get(command_id)
        if not pending:
            LOG.info("feedback for unknown/expired command_id=%s", command_id)
            return
        command = pending.command
        ros_state = feedback.get("state")
        task_id = feedback.get("task_id") or command.get("task_id")

        if ros_state == "ACCEPTED":
            pending.cancel_timer()
            pending.acked = True
            self._ack(command, "accepted", None)
        elif ros_state == "REJECTED":
            self._release_if_needed(command.get("cmd"))
            self._ack(
                command, "rejected", feedback.get("reason_code") or "COMMAND_REJECTED"
            )
            self._finalize(command_id, pending)
        elif ros_state == "EXECUTING":
            self._report_task(
                command_id, task_id, "executing",
                feedback.get("phase") or "NAVIGATING", feedback.get("progress") or 0,
                feedback,
            )
        elif ros_state in ("COMPLETED", "FAILED"):
            self._release_if_needed(command.get("cmd"))
            status = "completed" if ros_state == "COMPLETED" else "failed"
            phase = feedback.get("phase") or ("COMPLETED" if ros_state == "COMPLETED" else "FAILED")
            progress = feedback.get("progress") or (100 if ros_state == "COMPLETED" else 0)
            self._report_task(command_id, task_id, status, phase, progress, feedback)
            self._finalize(command_id, pending)

    def _finalize(self, command_id: str, pending: _Pending) -> None:
        pending.cancel_timer()
        with self._lock:
            self._pending.pop(command_id, None)
        self.dedup.mark_terminal(command_id)

    # ---- 反馈超时（ROS 未接）----
    def _on_timeout(self, command_id: str) -> None:
        with self._lock:
            pending = self._pending.get(command_id)
        if not pending or pending.acked:
            return
        self._release_if_needed(pending.command.get("cmd"))
        self._ack(pending.command, "rejected", "BRIDGE_ADAPTER_NOT_CONNECTED")
        self._finalize(command_id, pending)
        LOG.warning("命令 %s 反馈超时（ROS adapter 未接）", command_id)

    # ---- stub 模拟 ----
    def _simulate_stub_feedback(self, command: dict) -> None:
        """联调：模拟 ROS 未接入的 feedback，证明消息闭环。"""
        simulate = self.config.stub_feedback_simulation
        if simulate == "accepted":
            self.on_feedback(
                {"command_id": command["command_id"], "task_id": command.get("task_id"),
                 "state": "ACCEPTED"}
            )
        else:  # default: rejected / BRIDGE_ADAPTER_NOT_CONNECTED
            self.on_feedback(
                {"command_id": command["command_id"], "task_id": command.get("task_id"),
                 "state": "REJECTED", "reason_code": "BRIDGE_ADAPTER_NOT_CONNECTED"}
            )

    # ---- MQTT 回执 ----
    def _ack(self, command: dict, status: str, reason_code: str | None) -> None:
        msg = self.proto.base("command_ack")
        msg.update(
            {
                "command_id": command.get("command_id"),
                "task_id": command.get("task_id"),
                "status": status,
                "reason_code": reason_code,
                "reason": None,
            }
        )
        self.dedup.remember_ack(command.get("command_id"), msg)
        self._publish("command_ack", msg)
        LOG.info("↗ command_ack: cmd=%s status=%s reason=%s",
                 command.get("cmd"), status, reason_code)

    def _report_task(
        self,
        command_id: str,
        task_id,
        status: str,
        phase: str,
        progress: float,
        feedback: dict,
    ) -> None:
        msg = self.proto.base("task_status")
        msg.update(
            {
                "task_id": task_id,
                "status": status,
                "phase": phase,
                "progress": progress,
                "failure_code": feedback.get("reason_code"),
                "failure_message": feedback.get("message"),
            }
        )
        self.dedup.remember_task_status(command_id, msg)
        self._publish("task_status", msg)
        LOG.info("↗ task_status: task=%s status=%s phase=%s progress=%s",
                 str(task_id)[:8], status, phase, progress)

    def _publish(self, name: str, payload: dict) -> None:
        self.client.publish(
            self.proto.topic(name), json.dumps(payload, ensure_ascii=False), qos=1
        )

    def _release_if_needed(self, cmd: str) -> None:
        if cmd in TASK_CMDS:
            self.state.release_task()
