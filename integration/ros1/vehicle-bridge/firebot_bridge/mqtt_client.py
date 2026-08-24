"""MQTT 客户端：TLS、LWT、reconnect、订阅 command、发布上行。"""
from __future__ import annotations

import json
import logging

import paho.mqtt.client as mqtt

from .config import Config
from .identity import Identity
from .protocol import Protocol
from .uplink import availability as avail_uplink
from .uplink import capabilities as cap_uplink

LOG = logging.getLogger("firebot-bridge")

OnCommand = "callable[[dict], None]"


class MqttClient:
    def __init__(self, config: Config, identity: Identity, proto: Protocol, on_command) -> None:
        self.config = config
        self.identity = identity
        self.proto = proto
        self.on_command = on_command

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=identity.client_id,
            protocol=mqtt.MQTTv5,
        )
        self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        if config.mqtt_tls:
            self.client.tls_set(ca_certs=config.ca_cert)

        # LWT：异常掉线上报 offline（retain）。LWT 不按 seq 单调（v1.3 约束），
        # 服务器 ingress 对 availability 跳过 seq 检查，用 message_id 去重。
        lwt = avail_uplink.make_availability(self.proto, "offline", reason="LWT")
        self.client.will_set(
            self.proto.topic("availability"),
            json.dumps(lwt, ensure_ascii=False),
            qos=1,
            retain=True,
        )

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---- 回调 ----
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            LOG.error("MQTT connect failed rc=%s", reason_code)
            return
        LOG.info("MQTT connected (boot=%s)", self.identity.boot_id[:8])
        client.subscribe(self.proto.topic("command"), qos=1)
        # 上线 announcement + 能力声明（QoS1 retain）
        self.publish(
            self.proto.topic("availability"),
            avail_uplink.make_availability(self.proto, "online", reason="BRIDGE_START"),
            qos=1,
            retain=True,
        )
        self.publish(
            self.proto.topic("capabilities"),
            cap_uplink.make_capabilities(self.proto, self.config),
            qos=1,
            retain=True,
        )
        LOG.info("已订阅命令 topic: %s", self.proto.topic("command"))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        LOG.warning("MQTT disconnected rc=%s", reason_code)

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("type") != "command":
            return
        if payload.get("vehicle_id") != self.config.vehicle_id:
            return
        try:
            self.on_command(payload)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("command handler error: %s", exc)

    # ---- 发布 ----
    def publish(self, topic: str, payload: dict, qos: int = 0, retain: bool = False) -> None:
        self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=qos, retain=retain)

    # ---- 生命周期 ----
    def connect(self) -> None:
        self.client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=30)

    def loop_start(self) -> None:
        self.client.loop_start()

    def loop_stop(self) -> None:
        self.client.loop_stop()

    def disconnect(self) -> None:
        self.client.disconnect()

    def is_connected(self) -> bool:
        return self.client.is_connected()
