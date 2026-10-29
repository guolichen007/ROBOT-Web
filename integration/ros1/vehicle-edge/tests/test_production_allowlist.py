#!/usr/bin/env python3
"""生产 vehicle allowlist / denylist 零副作用测试（B0-FIX2）。

每条禁用命令必须在任何 git/curl/systemctl mutation/rostopic/文件写入之前被拒绝，
输出 COMMAND_SCOPE_REJECTED + FIREBOTCTL_LAYOUT=VEHICLE_TOOLS_RELEASE。

运行：cd vehicle-edge && python3 tests/test_production_allowlist.py
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


# B0-FIX2 denylist：生产 tools 模式拒绝的 vehicle 子命令（含参数形态）。
DENIED = [
    ["install", "--sha", "f" * 40],
    ["verify"],
    ["status"],
    ["start", "base"],
    ["start", "navigation"],
    ["start", "control"],
    ["stop", "control"],
    ["capability", "stop-only"],
    ["capability", "patrol"],
    ["location", "enable"],
    ["profile-sync"],
    ["media-secret", "check"],
    ["support-bundle"],
    ["rollback"],
    ["upgrade", "--sha", "f" * 40],
]


def test_production_denylist_zero_side_effect() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="prod-allowlist-"))
    try:
        edge_home = tmp / "firebot-edge"
        (edge_home / "tools" / "releases").mkdir(parents=True)
        src = make_source_dir(tmp)
        sha = "a" * 40
        subprocess.run(["bash", str(TOOLS_MANAGER), "build", str(src), sha, str(edge_home)],
                       check=True, capture_output=True, text=True)
        subprocess.run(["bash", str(TOOLS_MANAGER), "switch", sha, str(edge_home)],
                       check=True, capture_output=True, text=True)
        prod_ctl = edge_home / "tools" / "releases" / sha / "firebotctl"

        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        log = tmp / "calls.log"
        etc_root = tmp / "etc-firebot"
        etc_root.mkdir()
        for name in ("git", "curl", "rostopic"):
            (fakebin / name).write_text(f"#!/usr/bin/env bash\necho $0 $* >> '{log}'\nexit 0\n")
        (fakebin / "systemctl").write_text(textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            echo systemctl $* >> '{log}'
            case "$*" in
              is-active*) echo inactive ;;
              is-enabled*) echo disabled ;;
              *) exit 0 ;;
            esac
            exit 0
            """
        ))
        for f in fakebin.iterdir():
            import stat
            f.chmod(f.stat().st_mode | stat.S_IXUSR)

        env = {
            **os.environ,
            "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_EDGE_HOME": str(edge_home),
            "FIREBOT_TEST_SYSTEMCTL": str(fakebin / "systemctl"),
            "FIREBOT_TEST_CURL": str(fakebin / "curl"),
            "FIREBOT_TEST_ETC_ROOT": str(etc_root),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_SKIP_CHOWN": "1",
        }

        for args in DENIED:
            label = "vehicle " + " ".join(args)
            r = subprocess.run(["bash", str(prod_ctl), "vehicle", *args], env=env,
                               capture_output=True, text=True, timeout=120)
            check(f"{label} → COMMAND_SCOPE_REJECTED",
                  r.returncode != 0 and "COMMAND_SCOPE_REJECTED" in r.stdout, r.stdout[-200:])
            check(f"{label} → FIREBOTCTL_LAYOUT=VEHICLE_TOOLS_RELEASE",
                  "FIREBOTCTL_LAYOUT=VEHICLE_TOOLS_RELEASE" in r.stdout, r.stdout[-200:])

        calls = log.read_text() if log.exists() else ""
        check("GIT_CALLED=NO", "git" not in calls, calls[:200])
        check("CURL_CALLED=NO", "curl" not in calls, calls[:200])
        check("SYSTEMCTL_MUTATION=NO", "systemctl" not in calls, calls[:200])
        check("ROSTOPIC_CALLED=NO", "rostopic" not in calls, calls[:200])
        check("FILE_MUTATION=NO", not any(etc_root.iterdir()), str(list(etc_root.iterdir()))[:200])

        # 正向控制：生产允许的命令仍可用（tooling-status 只读）
        ok = subprocess.run(["bash", str(prod_ctl), "vehicle", "tooling-status"], env=env,
                            capture_output=True, text=True, timeout=120)
        check("生产允许 tooling-status 仍可用",
              ok.returncode == 0 and "GIT_RUNTIME_DEPENDENCY=NO" in ok.stdout, ok.stdout[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_production_denylist_zero_side_effect()
    print(f"\nPRODUCTION_ALLOWLIST_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
