"""firebot-edge 路径与安装配置解析（唯一 source of truth）。

工程边界（VEHICLE_EDGE_HOME_ROOT_CONSOLIDATION）：
  /home/tl/ROBOT-Web      Git 源码/交付源
  /home/tl/firebot-edge   车辆↔云通信 Runtime（本模块）
  /home/tl/firerobot_ws   ROS/底盘/导航/控制 Runtime（本模块绝不触碰）

Edge Home 结构（见 vehicle-edge/README.md）：
  releases/<SHA>/ current/ previous/ config/ certs/ secrets/ runtime/ data/ logs/
  backups/ tools/ docs/ manifest.json VERSION

权限模型（edge-init 建立；测试可经 FIREBOT_TEST_EDGE_HOME 注入）：
  EDGE_HOME root:root 0755；releases/current/previous root-only 更新；
  config 0644 root:root；certs 0644；secrets 0700/0600；runtime/data/logs tl:tl 0750；
  backups root:root 0700。

兼容：旧 /etc/firebot 仅作 legacy discovery/read 来源，不再是 canonical。
"""

from __future__ import annotations

import os

DEFAULT_EDGE_HOME = "/home/tl/firebot-edge"
LEGACY_ETC = "/etc/firebot"


def edge_home() -> str:
    """canonical Edge Home（测试注入 > install.env > 默认）。"""
    env = os.environ.get("FIREBOT_EDGE_HOME")
    if env:
        return env
    install_env = os.path.join(DEFAULT_EDGE_HOME, "config", "install.env")
    try:
        for line in open(install_env, encoding="utf-8"):
            line = line.strip()
            if line.startswith("FIREBOT_EDGE_HOME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return DEFAULT_EDGE_HOME


def read_install_env(home: str) -> dict:
    """读取 config/install.env（fail-closed：缺失 runtime user 即 CONFIG_ERROR）。"""
    path = os.path.join(home, "config", "install.env")
    out: dict = {}
    if not os.path.isfile(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"')
    return out


def runtime_user(home: str) -> str:
    """显式配置的 runtime user；缺失返回空（调用方 fail-closed：
    EDGE_RUNTIME_USER_NOT_CONFIGURED）。禁止 id -un / sudo 自动猜。"""
    return read_install_env(home).get("FIREBOT_EDGE_USER", "")


def edge_paths(home: str | None = None) -> dict:
    """Edge Home 全部规范路径。"""
    root = home or edge_home()
    return {
        "home": root,
        "config": os.path.join(root, "config"),
        "install_env": os.path.join(root, "config", "install.env"),
        "identity_env": os.path.join(root, "config", "identity.env"),
        "bootstrap_env": os.path.join(root, "config", "bootstrap.env"),
        "assignment_env": os.path.join(root, "config", "assignment.env"),
        "bridge_env": os.path.join(root, "config", "bridge.env"),
        "profile_env": os.path.join(root, "config", "profile.env"),
        "certs": os.path.join(root, "certs"),
        "mqtt_ca": os.path.join(root, "certs", "mqtt-ca.crt"),
        "secrets": os.path.join(root, "secrets"),
        "mqtt_env": os.path.join(root, "secrets", "mqtt.env"),
        "device_token": os.path.join(root, "secrets", "device-token"),
        "media_secret": os.path.join(root, "secrets", "media-secret.env"),
        "runtime": os.path.join(root, "runtime"),
        "status_json": os.path.join(root, "runtime", "status.json"),
        "health_json": os.path.join(root, "runtime", "health.json"),
        "data": os.path.join(root, "data"),
        "connection_state": os.path.join(root, "data", "connection-state.json"),
        "logs": os.path.join(root, "logs"),
        "connection_log": os.path.join(root, "logs", "connection.log"),
        "audit_log": os.path.join(root, "logs", "audit.log"),
        "backups": os.path.join(root, "backups"),
        "releases": os.path.join(root, "releases"),
        "current": os.path.join(root, "current"),
        "previous": os.path.join(root, "previous"),
        "manifest": os.path.join(root, "manifest.json"),
        "version": os.path.join(root, "VERSION"),
        # 管理平面 tools release domain（与数据平面 releases 独立）。
        "tools": os.path.join(root, "tools"),
        "tools_releases": os.path.join(root, "tools", "releases"),
        "tools_current": os.path.join(root, "tools", "current"),
        "tools_previous": os.path.join(root, "tools", "previous"),
        "tools_bootstrap": os.path.join(root, "tools", "bootstrap"),
    }


def legacy_bootstrap_env() -> str:
    """legacy 兼容来源（只读 discovery）。"""
    return os.path.join(LEGACY_ETC, "bootstrap.env")


def read_kv(path: str, key: str) -> str:
    """读 KV 文件中的键（无则空）。"""
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""
