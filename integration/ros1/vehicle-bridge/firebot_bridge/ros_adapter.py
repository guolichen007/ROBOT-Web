#!/usr/bin/env python3
"""ROS adapter 子进程。

父进程（firebot_bridge.main）在 ROS master 可用时 spawn 本进程、master 丢失/停机时
terminate 本进程。一个子进程 == 一个 ROS node 会话；roscore 重启后父进程 spawn 全新
子进程，向新 master 完整重新注册全部 pub/sub，彻底避免 rospy 单次 init_node 的不可逆
限制与 _TopicImpl 重复订阅累积。

IPC（newline-delimited JSON）：
  stdin : 父 → 子   {"type":"command","command":{...}}
  stdout: 子 → 父   每行前缀 FIREBOT_ROS_EVENT\\t，后接 JSON：
         {"type":"ready","ok":bool,"command_publisher":bool,"feedback":bool,"providers":{...},"reason":...}
         {"type":"feedback","feedback":{...}}
         {"type":"provider","channel":"battery|smoke","value":...}
         {"type":"status","mode":...,"estop_active":...,"active_task_id":...}
         {"type":"location","location":{...}}

本进程不做任何真实控制；只负责 ROS pub/sub 与 IPC 转发。
"""
from __future__ import annotations

import json
import math
import signal
import sys
import threading

EVENT_PREFIX = "FIREBOT_ROS_EVENT\t"

_STATUS_MODE_ENUM = {"IDLE", "MANUAL", "PATROL", "EXTINGUISH", "RETURN_DOCK", "ESTOP"}

_emit_lock = threading.Lock()


