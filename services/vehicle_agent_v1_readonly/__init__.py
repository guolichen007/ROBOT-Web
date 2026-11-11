"""Firebot R001 G0-A2：真实只读 Vehicle-Agent-V1。

运行在真实工控机（canonical ID ``firebot-vehicle-01``，云端显示名 ``firebot-001``）上的
独立只读 Agent：只采集 OS 与进程信息并按 ``services/vehicle_agent_v1.py`` 合同上行
availability/status/telemetry。本阶段不订阅任何命令、不调用任何控制输出接口。
"""

AGENT_NAME = "vehicle-agent-v1-readonly"
AGENT_VERSION = "0.1.0"
AGENT_ROLE = "readonly-telemetry"  # 只读遥测角色；无控制能力
