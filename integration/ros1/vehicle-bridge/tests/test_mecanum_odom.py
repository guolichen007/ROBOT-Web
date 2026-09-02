#!/usr/bin/env python3
"""Mecanum 平面速度测试：on_odom 必须用 hypot(vx,vy)，纯横移不得得到 linear=0。"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- fake ROS message 模块（让 ros_types 能 import 出非 None 的 Odometry 等）----
class _Vec:
    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0


class _Twist:
    def __init__(self):
        self.linear = _Vec()
        self.angular = _Vec()


class _TwistWithCov:
    def __init__(self):
        self.twist = _Twist()


class _Quat:
    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.w = 1.0


class _Pose:
    def __init__(self):
        self.position = _Vec()
        self.orientation = _Quat()


class _PoseWithCovariance:
    def __init__(self):
        self.pose = _Pose()


class _PoseWithCovarianceStamped:
    def __init__(self):
        self.pose = _PoseWithCovariance()


class _Odometry:
    def __init__(self):
        self.twist = _TwistWithCov()


class _Float32:
    def __init__(self, data=0.0):
        self.data = data


class _String:
    def __init__(self, data=""):
        self.data = data


std_msgs = types.ModuleType("std_msgs")
std_msgs_msg = types.ModuleType("std_msgs.msg")
std_msgs_msg.Float32 = _Float32
std_msgs_msg.String = _String
std_msgs.msg = std_msgs_msg

nav_msgs = types.ModuleType("nav_msgs")
nav_msgs_msg = types.ModuleType("nav_msgs.msg")
nav_msgs_msg.Odometry = _Odometry
nav_msgs.msg = nav_msgs_msg

geometry_msgs = types.ModuleType("geometry_msgs")
geometry_msgs_msg = types.ModuleType("geometry_msgs.msg")
geometry_msgs_msg.PoseWithCovarianceStamped = _PoseWithCovarianceStamped
geometry_msgs.msg = geometry_msgs_msg


class _Subscriber:
    def __init__(self, topic, msg_type, cb):
        self.topic = topic
        self.msg_type = msg_type
        self.cb = cb


class _Publisher:
    def __init__(self, topic, msg_type, queue_size=10):
        self.topic = topic
        self.msg_type = msg_type
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


rospy = types.ModuleType("rospy")
rospy.Publisher = _Publisher
rospy.Subscriber = _Subscriber
rospy.get_time = lambda: 0.0
rospy.sleep = lambda s: None
rospy.loginfo = lambda *a, **k: None
rospy.logwarn = lambda *a, **k: None

for name, mod in [
    ("std_msgs", std_msgs),
    ("std_msgs.msg", std_msgs_msg),
    ("nav_msgs", nav_msgs),
    ("nav_msgs.msg", nav_msgs_msg),
    ("geometry_msgs", geometry_msgs),
    ("geometry_msgs.msg", geometry_msgs_msg),
    ("rospy", rospy),
]:
    sys.modules[name] = mod

from firebot_bridge import ros_adapter as ra  # noqa: E402

subs: dict = {}


class _RegSub:
    def __init__(self, topic, msg_type, cb):
        self.topic = topic
        self.msg_type = msg_type
        self.cb = cb
        subs[topic] = self


rospy.Subscriber = _RegSub

captured = []
ra._emit = lambda payload: captured.append(payload)

comp = ra._build_components(rospy)
assert "/odom" in subs, "/odom subscriber 未注册"
assert "/amcl_pose" in subs, "/amcl_pose subscriber 未注册"

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


def run_case(vx, vy, wz, label):
    captured.clear()
    odom = _Odometry()
    odom.twist.twist.linear.x = vx
    odom.twist.twist.linear.y = vy
    odom.twist.twist.angular.z = wz
    subs["/odom"].cb(odom)
    subs["/amcl_pose"].cb(_PoseWithCovarianceStamped())
    loc = [p for p in captured if p.get("type") == "location"]
    check(label, bool(loc) and loc[-1]["location"]["linear"] > 0)


def main() -> int:
    # A: vx=0.1, vy=0 → planar > 0
    run_case(0.1, 0.0, 0.0, "A: vx=0.1, vy=0 → planar > 0")
    # B: vx=0, vy=0.1 → planar > 0（纯横移不得被判为零线速度）
    run_case(0.0, 0.1, 0.0, "B: vx=0, vy=0.1 → planar > 0（纯横移）")
    # C: vx=0, vy=0, wz=0 → stationary candidate（linear=0）
    captured.clear()
    odom = _Odometry()
    subs["/odom"].cb(odom)
    subs["/amcl_pose"].cb(_PoseWithCovarianceStamped())
    loc = [p for p in captured if p.get("type") == "location"]
    check(
        "C: vx=0, vy=0, wz=0 → linear=0（静止候选）",
        bool(loc) and loc[-1]["location"]["linear"] == 0,
    )

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
