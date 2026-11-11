"""真实工控机只读采集器。

只读取 /proc、/etc/os-release 与标准库接口；不订阅 ROS/硬件接口，不执行任何进程
或硬件控制。所有函数均为纯读取，文件路径可注入以便测试。
对于未接入的底盘/导航/电池/定位/烟雾/热成像：对应程序进程不存在即上报 False
（或 telemetry 显式 null），绝不生成模拟数值。
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 平台侧 programs 地图已知的程序键（与 mock-vehicle-agent 一致）。
# 真实工控机未接入时对应进程不存在，上报 False，绝不伪造。
ROBOT_PROGRAM_NAMES = ("chassis", "navigation", "patrol", "thermal", "battery", "smog", "camera")


def read_first_field(path: Path) -> float:
    text = path.read_text(encoding="utf-8", errors="replace").split()
    return float(text[0]) if text else 0.0


def os_info(os_release: Path = Path("/etc/os-release")) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in os_release.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"')
    return {
        "name": fields.get("NAME", platform.system()),
        "version_id": fields.get("VERSION_ID", ""),
        "pretty_name": fields.get("PRETTY_NAME", ""),
        "kernel": platform.release(),
        "machine": platform.machine(),
    }


def uptime_seconds(proc_uptime: Path = Path("/proc/uptime")) -> float:
    return read_first_field(proc_uptime)


def load_average() -> tuple[float, float, float]:
    return os.getloadavg()


def memory_bytes(meminfo: Path = Path("/proc/meminfo")) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            values[parts[0][:-1]] = int(parts[1]) * 1024
    return {
        "total": values.get("MemTotal", 0),
        "available": values.get("MemAvailable", 0),
    }


def disk_bytes(mount: Path = Path("/")) -> dict[str, int]:
    usage = shutil.disk_usage(mount)
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def _cmdline_args(pid_dir: Path) -> list[str]:
    raw = pid_dir.joinpath("cmdline").read_bytes()
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def process_liveness(
    markers: dict[str, tuple[str, ...]], proc_root: Path = Path("/proc")
) -> dict[str, bool]:
    """markers: 程序名 -> 进程 cmdline 子串（命中任一即存活）。只读扫描 /proc。"""
    found = {name: False for name in markers}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = _cmdline_args(entry)
        except (OSError, PermissionError):
            continue
        if not args:
            continue
        joined = " ".join(args)
        for name, needles in markers.items():
            if any(needle in joined for needle in needles):
                found[name] = True
    return found


@dataclass(frozen=True)
class HostSnapshot:
    agent_pid: int
    agent_version: str
    os: dict[str, str]
    uptime_seconds: float
    load: tuple[float, float, float]
    memory: dict[str, int]
    disk: dict[str, int]
    programs: dict[str, bool]
    collected_at: str  # ISO-8601 UTC

    def as_dict(self) -> dict:
        """本地可观测形态（默认只写本地日志，不上行）。"""
        return {
            "agent": {"pid": self.agent_pid, "version": self.agent_version},
            "os": self.os,
            "uptime_seconds": self.uptime_seconds,
            "load_average_1_5_15": list(self.load),
            "memory_bytes": self.memory,
            "disk_bytes": self.disk,
            "programs": dict(self.programs),
            "collected_at": self.collected_at,
        }


def collect(
    agent_version: str,
    markers: dict[str, tuple[str, ...]],
    *,
    now: datetime,
    proc_root: Path = Path("/proc"),
) -> HostSnapshot:
    return HostSnapshot(
        agent_pid=os.getpid(),
        agent_version=agent_version,
        os=os_info(),
        uptime_seconds=uptime_seconds(),
        load=load_average(),
        memory=memory_bytes(),
        disk=disk_bytes(),
        programs=process_liveness(markers, proc_root=proc_root),
        collected_at=now.isoformat(),
    )
