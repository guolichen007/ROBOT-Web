"""MQTT 客户端：TLS、LWT、初始/运行时重连、订阅 command、发布上行。"""
from __future__ import annotations

import json
import logging
import time

import paho.mqtt.client as mqtt

from .config import Config
from .identity import Identity
from .protocol import Protocol
from .uplink import availability as avail_uplink
from .uplink import capabilities as cap_uplink

LOG = logging.getLogger("firebot-bridge")

OnCommand = "callable[[dict], None]"


class MqttClient:
    def __init__(self, config: Config, identity: Identity, proto: Protocol, on_command, status=None) -> None:
        self.config = config
        self.identity = identity
        self.proto = proto
        self.on_command = on_command
        self.status = status

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=identity.client_id,
            protocol=mqtt.MQTTv5,
        )
        self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        if config.mqtt_tls:
            self.client.tls_set(ca_certs=config.ca_cert)

        # 运行时断线：由 paho 在进程内指数退避自动重连（boot_id 不变，不靠 systemd 重启）
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

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
            LOG.error("MQTT connect failed rc=%s（可能是 TLS/认证/网络，将持续重试）", reason_code)
            if self.status:
                self.status.set(mqtt_connected=False)
            return
        LOG.info("MQTT connected (boot=%s)", self.identity.boot_id[:8])
        if self.status:
            self.status.set(mqtt_connected=True)
        client.subscribe(self.proto.topic("command"), qos=1)
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
        LOG.warning("MQTT disconnected rc=%s（paho 将自动重连）", reason_code)
        if self.status:
            self.status.set(mqtt_connected=False)

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
    def connect_with_retry(self, max_backoff: float = 30.0) -> None:
        """初始连接：失败不退出进程，1s→2s→…→max 指数退避重试，boot_id 保持不变。"""
        backoff = 1.0
        while not self.client.is_connected():
            try:
                self.client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=30)
            except Exception as exc:  # noqa: BLE001
                LOG.error("MQTT connect 异常: %s", exc)
            # 等 CONNACK（on_connect 回调里完成）
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not self.client.is_connected():
                time.sleep(0.2)
            if self.client.is_connected():
                LOG.info("MQTT initial connect OK")
                return
            LOG.warning("MQTT connect 未完成，%.1fs 后重试", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    def loop_start(self) -> None:
        self.client.loop_start()

    def loop_stop(self) -> None:
        self.client.loop_stop()

    def disconnect(self) -> None:
        self.client.disconnect()

    def is_connected(self) -> bool:
        return self.client.is_connected()
