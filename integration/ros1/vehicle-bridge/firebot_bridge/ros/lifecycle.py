"""ROS 子进程生命周期管理（父进程侧）。

核心原则：MQTT/TLS 通信层必须独立于 ROS master 生死。ROS 全部 pub/sub 运行在独立的
`firebot_bridge.ros_adapter` 子进程里：

- 无 roscore：不 spawn 子进程，MQTT 仍在线，命令 rejected + BRIDGE_ADAPTER_NOT_CONNECTED。
- roscore 出现：spawn 子进程，子进程 init_node + 建全部 pub/sub 后上报 ready。
- roscore 死亡：terminate 子进程，MQTT 不断、父进程不退出。
- roscore 恢复：spawn 全新子进程，向新 master 完整重新注册（规避 rospy 单次 init_node
  与 _TopicImpl 重复订阅累积问题）。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

LOG = logging.getLogger("firebot-bridge")

_PROBE_TIMEOUT_S = 1.0
_POLL_INTERVAL_S = 2.0
_EVENT_PREFIX = "FIREBOT_ROS_EVENT\t"


def ros_master_reachable(timeout: float = _PROBE_TIMEOUT_S) -> bool:
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


def compute_adapter_ready(command_publisher: bool, feedback: bool) -> bool:
    """ROS_ADAPTER_READY 最小集合：命令发布 + 反馈订阅都就绪。"""
    return bool(command_publisher and feedback)


class RosChildManager:
    def __init__(self, config, state, status=None) -> None:
        self.config = config
        self.state = state
        self.status = status
        self._on_feedback = None
        self._proc = None
        self._stdin = None
        self._reader = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.master_available = False
        self.node_ready = False
        self.command_publisher_ready = False
        self.feedback_ready = False
        self.provider_ready = False
        self.battery_provider_seen = False
        self.battery_last_update = None

    # ---- 对外接口 ----
    def set_on_feedback(self, handler) -> None:
        self._on_feedback = handler

    @property
    def adapter_ready(self) -> bool:
        return compute_adapter_ready(self.command_publisher_ready, self.feedback_ready)

    def start(self) -> None:
        threading.Thread(target=self._run, name="ros-child-manager", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._terminate_child()

    def publish_command(self, command: dict) -> bool:
        if not self.adapter_ready or self._stdin is None or self._proc is None or self._proc.poll() is not None:
            LOG.info("ROS 未就绪（no child / not ready）：命令无法转发")
            return False
        try:
            self._stdin.write(json.dumps({"type": "command", "command": command}) + "\n")
            self._stdin.flush()
            return True
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ROS 命令转发失败: %s", exc)
            return False

    # ---- 生命周期线程 ----
    def _run(self) -> None:
        while not self._stop.is_set():
            reachable = ros_master_reachable()
            self.master_available = reachable
            if reachable and self._proc is None:
                self._spawn()
            elif not reachable and self._proc is not None:
                LOG.warning("ROS master 丢失：terminate ROS 子进程")
                self._terminate_child()
            self._refresh_status()
            self._stop.wait(_POLL_INTERVAL_S)

    def _spawn(self) -> None:
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "firebot_bridge.ros_adapter"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.error("ROS 子进程 spawn 失败: %s", exc)
            return
        with self._lock:
            self._proc = proc
            self._stdin = proc.stdin
        self._reset_ready()
        threading.Thread(target=self._reader_loop, args=(proc,), name="ros-child-reader", daemon=True).start()
        LOG.info("ROS 子进程已启动 pid=%s", proc.pid)

    def _terminate_child(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._stdin = None
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._reset_ready()

    def _reset_ready(self) -> None:
        self.node_ready = False
        self.command_publisher_ready = False
        self.feedback_ready = False
        self.provider_ready = False

    def _reader_loop(self, proc) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith(_EVENT_PREFIX):
                    try:
                        self._handle_event(json.loads(line[len(_EVENT_PREFIX):]))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            # stdout 关闭 = 子进程退出
            with self._lock:
                if self._proc is proc:
                    self._reset_ready()
            self._refresh_status()

    def _handle_event(self, payload: dict) -> None:
        t = payload.get("type")
        if t == "ready":
            self.node_ready = bool(payload.get("ok"))
            self.command_publisher_ready = bool(payload.get("command_publisher"))
            self.feedback_ready = bool(payload.get("feedback"))
            providers = payload.get("providers") or {}
            self.provider_ready = bool(providers) and all(providers.values())
            if not self.node_ready:
                LOG.warning("ROS 子进程未就绪: %s", payload.get("reason"))
        elif t == "feedback":
            if self._on_feedback:
                self._on_feedback(payload.get("feedback") or {})
        elif t == "provider":
            channel = payload.get("channel")
            if channel == "battery":
                self.state.set_battery(payload.get("value"))
                self.battery_provider_seen = True
                self.battery_last_update = time.time()
            elif channel == "smoke":
                self.state.set_smoke(payload.get("value"))
        elif t == "status":
            self.state.apply_status(payload)
        elif t == "location":
            self.state.set_location(payload.get("location"))
        self._refresh_status()

    def _refresh_status(self) -> None:
        if not self.status:
            return
        self.status.set(
            ros_master_available=self.master_available,
            ros_node_ready=self.node_ready,
            ros_command_publisher_ready=self.command_publisher_ready,
            ros_feedback_ready=self.feedback_ready,
            ros_provider_ready=self.provider_ready,
            ros_adapter_ready=self.adapter_ready,
            battery_provider_seen=self.battery_provider_seen,
            battery_last_update=self.battery_last_update,
        )
