#!/usr/bin/env python3
"""管理平面 tools release 行为测试（B0-FIX-CODE / 规格八~十三、十八、二十二）。

覆盖：
  A. TOOLS_BUILD=PASS
  B. 同 SHA 重试 → EXISTS_VERIFIED（不覆盖）
  C. 同 SHA artifact 被篡改 → RELEASE_CORRUPT_OR_MISMATCH
  D. Tools atomic switch（current/previous 正确）
  E. Tools rollback（只切管理平面，不碰 data-plane current/previous）
  F. firebotctl 生产模式布局发现（VEHICLE_TOOLS_RELEASE）+ version/tooling-status
  I. 生产 tools release 不含 Git repo / 车辆特定目录 / Stage 证据路径

运行：cd vehicle-edge && python3 tests/test_tools_release.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VEHICLE_EDGE_SRC = TESTS_DIR.parent
ROS1_SRC = VEHICLE_EDGE_SRC.parent  # integration/ros1

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


def readlink(p: Path) -> Path:
    """Path.readlink 兼容 shim（Python 3.8 无 Path.readlink）。"""
    return Path(os.readlink(str(p)))


def run(args, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", *args], capture_output=True, text=True, timeout=120, **env)


def make_source_dir(tmp: Path) -> Path:
    """构造交付源：firebotctl + vehicle-edge（不含 .git / tests / __pycache__）。"""
    src = tmp / "src"
    src.mkdir()
    shutil.copy2(ROS1_SRC / "firebotctl", src / "firebotctl")
    shutil.copytree(VEHICLE_EDGE_SRC, src / "vehicle-edge",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "docs"))
    return src


class Fixture:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tools-rel-"))
        self.edge_home = self.tmp / "firebot-edge"
        self.edge_home.mkdir()
        (self.edge_home / "tools" / "releases").mkdir(parents=True)
        self.src = make_source_dir(self.tmp)
        self.sha = "a" * 40

    def manager(self, *args: str) -> subprocess.CompletedProcess:
        return run([str(VEHICLE_EDGE_SRC / "install" / "tools-release-manager.sh"), *args],
                   env={**os.environ, "FIREBOT_EDGE_HOME": str(self.edge_home)})

    def sha2(self) -> str:
        return "b" * 40


def test_tools_build_and_verify() -> None:
    fx = Fixture()
    try:
        r = fx.manager("build", str(fx.src), fx.sha, str(fx.edge_home))
        check("TOOLS_BUILD=PASS", r.returncode == 0 and "TOOLS_RELEASE_BUILD=PASS" in r.stdout, r.stdout[-300:])
        rel = fx.edge_home / "tools" / "releases" / fx.sha
        check("release 含 firebotctl", (rel / "firebotctl").exists())
        check("release 含 manifest.json", (rel / "manifest.json").exists())
        check("release 含 checksums.sha256", (rel / "checksums.sha256").exists())
        check("release 含 vehicle-edge/lifecycle", (rel / "vehicle-edge" / "lifecycle" / "edge-install.sh").exists())
        manifest = json.loads((rel / "manifest.json").read_text())
        check("manifest product=firebot-edge-tools", manifest["product"] == "firebot-edge-tools")
        check("manifest source_sha", manifest["source_sha"] == fx.sha)
        # checksums 校验
        v = fx.manager("verify", fx.sha, str(fx.edge_home))
        check("TOOLS_RELEASE_VERIFY=PASS", v.returncode == 0 and "TOOLS_RELEASE_VERIFY=PASS" in v.stdout)
        # I. 生产 release 不含 .git / 车辆特定目录 / stage 证据
        check("release 不含 .git", not (rel / ".git").exists())
        check("release 不含 tests", not (rel / "vehicle-edge" / "tests").exists())
        check("release 不含 R002 目录", not any(p.name.startswith("R002") for p in rel.rglob("R00*")))
        blob = subprocess.run(["grep", "-rl", "r002-stage-a", str(rel)],
                              capture_output=True, text=True).stdout
        check("release 不含 /home/tl/r002-stage-a 运行依赖", blob == "", blob[:200])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_tools_build_exists_verified_and_corrupt() -> None:
    fx = Fixture()
    try:
        fx.manager("build", str(fx.src), fx.sha, str(fx.edge_home))
        # B. 同 SHA 重试
        r2 = fx.manager("build", str(fx.src), fx.sha, str(fx.edge_home))
        check("TOOLS_BUILD_EXISTS_VERIFIED", r2.returncode == 0
              and "TOOLS_RELEASE_BUILD=EXISTS_VERIFIED" in r2.stdout, r2.stdout[-300:])
        # C. 篡改 artifact → RELEASE_CORRUPT_OR_MISMATCH
        rel = fx.edge_home / "tools" / "releases" / fx.sha
        (rel / "firebotctl").write_text("#!/usr/bin/env bash\necho tampered\n")
        r3 = fx.manager("build", str(fx.src), fx.sha, str(fx.edge_home))
        check("TOOLS_RELEASE_CORRUPT_OR_MISMATCH", r3.returncode != 0
              and "RELEASE_CORRUPT_OR_MISMATCH" in r3.stdout, r3.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_tools_atomic_switch_and_rollback() -> None:
    fx = Fixture()
    try:
        fx.manager("build", str(fx.src), fx.sha, str(fx.edge_home))
        s1 = fx.manager("switch", fx.sha, str(fx.edge_home))
        check("TOOLS_SWITCH 第一次 previous=none", s1.returncode == 0
              and "TOOLS_PREVIOUS_SHA=none" in s1.stdout, s1.stdout[-300:])
        check("tools/current → releases/<sha>",
              readlink(fx.edge_home / "tools" / "current") == Path("releases") / fx.sha)

        # 第二个 SHA
        sha2 = "b" * 40
        fx.manager("build", str(fx.src), sha2, str(fx.edge_home))
        s2 = fx.manager("switch", sha2, str(fx.edge_home))
        check("TOOLS_SWITCH 第二次 previous 轮转", s2.returncode == 0
              and f"TOOLS_PREVIOUS_SHA={fx.sha}" in s2.stdout, s2.stdout[-300:])
        check("tools/current → sha2", readlink(fx.edge_home / "tools" / "current") == Path("releases") / sha2)
        check("tools/previous → sha1", readlink(fx.edge_home / "tools" / "previous") == Path("releases") / fx.sha)

        # 建立 data-plane current（管理平面 rollback 不得触碰）
        (fx.edge_home / "releases" / fx.sha).mkdir(parents=True)
        dp_cur = fx.edge_home / "current"
        dp_cur.symlink_to(Path("releases") / fx.sha)

        # E. rollback 只切管理平面
        rb = fx.manager("rollback", str(fx.edge_home))
        check("TOOLS_RELEASE_ROLLBACK=PASS", rb.returncode == 0
              and "TOOLS_RELEASE_ROLLBACK=PASS" in rb.stdout, rb.stdout[-300:])
        check("MANAGEMENT_PLANE_ROLLBACK=PASS", "MANAGEMENT_PLANE_ROLLBACK=PASS" in rb.stdout)
        check("DATA_PLANE_UNCHANGED=YES", "DATA_PLANE_UNCHANGED=YES" in rb.stdout)
        check("tools/current 回滚到 sha1", readlink(fx.edge_home / "tools" / "current") == Path("releases") / fx.sha)
        check("tools/previous 轮转到 sha2", readlink(fx.edge_home / "tools" / "previous") == Path("releases") / sha2)
        check("data-plane current 未变", readlink(dp_cur) == Path("releases") / fx.sha)

        # status
        st = fx.manager("status", str(fx.edge_home))
        check("TOOLS_STATUS current=sha1", "TOOLS_CURRENT_SHA=" + fx.sha in st.stdout)
        check("TOOLS_STATUS previous=sha2", "TOOLS_PREVIOUS_SHA=" + sha2 in st.stdout)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_firebotctl_production_layout() -> None:
    fx = Fixture()
    try:
        fx.manager("build", str(fx.src), fx.sha, str(fx.edge_home))
        fx.manager("switch", fx.sha, str(fx.edge_home))
        # F. 生产 firebotctl：tools/releases/<SHA>/firebotctl 有 manifest.json 兄弟 → VEHICLE_TOOLS_RELEASE
        prod_ctl = fx.edge_home / "tools" / "releases" / fx.sha / "firebotctl"
        env = {**os.environ, "FIREBOT_EDGE_HOME": str(fx.edge_home)}
        v = run([str(prod_ctl), "--version"], env=env)
        check("firebotctl --version 布局=VEHICLE_TOOLS_RELEASE", v.returncode == 0
              and "FIREBOTCTL_LAYOUT=VEHICLE_TOOLS_RELEASE" in v.stdout, v.stdout[-300:])
        check("version 输出 SOURCE_SHA", f"FIREBOTCTL_SOURCE_SHA={fx.sha}" in v.stdout)
        check("version 输出 PRODUCT=firebot-edge-tools", "FIREBOTCTL_PRODUCT=firebot-edge-tools" in v.stdout)

        ts = run([str(prod_ctl), "vehicle", "tooling-status"], env=env)
        check("tooling-status GIT_RUNTIME_DEPENDENCY=NO", ts.returncode == 0
              and "GIT_RUNTIME_DEPENDENCY=NO" in ts.stdout, ts.stdout[-300:])
        check("tooling-status TOOLS_CURRENT_SHA", f"TOOLS_CURRENT_SHA={fx.sha}" in ts.stdout)

        # 生产模式拒绝 server / fleet / vehicle rollback
        sv = run([str(prod_ctl), "server", "verify"], env=env)
        check("生产 server 拒绝 COMMAND_SCOPE_REJECTED", sv.returncode != 0
              and "COMMAND_SCOPE_REJECTED" in sv.stdout and "AVAILABLE_SCOPE=vehicle" in sv.stdout)
        rb = run([str(prod_ctl), "vehicle", "rollback"], env=env)
        check("生产 vehicle rollback 拒绝 COMMAND_SCOPE_REJECTED", rb.returncode != 0
              and "COMMAND_SCOPE_REJECTED" in rb.stdout
              and "FIREBOTCTL_LAYOUT=VEHICLE_TOOLS_RELEASE" in rb.stdout, rb.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_tools_switch_same_sha_preserves_previous() -> None:
    """B0-FIX2 / 规格：tools 同 SHA 幂等重试必须 ALREADY_CURRENT 且不破坏 previous 回滚指针。"""
    fx = Fixture()
    try:
        fx.manager("build", str(fx.src), fx.sha, str(fx.edge_home))
        fx.manager("switch", fx.sha, str(fx.edge_home))
        sha2 = "b" * 40
        fx.manager("build", str(fx.src), sha2, str(fx.edge_home))
        fx.manager("switch", sha2, str(fx.edge_home))
        # current=sha2 previous=sha1；再次 switch sha2
        r = fx.manager("switch", sha2, str(fx.edge_home))
        check("同 SHA 重试 → ALREADY_CURRENT", r.returncode == 0
              and "TOOLS_RELEASE_SWITCH=ALREADY_CURRENT" in r.stdout, r.stdout[-300:])
        check("TOOLS_CURRENT_SHA 仍 sha2", f"TOOLS_CURRENT_SHA={sha2}" in r.stdout)
        check("TOOLS_PREVIOUS_SHA 仍 sha1（不破坏回滚指针）", f"TOOLS_PREVIOUS_SHA={fx.sha}" in r.stdout, r.stdout[-300:])
        check("previous 仍指向 sha1", readlink(fx.edge_home / "tools" / "previous") == Path("releases") / fx.sha)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


if __name__ == "__main__":
    test_tools_build_and_verify()
    test_tools_build_exists_verified_and_corrupt()
    test_tools_atomic_switch_and_rollback()
    test_tools_switch_same_sha_preserves_previous()
    test_firebotctl_production_layout()
    print(f"\nTOOLS_RELEASE_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
