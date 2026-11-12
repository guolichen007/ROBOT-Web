"""参考 ROS 软件测试状态机节点（SOFTWARE_TEST / NO_HARDWARE）。

用于隔离联调与回归测试；001 现场已有同命名空间的软件节点时以现场节点为准，本节点
定义的消息合同（contract.py）即双方对齐基准。真实接收 ROS 消息、改变本地状态、发布
反馈与状态；不导入任何硬件驱动、不发布 /cmd_vel、不写 CAN/GPIO/继电器。

状态机：STOPPED --start--> IDLE --patrol--> PATROLLING --stop_patrol--> IDLE --close--> STOPPED
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone

try:
    from .contract import SOFTWARE_COMMANDS, topics
except ImportError:
    from contract import SOFTWARE_COMMANDS, topics  # type: ignore


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SoftwareStateMachine:
    def __init__(self) -> None:
        self.state = "STOPPED"
        self.lock = threading.Lock()
        self.transitions = 0

    def apply(self, command: str):
        """返回 (feedback_state, reason_code, message, phase)。"""
        with self.lock:
            current = self.state
            if command == "start":
                if current != "STOPPED":
                    return "COMPLETED", "ALREADY_RUNNING", "software already running", current
                self.state = "IDLE"
            elif command == "close":
                if current == "STOPPED":
                    return "COMPLETED", "ALREADY_CLOSED", "software already stopped", current
                self.state = "STOPPED"
            elif command == "patrol":
                if current == "STOPPED":
                    return "REJECTED", "NOT_READY", "software not started", current
                if current == "PATROLLING":
                    return "COMPLETED", "ALREADY_RUNNING", "patrol already running", current
                self.state = "PATROLLING"
            elif command == "stop_patrol":
                if current != "PATROLLING":
                    return "COMPLETED", "ALREADY_STOPPED", "patrol not running", current
                self.state = "IDLE"
            else:
                return "REJECTED", "UNKNOWN_COMMAND", command, current
            self.transitions += 1
            return "COMPLETED", None, f"{current} -> {self.state}", self.state

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "software_running": self.state != "STOPPED",
                "patrol": self.state == "PATROLLING",
                "hardware": "NO_HARDWARE",
                "mode": "SOFTWARE_TEST",
                "ts": now_iso(),
            }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="firebot-software-test-node")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--step-seconds", type=float, default=0.3, help="模拟软件处理耗时")
    args = parser.parse_args(argv)
    import rospy
    from std_msgs.msg import String

    names = topics(args.namespace)
    rospy.init_node("firebot_software_test_node", anonymous=False, disable_signals=True)
    feedback_pub = rospy.Publisher(names["feedback"], String, queue_size=50)
    status_pub = rospy.Publisher(names["status"], String, queue_size=5, latch=True)
    machine = SoftwareStateMachine()

    def feedback(request: dict, state: str, reason=None, message=None, phase=None) -> None:
        feedback_pub.publish(
            String(
                data=json.dumps(
                    {
                        "request_id": request.get("request_id"),
                        "command_id": request.get("command_id"),
                        "state": state,
                        "reason_code": reason,
                        "message": message,
                        "phase": phase,
                        "ts": now_iso(),
                    }
                )
            )
        )

    def handle(request: dict) -> None:
        command = request.get("command")
        if request.get("mode") != "SOFTWARE_TEST" or command not in SOFTWARE_COMMANDS:
            feedback(request, "REJECTED", "MODE_MISMATCH", "software node only runs SOFTWARE_TEST")
            return
        feedback(request, "ACCEPTED", phase=machine.snapshot()["state"])
        time.sleep(args.step_seconds)
        feedback(request, "EXECUTING", phase=machine.snapshot()["state"])
        time.sleep(args.step_seconds)
        state, reason, message, phase = machine.apply(command)
        status_pub.publish(String(data=json.dumps(machine.snapshot())))
        feedback(request, state, reason, message, phase)

    def on_command(message) -> None:
        try:
            request = json.loads(message.data)
        except ValueError:
            return
        threading.Thread(target=handle, args=(request,), daemon=True).start()

    rospy.Subscriber(names["command"], String, on_command, queue_size=20)
    status_pub.publish(String(data=json.dumps(machine.snapshot())))
    while not rospy.is_shutdown():
        status_pub.publish(String(data=json.dumps(machine.snapshot())))
        time.sleep(2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
