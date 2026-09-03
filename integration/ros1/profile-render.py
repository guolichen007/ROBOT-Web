#!/usr/bin/env python3
"""从 Fleet Profile YAML 渲染 runtime 配置（runtime.env）。

轻量 YAML 解析（只取本仓库需要的标量键，支持两层标量 + 三层 package/launch），
不引入第三方依赖，兼容 Python 3.8。
用法：python3 profile-render.py runtime <profile.yaml>   # 输出 KEY=VALUE
"""
from __future__ import annotations

import sys


def _parse(profile_path):
    """轻量 YAML：indent0=section，indent2=标量或子 map，indent4=子 map 内的标量。"""
    data = {}
    section = None
    subsection = None
    for raw in open(profile_path, encoding="utf-8"):
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if indent == 0:
            section = key
            data[section] = {}
            subsection = None
        elif indent == 2:
            if val == "":
                # 子 map（如 base_launch:）
                data.setdefault(section, {})[key] = {}
                subsection = key
            else:
                data.setdefault(section, {})[key] = val
                subsection = key
        elif indent == 4:
            data.setdefault(section, {}).setdefault(subsection, {})[key] = val
    return data


def _nav(entry, field):
    if isinstance(entry, dict):
        return entry.get(field, "")
    return ""


def render_runtime(profile_path):
    d = _parse(profile_path)
    ros = d.get("ros", {})
    base = d.get("base", {})
    nav = d.get("navigation", {})
    control = d.get("control", {})
    motion = d.get("motion", {})
    bridge = d.get("bridge", {})

    return "\n".join(
        [
            "# GENERATED FILE - DO NOT EDIT",
            "# 来源：Fleet Profile（车型合同）。",
            "ROS_DISTRO=%s" % ros.get("distro", "noetic"),
            "ROS_WORKSPACE=%s" % ros.get("workspace", "/home/tl/firerobot_ws"),
            "BASE_DEVICE=%s" % base.get("device", "/dev/agv"),
            "ROBOT_STATUS_TOPIC=%s" % base.get("status_topic", "/robot_status"),
            "ODOM_TOPIC=%s" % base.get("odom_topic", "/odom"),
            "CMD_VEL_TOPIC=%s" % base.get("cmd_vel_topic", "/cmd_vel"),
            "EXPECTED_CONTROL_MODE=%s" % base.get("expected_control_mode", "3"),
            "AMCL_TOPIC=%s" % nav.get("amcl_topic", "/amcl_pose"),
            "POSE_TOPIC=%s" % nav.get("pose_topic", "/waterplus/navi_pose"),
            "BASE_LAUNCH_PACKAGE=%s" % _nav(nav.get("base_launch"), "package"),
            "BASE_LAUNCH_FILE=%s" % _nav(nav.get("base_launch"), "launch"),
            "NAV_LAUNCH_PACKAGE=%s" % _nav(nav.get("navigation_launch"), "package"),
            "NAV_LAUNCH_FILE=%s" % _nav(nav.get("navigation_launch"), "launch"),
            "POSE_SERVER_PACKAGE=%s" % _nav(nav.get("pose_server"), "package"),
            "POSE_SERVER_LAUNCH=%s" % _nav(nav.get("pose_server"), "launch"),
            "CONTROL_ADAPTER=%s" % control.get("adapter", "firebot_control_adapter.py"),
            "STATIONARY_LINEAR_MPS=%s" % motion.get("stationary_linear_mps", "0.02"),
            "STATIONARY_ANGULAR_RADPS=%s" % motion.get("stationary_angular_radps", "0.03"),
            "LOCATION_STALE_SECONDS=%s" % bridge.get("location_stale_seconds", "3"),
            "",
        ]
    )


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "runtime":
        print("用法: profile-render.py runtime <profile.yaml>", file=sys.stderr)
        return 2
    print(render_runtime(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
