"""脱敏 + 限速日志（stderr → journald）。

- 所有输出经 redact：已知 secret 值、streamid/passphrase URL 参数、rtsp://user:pass@ 一律掩码；
- 每分钟最多 N 行（超出计数，下一窗口汇总一行 "suppressed=K"），journald 侧另有
  LogRateLimit 兜底；进程不写任何文件日志（无文件即无无限增长风险）。
"""

from __future__ import annotations

import re
import sys
import time
from typing import Iterable, List

_URL_SECRET = re.compile(r"(streamid|passphrase|password|token|pass)=([^&\s'\"]+)", re.I)
_RTSP_CRED = re.compile(r"(rtsp|rtsps|http|https)://[^/\s:@]+:[^/\s@]+@", re.I)
_STREAMID = re.compile(r"publish:[^\s'\"]+")


class Logger:
    def __init__(self, camera: str, lines_per_minute: int = 30, stream=None) -> None:
        self._camera = camera
        self._limit = lines_per_minute
        self._secrets: List[str] = []
        self._window = 0.0
        self._count = 0
        self._suppressed = 0
        self._stream = stream or sys.stderr

    def add_secrets(self, values: Iterable[str]) -> None:
        for v in values:
            if v and len(v) >= 4 and v not in self._secrets:
                self._secrets.append(v)
        self._secrets.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        text = _STREAMID.sub("publish:***", text)
        text = _URL_SECRET.sub(lambda m: m.group(1) + "=***", text)
        text = _RTSP_CRED.sub(lambda m: m.group(1) + "://***@", text)
        return text

    def log(self, level: str, msg: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if now - self._window >= 60:
            if self._suppressed:
                self._emit(
                    "WARN",
                    "MEDIA_LOG_SUPPRESSED count=%d（限速 %d 行/分钟）"
                    % (self._suppressed, self._limit),
                )
            self._window, self._count, self._suppressed = now, 0, 0
        if not force and self._count >= self._limit:
            self._suppressed += 1
            return
        self._count += 1
        self._emit(level, msg)

    def _emit(self, level: str, msg: str) -> None:
        line = "%s camera=%s %s" % (level, self._camera, self.redact(msg))
        try:
            self._stream.write(line[:600] + "\n")
            self._stream.flush()
        except (BrokenPipeError, ValueError):
            pass
