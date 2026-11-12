"""ROS 软件测试合同（std_msgs/String JSON；与 FIREBOT_BRIDGE_CONTRACT §3.1/3.2 同构）。

命名空间按车辆配置（默认 /firebot_001/software），不写死车辆：
  <ns>/command   下行  {"interface_version","request_id","command_id","command","params",
                        "mode":"SOFTWARE_TEST","issued_at","expires_at"}
  <ns>/feedback  上行  {"request_id","command_id","state","reason_code","message","phase","ts"}
  <ns>/status    上行  {"state","software_running","patrol","hardware":"NO_HARDWARE","ts"}
state ∈ ACCEPTED|EXECUTING|COMPLETED|REJECTED|FAILED。
本模块只用标准库（车端系统 Python 3.8 可用）。
"""

from __future__ import annotations

INTERFACE_VERSION = "g2-software-1.0"
DEFAULT_NAMESPACE = "/firebot_001/software"
FEEDBACK_STATES = ("ACCEPTED", "EXECUTING", "COMPLETED", "REJECTED", "FAILED")
TERMINAL_STATES = ("COMPLETED", "REJECTED", "FAILED")
SOFTWARE_COMMANDS = ("start", "close", "patrol", "stop_patrol")


def topics(namespace: str) -> dict:
    ns = "/" + namespace.strip("/")
    return {"command": ns + "/command", "feedback": ns + "/feedback", "status": ns + "/status"}
