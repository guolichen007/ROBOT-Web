#!/usr/bin/env python3
"""STOP_MOTION fail-closed 单元测试（mock rospy/actionlib，无真实 ROS）。"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1] / "scripts" / "firebot_control_adapter.py"


class _Vec:
    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0


class _Twist:
    def __init__(self):
        self.linear = _Vec()
        self.angular = _Vec()


class _String:
    def __init__(self, data=""):
        self.data = data


class _Publisher:
    def __init__(self, topic, msg_type, queue_size=10):
        self.topic = topic
        self.msg_type = msg_type
        self.published = []
        self.num_connections = 0

    def publish(self, msg):
        self.published.append(msg)

    def get_num_connections(self):
        return self.num_connections


class _Subscriber:
    def __init__(self, topic, msg_type, cb):
        self.topic = topic
        self.msg_type = msg_type
        self.cb = cb


class _Rate:
    def __init__(self, hz):
        self.hz = hz

    def sleep(self):
        pass


class _ActionClient:
    def __init__(self, name, action):
        self.name = name
        self.cancel_called = 0
        self.cancel_exc = None

    def cancel_all_goals(self):
        if self.cancel_exc:
            raise self.cancel_exc
        self.cancel_called += 1

    def wait_for_server(self, timeout=None):
        return True


rospy = types.ModuleType("rospy")
rospy.init_node = lambda *a, **k: None
rospy.Publisher = _Publisher
rospy.Subscriber = _Subscriber
rospy.Rate = _Rate
rospy.get_time = lambda: 0.0
rospy.sleep = lambda s: None
rospy.Duration = lambda s: s
rospy.loginfo = lambda *a, **k: None
rospy.logwarn = lambda *a, **k: None
rospy.spin = lambda: None
rospy.is_shutdown = lambda: False
rospy.ROSInterruptException = type("ROSInterruptException", (Exception,), {})

actionlib = types.ModuleType("actionlib")
actionlib.SimpleActionClient = _ActionClient

std_msgs = types.ModuleType("std_msgs")
std_msgs_msg = types.ModuleType("std_msgs.msg")
std_msgs_msg.String = _String
std_msgs.msg = std_msgs_msg

geometry_msgs = types.ModuleType("geometry_msgs")
geometry_msgs_msg = types.ModuleType("geometry_msgs.msg")


class _Pose:
    pass


geometry_msgs_msg.Pose = _Pose
geometry_msgs_msg.Twist = _Twist
geometry_msgs.msg = geometry_msgs_msg

actionlib_msgs = types.ModuleType("actionlib_msgs")
actionlib_msgs_msg = types.ModuleType("actionlib_msgs.msg")


class _GoalStatus:
    PENDING = 1
    ACTIVE = 2


class _GoalStatusArray:
    def __init__(self):
        self.status_list = []


actionlib_msgs_msg.GoalStatus = _GoalStatus
actionlib_msgs_msg.GoalStatusArray = _GoalStatusArray
actionlib_msgs.msg = actionlib_msgs_msg

move_base_msgs = types.ModuleType("move_base_msgs")
move_base_msgs_msg = types.ModuleType("move_base_msgs.msg")


class _MoveBaseAction:
    pass


move_base_msgs_msg.MoveBaseAction = _MoveBaseAction
move_base_msgs.msg = move_base_msgs_msg

for name, mod in [
    ("rospy", rospy),
    ("actionlib", actionlib),
    ("std_msgs", std_msgs),
    ("std_msgs.msg", std_msgs_msg),
    ("geometry_msgs", geometry_msgs),
    ("geometry_msgs.msg", geometry_msgs_msg),
    ("actionlib_msgs", actionlib_msgs),
    ("actionlib_msgs.msg", actionlib_msgs_msg),
    ("move_base_msgs", move_base_msgs),
    ("move_base_msgs.msg", move_base_msgs_msg),
]:
    sys.modules[name] = mod

spec = importlib.util.spec_from_file_location("firebot_control_adapter", ADAPTER)
adapter_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter_mod)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def make_adapter():
    return adapter_mod.FirebotControlAdapter()


def last_feedback(adapter):
    msgs = adapter.feedback_pub.published
    if not msgs:
        return None
    return json.loads(msgs[-1].data)


def cmd_msg(command, **kw):
    m = {"command": command, "command_id": "c1", "task_id": "t1", "expires_at": None}
    m.update(kw)
    return _String(json.dumps(m))


def main() -> int:
    # 过期命令 → REJECTED COMMAND_EXPIRED
    a = make_adapter()
    a._on_command(cmd_msg("STOP_MOTION", expires_at="2000-01-01T00:00:00+00:00"))
    fb = last_feedback(a)
    check(
        "过期 STOP_MOTION → REJECTED COMMAND_EXPIRED",
        fb and fb["state"] == "REJECTED" and fb.get("reason_code") == "COMMAND_EXPIRED",
    )

    # 无 /cmd_vel 接收者 → REJECTED，不 ACCEPTED
    a = make_adapter()
    a.cmd_vel_pub.num_connections = 0
    a._on_command(cmd_msg("STOP_MOTION"))
    fb = last_feedback(a)
    check(
        "无 /cmd_vel 接收者 → REJECTED（不 ACCEPTED）",
        fb and fb["state"] == "REJECTED" and fb.get("message") == "CMD_VEL_NO_RECEIVER",
    )

    # cancel_all_goals 异常 → REJECTED，不 ACCEPTED
    a = make_adapter()
    a.cmd_vel_pub.num_connections = 1
    a._mb_client.cancel_exc = RuntimeError("cancel fail")
    a._on_command(cmd_msg("STOP_MOTION"))
    fb = last_feedback(a)
    check(
        "cancel_all_goals 异常 → REJECTED（不 ACCEPTED）",
        fb and fb["state"] == "REJECTED" and fb.get("message") == "CANCEL_GOALS_FAILED",
    )

    # 成功执行停车动作 → ACCEPTED，且零速度 burst 多帧
    a = make_adapter()
    a.cmd_vel_pub.num_connections = 1
    a._on_command(cmd_msg("STOP_MOTION"))
    fb = last_feedback(a)
    check("成功执行停车动作 → ACCEPTED", fb and fb["state"] == "ACCEPTED")
    check("cancel_all_goals 被调用", a._mb_client.cancel_called == 1)
    check("零速度 burst 多帧（>1）", len(a.cmd_vel_pub.published) > 1)

    # PATROL_START 不回归：无下游就绪 → REJECTED（不崩溃）
    a = make_adapter()
    a._on_command(cmd_msg("PATROL_START"))
    fb = last_feedback(a)
    check("PATROL_START 无下游就绪 → REJECTED（不回归）", fb and fb["state"] == "REJECTED")

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
