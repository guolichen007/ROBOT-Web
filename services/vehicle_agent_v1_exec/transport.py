"""双向 MQTT transport（仅授权执行模式使用）。

- 网络循环在 paho 后台线程（loop_start）：消息回调只入队，不阻塞重连/keepalive/心跳；
- clean session：Broker 不为本设备保留离线命令队列（上一会话命令不会在重连后投递）；
- 只订阅 ``v1/robot/{vehicle_id}/cmd``（ACL 亦只允许读自车 cmd）；
- TLS 只信任配置 CA（不调用 tls_insecure_set(True)）；LWT retained offline。
"""

from __future__ import annotations

import json
import queue
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from services.vehicle_agent_v1 import TOPIC_CONTRACT, topic
from services.vehicle_agent_v1_readonly.identity import DeviceIdentity

MAX_QUEUED_MESSAGES = 64
MAX_INFLIGHT_MESSAGES = 8
COMMAND_QUEUE_LIMIT = 32


class DuplexTransport:
    def __init__(
        self,
        identity: DeviceIdentity,
        *,
        on_session: Callable[[], None] | None = None,
        tls: bool = True,
        reconnect_min: float = 1.0,
        reconnect_max: float = 30.0,
    ) -> None:
        self.identity = identity
        self.commands: queue.Queue = queue.Queue(maxsize=COMMAND_QUEUE_LIMIT)
        self._on_session = on_session
        self._connected = threading.Event()
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{identity.vehicle_id}-exec-v1",
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        self._client.max_queued_messages_set(MAX_QUEUED_MESSAGES)
        self._client.max_inflight_messages_set(MAX_INFLIGHT_MESSAGES)
        self._client.reconnect_delay_set(min_delay=reconnect_min, max_delay=reconnect_max)
        self._client.will_set(
            topic(identity.vehicle_id, "availability"), "offline", qos=1, retain=True
        )
        self._client.username_pw_set(identity.mqtt_username, identity.mqtt_password)
        if tls:
            self._client.tls_set(
                ca_certs=str(identity.ca_cert_path),
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message
        self.cmd_topic = topic(identity.vehicle_id, "cmd")
        self.dropped_commands = 0

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        self._client.connect_async(self.identity.mqtt_host, self.identity.mqtt_port, keepalive=30)
        self._client.loop_start()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def _handle_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            return
        # 先建立新会话（新 boot_id），再订阅：任何本会话收到的命令都按新会话校验
        if self._on_session:
            self._on_session()
        client.subscribe(self.cmd_topic, qos=TOPIC_CONTRACT["cmd"][0])
        self._connected.set()

    def _handle_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self._connected.clear()

    def _handle_message(self, client, userdata, message) -> None:
        # 只入队；执行在工作线程。retained 命令（不应存在）一律丢弃。
        if message.topic != self.cmd_topic or message.retain:
            return
        try:
            self.commands.put_nowait(bytes(message.payload))
        except queue.Full:
            self.dropped_commands += 1

    def publish(self, kind: str, data: Any, *, retain: bool | None = None, timeout: float = 3.0):
        qos, default_retain = TOPIC_CONTRACT[kind]
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        info = self._client.publish(
            topic(self.identity.vehicle_id, kind),
            payload,
            qos=qos,
            retain=default_retain if retain is None else retain,
        )
        if qos == 0:
            return info.rc == mqtt.MQTT_ERR_SUCCESS
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if info.is_published():
                return True
            if info.rc not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN):
                return False
            time.sleep(0.02)
        return False

    def graceful_offline(self) -> bool:
        if not self.connected:
            self.stop()
            return True
        confirmed = self.publish("availability", "offline", retain=True, timeout=2.0)
        self.stop()
        return confirmed
