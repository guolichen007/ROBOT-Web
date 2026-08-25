#!/usr/bin/env python3
"""Bridge 可靠性单测：MQTT 单一 owner、ROS 子进程 supervisor、IPC 隔离、SIGTERM。

运行：cd vehicle-bridge && python3 tests/test_reliability.py
"""
from __future__ import annotations

import io
import os
import socket
import sys
import threading
import time
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


class _BlockingStdout:
    """永不 EOF 的 stdout，避免 reader thread 在测试中立刻触发 _on_child_exit。"""

    def __init__(self):
        self._ev = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self._ev.wait()
        raise StopIteration


class _FakeProc:
    def __init__(self):
        self.pid = 999
        self.stdin = io.StringIO()
        self.stdout = _BlockingStdout()
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
    check("MQTT 用 connect_async", "connect_async" in rec)
    check("MQTT 用 loop_start（单一 owner）", rec.get("loop_start") is True)
    check("MQTT reconnect_delay_set(1,30)", rec.get("reconnect_delay") == (1, 30))

    print("=== T1/T2/T3/T7 ROS 子进程 supervisor ===")
    import firebot_bridge.ros.lifecycle as lc
    from firebot_bridge.state import BridgeState

    # T1: 无 master（关闭端口探测 False）→ 不 spawn、命令拒绝
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
        # T2: master 出现 → spawn（fresh child）
        mgr._spawn()
        check("master 出现：Popen 被调用", len(fake_sp.calls) == 1)
        cmd = fake_sp.calls[0][0][0]
        check("spawn 目标 firebot_bridge.ros_adapter", any("firebot_bridge.ros_adapter" in str(x) for x in cmd))

        mgr._handle_event({"type": "ready", "ok": True, "command_publisher": True,
                           "feedback": True, "providers": {"battery": True, "smoke": True}})
        check("ready 后 node_ready=True", mgr.node_ready is True)
        check("ready 后 adapter_ready=True", mgr.adapter_ready is True)

        # T7: battery provider 事件
        mgr._handle_event({"type": "provider", "channel": "battery", "value": 67.5})
        check("provider battery → state.battery", state.last_battery == 67.5)
        check("provider battery → battery_provider_seen", mgr.battery_provider_seen is True)

        # T3: master flap → terminate + respawn（fresh child，无 callback 累积）
        mgr._terminate_child()
        check("master 丢失：子进程被 terminate", fake_sp.procs[0].terminated is True)
        mgr._spawn()
        check("master 恢复：重新 spawn 全新子进程", len(fake_sp.calls) == 2)
    finally:
        lc.subprocess = _orig_sp

    # T6 partial init：feedback 未就绪 → adapter_ready=False
    mgr2 = lc.RosChildManager(_Cfg(), BridgeState(), status=None)
    mgr2._handle_event({"type": "ready", "ok": True, "command_publisher": True,
                        "feedback": False, "providers": {"battery": True}})
    check("feedback 未就绪 → adapter_ready=False", mgr2.adapter_ready is False)
    check("compute_adapter_ready 要求两者 true", lc.compute_adapter_ready(True, False) is False)

    print("=== P0 补强：child 崩溃恢复 / READY 超时 / generation 隔离 ===")
    # child EOF/crash → _on_child_exit：reap + _proc=None + 清 telemetry + backoff
    crash_state = BridgeState()
    crash_state.set_battery(88.0)
    crash_mgr = lc.RosChildManager(_Cfg(), crash_state, status=None)
    fp = _FakeProc()
    crash_mgr._proc = fp
    crash_mgr._stdin = fp.stdin
    crash_mgr._on_child_exit(fp)
    check("child crash：_proc 被清空", crash_mgr._proc is None)
    check("child crash：子进程被 terminate", fp.terminated is True)
    check("child crash：ROS telemetry 清空（不 stale）", crash_state.last_battery is None)
    check("child crash：退避生效（_next_spawn_allowed 未来）", crash_mgr._next_spawn_allowed > time.monotonic())

    # READY timeout（现在看 adapter_ready，不是 node_ready）
    timeout_mgr = lc.RosChildManager(_Cfg(), BridgeState(), status=None)
    timeout_mgr._proc = _FakeProc()
    timeout_mgr._spawn_time = time.monotonic() - 20
    check("READY 超时判定：adapter 未就绪且超时 → True", timeout_mgr._ready_timed_out(time.monotonic()) is True)
    timeout_mgr.command_publisher_ready = True
    timeout_mgr.feedback_ready = True
    check("READY 超时判定：adapter 就绪 → False", timeout_mgr._ready_timed_out(time.monotonic()) is False)

    # generation 隔离
    gen_mgr = lc.RosChildManager(_Cfg(), BridgeState(), status=None)
    pa = _FakeProc()
    gen_mgr._proc = pa
    gen_mgr._generation = 5
    check("generation 匹配 → 当前", gen_mgr._is_current(pa, 5) is True)
    check("旧 generation 事件被拒绝", gen_mgr._is_current(pa, 4) is False)
    check("旧 proc 事件被拒绝", gen_mgr._is_current(_FakeProc(), 5) is False)

    # P0：clear_ros_telemetry 绝不清任务锁（reported 与 lock 分离）
    lock_state = BridgeState()
    check("task A acquire 成功", lock_state.acquire_task("task-A") is True)
    lock_state.apply_status({"active_task_id": "task-A"})
    lock_state.clear_ros_telemetry()
    check("clear telemetry 清 reported_active_task_id", lock_state.reported_active_task_id is None)
    check("clear telemetry 绝不清 task_lock_id", lock_state.task_lock_id == "task-A")
    check("task B 仍 ACTIVE_TASK_CONFLICT", lock_state.acquire_task("task-B") is False)

    # P1：backoff 指数增长，且只有 adapter ready 才重置
    bk_mgr = lc.RosChildManager(_Cfg(), BridgeState(), status=None)
    bk_mgr._bump_backoff()
    b1 = bk_mgr._spawn_backoff
    bk_mgr._bump_backoff()
    b2 = bk_mgr._spawn_backoff
    bk_mgr._bump_backoff()
    b3 = bk_mgr._spawn_backoff
    check("backoff 指数增长 1→2→4", b1 == 2.0 and b2 == 4.0 and b3 == 8.0)
    bk_mgr._spawn_backoff = 30.0
    bk_mgr._handle_event({"type": "ready", "ok": True, "command_publisher": True,
                          "feedback": True, "providers": {"battery": True}})
    check("adapter ready 后 backoff 重置为 MIN", bk_mgr._spawn_backoff == lc._SPAWN_BACKOFF_MIN_S)

    # P1：node_ready=true 但 command adapter 未就绪 → READY timeout 仍触发
    half_mgr = lc.RosChildManager(_Cfg(), BridgeState(), status=None)
    half_mgr._proc = _FakeProc()
    half_mgr._spawn_time = time.monotonic() - 20
    half_mgr.node_ready = True
    half_mgr.command_publisher_ready = False
    half_mgr.feedback_ready = False
    check("node_ready=true 但 adapter_ready=false → READY timeout True",
          half_mgr._ready_timed_out(time.monotonic()) is True)

    print("=== IPC 隔离 / 最小权限 ===")
    from firebot_bridge.ros_adapter import normalize_status
    import firebot_bridge.ros.lifecycle as lc2

    ns = normalize_status({"type": "feedback", "feedback": {"state": "ACCEPTED"}})
    check("status 白名单：type 不可被覆盖", ns.get("type") == "status" and "feedback" not in ns)
    check("status 白名单：恶意 feedback 字段被丢弃", "feedback" not in ns)
    ns2 = normalize_status({"mode": "idle"})
    check("status mode uppercase", ns2.get("mode") == "IDLE")
    ns3 = normalize_status({"mode": "WARP"})
    check("status 非法 mode 被丢弃", "mode" not in ns3)
    ns4 = normalize_status({"estop_active": True, "active_task_id": "t1"})
    check("status estop/task 透传", ns4.get("estop_active") is True and ns4.get("active_task_id") == "t1")
    ns5 = normalize_status({"estop_active": "false"})
    check("estop 字符串 'false' 被丢弃（不误判 True）", "estop_active" not in ns5)
    ns6 = normalize_status({"active_task_id": 123})
    check("active_task_id 非 str/None 被丢弃", "active_task_id" not in ns6)

    saved_pw = os.environ.get("FIREBOT_MQTT_PASSWORD")
    os.environ["FIREBOT_MQTT_PASSWORD"] = "super-secret"
    try:
        env = lc2.build_child_env()
        check("child env 剥离 MQTT password", "FIREBOT_MQTT_PASSWORD" not in env)
    finally:
        if saved_pw is None:
            os.environ.pop("FIREBOT_MQTT_PASSWORD", None)
        else:
            os.environ["FIREBOT_MQTT_PASSWORD"] = saved_pw

    print("=== T6 SIGTERM/SIGINT 优雅停机 ===")
    from firebot_bridge.main import make_stop_handler

    stop = threading.Event()
    make_stop_handler(stop)(15, None)
    check("SIGTERM handler 设置 stop", stop.is_set() is True)

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
