#!/usr/bin/env python3
"""真实 systemd exit 语义 + systemctl show 权威读取回归测试（B0-FIX3 / B0-FINAL-HARDENING）。

真实 systemd：
  is-active  active   → stdout=active   exit 0
  is-active  inactive → stdout=inactive exit 3（非 0）
  is-enabled enabled  → stdout=enabled  exit 0
  is-enabled disabled → stdout=disabled exit 1（非 0）
  systemctl show 缺失/无法识别 → unknown（fail-closed）

本测试证明：
  1. unit_active_state / unit_enabled_state 输出单一状态（无重复行）；unknown fail-closed
  2. migration-stop-legacy cutover boundary 记账（CUTOVER_STARTED / CUTOVER_BOUNDARY_CROSSED / UNCERTAIN fail-closed）

运行：cd vehicle-edge && python3 tests/test_real_systemd_semantics.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fake_systemctl import write_fake, set_state

TESTS_DIR = Path(__file__).resolve().parent
VEHICLE_EDGE_SRC = TESTS_DIR.parent
LIB = VEHICLE_EDGE_SRC / "lib"

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


def make_fakebin(tmp: Path) -> Path:
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    write_fake(fakebin)
    return fakebin


def make_state(tmp: Path) -> Path:
    state = tmp / "state"
    set_state(state, "firebot-bridge", active="active", enabled="enabled", load="loaded")
    set_state(state, "firebot-vehicle-edge", active="inactive", enabled="disabled", load="loaded")
    set_state(state, "firebot-control", active="inactive", enabled="disabled", load="loaded")
    set_state(state, "tailscaled", active="active", load="loaded")
    return state


def env_for(fakebin: Path, state: Path, **extra: str) -> dict:
    e = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
        "FIREBOT_TEST_SYSTEMCTL": str(fakebin / "systemctl"),
        "FAKE_STATE": str(state),
        "FIREBOT_TEST_SKIP_ROOT": "1",
        "FIREBOT_TEST_SKIP_CHOWN": "1",
    }
    e.update(extra)
    return e


def test_unit_state_helper() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="real-systemd-helper-"))
    try:
        state = make_state(tmp)
        fakebin = make_fakebin(tmp)
        env = env_for(fakebin, state)

        # inactive(exit 3) → 单一 "inactive"
        set_state(state, "firebot-bridge", active="inactive")
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_active_state firebot-bridge'],
            env=env, capture_output=True, text=True, timeout=60)
        check("unit_active_state inactive → 单一值", r.stdout == "inactive\n", repr(r.stdout))

        set_state(state, "firebot-bridge", active="active")
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_active_state firebot-bridge'],
            env=env, capture_output=True, text=True, timeout=60)
        check("unit_active_state active → active", r.stdout == "active\n", repr(r.stdout))

        set_state(state, "firebot-control", enabled="disabled")
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_enabled_state firebot-control'],
            env=env, capture_output=True, text=True, timeout=60)
        check("unit_enabled_state disabled → 单一值", r.stdout == "disabled\n", repr(r.stdout))

        set_state(state, "firebot-control", enabled="enabled")
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_enabled_state firebot-control'],
            env=env, capture_output=True, text=True, timeout=60)
        check("unit_enabled_state enabled → enabled", r.stdout == "enabled\n", repr(r.stdout))

        # systemctl show 失败（DBus/systemd 故障）→ unknown（fail-closed）
        env_fail = dict(env)
        env_fail["FAKE_SHOW_FAIL"] = "1"
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/systemd_state.sh"; unit_active_state firebot-bridge'],
            env=env_fail, capture_output=True, text=True, timeout=60)
        check("unit_active_state show 失败 → unknown（fail-closed）", r.stdout == "unknown\n", repr(r.stdout))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_migration_stop_boundary() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="real-systemd-stop-"))
    try:
        state = make_state(tmp)
        fakebin = make_fakebin(tmp)
        # legacy 健康：bridge.env 精确 host + status.json connected
        etc = tmp / "etc-firebot"
        etc.mkdir()
        (etc / "bridge.env").write_text("FIREBOT_MQTT_HOST=100.110.31.112\n")
        status = tmp / "bridge-status.json"
        status.write_text('{"mqtt_connected": true, "boot_id": "b1"}\n')
        env = env_for(fakebin, state,
                      FIREBOT_TEST_LEGACY_BRIDGE_ENV=str(etc / "bridge.env"),
                      FIREBOT_TEST_BRIDGE_STATUS_FILE=str(status))
        script = VEHICLE_EDGE_SRC / "lifecycle" / "migration-stop-legacy.sh"
        r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
        out = r.stdout
        check("migration-stop 成功", r.returncode == 0, out[-400:] + r.stderr[-400:])
        check("CUTOVER_STARTED=YES", "CUTOVER_STARTED=YES" in out)
        check("CUTOVER_ACTION=STOP_LEGACY", "CUTOVER_ACTION=STOP_LEGACY" in out)
        check("CUTOVER_BOUNDARY_CROSSED=YES", "CUTOVER_BOUNDARY_CROSSED=YES" in out)
        check("COMMUNICATION_OWNER=NONE", "COMMUNICATION_OWNER=NONE" in out)
        check("MIGRATION_CUTOVER_BOUNDARY=ENTERED", "MIGRATION_CUTOVER_BOUNDARY=ENTERED" in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_migration_stop_boundary_uncertain() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="real-systemd-uncertain-"))
    try:
        state = make_state(tmp)
        fakebin = make_fakebin(tmp)
        etc = tmp / "etc-firebot"
        etc.mkdir()
        (etc / "bridge.env").write_text("FIREBOT_MQTT_HOST=100.110.31.112\n")
        status = tmp / "bridge-status.json"
        status.write_text('{"mqtt_connected": true, "boot_id": "b1"}\n')
        env = env_for(fakebin, state,
                      FIREBOT_TEST_LEGACY_BRIDGE_ENV=str(etc / "bridge.env"),
                      FIREBOT_TEST_BRIDGE_STATUS_FILE=str(status),
                      FAKE_POST_ACTIVE="failed")
        script = VEHICLE_EDGE_SRC / "lifecycle" / "migration-stop-legacy.sh"
        r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
        out = r.stdout
        check("migration-stop UNCERTAIN fail-closed", r.returncode != 0, out[-400:] + r.stderr[-400:])
        check("MIGRATION_STOP_LEGACY=UNCERTAIN", "MIGRATION_STOP_LEGACY=UNCERTAIN" in out)
        check("CUTOVER_STARTED=YES（stop 已发出）", "CUTOVER_STARTED=YES" in out)
        check("CUTOVER_STATE=UNCERTAIN", "CUTOVER_STATE=UNCERTAIN" in out)
        check("绝不输出 CUTOVER_STARTED=NO", "CUTOVER_STARTED=NO" not in out)
        check("不输出 CUTOVER_BOUNDARY_CROSSED=YES", "CUTOVER_BOUNDARY_CROSSED=YES" not in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_unit_state_helper()
    test_migration_stop_boundary()
    test_migration_stop_boundary_uncertain()
    print(f"\nREAL_SYSTEMD_SEMANTICS_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
