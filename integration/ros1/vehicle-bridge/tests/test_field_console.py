#!/usr/bin/env python3
"""Field trace + console 单测（无 ROS/MQTT 依赖）。"""
from __future__ import annotations

import contextlib
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
    # TRACE-01 critical 恒记录（disabled 仍输出）；telemetry 受 enabled 门控
    buf, handler, ft = _capture()
    FieldTrace(False).emit("mqtt.connected", broker="x")
    check("TRACE-01a critical disabled 仍输出", len(_trace_lines(buf)) == 1)
    _release(handler, ft)

    buf, handler, ft = _capture()
    FieldTrace(False).emit("ros.battery.rx", battery=67.5)
    check("TRACE-01b telemetry disabled 无输出", _trace_lines(buf) == [])
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

    print("=== 微修：wall clock / 因果顺序 / 视觉语义 / LINK / status 签名 ===")
    # wall time 毫秒显示（wall 只用于显示）
    c = field_console.FieldConsole()
    out = c.render({"event": "mqtt.connected", "level": "ok", "wall": 1700000000.136, "broker": "x"})
    check("wall 毫秒显示 HH:MM:SS.mmm", out is not None and ".136" in out)
    check("mono 不再被 localtime 误用", out is not None and ".136" in out)

    # feedback 因果顺序：ros.feedback.rx 必须先于 mqtt.command_ack.tx
    import firebot_bridge.ros.lifecycle as lc
    from firebot_bridge.state import BridgeState

    buf, handler, ft = _capture()
    trace = FieldTrace(True)
    state = BridgeState()
    mgr = lc.RosChildManager(_Cfg(), state, status=None, trace=trace)

    def _on_fb(fb):
        trace.emit("mqtt.command_ack.tx", level="tx", status="accepted", command_id=fb.get("command_id"))

    mgr.set_on_feedback(_on_fb)
    mgr._handle_event({"type": "feedback", "feedback": {"command_id": "c1", "state": "ACCEPTED"}})
    lines = _trace_lines(buf)
    rx = next((i for i, l in enumerate(lines) if "ros.feedback.rx" in l), None)
    ack = next((i for i, l in enumerate(lines) if "mqtt.command_ack.tx" in l), None)
    check("feedback trace 在 ACK trace 前", rx is not None and ack is not None and rx < ack)
    _release(handler, ft)

    # 视觉语义
    check("master AVAILABLE green", field_console.effective_flag("ros.master.changed", {"state": "AVAILABLE"}, "ok") == "ok")
    check("master UNAVAILABLE non-green", field_console.effective_flag("ros.master.changed", {"state": "UNAVAILABLE"}, "ok") == "warn")
    check("adapter READY green", field_console.effective_flag("ros.adapter.changed", {"state": "READY"}, "ok") == "ok")
    check("adapter NOT_READY non-green", field_console.effective_flag("ros.adapter.changed", {"state": "NOT_READY"}, "ok") == "warn")

    # header FIELD TRACE ON/OFF 动态
    h_off = field_console.FieldConsole().header({"field_trace_enabled": False})
    check("header FIELD TRACE OFF", "FIELD TRACE OFF" in h_off)
    h_on = field_console.FieldConsole().header({"field_trace_enabled": True})
    check("header FIELD TRACE ON", "FIELD TRACE ON" in h_on)

    # LINK 节点模型无歧义
    c = field_console.FieldConsole()
    c.mqtt = False
    c.master = True
    c.adapter = True
    link = c._link_line()
    check("MQTT 断连 LINK 无 MQTT ●", "MQTT ●" not in link)
    check("MQTT 断连 LINK 有 MQTT ×", "MQTT ×" in link)
    c.mqtt = True
    c.master = False
    link = c._link_line()
    check("master unavailable LINK 无 MASTER ●", "MASTER ●" not in link)

    # status 签名完整：mode / active_task_id 变化即使 battery 不变也产生 trace
    rec2 = {"publish_calls": 0}
    _install_fake_paho(rec2)
    from firebot_bridge.mqtt_client import MqttClient as _MC

    buf, handler, ft = _capture()
    mqtt2 = _MC(_Cfg(), _Identity(), _Proto(), lambda c: None, status=None, trace=FieldTrace(True))
    mqtt2._trace_publish({"type": "status", "battery": 67.5, "mode": "IDLE"})
    mqtt2._trace_publish({"type": "status", "battery": 67.5, "mode": "PATROL"})
    check("status mode 变化仍产生 trace", len(_trace_lines(buf)) == 2)
    _release(handler, ft)

    buf, handler, ft = _capture()
    mqtt3 = _MC(_Cfg(), _Identity(), _Proto(), lambda c: None, status=None, trace=FieldTrace(True))
    mqtt3._trace_publish({"type": "status", "battery": 67.5, "active_task_id": None})
    mqtt3._trace_publish({"type": "status", "battery": 67.5, "active_task_id": "t1"})
    check("status active_task_id 变化仍产生 trace", len(_trace_lines(buf)) == 2)
    _release(handler, ft)

    # install.sh 安装 verify.sh
    install_text = (ROOT / "install.sh").read_text(encoding="utf-8")
    check("install.sh 复制 verify.sh", 'verify.sh' in install_text)

    print("=== 收口：safe_text / wall 进位 / watcher / raw ===")
    # safe_text 显示层净化：控制字符不进入终端
    check("CONTROL_CHAR_SANITIZED ESC 被替换", "\x1b" not in field_console.safe_text("\x1b[2Jclear"))
    check("CONTROL_CHAR_SANITIZED C0 被替换", "\x07" not in field_console.safe_text("be\x07ep"))
    check("NEWLINE_SANITIZED CR/LF/TAB -> space", field_console.safe_text("a\nb\rc\td") == "a b c d")
    check("LONG_VALUE_BOUNDED 最大 160", len(field_console.safe_text("x" * 500)) == 160)
    check("CHINESE_PRESERVED 中文保留", field_console.safe_text("中文巡检") == "中文巡检")

    # wall 时间毫秒进位：.9996 应进位到下一秒 .000
    c = field_console.FieldConsole()
    carry = c.render({"event": "mqtt.connected", "level": "ok", "wall": 1700000000.9996, "broker": "x"})
    exact = c.render({"event": "mqtt.connected", "level": "ok", "wall": 1700000001.000, "broker": "x"})
    check("WALL_CARRY_CORRECT .9996 进位到下一秒",
          carry is not None and exact is not None and carry.split()[0] == exact.split()[0])
    check("WALL_CARRY_CORRECT 毫秒为 000", carry is not None and carry.split()[0].endswith(".000"))

    # watcher：单一事实源 events.jsonl（历史回放 + 实时跟随），不重放 journal 历史；inactive service 退出
    watch_text = (ROOT / "watch-bridge.sh").read_text(encoding="utf-8")
    check("WATCH 单一事实源 events.jsonl", "events.jsonl" in watch_text)
    check("WATCH 历史+实时 tail -F --jsonl", "-F" in watch_text and "--jsonl" in watch_text)
    check("WATCH 无 journalctl 命令（仅注释）", "journalctl -" not in watch_text)
    check("WATCH_INACTIVE_SERVICE_EXIT 有 exit 2", "exit 2" in watch_text)
    check("WATCH_INACTIVE_SERVICE_EXIT 有 ERROR", "ERROR" in watch_text)

    # raw 模式：只透传 journal，不输出 header/LINK
    _stdin, _argv = sys.stdin, sys.argv
    try:
        sys.stdin = io.StringIO("RAW-LINE-1\nRAW-LINE-2\n")
        sys.argv = ["field_console.py", "--raw"]
        raw_buf = io.StringIO()
        with contextlib.redirect_stdout(raw_buf):
            rc = field_console.main()
    finally:
        sys.stdin, sys.argv = _stdin, _argv
    raw_out = raw_buf.getvalue()
    check("RAW_MODE_NO_HEADER 无 header", "FIREBOT VEHICLE BRIDGE" not in raw_out)
    check("RAW_MODE_NO_HEADER 无 LINK", "LINK" not in raw_out)
    check("RAW_MODE_NO_HEADER 纯透传", raw_out == "RAW-LINE-1\nRAW-LINE-2\n")
    check("RAW_MODE_NO_HEADER rc=0", rc == 0)

    print("=== 2026-08-31 现场验证回归 ===")
    # ros.feedback.rx 关联 cmd：lifecycle 用 command_id → cmd 补观测事件（不改协议）
    buf, handler, ft = _capture()
    trace = FieldTrace(True)
    state = BridgeState()
    mgr = lc.RosChildManager(_Cfg(), state, status=None, trace=trace)
    mgr.command_publisher_ready = True
    mgr.feedback_ready = True

    class _FakeStdin:
        def __init__(self):
            self.writes = []

        def write(self, s):
            self.writes.append(s)

        def flush(self):
            pass

    class _FakeProc:
        def __init__(self):
            self.stdin = _FakeStdin()
            self.pid = 123

        def poll(self):
            return None

    proc = _FakeProc()
    mgr._proc = proc
    mgr._stdin = proc.stdin
    ok = mgr.publish_command({"cmd": "patrol", "command_id": "c1", "task_id": "t1"})
    check("publish_command 成功且记录 cmd", ok is True and mgr._cmd_by_id.get("c1") == "patrol")
    mgr._handle_event({"type": "feedback", "feedback": {
        "command_id": "c1", "state": "REJECTED",
        "reason_code": "COMMAND_REJECTED", "message": "NAV_EXECUTION_NOT_READY",
    }})
    lines = _trace_lines(buf)
    rx = next((i for i, l in enumerate(lines) if "ros.feedback.rx" in l), None)
    check("ros.feedback.rx 事件存在", rx is not None)
    fb_payload = json.loads(lines[rx].split(TRACE_PREFIX, 1)[1])
    check("ros.feedback.rx 含 cmd", fb_payload.get("cmd") == "patrol")
    check("ros.feedback.rx 含 message（不丢失）", fb_payload.get("message") == "NAV_EXECUTION_NOT_READY")
    check("ros.feedback.rx 含 reason_code", fb_payload.get("reason_code") == "COMMAND_REJECTED")
    _release(handler, ft)

    # field_console 中文：PATROL_START + REJECTED + NAV_EXECUTION_NOT_READY → 完整中文
    c = field_console.FieldConsole(lang="zh")
    out = c.render({"event": "ros.feedback.rx", "level": "rx", "cmd": "patrol", "state": "REJECTED",
                    "command_id": "c1", "reason_code": "COMMAND_REJECTED", "message": "NAV_EXECUTION_NOT_READY"})
    check("zh 显示 开始巡检：已拒绝", out is not None and "开始巡检：已拒绝" in out)
    check("zh 显示 导航执行环境未就绪", out is not None and "导航执行环境未就绪" in out)

    # FieldTrace 三级解耦：critical 恒记录；telemetry 落盘受 telemetry_log_enabled、刷屏受 enabled
    class _FakeRecorder:
        def __init__(self):
            self.records = []

        def enqueue(self, record, imp):
            self.records.append((record, imp))

    rec = _FakeRecorder()
    tr = FieldTrace(False, telemetry_log_enabled=True, recorder=rec)
    buf, handler, ft = _capture()
    tr.emit("ros.battery.rx", battery=67.5)
    check("telemetry enabled=False 不刷屏但仍落盘", _trace_lines(buf) == [] and len(rec.records) == 1)
    _release(handler, ft)

    rec2 = _FakeRecorder()
    tr2 = FieldTrace(True, telemetry_log_enabled=False, recorder=rec2)
    buf, handler, ft = _capture()
    tr2.emit("ros.battery.rx", battery=67.5)
    check("telemetry_log_enabled=False 不落盘", len(rec2.records) == 0)
    _release(handler, ft)

    rec3 = _FakeRecorder()
    tr3 = FieldTrace(False, telemetry_log_enabled=False, recorder=rec3)
    buf, handler, ft = _capture()
    tr3.emit("mqtt.connected", broker="x")
    check("critical 恒记录+刷屏（不受两开关影响）", len(rec3.records) == 1 and len(_trace_lines(buf)) == 1)
    _release(handler, ft)

    # EventRecorder 双队列分离：critical → event 队列；telemetry → telemetry 队列
    import tempfile as _tempfile

    from firebot_bridge.event_recorder import EventRecorder

    cfg = _Cfg()
    cfg.events_dir = _tempfile.mkdtemp()
    cfg.event_queue_size = 100
    rec4 = EventRecorder(cfg)
    rec4.enqueue({"event": "mqtt.command.rx"}, "critical")
    rec4.enqueue({"event": "ros.battery.rx"}, "telemetry")
    check("event/telemetry 队列分离", rec4._event_queue.qsize() == 1 and rec4._telemetry_queue.qsize() == 1)

    # state.py：task_lock_id 与 reported_active_task_id 分离，clear 不清 task_lock
    state2 = BridgeState()
    state2.acquire_task("task-lock-1")
    state2.apply_status({"mode": "PATROL", "estop_active": False, "active_task_id": "reported-task-9"})
    snap = state2.snapshot_telemetry()
    check("snapshot active_task_id = reported（非 task_lock）", snap["active_task_id"] == "reported-task-9")
    state2.clear_ros_telemetry()
    check("clear 清 reported_active_task_id", state2.reported_active_task_id is None)
    check("clear 不清 task_lock_id", state2.task_lock_id == "task-lock-1")

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
