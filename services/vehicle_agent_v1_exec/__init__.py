"""Firebot G2：Vehicle-Agent-V1 双向执行器（显式授权模式）。

与 ``vehicle_agent_v1_readonly`` 并存但互斥：readonly 仍是默认模式（零订阅）；本执行器
只有在设备存在有效执行授权文件（execution_grant.json）时才启动，并且只订阅本车
``v1/robot/{vehicle_id}/cmd``。执行能力严格受授权档限制：

- DIAGNOSTIC：ping / get_status / get_telemetry —— 真实本机采集；
- SOFTWARE_TEST：start / close / patrol / stop_patrol —— 仅经本地 ROS 软件桥驱动 ROS 软件
  状态机（/<ns>/command → /<ns>/feedback），无电机/CAN/继电器/GPIO；
- REAL（真实整车动作）：本执行器没有任何实现路径，一律回 NO_HARDWARE 拒绝。
"""

AGENT_NAME = "vehicle-agent-v1-exec"
AGENT_VERSION = "0.2.0"
AGENT_ROLE = "diagnostic+software-test"
