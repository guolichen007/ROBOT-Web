#!/usr/bin/env python3
# ruff: noqa: UP017  # ROS Noetic 运行于 python3.8，无 datetime.UTC（py311+）
"""firebot-001 软件命令节点（SOFTWARE_TEST）。

订阅 /firebot_001/software/command，驱动软件状态机：
  start        -> state=ready   （SOFTWARE_READY，无硬件执行器）
  close        -> state=idle
  patrol       -> state=patrol  （软件级巡检，产生真实日志与分步反馈）
  stop_patrol  -> state=idle
  status       -> 发布当前状态

所有反馈经 /firebot_001/software/feedback 发布，software_test 恒为 True：
收到命令必须产生真实日志与明确反馈，绝不只收消息假装成功。
本节点不连接 GPIO/CAN/Modbus/继电器/电机/底盘。
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import rospy
from firebot_001_software.msg import SoftwareCommand, SoftwareFeedback, SoftwareStatus

NODE_NAME = "firebot_001_command"
STATE_IDLE = "idle"
STATE_READY = "ready"
STATE_PATROL = "patrol"
STATE_FAULT = "fault"


class SoftwareStateMachine:
    """软件状态机：状态迁移全部真实执行并记录日志。"""

    def __init__(self, feedback_pub, status_pub):
        self.state = STATE_IDLE
        self.current_command = None
        self.feedback_pub = feedback_pub
        self.status_pub = status_pub
        self._patrol_thread = None
        self._patrol_stop = threading.Event()
        self.lock = threading.Lock()

    def _feedback(self, command_id, action, status, detail):
        msg = SoftwareFeedback()
        msg.command_id = command_id
        msg.action = action
        msg.status = status
        msg.detail = detail
        msg.software_test = True
        msg.ts = datetime.now(timezone.utc).isoformat()
        self.feedback_pub.publish(msg)
        rospy.loginfo(
            f"[SOFTWARE_TEST][{action}] command_id={command_id} {status} -> {self.state} ({detail})"
        )

    def _publish_status(self):
        msg = SoftwareStatus()
        msg.state = self.state
        with open("/proc/uptime") as fh:
            msg.uptime_seconds = float(fh.read().split()[0])
        msg.pid = os.getpid()
        msg.load_1m = os.getloadavg()[0]
        with open("/proc/meminfo") as fh:
            avail = next(
                int(line.split()[1]) * 1024 for line in fh if line.startswith("MemAvailable:")
            )
        msg.mem_available_bytes = float(avail)
        msg.diagnostics = (
            f"command={self.current_command or 'none'}; "
            "hardware_executors=NONE (software test only)"
        )
        msg.ts = datetime.now(timezone.utc).isoformat()
        self.status_pub.publish(msg)

    def _patrol_loop(self, command_id, rounds):
        for index in range(rounds):
            if self._patrol_stop.is_set():
                with self.lock:
                    self.state = STATE_IDLE
                    self.current_command = None
                self._feedback(
                    command_id, "patrol", "STOPPED", f"software patrol stopped at round {index + 1}"
                )
                self._publish_status()
                return
            time.sleep(1.0)
            self._feedback(
                command_id,
                "patrol",
                "RUNNING",
                f"software patrol round {index + 1}/{rounds} (no hardware)",
            )
            rospy.loginfo(
                f"[SOFTWARE_TEST] patrol round {index + 1}/{rounds} tick, command_id={command_id}"
            )
        with self.lock:
            self.state = STATE_IDLE
            self.current_command = None
        self._feedback(
            command_id, "patrol", "COMPLETED", f"software patrol finished {rounds} rounds"
        )
        self._publish_status()

    def dispatch(self, cmd):
        command_id = cmd.command_id
        action = cmd.action
        params = {}
        try:
            if cmd.params:
                params = json.loads(cmd.params)
        except ValueError as exc:
            self._feedback(command_id, action, "REJECTED", f"invalid params JSON: {exc}")
            return
        with self.lock:
            current = self.state
            self.current_command = command_id
            if action == "start":
                if current == STATE_READY:
                    self._feedback(command_id, action, "REJECTED", "already ready")
                    return
                self.state = STATE_READY
                self._feedback(command_id, action, "ACCEPTED", "software ready (SOFTWARE_TEST)")
                self._feedback(command_id, action, "COMPLETED", "state=ready")
            elif action == "close":
                self.state = STATE_IDLE
                self._feedback(command_id, action, "ACCEPTED", "closing software session")
                self._feedback(command_id, action, "COMPLETED", "state=idle")
            elif action == "patrol":
                if current != STATE_READY:
                    self._feedback(command_id, action, "REJECTED", f"not ready (state={current})")
                    return
                self.state = STATE_PATROL
                self._feedback(command_id, action, "ACCEPTED", "software patrol started")
                rounds = int(params.get("rounds", 3))
                self._patrol_thread = threading.Thread(
                    target=self._patrol_loop, args=(command_id, rounds), daemon=True
                )
                self._patrol_thread.start()
            elif action == "stop_patrol":
                if current != STATE_PATROL:
                    self._feedback(
                        command_id,
                        action,
                        "REJECTED",
                        f"not patrolling (state={current})",
                    )
                    return
                self._feedback(command_id, action, "ACCEPTED", "stop requested")
                self._patrol_stop.set()
            elif action == "status":
                self._feedback(command_id, action, "COMPLETED", f"state={current}")
            else:
                self._feedback(command_id, action, "REJECTED", f"unknown action {action}")
                return
            self._publish_status()


def main():
    rospy.init_node(NODE_NAME, log_level=rospy.INFO)
    feedback_pub = rospy.Publisher(
        "/firebot_001/software/feedback", SoftwareFeedback, queue_size=10
    )
    status_pub = rospy.Publisher(
        "/firebot_001/software/status", SoftwareStatus, queue_size=10, latch=True
    )
    machine = SoftwareStateMachine(feedback_pub, status_pub)
    machine._publish_status()
    rospy.loginfo(
        "[SOFTWARE_TEST] command node ready; namespace /firebot_001/software; "
        "hardware executors=NONE"
    )
    rospy.Subscriber(
        "/firebot_001/software/command", SoftwareCommand, machine.dispatch, queue_size=10
    )
    rospy.spin()


if __name__ == "__main__":
    main()
