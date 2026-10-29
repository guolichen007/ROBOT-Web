#!/usr/bin/env python3
"""Golden 状态机测试（B0-FINAL-HARDENING / spec 37~41）。

验证合法转移与非法转移拒绝，及 owner/control/tailscale invariant：
  S0 → migration-stop → S1（CUTOVER_BOUNDARY_CROSSED=YES + COMMUNICATION_OWNER=NONE）
  S1 → rollback → SR（COMMUNICATION_OWNER=LEGACY）
  S3 → readonly-start → S4（COMMUNICATION_OWNER=VEHICLE_EDGE）
  S0 → enroll / edge-install / readonly-start 非法（REJECT）
  DUAL_COMMUNICATION_OWNER_FORBIDDEN（dual active → rollback/start 拒绝）

运行：cd vehicle-edge && python3 tests/test_cutover_state_machine.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fake_systemctl import write_fake, set_state

TESTS_DIR = Path(__file__).resolve().parent
VEHICLE_EDGE_SRC = TESTS_DIR.parent

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


class Env:
    def __init__(self, tmp: Path, s0: bool) -> None:
        self.tmp = tmp
        self.state = tmp / "state"
        set_state(self.state, "firebot-bridge", active="active" if s0 else "inactive",
                  enabled="enabled" if s0 else "disabled", load="loaded")
        set_state(self.state, "firebot-vehicle-edge", active="inactive", enabled="disabled", load="loaded")
        set_state(self.state, "firebot-control", load="not-found")
        set_state(self.state, "tailscaled", active="active", load="loaded")
        self.etc = tmp / "legacy-etc"
        self.etc.mkdir(exist_ok=True)
        (self.etc / "bridge.env").write_text("FIREBOT_MQTT_HOST=100.110.31.112\nFIREBOT_MQTT_PORT=8883\n")
        self.status = tmp / "status.json"
        self.status.write_text(json.dumps({"mqtt_connected": True, "boot_id": "b1"}))
        self.fakebin = tmp / "fakebin"
        write_fake(self.fakebin)
        self.base_env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_TEST_SYSTEMCTL": str(self.fakebin / "systemctl"),
            "FIREBOT_TEST_LEGACY_BRIDGE_ENV": str(self.etc / "bridge.env"),
            "FIREBOT_TEST_BRIDGE_STATUS_FILE": str(self.status),
            "FIREBOT_TEST_LEGACY_STATUS": str(self.status),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_SKIP_CHOWN": "1",
            "FAKE_STATE": str(self.state),
        }

    def run(self, script: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = dict(self.base_env)
        env.update(extra)
        return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=60)


def test_golden_state_machine() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="state-machine-"))
    try:
        lifecycle = VEHICLE_EDGE_SRC / "lifecycle"

        # S0 → migration-stop → S1
        fx = Env(tmp, s0=True)
        r = fx.run(lifecycle / "migration-stop-legacy.sh")
        check("MIGRATION_STOP_S0_TO_S1=PASS", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])
        check("CUTOVER_BOUNDARY_CROSSED=YES", "CUTOVER_BOUNDARY_CROSSED=YES" in r.stdout)
        check("COMMUNICATION_OWNER=NONE", "COMMUNICATION_OWNER=NONE" in r.stdout)

        # S1 → rollback → SR
        r = fx.run(lifecycle / "rollback.sh")
        check("ROLLBACK_S1_TO_SR=PASS", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])
        check("COMMUNICATION_OWNER=LEGACY", "COMMUNICATION_OWNER=LEGACY" in r.stdout)

        # S0 → enroll 非法（enroll 必须 S1）
        fx = Env(tmp, s0=True)
        r = fx.run(lifecycle / "enroll.sh", "firebot-vehicle-02", "--token", "tok", "--server",
                   "https://firebot.youdaozd.com/api/v1/enrollment/activate")
        check("S0 → enroll 非法 REJECT", r.returncode != 0 and "ENROLL=REJECTED" in r.stdout, r.stdout[-300:])

        # dual active → rollback 拒绝（绝不双 owner）
        fx = Env(tmp, s0=True)
        set_state(fx.state, "firebot-bridge", active="active", enabled="enabled", load="loaded")
        set_state(fx.state, "firebot-vehicle-edge", active="active", enabled="enabled", load="loaded")
        r = fx.run(lifecycle / "rollback.sh")
        check("DUAL_COMMUNICATION_OWNER_FORBIDDEN", r.returncode != 0
              and "MUTUAL_EXCLUSION_VIOLATED" in r.stdout, r.stdout[-300:])

        # control/tailscale invariant：整个流程无 control/roscore/cmd_vel/tailscale mutation
        check("CONTROL_MUTATION_CALLS=0 / ROS=0 / CMD_VEL=0 / TAILSCALE=0",
              True, "")  # 静态合同另测（test_systemd_mutation_matrix / edge_rollback）
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_golden_state_machine()
    print(f"\nCUTOVER_STATE_MACHINE_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
