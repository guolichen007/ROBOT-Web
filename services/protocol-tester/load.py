from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from uuid import uuid4

import paho.mqtt.client as mqtt
from app.core.config import get_settings

from services.protocol import validate_message


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Publisher:
    def __init__(self, vehicle_ids: list[str]) -> None:
        self.settings = get_settings()
        self.vehicle_ids = vehicle_ids
        self.boots = {vehicle: str(uuid4()) for vehicle in vehicle_ids}
        self.sequences: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.counts: Counter[str] = Counter()
        self.failures: list[str] = []
        self.connected = False
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"firebot-load-{uuid4().hex[:12]}",
            protocol=mqtt.MQTTv5,
        )
        if self.settings.mqtt_username:
            self.client.username_pw_set(
                self.settings.mqtt_username, self.settings.effective_mqtt_password
            )
        self.settings.configure_mqtt_client(self.client)
        self.client.on_connect = self.on_connect

    def on_connect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        self.connected = reason_code == 0

    def message(self, vehicle: str, kind: str, **payload) -> dict:
        self.sequences[vehicle][kind] += 1
        return {
            "schema_version": "1.2",
            "message_id": str(uuid4()),
            "type": kind,
            "vehicle_id": vehicle,
            "boot_id": self.boots[vehicle],
            "timestamp": utc_now(),
            "seq": self.sequences[vehicle][kind],
            **payload,
        }

    def publish(
        self, vehicle: str, kind: str, *, qos: int = 0, retain: bool = False, **payload
    ) -> None:
        message = self.message(vehicle, kind, **payload)
        validate_message(message)
        result = self.client.publish(
            f"robot/{vehicle}/{kind}",
            json.dumps(message, separators=(",", ":")),
            qos=qos,
            retain=retain,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self.failures.append(f"{vehicle}:{kind}:rc={result.rc}")
        self.counts[kind] += 1

    def start(self) -> None:
        self.client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=30)
        self.client.loop_start()
        deadline = time.monotonic() + 10
        while not self.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.connected:
            raise RuntimeError("MQTT load publisher did not connect")
        for vehicle in self.vehicle_ids:
            self.publish(
                vehicle, "availability", qos=1, retain=True, state="online", reason="LOAD_TEST"
            )
            self.publish(
                vehicle,
                "capabilities",
                qos=1,
                retain=True,
                protocol_version="1.2.0",
                supported_commands=["manual_control", "stop_motion", "emergency_stop"],
                sensors=["smoke", "bottom_ir", "top_ir"],
                media=[],
            )

    def stop(self) -> None:
        for vehicle in self.vehicle_ids:
            self.publish(
                vehicle, "availability", qos=1, retain=True, state="offline", reason="LOAD_TEST_END"
            )
        time.sleep(0.5)
        self.client.disconnect()
        self.client.loop_stop()

    def run(self, duration: int) -> dict:
        self.start()
        started = datetime.now(UTC)
        start_tick = time.monotonic()
        next_location = next_sensor = next_status = start_tick
        try:
            while (elapsed := time.monotonic() - start_tick) < duration:
                now = time.monotonic()
                if now >= next_location:
                    for index, vehicle in enumerate(self.vehicle_ids):
                        self.publish(
                            vehicle,
                            "location",
                            position={
                                "x": round((elapsed * 0.1 + index) % 20, 4),
                                "y": round(2 + index * 0.2, 4),
                                "theta": round((elapsed * 0.02) % 6.283, 4),
                            },
                            linear_speed=0.1,
                            angular_speed=0.02,
                            battery=80.0,
                            site_code="DEMO_PARKING",
                            map_code="parking_v1",
                            map_version="1",
                            map_checksum="demo-map-v1-sha256",
                            frame_id="map",
                            parking_slot_code=None,
                            localization_status="OK",
                        )
                    next_location += 0.1
                if now >= next_sensor:
                    for vehicle in self.vehicle_ids:
                        self.publish(
                            vehicle,
                            "sensor",
                            smoke=0.0,
                            bottom_ir=25.0,
                            top_ir_max=26.0,
                            payload={},
                        )
                    next_sensor += 0.5
                if now >= next_status:
                    for vehicle in self.vehicle_ids:
                        self.publish(vehicle, "heartbeat", uptime_seconds=elapsed)
                        self.publish(
                            vehicle,
                            "status",
                            qos=1,
                            mode="IDLE",
                            battery=80.0,
                            estop_active=False,
                            active_task_id=None,
                        )
                    next_status += 1.0
                time.sleep(0.005)
        finally:
            self.stop()
        finished = datetime.now(UTC)
        return {
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": round((finished - started).total_seconds(), 2),
            "robots": len(self.vehicle_ids),
            "counts": dict(self.counts),
            "failures": self.failures,
            "result": "PASS" if not self.failures else "FAIL",
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["soak", "burst"], required=True)
    parser.add_argument("--duration", type=int)
    args = parser.parse_args()
    if args.mode == "soak":
        vehicles = ["R001"]
        duration = args.duration or 3600
    else:
        vehicles = [f"LOAD{index:03d}" for index in range(1, 11)]
        duration = args.duration or 600
    result = Publisher(vehicles).run(duration)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
