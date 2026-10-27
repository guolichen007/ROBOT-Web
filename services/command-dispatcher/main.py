from __future__ import annotations

import json
import logging
import math
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta

import paho.mqtt.client as mqtt
from app.core.config import get_settings, mqtt_message_expiry_properties
from app.core.events import get_redis, queue_event
from app.core.logging import configure_logging
from app.core.serialization import serialize_model
from app.db.models import Command, OutboxEvent, Robot
from app.db.session import SessionLocal
from redis.exceptions import ResponseError
from sqlalchemy import func, select

from services.protocol import validate_message

settings = get_settings()
configure_logging("command-dispatcher")
logger = logging.getLogger("command-dispatcher")
redis = get_redis()


def dispatcher_instance_id() -> str:
    # 稳定 client id（R2-6）：随机后缀使每次重启成为全新 MQTT 会话，
    # 破坏会话连续性并遗留旧会话；生产为单副本部署约束，
    # 未来多副本须用 FIREBOT_SERVICE_INSTANCE_ID 显式区分。
    return os.environ.get(
        "FIREBOT_SERVICE_INSTANCE_ID", f"command-dispatcher-{socket.gethostname()}"
    )


INSTANCE_ID = dispatcher_instance_id()
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=INSTANCE_ID, protocol=mqtt.MQTTv5)


def publish_command(payload: dict, qos: int) -> None:
    validate_message(payload)
    expires = datetime.fromisoformat(payload["expires_at"])
    now = datetime.now(UTC)
    if expires <= now:
        raise TimeoutError("command expired before publish")
    # MQTT5 Message Expiry Interval（R2-8）：max(1, ceil(remaining))；
    # broker 对持久会话离线队列中的过期消息到期即弃（第二层防护），
    # 车辆仍必须校验 payload.expires_at。
    expiry_interval = max(1, math.ceil((expires - now).total_seconds()))
    destination = f"robot/{payload['vehicle_id']}/command"
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == payload["vehicle_id"]))
        if robot is None:
            raise ValueError("unknown command vehicle")
        if robot.agent_protocol == "VEHICLE_AGENT_V1":
            from app.core.vehicle_agent_health import adapter_healthy

            if not adapter_healthy():
                raise ValueError("v1 adapter unavailable before dispatch")
            if not settings.vehicle_agent_v1_downlink_enabled:
                raise ValueError("v1 downlink disabled")
            if payload.get("schema_version") != "vehicle-agent-v1":
                raise ValueError("legacy command forbidden on v1 transport")
            if payload["target_boot_id"] != robot.boot_id:
                raise ValueError("v1 boot changed before dispatch")
            destination = f"_platform/vehicle-agent-v1/{robot.vehicle_id}/command"
            redis.setex(
                f"vehicle-agent:binding:{robot.vehicle_id}",
                300,
                f"VEHICLE_AGENT_V1:{robot.boot_id}",
            )
        elif payload.get("schema_version") == "vehicle-agent-v1":
            raise ValueError("v1 command forbidden on legacy transport")
        elif robot.agent_protocol == "ROS_COMPAT":
            raise ValueError("ROS_COMPAT downlink is not implemented")
    info = client.publish(
        destination,
        json.dumps(payload, ensure_ascii=False),
        qos=qos,
        retain=False,
        properties=mqtt_message_expiry_properties(expiry_interval),
    )
    info.wait_for_publish(timeout=3)
    if not info.is_published():
        raise RuntimeError("MQTT publish did not complete")


def _manual_heartbeat() -> None:
    try:
        redis.setex("service:command-dispatcher:manual-heartbeat", 5, datetime.now(UTC).isoformat())
    except Exception:
        logger.exception("manual heartbeat failed")


def manual_loop() -> None:
    """Manual-command pubsub consumer with supervisor.

    A dropped Redis pubsub connection must never silently kill manual control
    for the lifetime of the process: on any exception the subscription is
    recreated with bounded exponential backoff, and a liveness heartbeat is
    published so ops can observe the loop independently.
    """
    backoff = 0.5
    while True:
        pubsub = None
        try:
            pubsub = redis.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe("firebot:manual_commands")
            while True:
                _manual_heartbeat()
                message = pubsub.get_message(timeout=1.0)
                if message is None or message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                    if datetime.fromisoformat(payload["expires_at"]) <= datetime.now(UTC):
                        continue
                    publish_command(payload, qos=0)
                    backoff = 0.5
                except Exception:
                    logger.warning("manual pulse dropped", exc_info=True)
        except Exception:
            logger.exception("manual loop failed; recreating subscription")
        finally:
            if pubsub is not None:
                try:
                    pubsub.close()
                except Exception:
                    pass
        time.sleep(backoff)
        backoff = min(30.0, backoff * 2)


