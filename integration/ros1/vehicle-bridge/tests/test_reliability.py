#!/usr/bin/env python3
"""Bridge 可靠性单测：MQTT 单一 owner、ROS 子进程生命周期、SIGTERM、readiness 真实性。

运行：cd vehicle-bridge && python3 tests/test_reliability.py
"""
from __future__ import annotations

import io
import os
import socket
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def _install_fake_paho(record: dict):
    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    client_mod = types.ModuleType("paho.mqtt.client")

    class _Callbacks:
        VERSION2 = "2"

    class _FakeInfo:
        def wait_for_publish(self, timeout=None):
            pass

    class _FakeMqttClient:
        def __init__(self, *a, **k):
            pass

        def username_pw_set(self, *a, **k):
            pass

        def tls_set(self, *a, **k):
            pass

        def will_set(self, *a, **k):
            pass

        def reconnect_delay_set(self, min_delay, max_delay):
            record["reconnect_delay"] = (min_delay, max_delay)

        def connect_async(self, host, port, keepalive=30):
            record["connect_async"] = (host, port, keepalive)

        def loop_start(self):
            record["loop_start"] = True

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

        def is_connected(self):
            return False

        def subscribe(self, *a, **k):
            pass

        def publish(self, topic, payload, qos=0, retain=False):
            return _FakeInfo()

    client_mod.Client = _FakeMqttClient
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
            "schema_version": "1.3",
            "message_id": str(uuid.uuid4()),
            "type": msg_type,
            "vehicle_id": "firebot-vehicle-01",
            "boot_id": "boot-0001",
            "timestamp": "2026-08-24T00:00:00+00:00",
            "seq": 1,
        }


class _FakeProc:
    def __init__(self):
        self.pid = 999
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


class _FakeSubprocess:
    PIPE = -1

    def __init__(self):
        self.calls = []
        self.procs = []

    def Popen(self, *a, **k):
        self.calls.append((a, k))
        p = _FakeProc()
        self.procs.append(p)
        return p


def main() -> int:
    print("=== T4/T5 MQTT 单一连接 owner ===")
    rec: dict = {}
    _install_fake_paho(rec)
    from firebot_bridge.mqtt_client import MqttClient

    mqtt = MqttClient(_Cfg(), _Identity(), _Proto(), lambda c: None, status=None)
    mqtt.start()
    check("MQTT 用 connect_async（不手写 connect retry loop）", "connect_async" in rec)
    check("MQTT 用 loop_start（Paho 单一 owner）", rec.get("loop_start") is True)
    check("MQTT reconnect_delay_set(1,30)", rec.get("reconnect_delay") == (1, 30))

    print("=== T1/T2/T3/T7 ROS 子进程生命周期 ===")
    import firebot_bridge.ros.lifecycle as lc
    from firebot_bridge.state import BridgeState

    # T1: 无 master（关闭端口探测为 False）→ 不 spawn、命令拒绝
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    os.environ["ROS_MASTER_URI"] = f"http://127.0.0.1:{port}"
    check("ros_master_reachable 对关闭端口返回 False", lc.ros_master_reachable(timeout=0.3) is False)

    state = BridgeState()
    mgr = lc.RosChildManager(_Cfg(), state, status=None)
    check("无 master：adapter_ready=False", mgr.adapter_ready is False)
    check("无 child：publish_command 返回 False", mgr.publish_command({"cmd": "patrol", "command_id": "C"}) is False)

    _orig_sp = lc.subprocess
    fake_sp = _FakeSubprocess()
    lc.subprocess = fake_sp
    try:
        # T2: master 出现 → spawn 子进程（fresh child）
        mgr._spawn()
        check("master 出现：Popen 被调用", len(fake_sp.calls) == 1)
        cmd = fake_sp.calls[0][0][0]
        check("spawn 目标是 firebot_bridge.ros_adapter", any("firebot_bridge.ros_adapter" in str(x) for x in cmd))

        # ready 上报 → adapter ready
        mgr._handle_event({"type": "ready", "ok": True, "command_publisher": True,
                           "feedback": True, "providers": {"battery": True, "smoke": True}})
        check("ready 后 node_ready=True", mgr.node_ready is True)
        check("ready 后 adapter_ready=True", mgr.adapter_ready is True)

        # T7: battery provider 事件 → state.battery + provider_seen
        mgr._handle_event({"type": "provider", "channel": "battery", "value": 67.5})
        check("provider battery 67.5 → state.battery", state.last_battery == 67.5)
        check("provider battery → battery_provider_seen=True", mgr.battery_provider_seen is True)

        # T3: master flap → terminate + respawn（全新子进程，无 callback 累积）
        mgr._terminate_child()
        check("master 丢失：子进程被 terminate", fake_sp.procs[0].terminated is True)
        mgr._spawn()
        check("master 恢复：重新 spawn 全新子进程", len(fake_sp.calls) == 2)
    finally:
        lc.subprocess = _orig_sp

    # T6 partial init：feedback 未就绪 → adapter_ready=False（禁止假 READY）
    state2 = BridgeState()
    mgr2 = lc.RosChildManager(_Cfg(), state2, status=None)
    mgr2._handle_event({"type": "ready", "ok": True, "command_publisher": True,
                        "feedback": False, "providers": {"battery": True}})
    check("feedback 未就绪 → adapter_ready=False", mgr2.adapter_ready is False)
    check("compute_adapter_ready 要求两者都 true", lc.compute_adapter_ready(True, False) is False)

    print("=== T6 SIGTERM/SIGINT 优雅停机 ===")
    from firebot_bridge.main import make_stop_handler

    stop = threading.Event()
    handler = make_stop_handler(stop)
    handler(15, None)
    check("SIGTERM handler 设置 stop", stop.is_set() is True)

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
