#!/usr/bin/env python3
"""Firebot 车端 ROS Bridge 主入口。

职责（通信层）：
  - MQTT/TLS 常驻连接 + LWT + reconnect
  - 上行：availability/heartbeat/capabilities/status(battery)/sensor(smoke)/location
  - 下行：订阅 robot/{id}/command → 校验/去重 → 转发 ROS placeholder → 据 feedback 回 ACK/task_status
  - 不做任何车辆运动/巡航/急停/灭火/回充/手动控制执行

边界（冻结，见 FIREBOT_BRIDGE_CONTRACT_1.3.md）：
  - 生产 BRIDGE_STUB_MODE=false：命令只转发，无 ROS feedback 时回 rejected/BRIDGE_ADAPTER_NOT_CONNECTED
  - 联调 BRIDGE_STUB_MODE=true：可临时声明测试命令、模拟 feedback
"""
from __future__ import annotations

import logging
import threading
import time

# 尝试导入 rospy（ROS Noetic）；不可用时降级 MQTT-only（不订阅 ROS，命令无法转发）
try:
    import rospy  # type: ignore
except Exception:  # noqa: BLE001
    rospy = None

from .config import get_config
from .identity import Identity
from .protocol import Protocol
from .state import BridgeState
from .mqtt_client import MqttClient
from .downlink.command_receiver import CommandProcessor
from .downlink.ros_placeholder import RosPlaceholder
from .ros.feedback import FeedbackListener
from .ros.providers import RosProviders
from .ros.ros_types import IgkRobotStatus, Odometry, PoseWithCovarianceStamped
from .uplink import availability as avail_uplink
from .uplink import heartbeat as hb_uplink
from .uplink import location as loc_uplink
from .uplink import sensor as sensor_uplink
from .uplink import status as status_uplink

LOG = logging.getLogger("firebot-bridge")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)


# ---------------- 周期上报 ----------------
def heartbeat_loop(proto, mqtt, stop: threading.Event) -> None:
    uptime = 0.0
    period = 1.0 / get_config().heartbeat_hz
    while not stop.is_set():
        uptime += period
        mqtt.publish(proto.topic("heartbeat"), hb_uplink.make_heartbeat(proto, uptime), qos=0)
        stop.wait(period)


def telemetry_loop(proto, mqtt, state, stop: threading.Event) -> None:
    config = get_config()
    period = 1.0 / config.status_hz
    while not stop.is_set():
        status_msg = status_uplink.make_status(proto, state)
        if status_msg is not None:
            # status 为业务遥测，契约 QoS1；heartbeat/sensor/location 为 QoS0。
            mqtt.publish(proto.topic("status"), status_msg, qos=1)
        sensor_msg = sensor_uplink.make_sensor(proto, state)
        if sensor_msg is not None:
            mqtt.publish(proto.topic("sensor"), sensor_msg, qos=0)
        stop.wait(period)


def location_loop(proto, mqtt, state, stop: threading.Event) -> None:
    config = get_config()
    min_interval = 1.0 / max(config.location_max_hz, 0.5)
    last_sent = 0.0
    while not stop.is_set():
        now = time.monotonic()
        if now - last_sent >= min_interval:
            loc_msg = loc_uplink.make_location(proto, state, config)
            if loc_msg is not None:
                mqtt.publish(proto.topic("location"), loc_msg, qos=0)
                last_sent = now
        stop.wait(0.1)


# ---------------- ROS 订阅（只读 → 缓存） ----------------
def setup_ros_subscribers(state: BridgeState) -> None:
    """订阅 /odom、/amcl_pose、/robot_status，缓存数据。必须在 rospy.init_node 之后调用。"""
    if rospy is None:
        return
    _speed = {"linear": 0.0, "angular": 0.0}

    def handle_odom(msg) -> None:
        try:
            _speed["linear"] = msg.twist.twist.linear.x
            _speed["angular"] = msg.twist.twist.angular.z
        except Exception:  # noqa: BLE001
            pass

    def handle_amcl(msg) -> None:
        """/amcl_pose（map 系）→ location。以 amcl 为准（odom 是 LOCAL，不冒充 map）。"""
        try:
            p = msg.pose.pose
            q = p.orientation
            import math

            yaw = math.atan2(
                2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
            )
            state.set_location(
                {
                    "position": {"x": p.position.x, "y": p.position.y, "theta": yaw},
                    "linear": _speed["linear"],
                    "angular": _speed["angular"],
                    "localization_status": "OK",
                }
            )
        except Exception:  # noqa: BLE001
            pass

    def handle_robot_status(msg) -> None:
        """/robot_status.battery_percentage → battery（本轮电量真实源）。"""
        try:
            battery = getattr(msg, "battery_percentage", None)
            if battery is not None:
                state.set_battery(float(battery))
        except Exception:  # noqa: BLE001
            pass

    try:
        # 使用真实 import 的 message class，不用字符串类名。
        if Odometry is not None:
            rospy.Subscriber("/odom", Odometry, handle_odom)
        if PoseWithCovarianceStamped is not None:
            rospy.Subscriber("/amcl_pose", PoseWithCovarianceStamped, handle_amcl)
        if IgkRobotStatus is not None:
            rospy.Subscriber("/robot_status", IgkRobotStatus, handle_robot_status)
        LOG.info("ROS 订阅就绪：/odom /amcl_pose /robot_status")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("ROS 订阅失败：%s", exc)


# ---------------- 主流程 ----------------
def main() -> int:
    config = get_config()
    identity = Identity(config.vehicle_id)
    proto = Protocol(config.vehicle_id, identity.boot_id)
    state = BridgeState()

    LOG.info("bridge 启动: vehicle=%s boot=%s stub=%s",
             config.vehicle_id, identity.boot_id[:8], config.bridge_stub_mode)

    # rospy.init_node 必须先于任何 Publisher/Subscriber 创建。
    ros_ok = rospy is not None
    if ros_ok:
        rospy.init_node("firebot_bridge", anonymous=True)

    # ROS 占位
    placeholder = RosPlaceholder(rospy)
    processor = CommandProcessor(config, state, identity, proto, mqtt_client=None, placeholder=placeholder)

    # ROS feedback + providers + 数据订阅（init_node 之后）
    feedback = FeedbackListener(rospy, processor.on_feedback)
    feedback.start()
    providers = RosProviders(rospy, state)
    providers.start()
    setup_ros_subscribers(state)

    # MQTT
    mqtt = MqttClient(config, identity, proto, processor.on_command)
    processor.client = mqtt  # 注入 MQTT client 用于回执
    mqtt.connect()
    mqtt.loop_start()

    # 周期线程
    stop = threading.Event()
    threads = [
        threading.Thread(target=heartbeat_loop, args=(proto, mqtt, stop), daemon=True),
        threading.Thread(target=telemetry_loop, args=(proto, mqtt, state, stop), daemon=True),
        threading.Thread(target=location_loop, args=(proto, mqtt, state, stop), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        if ros_ok:
            rospy.spin()
        else:
            LOG.info("MQTT-only 模式（rospy 不可用）；等待 Ctrl+C")
            stop.wait()
    except KeyboardInterrupt:
        LOG.info("收到 Ctrl+C，停止")
    finally:
        mqtt.publish(
            proto.topic("availability"),
            avail_uplink.make_availability(proto, "offline", reason="BRIDGE_STOP"),
            qos=1,
            retain=True,
        )
        time.sleep(0.3)
        mqtt.loop_stop()
        mqtt.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
