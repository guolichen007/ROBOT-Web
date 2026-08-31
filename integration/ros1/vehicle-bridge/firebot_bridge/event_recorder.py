"""异步事件持久化（Event/Audit/Telemetry）。

Bridge Parent 单一 owner：FieldTrace.emit 把结构化事件放入有界队列，本模块的单 writer
线程异步落盘，绝不阻塞 MQTT/ROS 控制线程。

设计约束：
- fail-open：任何 mkdir/权限/磁盘只读/配置非法/写失败都不得导致 Bridge 退出；
  降级为 no-op，event_logger_ready=false。
- 双队列：event（critical/important）与 telemetry 分离，writer 优先消费 event。
- event 满：event_dropped++ + 健康 DEGRADED；telemetry 满：telemetry_dropped++（可丢弃）。
- 轮转：单文件大小 + 时长 + 文件数 + 目录总容量 + 磁盘最低剩余 多重约束。
- 磁盘不足：先淘汰 telemetry 轮转，绝不删 active，绝不影响 Bridge 控制。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import threading
import time

LOG = logging.getLogger("firebot-bridge")


class _RotatingWriter:
    """单个 JSONL 文件的追加写 + 轮转 + 保留策略。"""

    def __init__(self, events_dir, prefix, active_name, keep, max_bytes, max_age_hours):
        self.events_dir = events_dir
        self.prefix = prefix  # "events" / "telemetry"
        self.active_name = active_name  # "events.jsonl" / "telemetry.jsonl"
        self.active_path = os.path.join(events_dir, active_name)
        self.keep = max(int(keep), 1)
        self.max_bytes = max(int(max_bytes), 1)
        self.max_age_hours = float(max_age_hours)
        self._fh = None
        self._open_at = 0.0
        self._size = 0

    def open(self) -> None:
        self._fh = open(self.active_path, "a", encoding="utf-8")
        self._open_at = time.time()
        try:
            self._size = os.path.getsize(self.active_path)
        except OSError:
            self._size = 0

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def write(self, line: str) -> None:
        if self._fh is None:
            return
        if self._needs_rotate():
            self._rotate()
        self._fh.write(line)
        self._fh.flush()
        self._size += len(line)

    def _needs_rotate(self) -> bool:
        if self._size >= self.max_bytes:
            return True
        if self.max_age_hours > 0 and (time.time() - self._open_at) >= self.max_age_hours * 3600:
            return True
        return False

    def _rotate(self) -> None:
        self.close()
        now = time.time()
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        micro = int((now - int(now)) * 1_000_000)
        new_path = os.path.join(self.events_dir, f"{self.prefix}-{ts}-{micro:06d}.jsonl")
        try:
            os.rename(self.active_path, new_path)
        except OSError:
            pass
        self.open()
        self._prune()

    def _prune(self) -> None:
        """删除超出 keep 的最旧轮转文件（不删 active）。"""
        try:
            names = sorted(
                n for n in os.listdir(self.events_dir)
                if n.startswith(self.prefix + "-") and n.endswith(".jsonl")
            )
        except OSError:
            return
        excess = len(names) - self.keep
        for name in names[:excess]:
            try:
                os.remove(os.path.join(self.events_dir, name))
            except OSError:
                pass


class EventRecorder:
    """异步事件落盘器（双队列，fail-open）。"""

    def __init__(self, config, status=None):
        self._status = status
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._event_dropped = 0
        self._telemetry_dropped = 0
        self._write_errors = 0
        self._degraded = False
        self._disk_available = True
        self._last_health = 0.0
        self._ready = False

        qsize = max(getattr(config, "event_queue_size", 20000), 1)
        self._event_queue = queue.Queue(maxsize=qsize)
        self._telemetry_queue = queue.Queue(maxsize=qsize)

        self.events_dir = getattr(config, "events_dir", "") or ""
        self.min_free_bytes = getattr(config, "event_log_min_free_bytes", 1 * 1024 * 1024 * 1024)
        self.max_total_bytes = getattr(config, "event_log_max_total_bytes", 2 * 1024 * 1024 * 1024)

        self._events_writer = None
        self._tele_writer = None

        if not self.events_dir:
            LOG.warning("FIREBOT_EVENTS_DIR 未配置：事件文件持久化禁用（不影响控制）")
            return
        try:
            os.makedirs(self.events_dir, exist_ok=True)
            self._events_writer = _RotatingWriter(
                self.events_dir, "events", "events.jsonl",
                getattr(config, "event_log_keep", 14),
                getattr(config, "event_log_max_bytes", 10 * 1024 * 1024),
                getattr(config, "event_log_max_age_hours", 24.0),
            )
            self._tele_writer = _RotatingWriter(
                self.events_dir, "telemetry", "telemetry.jsonl",
                getattr(config, "telemetry_log_keep", 7),
                getattr(config, "event_log_max_bytes", 10 * 1024 * 1024),
                getattr(config, "event_log_max_age_hours", 24.0),
            )
            self._ready = True
        except Exception as exc:  # noqa: BLE001 — fail-open
            LOG.warning("事件 recorder 初始化失败（fail-open，不影响控制）: %s", exc)
            self._ready = False

    def start(self) -> None:
        if not self._ready:
            self._publish_health(force=True)
            return
        try:
            self._events_writer.open()
            self._tele_writer.open()
        except Exception as exc:  # noqa: BLE001 — fail-open
            LOG.warning("事件 recorder 打开文件失败（fail-open，不影响控制）: %s", exc)
            self._ready = False
            self._disk_available = False
            self._publish_health(force=True)
            return
        threading.Thread(target=self._writer_loop, name="event-recorder", daemon=True).start()
        self._publish_health(force=True)

    def stop(self) -> None:
        self._stop.set()
        # 有界 drain：把已入队事件尽量落盘（关机路径，可短暂阻塞）
        self._drain(self._event_queue, "events")
        self._drain(self._telemetry_queue, "telemetry")
        try:
            if self._events_writer:
                self._events_writer.close()
            if self._tele_writer:
                self._tele_writer.close()
        except OSError:
            pass
        self._ready = False
        self._publish_health(force=True)

    def enqueue(self, record: dict, importance: str) -> None:
        if not self._ready:
            return
        if importance in ("critical", "important"):
            q = self._event_queue
        else:
            if self._degraded:
                with self._lock:
                    self._telemetry_dropped += 1
                return
            q = self._telemetry_queue
        try:
            q.put_nowait(record)
        except queue.Full:
            with self._lock:
                if importance in ("critical", "important"):
                    self._event_dropped += 1
                else:
                    self._telemetry_dropped += 1
            self._publish_health(force=True)

    # ---- writer 线程 ----
    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            # 1) 优先消费 event 队列（critical/important）
            self._drain(self._event_queue, "events")
            # 2) event 空，消费一条 telemetry（带 timeout，避免忙等）
            try:
                record = self._telemetry_queue.get(timeout=0.5)
            except queue.Empty:
                pass
            else:
                self._write("telemetry", record)
            # 3) 节流发布健康状态
            self._publish_health()

    def _drain(self, q, kind: str) -> None:
        while True:
            try:
                record = q.get_nowait()
            except queue.Empty:
                return
            self._write(kind, record)

    def _write(self, kind: str, record: dict) -> None:
        writer = self._events_writer if kind == "events" else self._tele_writer
        if writer is None:
            return
        try:
            writer.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: BLE001 — 写失败不影响控制
            with self._lock:
                self._write_errors += 1
                self._disk_available = False
            self._degraded = True
            LOG.warning("事件写盘失败（不影响控制）: %s", exc)
        else:
            self._check_total_bytes()
            self._maybe_clear_degraded()

    def _check_total_bytes(self) -> None:
        """目录总容量超限时，先删最旧 telemetry 轮转，再删最旧 events 轮转。"""
        try:
            files = [n for n in os.listdir(self.events_dir) if n.endswith(".jsonl")]
            total = sum(
                os.path.getsize(os.path.join(self.events_dir, n)) for n in files
            )
        except OSError:
            return
        if total <= self.max_total_bytes:
            return
        for prefix in ("telemetry", "events"):
            try:
                names = sorted(
                    n for n in os.listdir(self.events_dir)
                    if n.startswith(prefix + "-") and n.endswith(".jsonl")
                )
            except OSError:
                continue
            for name in names:
                if total <= self.max_total_bytes:
                    return
                path = os.path.join(self.events_dir, name)
                try:
                    sz = os.path.getsize(path)
                    os.remove(path)
                    total -= sz
                except OSError:
                    pass

    def _maybe_clear_degraded(self) -> None:
        """磁盘恢复（滞回 >1.2×min_free）后解除降级。"""
        if not self._degraded:
            with self._lock:
                self._disk_available = True
            return
        try:
            usage = shutil.disk_usage(self.events_dir)
        except OSError:
            return
        if usage.free >= 1.2 * self.min_free_bytes:
            self._degraded = False
            with self._lock:
                self._disk_available = True

    def _publish_health(self, force: bool = False) -> None:
        if self._status is None:
            return
        now = time.monotonic()
        if not force and now - self._last_health < 1.0:
            return
        self._last_health = now
        with self._lock:
            depth = self._event_queue.qsize() + self._telemetry_queue.qsize()
            self._status.set(
                event_logger_ready=self._ready,
                event_queue_depth=depth,
                event_dropped=self._event_dropped,
                telemetry_dropped=self._telemetry_dropped,
                write_errors=self._write_errors,
                disk_log_available=self._disk_available,
            )
