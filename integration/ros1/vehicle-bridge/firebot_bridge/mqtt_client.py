"""MQTT 客户端：TLS、LWT、单一连接 owner（connect_async + loop_start）、订阅 command、发布上行。

连接所有权归 Paho 自己的后台线程：loop_start() 内部 loop_forever(retry_first_connection=True)，
配合 reconnect_delay_set(1, 30) 处理初次失败、运行期断线与 broker 重启，全链路只有一个
reconnect owner，不再手写 retry loop 与后台线程抢连接状态。
"""
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
    def __init__(self, config: Config, identity: Identity, proto: Protocol, on_command, status=None, trace=None) -> None:
        self.config = config
        self.identity = identity
        self.proto = proto
        self.on_command = on_command
        self.status = status
        self.trace = trace

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=identity.client_id,
            protocol=mqtt.MQTTv5,
        )
        self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        if config.mqtt_tls:
            self.client.tls_set(ca_certs=config.ca_cert)

        # LWT：异常掉线上报 offline（retain）。LWT 不按 seq 单调（v1.3 约束）。
        lwt = avail_uplink.make_availability(self.proto, "offline", reason="LWT")
        self.client.will_set(
            self.proto.topic("availability"),
            json.dumps(lwt, ensure_ascii=False),
            qos=1,
            retain=True,
        )

        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---- 启动（单一连接 owner）----
    def start(self) -> None:
        # 指数退避由 Paho 内部接管；本进程/ boot_id 全程不变。
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.connect_async(self.config.mqtt_host, self.config.mqtt_port, keepalive=30)
        self.client.loop_start()

    # ---- 回调 ----
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            LOG.error("MQTT connect failed rc=%s（可能是 TLS/认证/网络）", reason_code)
            if self.status:
                self.status.set(mqtt_connected=False)
            return
        LOG.info("MQTT connected (boot=%s)", self.identity.boot_id[:8])
        if self.trace:
            self.trace.emit(
                "mqtt.connected",
                level="ok",
                broker=f"{self.config.mqtt_host}:{self.config.mqtt_port}",
                boot=self.identity.boot_id,
            )
        client.subscribe(self.proto.topic("command"), qos=1)
        if self.trace:
            self.trace.emit("mqtt.subscribed", level="ok", topic=self.proto.topic("command"), qos=1)
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
        if self.status:
            self.status.set(mqtt_connected=True)
        LOG.info("已订阅命令 topic: %s", self.proto.topic("command"))

    def _on_connect_fail(self, client, userdata) -> None:
        LOG.warning("MQTT connect fail（Paho 将按退避重试）")
        if self.trace:
            self.trace.emit("mqtt.connect_failed", level="warn",
                            broker=f"{self.config.mqtt_host}:{self.config.mqtt_port}")
        if self.status:
            self.status.set(mqtt_connected=False)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        LOG.warning("MQTT disconnected rc=%s（Paho 自动重连）", reason_code)
        if self.trace:
            self.trace.emit("mqtt.disconnected", level="warn", rc=reason_code)
        if self.status:
            self.status.set(mqtt_connected=False)

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if self.trace:
                self.trace.emit("mqtt.command.ignored", level="debug", reason="INVALID_JSON")
            return
        if not isinstance(payload, dict) or payload.get("type") != "command":
            if self.trace:
                self.trace.emit("mqtt.command.ignored", level="debug", reason="NOT_COMMAND")
            return
        if payload.get("vehicle_id") != self.config.vehicle_id:
            if self.trace:
                self.trace.emit("mqtt.command.ignored", level="debug", reason="VEHICLE_MISMATCH")
            return
        if self.trace:
            self.trace.command_received(payload)
        try:
            self.on_command(payload)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("command handler error: %s", exc)

    # ---- 发布 ----
    def publish(self, topic: str, payload: dict, qos: int = 0, retain: bool = False):
        result = self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=qos, retain=retain)
        self._trace_publish(payload)
        return result

    def _trace_publish(self, payload: dict) -> None:
        if not self.trace:
            return
        try:
            msg_type = payload.get("type")
            if msg_type == "heartbeat":
                self.trace.throttle("mqtt.heartbeat", 10.0, "mqtt.heartbeat.tx", level="debug",
                                    seq=payload.get("seq"), uptime=payload.get("uptime_seconds"))
            elif msg_type == "status":
                self.trace.changed("mqtt.status.battery", payload.get("battery"), "mqtt.status.tx",
                                   tolerance=0.1, battery=payload.get("battery"), mode=payload.get("mode"))
            elif msg_type == "sensor":
                self.trace.changed("mqtt.sensor.smoke", payload.get("smoke"), "mqtt.sensor.tx",
                                   smoke=payload.get("smoke"))
            elif msg_type == "location":
                self.trace.throttle("mqtt.location", 5.0, "mqtt.location.tx", level="debug")
            elif msg_type == "availability":
                self.trace.emit("mqtt.availability.tx", level="tx",
                                state=payload.get("state"), reason=payload.get("reason"))
            elif msg_type == "capabilities":
                self.trace.emit("mqtt.capabilities.tx", level="tx",
                                commands=len(payload.get("supported_commands") or []),
                                sensors=len(payload.get("sensors") or []))
            # command_ack / task_status 的 TX 事件由 CommandProcessor 里 emit（带 latency），此处不重复
        except Exception:  # noqa: BLE001 — trace 绝不能影响 publish
            pass

    # ---- 生命周期 ----
    def loop_stop(self) -> None:
        self.client.loop_stop()

    def disconnect(self) -> None:
        self.client.disconnect()

    def is_connected(self) -> bool:
        return self.client.is_connected()
