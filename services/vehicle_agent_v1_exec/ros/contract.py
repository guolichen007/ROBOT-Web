"""001 ROS 软件接口适配合同（G2-R1）。

正式接口：integration/ros1/firebot-001-software/G2-INTERFACE.md（随本分支版本化）。
话题与类型（默认 namespace /firebot_001/software，按车辆配置，不写死车辆）：
  <ns>/command   firebot_001_software/SoftwareCommand  {command_id, action, params(JSON 字符串)}
  <ns>/feedback  firebot_001_software/SoftwareFeedback {command_id, action, status, detail,
                                                       software_test, ts}
  <ns>/status    firebot_001_software/SoftwareStatus   {state, uptime_seconds, pid, load_1m,
                                                       mem_available_bytes, diagnostics, ts}
SoftwareCommand.command_id = V1 msg_id（UUID）；云端业务 command_id 只在云端关联表中，二者不混用。
本模块只用标准库（车端 ROS Noetic 系统 Python 3.8 可用）。
"""

from __future__ import annotations

import json

INTERFACE = "firebot_001_software/G2-INTERFACE"
DEFAULT_NAMESPACE = "/firebot_001/software"
DEFAULT_MESSAGE_PACKAGE = "firebot_001_software"
SOFTWARE_COMMANDS = ("start", "close", "patrol", "stop_patrol")
# 车端 status → V1 软件反馈状态
STATUS_MAP = {
    "ACCEPTED": "ACCEPTED",
    "RUNNING": "EXECUTING",
    "COMPLETED": "COMPLETED",
    "REJECTED": "REJECTED",
    "STOPPED": "STOPPED",
}
TERMINAL_STATES = ("COMPLETED", "REJECTED", "FAILED", "STOPPED")
# detail 前缀 → 规范 reason_code（原文 detail 始终保留在 message）
REJECT_REASONS = (
    ("not ready", "NOT_READY"),
    ("already ready", "ALREADY_RUNNING"),
    ("not patrolling", "ALREADY_STOPPED"),
    ("unknown action", "UNKNOWN_COMMAND"),
    ("invalid params", "INVALID_PARAM"),
)


def topics(namespace: str) -> dict:
    ns = "/" + namespace.strip("/")
    return {"command": ns + "/command", "feedback": ns + "/feedback", "status": ns + "/status"}


def build_command(command_type, msg_id: str, action: str, params: dict):
    """V1 命令 → SoftwareCommand（params 序列化为 JSON 字符串）。"""
    message = command_type()
    message.command_id = msg_id
    message.action = action
    message.params = json.dumps(params or {}, sort_keys=True)
    return message


def feedback_to_dict(message) -> dict:
    """SoftwareFeedback → 可序列化反馈；software_test 必须为 true，否则判定为不可信失败。"""
    raw_status = str(message.status)
    state = STATUS_MAP.get(raw_status, "FAILED")
    detail = str(message.detail or "")
    reason = None
    if raw_status not in STATUS_MAP:
        reason = "UNKNOWN_FEEDBACK_STATUS"
    elif state == "REJECTED":
        lowered = detail.lower()
        reason = next((code for prefix, code in REJECT_REASONS if lowered.startswith(prefix)), None)
        reason = reason or "ROS_REJECTED"
    if message.software_test is not True:
        state, reason = "FAILED", "SOFTWARE_TEST_FLAG_MISSING"
    return {
        "command_id": str(message.command_id),
        "action": str(message.action),
        "state": state,
        "ros_status": raw_status,
        "reason_code": reason,
        "message": detail,
        "software_test": bool(message.software_test),
        "ts": str(message.ts),
    }


def status_to_dict(message) -> dict:
    """SoftwareStatus 自定义消息 → 可序列化软件状态（不对 ROS 对象做 json.loads）。"""
    state = str(message.state)
    return {
        "state": state,
        "software_running": state not in ("idle", ""),
        "patrol": state == "patrol",
        "uptime_seconds": float(message.uptime_seconds),
        "pid": int(message.pid),
        "load_1m": float(message.load_1m),
        "mem_available_bytes": float(message.mem_available_bytes),
        "diagnostics": str(message.diagnostics),
        "ts": str(message.ts),
        "hardware": "NO_HARDWARE",
        "mode": "SOFTWARE_TEST",
    }
