"""统一健康状态模型（VEHICLE_EDGE_HOME_ROOT_CONSOLIDATION / 规格二十七）。

状态：HEALTHY / DEGRADED / OFFLINE / UNSAFE / CONFIG_ERROR / INTERNAL_ERROR
exit code：0 / 10 / 20 / 30 / 40 / 50

评估只读 canonical 本地 status.json + systemd 状态 + 配置；不新建 MQTT client、
不发布/订阅 probe（避免把真实 Bridge client 顶下线）。
"""

from __future__ import annotations

import json
import os
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


def worse(a: str, b: str) -> str:
    """取更严重的状态。"""
    return a if SEVERITY_ORDER.index(a) >= SEVERITY_ORDER.index(b) else b


def load_status(home: str | None = None) -> dict:
    paths = edge_paths(home)
    path = paths["status_json"]
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def service_active(unit: str) -> bool:
    """只读 systemctl 状态（无 root 也能 is-active 查询）。"""
    import subprocess

    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def control_state(home: str | None = None) -> dict:
    """Control 安全判定（只读铁律：active/enabled 任一 → UNSAFE）。"""
    import subprocess

    def state(unit: str) -> str:
        try:
            result = subprocess.run(
                ["systemctl", "is-enabled", unit], capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip() or "disabled"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    enabled = state("firebot-control")
    return {
        "control_enabled": enabled,
        "control_active": service_active("firebot-control"),
    }


def evaluate(home: str | None = None) -> dict:
    """综合评估（规格二十七），返回 {overall, checks, exit_code}。

    UNSAFE 判定：control active/enabled，或 status.json supported_commands 非空
    （只读铁律违反）。
    """
    paths = edge_paths(home)
    status = load_status(home)
    checks: dict = {}
    overall = "HEALTHY"

    # 配置
    install_env = read_install_env(home or paths["home"])
    if not install_env.get("FIREBOT_EDGE_USER"):
        checks["config"] = "MISSING_RUNTIME_USER"
        return {
            "overall": "CONFIG_ERROR",
            "checks": checks,
            "exit_code": STATUS_EXIT["CONFIG_ERROR"],
        }
    checks["config"] = "OK"

    # service
    edge_active = service_active("firebot-vehicle-edge")
    legacy_active = service_active("firebot-bridge")
    checks["edge_service_active"] = edge_active
    checks["legacy_bridge_active"] = legacy_active
    if legacy_active and edge_active:
        checks["mutual_exclusion"] = "VIOLATED"
        overall = worse(overall, "UNSAFE")
    else:
        checks["mutual_exclusion"] = "OK"

    # mqtt（本地 canonical status，只读）
    if status.get("mqtt_connected") is True:
        checks["mqtt"] = "CONNECTED"
    elif edge_active:
        checks["mqtt"] = "DISCONNECTED"
        overall = worse(overall, "DEGRADED")
    else:
        checks["mqtt"] = "NONE"
        overall = worse(overall, "OFFLINE")

    # 心跳新鲜度（heartbeat_age 由本地 status 提供；>15s 视为 DEGRADED）
    hb_age = status.get("heartbeat_age_seconds")
    if isinstance(hb_age, (int, float)) and edge_active:
        checks["heartbeat_age_seconds"] = hb_age
        if hb_age > 15:
            overall = worse(overall, "DEGRADED")
    else:
        checks["heartbeat_age_seconds"] = None

    # 只读铁律：control 安全 + supported_commands 空
    ctl = control_state(home)
    checks.update(ctl)
    if ctl["control_active"] or ctl["control_enabled"] == "enabled":
        overall = worse(overall, "UNSAFE")
    supported = status.get("supported_commands") or []
    checks["supported_commands"] = supported
    if supported:
        overall = worse(overall, "UNSAFE")

    # 服务未运行时最后的状态基线
    if overall == "HEALTHY" and not edge_active:
        overall = "OFFLINE"

    return {"overall": overall, "checks": checks, "exit_code": STATUS_EXIT[overall]}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
