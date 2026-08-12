from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

import paho.mqtt.client as mqtt
from app.core.config import get_settings
from app.core.events import get_redis
from app.core.logging import configure_logging
from app.db.models import Map, MapVersion, Robot, RobotExternalAlias, Site
from app.db.session import SessionLocal
from sqlalchemy import select

from services.protocol import validate_message

settings = get_settings()
configure_logging("ros-compat-adapter")
logger = logging.getLogger("ros-compat-adapter")
redis = get_redis()

ENABLED = os.getenv("ROS_COMPAT_MODE", "false").lower() == "true"
INTERNAL_ID = os.getenv("SINGLE_ROBOT_INTERNAL_ID", "R001")
EXPECTED_EXTERNAL_ID = os.getenv("ROS_COMPAT_EXPECTED_EXTERNAL_ID", "").strip()
RAW_MAX_BYTES = int(os.getenv("ROS_COMPAT_RAW_MAX_BYTES", "16384"))
RAW_RETENTION_SECONDS = int(os.getenv("ROS_COMPAT_RAW_RETENTION_SECONDS", "900"))
MASK = re.compile(r"(?i)(token|password|secret|authorization)[\"']?\s*[:=]\s*[\"']?[^,}\s]+")


class Adapter:
    def __init__(self) -> None:
        self.boot_id = str(uuid4())
        self.seq = 0
        self.lock = threading.Lock()
        self.latest: dict[str, object] = {}
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"firebot-ros-compat-{self.boot_id[:8]}",
            protocol=mqtt.MQTTv5,
        )
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.effective_mqtt_password)
        settings.configure_mqtt_client(self.client)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            logger.error("ROS compatibility MQTT connect failed: %s", reason_code)
            return
        for suffix in ("status", "battery", "pose", "odom", "heartbeat"):
            client.subscribe(f"robot/+/{suffix}", qos=1)
        logger.info("ROS compatibility adapter connected in read-only normalization mode")

    @staticmethod
    def external_id(topic: str) -> str | None:
        parts = topic.split("/")
        return parts[1] if len(parts) == 3 and parts[0] == "robot" else None

    def binding(self, external_id: str) -> bool:
        if EXPECTED_EXTERNAL_ID and external_id != EXPECTED_EXTERNAL_ID:
            self.discovery(external_id, "EXPECTED_ID_MISMATCH")
            return False
        with SessionLocal() as db:
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == INTERNAL_ID))
            alias = db.scalar(
                select(RobotExternalAlias).where(
                    RobotExternalAlias.external_id == external_id,
                    RobotExternalAlias.state == "CONFIRMED",
                )
            )
            if not robot:
                logger.error("internal robot %s does not exist", INTERNAL_ID)
                return False
            if EXPECTED_EXTERNAL_ID:
                # An explicit deploy-owner binding is deterministic and needs no
                # first-seen behavior; API confirmation remains recommended.
                return not alias or alias.robot_id == robot.id
            if alias and alias.robot_id == robot.id:
                return True
        self.discovery(external_id, "AWAITING_ADMIN_CONFIRMATION")
        return False

    def discovery(self, external_id: str, state: str) -> None:
        now = datetime.now(UTC).isoformat()
        key = f"ros_compat:discovery:{external_id}"
        previous = redis.get(key)
        data = (
            json.loads(previous) if previous else {"external_id": external_id, "first_seen_at": now}
        )
        data.update({"state": state, "last_seen_at": now, "internal_candidate": INTERNAL_ID})
        redis.setex(key, 86400, json.dumps(data, ensure_ascii=False))

    def capture_raw(self, topic: str, payload: bytes) -> None:
        text = payload[:RAW_MAX_BYTES].decode("utf-8", errors="replace")
        text = MASK.sub(lambda match: f"{match.group(1)}=***", text)
        redis.xadd(
            "firebot:ros_compat:raw",
            {"topic": topic, "payload": text, "received_at": datetime.now(UTC).isoformat()},
            maxlen=1000,
            approximate=True,
        )
        redis.expire("firebot:ros_compat:raw", RAW_RETENTION_SECONDS)
        cutoff_ms = int((time.time() - RAW_RETENTION_SECONDS) * 1000)
        redis.xtrim("firebot:ros_compat:raw", minid=f"{cutoff_ms}-0", approximate=True)

    @staticmethod
    def timestamp(payload: dict) -> datetime:
        value = payload.get("timestamp", payload.get("ts"))
        if isinstance(value, float | int):
            return datetime.fromtimestamp(float(value), UTC)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                pass
        return datetime.now(UTC)

    def base(self, kind: str, source_time: datetime) -> dict:
        return {
            "schema_version": "1.2",
            "message_id": str(uuid4()),
            "type": kind,
            "vehicle_id": INTERNAL_ID,
            "boot_id": self.boot_id,
            "timestamp": source_time.isoformat(),
            "seq": self.next_seq(),
        }

    def map_facts(self) -> tuple[str, str, str, str]:
        with SessionLocal() as db:
            robot = db.scalar(select(Robot).where(Robot.vehicle_id == INTERNAL_ID))
            map_row = db.get(Map, robot.current_map_id) if robot and robot.current_map_id else None
            version = (
                db.scalar(
                    select(MapVersion).where(
                        MapVersion.map_id == map_row.id,
                        MapVersion.version == robot.current_map_version,
                    )
                )
                if map_row and robot and robot.current_map_version
                else None
            )
            site = db.get(Site, robot.site_id) if robot else None
            site_code = site.code if site else "DEFAULT_SITE"
            return (
                site_code,
                map_row.code if map_row else "default_map",
                version.version if version else "1",
                version.checksum if version and version.checksum else "UNKNOWN00",
            )

    def normalize(self, suffix: str, payload: dict) -> list[tuple[str, dict]]:
        source_time = self.timestamp(payload)
        if suffix == "pose":
            x = payload.get("x", payload.get("position", {}).get("x"))
            y = payload.get("y", payload.get("position", {}).get("y"))
            yaw = payload.get("yaw", payload.get("theta", payload.get("position", {}).get("theta")))
            if x is None or y is None or yaw is None:
                raise ValueError("pose requires x, y and yaw/theta")
            self.latest.update({"x": float(x), "y": float(y), "theta": float(yaw)})
            site, map_code, version, checksum = self.map_facts()
            message = {
                **self.base("location", source_time),
                "position": {"x": float(x), "y": float(y), "theta": float(yaw)},
                "site_code": site,
                "map_code": map_code,
                "map_version": version,
                "map_checksum": checksum,
                "frame_id": "map",
                "localization_status": str(payload.get("localization_status", "UNVERIFIED")),
            }
            if "linear_speed" in self.latest:
                message["linear_speed"] = self.latest["linear_speed"]
            if "angular_speed" in self.latest:
                message["angular_speed"] = self.latest["angular_speed"]
            if "battery" in self.latest:
                message["battery"] = self.latest["battery"]
            return [("location", message)]
        if suffix == "odom":
            linear = payload.get("vx", payload.get("linear_speed"))
            angular = payload.get("wz", payload.get("angular_speed"))
            if linear is not None:
                self.latest["linear_speed"] = float(linear)
            if angular is not None:
                self.latest["angular_speed"] = float(angular)
            return [
                (
                    "compat_odom",
                    {
                        "internal_contract": "ros-compat-v1",
                        "vehicle_id": INTERNAL_ID,
                        "timestamp": source_time.isoformat(),
                        "linear_speed": self.latest.get("linear_speed"),
                        "angular_speed": self.latest.get("angular_speed"),
                    },
                )
            ]
        if suffix == "battery":
            value = payload.get("percentage", payload.get("battery"))
            if value is None:
                raise ValueError("battery requires percentage")
            self.latest["battery"] = min(100.0, max(0.0, float(value)))
            return [
                (
                    "compat_battery",
                    {
                        "internal_contract": "ros-compat-v1",
                        "vehicle_id": INTERNAL_ID,
                        "timestamp": source_time.isoformat(),
                        "battery": self.latest["battery"],
                    },
                )
            ]
        if suffix == "status":
            raw = str(payload.get("control_mode_str", payload.get("mode", "IDLE"))).upper()
            mode = "MANUAL" if raw in {"MANUAL", "1"} else "IDLE"
            message = {
                "internal_contract": "ros-compat-v1",
                "vehicle_id": INTERNAL_ID,
                "timestamp": source_time.isoformat(),
                "mode": mode,
            }
            return [("compat_status", message)]
        if suffix == "heartbeat":
            return [
                (
                    "heartbeat",
                    {
                        **self.base("heartbeat", source_time),
                        "uptime_seconds": max(
                            0.0, float(payload.get("uptime_sec", payload.get("uptime_seconds", 0)))
                        ),
                    },
                )
            ]
        return []

    @staticmethod
    def is_canonical(payload: object) -> bool:
        return isinstance(payload, dict) and "schema_version" in payload

    def on_message(self, client, userdata, message) -> None:
        if not ENABLED:
            return
        external_id = self.external_id(message.topic)
        if not external_id:
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.capture_raw(message.topic, message.payload)
            logger.warning("invalid ROS native JSON on %s", message.topic)
            return
        if not isinstance(payload, dict) or self.is_canonical(payload):
            # Canonical MQTT traffic is owned by mqtt-ingress and must never be
            # normalized a second time or copied into the ROS-native raw stream.
            return
        self.capture_raw(message.topic, message.payload)
        if not self.binding(external_id):
            return
        suffix = message.topic.rsplit("/", 1)[-1]
        try:
            messages = self.normalize(suffix, payload)
            for kind, normalized in messages:
                if not kind.startswith("compat_"):
                    validate_message(normalized)
                client.publish(
                    f"_platform/compat/{INTERNAL_ID}/{kind}",
                    json.dumps(normalized, ensure_ascii=False),
                    qos=1 if kind != "location" else 0,
                    retain=False,
                )
        except Exception:
            logger.warning("ROS native payload rejected on %s", message.topic, exc_info=True)


def main() -> None:
    if not ENABLED:
        logger.info("ROS compatibility mode is disabled")
        while True:
            redis.setex("service:ros-compat-adapter:heartbeat", 5, "DISABLED")
            time.sleep(2)
    adapter = Adapter()

    def heartbeat() -> None:
        while True:
            redis.setex("service:ros-compat-adapter:heartbeat", 5, datetime.now(UTC).isoformat())
            time.sleep(1)

    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        try:
            adapter.client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30)
            adapter.client.loop_forever(retry_first_connection=True)
        except Exception:
            logger.exception("ROS compatibility adapter loop failed")
            time.sleep(3)


if __name__ == "__main__":
    main()
