#!/usr/bin/env python3
"""Field console：从 stdin 读取 journal lines，渲染 FBTRACE 结构化事件。

只观察、只渲染，绝不启动/重启 Bridge、绝不修改配置、绝不回写 status。

颜色只存在于本 viewer；journal 原始记录永远无 ANSI。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

TRACE_PREFIX = "FBTRACE\t"

# 固定颜色语义（仅 viewer 使用）
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_BLUE = "\033[34m"
_DIM = "\033[2m"
_RESET = "\033[0m"

# event 名 → (PATH, EVENT 标签, level flag)
_EVENT_TABLE = {
    "bridge.started": ("BRIDGE", "STARTED", "ok"),
    "bridge.stopping": ("BRIDGE", "STOPPING", "ok"),
    "mqtt.connected": ("MQTT", "CONNECTED", "ok"),
    "mqtt.connect_failed": ("MQTT", "CONNECT_FAILED", "warn"),
    "mqtt.disconnected": ("MQTT", "DISCONNECTED", "warn"),
    "mqtt.subscribed": ("MQTT", "SUBSCRIBED", "ok"),
    "mqtt.command.rx": ("MQTT", "COMMAND", "rx"),
    "mqtt.command.ignored": ("MQTT", "IGNORED", "debug"),
    "mqtt.availability.tx": ("MQTT", "AVAILABILITY", "tx"),
    "mqtt.capabilities.tx": ("MQTT", "CAPABILITIES", "tx"),
    "mqtt.heartbeat.tx": ("MQTT", "HEARTBEAT", "debug"),
    "mqtt.status.tx": ("MQTT", "STATUS", "tx"),
    "mqtt.sensor.tx": ("MQTT", "SENSOR", "tx"),
    "mqtt.location.tx": ("MQTT", "LOCATION", "tx"),
    "mqtt.command_ack.tx": ("MQTT", "COMMAND_ACK", "tx"),
    "mqtt.task_status.tx": ("MQTT", "TASK_STATUS", "tx"),
    "ros.master.changed": ("ROS", "MASTER", "ok"),
    "ros.child.spawned": ("ROS", "CHILD_SPAWNED", "ok"),
    "ros.child.exited": ("ROS", "CHILD_EXITED", "warn"),
    "ros.child.ready_timeout": ("ROS", "READY_TIMEOUT", "warn"),
    "ros.child.backoff": ("ROS", "BACKOFF", "warn"),
    "ros.adapter.changed": ("ROS", "ADAPTER", "ok"),
    "ros.command.tx": ("ROS", "COMMAND", "tx"),
    "ros.command.tx_failed": ("ROS", "COMMAND_TX_FAILED", "warn"),
    "ros.feedback.rx": ("ROS", "FEEDBACK", "rx"),
    "ros.battery.rx": ("ROS", "BATTERY", "rx"),
    "ros.smoke.rx": ("ROS", "SMOKE", "rx"),
    "ros.status.rx": ("ROS", "STATUS", "rx"),
    "ros.location.rx": ("ROS", "LOCATION", "rx"),
}

_FLAG_SYMBOL = {
    "ok": "●",
    "warn": "⚠",
    "error": "×",
    "rx": "↓",
    "tx": "↑",
    "debug": "·",
}

_LEVEL_COLOR = {
    "ok": _GREEN,
    "warn": _YELLOW,
    "error": _RED,
    "rx": _CYAN,
    "tx": _BLUE,
    "debug": _DIM,
}


def short_id(value, full=False):
    if value is None:
        return None
    s = str(value)
    return s if full else s[:8]


def parse_trace(line: str):
    """从一行日志里解析出 FBTRACE 事件 dict；不是 trace 返回 None。"""
    marker = line.find(TRACE_PREFIX)
    if marker < 0:
        return None
    raw = line[marker + len(TRACE_PREFIX):]
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


class FieldConsole:
    def __init__(self, *, full_id=False, use_color=False, verbose=False, compact=False):
        self.full_id = full_id
        self.use_color = use_color
        self.verbose = verbose
        self.compact = compact
        self.mqtt = False
        self.master = False
        self.adapter = False

    def _c(self, color, text):
        return f"{color}{text}{_RESET}" if self.use_color else text

    def _flag(self, level):
        return self._c(_LEVEL_COLOR.get(level, ""), _FLAG_SYMBOL.get(level, "·"))

    def _detail(self, event, ev) -> str:
        name = event
        if name == "mqtt.connected":
            return f"{ev.get('broker')}"
        if name == "mqtt.subscribed":
            return f"{ev.get('topic')} qos={ev.get('qos')}"
        if name == "mqtt.command.rx":
            return f"{ev.get('cmd')}  cid={short_id(ev.get('command_id'), self.full_id)} task={short_id(ev.get('task_id'), self.full_id)}"
        if name == "mqtt.availability.tx":
            return f"{ev.get('state')}"
        if name == "mqtt.capabilities.tx":
            return f"commands={ev.get('commands')} sensors={ev.get('sensors')}"
        if name == "mqtt.status.tx":
            return f"battery={ev.get('battery')} mode={ev.get('mode')}"
        if name == "mqtt.sensor.tx":
            return f"smoke={ev.get('smoke')}"
        if name == "mqtt.command_ack.tx":
            return f"{ev.get('status')}  cid={short_id(ev.get('command_id'), self.full_id)} latency={ev.get('latency_ms')}ms"
        if name == "mqtt.task_status.tx":
            return f"{ev.get('status')} {ev.get('phase')} {ev.get('progress')}%"
        if name == "mqtt.command.ignored":
            return f"{ev.get('reason')}"
        if name == "ros.master.changed":
            prev = ev.get("previous")
            return f"{prev} -> {ev.get('state')}" if prev is not None else str(ev.get("state"))
        if name == "ros.adapter.changed":
            prev = ev.get("previous")
            base = f"{prev} -> {ev.get('state')}" if prev is not None else str(ev.get("state"))
            return f"{base}  node={ev.get('node')} pub={ev.get('publisher')} fb={ev.get('feedback')}"
        if name == "ros.child.spawned":
            return f"pid={ev.get('pid')} gen={ev.get('generation')}"
        if name == "ros.child.exited":
            return f"pid={ev.get('pid')} gen={ev.get('generation')} rc={ev.get('returncode')}"
        if name == "ros.child.ready_timeout":
            return f"pid={ev.get('pid')} timeout={ev.get('timeout_s')}s"
        if name == "ros.child.backoff":
            return f"next≈{ev.get('seconds')}s"
        if name == "ros.command.tx":
            return f"{ev.get('cmd')}  cid={short_id(ev.get('command_id'), self.full_id)} latency={ev.get('latency_ms')}ms"
        if name == "ros.command.tx_failed":
            return f"{ev.get('cmd')}  reason={ev.get('reason')} cid={short_id(ev.get('command_id'), self.full_id)}"
        if name == "ros.feedback.rx":
            return f"{ev.get('state')}  cid={short_id(ev.get('command_id'), self.full_id)} latency={ev.get('latency_ms')}ms"
        if name == "ros.battery.rx":
            return f"{ev.get('battery')} %"
        if name == "ros.smoke.rx":
            return f"{ev.get('smoke')}"
        if name == "ros.status.rx":
            return f"mode={ev.get('mode')} estop={ev.get('estop_active')} task={ev.get('active_task_id')}"
        if name == "ros.location.rx":
            return f"x={ev.get('x')} y={ev.get('y')} theta={ev.get('theta')}"
        if name == "bridge.started":
            return f"vehicle={ev.get('vehicle')} boot={short_id(ev.get('boot'), self.full_id)} proto={ev.get('protocol')}"
        if name == "bridge.stopping":
            return f"boot={short_id(ev.get('boot'), self.full_id)}"
        return ""

    def _update_link(self, event, ev):
        before = (self.mqtt, self.master, self.adapter)
        if event == "mqtt.connected":
            self.mqtt = True
        elif event in ("mqtt.disconnected", "mqtt.connect_failed"):
            self.mqtt = False
        elif event == "ros.master.changed":
            self.master = ev.get("state") == "AVAILABLE"
        elif event == "ros.adapter.changed":
            self.adapter = ev.get("state") == "READY"
        after = (self.mqtt, self.master, self.adapter)
        return before != after

    def _link_line(self):
        p1 = "●" if self.mqtt else "×"
        p3 = "●" if self.adapter else "○"
        p4 = "●" if self.master else "○"
        return f"LINK  SERVER {p1}──MQTT●──BRIDGE{p3}──ROS{p4} VEHICLE"

    def render(self, event: dict):
        """渲染单个 FBTRACE 事件，返回字符串或 None（verbose 过滤的 debug 事件）。"""
        name = event.get("event", "unknown")
        level = event.get("level", "info")
        if name not in _EVENT_TABLE:
            if not self.verbose:
                return None
            return self._c(_DIM, f"  UNKNOWN  {name} {json.dumps(event, ensure_ascii=False)}")
        path, label, flag = _EVENT_TABLE[name]
        # debug 级别只在 verbose 显示
        if flag == "debug" and not self.verbose:
            return None
        ts = time.strftime("%H:%M:%S", time.localtime(event.get("mono", 0)))
        detail = self._detail(name, event)
        if self.compact:
            line = f"{ts} {self._flag(flag)} {path} {label} {detail}"
        else:
            line = f"{ts}  {self._flag(flag)} {path:6}  {label:16}  {detail}"
        link = ""
        if self._update_link(name, event):
            link = "\n" + self._link_line()
        return line + link

    def header(self, status: dict):
        """渲染启动快照头。"""
        boot = short_id(status.get("boot_id") or "?", self.full_id)
        vehicle = status.get("vehicle_id") or "?"
        proto = status.get("protocol_version") or "?"
        pid = status.get("pid") or "?"
        mqtt = "CONNECTED" if status.get("mqtt_connected") else "OFFLINE"
        master = "AVAILABLE" if status.get("ros_master_available") else "WAIT"
        adapter = "READY" if status.get("ros_adapter_ready") else "NOT READY"
        stub = status.get("stub_mode")
        cmds = status.get("supported_commands") or []
        sensors = status.get("sensors") or []
        loc = status.get("location_enabled")

        def row(left_k, left_v, right_k, right_v):
            return f"│ {left_k:<10}{left_v:<22} {right_k:<12}{right_v:<24}│"

        lines = [
            "┌────────────────────────────────────────────────────────────────────────────┐",
            "│ FIREBOT VEHICLE BRIDGE                                    FIELD TRACE ON  │",
            "├────────────────────────────────────────────────────────────────────────────┤",
            row("Vehicle", vehicle, "Protocol", proto),
            row("Boot", boot, "PID", str(pid)),
            row("MQTT", mqtt, "ROS Master", master),
            row("Adapter", adapter, "Stub", "ON" if stub else "OFF"),
        ]
        if cmds:
            lines.append(self._c(_YELLOW, f"│ Commands  ENABLED: {','.join(cmds):<46}          │"))
        else:
            lines.append("│ Commands  NONE                                                              │")
        if sensors:
            lines.append(f"│ Sensors   {','.join(sensors):<66}│")
        else:
            lines.append("│ Sensors   NONE                                                              │")
        lines.append(f"│ Location  {'ON' if loc else 'OFF':<22}  Control    NOT IMPLEMENTED            │")
        if stub:
            lines.append(self._c(_YELLOW, "│ STUB MODE ENABLED                                                          │"))
        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append("│ SERVER ──MQTT──▶ BRIDGE ──ROS──▶ VEHICLE                                 │")
        lines.append("└────────────────────────────────────────────────────────────────────────────┘")
        return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Firebot Bridge field console")
    parser.add_argument("--status-file", default="/run/firebot-bridge/status.json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--full-id", action="store_true")
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    use_color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    columns = shutil.get_terminal_size((120, 24)).columns
    console = FieldConsole(
        full_id=args.full_id,
        use_color=use_color,
        verbose=args.verbose,
        compact=columns < 90,
    )

    status = {}
    try:
        with open(args.status_file, encoding="utf-8") as handle:
            status = json.load(handle)
    except Exception:  # noqa: BLE001
        status = {}

    print(console.header(status))
    console.mqtt = bool(status.get("mqtt_connected"))
    console.master = bool(status.get("ros_master_available"))
    console.adapter = bool(status.get("ros_adapter_ready"))
    print(console._link_line())

    for line in sys.stdin:
        if args.raw:
            sys.stdout.write(line)
            sys.stdout.flush()
            continue
        event = parse_trace(line)
        if event is None:
            continue
        try:
            rendered = console.render(event)
        except Exception:  # noqa: BLE001
            rendered = None
        if rendered:
            print(rendered)
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
