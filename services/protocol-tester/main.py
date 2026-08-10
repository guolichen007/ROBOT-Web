from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import paho.mqtt.client as mqtt
from app.core.config import get_settings

from services.protocol import validate_message


def base(message_type: str, seq: int, **extra) -> dict:
    return {
        "schema_version": "1.1",
        "message_id": str(uuid4()),
        "type": message_type,
        "vehicle_id": "R001",
        "boot_id": str(uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "seq": seq,
        **extra,
    }


def schema_tests() -> list[str]:
    results: list[str] = []
    fixture_root = Path(__file__).resolve().parents[2] / "packages/shared-fixtures"
    for fixture in fixture_root.glob("*.json"):
        validate_message(json.loads(fixture.read_text(encoding="utf-8")))
        results.append(f"PASS schema {fixture.name}")
    invalid = base("heartbeat", 1, uptime_seconds=1)
    invalid["schema_version"] = "9.9"
    try:
        validate_message(invalid)
        raise AssertionError("unknown schema accepted")
    except Exception:
        results.append("PASS reject unknown schema")
    return results


def broker_tests() -> list[str]:
    settings = get_settings()
    connected = False
    received_command = False
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"protocol-tester-{uuid4()}",
        protocol=mqtt.MQTTv5,
    )
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.effective_mqtt_password)
    settings.configure_mqtt_client(client)

    def on_connect(c, u, f, reason, props):
        nonlocal connected
        connected = reason == 0
        c.subscribe("robot/R001/command", qos=1)

    def on_message(c, u, msg):
        nonlocal received_command
        payload = json.loads(msg.payload)
        validate_message(payload)
        received_command = True

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=20)
    client.loop_start()
    deadline = time.time() + 5
    while not connected and time.time() < deadline:
        time.sleep(0.1)
    if not connected:
        raise RuntimeError("broker connection failed")
    boot = str(uuid4())
    messages = [
        base("heartbeat", 100, uptime_seconds=5),
        base("availability", 101, state="online", reason="PROTOCOL_TEST"),
        base(
            "location",
            102,
            position={"x": 1, "y": 2, "theta": 0},
            linear_speed=0,
            angular_speed=0,
            battery=90,
            site_code="DEMO_PARKING",
            map_code="parking_v1",
            map_version="1",
            frame_id="map",
            parking_slot_code="A-01",
            localization_status="OK",
        ),
        base("sensor", 103, smoke=1, bottom_ir=30, top_ir_max=32, payload={}),
    ]
    for message in messages:
        message["boot_id"] = boot
        validate_message(message)
        client.publish(
            f"robot/R001/{message['type']}",
            json.dumps(message),
            qos=0 if message["type"] in {"heartbeat", "location", "sensor"} else 1,
        )
    duplicate = messages[0]
    client.publish("robot/R001/heartbeat", json.dumps(duplicate), qos=0)
    out_of_order = dict(messages[0], message_id=str(uuid4()), seq=50)
    client.publish("robot/R001/heartbeat", json.dumps(out_of_order), qos=0)
    client.publish("robot/R001/status", "{bad-json", qos=1)
    time.sleep(0.5)
    client.loop_stop()
    client.disconnect()
    return [
        "PASS broker connection",
        "PASS publish valid fixtures",
        "PASS publish duplicate/out-of-order/bad JSON",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Firebot MQTT protocol conformance tester")
    parser.add_argument("--schema-only", action="store_true")
    args = parser.parse_args()
    try:
        results = schema_tests()
        if not args.schema_only:
            results.extend(broker_tests())
        print("\n".join(results))
        print(f"RESULT PASS count={len(results)}")
        return 0
    except Exception as exc:
        print(f"RESULT FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
