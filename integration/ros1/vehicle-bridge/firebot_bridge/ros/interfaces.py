"""ROS Placeholder Contract 冻结定义（v1.0）。

车端 ROS 控制程序只接这一侧，不修改 MQTT 层。
详见 FIREBOT_BRIDGE_CONTRACT_1.3.md 第 3 章。
"""
from __future__ import annotations

# ---- topic 前缀 ----
ROS_NS = "/firebot_bridge"

# ---- 下行（Bridge → ROS）----
TOPIC_ROS_COMMAND = f"{ROS_NS}/command"

# ---- 上行（ROS → Bridge）----
TOPIC_ROS_FEEDBACK = f"{ROS_NS}/command_feedback"
TOPIC_ROS_BATTERY = f"{ROS_NS}/battery"
TOPIC_ROS_SMOKE = f"{ROS_NS}/smoke"
TOPIC_ROS_STATUS = f"{ROS_NS}/status"
TOPIC_ROS_LOCATION = f"{ROS_NS}/location"
TOPIC_ROS_ALARM = f"{ROS_NS}/alarm"

# ---- MQTT cmd → ROS command 映射（冻结）----
MQTT_CMD_TO_ROS = {
    "patrol": "PATROL_START",
    "stop_motion": "STOP_MOTION",
    "emergency_stop": "EMERGENCY_STOP",
    "reset_estop": "RESET_ESTOP",
    "return_dock": "RETURN_DOCK",
    "extinguish": "EXTINGUISH_START",
    "cancel_task": "CANCEL_TASK",
    "manual_control": "MANUAL_CONTROL",
}

# ---- ROS feedback state 枚举 ----
ROS_FEEDBACK_STATES = {"ACCEPTED", "EXECUTING", "COMPLETED", "REJECTED", "FAILED", "CANCELLED"}

INTERFACE_VERSION = "1.0"


def build_ros_command(mqtt_command: dict) -> dict:
    """把 MQTT command 转成 /firebot_bridge/command 的 JSON payload。"""
    cmd = mqtt_command.get("cmd")
    return {
        "interface_version": INTERFACE_VERSION,
        "command_id": mqtt_command.get("command_id"),
        "task_id": mqtt_command.get("task_id"),
        "command": MQTT_CMD_TO_ROS.get(cmd, cmd.upper()),
        "params": mqtt_command.get("params", {}) or {},
        "received_at": mqtt_command.get("issued_at"),
        "expires_at": mqtt_command.get("expires_at"),
    }
