from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import paho.mqtt.client as mqtt
from app.core.config import get_settings
from app.core.events import get_redis
from app.core.logging import configure_logging
from app.db.models import Robot, RobotExternalAlias
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
        self.external_id_for_normalize = EXPECTED_EXTERNAL_ID or "firerobot-01"
        self.seq = 0
        self.lock = threading.Lock()
        self.latest: dict[str, object] = {}
        self.last_sequences: dict[tuple[str, str], int] = {}
        self.last_source_times: dict[tuple[str, str], datetime] = {}
        self.map_identity: dict[str, str] | None = None
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
        client.subscribe("robot/+/availability", qos=1)
        client.subscribe("robot/+/map", qos=1)
        client.subscribe("robot/+/heartbeat", qos=0)
        client.subscribe("robot/+/pose", qos=1)
        client.subscribe("robot/+/odom", qos=0)
        for suffix in ("status", "battery"):
            client.subscribe(f"robot/+/{suffix}", qos=1)
        client.subscribe("robot/+/nav/status", qos=1)
        client.subscribe("robot/+/nav/result", qos=1)
        logger.info("ROS compatibility adapter connected in read-only normalization mode")

    @staticmethod
    def external_id(topic: str) -> str | None:
        parts = topic.split("/")
        return parts[1] if len(parts) in {3, 4} and parts[0] == "robot" else None

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

    def validate_envelope(
        self, external_id: str, suffix: str, payload: dict, received: datetime
    ) -> datetime:
        if payload.get("compat_schema_version") != "1.1":
            raise ValueError("compat_schema_version must be 1.1")
        if payload.get("external_id") != external_id:
            raise ValueError("external_id must match MQTT topic")
        boot_id = str(payload.get("bridge_boot_id", ""))
        try:
            UUID(boot_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("bridge_boot_id must be UUID") from exc
        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValueError("seq must be a non-negative integer")
        value = payload.get("ts")
        if isinstance(value, float | int):
            source_time = datetime.fromtimestamp(float(value), UTC)
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("ts must be ISO-8601 or Unix seconds") from exc
            if parsed.tzinfo is None:
                raise ValueError("ts must include timezone")
            source_time = parsed.astimezone(UTC)
        else:
            raise ValueError("ts is required")
        max_age = (
            settings.ros_compat_heartbeat_max_age_seconds
            if suffix in {"heartbeat", "availability"}
            else settings.ros_compat_pose_max_age_seconds
        )
        age = (received - source_time).total_seconds()
        if age > max_age:
            raise ValueError("source timestamp is stale")
        if age < -settings.ros_compat_max_future_skew_seconds:
            raise ValueError("source timestamp is in the future")
        sequence_key = (boot_id, suffix)
        if seq <= self.last_sequences.get(sequence_key, -1):
            raise ValueError("duplicate or out-of-order seq")
        prior_time = self.last_source_times.get(sequence_key)
        if prior_time and source_time < prior_time:
            raise ValueError("source timestamp moved backwards")
        self.last_sequences[sequence_key] = seq
        self.last_source_times[sequence_key] = source_time
        return source_time

    def base(self, kind: str, source_time: datetime, payload: dict) -> dict:
        return {
            "schema_version": "1.2",
            "message_id": str(uuid4()),
            "type": kind,
            "vehicle_id": INTERNAL_ID,
            "boot_id": payload["bridge_boot_id"],
            "timestamp": source_time.isoformat(),
            "seq": payload["seq"],
        }

    def map_facts(self) -> tuple[str, str, str, str]:
        """Return only vehicle-reported map facts; never query server defaults."""

        identity = self.map_identity or {}
        return (
            identity.get("site_code", "UNVERIFIED"),
            identity.get("map_code", "UNVERIFIED"),
            identity.get("map_version", "UNVERIFIED"),
            identity.get("map_checksum", "UNVERIFIED"),
        )

    def normalize(
        self,
        suffix: str,
        payload: dict,
        *,
        external_id: str | None = None,
        received: datetime | None = None,
    ) -> list[tuple[str, dict]]:
        compatibility_fixture = external_id is None
        external_id = external_id or str(payload.get("external_id", self.external_id_for_normalize))
        payload = dict(payload)
        if compatibility_fixture:
            payload.setdefault("compat_schema_version", "1.1")
            payload.setdefault("external_id", external_id)
            payload.setdefault("bridge_boot_id", self.boot_id)
            payload.setdefault("seq", self.next_seq())
        if received is None:
            value = payload.get("ts")
            if isinstance(value, float | int):
                received = datetime.fromtimestamp(float(value), UTC)
            elif isinstance(value, str):
                received = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
            else:
                received = datetime.now(UTC)
        source_time = self.validate_envelope(external_id, suffix, payload, received)
        if suffix == "map":
            required = ("site_code", "map_code", "map_version", "map_checksum")
            if any(not payload.get(key) for key in required):
                raise ValueError("map identity requires site/map/version/checksum")
            self.map_identity = {key: str(payload[key]) for key in required}
            return [
                (
                    "compat_map",
                    {
                        "internal_contract": "ros-compat-v1",
                        "vehicle_id": INTERNAL_ID,
                        "external_id": external_id,
                        "bridge_boot_id": payload["bridge_boot_id"],
                        "seq": payload["seq"],
                        "timestamp": source_time.isoformat(),
                        **self.map_identity,
                    },
                )
            ]
        if suffix == "availability":
            state = str(payload.get("state", "")).lower()
            if state not in {"online", "offline"}:
                raise ValueError("availability state must be online/offline")
            return [
                (
                    "compat_availability",
                    {
                        "internal_contract": "ros-compat-v1",
                        "vehicle_id": INTERNAL_ID,
                        "external_id": external_id,
                        "bridge_boot_id": payload["bridge_boot_id"],
                        "seq": payload["seq"],
                        "timestamp": source_time.isoformat(),
                        "state": state,
                        "reason": payload.get("reason"),
                    },
                )
            ]
        if suffix == "pose":
            x = payload.get("x", payload.get("position", {}).get("x"))
            y = payload.get("y", payload.get("position", {}).get("y"))
            yaw = payload.get("yaw", payload.get("theta", payload.get("position", {}).get("theta")))
            if x is None or y is None or yaw is None:
                raise ValueError("pose requires x, y and yaw/theta")
            self.latest.update({"x": float(x), "y": float(y), "theta": float(yaw)})
            frame_id = str(payload.get("frame_id", "map"))
            if frame_id != "map":
                raise ValueError("AMCL global pose requires frame_id=map")
            covariance = payload.get("covariance") or {
                key: payload[key] for key in ("cov_xx", "cov_yy", "cov_yawyaw") if key in payload
            }
            site, map_code, version, checksum = self.map_facts()
            map_verified = all(
                value and value != "UNVERIFIED" for value in (site, map_code, version, checksum)
            )
            message = {
                **self.base("location", source_time, payload),
                "position": {"x": float(x), "y": float(y), "theta": float(yaw)},
                "site_code": site,
                "map_code": map_code,
                "map_version": version,
                "map_checksum": checksum,
                "frame_id": frame_id,
                "localization_status": (
                    str(payload.get("localization_status", "VALID_SOURCE"))
                    if map_verified
                    else "DEGRADED_MAP_UNVERIFIED"
                ),
            }
            if covariance:
                self.latest["amcl_covariance"] = covariance
            if "planar_speed" in self.latest:
                message["linear_speed"] = self.latest["planar_speed"]
            if "angular_z" in self.latest:
                message["angular_speed"] = self.latest["angular_z"]
            if "battery" in self.latest:
                message["battery"] = self.latest["battery"]
            return [("location", message)]
        if suffix == "odom":
            linear_x = payload.get("vx", payload.get("linear_x", payload.get("linear_speed")))
            linear_y = payload.get("vy", payload.get("linear_y", 0.0))
            angular_z = payload.get("wz", payload.get("angular_z", payload.get("angular_speed")))
            if linear_x is None or angular_z is None:
                raise ValueError("odom requires vx/linear_x and wz/angular_z")
            linear_x = float(linear_x)
            linear_y = float(linear_y)
            angular_z = float(angular_z)
            planar_speed = math.hypot(linear_x, linear_y)
            self.latest.update(
                {
                    "linear_x": linear_x,
                    "linear_y": linear_y,
                    "angular_z": angular_z,
                    "planar_speed": planar_speed,
                }
            )
            return [
                (
                    "compat_odom",
                    {
                        "internal_contract": "ros-compat-v1",
                        "vehicle_id": INTERNAL_ID,
                        "bridge_boot_id": payload["bridge_boot_id"],
                        "seq": payload["seq"],
                        "timestamp": source_time.isoformat(),
                        "linear_x": linear_x,
                        "linear_y": linear_y,
                        "angular_z": angular_z,
                        "planar_speed": planar_speed,
                    },
                )
            ]
        if suffix == "battery":
            value = payload.get(
                "battery_percentage", payload.get("percentage", payload.get("battery"))
            )
            if value is None:
                raise ValueError("battery requires percentage")
            self.latest["battery"] = min(100.0, max(0.0, float(value)))
            diagnostics = {
                key: payload[key]
                for key in (
                    "battery_voltage",
                    "battery_current",
                    "battery_temperature",
                    "battery_capacity",
                    "battery_charge_state",
                    "battery_cycle_count",
                )
                if key in payload
            }
            return [
                (
                    "compat_battery",
                    {
                        "internal_contract": "ros-compat-v1",
                        "vehicle_id": INTERNAL_ID,
                        "bridge_boot_id": payload["bridge_boot_id"],
                        "seq": payload["seq"],
                        "timestamp": source_time.isoformat(),
                        "battery": self.latest["battery"],
                        "diagnostics": diagnostics,
                    },
                )
            ]
        if suffix == "status":
            raw_value = payload.get(
                "control_mode",
                payload.get("control_mode_str", payload.get("mode")),
            )
            raw = str(raw_value if raw_value is not None else "UNKNOWN").upper()
            control_mode = int(raw) if raw.isdigit() else ({"MANUAL": 1, "ROS": 3}.get(raw))
            # ROS control mode is a transport/controller fact, not a Firebot patrol state.
            mode = "MANUAL" if control_mode == 1 else "IDLE"
            message = {
                "internal_contract": "ros-compat-v1",
                "vehicle_id": INTERNAL_ID,
                "bridge_boot_id": payload["bridge_boot_id"],
                "seq": payload["seq"],
                "timestamp": source_time.isoformat(),
                "mode": mode,
                "ros_control_mode": control_mode,
            }
            return [("compat_status", message)]
        if suffix == "heartbeat":
            return [
                (
                    "heartbeat",
                    {
                        **self.base("heartbeat", source_time, payload),
                        "uptime_seconds": max(
                            0.0, float(payload.get("uptime_sec", payload.get("uptime_seconds", 0)))
                        ),
                    },
                )
            ]
        if suffix in {"nav_status", "nav_result"}:
            # Native move_base diagnostics have no Firebot task/command correlation.
            # They are intentionally kept diagnostic-only and never translated into
            # canonical task_status.
            return [
                (
                    f"compat_{suffix}",
                    {
                        "internal_contract": "ros-compat-v1",
                        "vehicle_id": INTERNAL_ID,
                        "bridge_boot_id": payload["bridge_boot_id"],
                        "seq": payload["seq"],
                        "timestamp": source_time.isoformat(),
                        "external_goal_id": payload.get("goal_id"),
                        "status": str(payload.get("status", payload.get("state", "UNKNOWN"))),
                        "payload": payload,
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
        parts = message.topic.split("/")
        suffix = f"nav_{parts[-1]}" if len(parts) == 4 and parts[-2] == "nav" else parts[-1]
        try:
            messages = self.normalize(
                suffix, payload, external_id=external_id, received=datetime.now(UTC)
            )
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
