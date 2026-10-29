#!/usr/bin/env python3
"""稳定 Edge Runtime 入口测试（B0-FIX2 / EDGE_STABLE_BIN）。

覆盖：
  - edge-install 创建 bin/firebot-edge -> ../current/bin/firebot-edge（symlink）
  - readlink -f 解析到 releases/<SHA>/bin/firebot-edge
  - edge-verify 报 EDGE_STABLE_BIN_PRESENT / SYMLINK / RELEASE_SHA
  - 真实执行 $EDGE_HOME/bin/firebot-edge version（不是只查 systemd 字符串）
  - 同 SHA 再装 → EDGE_STABLE_BIN=ALREADY_PRESENT（幂等）
  - bin/firebot-edge 已是普通文件 → EDGE_BIN_CONFLICT 拒绝

运行：cd vehicle-edge && python3 tests/test_stable_edge_bin.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from test_edge_home import Fixture

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


def test_stable_edge_bin_created() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        fx.write_public_bridge_env()
        r = fx.run(fx.lifecycle("edge-install.sh"), str(fx.repo), FIREBOT_REQUIRE_SHA=fx.sha)
        check("EDGE_INSTALL=PASS", r.returncode == 0 and "EDGE_INSTALL=PASS" in r.stdout, r.stdout[-300:])

        stable = fx.edge_home / "bin" / "firebot-edge"
        check("EDGE_STABLE_BIN 存在且是 symlink", stable.is_symlink(), str(stable))
        check("EDGE_STABLE_BIN 指向 ../current/bin/firebot-edge",
              os.readlink(str(stable)) == "../current/bin/firebot-edge", os.readlink(str(stable)))
        realpath = os.path.realpath(str(stable))
        check("readlink -f → releases/<SHA>/bin/firebot-edge",
              realpath == str(fx.edge_home / "releases" / fx.sha / "bin" / "firebot-edge"), realpath)

        # 真实执行 fixture 的 $EDGE_HOME/bin/firebot-edge version（不是只查 systemd 字符串）
        v = subprocess.run([str(stable), "version"], env=fx.env(),
                           capture_output=True, text=True, timeout=120)
        check("真实执行 bin/firebot-edge version", v.returncode == 0, (v.stdout + v.stderr)[-300:])

        # edge-verify 报 stable bin 三项 + systemd 目标有效
        ve = fx.run(fx.lifecycle("edge-verify.sh"), "--edge-home", str(fx.edge_home),
                    FIREBOT_REQUIRE_SHA=fx.sha, FIREBOT_TEST_SKIP_OWNERSHIP="1")
        check("edge-verify EDGE_STABLE_BIN_PRESENT=YES", "EDGE_STABLE_BIN_PRESENT=YES" in ve.stdout, ve.stdout[-300:])
        check("edge-verify EDGE_STABLE_BIN_SYMLINK=YES", "EDGE_STABLE_BIN_SYMLINK=YES" in ve.stdout)
        check("edge-verify EDGE_STABLE_BIN_RELEASE_SHA",
              f"EDGE_STABLE_BIN_RELEASE_SHA={fx.sha}" in ve.stdout, ve.stdout[-300:])
        check("edge-verify SYSTEMD_EXEC_TARGET_VALID=YES", "SYSTEMD_EXEC_TARGET_VALID=YES" in ve.stdout)

        # 同 SHA 再装 → 幂等 PASS
        r2 = fx.run(fx.lifecycle("edge-install.sh"), str(fx.repo), FIREBOT_REQUIRE_SHA=fx.sha)
        check("同 SHA 再装 → EDGE_STABLE_BIN=ALREADY_PRESENT",
              r2.returncode == 0 and "EDGE_STABLE_BIN=ALREADY_PRESENT" in r2.stdout, r2.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_stable_edge_bin_conflict_rejected() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        fx.write_public_bridge_env()
        # 预置 bin/firebot-edge 为普通文件 → EDGE_BIN_CONFLICT，禁止静默覆盖
        (fx.edge_home / "bin").mkdir(parents=True, exist_ok=True)
        (fx.edge_home / "bin" / "firebot-edge").write_text("#!/usr/bin/env bash\necho evil\n")
        r = fx.run(fx.lifecycle("edge-install.sh"), str(fx.repo), FIREBOT_REQUIRE_SHA=fx.sha)
        check("EDGE_BIN_CONFLICT 拒绝静默覆盖",
              r.returncode != 0 and "EDGE_BIN_CONFLICT" in r.stdout, r.stdout[-300:])
        check("普通文件未被覆盖", not (fx.edge_home / "bin" / "firebot-edge").is_symlink())
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


if __name__ == "__main__":
    test_stable_edge_bin_created()
    test_stable_edge_bin_conflict_rejected()
    print(f"\nSTABLE_EDGE_BIN_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
