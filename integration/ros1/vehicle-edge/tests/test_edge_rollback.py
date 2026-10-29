#!/usr/bin/env python3
"""edge-rollback 行为测试（B0-FIX-CODE / 规格二十五、二十七、L、M）。

覆盖：
  K. edge-rollback 正常回滚 / 幂等（A/B/C/D）/ legacy config drift / 双 active
  L. Control contract（不启动/不 enable control、不 catkin_make、不 cmd_vel）
  M. Tailscale（不修改 Tailscale）

运行：cd vehicle-edge && python3 tests/test_edge_rollback.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from fake_systemctl import write_fake

TESTS_DIR = Path(__file__).resolve().parent
VEHICLE_EDGE_SRC = TESTS_DIR.parent
ROLLBACK = VEHICLE_EDGE_SRC / "lifecycle" / "rollback.sh"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


class Fixture:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="edge-rollback-"))
        self.edge_home = self.tmp / "firebot-edge"
        self.edge_home.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "ctl_enabled").write_text("disabled")
        (self.state / "ctl_active").write_text("inactive")
        (self.state / "bridge_enabled").write_text("disabled")
        (self.state / "bridge_active").write_text("inactive")
        (self.state / "edge_active").write_text("inactive")
        # legacy unit + bridge.env + status.json（fixture 注入）
        self.legacy_etc = self.tmp / "legacy-etc"
        self.legacy_etc.mkdir()
        (self.legacy_etc / "bridge.env").write_text(
            "FIREBOT_MQTT_HOST=100.110.31.112\nFIREBOT_MQTT_PORT=8883\n")
        self.legacy_unit = self.tmp / "firebot-bridge.service"
        self.legacy_unit.write_text("[Unit]\n")
        self.status = self.tmp / "legacy-status.json"
        self.status.write_text('{"mqtt_connected": true}\n')
        self.syslog = self.tmp / "systemctl.log"
        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self._write_fake_systemctl()
        for f in self.fakebin.iterdir():
            import stat
            f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def _write_fake_systemctl(self) -> None:
        self.fake_systemctl = write_fake(self.fakebin)

    def env(self) -> dict:
        return {
            "PATH": f"{self.fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_TEST_SYSTEMCTL": str(self.fake_systemctl),
            "FIREBOT_EDGE_HOME": str(self.edge_home),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_LEGACY_BRIDGE_ENV": str(self.legacy_etc / "bridge.env"),
            "FIREBOT_TEST_LEGACY_UNIT": str(self.legacy_unit),
            "FIREBOT_TEST_LEGACY_STATUS": str(self.status),
            "FAKE_STATE": str(self.state),
            "FAKE_LOG": str(self.syslog),
        }

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = self.env()
        env.update(extra)
        return subprocess.run(["bash", str(ROLLBACK), *args], env=env,
                              capture_output=True, text=True, timeout=120)


def test_rollback_normal_and_idempotent() -> None:
    fx = Fixture()
    try:
        # 情况 A：edge active, legacy inactive → 正常回滚
        (fx.state / "edge_active").write_text("active")
        r = fx.run()
        check("EDGE_ROLLBACK 正常 PASS（A）", r.returncode == 0 and "EDGE_ROLLBACK=PASS" in r.stdout
              and "LEGACY_ROLLBACK=PASS" in r.stdout, r.stdout[-300:])
        check("MQTT_HOST=100.110.31.112", "MQTT_HOST=100.110.31.112" in r.stdout)
        check("MQTT_CONNECTED=YES", "MQTT_CONNECTED=YES" in r.stdout)
        check("legacy 被 enable/start", (fx.state / "bridge_active").read_text().strip() == "active")
        check("edge 被 disable", (fx.state / "edge_active").read_text().strip() == "inactive")

        # 情况 C：edge inactive, legacy active → ALREADY_ROLLED_BACK
        r2 = fx.run()
        check("EDGE_ROLLBACK 幂等 ALREADY_ROLLED_BACK（C）", r2.returncode == 0
              and "EDGE_ROLLBACK=ALREADY_ROLLED_BACK" in r2.stdout
              and "LEGACY_ROLLBACK=PASS" in r2.stdout, r2.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_rollback_case_b_both_inactive() -> None:
    fx = Fixture()
    try:
        # 情况 B：双 inactive → 启动 legacy
        r = fx.run()
        check("EDGE_ROLLBACK 双 inactive 启动 legacy（B）", r.returncode == 0
              and "EDGE_ROLLBACK=PASS" in r.stdout, r.stdout[-300:])
        check("legacy active", (fx.state / "bridge_active").read_text().strip() == "active")
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_rollback_case_d_mutual_exclusion() -> None:
    fx = Fixture()
    try:
        # 情况 D：edge active + legacy active → REJECTED / MUTUAL_EXCLUSION_VIOLATED
        (fx.state / "edge_active").write_text("active")
        (fx.state / "bridge_active").write_text("active")
        r = fx.run()
        check("EDGE_ROLLBACK 双 active 拒绝（D）", r.returncode != 0
              and "EDGE_ROLLBACK=REJECTED" in r.stdout
              and "MUTUAL_EXCLUSION_VIOLATED" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_rollback_control_not_safe() -> None:
    fx = Fixture()
    try:
        (fx.state / "ctl_active").write_text("active")
        r = fx.run()
        check("EDGE_ROLLBACK CONTROL_NOT_SAFE", r.returncode != 0
              and "EDGE_ROLLBACK=REJECTED" in r.stdout and "CONTROL_NOT_SAFE" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_rollback_legacy_config_drift() -> None:
    fx = Fixture()
    try:
        # legacy bridge.env 漂移（MQTT_HOST 不是 100.110.31.112）
        (fx.legacy_etc / "bridge.env").write_text("FIREBOT_MQTT_HOST=firebot.youdaozd.com\n")
        r = fx.run()
        check("EDGE_ROLLBACK LEGACY_CONFIG_DRIFTED", r.returncode != 0
              and "LEGACY_CONFIG_DRIFTED" in r.stdout
              and "MANUAL_RESTORE_REQUIRED=YES" in r.stdout, r.stdout[-300:])
        check("漂移时不自动启动 legacy", (fx.state / "bridge_active").read_text().strip() == "inactive")
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_rollback_legacy_unit_missing() -> None:
    fx = Fixture()
    try:
        # B0-FINAL-HARDENING：legacy unit 存在性改用 unit_load_state（LoadState=not-found）。
        fx.legacy_unit.unlink()
        (fx.state / "bridge_load").write_text("not-found")
        r = fx.run()
        check("EDGE_ROLLBACK LEGACY_UNIT_MISSING", r.returncode != 0
              and "LEGACY_UNIT_MISSING" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_rollback_static_contracts() -> None:
    fx = Fixture()
    try:
        sh = ROLLBACK.read_text()
        active = [ln for ln in sh.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        # L. Control contract：不得 enable/start/restart firebot-control、catkin_make、cmd_vel
        # （is-enabled/is-active 是只读查询，不算变更动作；只查变更型动词）
        mutates_control = [
            ln for ln in active
            if "firebot-control" in ln
            and any(v in ln for v in ("enable --now firebot-control", "enable firebot-control",
                                      "start firebot-control", "restart firebot-control"))
        ]
        check("无 firebot-control enable/start", not mutates_control, str(mutates_control)[:200])
        check("无 catkin_make", not any("catkin_make" in ln for ln in active))
        check("无 cmd_vel", not any("cmd_vel" in ln for ln in active))
        # M. Tailscale：不得修改 Tailscale（只读 tailscale_is_safe 不算变更动作）
        check("无 tailscale 修改（tailscale up/down/logout/disable）",
              not any(t in ln for ln in active
                      for t in ("tailscale up", "tailscale down", "tailscale logout",
                                "disable tailscaled", "stop tailscaled", "restart tailscaled")))
        # 不得自动覆盖 /etc/firebot（cp/mv 到 legacy etc）
        check("不自动覆盖 legacy config（无 cp/mv 到 /etc/firebot）",
              not any(("cp" in ln or "mv" in ln) and "/etc/firebot" in ln for ln in active))
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


if __name__ == "__main__":
    test_rollback_normal_and_idempotent()
    test_rollback_case_b_both_inactive()
    test_rollback_case_d_mutual_exclusion()
    test_rollback_control_not_safe()
    test_rollback_legacy_config_drift()
    test_rollback_legacy_unit_missing()
    test_rollback_static_contracts()
    print(f"\nEDGE_ROLLBACK_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
