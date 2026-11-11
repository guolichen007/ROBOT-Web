"""单通信 owner 守卫（fail-closed）。

新 Agent 在两种情况下必须拒绝启动：
1. 旧 vehicle-bridge 仍在运行 —— 避免同一设备出现两个通信 owner；
2. 本 Agent 已有另一实例在运行 —— 本地 lock 文件互斥。

守卫只读取 /proc 与本地 run 目录，不执行任何控制操作。
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass, field
from pathlib import Path

LEGACY_BRIDGE_MARKERS = ("vehicle-bridge", "/opt/firebot/vehicle-bridge")
LOCK_FILE_NAME = "agent.lock"


class OwnerConflictError(RuntimeError):
    """已存在通信 owner，本 Agent 拒绝启动。"""


def legacy_bridge_running(proc_root: Path = Path("/proc")) -> bool:
    """只读扫描 /proc 进程 cmdline，检测旧 vehicle-bridge。"""
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = entry.joinpath("cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        text = raw.decode("utf-8", errors="replace").replace("\0", " ")
        if any(marker in text for marker in LEGACY_BRIDGE_MARKERS):
            return True
    return False


@dataclass
class OwnerGuard:
    run_dir: Path
    proc_root: Path = Path("/proc")
    _lock_fd: int | None = field(default=None, init=False)

    def acquire(self) -> None:
        if legacy_bridge_running(self.proc_root):
            raise OwnerConflictError(
                "legacy vehicle-bridge process detected; readonly agent refuses to "
                "become a second communication owner"
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.run_dir / LOCK_FILE_NAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise OwnerConflictError(f"another readonly agent instance holds {lock_path}") from None
        self._lock_fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())

    def release(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None
