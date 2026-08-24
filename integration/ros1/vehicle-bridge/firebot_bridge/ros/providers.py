"""ROS 数据提供：订阅 /firebot_bridge/{battery,smoke,status,location} → 缓存到 state。

battery 真实来源：/robot_status.battery_percentage（见 main.py 的 handle_robot_status）。
smoke 真实来源：由车端提供方发布到 /firebot_bridge/smoke（Modbus/standalone 脚本）。
没有真实 smoke 源时，providers 不产生 smoke 数据，Bridge 不发布 sensor（不伪造 0）。
"""
from __future__ import annotations

import json
import logging

LOG = logging.getLogger("firebot-bridge")


class RosProviders:
    """订阅 ROS 数据 topic，写入 BridgeState。rospy 不可用时降级（无 provider 数据）。"""

    def __init__(self, rospy, state: "BridgeState") -> None:
        self.rospy = rospy
        self.state = state
        self._subs = []

    def start(self) -> None:
        if self.rospy is None:
            LOG.info("rospy 不可用：ROS provider 不启动（无 battery/smoke/location 数据源）")
            return
        from . import interfaces as I
        from .ros_types import StdFloat32, StdString

        if StdFloat32 is None or StdString is None:
            LOG.warning("ROS message 类不可用：provider 不启动")
            return
        subs = [
            # battery / smoke：std_msgs/Float32
            (I.TOPIC_ROS_BATTERY, StdFloat32, self._on_battery),
            (I.TOPIC_ROS_SMOKE, StdFloat32, self._on_smoke),
            # status / location：std_msgs/String(JSON)
            (I.TOPIC_ROS_STATUS, StdString, self._on_status),
            (I.TOPIC_ROS_LOCATION, StdString, self._on_location),
        ]
        for topic, msg_type, cb in subs:
            try:
                sub = self.rospy.Subscriber(topic, msg_type, cb)
                self._subs.append(sub)
                LOG.info("ROS provider 订阅: %s", topic)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("ROS provider 订阅失败 %s: %s", topic, exc)

    # ---- callbacks ----
    def _on_battery(self, msg) -> None:
        try:
            self.state.set_battery(float(msg.data))
        except Exception:  # noqa: BLE001
            pass

    def _on_smoke(self, msg) -> None:
        try:
            self.state.set_smoke(float(msg.data))
        except Exception:  # noqa: BLE001
            pass

    def _on_status(self, msg) -> None:
        try:
            data = json.loads(msg.data)
            with self.state._lock:
                if "mode" in data:
                    self.state.mode = str(data["mode"]).upper()
                if "estop_active" in data:
                    self.state.estop_active = bool(data["estop_active"])
                if "active_task_id" in data:
                    self.state.active_task_id = data.get("active_task_id")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ROS status 解析失败: %s", exc)

    def _on_location(self, msg) -> None:
        try:
            self.state.set_location(json.loads(msg.data))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ROS location 解析失败: %s", exc)
