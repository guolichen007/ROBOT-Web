"""ROS runtime / lifecycle manager。

核心原则：MQTT/TLS 通信层必须独立于 ROS master 生死。

- 无 roscore：Bridge 仍存活、MQTT 在线、heartbeat 持续；ROS 状态 WAITING_MASTER，
  命令安全 rejected + BRIDGE_ADAPTER_NOT_CONNECTED。
- roscore 延迟出现：后台探测到 master 后自动 init_node 并建 subscribers/publishers，
  无需重启进程，boot_id 不变。
- roscore 中途死亡：MQTT 不断、进程不退出，ROS adapter 降级 not-ready。
- 本类不包含任何真实控制逻辑。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from urllib.parse import urlparse

from .interfaces import TOPIC_ROS_COMMAND, build_ros_command
from .providers import RosProviders
from .feedback import FeedbackListener
from .ros_types import Odometry, PoseWithCovarianceStamped, StdString

LOG = logging.getLogger("firebot-bridge")

_PROBE_TIMEOUT_S = 1.0
_RETRY_INTERVAL_S = 2.0


def _ros_master_reachable(timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """TCP 探测 roscore 的 XMLRPC 端口是否可达（不调用 rospy，绝不阻塞）。"""
    uri = os.environ.get("ROS_MASTER_URI", "http://localhost:11311")
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 11311
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def setup_ros_subscribers(rospy, state) -> None:
    """订阅 /odom、/amcl_pose（位置/速度）。必须在 rospy.init_node 之后、由 lifecycle 调用。

    battery 不在这里：由 RosProviders 订阅 /firebot_bridge/battery（canonical 契约）。
    """
    _speed = {"linear": 0.0, "angular": 0.0}

    def handle_odom(msg) -> None:
        try:
            _speed["linear"] = msg.twist.twist.linear.x
            _speed["angular"] = msg.twist.twist.angular.z
        except Exception:  # noqa: BLE001
            pass

    def handle_amcl(msg) -> None:
        """/amcl_pose（map 系）→ location。以 amcl 为准（odom 是 LOCAL，不冒充 map）。"""
        try:
            p = msg.pose.pose
            q = p.orientation
            import math

            yaw = math.atan2(
                2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
            )
            state.set_location(
                {
                    "position": {"x": p.position.x, "y": p.position.y, "theta": yaw},
                    "linear": _speed["linear"],
                    "angular": _speed["angular"],
                    "localization_status": "OK",
                }
            )
        except Exception:  # noqa: BLE001
            pass

    if Odometry is not None:
        rospy.Subscriber("/odom", Odometry, handle_odom)
    if PoseWithCovarianceStamped is not None:
        rospy.Subscriber("/amcl_pose", PoseWithCovarianceStamped, handle_amcl)
    LOG.info("ROS 数据订阅就绪：/odom /amcl_pose")


class RosLifecycle:
    """ROS master 探测 / readiness / 组件启动，failure isolation。"""

    def __init__(self, rospy, state, status=None) -> None:
        self.rospy = rospy
        self.state = state
        self.status = status
        self._on_feedback = None
        self._ready = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._publisher = None

    def set_on_feedback(self, handler) -> None:
        self._on_feedback = handler

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    def _set_ready(self, value: bool) -> None:
        with self._lock:
            self._ready = value

    def start(self) -> None:
        threading.Thread(target=self._run, name="ros-lifecycle", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ---- 命令转发占位（CommandProcessor 调此方法）----
    def publish_command(self, command: dict) -> bool:
        if not self.ready or self.rospy is None or StdString is None:
            LOG.info("ROS 未就绪（WAITING_MASTER / degraded）：命令无法转发")
            return False
        try:
            payload = json.dumps(build_ros_command(command), ensure_ascii=False)
            if self._publisher is None:
                self._publisher = self.rospy.Publisher(
                    TOPIC_ROS_COMMAND, StdString, queue_size=10
                )
            self._publisher.publish(StdString(data=payload))
            LOG.info("已转发到 %s: cmd=%s", TOPIC_ROS_COMMAND, command.get("cmd"))
            return True
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ROS 转发失败: %s", exc)
            return False

    # ---- 生命周期线程 ----
    def _run(self) -> None:
        while not self._stop.is_set():
            if self._ready:
                self._watch_master()
            elif self._try_init():
                self._set_ready(True)
                LOG.info("ROS adapter ready")
                if self.status:
                    self.status.set(ros_master_available=True, ros_adapter_ready=True)
            else:
                self._stop.wait(_RETRY_INTERVAL_S)

    def _try_init(self) -> bool:
        if self.rospy is None:
            return False
        if not _ros_master_reachable():
            return False
        try:
            self.rospy.init_node("firebot_bridge", anonymous=True, disable_signals=True)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("rospy.init_node 失败: %s", exc)
            return False
        try:
            if self._on_feedback:
                FeedbackListener(self.rospy, self._on_feedback).start()
            RosProviders(self.rospy, self.state).start()
            setup_ros_subscribers(self.rospy, self.state)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ROS 组件启动失败: %s", exc)
            return False
        return True

    def _watch_master(self) -> None:
        while not self._stop.is_set():
            if not _ros_master_reachable():
                LOG.warning("ROS master 丢失：ROS adapter degraded")
                self._set_ready(False)
                if self.status:
                    self.status.set(ros_master_available=False, ros_adapter_ready=False)
                return
            self._stop.wait(_RETRY_INTERVAL_S)
