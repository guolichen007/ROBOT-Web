#!/usr/bin/env python3
"""生产默认环境测试（B0-FIX2 / FIREBOT_EDGE_HOME 完全未设置时 firebotctl 正常运行）。

覆盖：
  - env 无 FIREBOT_EDGE_HOME 时 firebotctl --version / vehicle tooling-status 无 unbound variable
  - Edge Home 推导：tools/releases/<SHA>/firebotctl → EDGE_HOME = <edge-home>（不是 <edge-home>/tools）
  - 经 /usr/local/bin/firebotctl 等价 symlink 调用

运行：cd vehicle-edge && python3 tests/test_production_default_env.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VEHICLE_EDGE_SRC = TESTS_DIR.parent
ROS1_SRC = VEHICLE_EDGE_SRC.parent
TOOLS_MANAGER = VEHICLE_EDGE_SRC / "install" / "tools-release-manager.sh"

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


def make_source_dir(tmp: Path) -> Path:
    src = tmp / "src"
    src.mkdir()
    shutil.copy2(ROS1_SRC / "firebotctl", src / "firebotctl")
    shutil.copytree(VEHICLE_EDGE_SRC, src / "vehicle-edge",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "docs"))
    return src


def test_production_default_env() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="prod-default-env-"))
    try:
        edge_home = tmp / "firebot-edge"
        (edge_home / "tools" / "releases").mkdir(parents=True)
        src = make_source_dir(tmp)
        sha = "e" * 40
        subprocess.run(["bash", str(TOOLS_MANAGER), "build", str(src), sha, str(edge_home)],
                       check=True, capture_output=True, text=True)
        subprocess.run(["bash", str(TOOLS_MANAGER), "switch", sha, str(edge_home)],
                       check=True, capture_output=True, text=True)

        # /usr/local/bin/firebotctl 等价 symlink
        bin_dir = tmp / "usr-local-bin"
        bin_dir.mkdir()
        link = bin_dir / "firebotctl"
        link.symlink_to(edge_home / "tools" / "current" / "firebotctl")

        # 完全未设置 FIREBOT_EDGE_HOME（真实默认 shell）
        base_env = {k: v for k, v in os.environ.items() if k != "FIREBOT_EDGE_HOME"}
        base_env["FIREBOT_TEST_SKIP_ROOT"] = "1"
        base_env["FIREBOT_TEST_SKIP_CHOWN"] = "1"

        v = subprocess.run(["bash", str(link), "--version"], env=base_env,
                           capture_output=True, text=True, timeout=120)
        check("NO_UNBOUND_VARIABLE=YES（--version 无 unbound variable）",
              v.returncode == 0 and "unbound variable" not in v.stderr,
              (v.stdout + v.stderr)[-400:])
        check("EDGE_HOME_DERIVATION=PASS（--version 生产布局）",
              "FIREBOTCTL_LAYOUT=VEHICLE_TOOLS_RELEASE" in v.stdout, v.stdout[-300:])
        check("PRODUCTION_LAYOUT=PASS（--version source_sha）",
              f"FIREBOTCTL_SOURCE_SHA={sha}" in v.stdout, v.stdout[-300:])

        ts = subprocess.run(["bash", str(link), "vehicle", "tooling-status"], env=base_env,
                            capture_output=True, text=True, timeout=120)
        check("NO_UNBOUND_VARIABLE=YES（tooling-status 无 unbound variable）",
              ts.returncode == 0 and "unbound variable" not in ts.stderr,
              (ts.stdout + ts.stderr)[-400:])
        check("tooling-status GIT_RUNTIME_DEPENDENCY=NO",
              "GIT_RUNTIME_DEPENDENCY=NO" in ts.stdout, ts.stdout[-300:])
        check("tooling-status TOOLS_CURRENT_SHA 推导正确",
              f"TOOLS_CURRENT_SHA={sha}" in ts.stdout, ts.stdout[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_production_default_env()
    print(f"\nPRODUCTION_DEFAULT_ENV_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
