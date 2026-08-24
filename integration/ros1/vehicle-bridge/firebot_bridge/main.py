#!/usr/bin/env python3
"""Firebot 车端 ROS Bridge 主入口。

职责（通信层）：
  - MQTT/TLS 常驻连接 + LWT + 初始/运行时重连
  - 上行：availability/heartbeat/capabilities/status(battery)/sensor(smoke)/location
  - 下行：订阅 robot/{id}/command → 校验/去重 → 转发 ROS placeholder → 据 feedback 回 ACK/task_status
  - 不做任何车辆运动/巡航/急停/灭火/回充/手动控制执行

关键架构：MQTT 通信层与 ROS master 生死解耦。ROS 由后台 RosLifecycle 管理——
无 roscore 时 MQTT 仍在线（命令 rejected + BRIDGE_ADAPTER_NOT_CONNECTED），
roscore 延迟出现自动初始化，进程/boot_id 不变。
"""
from __future__ import annotations

import logging
import threading
import time

# 尝试导入 rospy（ROS Noetic）；不可用时 MQTT 仍工作，命令回 rejected
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
from .ros.lifecycle import RosLifecycle
from .runtime_status import RuntimeStatus
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


# ---------------- 主流程 ----------------
def main() -> int:
    config = get_config()
    identity = Identity(config.vehicle_id)
    proto = Protocol(config.vehicle_id, identity.boot_id)
    state = BridgeState()

    LOG.info("bridge 启动: vehicle=%s boot=%s stub=%s",
             config.vehicle_id, identity.boot_id[:8], config.bridge_stub_mode)

    status = RuntimeStatus()
    status.set(boot_id=identity.boot_id)

    # 1) ROS lifecycle（后台线程，与 MQTT 解耦，绝不阻塞）
    ros = RosLifecycle(rospy, state, status=status)
    processor = CommandProcessor(config, state, identity, proto, mqtt_client=None, placeholder=ros)
    ros.set_on_feedback(processor.on_feedback)

    # 2) MQTT 先上线（与 ROS master 无关）
    mqtt = MqttClient(config, identity, proto, processor.on_command, status=status)
    processor.client = mqtt  # 注入 MQTT client 用于回执

    ros.start()
    mqtt.loop_start()
    mqtt.connect_with_retry()  # 初始连接失败不退出进程，指数退避重试

    # 3) 周期上报线程（连上后才启动）
    stop = threading.Event()
    threads = [
        threading.Thread(target=heartbeat_loop, args=(proto, mqtt, stop), daemon=True),
        threading.Thread(target=telemetry_loop, args=(proto, mqtt, state, stop), daemon=True),
        threading.Thread(target=location_loop, args=(proto, mqtt, state, stop), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        stop.wait()
    except KeyboardInterrupt:
        LOG.info("收到 Ctrl+C，停止")
    finally:
        ros.stop()
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
