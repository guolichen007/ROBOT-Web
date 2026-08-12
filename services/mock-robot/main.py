from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

import paho.mqtt.client as mqtt
from app.core.config import get_settings
from app.core.logging import configure_logging
from jsonschema import ValidationError

from services.protocol import validate_message

settings = get_settings()
configure_logging("mock-robot")
logger = logging.getLogger("mock-robot")

SUPPORTED_COMMANDS = {
    "manual_control",
    "stop_motion",
    "emergency_stop",
    "reset_estop",
    "return_dock",
    "patrol",
    "extinguish",
    "cancel_task",
}


class MockRobot:
    def __init__(self) -> None:
        self.vehicle_id = os.getenv("MOCK_VEHICLE_ID", "R001")
        self.site_code = os.getenv("MOCK_SITE_CODE", "DEMO_PARKING")
        self.map_code = os.getenv("MOCK_MAP_CODE", "parking_v1")
        self.map_version = os.getenv("MOCK_MAP_VERSION", "1")
        self.map_checksum = os.getenv("MOCK_MAP_CHECKSUM", "demo-map-v1")
        self.boot_id = str(uuid4())
        self.seq = 0
        self.x, self.y, self.theta = 39.0, 2.0, math.pi / 2
        self.linear = 0.0
        self.angular = 0.0
        self.battery = 96.0
        self.mode = "IDLE"
        self.estop = False
        self.active_task_id: str | None = None
        self.last_manual = 0.0
        self.started = time.monotonic()
        self.processed: dict[str, tuple[dict, list[dict]]] = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.packet_loss = float(os.getenv("MOCK_PACKET_LOSS", "0"))
        self.delay_ms = int(os.getenv("MOCK_PACKET_DELAY_MS", "0"))
        self.fire_after = int(os.getenv("MOCK_FIRE_AFTER_SECONDS", "25"))
        self.fire_sent = False
        self.timestamp_skew_seconds = float(os.getenv("MOCK_TIMESTAMP_SKEW_SECONDS", "0"))
        self.duplicate_every = int(os.getenv("MOCK_DUPLICATE_EVERY", "0"))
        self.out_of_order_every = int(os.getenv("MOCK_OUT_OF_ORDER_EVERY", "0"))
        self.invalid_schema_every = int(os.getenv("MOCK_INVALID_SCHEMA_EVERY", "0"))
        self.bad_json_every = int(os.getenv("MOCK_BAD_JSON_EVERY", "0"))
        self.reboot_after = int(os.getenv("MOCK_REBOOT_AFTER_SECONDS", "0"))
        self.offline_after = int(os.getenv("MOCK_OFFLINE_AFTER_SECONDS", "0"))
        self.duplicate_ack = os.getenv("MOCK_DUPLICATE_ACK", "false").lower() == "true"
        self.wrong_ack = os.getenv("MOCK_WRONG_COMMAND_ID_ACK", "false").lower() == "true"
        unsupported = {
            item.strip()
            for item in os.getenv("MOCK_UNSUPPORTED_COMMANDS", "").split(",")
            if item.strip()
        }
        self.supported_commands = SUPPORTED_COMMANDS - unsupported
        self.publish_count = 0
        self.rebooted = False
        self.offline_sent = False
        self.patrol_route = [
            *[(39.0, 2.0 + index * 0.55) for index in range(49)],
            *[(39.0 - index * 0.6, 28.4) for index in range(62)],
        ]
        self.route_index = 0
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"mock-{self.vehicle_id}-{self.boot_id[:8]}",
            protocol=mqtt.MQTTv5,
        )
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.effective_mqtt_password)
        settings.configure_mqtt_client(self.client)
        lwt = self.message("availability", state="offline", reason="LWT")
        self.client.will_set(self.topic("availability"), json.dumps(lwt), qos=1, retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def message(self, message_type: str, **payload) -> dict:
        return {
            "schema_version": "1.2",
            "message_id": str(uuid4()),
            "type": message_type,
            "vehicle_id": self.vehicle_id,
            "boot_id": self.boot_id,
            "timestamp": datetime.fromtimestamp(
                datetime.now(UTC).timestamp() + self.timestamp_skew_seconds, UTC
            ).isoformat(),
            "seq": self.next_seq(),
            **payload,
        }

    def topic(self, name: str) -> str:
        return f"robot/{self.vehicle_id}/{name}"

    def publish(
        self, name: str, payload: dict, qos: int, retain: bool = False, allow_loss: bool = False
    ) -> None:
        if allow_loss and random.random() < self.packet_loss:
            return
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000)
        topic = self.topic(name)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, encoded, qos=qos, retain=retain)
        if name not in {"availability", "capabilities"}:
            self.publish_count += 1
            if self.duplicate_every and self.publish_count % self.duplicate_every == 0:
                self.client.publish(topic, encoded, qos=qos, retain=False)
            if self.out_of_order_every and self.publish_count % self.out_of_order_every == 0:
                old = {**payload, "message_id": str(uuid4()), "seq": max(0, payload["seq"] - 2)}
                self.client.publish(topic, json.dumps(old), qos=qos, retain=False)
            if self.invalid_schema_every and self.publish_count % self.invalid_schema_every == 0:
                invalid = {**payload, "message_id": str(uuid4()), "schema_version": "9.9"}
                self.client.publish(topic, json.dumps(invalid), qos=qos, retain=False)
            if self.bad_json_every and self.publish_count % self.bad_json_every == 0:
                self.client.publish(topic, "{bad-json", qos=qos, retain=False)

    def on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            logger.error("Mock MQTT connect failed: %s", reason_code)
            return
        client.subscribe(self.topic("command"), qos=1)
        self.publish(
            "availability",
            self.message("availability", state="online", reason="CONNECTED"),
            1,
            True,
        )
        self.publish(
            "capabilities",
            self.message(
                "capabilities",
                protocol_version="1.2.0",
                supported_commands=sorted(self.supported_commands),
                sensors=["smoke", "bottom_ir", "top_ir"],
                media=["roof_rgb", "roof_thermal", "bottom_ir"],
            ),
            1,
            True,
        )
        logger.info("Mock R001 connected through MQTT")

    def ack(self, command: dict, status: str, reason_code: str | None = None) -> dict:
        payload = self.message(
            "command_ack",
            command_id=command["command_id"],
            task_id=command.get("task_id"),
            status=status,
            reason_code=reason_code,
            reason=None,
        )
        late_ms = int(os.getenv("MOCK_LATE_ACK_MS", "0"))
        if late_ms:
            time.sleep(late_ms / 1000)
        self.publish("command_ack", payload, 1)
        if self.wrong_ack:
            wrong = {**payload, "message_id": str(uuid4()), "command_id": str(uuid4())}
            self.publish("command_ack", wrong, 1)
        if self.duplicate_ack:
            duplicate = {**payload, "message_id": str(uuid4()), "seq": self.next_seq()}
            self.publish("command_ack", duplicate, 1)
        return payload

    def task_status(self, task_id: str, status: str, phase: str, progress: float) -> dict:
        payload = self.message(
            "task_status",
            task_id=task_id,
            status=status,
            phase=phase,
            progress=progress,
            failure_code=None,
            failure_message=None,
        )
        self.publish("task_status", payload, 1)
        return payload

    def simulate_task(self, command: dict) -> None:
        task_id = command.get("task_id") or command.get("params", {}).get("task_id")
        if not task_id:
            return
        with self.lock:
            self.active_task_id = task_id
            self.mode = (
                "EXTINGUISH"
                if command["cmd"] == "extinguish"
                else "PATROL"
                if command["cmd"] == "patrol"
                else "RETURN_DOCK"
            )
        params = command.get("params", {})
        preset_navigation = params.get("mission_kind") == "NAVIGATE_TO_PRESET"
        target_pose = params.get("target_pose", {}) if preset_navigation else {}
        emitted = [self.task_status(task_id, "accepted", "ACCEPTED", 0)]
        for progress in (10, 25, 45, 65, 85):
            if self.stop_event.wait(0.6):
                return
            with self.lock:
                if self.estop or self.active_task_id != task_id:
                    return
                if command["cmd"] == "patrol" and (
                    preset_navigation or self.route_index < len(self.patrol_route)
                ):
                    target_x, target_y = (
                        (float(target_pose["x"]), float(target_pose["y"]))
                        if preset_navigation
                        else self.patrol_route[self.route_index]
                    )
                    desired = math.atan2(target_y - self.y, target_x - self.x)
                    delta = math.atan2(
                        math.sin(desired - self.theta), math.cos(desired - self.theta)
                    )
                    self.angular = max(-0.6, min(0.6, delta * 1.8))
                    self.linear = 0.24 if abs(delta) < 0.4 else 0.08
                    if (
                        not preset_navigation
                        and math.hypot(target_x - self.x, target_y - self.y) < 0.45
                    ):
                        self.route_index = (self.route_index + 1) % len(self.patrol_route)
                else:
                    self.linear = 0.18
                    self.angular = 0.08 * math.sin(progress)
            emitted.append(self.task_status(task_id, "executing", "NAVIGATING", progress))
        if command["cmd"] == "extinguish":
            emitted.append(self.task_status(task_id, "executing", "EXTINGUISHING", 95))
            time.sleep(0.8)
        if preset_navigation:
            with self.lock:
                self.x = float(target_pose["x"])
                self.y = float(target_pose["y"])
                self.theta = float(target_pose.get("theta", self.theta))
                self.linear = self.angular = 0
        emitted.append(self.task_status(task_id, "completed", "COMPLETED", 100))
        with self.lock:
            self.active_task_id = None
            self.mode = "IDLE"
            self.linear = self.angular = 0
        cached = self.processed.get(command["command_id"])
        if cached:
            self.processed[command["command_id"]] = (cached[0], emitted)

    def execute_command(self, command: dict) -> None:
        if command["command_id"] in self.processed:
            ack, statuses = self.processed[command["command_id"]]
            self.publish("command_ack", ack, 1)
            for status in statuses[-1:]:
                self.publish("task_status", status, 1)
            return
        if datetime.fromisoformat(command["expires_at"]) <= datetime.now(UTC):
            ack = self.ack(command, "rejected", "COMMAND_EXPIRED")
            self.processed[command["command_id"]] = (ack, [])
            return
        cmd = command["cmd"]
        if cmd != "emergency_stop" and command.get("target_boot_id") != self.boot_id:
            ack = self.ack(command, "rejected", "ROBOT_BOOT_SESSION_UNKNOWN")
            self.processed[command["command_id"]] = (ack, [])
            return
        if cmd == "emergency_stop" and command.get("target_boot_id") not in {None, self.boot_id}:
            ack = self.ack(command, "rejected", "ROBOT_BOOT_SESSION_UNKNOWN")
            self.processed[command["command_id"]] = (ack, [])
            return
        if cmd not in self.supported_commands:
            ack = self.ack(command, "unsupported", "COMMAND_UNSUPPORTED")
            self.processed[command["command_id"]] = (ack, [])
            return
        if cmd == "manual_control":
            with self.lock:
                if self.estop:
                    ack = self.ack(command, "rejected", "ROBOT_ESTOP_ACTIVE")
                    self.processed[command["command_id"]] = (ack, [])
                    return
                self.linear = float(command["params"].get("linear_x", 0))
                self.angular = float(command["params"].get("angular_z", 0))
                self.last_manual = time.monotonic()
                self.mode = "MANUAL"
            return
        if cmd == "stop_motion":
            with self.lock:
                self.linear = self.angular = 0
                self.mode = "ESTOP" if self.estop else "IDLE"
            ack = self.ack(command, "accepted")
            self.processed[command["command_id"]] = (ack, [])
            return
        if cmd == "emergency_stop":
            with self.lock:
                self.estop = True
                self.linear = self.angular = 0
                self.mode = "ESTOP"
                self.active_task_id = None
            ack = self.ack(command, "accepted")
            self.processed[command["command_id"]] = (ack, [])
            return
        if cmd == "reset_estop":
            with self.lock:
                self.estop = False
                self.mode = "IDLE"
            ack = self.ack(command, "accepted")
            self.processed[command["command_id"]] = (ack, [])
            return
        if self.estop:
            ack = self.ack(command, "rejected", "ROBOT_ESTOP_ACTIVE")
            self.processed[command["command_id"]] = (ack, [])
            return
        if cmd == "cancel_task":
            task_id = command.get("task_id") or command.get("params", {}).get("task_id")
            with self.lock:
                self.active_task_id = None
                self.linear = self.angular = 0
                self.mode = "IDLE"
            ack = self.ack(command, "accepted")
            statuses = [self.task_status(task_id, "cancelled", "CANCELLED", 0)] if task_id else []
            self.processed[command["command_id"]] = (ack, statuses)
            return
        if cmd in {"patrol", "extinguish", "return_dock"}:
            if self.active_task_id:
                ack = self.ack(command, "rejected", "ACTIVE_TASK_CONFLICT")
                self.processed[command["command_id"]] = (ack, [])
                return
            ack = self.ack(command, "accepted")
            self.processed[command["command_id"]] = (ack, [])
            threading.Thread(target=self.simulate_task, args=(command,), daemon=True).start()
            return
        ack = self.ack(command, "unsupported", "COMMAND_UNSUPPORTED")
        self.processed[command["command_id"]] = (ack, [])

    def on_message(self, client, userdata, message) -> None:
        try:
            command = json.loads(message.payload.decode("utf-8"))
            validate_message(command)
            if command["type"] != "command" or command["vehicle_id"] != self.vehicle_id:
                return
            self.execute_command(command)
        except (ValueError, ValidationError):
            logger.warning("Mock rejected invalid command", exc_info=True)

    def motion_loop(self) -> None:
        last = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            dt = now - last
            last = now
            with self.lock:
                if self.mode == "MANUAL" and now - self.last_manual > 0.5:
                    self.linear = self.angular = 0
                    self.mode = "IDLE"
                self.theta += self.angular * dt
                self.x = max(0.5, min(47.5, self.x + math.cos(self.theta) * self.linear * dt))
                self.y = max(0.5, min(33.5, self.y + math.sin(self.theta) * self.linear * dt))
                self.battery = max(5, self.battery - 0.0003)
                location = self.message(
                    "location",
                    position={"x": self.x, "y": self.y, "theta": self.theta},
                    linear_speed=self.linear,
                    angular_speed=self.angular,
                    battery=self.battery,
                    site_code=self.site_code,
                    map_code=self.map_code,
                    map_version=self.map_version,
                    map_checksum=self.map_checksum,
                    frame_id="map",
                    parking_slot_code=f"A-{max(1, min(54, int(self.y / 2.8) + 19)):02d}",
                    localization_status="OK",
                )
            self.publish("location", location, 0, allow_loss=True)
            time.sleep(0.1)

    def periodic_loop(self) -> None:
        status_tick = sensor_tick = heartbeat_tick = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            uptime = now - self.started
            if self.reboot_after > 0 and not self.rebooted and uptime >= self.reboot_after:
                self.rebooted = True
                with self.lock:
                    self.boot_id = str(uuid4())
                    self.seq = 0
                    self.active_task_id = None
                    self.linear = self.angular = 0
                    self.mode = "IDLE"
                self.publish(
                    "availability",
                    self.message("availability", state="online", reason="MOCK_REBOOT"),
                    1,
                    True,
                )
                self.publish(
                    "capabilities",
                    self.message(
                        "capabilities",
                        protocol_version="1.2.0",
                        supported_commands=sorted(self.supported_commands),
                        sensors=["smoke", "bottom_ir", "top_ir"],
                        media=["roof_rgb", "roof_thermal", "bottom_ir"],
                    ),
                    1,
                    True,
                )
            if self.offline_after > 0 and not self.offline_sent and uptime >= self.offline_after:
                self.offline_sent = True
                self.publish(
                    "availability",
                    self.message("availability", state="offline", reason="FAULT_INJECTION"),
                    1,
                    True,
                )
            if self.offline_sent:
                time.sleep(0.05)
                continue
            if now >= status_tick:
                with self.lock:
                    payload = self.message(
                        "status",
                        mode=self.mode,
                        battery=self.battery,
                        estop_active=self.estop,
                        active_task_id=self.active_task_id,
                        network_rssi=-48,
                    )
                self.publish("status", payload, 1)
                status_tick = now + 1
            if now >= sensor_tick:
                smoke = 3 + random.random() * 2
                self.publish(
                    "sensor",
                    self.message(
                        "sensor",
                        smoke=smoke,
                        bottom_ir=31 + random.random(),
                        top_ir_max=36 + random.random() * 2,
                        payload={},
                    ),
                    0,
                    allow_loss=True,
                )
                sensor_tick = now + 0.5
            if now >= heartbeat_tick:
                self.publish(
                    "heartbeat", self.message("heartbeat", uptime_seconds=now - self.started), 0
                )
                heartbeat_tick = now + 1
            if self.fire_after > 0 and not self.fire_sent and now - self.started >= self.fire_after:
                self.fire_sent = True
                self.publish(
                    "alarm",
                    self.message(
                        "alarm",
                        event_id=f"MOCK-FIRE-{int(now)}",
                        fire_type="smoke",
                        severity="HIGH",
                        confidence=0.93,
                        parking_slot_code="A-12",
                        position={"x": 25.0, "y": 13.0, "theta": 0.0},
                        media={},
                    ),
                    1,
                )
            time.sleep(0.05)

    def run(self) -> None:
        self.client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30)
        self.client.loop_start()
        threading.Thread(target=self.motion_loop, daemon=True).start()
        self.periodic_loop()


if __name__ == "__main__":
    while True:
        try:
            MockRobot().run()
        except Exception:
            logger.exception("Mock Robot restarting")
            time.sleep(3)
