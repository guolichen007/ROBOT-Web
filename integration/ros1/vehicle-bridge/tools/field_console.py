#!/usr/bin/env python3
"""Field console：从 stdin 读取 journal lines，渲染 FBTRACE 结构化事件。

只观察、只渲染，绝不启动/重启 Bridge、绝不修改配置、绝不回写 status。

颜色只存在于本 viewer；journal 原始记录永远无 ANSI。
"""
from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import sys
import time
from datetime import datetime

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
    "ros.battery.stale": ("ROS", "BATTERY_STALE", "warn"),
    "ros.battery.recovered": ("ROS", "BATTERY_RECOVERED", "ok"),
    "ros.smoke.stale": ("ROS", "SMOKE_STALE", "warn"),
    "ros.smoke.recovered": ("ROS", "SMOKE_RECOVERED", "ok"),
}

# 默认隐藏（仅 --verbose 显示）的事件：技术细节 + telemetry
_VERBOSE_ONLY_EVENTS = {
    "mqtt.subscribed",
    "mqtt.availability.tx",
    "mqtt.capabilities.tx",
    "ros.child.spawned",
    "ros.child.backoff",
    "mqtt.command.ignored",
    "mqtt.heartbeat.tx",
    "mqtt.status.tx",
    "mqtt.sensor.tx",
    "mqtt.location.tx",
    "ros.battery.rx",
    "ros.smoke.rx",
    "ros.status.rx",
    "ros.location.rx",
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

# 中文显示层映射（只影响 viewer 渲染，events.jsonl 保留原始值）
_ZH_PATH = {
    "mqtt.command.rx": "服务器→车辆",
    "ros.command.tx": "Bridge→ROS",
    "ros.feedback.rx": "ROS控制",
    "mqtt.command_ack.tx": "车辆→服务器",
    "mqtt.task_status.tx": "任务",
    "mqtt.connected": "服务器",
    "mqtt.connect_failed": "服务器",
    "mqtt.disconnected": "服务器",
    "ros.master.changed": "ROS主节点",
    "ros.adapter.changed": "ROS适配器",
    "bridge.started": "Bridge",
    "bridge.stopping": "Bridge",
    "ros.battery.stale": "电量数据源",
    "ros.battery.recovered": "电量数据源",
    "ros.smoke.stale": "烟雾数据源",
    "ros.smoke.recovered": "烟雾数据源",
}

_ZH_LABEL = {
    "mqtt.command.rx": "收到",
    "ros.command.tx": "已转发",
    "ros.command.tx_failed": "发送失败",
    "ros.feedback.rx": "",
    "mqtt.command_ack.tx": "控制结果已发送",
    "mqtt.task_status.tx": "任务状态已发送",
    "mqtt.connected": "已连接",
    "mqtt.connect_failed": "连接失败",
    "mqtt.disconnected": "已断开",
    "ros.master.changed": "变化",
    "ros.adapter.changed": "变化",
    "bridge.started": "启动",
    "bridge.stopping": "停止",
    "ros.battery.stale": "已超时",
    "ros.battery.recovered": "已恢复",
    "ros.smoke.stale": "已超时",
    "ros.smoke.recovered": "已恢复",
}

_ZH_STATE = {
    "accepted": "接受",
    "rejected": "拒绝",
    "executing": "执行中",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "取消",
}

_ZH_REASON = {
    "COMMAND_REJECTED": "指令被拒绝",
    "NAV_EXECUTION_NOT_READY": "导航执行环境未就绪",
    "BRIDGE_ADAPTER_NOT_CONNECTED": "桥接适配器未连接",
    "ACTIVE_TASK_CONFLICT": "任务冲突",
    "COMMAND_UNSUPPORTED": "指令不支持",
    "COMMAND_EXPIRED": "指令已过期",
    "COMMAND_INVALID": "指令无效",
}

# 命令名 → 操作员中文（只影响 viewer，内部 cmd 原码保留在 events.jsonl）
_ZH_CMD = {
    "patrol": "开始巡检",
    "PATROL_START": "开始巡检",
    "PATROL": "开始巡检",
}


def _zh_cmd(cmd):
    """命令名中文显示；未知命令原样返回。"""
    if cmd is None:
        return ""
    return _ZH_CMD.get(str(cmd), str(cmd))


def _zh_reason(ev):
    """提取反馈拒绝原因（message 优先，否则 reason_code），并翻译。"""
    msg = ev.get("message") or ev.get("reason_code")
    if not msg:
        return None
    return _ZH_REASON.get(str(msg), str(msg))


def _event_wall(event: dict) -> float:
    """兼容新 schema（timestamp_utc）与旧 bridge.log（wall）的时间解析。"""
    ts = event.get("timestamp_utc")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            pass
    wall = event.get("wall")
    if wall is not None:
        try:
            return float(wall)
        except (ValueError, TypeError):
            pass
    return time.time()


def short_id(value, full=False):
    if value is None:
        return None
    s = str(value)
    return s if full else s[:8]


def safe_text(value, max_len=160):
    """显示层净化：替换控制字符，防止外部字段破坏终端可信显示。

    CR/LF/TAB -> 空格；其余 C0/DEL/C1 -> '?'；中文及其它可见字符保留。
    只作用于 viewer 显示，绝不修改任何协议/数据。
    """
    if value is None:
        return ""

    out = []

    for ch in str(value):
        code = ord(ch)

        if ch in "\r\n\t":
            out.append(" ")
        elif code < 32 or 127 <= code < 160:
            out.append("?")
        else:
            out.append(ch)

        if len(out) >= max_len:
            break

    return "".join(out)


def effective_flag(name, ev, default):
    """按事件内容动态判定视觉状态：transition 到健康才绿，否则黄。"""
    if name == "ros.master.changed":
        return "ok" if ev.get("state") == "AVAILABLE" else "warn"
    if name == "ros.adapter.changed":
        return "ok" if ev.get("state") == "READY" else "warn"
    return default


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


def parse_jsonl(line: str):
    """从一行裸 JSON（events.jsonl 格式）解析事件 dict；失败返回 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


class FieldConsole:
    def __init__(self, *, full_id=False, use_color=False, verbose=False, compact=False, lang="en"):
        self.full_id = full_id
        self.use_color = use_color
        self.verbose = verbose
        self.compact = compact
        self.lang = lang
        self.mqtt = False
        self.master = False
        self.adapter = False
        # 会话级去重：同一 (boot_id, event_seq) 只显示一次
        self._seen_keys = set()

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
            return f"battery={ev.get('battery')} mode={ev.get('mode')} estop={ev.get('estop_active')} task={ev.get('active_task_id')}"
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

    def _detail_zh(self, event, ev) -> str:
        """中文显示层（只影响 viewer，原始值保留在 events.jsonl）。"""
        name = event
        if name == "mqtt.command.rx":
            cmd = _zh_cmd(ev.get("cmd"))
            # 指令编号格式固定且不长，完整显示以便对账；任务编号可短显示
            cid = ev.get("command_id")
            tid = short_id(ev.get("task_id"), self.full_id)
            parts = [cmd]
            if cid:
                parts.append(f"指令编号：{cid}")
            if tid:
                parts.append(f"任务编号：{tid}")
            return "  ".join(parts)
        if name == "ros.command.tx":
            return _zh_cmd(ev.get("cmd"))
        if name == "ros.command.tx_failed":
            return f"{_zh_cmd(ev.get('cmd'))}（{ev.get('reason') or ''}）"
        if name == "ros.feedback.rx":
            raw = str(ev.get("state") or "").lower()
            cmd = _zh_cmd(ev.get("cmd"))
            # 任务名随状态演进：未执行=命令名（开始巡检）；执行中=导航任务；终态=巡检任务
            if raw == "executing":
                subject = "导航任务"
            elif raw in ("completed", "failed", "cancelled"):
                subject = "巡检任务"
            else:
                subject = cmd or "巡检任务"
            if raw == "accepted":
                return f"{subject}：已接受"
            if raw == "rejected":
                reason = _zh_reason(ev)
                base = f"{subject}：已拒绝"
                return f"{base}，原因：{reason}" if reason else base
            if raw == "executing":
                return f"{subject}：执行中"
            if raw == "completed":
                return f"{subject}：已完成"
            if raw == "failed":
                reason = _zh_reason(ev)
                base = f"{subject}：执行失败"
                return f"{base}，原因：{reason}" if reason else base
            if raw == "cancelled":
                return f"{subject}：已取消"
            state = _ZH_STATE.get(raw, ev.get("state") or "?")
            return f"{subject}：{state}" if subject else state
        if name == "mqtt.command_ack.tx":
            status = _ZH_STATE.get(str(ev.get("status") or "").lower(), ev.get("status") or "?")
            return f"{status}"
        if name == "mqtt.task_status.tx":
            status = _ZH_STATE.get(str(ev.get("status") or "").lower(), ev.get("status") or "?")
            phase = ev.get("phase") or ""
            return f"{status} {phase}".strip()
        if name == "mqtt.connected":
            return f"{ev.get('broker') or ''}"
        if name == "mqtt.connect_failed":
            return f"{ev.get('broker') or ''}"
        if name == "mqtt.disconnected":
            return f"rc={ev.get('rc')}"
        if name == "ros.master.changed":
            prev = ev.get("previous")
            state = "可用" if ev.get("state") == "AVAILABLE" else "不可用"
            return state if prev is None else f"{'可用' if prev else '不可用'}→{state}"
        if name == "ros.adapter.changed":
            prev = ev.get("previous")
            state = "就绪" if ev.get("state") == "READY" else "未就绪"
            return state if prev is None else f"{'就绪' if prev else '未就绪'}→{state}"
        if name == "bridge.started":
            return f"vehicle={ev.get('vehicle')} boot={short_id(ev.get('boot'), self.full_id)}"
        if name == "bridge.stopping":
            return f"boot={short_id(ev.get('boot'), self.full_id)}"
        return self._detail(event, ev)

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
        mqtt = "●" if self.mqtt else "×"
        master = "●" if self.master else "○"
        adapter = "●" if self.adapter else "○"
        # 节点模型：每个 symbol 只表示紧邻节点自身状态，避免线段两边混 symbol
        return f"LINK  SERVER ── MQTT {mqtt} ── BRIDGE ● ── ROS MASTER {master} ── ADAPTER {adapter}"

    def _link_line_zh(self):
        mqtt = "●" if self.mqtt else "×"
        master = "●" if self.master else "○"
        adapter = "●" if self.adapter else "○"
        return f"链路  服务器 ── MQTT {mqtt} ── Bridge ● ── ROS主节点 {master} ── 适配器 {adapter}"

    def render(self, event: dict):
        """渲染单个 FBTRACE 事件，返回字符串或 None（verbose 过滤的 debug / 重复事件）。"""
        # 会话级去重：同一 (boot_id, event_seq) 只显示一次（防御 tail 重连/轮转重读）
        boot = event.get("boot_id")
        seq = event.get("event_seq")
        if boot is not None and seq is not None:
            key = (boot, seq)
            if key in self._seen_keys:
                return None
            self._seen_keys.add(key)
        name = event.get("event", "unknown")
        level = event.get("level", "info")
        if name not in _EVENT_TABLE:
            if not self.verbose:
                return None
            return self._c(_DIM, f"  UNKNOWN  {name} {json.dumps(event, ensure_ascii=False)}")
        path, label, flag = _EVENT_TABLE[name]
        flag = effective_flag(name, event, flag)
        # debug 级别只在 verbose 显示
        if flag == "debug" and not self.verbose:
            return None
        # 默认隐藏的技术细节 + telemetry
        if name in _VERBOSE_ONLY_EVENTS and not self.verbose:
            return None
        wall = _event_wall(event)
        total_ms = int(round(wall * 1000))
        seconds, millis = divmod(total_ms, 1000)
        ts = f"{time.strftime('%H:%M:%S', time.localtime(seconds))}.{millis:03d}"
        if self.lang == "zh":
            detail = safe_text(self._detail_zh(name, event))
            path = _ZH_PATH.get(name, path)
            label = _ZH_LABEL.get(name, label)
        else:
            detail = safe_text(self._detail(name, event))
        if self.compact or self.lang == "zh":
            if label:
                line = f"{ts} {self._flag(flag)} {path} {label} {detail}"
            else:
                line = f"{ts} {self._flag(flag)} {path} {detail}"
        else:
            line = f"{ts}  {self._flag(flag)} {path:6}  {label:16}  {detail}"
        link = ""
        if self._update_link(name, event):
            line_fn = self._link_line_zh if self.lang == "zh" else self._link_line
            link = "\n" + line_fn()
        return line + link

    def header(self, status: dict):
        """渲染启动快照头。"""
        boot = safe_text(short_id(status.get("boot_id") or "?", self.full_id))
        vehicle = safe_text(status.get("vehicle_id") or "?")
        proto = safe_text(status.get("protocol_version") or "?")
        pid = safe_text(status.get("pid") or "?")
        mqtt = "CONNECTED" if status.get("mqtt_connected") else "OFFLINE"
        master = "AVAILABLE" if status.get("ros_master_available") else "WAIT"
        adapter = "READY" if status.get("ros_adapter_ready") else "NOT READY"
        stub = status.get("stub_mode")
        cmds = [safe_text(c) for c in (status.get("supported_commands") or [])]
        sensors = [safe_text(s) for s in (status.get("sensors") or [])]
        loc = status.get("location_enabled")

        def row(left_k, left_v, right_k, right_v):
            return f"│ {left_k:<10}{left_v:<22} {right_k:<12}{right_v:<24}│"

        trace_label = "FIELD TRACE ON" if status.get("field_trace_enabled") else "FIELD TRACE OFF"
        lines = [
            "┌────────────────────────────────────────────────────────────────────────────┐",
            f"│ FIREBOT VEHICLE BRIDGE                                    {trace_label:<16}│",
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

    def header_zh(self, status: dict):
        """中文启动状态头（只影响 viewer，不改变任何协议/数据）。"""
        vehicle = safe_text(status.get("vehicle_id") or "?")
        boot = safe_text(short_id(status.get("boot_id") or "?", self.full_id))
        mqtt = "已连接" if status.get("mqtt_connected") else "未连接"
        ros = "正常" if status.get("ros_master_available") else "未就绪"
        adapter = "正常" if status.get("ros_adapter_ready") else "未就绪"
        bridge = "正常" if status.get("pid") else "异常"
        event_log = "正常" if status.get("event_logger_ready") else "异常"
        lines = [
            "========== Firebot 车端运行事件 ==========",
            f"设备：{vehicle}",
            f"服务器：{mqtt}",
            f"Bridge：{bridge}",
            f"ROS主节点：{ros}",
            f"Bridge ROS适配器：{adapter}",
            f"启动编号：{boot}",
            f"事件日志：{event_log}",
            "----------------------------------------",
        ]
        return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Firebot Bridge field console")
    parser.add_argument("--status-file", default="/run/firebot-bridge/status.json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--full-id", action="store_true")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--jsonl", action="store_true", help="stdin 为裸 JSON（events.jsonl 格式），非 journal FBTRACE 行")
    parser.add_argument("--lang", choices=["en", "zh"], default="en")
    parser.add_argument("--history", type=int, default=15,
                        help="历史回放条数，归入「最近事件」区；0=不分区，全部当作实时")
    args = parser.parse_args()

    if args.raw:
        # 纯透传：不读 status、不输出 header/LINK，只原样转发 journal 输入。
        for line in sys.stdin:
            sys.stdout.write(line)
            sys.stdout.flush()
        return 0

    use_color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    columns = shutil.get_terminal_size((120, 24)).columns
    console = FieldConsole(
        full_id=args.full_id,
        use_color=use_color,
        verbose=args.verbose,
        compact=columns < 90,
        lang=args.lang,
    )

    status = {}
    try:
        with open(args.status_file, encoding="utf-8") as handle:
            status = json.load(handle)
    except Exception:  # noqa: BLE001
        status = {}

    if args.lang == "zh":
        print(console.header_zh(status))
    else:
        print(console.header(status))
    console.mqtt = bool(status.get("mqtt_connected"))
    console.master = bool(status.get("ros_master_available"))
    console.adapter = bool(status.get("ros_adapter_ready"))
    if args.lang != "zh":
        print(console._link_line())

    def _consume_line(line: str):
        """解析并渲染一行 stdin；返回渲染结果字符串或 None。"""
        event = parse_jsonl(line) if args.jsonl else parse_trace(line)
        if event is None:
            return None
        try:
            return console.render(event)
        except Exception:  # noqa: BLE001
            return None

    # 历史/实时分区：前 --history 条归「最近事件」，之后归「实时事件」。
    # 用 select 短超时兜底：文件不足 --history 条时，历史区读完后立即进入实时区。
    if args.history > 0:
        print("========== 最近事件 ==========")
        sys.stdout.flush()
        history_left = args.history
        while history_left > 0:
            r, _, _ = select.select([sys.stdin], [], [], 0.5)
            if not r:
                break  # tail 已到 EOF 等待新行：历史已读完
            line = sys.stdin.readline()
            if not line:
                break
            history_left -= 1
            rendered = _consume_line(line)
            if rendered:
                print(rendered)
                sys.stdout.flush()
        print("========== 实时事件 ==========")
        print("等待新的车辆事件...")
        sys.stdout.flush()

    # 实时跟随（history=0 时从头即为实时）
    for line in sys.stdin:
        rendered = _consume_line(line)
        if rendered:
            print(rendered)
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
