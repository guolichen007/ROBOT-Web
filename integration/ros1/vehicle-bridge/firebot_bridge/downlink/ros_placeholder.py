"""ROS placeholder 转发：把 MQTT 命令发布到 /firebot_bridge/command。

Bridge 不做任何执行，只负责把命令可靠交给 ROS 层。
"""
from __future__ import annotations

import json
import logging

LOG = logging.getLogger("firebot-bridge")


class RosPlaceholder:
    def __init__(self, rospy) -> None:
        self.rospy = rospy
        self._publisher = None

    def publish_command(self, mqtt_command: dict) -> bool:
        """发布到 /firebot_bridge/command。返回是否成功投递到 ROS 层。"""
        from ..ros.interfaces import TOPIC_ROS_COMMAND, build_ros_command
        from ..ros.ros_types import StdString

        if self.rospy is None or StdString is None:
            LOG.info("rospy 不可用：命令无法转发到 ROS placeholder")
            return False
        try:
            payload = json.dumps(build_ros_command(mqtt_command), ensure_ascii=False)
            if self._publisher is None:
                self._publisher = self.rospy.Publisher(
                    TOPIC_ROS_COMMAND, StdString, queue_size=10
                )
            self._publisher.publish(StdString(data=payload))
            LOG.info("已转发到 %s: cmd=%s command_id=%s",
                     TOPIC_ROS_COMMAND, mqtt_command.get("cmd"), mqtt_command.get("command_id"))
            return True
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ROS placeholder 发布失败: %s", exc)
            return False