def _emit(payload: dict) -> None:
    with _emit_lock:
        sys.stdout.write(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def normalize_status(data: dict) -> dict:
    """status 字段白名单：只透传 mode/estop_active/active_task_id，绝不 `**data` 覆盖 type。

    mode 一律 uppercase 且只允许 schema enum；estop_active 只接受 bool；active_task_id
    只接受 str/None。类型不符直接丢弃（防止 "false" 字符串被 bool() 误判为 True）。
    """
    out: dict = {"type": "status"}
    if "mode" in data:
        mode = str(data["mode"]).upper()
        if mode in _STATUS_MODE_ENUM:
            out["mode"] = mode
    if "estop_active" in data and isinstance(data["estop_active"], bool):
        out["estop_active"] = data["estop_active"]
    if "active_task_id" in data and isinstance(data["active_task_id"], (str, type(None))):
        out["active_task_id"] = data["active_task_id"]
    return out


def _import_rospy():
    try:
        import rospy  # type: ignore

        return rospy
    except Exception:  # noqa: BLE001
        return None


def _build_components(rospy):
    """创建全部 pub/sub，逐项捕获成败，返回 {flags, providers, publish}。"""
    from .ros.interfaces import (
        TOPIC_ROS_BATTERY,
        TOPIC_ROS_COMMAND,
        TOPIC_ROS_FEEDBACK,
        TOPIC_ROS_LOCATION,
        TOPIC_ROS_SMOKE,
        TOPIC_ROS_STATUS,
        build_ros_command,
    )
    from .ros.ros_types import Odometry, PoseWithCovarianceStamped, StdFloat32, StdString

    flags = {"command_publisher": False, "feedback": False}
    providers: dict[str, bool] = {}
    publisher = None

    try:
        publisher = rospy.Publisher(TOPIC_ROS_COMMAND, StdString, queue_size=10)
        flags["command_publisher"] = True
    except Exception:  # noqa: BLE001
        pass

    def on_feedback(msg):
        try:
            fb = json.loads(msg.data)
            if isinstance(fb, dict):
                _emit({"type": "feedback", "feedback": fb})
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    try:
        rospy.Subscriber(TOPIC_ROS_FEEDBACK, StdString, on_feedback)
        flags["feedback"] = True
    except Exception:  # noqa: BLE001
        pass

    def on_float(channel, msg):
        try:
            _emit({"type": "provider", "channel": channel, "value": float(msg.data)})
        except Exception:  # noqa: BLE001
            pass

    for channel, topic in (("battery", TOPIC_ROS_BATTERY), ("smoke", TOPIC_ROS_SMOKE)):
        try:
            rospy.Subscriber(topic, StdFloat32, lambda m, c=channel: on_float(c, m))
            providers[channel] = True
        except Exception:  # noqa: BLE001
            providers[channel] = False

    def on_status(msg):
        try:
            data = json.loads(msg.data)
            if isinstance(data, dict):
                _emit(normalize_status(data))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    try:
        rospy.Subscriber(TOPIC_ROS_STATUS, StdString, on_status)
        providers["status"] = True
    except Exception:  # noqa: BLE001
        providers["status"] = False

    def on_location(msg):
        try:
            data = json.loads(msg.data)
            if isinstance(data, dict):
                _emit({"type": "location", "location": data})
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    try:
        rospy.Subscriber(TOPIC_ROS_LOCATION, StdString, on_location)
        providers["location"] = True
    except Exception:  # noqa: BLE001
        providers["location"] = False

    speed = {"linear": 0.0, "angular": 0.0}

    def on_odom(msg):
        try:
            # Mecanum：平面速度模长 = hypot(vx, vy)，纯横移不能被视为零线速度。
            vx = msg.twist.twist.linear.x
            vy = msg.twist.twist.linear.y
            speed["linear"] = math.hypot(vx, vy)
            speed["angular"] = msg.twist.twist.angular.z
        except Exception:  # noqa: BLE001
            pass

    def on_amcl(msg):
        try:
            p = msg.pose.pose
            q = p.orientation
            import math

            yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
            _emit({
                "type": "location",
                "location": {
                    "position": {"x": p.position.x, "y": p.position.y, "theta": yaw},
                    "linear": speed["linear"],
                    "angular": speed["angular"],
                    "localization_status": "OK",
                },
            })
        except Exception:  # noqa: BLE001
            pass

    # /odom、/amcl_pose 订阅失败不能导致 child 异常退出
    try:
        if Odometry is not None:
            rospy.Subscriber("/odom", Odometry, on_odom)
    except Exception:  # noqa: BLE001
        pass
    try:
        if PoseWithCovarianceStamped is not None:
            rospy.Subscriber("/amcl_pose", PoseWithCovarianceStamped, on_amcl)
    except Exception:  # noqa: BLE001
        pass

    def publish_command(command):
        payload = json.dumps(build_ros_command(command), ensure_ascii=False)
        publisher.publish(StdString(data=payload))

    return {"flags": flags, "providers": providers, "publish": publish_command}


def _stdin_reader(rospy, comp):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(msg, dict) or msg.get("type") != "command":
            continue
        try:
            comp["publish"](msg.get("command") or {})
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    rospy = _import_rospy()
    if rospy is None:
        _emit({"type": "ready", "ok": False, "reason": "ROSPY_IMPORT_FAILED",
               "command_publisher": False, "feedback": False, "providers": {}})
        return 1

    def _on_signal(signum, frame):
        try:
            rospy.signal_shutdown("signal")
        except Exception:  # noqa: BLE001
            pass

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        rospy.init_node("firebot_bridge_ros_adapter", anonymous=True, disable_signals=True)
    except Exception as exc:  # noqa: BLE001
        _emit({"type": "ready", "ok": False, "reason": "NODE_INIT_FAILED:" + type(exc).__name__,
               "command_publisher": False, "feedback": False, "providers": {}})
        return 1

    comp = _build_components(rospy)
    _emit({"type": "ready", "ok": True,
           "command_publisher": comp["flags"]["command_publisher"],
           "feedback": comp["flags"]["feedback"],
           "providers": comp["providers"]})

    threading.Thread(target=_stdin_reader, args=(rospy, comp), daemon=True).start()
    rospy.spin()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
