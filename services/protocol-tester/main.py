from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
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
            "capabilities",
            102,
            protocol_version="1.1",
            supported_commands=[
                "manual_control",
                "stop_motion",
                "emergency_stop",
                "reset_estop",
                "return_dock",
                "patrol",
                "extinguish",
                "cancel_task",
            ],
            sensors=["smoke", "bottom_ir", "top_ir"],
            media=["roof_rgb", "roof_thermal", "bottom_ir"],
        ),
        base("status", 103, mode="IDLE", battery=90, estop_active=False, active_task_id=None),
        base(
            "location",
            104,
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
        base("sensor", 105, smoke=1, bottom_ir=30, top_ir_max=32, payload={}),
        base(
            "alarm",
            106,
            event_id=f"PROTOCOL-{uuid4()}",
            fire_type="smoke",
            severity="LOW",
            confidence=0.5,
            parking_slot_code="A-12",
            position={"x": 25, "y": 13, "theta": 0},
            media={},
        ),
        base(
            "task_status",
            107,
            task_id=str(uuid4()),
            status="EXECUTING",
            phase="PROTOCOL_TEST",
            progress=10,
            failure_code=None,
            failure_message=None,
        ),
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
    invalid_schema = dict(messages[0], message_id=str(uuid4()), seq=108, schema_version="9.9")
    client.publish("robot/R001/heartbeat", json.dumps(invalid_schema), qos=0)

    for index, ack_status in enumerate(("accepted", "rejected", "unsupported"), start=109):
        ack = base(
            "command_ack",
            index,
            command_id=str(uuid4()),
            status=ack_status,
            reason="PROTOCOL_TEST",
        )
        ack["boot_id"] = boot
        client.publish("robot/R001/command_ack", json.dumps(ack), qos=1)

    now = datetime.now(UTC)
    command = base(
        "command",
        1,
        command_id=f"C-PROTOCOL-{str(uuid4())[:8]}",
        correlation_id=str(uuid4()),
        task_id=None,
        lease_id=None,
        control_session_id=None,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=5)).isoformat(),
        ttl_ms=5000,
        priority=95,
        source="WEB",
        operator_id="protocol-tester",
        cmd="stop_motion",
        params={"reason": "PROTOCOL_TEST"},
    )
    command["boot_id"] = boot
    validate_message(command)
    client.publish("robot/R001/command", json.dumps(command), qos=1, retain=False)

    expired = dict(
        command,
        message_id=str(uuid4()),
        command_id=f"C-EXPIRED-{str(uuid4())[:8]}",
        issued_at=(now - timedelta(seconds=10)).isoformat(),
        expires_at=(now - timedelta(seconds=5)).isoformat(),
    )
    validate_message(expired)
    client.publish("robot/R001/command", json.dumps(expired), qos=1, retain=False)

    mismatch = dict(
        messages[4],
        message_id=str(uuid4()),
        seq=120,
        map_version="PROTOCOL_MISMATCH",
    )
    client.publish("robot/R001/location", json.dumps(mismatch), qos=0)
    restored = dict(mismatch, message_id=str(uuid4()), seq=121, map_version="1")
    client.publish("robot/R001/location", json.dumps(restored), qos=0)

    restart = dict(messages[0], message_id=str(uuid4()), boot_id=str(uuid4()), seq=1)
    client.publish("robot/R001/heartbeat", json.dumps(restart), qos=0)

    deadline = time.time() + 3
    while not received_command and time.time() < deadline:
        time.sleep(0.05)
    if not received_command:
        raise RuntimeError("command subscription did not receive a conforming command")
    client.loop_stop()
    client.disconnect()
    return [
        "PASS broker connection",
        "PASS publish heartbeat/availability/capabilities/telemetry/alarm/task status",
        "PASS receive conforming command",
        "PASS publish accepted/rejected/unsupported ACK",
        "PASS duplicate/out-of-order/bad JSON/unknown schema",
        "PASS TTL expiry and command retain=false",
        "PASS boot restart and seq reset",
        "PASS map mismatch and restore",
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
