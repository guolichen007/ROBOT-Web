"""独立 MQTT gateway。只转换协议，权限/DB/任务仍由既有控制面负责。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import paho.mqtt.client as mqtt
from app.core.config import get_settings
from app.core.events import get_redis
from app.core.logging import configure_logging
from redis.exceptions import WatchError

from services.vehicle_agent_v1 import (
    PROTOCOL,
    TOPIC_CONTRACT,
    command_from_canonical,
    normalize,
    parse_topic,
    topic,
)

INTERNAL = "_platform/vehicle-agent-v1"
PENDING = "vehicle-agent:v1:pending"
logger = logging.getLogger("vehicle-agent-adapter")


def key(vehicle: str, msg_id: str) -> str:
    return f"vehicle-agent:v1:correlation:{vehicle}:{msg_id}"


def safe_json(payload: bytes) -> Any:
    if len(payload) > 262144:
        raise ValueError("payload too large")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        value = payload.decode("utf-8")

    def check(item: Any, depth: int = 0) -> None:
        if depth > 20:
            raise ValueError("payload too deep")
        if isinstance(item, str) and item.lower().startswith(("data:image/", "data:video/")):
            raise ValueError("media payload forbidden")
        if isinstance(item, dict):
            for nested in item.values():
                check(nested, depth + 1)
        if isinstance(item, list):
            for nested in item:
                check(nested, depth + 1)

    check(value)
    return value


class Adapter:
    def __init__(self, redis: Any, publish: Any, *, downlink: bool = False, ack_seconds: int = 90):
        self.redis = redis
        self.publish = publish
        self.downlink = downlink
        self.ack_seconds = ack_seconds

    def emit(self, vehicle: str, kind: str, data: dict, *, retained: bool = False) -> None:
        self.publish(
            f"{INTERNAL}/{vehicle}/{kind}",
            json.dumps(
                {
                    "event_id": str(uuid4()),
                    "retained": retained,
                    "vehicle_id": vehicle,
                    "kind": kind,
                    "received_at": datetime.now(UTC).isoformat(),
                    "data": data,
                },
                ensure_ascii=False,
            ),
            1,
            False,
        )

    def down(self, vehicle: str, payload: dict) -> None:
        wire = command_from_canonical(payload)
        if payload["vehicle_id"] != vehicle:
            raise ValueError("vehicle/topic mismatch")
        if not self.downlink:
            raise ValueError("vehicle_agent downlink disabled")
        binding = f"{PROTOCOL}:{payload['target_boot_id']}"
        if self.redis.get(f"vehicle-agent:binding:{vehicle}") != binding:
            raise ValueError("binding/boot no longer active")
        now = datetime.now(UTC)
        expires_at = datetime.fromisoformat(payload["expires_at"])
        if expires_at <= now:
            raise ValueError("expired canonical command")
        # MQTT5 Message Expiry Interval（R2-8）：max(1, ceil(remaining))，broker 过期即弃
        expiry_interval = max(1, math.ceil((expires_at - now).total_seconds()))
        correlation_key = key(vehicle, wire["msg_id"])
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        entry = {
            "vehicle_id": vehicle,
            "msg_id": wire["msg_id"],
            "command_id": payload["command_id"],
            "message_id": payload["message_id"],
            "target_boot_id": payload["target_boot_id"],
            "fingerprint": fingerprint,
            "deadline": (now + timedelta(seconds=self.ack_seconds)).timestamp(),
        }
        created = self.redis.set(correlation_key, json.dumps(entry), ex=86400, nx=True)
        if not created:
            entry = json.loads(self.redis.get(correlation_key))
            if entry["fingerprint"] != fingerprint:
                raise ValueError("msg_id reused with different command")
            cached = self.redis.get(correlation_key + ":ack")
            if cached:
                self.emit(vehicle, "ack", json.loads(cached))
                return
            if now.timestamp() >= entry["deadline"]:
                self.finish(entry, self.timeout_ack(entry))
                return
        self.redis.zadd(PENDING, {correlation_key: entry["deadline"]})
        # Retried canonical messages retain EXACTLY the same UUID; the vehicle's
        # five-minute dedup cache prevents a second execution after ACK loss.
        qos, retain = TOPIC_CONTRACT["cmd"]
        self.publish(
            topic(vehicle, "cmd"),
            json.dumps(wire, ensure_ascii=False),
            qos,
            retain,
            expiry_interval,
        )

    @staticmethod
    def timeout_ack(entry: dict) -> dict:
        return {
            "msg_id": entry["msg_id"],
            "success": False,
            "accepted": False,
            "code": "TIMEOUT",
            "message": "等待 vehicle_agent ACK 超时，执行结果未确认",
            "data": {},
            "duration_ms": 0,
            "ts": datetime.now(UTC).isoformat(),
            "ack_source": "transport_timeout",
        }

    def finish(self, entry: dict, ack: dict) -> None:
        correlation_key = key(entry["vehicle_id"], entry["msg_id"])
        normalized = {
            **ack,
            "ack_source": ack.get("ack_source", "vehicle"),
            "command_id": entry["command_id"],
            "message_id": entry["message_id"],
            "target_boot_id": entry["target_boot_id"],
        }
        # First real vehicle ACK wins. A transport timeout is only an observation
        # of missing ACK and can be replaced by that exact command's late ACK.
        cache_key = correlation_key + ":ack"
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(cache_key)
                    cached = pipe.get(cache_key)
                    previous = json.loads(cached) if cached else None
                    if previous and (
                        previous.get("ack_source") == "vehicle"
                        or normalized["ack_source"] == "transport_timeout"
                    ):
                        normalized = previous
                        pipe.unwatch()
                    else:
                        pipe.multi()
                        pipe.set(cache_key, json.dumps(normalized), ex=86400)
                        pipe.execute()
                    break
                except WatchError:
                    continue
        # Emit duplicates too: durable ingress correlation makes this harmless,
        # and it repairs a lost internal publish without re-executing the vehicle.
        self.emit(entry["vehicle_id"], "ack", normalized)
        self.redis.zrem(PENDING, correlation_key)

    def expire(self) -> None:
        for correlation_key in self.redis.zrangebyscore(
            PENDING, "-inf", time.time(), start=0, num=100
        ):
            raw = self.redis.get(correlation_key)
            if raw:
                entry = json.loads(raw)
                self.finish(entry, self.timeout_ack(entry))
            else:
                self.redis.zrem(PENDING, correlation_key)

    def process(self, name: str, payload: bytes, *, retained: bool = False) -> None:
        data = safe_json(payload)
        if name.startswith(INTERNAL + "/"):
            parts = name.split("/")
            if len(parts) != 4 or parts[3] != "command":
                return
            self.down(parts[2], data)
            return
        vehicle, kind = parse_topic(name)
        if kind == "cmd":
            return
        bucket = f"vehicle-agent:rate:{vehicle}:{kind}:{int(time.time())}"
        count = self.redis.incr(bucket)
        if count == 1:
            self.redis.expire(bucket, 2)
        if count > get_settings().mqtt_rate_limit_per_second:
            raise ValueError("vehicle_agent message rate exceeded")
        if kind == "ack" and retained:
            raise ValueError("retained ACK forbidden")
        normalized = normalize(kind, data)
        if kind == "ack":
            raw = self.redis.get(key(vehicle, normalized["msg_id"]))
            if not raw:
                raise ValueError("ACK has no exact vehicle/msg_id correlation")
            self.finish(json.loads(raw), normalized)
        else:
            self.emit(vehicle, kind, normalized, retained=retained)


def main() -> None:
    settings = get_settings()
    configure_logging("vehicle-agent-adapter")
    redis = get_redis()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        # 稳定 client id（R2-6）：随机后缀每次重启成为全新会话，丢失持久会话的 ACK 恢复能力
        client_id=os.environ.get(
            "FIREBOT_SERVICE_INSTANCE_ID", f"vehicle-agent-adapter-{socket.gethostname()}"
        ),
        protocol=mqtt.MQTTv5,
    )

    def publish(
        name: str, payload: str, qos: int, retain: bool, message_expiry: int | None = None
    ) -> None:
        props = (
            mqtt.Properties(MessageExpiryInterval=message_expiry) if message_expiry else None
        )
        info = client.publish(name, payload, qos=qos, retain=retain, properties=props)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("adapter MQTT publish rejected")

    adapter = Adapter(
        redis,
        publish,
        downlink=settings.vehicle_agent_v1_downlink_enabled,
        ack_seconds=settings.vehicle_agent_v1_ack_timeout_seconds,
    )

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            return
        for kind in TOPIC_CONTRACT:
            if kind != "cmd":
                client.subscribe(f"v1/robot/+/{kind}", qos=TOPIC_CONTRACT[kind][0])
        client.subscribe(f"{INTERNAL}/+/command", qos=1)

    def on_message(client, userdata, message):
        try:
            adapter.process(message.topic, message.payload, retained=message.retain)
        except Exception:
            logger.warning("adapter rejected message on %s", message.topic, exc_info=True)

    client.on_connect = on_connect
    client.on_message = on_message
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.effective_mqtt_password)
    settings.configure_mqtt_client(client)
    # 持久会话（clean_start=False + Session Expiry 600s）：adapter 重启后
    # 恢复订阅并接收 broker 代存的 v1 QoS1 ACK（late ACK 修复的第二层保障）
    client.connect_async(
        settings.mqtt_host,
        settings.mqtt_port,
        keepalive=30,
        clean_start=False,
        properties=settings.mqtt_connect_properties(durable=True),
    )
    client.loop_start()
    while True:
        try:
            adapter.expire()
            if client.is_connected():
                redis.setex(
                    "service:vehicle-agent-adapter:heartbeat", 5, datetime.now(UTC).isoformat()
                )
        except Exception:
            logger.exception("adapter maintenance failed")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
