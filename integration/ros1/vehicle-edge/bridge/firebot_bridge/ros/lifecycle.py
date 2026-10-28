"""ROS 子进程生命周期管理（父进程侧 supervisor）。

核心原则：MQTT/TLS 通信层必须独立于 ROS master 生死。ROS 全部 pub/sub 运行在独立的
`firebot_bridge.ros_adapter` 子进程里：

- 无 roscore：不 spawn 子进程，MQTT 仍在线，命令 rejected + BRIDGE_ADAPTER_NOT_CONNECTED。
- roscore 出现：spawn 子进程，等待 READY handshake（超时 terminate 重试）。
- roscore 死亡 / child 崩溃 / READY 超时：reap 子进程、清 readiness、清 ROS telemetry、
  退避后重新 spawn 全新子进程（规避 rospy 单次 init_node 与 _TopicImpl 重复订阅累积）。
- child generation 隔离：旧 child 残留事件不再作用于当前状态。
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
_READY_TIMEOUT_S = 8.0
_SPAWN_BACKOFF_MIN_S = 1.0
_SPAWN_BACKOFF_MAX_S = 30.0
_EVENT_PREFIX = "FIREBOT_ROS_EVENT\t"
_SECRET_ENV_KEYS = ("FIREBOT_MQTT_PASSWORD",)


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


def build_child_env() -> dict:
    """子进程环境：剥离 MQTT 密码等 secret（最小权限）。"""
    env = os.environ.copy()
    for key in _SECRET_ENV_KEYS:
        env.pop(key, None)
    return env


class RosChildManager:
    def __init__(self, config, state, status=None, trace=None) -> None:
        self.config = config
        self.state = state
        self.status = status
        self.trace = trace
        self._on_feedback = None
        self._proc = None
        self._stdin = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        # command_id → 原命令 cmd 的观测关联（只用于 ros.feedback.rx 补 cmd，绝不进协议/状态）
        self._cmd_lock = threading.Lock()
        self._cmd_by_id: dict = {}

        self._generation = 0
        self._spawn_time = 0.0
        self._spawn_backoff = _SPAWN_BACKOFF_MIN_S
        self._next_spawn_allowed = 0.0

        self.master_available = False
        self.node_ready = False
        self.command_publisher_ready = False
        self.feedback_ready = False
        self.provider_ready = False
        self.battery_provider_seen = False
        self.battery_last_update = None
        self.smoke_provider_seen = False
        self.smoke_last_update = None

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
        if not self.adapter_ready or self._proc is None or self._proc.poll() is not None:
            LOG.info("ROS 未就绪（no child / not ready）：命令无法转发")
            if self.trace:
                self.trace.emit(
                    "ros.command.tx_failed",
                    level="warn",
                    reason="ADAPTER_NOT_READY",
                    cmd=command.get("cmd"),
                    command_id=command.get("command_id"),
                    task_id=command.get("task_id"),
                )
            return False
        with self._stdin_lock:
            if self._stdin is None:
                return False
            try:
                self._stdin.write(json.dumps({"type": "command", "command": command}) + "\n")
                self._stdin.flush()
                cid = command.get("command_id")
                if cid:
                    with self._cmd_lock:
                        self._cmd_by_id[cid] = command.get("cmd")
                        while len(self._cmd_by_id) > 256:
                            self._cmd_by_id.pop(next(iter(self._cmd_by_id)), None)
                if self.trace:
                    self.trace.emit(
                        "ros.command.tx",
                        level="tx",
                        cmd=command.get("cmd"),
                        command_id=command.get("command_id"),
                        task_id=command.get("task_id"),
                        latency_ms=self.trace.latency_ms(command.get("command_id")),
                    )
                return True
            except (BrokenPipeError, ValueError, OSError) as exc:
                LOG.warning("ROS 命令转发失败: %s", exc)
                if self.trace:
                    self.trace.emit(
                        "ros.command.tx_failed",
                        level="warn",
                        reason="BROKEN_PIPE",
                        cmd=command.get("cmd"),
                        command_id=command.get("command_id"),
                    )
                return False

    # ---- 生命周期线程 ----
    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            reachable = ros_master_reachable()
            self.master_available = reachable
            if not reachable:
                if self._proc is not None:
                    LOG.warning("ROS master 丢失：terminate ROS 子进程")
                    self._terminate_child()
            else:
                if self._proc is None:
                    if now >= self._next_spawn_allowed:
                        self._spawn()
                elif self._ready_timed_out(now):
                    LOG.warning("ROS child READY 超时（%.1fs），terminate 后重试", _READY_TIMEOUT_S)
                    if self.trace:
                        self.trace.emit(
                            "ros.child.ready_timeout",
                            level="warn",
                            pid=self._proc.pid if self._proc else None,
                            generation=self._generation,
                            timeout_s=_READY_TIMEOUT_S,
                        )
                    self._bump_backoff()
                    self._terminate_child()
            self._refresh_status()
            self._stop.wait(_POLL_INTERVAL_S)

    def _ready_timed_out(self, now: float) -> bool:
        # 关键就绪 = adapter_ready（command_publisher && feedback），不是 node_ready。
        # node_ready=true 但 publisher/feedback 未就绪时也必须超时重启。
        return (
            self._proc is not None
            and not self.adapter_ready
            and now - self._spawn_time > _READY_TIMEOUT_S
        )

    def _is_current(self, proc, generation: int) -> bool:
        with self._lock:
            return self._generation == generation and self._proc is proc

    def _spawn(self) -> None:
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "firebot_bridge.ros_adapter"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=build_child_env(),
            )
        except Exception as exc:  # noqa: BLE001
            LOG.error("ROS 子进程 spawn 失败: %s", exc)
            self._bump_backoff()
            return
        with self._lock:
            self._generation += 1
            self._proc = proc
            self._stdin = proc.stdin
        self._spawn_time = time.monotonic()
        self._next_spawn_allowed = self._spawn_time
        self._reset_ready()
        threading.Thread(
            target=self._reader_loop,
            args=(proc, self._generation),
            name="ros-child-reader",
            daemon=True,
        ).start()
        LOG.info("ROS 子进程已启动 pid=%s gen=%s", proc.pid, self._generation)
        if self.trace:
            self.trace.emit("ros.child.spawned", level="ok", pid=proc.pid, generation=self._generation)

    def _bump_backoff(self) -> None:
        self._spawn_backoff = min(self._spawn_backoff * 2, _SPAWN_BACKOFF_MAX_S)
        self._next_spawn_allowed = time.monotonic() + self._spawn_backoff
        LOG.warning("ROS child 退避 %.1fs 后允许重启", self._spawn_backoff)
        if self.trace:
            self.trace.emit("ros.child.backoff", level="warn", seconds=self._spawn_backoff)

    def _terminate_child(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._stdin = None
        if proc is None:
            return
        self._reap(proc)
        self._reset_ready()
        self._clear_telemetry()
        self._refresh_status()

    def _reap(self, proc) -> None:
        if proc.poll() is None:
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
        try:
            proc.wait(timeout=1)
        except Exception:  # noqa: BLE001
            pass

    def _on_child_exit(self, proc) -> None:
        """child EOF / 崩溃：reap + 清引用 + 清 readiness/telemetry + 退避。"""
        self._reap(proc)
        if self.trace:
            self.trace.emit(
                "ros.child.exited",
                level="warn",
                pid=getattr(proc, "pid", None),
                generation=self._generation,
                returncode=proc.poll(),
            )
        with self._lock:
            if self._proc is proc:
                self._proc = None
                self._stdin = None
        self._reset_ready()
        self._clear_telemetry()
        self._bump_backoff()
        self._refresh_status()

    def _reset_ready(self) -> None:
        self.node_ready = False
        self.command_publisher_ready = False
        self.feedback_ready = False
        self.provider_ready = False

    def _clear_telemetry(self) -> None:
        self.state.clear_ros_telemetry()
        self.battery_provider_seen = False
        self.battery_last_update = None
        self.smoke_provider_seen = False
        self.smoke_last_update = None
        if self.status:
            self.status.set(
                battery_fresh=False, smoke_fresh=False,
                battery_last_update=None, smoke_last_update=None,
            )

    def _reader_loop(self, proc, generation: int) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                line = line.strip()
                if not line.startswith(_EVENT_PREFIX):
                    continue
                if not self._is_current(proc, generation):
                    continue  # 旧 generation 残留事件丢弃
                try:
                    self._handle_event(json.loads(line[len(_EVENT_PREFIX):]))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._is_current(proc, generation):
                self._on_child_exit(proc)

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
            if self.adapter_ready:
                # 只有真正 command adapter 就绪稳定后才重置退避（不是 Popen 成功就重置）
                self._spawn_backoff = _SPAWN_BACKOFF_MIN_S
        elif t == "feedback":
            fb = payload.get("feedback") or {}
            # 必须先 trace「ROS 已收到」，再进入状态机（否则 ACK trace 会先于 feedback trace）
            if self.trace:
                with self._cmd_lock:
                    cmd = self._cmd_by_id.get(fb.get("command_id"))
                self.trace.emit(
                    "ros.feedback.rx",
                    level="rx",
                    cmd=cmd,
                    state=fb.get("state"),
                    command_id=fb.get("command_id"),
                    task_id=fb.get("task_id"),
                    reason_code=fb.get("reason_code"),
                    message=fb.get("message"),
                    phase=fb.get("phase"),
                    progress=fb.get("progress"),
                    latency_ms=self.trace.latency_ms(fb.get("command_id")),
                )
            if self._on_feedback:
                self._on_feedback(fb)
        elif t == "provider":
            channel = payload.get("channel")
            if channel == "battery":
                value = payload.get("value")
                recovered = self.state.set_battery(value)
                self.battery_provider_seen = True
                self.battery_last_update = time.time()
                if self.status:
                    self.status.set(battery_fresh=True, battery_last_update=self.battery_last_update)
                if self.trace:
                    # 变化 ≥0.1% 立即记；同时每 30s 至少一个快照（独立 key，互不干扰）
                    self.trace.changed(
                        "ros.battery", value, "ros.battery.rx",
                        tolerance=0.1, battery=value, source=self.config.battery_source,
                    )
                    self.trace.throttle(
                        "ros.battery.snapshot", 30.0, "ros.battery.rx",
                        battery=value, source=self.config.battery_source,
                    )
                    if recovered:
                        self.trace.emit(
                            "ros.battery.recovered", level="ok",
                            source=self.config.battery_source,
                        )
            elif channel == "smoke":
                value = payload.get("value")
                recovered = self.state.set_smoke(value)
                self.smoke_provider_seen = True
                self.smoke_last_update = time.time()
                if self.status:
                    self.status.set(smoke_fresh=True, smoke_last_update=self.smoke_last_update)
                if self.trace:
                    self.trace.changed(
                        "ros.smoke", value, "ros.smoke.rx",
                        smoke=value, source=self.config.smoke_source,
                    )
                    if recovered:
                        self.trace.emit(
                            "ros.smoke.recovered", level="ok",
                            source=self.config.smoke_source,
                        )
        elif t == "status":
            # apply_status 只取 mode/estop_active/active_task_id，天然忽略 type 等其它字段
            self.state.apply_status(payload)
            if self.trace:
                self.trace.changed(
                    "ros.status",
                    (payload.get("mode"), payload.get("estop_active"), payload.get("active_task_id")),
                    "ros.status.rx",
                    mode=payload.get("mode"),
                    estop_active=payload.get("estop_active"),
                    active_task_id=payload.get("active_task_id"),
                )
        elif t == "location":
            loc = payload.get("location") or {}
            self.state.set_location(loc)
            if self.trace:
                pos = loc.get("position") or loc
                self.trace.throttle(
                    "ros.location",
                    5.0,
                    "ros.location.rx",
                    x=pos.get("x"),
                    y=pos.get("y"),
                    theta=pos.get("theta"),
                    localization_status=loc.get("localization_status"),
                    enabled=self.config.location_enabled,
                )
        self._refresh_status()

    def _refresh_status(self) -> None:
        if self.trace:
            self.trace.transition(
                "ros.master",
                self.master_available,
                "ros.master.changed",
                state="AVAILABLE" if self.master_available else "UNAVAILABLE",
            )
            self.trace.transition(
                "ros.adapter",
                self.adapter_ready,
                "ros.adapter.changed",
                state="READY" if self.adapter_ready else "NOT_READY",
                node=self.node_ready,
                publisher=self.command_publisher_ready,
                feedback=self.feedback_ready,
            )
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
            smoke_provider_seen=self.smoke_provider_seen,
            smoke_last_update=self.smoke_last_update,
        )
