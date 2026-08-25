#!/usr/bin/env python3
"""Field trace + console 单测（无 ROS/MQTT 依赖）。"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from firebot_bridge.field_trace import TRACE_PREFIX, FieldTrace, sanitize  # noqa: E402
import field_console  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def _capture():
    import firebot_bridge.field_trace as ft

    ft.LOG.setLevel(logging.DEBUG)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    ft.LOG.addHandler(handler)
    return buf, handler, ft


def _release(handler, ft):
    ft.LOG.removeHandler(handler)


def _trace_lines(buf):
    return [line for line in buf.getvalue().splitlines() if TRACE_PREFIX in line]


def _install_fake_paho(record: dict):
    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    client_mod = types.ModuleType("paho.mqtt.client")

    class _Callbacks:
        VERSION2 = "2"

    class _Info:
        def wait_for_publish(self, timeout=None):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def username_pw_set(self, *a, **k):
            pass

        def tls_set(self, *a, **k):
            pass

        def will_set(self, *a, **k):
            pass

        def publish(self, topic, payload, qos=0, retain=False):
            record["publish_calls"] += 1
            return _Info()

    client_mod.Client = _Client
    client_mod.CallbackAPIVersion = _Callbacks
    client_mod.MQTTv5 = 5
    paho_mqtt.client = client_mod
    paho.mqtt = paho_mqtt
    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = paho_mqtt
    sys.modules["paho.mqtt.client"] = client_mod


class _Cfg:
    mqtt_host = "127.0.0.1"
    mqtt_port = 8883
    mqtt_username = "u"
    mqtt_password = "p"
    mqtt_tls = False
    ca_cert = ""
    vehicle_id = "firebot-vehicle-01"
    protocol_version = "1.3.0"
    supported_commands = []
    sensors = []
    media = []


class _Identity:
    client_id = "test-client"
    boot_id = "boot-0001"


class _Proto:
    def topic(self, name):
        return f"robot/firebot-vehicle-01/{name}"

    def base(self, msg_type):
        import uuid

        return {
            "schema_version": "1.3", "message_id": str(uuid.uuid4()), "type": msg_type,
            "vehicle_id": "firebot-vehicle-01", "boot_id": "boot-0001",
            "timestamp": "2026-08-24T00:00:00+00:00", "seq": 1,
        }


def main() -> int:
    print("=== TRACE：runtime 事件层 ===")
    # TRACE-01 disabled 不输出
    buf, handler, ft = _capture()
    FieldTrace(False).emit("mqtt.connected", broker="x")
    check("TRACE-01 disabled 不输出 FBTRACE", _trace_lines(buf) == [])
    _release(handler, ft)

    # TRACE-02 enabled 单行合法 JSON
    buf, handler, ft = _capture()
    FieldTrace(True).emit("mqtt.connected", broker="1.2.3.4:8883", boot="boot-0001")
    lines = _trace_lines(buf)
    check("TRACE-02 enabled 输出单行", len(lines) == 1)
    payload = json.loads(lines[0].split(TRACE_PREFIX, 1)[1])
    check("TRACE-02 event 合法 JSON", payload.get("event") == "mqtt.connected")
    _release(handler, ft)

    # TRACE-03 secret redaction
    check("TRACE-03 secret redaction", sanitize({"password": "super"}) == {"password": "<redacted>"})

    # TRACE-04 无 ANSI
    buf, handler, ft = _capture()
    FieldTrace(True).emit("mqtt.connected", broker="x")
    check("TRACE-04 无 ANSI escape", "\x1b[" not in buf.getvalue())
    _release(handler, ft)

    # TRACE-05/06 transition
    buf, handler, ft = _capture()
    tr = FieldTrace(True)
    tr.transition("ros.master", True, "ros.master.changed", state="AVAILABLE")
    tr.transition("ros.master", True, "ros.master.changed", state="AVAILABLE")
    check("TRACE-05 相同状态 suppress", len(_trace_lines(buf)) == 1)
    tr.transition("ros.master", False, "ros.master.changed", state="UNAVAILABLE")
    check("TRACE-06 变化输出", len(_trace_lines(buf)) == 2)
    _release(handler, ft)

    # TRACE-07 throttle
    buf, handler, ft = _capture()
    tr = FieldTrace(True)
    tr.throttle("hb", 10.0, "mqtt.heartbeat.tx", level="debug")
    tr.throttle("hb", 10.0, "mqtt.heartbeat.tx", level="debug")
    check("TRACE-07 heartbeat throttle", len(_trace_lines(buf)) == 1)
    _release(handler, ft)

    # TRACE-09/10/11 battery changed
    buf, handler, ft = _capture()
    tr = FieldTrace(True)
    tr.changed("b", 67.5, "ros.battery.rx", tolerance=0.1, battery=67.5)
    check("TRACE-09 battery 首次输出", len(_trace_lines(buf)) == 1)
    tr.changed("b", 67.5, "ros.battery.rx", tolerance=0.1, battery=67.5)
    check("TRACE-10 battery 重复 suppress", len(_trace_lines(buf)) == 1)
    tr.changed("b", 67.0, "ros.battery.rx", tolerance=0.1, battery=67.0)
    check("TRACE-11 battery 变化输出", len(_trace_lines(buf)) == 2)
    _release(handler, ft)

    # TRACE-12 command summary 不含 params 全量
    buf, handler, ft = _capture()
    tr = FieldTrace(True)
    tr.command_received({"cmd": "patrol", "command_id": "c1", "task_id": "t1", "params": {"secret": "x"}})
    payload = json.loads(_trace_lines(buf)[0].split(TRACE_PREFIX, 1)[1])
    check("TRACE-12 command summary 不含 params", "params" not in payload)
    check("TRACE-12 command summary 不含 secret", "secret" not in payload)
    _release(handler, ft)

    # TRACE-13 latency context 不进 BridgeState
    from firebot_bridge.state import BridgeState

    check("TRACE-13 BridgeState 无 trace 字段", not hasattr(BridgeState(), "_seen_at"))

    # TRACE-08 heartbeat trace 不影响真实 publish 次数
    rec = {"publish_calls": 0}
    _install_fake_paho(rec)
    from firebot_bridge.mqtt_client import MqttClient

    mqtt = MqttClient(_Cfg(), _Identity(), _Proto(), lambda c: None, status=None, trace=FieldTrace(True))
    for _ in range(10):
        mqtt.publish("robot/x/heartbeat", {"type": "heartbeat", "seq": 1, "uptime_seconds": 1.0})
    check("TRACE-08 heartbeat 真实 publish 10 次", rec["publish_calls"] == 10)

    print("=== VIEW：terminal viewer ===")
    # VIEW-01 valid render
    line = '2026-08-24 INFO firebot-bridge FBTRACE\t{"event":"mqtt.connected","level":"ok","broker":"x"}'
    ev = field_console.parse_trace(line)
    check("VIEW-01 parse valid", ev is not None and ev.get("event") == "mqtt.connected")

    # VIEW-02 malformed 不 crash
    bad = '2026-08-24 INFO firebot-bridge FBTRACE\t{not-json'
    check("VIEW-02 malformed 不 crash", field_console.parse_trace(bad) is None)

    # VIEW-03 ordinary line
    check("VIEW-03 ordinary line 忽略", field_console.parse_trace("2026-08-24 INFO firebot-bridge hello") is None)

    # VIEW-04 no-color 无 ANSI
    c = field_console.FieldConsole(use_color=False)
    out = c.render({"event": "mqtt.connected", "level": "ok", "mono": 0, "broker": "x"})
    check("VIEW-04 no-color 无 ANSI", out is not None and "\x1b[" not in out)

    # VIEW-05 NO_COLOR（等价 no-color）
    os.environ["NO_COLOR"] = "1"
    c = field_console.FieldConsole(use_color=False)
    out = c.render({"event": "mqtt.connected", "level": "ok", "mono": 0, "broker": "x"})
    check("VIEW-05 NO_COLOR 无 ANSI", out is not None and "\x1b[" not in out)
    os.environ.pop("NO_COLOR", None)

    # VIEW-06 ID 短显
    check("VIEW-06 ID 默认 8 char", field_console.short_id("a739616e-f049-4bb8-998a") == "a739616e")

    # VIEW-07 full id
    check("VIEW-07 --full-id 完整", field_console.short_id("a739616e-f049-4bb8-998a", full=True) == "a739616e-f049-4bb8-998a")

    # VIEW-08 compact
    c = field_console.FieldConsole(compact=True)
    out = c.render({"event": "mqtt.command.rx", "level": "rx", "mono": 0, "cmd": "patrol", "command_id": "c1"})
    check("VIEW-08 compact 渲染", out is not None and "MQTT" in out and "patrol" in out)

    # VIEW-09 unknown event 不 crash
    c = field_console.FieldConsole()
    check("VIEW-09 unknown event 不 crash", c.render({"event": "unknown.thing"}) is None)

    # VIEW-10 EOF 等价空行不 crash
    check("VIEW-10 空行不 crash", field_console.parse_trace("") is None)

    print("=== STATUS ===")
    from firebot_bridge.runtime_status import _DEFAULT_FIELDS

    # STATUS-01 status 文件无 secret
    secret_keys = [k for k in _DEFAULT_FIELDS if any(s in k.lower() for s in ("password", "secret", "token", "cookie"))]
    check("STATUS-01 status 无 secret 字段", secret_keys == [])

    # STATUS-02 启动头显示安全态
    header = field_console.FieldConsole().header({
        "boot_id": "boot-0001", "vehicle_id": "firebot-vehicle-01", "protocol_version": "1.3.0",
        "pid": 123, "stub_mode": False, "supported_commands": [], "sensors": [],
        "location_enabled": False,
    })
    check("STATUS-02 Commands NONE", "NONE" in header)
    check("STATUS-02 Stub OFF", "OFF" in header)
    check("STATUS-02 Control NOT IMPLEMENTED", "NOT IMPLEMENTED" in header)

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