def safety_loop() -> None:
    while True:
        try:
            try:
                redis.xgroup_create(
                    "firebot:safety_commands", "dispatchers", id="0-0", mkstream=True
                )
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            if client.is_connected():
                redis.setex(
                    "service:command-dispatcher:safety-heartbeat",
                    5,
                    datetime.now(UTC).isoformat(),
                )
            claimed = redis.xautoclaim(
                "firebot:safety_commands",
                "dispatchers",
                INSTANCE_ID,
                min_idle_time=1000,
                start_id="0-0",
                count=10,
            )
            process_safety_events(claimed[1])
            rows = redis.xreadgroup(
                "dispatchers",
                INSTANCE_ID,
                {"firebot:safety_commands": ">"},
                count=10,
                block=1000,
            )
            for _, events in rows:
                process_safety_events(events)
        except Exception:
            logger.exception("safety dispatcher loop failed; recreating consumer group")
            time.sleep(1)


def process_safety_events(events: list) -> None:
    for stream_id, fields in events:
        payload = json.loads(fields["command"])
        acknowledge = False
        try:
            with SessionLocal.begin() as db:
                command = db.scalar(
                    select(Command).where(Command.command_id == payload["command_id"])
                )
                try:
                    publish_command(payload, qos=1)
                    if command:
                        command.lifecycle_status = "PUBLISHED"
                        command.published_at = datetime.now(UTC)
                        queue_event(db, "command.updated", serialize_model(command))
                    acknowledge = True
                except TimeoutError:
                    if command:
                        command.lifecycle_status = "EXPIRED"
                        command.terminal_at = datetime.now(UTC)
                    acknowledge = True
                except Exception as exc:
                    logger.warning("safety command publish retry", exc_info=True)
                    if command:
                        command.ack_reason = type(exc).__name__
        except Exception:
            logger.exception("safety command database transaction failed; event remains pending")
            acknowledge = False
        if acknowledge:
            redis.xack("firebot:safety_commands", "dispatchers", stream_id)


def dispatch_next() -> bool:
    """One transactional Outbox cycle, shared by the worker and mock integration tests."""
    # 心跳绑定 MQTT 连接（R2-7）：断连时 healthcheck 暴露 unhealthy；
    # 恢复依赖 paho 有界重连，而非容器重启。
    if client.is_connected():
        redis.setex("service:command-dispatcher:outbox-heartbeat", 5, datetime.now(UTC).isoformat())
    with SessionLocal.begin() as db:
        row = db.scalar(
            select(OutboxEvent)
            .outerjoin(Command, Command.command_id == OutboxEvent.aggregate_id)
            .where(
                OutboxEvent.status == "PENDING",
                OutboxEvent.available_at <= datetime.now(UTC),
            )
            .order_by(
                func.coalesce(Command.priority, 50).desc(), OutboxEvent.created_at, OutboxEvent.id
            )
            .with_for_update(skip_locked=True, of=OutboxEvent)
        )
        if not row:
            return False
        command = db.scalar(select(Command).where(Command.command_id == row.aggregate_id))
        try:
            publish_command(row.payload_json, qos=1)
            row.status = "PUBLISHED"
            row.published_at = datetime.now(UTC)
            row.attempts += 1
            if command:
                command.lifecycle_status = "PUBLISHED"
                command.published_at = row.published_at
                queue_event(db, "command.updated", serialize_model(command))
        except TimeoutError:
            row.status = "EXPIRED"
            row.last_error = "expired"
            if command:
                command.lifecycle_status = "EXPIRED"
                command.terminal_at = datetime.now(UTC)
                queue_event(db, "command.updated", serialize_model(command))
        except Exception as exc:
            row.attempts += 1
            row.last_error = str(exc)[:500]
            row.available_at = datetime.now(UTC) + timedelta(
                seconds=min(30, 2 ** min(row.attempts, 5))
            )
            logger.warning("outbox publish failed", exc_info=True)
    return True


def outbox_loop() -> None:
    while True:
        try:
            if not dispatch_next():
                time.sleep(0.25)
        except Exception:
            logger.exception("dispatcher cycle failed; retrying")
            time.sleep(1)


def connect() -> None:
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.effective_mqtt_password)
    settings.configure_mqtt_client(client)
    while True:
        try:
            # dispatcher 是纯发布方：显式 clean session（无 broker 侧离线队列）
            client.connect(
                settings.mqtt_host,
                settings.mqtt_port,
                keepalive=30,
                clean_start=settings.mqtt_clean_start,
                properties=settings.mqtt_connect_properties(),
            )
            client.loop_start()
            for _ in range(50):
                if client.is_connected():
                    return
                time.sleep(0.1)
        except Exception:
            logger.exception("dispatcher MQTT connection failed")
        time.sleep(2)


def main() -> None:
    connect()
    threading.Thread(target=manual_loop, daemon=True).start()
    threading.Thread(target=safety_loop, daemon=True).start()
    outbox_loop()


if __name__ == "__main__":
    main()
