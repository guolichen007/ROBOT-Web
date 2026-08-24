"""ROS 命令反馈：订阅 /firebot_bridge/command_feedback → 回调。

回调由 main 注入，负责把 feedback 转成 MQTT command_ack / task_status。
"""
from __future__ import annotations

import json
import logging
from typing import Callable

LOG = logging.getLogger("firebot-bridge")

FeedbackHandler = Callable[[dict], None]


class FeedbackListener:
    def __init__(self, rospy, handler: FeedbackHandler) -> None:
        self.rospy = rospy
        self.handler = handler

    def start(self) -> None:
        if self.rospy is None:
            LOG.info("rospy 不可用：command_feedback 监听不启动（命令将回 rejected）")
            return
        from . import interfaces as I
        from .ros_types import StdString

        if StdString is None:
            LOG.warning("std_msgs/String 不可用：feedback 监听不启动")
            return
        try:
            self.rospy.Subscriber(I.TOPIC_ROS_FEEDBACK, StdString, self._on_feedback)
            LOG.info("ROS feedback 订阅: %s", I.TOPIC_ROS_FEEDBACK)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ROS feedback 订阅失败: %s", exc)

    def _on_feedback(self, msg) -> None:
        try:
            feedback = json.loads(msg.data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            LOG.warning("ROS feedback 非 JSON: %s", exc)
            return
        if not isinstance(feedback, dict):
            return
        try:
            self.handler(feedback)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("feedback handler 异常: %s", exc)
