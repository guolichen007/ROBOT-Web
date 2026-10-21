"""无 ROS 的 v1 fake；只允许显式启用的本地 test 环境。"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
from app.core.config import get_settings

from services.vehicle_agent_v1 import WireCommand, program_ready, topic


class MockAgent:
    def __init__(self):
        self.online = True
        self.programs = dict.fromkeys(
            ["chassis", "navigation", "patrol", "thermal", "battery", "smog", "camera"], False
        )
        self.task = {
            "state": "idle",
            "patrol_next": None,
            "patrol_count": 0,
            "patrol_paused": False,
        }
        self.cache: dict[str, tuple[float, dict]] = {}
        self.executions: dict[str, int] = {}
        self.failure: str | None = None

    def status(self) -> dict:
        return {
            "online": self.online,
            "programs": dict(self.programs),
            "task": dict(self.task),
            "ts": datetime.now(UTC).isoformat(),
        }

    def telemetry(self) -> dict:
        return {
            "pose": {"x": 2.0, "y": 3.0, "yaw": 0.0, "frame_id": "map"},
            "battery": {"percentage": 73.0},
            "smog": {"percentage": 2.0, "raw": 20, "alarm": False},
            "thermal": {"max_temp": 25.0},
            "ts": datetime.now(UTC).isoformat(),
        }

    def arrive(self) -> None:
        self.task["state"] = "idle"

    def execute(self, data: dict, *, now: float | None = None) -> dict | None:
        wire = WireCommand.model_validate(data)
        stamp = now if now is not None else time.monotonic()
        self.cache = {k: v for k, v in self.cache.items() if stamp - v[0] < 300}
        msg_id = str(wire.msg_id)
        if msg_id in self.cache:
            return self.cache[msg_id][1]
        if self.failure == "TIMEOUT":
            return None
        action = wire.command
        code = self.failure or "OK"
        state, paused = self.task["state"], self.task["patrol_paused"]
        ready = program_ready(self.programs["chassis"]) and program_ready(
            self.programs["navigation"]
        )
        if code == "OK":
            if action in {"patrol", "resume_patrol", "homing", "navigate"} and not ready:
                code = "NOT_READY"
            elif action in {"patrol", "homing", "navigate"} and (state != "idle" or paused):
                code = "BUSY"
            elif action == "resume_patrol" and not paused:
                code = "NOT_READY"
            elif action == "close" and state != "idle" and not paused:
                code = "BUSY"
        if code == "OK":
            self.executions[action] = self.executions.get(action, 0) + 1
            if action == "start":
                code = "ALREADY_RUNNING" if self.programs["chassis"] else "OK"
                self.programs = dict.fromkeys(self.programs, True)
            elif action == "close":
                code = "ALREADY_CLOSED" if not self.programs["chassis"] else "OK"
                self.programs = dict.fromkeys(self.programs, False)
                self.task.update(state="idle", patrol_paused=False)
            elif action in {"patrol", "resume_patrol"}:
                self.task.update(state="patrol", patrol_paused=False)
            elif action == "stop_patrol":
                code = "ALREADY_STOPPED" if state != "patrol" else "OK"
                self.task.update(state="idle", patrol_paused=state == "patrol" or paused)
            elif action == "homing":
                self.task.update(state="homing")
            elif action == "navigate":
                self.task.update(state="spot")
            elif action == "stop_navigate":
                code = "ALREADY_STOPPED" if state != "spot" else "OK"
                self.task.update(state="idle")
        ack = {
            "msg_id": msg_id,
            "success": code in {"OK", "ALREADY_RUNNING", "ALREADY_CLOSED", "ALREADY_STOPPED"},
            "code": code,
            "message": f"mock {action}: {code}",
            "data": {},
            "duration_ms": 1,
            "ts": datetime.now(UTC).isoformat(),
        }
        if action == "get_status":
            ack["data"] = self.status()
        if action == "get_telemetry":
            ack["data"] = self.telemetry()
        self.cache[msg_id] = (stamp, ack)
        return ack


def main() -> None:
    import os

    settings = get_settings()
    if (
        settings.app_env != "test"
        or not settings.mock_enabled
        or settings.mqtt_host not in {"mosquitto", "localhost", "127.0.0.1"}
    ):
        raise RuntimeError("mock vehicle_agent requires isolated local test environment")
    vehicle = os.environ.get("MOCK_VEHICLE_ID", "R001")
    mock = MockAgent()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"mock-vehicle-agent-{vehicle}"
    )
    client.will_set(topic(vehicle, "availability"), "offline", qos=1, retain=True)

    def publish(kind, data, qos=1, retain=False):
        client.publish(topic(vehicle, kind), json.dumps(data), qos=qos, retain=retain)

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(topic(vehicle, "cmd"), qos=1)
        client.publish(topic(vehicle, "availability"), "online", qos=1, retain=True)

    def on_message(client, userdata, message):
        ack = mock.execute(json.loads(message.payload))
        if ack:
            publish("ack", ack)
        publish("status", mock.status(), retain=True)

    client.on_connect, client.on_message = on_connect, on_message
    client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=10)
    client.loop_start()
    try:
        while True:
            publish("status", mock.status(), retain=True)
            publish("telemetry", mock.telemetry(), qos=0)
            time.sleep(0.5)
    finally:
        client.publish(
            topic(vehicle, "availability"), "offline", qos=1, retain=True
        ).wait_for_publish(2)
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
