#!/usr/bin/env python3
"""指令追踪：从结构化事件日志重建单条 command 的时间线（只读）。

数据源 = events.jsonl + 所有保留的 events-*.jsonl（轮转后旧 command_id 仍可查）。
绝不读进程状态、绝不读 status.json。排序依据 = (boot_id, event_seq)，不是仅 timestamp。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

_ZH_LABEL = {
    "mqtt.command.rx": "服务器命令收到",
    "ros.command.tx": "Bridge发送ROS",
    "ros.command.tx_failed": "Bridge发送失败",
    "ros.feedback.rx": "ROS控制处理",
    "mqtt.command_ack.tx": "ACK上传",
    "mqtt.task_status.tx": "任务状态上报",
}

_ZH_STATE = {
    "accepted": "接受", "rejected": "拒绝", "executing": "执行中",
    "completed": "完成", "failed": "失败", "cancelled": "取消",
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

_ZH_CMD = {
    "patrol": "开始巡检",
    "PATROL_START": "开始巡检",
    "PATROL": "开始巡检",
}


def _zh_cmd(cmd):
    if cmd is None:
        return ""
    return _ZH_CMD.get(str(cmd), str(cmd))


def load_all(events_dir: str) -> list:
    """读 events.jsonl + 轮转历史，按 (boot_id, event_seq) 排序。"""
    records = []
    if not os.path.isdir(events_dir):
        return records
    names = ["events.jsonl"] + sorted(
        n for n in os.listdir(events_dir)
        if n.startswith("events-") and n.endswith(".jsonl")
    )
    for name in names:
        path = os.path.join(events_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except (json.JSONDecodeError, ValueError):
                        pass
        except OSError:
            pass
    records.sort(key=lambda r: (str(r.get("boot_id") or ""), int(r.get("event_seq") or 0)))
    return records


def pick_command_id(records: list, target: str):
    """latest = event_seq 最大的 mqtt.command.rx 的 command_id。"""
    if target != "latest":
        return target
    best = None
    for r in records:
        if r.get("event") == "mqtt.command.rx":
            cid = r.get("command_id")
            seq = int(r.get("event_seq") or 0)
            if cid and (best is None or seq > best[1]):
                best = (cid, seq)
    return best[0] if best else None


def ts_display(r: dict) -> str:
    ts = r.get("timestamp_utc") or ""
    if isinstance(ts, str) and "T" in ts:
        try:
            wall = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            total_ms = int(round(wall * 1000))
            seconds, millis = divmod(total_ms, 1000)
            return f"{time.strftime('%H:%M:%S', time.localtime(seconds))}.{millis:03d}"
        except (ValueError, TypeError):
            pass
    return ts


def zh_detail(r: dict, cmd=None) -> str:
    ev = r.get("event")
    if ev in ("mqtt.command.rx", "ros.command.tx"):
        return _zh_cmd(r.get("cmd"))
    if ev == "ros.command.tx_failed":
        return f"{_zh_cmd(r.get('cmd'))} reason={r.get('reason')}"
    if ev == "ros.feedback.rx":
        raw = str(r.get("state") or "").lower()
        state = _ZH_STATE.get(raw, r.get("state") or "?")
        if raw in ("accepted", "rejected", "completed", "failed", "cancelled"):
            state = "已" + state
        msg = r.get("message") or r.get("reason_code")
        reason = _ZH_REASON.get(msg, msg) if msg else None
        s = f"{cmd}：{state}" if cmd else state
        if reason:
            s += f"，原因：{reason}"
        return s
    if ev == "mqtt.command_ack.tx":
        status = _ZH_STATE.get(str(r.get("status") or "").lower(), r.get("status") or "?")
        return f"status={status} reason_code={r.get('reason_code')}"
    if ev == "mqtt.task_status.tx":
        status = _ZH_STATE.get(str(r.get("status") or "").lower(), r.get("status") or "?")
        return f"status={status} phase={r.get('phase')}"
    return ""


def en_detail(r: dict) -> str:
    skip = {"event", "level", "trace_schema_version", "monotonic",
            "vehicle_id", "boot_id", "event_seq", "timestamp_utc"}
    return json.dumps({k: v for k, v in r.items() if k not in skip}, ensure_ascii=False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Firebot command timeline（从 events.jsonl 重建）")
    parser.add_argument("events_dir")
    parser.add_argument("target", nargs="?", default="latest")
    parser.add_argument("--task-id")
    parser.add_argument("--lang", choices=["en", "zh"], default="zh")
    args = parser.parse_args(argv)

    records = load_all(args.events_dir)
    if not records:
        print("无事件（events_dir 为空或不存在）", file=sys.stderr)
        return 1

    command_id = None if args.task_id else pick_command_id(records, args.target)
    if command_id is None and args.task_id is None:
        print("未找到目标命令（无 mqtt.command.rx 事件）", file=sys.stderr)
        return 1

    matched = [
        r for r in records
        if (command_id and r.get("command_id") == command_id)
        or (args.task_id and r.get("task_id") == args.task_id)
    ]
    if not matched:
        print("未找到匹配事件", file=sys.stderr)
        return 1

    first = matched[0]
    print("========== 指令追踪 ==========")
    print(f"设备：{first.get('vehicle_id') or '?'}")
    print(f"启动编号：{first.get('boot_id') or '?'}")
    if command_id:
        print(f"指令编号：{command_id}")
    rx = next((r for r in matched if r.get("event") == "mqtt.command.rx"), None)
    if rx:
        print(f"指令：{_zh_cmd(rx.get('cmd'))}")
        if rx.get("task_id"):
            print(f"任务编号：{rx.get('task_id')}")
    elif args.task_id:
        print(f"任务编号：{args.task_id}")
    print("----------------------------")

    # ---- 环节判定摘要（车端只能证明「已发送」，不能证明「服务器已收到」）----
    if args.lang == "zh":
        def _has(ev_name):
            return any(r.get("event") == ev_name for r in matched)

        fb = next((r for r in matched if r.get("event") == "ros.feedback.rx"), None)
        ack = next((r for r in matched if r.get("event") == "mqtt.command_ack.tx"), None)

        print(f"服务器命令收到：{'通过' if _has('mqtt.command.rx') else '未找到'}")
        print(f"Bridge转发ROS：{'通过' if _has('ros.command.tx') else '未找到'}")
        print(f"ROS控制处理：{'通过' if fb else '未找到'}")
        if fb:
            raw_state = str(fb.get("state") or "").lower()
            print(f"控制结果：{_ZH_STATE.get(raw_state, fb.get('state') or '?')}")
            reason = fb.get("message") or fb.get("reason_code")
            if reason:
                print(f"原因：{_ZH_REASON.get(str(reason), str(reason))}")
        else:
            print("控制结果：未找到")
        print(f"ACK发送：{'通过' if ack else '未找到'}")
        chain_ok = _has('mqtt.command.rx') and _has('ros.command.tx') and fb is not None and ack is not None
        print(f"通信控制链：{'完整' if chain_ok else '不完整'}")
        if fb:
            nav = {
                "rejected": "未开始", "accepted": "已开始", "executing": "已开始",
                "completed": "完成", "failed": "失败", "cancelled": "取消",
            }.get(str(fb.get("state") or "").lower(), "未知")
            print(f"导航执行：{nav}")
        else:
            print("导航执行：未开始")
        print("----------------------------")

    cmd = _zh_cmd(rx.get("cmd")) if rx else None
    for r in matched:
        ev = r.get("event")
        ts = ts_display(r)
        label = _ZH_LABEL.get(ev, ev) if args.lang == "zh" else ev
        detail = zh_detail(r, cmd) if args.lang == "zh" else en_detail(r)
        lat = r.get("latency_ms")
        lat_s = f"  latency={lat}ms" if lat is not None else ""
        print(f"{ts}  {label}  {detail}{lat_s}")

    print("============================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
