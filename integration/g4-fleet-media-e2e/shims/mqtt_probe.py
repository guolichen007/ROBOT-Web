#!/usr/bin/env python3
"""用 Edge Home 中 enrollment 下发的凭证，对隔离 broker 做真实 MQTT-TLS 认证（MQTTv5 CONNACK）。

用法：mqtt_probe.py <EDGE_HOME>     环境：SD_BROKER_PORT、SD_BROKER_CA
退出码 0 = 认证成功；其余 = 失败。只连接 127.0.0.1（绝不使用 bridge.env 中的生产主机名）。
"""

import os
import sys
import time

import paho.mqtt.client as mqtt

edge = sys.argv[1]


def kv(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            out[k] = v
    return out


ident = kv(os.path.join(edge, "config", "identity.env"))
secret = kv(os.path.join(edge, "secrets", "mqtt.env"))
result = {}
c = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2, client_id="probe-%d" % time.time(), protocol=mqtt.MQTTv5
)
c.username_pw_set(ident["DEVICE_ID"], secret["FIREBOT_MQTT_PASSWORD"])
c.tls_set(ca_certs=os.environ["SD_BROKER_CA"])
c.on_connect = lambda cl, ud, flags, rc, props=None: result.setdefault(
    "rc", rc.value if hasattr(rc, "value") else rc
)
try:
    c.connect("127.0.0.1", int(os.environ["SD_BROKER_PORT"]), 10)
    c.loop_start()
    for _ in range(50):
        if "rc" in result:
            break
        time.sleep(0.1)
    c.loop_stop()
    c.disconnect()
except Exception:  # noqa: BLE001
    sys.exit(2)
sys.exit(0 if result.get("rc") == 0 else 1)
