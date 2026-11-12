#!/usr/bin/env python3
# ruff: noqa: UP017  # ROS Noetic 运行于 python3.8，无 datetime.UTC（py311+）
"""firebot-001 进程与诊断状态采集节点（只读）。

周期性发布进程/OS 诊断到 /firebot_001/software/diagnostics：
主机名、Agent/ROS 进程存活、系统负载与内存。只读 /proc，无任何硬件访问。
"""

import os
import platform
import socket
from datetime import datetime, timezone

import rospy
from std_msgs.msg import String

NODE_NAME = "firebot_001_diagnostics"
RATE_HZ = 0.2

WATCH_PROCESSES = ("rosmaster", "command_node.py", "diagnostics_node.py")


def process_alive(needle):
    """按 cmdline 子串匹配（comm 是脚本名，节点名 ≠ 进程名）。"""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ")
            if needle.encode() in cmdline:
                return True
        except OSError:
            continue
    return False


def main():
    rospy.init_node(NODE_NAME, log_level=rospy.INFO)
    pub = rospy.Publisher("/firebot_001/software/diagnostics", String, queue_size=10)
    rate = rospy.Rate(RATE_HZ)
    rospy.loginfo("[SOFTWARE_TEST] diagnostics node ready; read-only /proc only")
    while not rospy.is_shutdown():
        alive = {name: process_alive(name) for name in WATCH_PROCESSES}
        payload = {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "pid": os.getpid(),
            "processes": alive,
            "load_1m": os.getloadavg()[0],
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        pub.publish(str(payload))
        rate.sleep()


if __name__ == "__main__":
    main()
