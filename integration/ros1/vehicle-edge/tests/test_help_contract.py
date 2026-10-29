#!/usr/bin/env python3
"""lifecycle help 零副作用合同测试（B0-FIX-CODE / 规格二十八 / N）。

所有生命周期脚本 + firebotctl 必须支持 --help/-h/help，且：
  exit 0、ZERO STATE MUTATION、NO SYSTEMCTL、NO CURL、NO FILE WRITE、NO TOKEN CONSUMPTION。

运行：cd vehicle-edge && python3 tests/test_help_contract.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VEHICLE_EDGE_SRC = TESTS_DIR.parent
ROS1_SRC = VEHICLE_EDGE_SRC.parent
FIREBOTCTL = ROS1_SRC / "firebotctl"

LIFECYCLE = {
    "enroll.sh": "enroll",
    "edge-install.sh": "edge-install",
    "edge-verify.sh": "edge-verify",
    "edge-backup-legacy.sh": "edge-backup-legacy",
    "readonly-preflight.sh": "readonly-preflight",
    "readonly-start.sh": "readonly-start",
    "migration-stop-legacy.sh": "migration-stop-legacy",
    "rollback.sh": "edge-rollback",
}

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
    log = tmp / "calls.log"
    # 任何被调用的命令都记录并成功退出（证明 help 不应调用它们）
    for name in ("systemctl", "curl", "git"):
        (fakebin / name).write_text(f"#!/usr/bin/env bash\necho $0 $* >> '{log}'\nexit 0\n")
    for f in fakebin.iterdir():
        import stat
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return fakebin


def env_for(fakebin: Path, edge_home: Path) -> dict:
    return {
        "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
        "FIREBOT_TEST_SYSTEMCTL": str(fakebin / "systemctl"),
        "FIREBOT_TEST_CURL": str(fakebin / "curl"),
        "FIREBOT_EDGE_HOME": str(edge_home),
        "FIREBOT_TEST_SKIP_ROOT": "1",
        "FIREBOT_TEST_SKIP_CHOWN": "1",
    }


def run(args, env) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", *args], env=env, capture_output=True, text=True, timeout=120)


def test_lifecycle_help_zero_mutation() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="help-"))
    try:
        fakebin = make_fakebin(tmp)
        edge_home = tmp / "firebot-edge"
        edge_home.mkdir()
        env = env_for(fakebin, edge_home)
        log = tmp / "calls.log"
        for script, _ in LIFECYCLE.items():
            for flag in ("--help", "-h", "help"):
                r = run([str(VEHICLE_EDGE_SRC / "lifecycle" / script), flag], env)
                check(f"{script} {flag} exit 0", r.returncode == 0, r.stdout[-200:])
        # 零副作用：help 期间不调用 systemctl/curl/git
        calls = log.read_text() if log.exists() else ""
        check("lifecycle help 零 systemctl/curl/git", calls.strip() == "", calls[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_firebotctl_help_zero_mutation() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="help-ctl-"))
    try:
        fakebin = make_fakebin(tmp)
        edge_home = tmp / "firebot-edge"
        edge_home.mkdir()
        env = env_for(fakebin, edge_home)
        log = tmp / "calls.log"
        for flag in ("--help", "-h", "help"):
            r = run([str(FIREBOTCTL), flag], env)
            check(f"firebotctl {flag} exit 0", r.returncode == 0, r.stdout[-200:])
        for sub in ("enroll", "edge-install", "edge-verify", "edge-rollback",
                    "readonly-preflight", "readonly-start", "migration-stop-legacy", "edge-init"):
            r = run([str(FIREBOTCTL), "vehicle", sub, "--help"], env)
            check(f"firebotctl vehicle {sub} --help exit 0", r.returncode == 0, r.stdout[-200:])
        calls = log.read_text() if log.exists() else ""
        check("firebotctl help 零 systemctl/curl/git", calls.strip() == "", calls[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_lifecycle_help_zero_mutation()
    test_firebotctl_help_zero_mutation()
    print(f"\nHELP_CONTRACT_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
