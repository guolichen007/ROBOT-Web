#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Firebot 服务器端 → 车端 持续「开始巡航」命令发送器（标准 schema 1.3 协议）
====================================================================

向 `robot/{vehicle_id}/command` 发布标准 command 消息（cmd=patrol），
供车端 Bridge 接收并转发到 ROS placeholder。仅作联通性测试工具。

车端当前 boot_id 通过订阅其 heartbeat 获取，填入 target_boot_id。

安全：本工具只发消息，不执行任何车辆运动。车端在 BRIDGE_STUB_MODE 下
会回 rejected/BRIDGE_ADAPTER_NOT_CONNECTED（预期）。

用法：
  python3 next-cruise-loop-sender.py --once        # 发一条
  python3 next-cruise-loop-sender.py --interval 5  # 每 5 秒发
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt

LOG = logging.getLogger("cruise-sender")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# ---------------- 配置 ----------------
MQTT_HOST = os.environ.get("FIREBOT_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("FIREBOT_MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("FIREBOT_MQTT_USER", "platform")
MQTT_PASSWORD = os.environ.get("FIREBOT_MQTT_PASSWORD", "")
CA_CERT = os.environ.get("FIREBOT_CA_CERT", "/opt/firebot/production-ca/ca.crt")
VEHICLE_ID = os.environ.get("FIREBOT_VEHICLE_ID", "firebot-vehicle-01")
INTERVAL_SECONDS = float(os.environ.get("FIREBOT_CRUISE_INTERVAL", "5"))
TTL_MS = int(os.environ.get("FIREBOT_COMMAND_TTL_MS", "30000"))
OPERATOR_ID = os.environ.get("FIREBOT_OPERATOR_ID", "cruise-sender")

CMD_TOPIC = f"robot/{VEHICLE_ID}/command"
CMD = "patrol"

BOOT_LOCK = threading.Lock()
current_boot_id: str | None = None


# ---------------- 凭据解析（只从环境变量/安全配置，不自动 sudo 取 secret） ----------------
def resolve_mqtt_password() -> str:
    if MQTT_PASSWORD:
        return MQTT_PASSWORD
    raise SystemExit("未设置 FIREBOT_MQTT_PASSWORD（本工具只从环境变量取凭据，不自动 sudo 读取 secret）。")


def resolve_ca_cert() -> str:
    if os.path.exists(CA_CERT):
        return CA_CERT
    raise SystemExit(f"无法读取 CA 证书：{CA_CERT}（请通过 FIREBOT_CA_CERT 环境变量显式指定路径）。")


# ---------------- 车端 boot 捕获 ----------------
def on_message(client, userdata, message) -> None:
    global current_boot_id
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if message.topic.endswith("/heartbeat") and payload.get("vehicle_id") == VEHICLE_ID:
        boot = payload.get("boot_id")
        if boot:
            with BOOT_LOCK:
                current_boot_id = str(boot)
            LOG.info("车端 heartbeat: boot_id=%s", current_boot_id[:8])
    elif message.topic.endswith("/command_ack"):
        LOG.info(
            "  ⬅ command_ack: cmd=%s status=%s reason=%s",
            payload.get("command_id"), payload.get("status"), payload.get("reason_code"),
        )
    elif message.topic.endswith("/task_status"):
        LOG.info(
            "  ⬅ task_status: task=%s status=%s phase=%s progress=%s%%",
            str(payload.get("task_id"))[:8], payload.get("status"),
            payload.get("phase"), payload.get("progress"),
        )


def on_connect(client, userdata, flags, reason_code, properties) -> None:
    if reason_code == 0:
        LOG.info("MQTT connected to %s:%s as %s", MQTT_HOST, MQTT_PORT, MQTT_USER)
        client.subscribe(f"robot/{VEHICLE_ID}/heartbeat", qos=1)
        client.subscribe(f"robot/{VEHICLE_ID}/command_ack", qos=1)
        client.subscribe(f"robot/{VEHICLE_ID}/task_status", qos=1)
    else:
        LOG.error("MQTT connect failed rc=%s", reason_code)


# ---------------- 标准 command 构造 ----------------
def build_patrol_command(boot_id: str) -> dict:
    now = datetime.now(timezone.utc)
    task_id = str(uuid.uuid4())
    return {
        "schema_version": "1.3",
        "message_id": str(uuid.uuid4()),
        "type": "command",
        "vehicle_id": VEHICLE_ID,
        "target_boot_id": boot_id,
        "command_id": f"C{now:%Y%m%d%H%M%S}-{str(uuid.uuid4())[:8]}",
        "correlation_id": str(uuid.uuid4()),
        "task_id": task_id,                     # task_id 用顶层字段
        "lease_id": None,
        "control_session_id": None,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(milliseconds=TTL_MS)).isoformat(),
        "ttl_ms": TTL_MS,
        "priority": 50,
        "source": "WEB",                         # source 必须 WEB（schema const）
        "operator_id": OPERATOR_ID,
        "cmd": CMD,
        "params": {},                            # task_id 只在顶层，params 只放命令自身参数
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="服务器端 → 车端 开始巡航（标准 schema 1.3）")
    parser.add_argument("--interval", type=float, default=INTERVAL_SECONDS,
                        help="发送间隔秒数（默认 %(default)s）")
    parser.add_argument("--once", action="store_true", help="只发一条后退出")
    args = parser.parse_args()
    if args.interval <= 0:
        raise SystemExit("间隔必须 > 0")

    password = resolve_mqtt_password()
    ca_cert = resolve_ca_cert()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"cruise-sender-{time.time():.0f}",
        protocol=mqtt.MQTTv5,
    )
    client.username_pw_set(MQTT_USER, password)
    client.tls_set(ca_certs=ca_cert)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    deadline = time.time() + 15
    while not client.is_connected() and time.time() < deadline:
        time.sleep(0.2)

    sent = 0
    try:
        while True:
            with BOOT_LOCK:
                boot = current_boot_id
            if not boot:
                LOG.warning("尚未收到车端 heartbeat（boot 未知），等待车端上线后发送…")
                time.sleep(2)
                continue
            command = build_patrol_command(boot)
            sent += 1
            info = client.publish(
                CMD_TOPIC, json.dumps(command, ensure_ascii=False), qos=1, retain=False,
            )
            info.wait_for_publish(timeout=5)
            LOG.info(
                "➡ [%d] 已发送 %s → %s (boot=%s ttl=%sms)",
                sent, command["cmd"], CMD_TOPIC, boot[:8], TTL_MS,
            )
            if args.once:
                time.sleep(3)
                LOG.info("--once 模式：已发送并观察回执，退出")
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        LOG.info("收到 Ctrl+C，停止发送")
    finally:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
