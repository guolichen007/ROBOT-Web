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

# G2 逐车 Broker 路由：本进程是唯一 Outbox 认领者（FOR UPDATE SKIP LOCKED 不变），
# 按车辆 transport_profile 选择 MQTT 客户端。default=原 Broker（上方 client，行为不变）；
# 其余 profile 各自一个独立客户端，使用该 Broker 独立签发的 platform 凭证
# （DISPATCHER_BROKER_PROFILES_FILE，口令只以文件路径给出）。未配置客户端的 profile
# 一律拒绝派发，绝不回退到原 Broker。
profile_clients: dict[str, mqtt.Client] = {}
profile_specs: dict[str, dict] = {}


def load_broker_profiles() -> None:
    from app.core.vehicle_routing import DEFAULT_PROFILE, known_profiles, validate_configuration

    validate_configuration()
    path = settings.dispatcher_broker_profiles_file
    if not path:
        return
    with open(path, encoding="utf-8") as handle:
        specs = json.load(handle)
    for name, spec in specs.items():
        if name == DEFAULT_PROFILE or name not in known_profiles():
            raise ValueError(f"broker profile {name!r} not declared in TRANSPORT_PROFILE_ADAPTERS")
        if spec.get("password_file") is None or "password" in spec:
            raise ValueError("broker profile credentials must be given as password_file only")
        if spec.get("password_file") == settings.mqtt_password_file:
            raise ValueError("independent broker must not reuse the default platform credential")
        profile_specs[name] = spec
        profile_clients[name] = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{INSTANCE_ID}-{name}",
            protocol=mqtt.MQTTv5,
        )


def client_for(profile: str) -> mqtt.Client:
    from app.core.vehicle_routing import DEFAULT_PROFILE, RoutingError

    if profile == DEFAULT_PROFILE:
        return client
    try:
        return profile_clients[profile]
    except KeyError as exc:
        raise RoutingError(
            f"no dispatcher broker client for transport_profile {profile!r}"
        ) from exc


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
    from app.core.vehicle_routing import (
        DEFAULT_PROFILE,
        RoutingError,
        adapter_instance,
        execution_mode,
        transport_profile,
    )

    # 未知 profile → RoutingError（fail-closed，进入退避重试并记录 last_error，不回退）
    profile = transport_profile(payload["vehicle_id"])
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == payload["vehicle_id"]))
        if robot is None:
            raise ValueError("unknown command vehicle")
        if profile != DEFAULT_PROFILE and robot.agent_protocol != "VEHICLE_AGENT_V1":
            raise RoutingError("independent transport_profile only carries vehicle-agent-v1")
        if robot.agent_protocol == "VEHICLE_AGENT_V1":
            from app.core.vehicle_agent_health import adapter_healthy

            instance = adapter_instance(profile)
            if not adapter_healthy(instance=instance):
                raise ValueError("v1 adapter unavailable before dispatch")
            from app.core.vehicle_routing import adapter_config_consistent

            if not adapter_config_consistent(redis, instance):
                raise RoutingError("dedicated adapter routing config differs from dispatcher")
            # 执行模式复核：API 写入的 execution_mode 必须与当前逐车执行档一致
            if (payload.get("execution_mode") or "REAL") != execution_mode(
                robot.vehicle_id, payload["cmd"]
            ):
                raise RoutingError("execution_mode no longer permitted for vehicle")
            if not settings.vehicle_agent_v1_downlink_enabled:
                raise ValueError("v1 downlink disabled")
            if not settings.vehicle_downlink_allowed(robot.vehicle_id):
                raise ValueError("vehicle not in v1 downlink allowlist")
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
    target = client_for(profile)
    if profile != DEFAULT_PROFILE and not target.is_connected():
        # 独立 Broker 未连接：拒绝（退避重试），绝不改走默认 Broker
        raise RuntimeError(f"dispatcher broker client {profile!r} not connected")
    info = target.publish(
        destination,
        json.dumps(payload, ensure_ascii=False),
        qos=qos,
        retain=False,
        properties=mqtt_message_expiry_properties(expiry_interval),
    )
    info.wait_for_publish(timeout=3)
    if not info.is_published():
        raise RuntimeError("MQTT publish did not complete")
    # 注意：is_published 仅表示目标 Broker 已 PUBACK（Broker 收到），不代表车端收到或执行。


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
    for name, extra in profile_clients.items():
        if extra.is_connected():
            redis.setex(
                f"service:command-dispatcher:broker-heartbeat:{name}",
                5,
                datetime.now(UTC).isoformat(),
            )
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


def connect_profile(name: str, extra: mqtt.Client) -> None:
    """独立 Broker 客户端：异步连接 + paho 有界重连；连接失败只影响该 profile 的命令。"""
    spec = profile_specs[name]
    with open(spec["password_file"], encoding="utf-8") as handle:
        password = handle.read().strip()
    if not password or password == settings.effective_mqtt_password:
        raise ValueError("independent broker must use its own platform credential")
    extra.username_pw_set(spec["username"], password)
    extra.reconnect_delay_set(
        min_delay=settings.mqtt_reconnect_min_delay, max_delay=settings.mqtt_reconnect_max_delay
    )
    if spec.get("tls", True):
        import ssl

        extra.tls_set(ca_certs=spec["ca_file"], cert_reqs=ssl.CERT_REQUIRED)
        extra.tls_insecure_set(False)
    extra.connect_async(
        spec["host"],
        int(spec.get("port", 8883)),
        keepalive=30,
        clean_start=True,
        properties=settings.mqtt_connect_properties(),
    )
    extra.loop_start()


def main() -> None:
    load_broker_profiles()
    for name, extra in profile_clients.items():
        connect_profile(name, extra)
    connect()
    threading.Thread(target=manual_loop, daemon=True).start()
    threading.Thread(target=safety_loop, daemon=True).start()
    outbox_loop()


if __name__ == "__main__":
    main()
