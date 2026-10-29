#!/usr/bin/env python3
"""Git 独立性 + tools 零中断部署测试（B0-FIX-CODE / 规格三十、三十二、J、O）。

覆盖：
  G. Git independence：生产 tools release 下 firebotctl 不调用 git
  J. edge-install 生产模式（FIREBOT_EDGE_TOOLS_MODE）用 bundled source，不依赖 git
  O. tools deploy zero downtime：install-tools-release.sh 前后 legacy/edge/control/Tailscale 不变

运行：cd vehicle-edge && python3 tests/test_git_independence.py
"""
from __future__ import annotations

import os
import shutil
import stat
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


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@ci", "-c", "user.name=ci", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp: Path) -> tuple[Path, str]:
    """构造含 integration/ros1 的 git 交付源。"""
    repo = tmp / "repo"
    (repo / "integration" / "ros1").mkdir(parents=True)
    shutil.copy2(ROS1_SRC / "firebotctl", repo / "integration" / "ros1" / "firebotctl")
    shutil.copytree(VEHICLE_EDGE_SRC, repo / "integration" / "ros1" / "vehicle-edge",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    sha = git(repo, "rev-parse", "HEAD")
    return repo, sha


def make_source_no_git(tmp: Path) -> Path:
    src = tmp / "src"
    src.mkdir()
    shutil.copy2(ROS1_SRC / "firebotctl", src / "firebotctl")
    shutil.copytree(VEHICLE_EDGE_SRC, src / "vehicle-edge",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "docs"))
    return src


def test_production_no_git_dependency() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="git-indep-"))
    try:
        edge_home = tmp / "firebot-edge"
        (edge_home / "tools" / "releases").mkdir(parents=True)
        src = make_source_no_git(tmp)
        sha = "c" * 40
        subprocess.run(["bash", str(TOOLS_MANAGER), "build", str(src), sha, str(edge_home)],
                       check=True, capture_output=True, text=True)
        subprocess.run(["bash", str(TOOLS_MANAGER), "switch", sha, str(edge_home)],
                       check=True, capture_output=True, text=True)

        # 制造一个「git 不可用」的 PATH（fake git 记录并失败）
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        marker = tmp / "git-called"
        (fakebin / "git").write_text(f"#!/usr/bin/env bash\necho git > '{marker}'\nexit 1\n")
        (fakebin / "git").chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_EDGE_HOME": str(edge_home),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_SKIP_CHOWN": "1",
        }
        prod_ctl = edge_home / "tools" / "releases" / sha / "firebotctl"

        v = subprocess.run(["bash", str(prod_ctl), "--version"], env=env, capture_output=True, text=True)
        check("生产 --version 不依赖 git", v.returncode == 0
              and "FIREBOTCTL_LAYOUT=VEHICLE_TOOLS_RELEASE" in v.stdout, v.stdout[-200:])

        # edge-install 生产模式：错误 SHA → TOOLS_SOURCE_SHA_MISMATCH（不是 HEAD_SHA_MISMATCH）
        bad = "d" * 40
        r = subprocess.run(["bash", str(prod_ctl), "vehicle", "edge-install", "--sha", bad],
                           env=env, capture_output=True, text=True)
        check("edge-install 错误 SHA → TOOLS_SOURCE_SHA_MISMATCH（非 Git）",
              r.returncode != 0 and "TOOLS_SOURCE_SHA_MISMATCH" in r.stdout
              and "HEAD_SHA_MISMATCH" not in r.stdout, r.stdout[-200:])

        check("全程未调用 git", not marker.exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_tools_deploy_zero_downtime() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tools-deploy-"))
    try:
        repo, sha = make_repo(tmp)
        edge_home = tmp / "firebot-edge"
        edge_home.mkdir()
        bin_dir = tmp / "usr-local-bin"
        bin_dir.mkdir()

        state = tmp / "state"
        state.mkdir()
        (state / "bridge_active").write_text("active")
        (state / "edge_active").write_text("inactive")
        (state / "ctl_enabled").write_text("disabled")
        (state / "ctl_active").write_text("inactive")
        (state / "tailscaled_active").write_text("active")

        # B0-FIX2：legacy active 时必须有健康的 MQTT 状态（host 非空 + mqtt_connected=true）。
        status_file = tmp / "bridge-status.json"
        status_file.write_text('{"mqtt_host": "100.110.31.112", "mqtt_connected": true, "boot_id": "b1"}\n')

        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        (fakebin / "systemctl").write_text(textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$*" in
              "is-active firebot-bridge") cat "${FAKE_STATE:?}/bridge_active" ;;
              "is-active firebot-vehicle-edge") cat "$FAKE_STATE/edge_active" ;;
              "is-enabled firebot-control") cat "$FAKE_STATE/ctl_enabled" ;;
              "is-active firebot-control") cat "$FAKE_STATE/ctl_active" ;;
              "is-active tailscaled") cat "$FAKE_STATE/tailscaled_active" ;;
              *) echo "unexpected: $*" >&2; exit 1 ;;
            esac
            """
        ))
        (fakebin / "systemctl").chmod(0o755)

        installer = repo / "integration" / "ros1" / "vehicle-edge" / "install" / "install-tools-release.sh"
        env = {
            **os.environ,
            "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_EDGE_HOME": str(edge_home),
            "FIREBOT_TEST_SYSTEMCTL": str(fakebin / "systemctl"),
            "FIREBOT_TEST_BRIDGE_STATUS_FILE": str(status_file),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_SKIP_CHOWN": "1",
            "FAKE_STATE": str(state),
        }
        r = subprocess.run(["bash", str(installer), "--sha", sha,
                            "--edge-home", str(edge_home), "--bin-dir", str(bin_dir)],
                           env=env, capture_output=True, text=True, timeout=180)
        check("TOOLS_INSTALL=PASS", r.returncode == 0 and "TOOLS_INSTALL=PASS" in r.stdout, r.stdout[-400:])
        check("LEGACY_BRIDGE_STATE_UNCHANGED=YES", "LEGACY_BRIDGE_STATE_UNCHANGED=YES" in r.stdout)
        check("LEGACY_MQTT_HOST_UNCHANGED=YES", "LEGACY_MQTT_HOST_UNCHANGED=YES" in r.stdout)
        check("LEGACY_MQTT_CONNECTED_UNCHANGED=YES", "LEGACY_MQTT_CONNECTED_UNCHANGED=YES" in r.stdout)
        check("EDGE_STATE_UNCHANGED=YES", "EDGE_STATE_UNCHANGED=YES" in r.stdout)
        check("CONTROL_ENABLED_UNCHANGED=YES", "CONTROL_ENABLED_UNCHANGED=YES" in r.stdout)
        check("CONTROL_ACTIVE_UNCHANGED=YES", "CONTROL_ACTIVE_UNCHANGED=YES" in r.stdout)
        check("TAILSCALE_STATE_UNCHANGED=YES", "TAILSCALE_STATE_UNCHANGED=YES" in r.stdout)
        # /usr/local/bin/firebotctl realpath → tools/current/firebotctl
        link = bin_dir / "firebotctl"
        check("/usr/local/bin/firebotctl 存在", link.is_symlink())
        realpath = os.path.realpath(str(link))
        check("firebotctl realpath → tools/releases/<sha>/firebotctl",
              realpath == str(edge_home / "tools" / "releases" / sha / "firebotctl"))
        check("FIREBOTCTL_REALPATH 输出", f"FIREBOTCTL_REALPATH={realpath}" in r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_production_no_git_dependency()
    test_tools_deploy_zero_downtime()
    print(f"\nGIT_INDEPENDENCE_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
