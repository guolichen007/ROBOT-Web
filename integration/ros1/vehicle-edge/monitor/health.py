"""统一健康状态模型（VEHICLE_EDGE_RUNTIME_FRESHNESS_HARDENING / P0-3 P0-5）。

状态：HEALTHY / DEGRADED / OFFLINE / UNSAFE / CONFIG_ERROR / INTERNAL_ERROR
exit code：0 / 10 / 20 / 30 / 40 / 50

本模块只读评估，绝不新建 MQTT client / 不发 probe（避免把真实 Bridge client 顶下线）。

HEALTHY 至少要求（P0-5 进程绑定）：
  - firebot-vehicle-edge service active
  - status.pid == service MainPID（status 属于当前进程）
  - status.release_sha == current release SHA（否则 CONFIG_ERROR）
  - mqtt_connected=true
  - heartbeat fresh（last_heartbeat_at 存在且 age <= 5s；>5s DEGRADED，>15s HEARTBEAT_STALE）
  - control disabled/inactive；supported_commands=[]
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

from lib.edge_paths import edge_paths, read_install_env

STATUS_EXIT = {
    "HEALTHY": 0,
    "DEGRADED": 10,
    "OFFLINE": 20,
    "UNSAFE": 30,
    "CONFIG_ERROR": 40,
    "INTERNAL_ERROR": 50,
}

SEVERITY_ORDER = ["HEALTHY", "DEGRADED", "OFFLINE", "UNSAFE", "CONFIG_ERROR", "INTERNAL_ERROR"]

HEARTBEAT_HEALTHY_SECONDS = 5.0
HEARTBEAT_DEGRADED_SECONDS = 15.0


def worse(a: str, b: str) -> str:
    return a if SEVERITY_ORDER.index(a) >= SEVERITY_ORDER.index(b) else b


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def load_status(home: str | None = None) -> dict:
    paths = edge_paths(home)
    try:
        with open(paths["status_json"], encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def systemctl(args: list[str]) -> str:
    # 测试可经 FIREBOT_TEST_SYSTEMCTL 注入 fixture（生产走真实 systemctl）
    cmd = os.environ.get("FIREBOT_TEST_SYSTEMCTL", "systemctl")
    try:
        result = subprocess.run([cmd, *args], capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def service_active(unit: str) -> bool:
    return systemctl(["is-active", unit]) == "active"


def service_main_pid(unit: str) -> int:
    """systemctl show -p MainPID --value；解析失败返回 -1。"""
    try:
        return int(systemctl(["show", unit, "-p", "MainPID", "--value"]) or "-1")
    except ValueError:
        return -1


def current_release_sha(home: str | None = None) -> str:
    """current symlink 指向的 release SHA（releases/<SHA> 的 basename）。"""
    paths = edge_paths(home)
    if not os.path.islink(paths["current"]):
        return ""
    target = os.readlink(paths["current"])
    name = os.path.basename(target)
    return name if len(name) == 40 else ""


def heartbeat_age_seconds(status: dict, now: datetime | None = None) -> float | None:
    """P0-3：动态计算 age = now - last_heartbeat_at（不依赖持久 age 数字）。"""
    last = parse_ts(status.get("last_heartbeat_at"))
    if last is None:
        return None
    return max(0.0, ((now or utcnow()) - last).total_seconds())


def control_state() -> dict:
    return {
        "control_enabled": systemctl(["is-enabled", "firebot-control"]) or "disabled",
        "control_active": service_active("firebot-control"),
    }


def evaluate(home: str | None = None) -> dict:
    paths = edge_paths(home)
    status = load_status(home)
    checks: dict = {}
    overall = "HEALTHY"

    # 配置
    install_env = read_install_env(home or paths["home"])
    if not install_env.get("FIREBOT_EDGE_USER"):
        return {
            "overall": "CONFIG_ERROR",
            "checks": {"config": "MISSING_RUNTIME_USER"},
            "exit_code": STATUS_EXIT["CONFIG_ERROR"],
        }
    checks["config"] = "OK"

    # service（edge 与 legacy 互斥）
    edge_active = service_active("firebot-vehicle-edge")
    legacy_active = service_active("firebot-bridge")
    checks["edge_service_active"] = edge_active
    checks["legacy_bridge_active"] = legacy_active
    if legacy_active and edge_active:
        overall = worse(overall, "UNSAFE")
        checks["mutual_exclusion"] = "VIOLATED"
    else:
        checks["mutual_exclusion"] = "OK"

    # P0-5：status 必须属于当前进程（pid == MainPID）
    main_pid = service_main_pid("firebot-vehicle-edge") if edge_active else -1
    status_pid = status.get("pid") or -1
    checks["main_pid"] = main_pid
    checks["status_pid"] = status_pid
    if edge_active and main_pid > 0:
        if status_pid == main_pid:
            checks["pid_binding"] = "OK"
        else:
            checks["pid_binding"] = "MISMATCH"
            overall = worse(overall, "DEGRADED")
    else:
        checks["pid_binding"] = "N/A"

    # P0-5：status.release_sha == current release SHA（不一致 → CONFIG_ERROR）
    current_sha = current_release_sha(home)
    status_release = status.get("release_sha") or ""
    checks["current_release"] = current_sha
    checks["status_release"] = status_release
    if edge_active and current_sha:
        if status_release and status_release == current_sha:
            checks["release_binding"] = "OK"
        else:
            checks["release_binding"] = "MISMATCH"
            overall = worse(overall, "CONFIG_ERROR")
    else:
        checks["release_binding"] = "N/A"

    # mqtt（本地 canonical status，只读）
    mqtt_connected = status.get("mqtt_connected") is True
    checks["mqtt_connected"] = mqtt_connected
    if mqtt_connected:
        checks["mqtt"] = "CONNECTED"
    elif edge_active:
        checks["mqtt"] = "DISCONNECTED"
        overall = worse(overall, "DEGRADED")
    else:
        checks["mqtt"] = "NONE"
        overall = worse(overall, "OFFLINE")

    # P0-3：heartbeat freshness 动态计算；edge active + mqtt connected + last_heartbeat_at
    # 缺失 → 不得 HEALTHY（DEGRADED）。
    age = heartbeat_age_seconds(status)
    checks["heartbeat_age_seconds"] = age
    if edge_active and mqtt_connected:
        if age is None:
            checks["heartbeat"] = "MISSING"
            overall = worse(overall, "DEGRADED")
        elif age <= HEARTBEAT_HEALTHY_SECONDS:
            checks["heartbeat"] = "FRESH"
        elif age <= HEARTBEAT_DEGRADED_SECONDS:
            checks["heartbeat"] = "AGING"
            overall = worse(overall, "DEGRADED")
        else:
            checks["heartbeat"] = "HEARTBEAT_STALE"
            overall = worse(overall, "DEGRADED")
    else:
        checks["heartbeat"] = "N/A"

    # 只读铁律：control 安全 + supported_commands 空
    ctl = control_state()
    checks.update(ctl)
    if ctl["control_active"] or ctl["control_enabled"] == "enabled":
        overall = worse(overall, "UNSAFE")
    supported = status.get("supported_commands") or []
    checks["supported_commands"] = supported
    if supported:
        overall = worse(overall, "UNSAFE")

    # 服务未运行时的基线
    if overall == "HEALTHY" and not edge_active:
        overall = "OFFLINE"

    return {"overall": overall, "checks": checks, "exit_code": STATUS_EXIT[overall]}
