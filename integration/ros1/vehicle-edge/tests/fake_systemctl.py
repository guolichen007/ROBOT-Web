"""共享 fake systemctl helper（B0-FINAL-HARDENING）。

测试 fixture 统一使用 tests/fake_systemctl.sh 作为 fake systemctl，
避免各测试各自实现 is-active/is-enabled/show/mutation 语义不一致。
"""
from __future__ import annotations

import shutil
import stat
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
FAKE_SRC = TESTS_DIR / "fake_systemctl.sh"

# 与 fake_systemctl.sh 的 fname() 一致的旧名映射
_FIELDS = {
    "firebot-bridge": {"active": "bridge_active", "enabled": "bridge_enabled", "load": "bridge_load", "mainpid": "bridge_mainpid"},
    "firebot-vehicle-edge": {"active": "edge_active", "enabled": "edge_enabled", "load": "edge_load", "mainpid": "main_pid"},
    "firebot-control": {"active": "ctl_active", "enabled": "ctl_enabled", "load": "ctl_load", "mainpid": "ctl_mainpid"},
    "tailscaled": {"active": "tailscaled_active", "enabled": "tailscaled_enabled", "load": "tailscaled_load", "mainpid": "tailscaled_mainpid"},
    "systemd-timesyncd": {"active": "timesyncd_active", "enabled": "timesyncd_enabled", "load": "timesyncd_load", "mainpid": "timesyncd_mainpid"},
}


def write_fake(fakebin: Path) -> Path:
    """把 canonical fake systemctl 写入 fakebin/。"""
    fakebin.mkdir(parents=True, exist_ok=True)
    dst = fakebin / "systemctl"
    shutil.copy(FAKE_SRC, dst)
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dst


def set_state(state_dir: Path, unit: str, active: str | None = None,
              enabled: str | None = None, load: str | None = None,
              mainpid: str | None = None) -> None:
    """写 per-unit 状态文件（缺省字段不写，保留 fake 默认）。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    fields = _FIELDS.get(unit, {})
    if active is not None:
        (state_dir / fields.get("active", f"{unit}.active")).write_text(active)
    if enabled is not None:
        (state_dir / fields.get("enabled", f"{unit}.enabled")).write_text(enabled)
    if load is not None:
        (state_dir / fields.get("load", f"{unit}.load")).write_text(load)
    if mainpid is not None:
        (state_dir / fields.get("mainpid", f"{unit}.mainpid")).write_text(mainpid)


def default_state(state_dir: Path) -> None:
    """默认安全状态：legacy down、edge down、control not-found（safe）、tailscale active。"""
    set_state(state_dir, "firebot-bridge", active="inactive", enabled="disabled", load="loaded")
    set_state(state_dir, "firebot-vehicle-edge", active="inactive", enabled="disabled", load="loaded")
    set_state(state_dir, "firebot-control", load="not-found")
    set_state(state_dir, "tailscaled", active="active", load="loaded")
