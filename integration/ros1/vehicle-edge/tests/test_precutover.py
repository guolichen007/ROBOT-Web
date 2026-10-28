#!/usr/bin/env python3
"""Pre-cutover 语义行为测试（VEHICLE_EDGE_PRE_CUTOVER_SEMANTICS_FIX）。

复用 test_edge_home.Fixture（fake systemctl / fake repo / Edge Home fixture）。

覆盖（P0-4）：
  BOOTSTRAP_LEGACY_ACTIVE=PASS / BOOTSTRAP_LEGACY_INACTIVE=PASS
  BOOTSTRAP_EDGE_ACTIVE=REJECTED / BOOTSTRAP_LEGACY_ACTIVE_EDGE_ACTIVE=REJECTED
  ENROLLED_LEGACY_ACTIVE=REJECTED / ENROLLED_EDGE_ACTIVE=REJECTED / ENROLLED_BOTH_INACTIVE=PASS
  preflight 纯只读（fake systemctl 日志无 stop/restart/disable）
覆盖（P1）：
  EDGE_INIT_PERMISSION_IDEMPOTENCY（EXISTS 路径也收口 ownership/mode）
  LEGACY_BACKUP_TOOL（备份成功 + 零运行影响 + 无 secret 输出）

运行：cd vehicle-edge && python3 tests/test_precutover.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SRC = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(SRC))

import test_edge_home  # noqa: E402
from test_edge_home import Fixture, check, git  # noqa: E402

PREFLIGHT = SRC / "lifecycle" / "readonly-preflight.sh"
BACKUP = SRC / "lifecycle" / "edge-backup-legacy.sh"


def setup_preflight_env(fx: Fixture, legacy_active: bool, edge_active: bool, enrolled: bool = False) -> None:
    """preflight 所需 fixture（bootstrap/enrolled 共用）。"""
    fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
    (fx.edge_home / "config").mkdir(exist_ok=True)
    (fx.edge_home / "certs").mkdir(exist_ok=True)
    (fx.edge_home / "secrets").mkdir(exist_ok=True)
    (fx.edge_home / "runtime").mkdir(exist_ok=True)
    (fx.edge_home / "config" / "bootstrap.env").write_text(
        "FIREBOT_SERVER_URL=https://firebot.youdaozd.com\n")
    (fx.edge_home / "certs" / "mqtt-ca.crt").write_text("fixture\n")
    (fx.state / "bridge_active").write_text("active" if legacy_active else "inactive")
    (fx.state / "edge_active").write_text("active" if edge_active else "inactive")
    if enrolled:
        (fx.edge_home / "config" / "identity.env").write_text(
            "DEVICE_ID=firebot-vehicle-02\nPROFILE_ID=firebot_ros1_standard_v1\n")
        ca = fx.edge_home / "certs" / "mqtt-ca.crt"
        (fx.edge_home / "config" / "bridge.env").write_text(
            "FIREBOT_VEHICLE_ID=firebot-vehicle-02\nFIREBOT_MQTT_HOST=firebot.youdaozd.com\n"
            f"FIREBOT_MQTT_PORT=8883\nFIREBOT_CA_CERT={ca}\nFIREBOT_SUPPORTED_COMMANDS=\n")
        (fx.edge_home / "secrets" / "mqtt.env").write_text("FIREBOT_MQTT_PASSWORD=x\n")
        (fx.edge_home / "secrets" / "mqtt.env").chmod(0o600)
        (fx.edge_home / "config" / "assignment.env").write_text("SUPPORTED_COMMANDS=\n")


def run_preflight(fx: Fixture, phase: str) -> object:
    return fx.run(
        PREFLIGHT, "--phase", phase, "--device-id", "firebot-vehicle-02",
        "--expected-sha", fx.sha, "--repo-dir", str(fx.repo),
    )


def test_bootstrap_legacy_active_allowed() -> None:
    fx = Fixture()
    try:
        setup_preflight_env(fx, legacy_active=True, edge_active=False)
        r = run_preflight(fx, "bootstrap")
        check("BOOTSTRAP_LEGACY_ACTIVE=PASS", r.returncode == 0
              and "READONLY_BOOTSTRAP_PREFLIGHT=PASS" in r.stdout, r.stdout[-300:])
        check("输出 LEGACY_BRIDGE_STATE=ACTIVE", "LEGACY_BRIDGE_STATE=ACTIVE" in r.stdout)
        check("PRE_CUTOVER_LEGACY_LINK_PRESERVED=YES",
              "PRE_CUTOVER_LEGACY_LINK_PRESERVED=YES" in r.stdout)
        check("COMMUNICATION_OWNER=LEGACY", "COMMUNICATION_OWNER=LEGACY" in r.stdout)
        log = fx.syslog.read_text()
        check("bootstrap legacy active 纯只读（无 stop/restart/disable）",
              not any(t in log for t in ("stop ", "restart", "disable --now", "disable firebot")))
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_bootstrap_legacy_inactive_allowed() -> None:
    fx = Fixture()
    try:
        setup_preflight_env(fx, legacy_active=False, edge_active=False)
        r = run_preflight(fx, "bootstrap")
        check("BOOTSTRAP_LEGACY_INACTIVE=PASS", r.returncode == 0
              and "READONLY_BOOTSTRAP_PREFLIGHT=PASS" in r.stdout, r.stdout[-300:])
        check("LEGACY_BRIDGE_STATE=INACTIVE", "LEGACY_BRIDGE_STATE=INACTIVE" in r.stdout)
        check("COMMUNICATION_OWNER=NONE", "COMMUNICATION_OWNER=NONE" in r.stdout)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_bootstrap_edge_active_rejected() -> None:
    fx = Fixture()
    try:
        setup_preflight_env(fx, legacy_active=False, edge_active=True)
        r = run_preflight(fx, "bootstrap")
        check("BOOTSTRAP_EDGE_ACTIVE=REJECTED", r.returncode != 0
              and "EDGE_STARTED_BEFORE_CUTOVER" in r.stdout
              and "READONLY_BOOTSTRAP_PREFLIGHT=FAIL" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_bootstrap_legacy_and_edge_active_rejected() -> None:
    fx = Fixture()
    try:
        setup_preflight_env(fx, legacy_active=True, edge_active=True)
        r = run_preflight(fx, "bootstrap")
        check("BOOTSTRAP_LEGACY_ACTIVE_EDGE_ACTIVE=REJECTED", r.returncode != 0
              and "EDGE_STARTED_BEFORE_CUTOVER" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_enrolled_owner_semantics() -> None:
    fx = Fixture()
    try:
        setup_preflight_env(fx, legacy_active=True, edge_active=False, enrolled=True)
        r = run_preflight(fx, "enrolled")
        check("ENROLLED_LEGACY_ACTIVE=REJECTED", r.returncode != 0
              and "READONLY_ENROLLED_PREFLIGHT=FAIL" in r.stdout
              and "COMMUNICATION_OWNER=CONFLICT" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)

    fx = Fixture()
    try:
        setup_preflight_env(fx, legacy_active=False, edge_active=True, enrolled=True)
        r = run_preflight(fx, "enrolled")
        check("ENROLLED_EDGE_ACTIVE=REJECTED", r.returncode != 0
              and "READONLY_ENROLLED_PREFLIGHT=FAIL" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)

    fx = Fixture()
    try:
        setup_preflight_env(fx, legacy_active=False, edge_active=False, enrolled=True)
        os.environ["FAKE_RELEASE_SHA"] = fx.sha
        (fx.edge_home / "releases" / fx.sha).mkdir(parents=True, exist_ok=True)
        (fx.edge_home / "releases" / fx.sha / "manifest.json").write_text(
            json.dumps({"source_sha": fx.sha}))
        cur = fx.edge_home / "current"
        if cur.exists() or cur.is_symlink():
            cur.unlink()
        cur.symlink_to(f"releases/{fx.sha}")
        r = run_preflight(fx, "enrolled")
        check("ENROLLED_BOTH_INACTIVE=PASS", r.returncode == 0
              and "READONLY_ENROLLED_PREFLIGHT=PASS" in r.stdout
              and "COMMUNICATION_OWNER=NONE" in r.stdout, r.stdout[-300:])
        del os.environ["FAKE_RELEASE_SHA"]
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_edge_init_permission_idempotency() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        # 破坏 mode（幂等路径应收口）
        (fx.edge_home / "secrets").chmod(0o755)
        r2 = fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        check("EDGE_INIT_PERMISSION_IDEMPOTENCY（EXISTS 仍收口 mode）", r2.returncode == 0
              and "EDGE_INIT_IDEMPOTENT=PASS" in r2.stdout, r2.stdout[-200:])
        import stat
        check("secrets mode 收口回 700",
              oct(stat.S_IMODE((fx.edge_home / "secrets").stat().st_mode)) == "0o700")
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_legacy_backup_tool() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        # legacy fixture（测试注入路径）
        legacy_etc = fx.tmp / "legacy-etc"
        legacy_bridge = fx.tmp / "legacy-bridge"
        legacy_etc.mkdir()
        legacy_bridge.mkdir()
        (legacy_etc / "bridge.env").write_text(
            "FIREBOT_MQTT_HOST=100.110.31.112\nFIREBOT_MQTT_PORT=8883\n")
        (legacy_etc / "release.env").write_text("FINAL_SHA=" + "1" * 40 + "\n")
        (legacy_bridge / "APPROVED_RUNTIME.txt").write_text("1" * 40 + "\n")
        (fx.systemd_dir / "firebot-bridge.service").write_text("[Unit]\n")
        (fx.state / "bridge_active").write_text("active")
        env = fx.env()
        env["FIREBOT_TEST_LEGACY_ETC"] = str(legacy_etc)
        env["FIREBOT_TEST_LEGACY_BRIDGE"] = str(legacy_bridge)
        env["FIREBOT_TEST_SYSTEMD_DIR"] = str(fx.systemd_dir)
        import subprocess
        r = subprocess.run(["bash", str(BACKUP)], env=env, capture_output=True, text=True, timeout=120)
        check("LEGACY_BACKUP_TOOL=PASS", r.returncode == 0 and "LEGACY_BACKUP=PASS" in r.stdout,
              r.stdout[-300:])
        check("BACKUP_ROOT 输出（canonical Edge Home）", "BACKUP_ROOT=" in r.stdout
              and f"{fx.edge_home}/backups/legacy/" in r.stdout)
        check("LEGACY_BACKUP_ZERO_RUNTIME_IMPACT（状态不变断言）",
              "LEGACY_SERVICE_STATE_UNCHANGED=PASS" in r.stdout and "ZERO_RUNTIME_IMPACT=PASS" in r.stdout)
        log = fx.syslog.read_text()
        check("backup 不 stop/restart/reload/disable 服务",
              not any(t in log for t in ("stop ", "restart", "reload", "disable --now")))
        check("backup 输出无 secret（password/token/secret/key）",
              not any(t in r.stdout for t in ("password", "token", "secret", "PRIVATE")))
        # 备份内容存在
        backup_root = [p for p in (fx.edge_home / "backups" / "legacy").iterdir()][0]
        check("备份含 etc-firebot/bridge.env",
              (backup_root / "etc-firebot" / "bridge.env").exists())
        check("备份含 legacy-bridge APPROVED_RUNTIME",
              (backup_root / "opt-firebot-vehicle-bridge" / "APPROVED_RUNTIME.txt").exists())
        check("备份含 systemd unit", (backup_root / "firebot-bridge.service").exists())
        check("备份含 legacy-release-info", (backup_root / "legacy-release-info.txt").exists())
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


if __name__ == "__main__":
    test_bootstrap_legacy_active_allowed()
    test_bootstrap_legacy_inactive_allowed()
    test_bootstrap_edge_active_rejected()
    test_bootstrap_legacy_and_edge_active_rejected()
    test_enrolled_owner_semantics()
    test_edge_init_permission_idempotency()
    test_legacy_backup_tool()
    print(f"\nPRECUTOVER_TEST total={test_edge_home.PASS + test_edge_home.FAIL} "
          f"pass={test_edge_home.PASS} fail={test_edge_home.FAIL}")
    if test_edge_home.FAIL:
        sys.exit(1)
