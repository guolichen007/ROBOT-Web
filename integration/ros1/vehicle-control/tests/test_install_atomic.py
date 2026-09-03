#!/usr/bin/env python3
"""firebot_control install.sh 原子安装回归测试（无 ROS 依赖）。

验证：staging 复制的是「目录内容」，package.xml/CMakeLists.txt 位于 catkin 包根；
second install 后 previous 保留、APPROVED_RUNTIME.txt 记录来源 SHA。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

CONTROL_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = CONTROL_DIR / "install.sh"

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def run_install(ros_src_dir: Path):
    env = os.environ.copy()
    env["FIREBOT_ROS_SRC_DIR"] = str(ros_src_dir)
    head = subprocess.check_output(
        ["git", "-C", str(CONTROL_DIR), "rev-parse", "HEAD"], text=True
    ).strip()
    env["FIREBOT_REQUIRE_SHA"] = head
    proc = subprocess.run(["bash", str(INSTALL_SH)], env=env, capture_output=True, text=True)
    return proc, head


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "firerobot_ws"
        ros_src = ws / "src"

        (rc1, head) = run_install(ros_src)
        check("first install rc=0", rc1.returncode == 0)
        pkg = ros_src / "firebot_control"
        check("package.xml 位于 catkin 包根", (pkg / "package.xml").is_file())
        check("CMakeLists.txt 位于 catkin 包根", (pkg / "CMakeLists.txt").is_file())
        check("install.sh 未进入 catkin 包", not (pkg / "install.sh").exists())
        check(
            "APPROVED_RUNTIME.txt == HEAD",
            (pkg / "APPROVED_RUNTIME.txt").read_text().strip() == head,
        )

        (rc2, _) = run_install(ros_src)
        check("second install rc=0", rc2.returncode == 0)
        check(
            "second install 后 previous 保留 package.xml",
            (ws / ".firebot_control.previous" / "package.xml").is_file(),
        )

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
