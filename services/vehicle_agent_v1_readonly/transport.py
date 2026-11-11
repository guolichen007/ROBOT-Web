"""MQTT 上行 transport：TLS 校验证书、per-device 凭证、LWT、重连退避、正常退出离线。

只实现上行发布；本 transport 结构上不提供订阅能力（subscribe 直接抛错），保证本
阶段 Agent 不可能接收/响应任何命令。正式 MQTT 必须验证服务端证书：只信任配置 CA，
绝不调用 tls_insecure_set。
"""

from __future__ import annotations

import json
import random
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import paho.mqtt.client as mqtt

from services.vehicle_agent_v1 import topic
from services.vehicle_agent_v1_readonly.identity import DeviceIdentity


class SubscribeForbiddenError(RuntimeError):
    """只读 Agent 结构上禁止订阅。"""


class MqttConnectError(RuntimeError):
    """连接被拒绝（网络 / TLS / 凭证 / CONNACK 非 0）。"""


@dataclass
class TransportConfig:
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    keepalive_seconds: int = 30
    connect_wait_seconds: float = 15.0
    offline_publish_timeout_seconds: float = 2.0
    sleep_fn: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)

    def backoff_delay(self, attempt: int) -> float:
        """attempt 从 0 开始：min * 2^attempt，封顶 max，加 0~10% 抖动。"""
        delay = min(self.reconnect_max_seconds, self.reconnect_min_seconds * (2**attempt))
        return delay + self.rng.uniform(0.0, delay * 0.1)


class MqttTransport:
    """只发布上行消息的 MQTT transport（paho 封装）。"""

    def __init__(self, identity: DeviceIdentity, config: TransportConfig | None = None) -> None:
        self.identity = identity
        self.config = config or TransportConfig()
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=identity.client_id,
            protocol=mqtt.MQTTv311,
        )
        self._connected = False
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        # Last Will：异常断线时由 broker 发布 retained offline（连接前固定 payload）。
        self._client.will_set(
            topic(identity.vehicle_id, "availability"), "offline", qos=1, retain=True
        )
        # per-device 凭证；ACL 合同要求 username == canonical vehicle_id。
        self._client.username_pw_set(identity.mqtt_username, identity.mqtt_password)
        # 正式 MQTT 必须验证服务端证书：仅信任配置 CA。
        self._client.tls_set(
            ca_certs=str(identity.ca_cert_path),
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        try:
            rc = self._client.connect(
                self.identity.mqtt_host,
                self.identity.mqtt_port,
                keepalive=self.config.keepalive_seconds,
            )
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise MqttConnectError(f"connect failed: {exc}") from exc
        if rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttConnectError(f"paho connect returned rc={rc}")

    def loop(self, timeout: float = 0.1) -> None:
        self._client.loop(timeout=timeout)

    def wait_for_connect(self, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout or self.config.connect_wait_seconds)
        while time.monotonic() < deadline:
            if self._connected:
                return True
            self.loop(0.05)
        return False

    def publish_availability(self, state: str) -> None:
        self._publish("availability", state, qos=1, retain=True)

    def publish_status(self, data: dict[str, Any]) -> None:
        self._publish("status", json.dumps(data), qos=1, retain=True)

    def publish_telemetry(self, data: dict[str, Any]) -> None:
        self._publish("telemetry", json.dumps(data), qos=0, retain=False)

    def subscribe(self, *args: Any, **kwargs: Any) -> None:
        # 反证保证：本阶段 Agent 结构上不存在订阅路径，永不接收 cmd。
        raise SubscribeForbiddenError("readonly agent never subscribes to any topic")

    def disconnect(self) -> None:
        self._client.disconnect()

    def graceful_offline(self) -> None:
        """正常退出：发布 retained offline 后干净断开（broker 不触发 LWT）。"""
        if self._connected:
            info = self._client.publish(
                topic(self.identity.vehicle_id, "availability"),
                "offline",
                qos=1,
                retain=True,
            )
            info.wait_for_publish(self.config.offline_publish_timeout_seconds)
        self.disconnect()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and self._connected:
            self.loop(0.05)

    def _publish(self, kind: str, payload: str, *, qos: int, retain: bool) -> None:
        info = self._client.publish(
            topic(self.identity.vehicle_id, kind), payload, qos=qos, retain=retain
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttConnectError(f"publish to {kind} failed: rc={info.rc}")

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        self._connected = reason_code == 0

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self._connected = False
