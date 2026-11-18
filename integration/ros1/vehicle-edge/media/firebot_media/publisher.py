"""发布主循环：一路相机一个进程（firebot-media@<camera>.service）。

生命周期语义（修复草稿 1c4bf74 的 wait -n || true 等缺陷）：
  - 永久配置错误 → 退出码 78（EX_CONFIG），systemd RestartPreventExitStatus=78 不再重启；
  - 可恢复故障（相机/源不可用、网络断开、云端拒绝）在进程内指数退避重试，进程不退出 →
    systemd StartLimit 永远不会被耗尽，长时间断网恢复后自动续推；
  - 未预期异常 → 退出码 1（非 0），systemd Restart=always 兜底；
  - ffmpeg 子进程：独立进程组 + PR_SET_PDEATHSIG(SIGKILL)，父进程任何方式退出都不留孤儿；
    SIGTERM → 先停 ffmpeg（TERM→5s→KILL）→ 关闭 SRT → 退出 0。
  - 先确认源产出首个 TS 包，再建立 SRT 发布会话（相机缺失时不在云端留下空会话）。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Deque, Optional

from . import __version__
from .config import EX_CONFIG, ConfigError, MediaConfig, load_config
from .log import Logger
from .sources import RtspInput, build_command, probe_rtsp_codec, select_encoder
from .srt import SrtError, SrtSender, library_version, load_lib

EX_SOFTWARE = 1
PR_SET_PDEATHSIG = 1

STATE_STARTING = "STARTING"
STATE_SOURCE_WAIT = "SOURCE_UNAVAILABLE"
STATE_SOURCE_STALLED = "SOURCE_STALLED"
STATE_DEVICE_BUSY = "DEVICE_BUSY"
STATE_CONNECTING = "CONNECTING"
STATE_LIVE = "LIVE"
STATE_NET_DOWN = "NETWORK_DOWN"
STATE_AUTH = "AUTH_REJECTED"
STATE_CONFIG = "CONFIG_ERROR"
STATE_STOPPED = "STOPPED"


class SourceError(Exception):
    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


try:  # 父进程预加载 libc：fork 后的 preexec_fn 内不得 dlopen（多线程 fork 死锁风险）
    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:  # pragma: no cover
    _LIBC = None


def _pdeathsig() -> None:
    os.setsid()
    if _LIBC is not None:
        _LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


class Publisher:
    def __init__(self, cfg: MediaConfig, log: Logger) -> None:
        self.cfg = cfg
        self.log = log
        self.stop = threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.sender: Optional[SrtSender] = None
        self.encoder = ""
        self.state = STATE_STARTING
        self.detail = ""
        self.last_live_at = 0.0
        self.reconnects = 0
        self.stderr_tail: Deque[str] = deque(maxlen=20)
        self._backoff = cfg.backoff_initial_s

    # ---------------- status ----------------
    def write_status(self, state: str, detail: str = "") -> None:
        if state != self.state or detail != self.detail:
            self.log.log("INFO", "MEDIA_STATE=%s %s" % (state, detail))
        self.state, self.detail = state, detail
        data = {
            "schema": 1,
            "camera": self.cfg.camera.camera,
            "vehicle_id": self.cfg.vehicle_id,
            "stream_id": self.cfg.stream_id,
            "provider": self.cfg.camera.provider,
            "encoder": self.encoder,
            "state": state,
            "detail": self.log.redact(detail)[:200],
            "updated_at": time.time(),
            "last_live_at": self.last_live_at or None,
            "reconnects": self.reconnects,
            "bytes_sent": self.sender.bytes_sent if self.sender else 0,
            "pid": os.getpid(),
            "version": __version__,
        }
        try:
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            path = os.path.join(self.cfg.state_dir, "%s.json" % self.cfg.camera.camera)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    # ---------------- child process ----------------
    def _start_ffmpeg(self) -> Optional[RtspInput]:
        rtsp = None
        codec = None
        if self.cfg.camera.provider == "rtsp":
            rtsp = RtspInput(self.cfg)
            self.log.add_secrets([rtsp.url])
            codec = probe_rtsp_codec(rtsp)
            if codec is None:
                rtsp.close()
                raise SourceError(STATE_SOURCE_WAIT, "RTSP 源不可达或无视频流")
        cmd, copy = build_command(self.cfg, self.encoder, rtsp, codec)
        if copy:
            self.log.log("INFO", "RTSP 实测 H.264/yuv420p → 转封装（-c copy）")
        elif codec:
            self.log.log(
                "INFO",
                "RTSP 实测 codec=%s pix_fmt=%s → 转码 %s"
                % (codec.get("codec_name"), codec.get("pix_fmt"), self.encoder),
            )
        self.stderr_tail.clear()
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_pdeathsig,
            close_fds=True,
        )
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        return rtsp

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail.append(line)
                self.log.log("FFMPEG", line)

    def _stop_ffmpeg(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def _classify_source_exit(self) -> SourceError:
        tail = " ".join(self.stderr_tail).lower()
        if "busy" in tail:
            return SourceError(STATE_DEVICE_BUSY, "相机设备被占用（ROS 采集节点持有）；不争用")
        if "no such file" in tail or "no such device" in tail:
            return SourceError(STATE_SOURCE_WAIT, "相机设备不存在")
        if "connection refused" in tail or "failed to resolve" in tail or "end of file" in tail:
            return SourceError(STATE_SOURCE_WAIT, "视频源不可达（web_video_server 未运行？）")
        return SourceError(
            STATE_SOURCE_WAIT, "视频源进程退出 rc=%s" % (self.proc.returncode if self.proc else "?")
        )

    def _read(self, timeout: float) -> bytes:
        assert self.proc is not None and self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return b""
        data = os.read(fd, 1316 * 64)
        if not data:
            raise self._classify_source_exit()
        return data

    # ---------------- one session ----------------
    def run_once(self) -> None:
        rtsp = self._start_ffmpeg()
        try:
            self.write_status(STATE_STARTING, "等待视频源首帧")
            first = b""
            deadline = time.monotonic() + self.cfg.camera.stall_timeout_s
            while not first:
                if self.stop.is_set():
                    return
                if self.proc and self.proc.poll() is not None:
                    raise self._classify_source_exit()
                if time.monotonic() > deadline:
                    raise SourceError(
                        STATE_SOURCE_STALLED,
                        "视频源 %.0fs 无帧（相机缺失或话题未发布）"
                        % self.cfg.camera.stall_timeout_s,
                    )
                first = self._read(0.5)
            self.write_status(STATE_CONNECTING, "%s:%d" % (self.cfg.srt_host, self.cfg.srt_port))
            self.sender = SrtSender(
                self.cfg.srt_host,
                self.cfg.srt_port,
                streamid=self.cfg.streamid,
                passphrase=self.cfg.passphrase,
                latency_ms=self.cfg.srt_latency_ms,
            )
            self.sender.connect()
            self.sender.send(first)
            self.last_live_at = time.time()
            self.write_status(STATE_LIVE, "encoder=%s" % self.encoder)
            self._backoff = self.cfg.backoff_initial_s
            last_data = time.monotonic()
            last_status = 0.0
            while not self.stop.is_set():
                data = self._read(1.0)
                now = time.monotonic()
                if data:
                    self.sender.send(data)
                    last_data = now
                    if now - last_status > 5:
                        self.last_live_at = time.time()
                        self.write_status(STATE_LIVE, "encoder=%s" % self.encoder)
                        last_status = now
                elif now - last_data > self.cfg.camera.stall_timeout_s:
                    raise SourceError(
                        STATE_SOURCE_STALLED, "视频源中断 %.0fs" % self.cfg.camera.stall_timeout_s
                    )
        finally:
            self._stop_ffmpeg()
            if self.sender:
                self.sender.close()
            if rtsp:
                rtsp.close()

    def _sleep(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def run(self) -> int:
        while not self.stop.is_set():
            try:
                self.run_once()
            except SourceError as exc:
                self.write_status(exc.state, str(exc))
                wait = max(self._backoff, 30.0) if exc.state == STATE_DEVICE_BUSY else self._backoff
            except SrtError as exc:
                if exc.kind == "AUTH":
                    self.write_status(STATE_AUTH, str(exc))
                    wait = self.cfg.auth_backoff_s
                elif exc.kind == "LIB":
                    self.write_status(STATE_CONFIG, str(exc))
                    return EX_CONFIG
                else:
                    self.write_status(STATE_NET_DOWN, str(exc))
                    wait = self._backoff
            except ConfigError as exc:
                self.write_status(STATE_CONFIG, str(exc))
                return EX_CONFIG
            else:
                break
            if self.stop.is_set():
                break
            self.reconnects += 1
            self._backoff = min(self._backoff * 2, self.cfg.backoff_max_s)
            self._sleep(wait)
        self.write_status(STATE_STOPPED, "")
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="firebot-media-publisher")
    parser.add_argument("camera")
    parser.add_argument(
        "--edge-home", default=os.environ.get("FIREBOT_EDGE_HOME", "/home/tl/firebot-edge")
    )
    parser.add_argument(
        "--check", action="store_true", help="只做配置/凭证/编码器/libsrt 预检，不推流"
    )
    args = parser.parse_args(argv)
    log = Logger(args.camera)
    try:
        cfg = load_config(args.edge_home, args.camera)
    except ConfigError as exc:
        log.log("ERROR", "MEDIA_CONFIG=FAIL %s" % exc, force=True)
        return EX_CONFIG
    log = Logger(args.camera, cfg.log_lines_per_minute)
    log.add_secrets(cfg.secret_values())
    try:
        lib = load_lib(os.environ.get("FIREBOT_MEDIA_LIBSRT"), args.edge_home)
        encoder = select_encoder(cfg)
    except (ConfigError, SrtError) as exc:
        log.log("ERROR", "MEDIA_PREFLIGHT=FAIL %s" % exc, force=True)
        pub = Publisher(cfg, log)
        pub.write_status(STATE_CONFIG, str(exc))
        return EX_CONFIG
    log.log(
        "INFO",
        "MEDIA_PREFLIGHT=PASS stream_id=%s provider=%s encoder=%s libsrt=%s"
        % (cfg.stream_id, cfg.camera.provider, encoder, library_version(lib)),
        force=True,
    )
    if args.check:
        return 0
    pub = Publisher(cfg, log)
    pub.encoder = encoder

    def _term(signum, _frame):
        pub.stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        return pub.run()
    except Exception as exc:  # noqa: BLE001 — 未预期异常：非 0 退出交 systemd 重启
        log.log("ERROR", "MEDIA_INTERNAL_ERROR %s: %s" % (type(exc).__name__, exc), force=True)
        pub._stop_ffmpeg()
        return EX_SOFTWARE


if __name__ == "__main__":
    sys.exit(main())
