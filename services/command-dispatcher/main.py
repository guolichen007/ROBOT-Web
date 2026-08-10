from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import paho.mqtt.client as mqtt
from app.core.config import get_settings
from app.core.events import append_event, get_redis
from app.core.logging import configure_logging
from app.core.serialization import serialize_model
from app.db.models import Command, OutboxEvent
from app.db.session import SessionLocal
from redis.exceptions import ResponseError
from sqlalchemy import select

from services.protocol import validate_message

settings = get_settings()
configure_logging("command-dispatcher")
logger = logging.getLogger("command-dispatcher")
redis = get_redis()
client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2, client_id="firebot-command-dispatcher", protocol=mqtt.MQTTv5
)


def publish_command(payload: dict, qos: int) -> None:
    validate_message(payload)
    expires = datetime.fromisoformat(payload["expires_at"])
    if expires <= datetime.now(UTC):
        raise TimeoutError("command expired before publish")
    info = client.publish(
        f"robot/{payload['vehicle_id']}/command",
        json.dumps(payload, ensure_ascii=False),
        qos=qos,
        retain=False,
    )
    info.wait_for_publish(timeout=3)
    if not info.is_published():
        raise RuntimeError("MQTT publish did not complete")


def manual_loop() -> None:
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe("firebot:manual_commands")
    for message in pubsub.listen():
        try:
            payload = json.loads(message["data"])
            if datetime.fromisoformat(payload["expires_at"]) <= datetime.now(UTC):
                continue
            publish_command(payload, qos=0)
        except Exception:
            logger.warning("manual pulse dropped", exc_info=True)


def safety_loop() -> None:
    try:
        redis.xgroup_create("firebot:safety_commands", "dispatchers", id="0-0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    while True:
        claimed = redis.xautoclaim(
            "firebot:safety_commands",
            "dispatchers",
            "dispatcher-1",
            min_idle_time=1000,
            start_id="0-0",
            count=10,
        )
        process_safety_events(claimed[1])
        rows = redis.xreadgroup(
            "dispatchers", "dispatcher-1", {"firebot:safety_commands": ">"}, count=10, block=1000
        )
        for _, events in rows:
            process_safety_events(events)


def process_safety_events(events: list) -> None:
    for stream_id, fields in events:
        payload = json.loads(fields["command"])
        with SessionLocal.begin() as db:
            command = db.scalar(select(Command).where(Command.command_id == payload["command_id"]))
            try:
                publish_command(payload, qos=1)
                if command:
                    command.lifecycle_status = "PUBLISHED"
                    command.published_at = datetime.now(UTC)
                    append_event("command.updated", serialize_model(command))
                redis.xack("firebot:safety_commands", "dispatchers", stream_id)
            except TimeoutError:
                if command:
                    command.lifecycle_status = "EXPIRED"
                    command.terminal_at = datetime.now(UTC)
                redis.xack("firebot:safety_commands", "dispatchers", stream_id)
            except Exception as exc:
                logger.warning("safety command publish retry", exc_info=True)
                if command:
                    command.ack_reason = type(exc).__name__


def outbox_loop() -> None:
    while True:
        with SessionLocal.begin() as db:
            row = db.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == "PENDING", OutboxEvent.available_at <= datetime.now(UTC)
                )
                .order_by(OutboxEvent.created_at)
                .with_for_update(skip_locked=True)
            )
            if not row:
                redis.setex(
                    "service:command-dispatcher:heartbeat", 5, datetime.now(UTC).isoformat()
                )
                time.sleep(0.25)
                continue
            command = db.scalar(select(Command).where(Command.command_id == row.aggregate_id))
            try:
                publish_command(row.payload_json, qos=1)
                row.status = "PUBLISHED"
                row.published_at = datetime.now(UTC)
                row.attempts += 1
                if command:
                    command.lifecycle_status = "PUBLISHED"
                    command.published_at = row.published_at
                    append_event("command.updated", serialize_model(command))
            except TimeoutError:
                row.status = "EXPIRED"
                row.last_error = "expired"
                if command:
                    command.lifecycle_status = "EXPIRED"
                    command.terminal_at = datetime.now(UTC)
            except Exception as exc:
                row.attempts += 1
                row.last_error = str(exc)[:500]
                row.available_at = datetime.now(UTC) + timedelta(
                    seconds=min(30, 2 ** min(row.attempts, 5))
                )
                logger.warning("outbox publish failed", exc_info=True)


def connect() -> None:
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.effective_mqtt_password)
    settings.configure_mqtt_client(client)
    while True:
        try:
            client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30)
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
