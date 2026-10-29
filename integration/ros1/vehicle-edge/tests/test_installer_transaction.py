#!/usr/bin/env python3
"""tools installer 事务回滚 + MQTT 零中断测试（B0-FIX2 / 规格七、八、十一）。

覆盖：
  CASE A 新 CLI 自检失败 → OLD_FIREBOTCTL_LINK 保持 / tools/current 保持 / services 不变
  CASE B tools/current 切换后 tooling-status 失败 → current/previous 自动恢复、旧 CLI 保持
  CASE C /usr/local/bin 切换后 postcheck 失败 → 旧 CLI 链接恢复、tools pointers 恢复
  CASE D legacy mqtt_connected true→false 漂移 → TOOLS_INSTALL=FAIL，不 restart/stop legacy、不 start edge
  正常：MQTT 零中断（legacy active + mqtt_connected=true 前后一致）

运行：cd vehicle-edge && python3 tests/test_installer_transaction.py
"""
from __future__ import annotations

import json
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
    return Path(os.readlink(str(p)))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@ci", "-c", "user.name=ci", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    (repo / "integration" / "ros1").mkdir(parents=True)
    shutil.copy2(ROS1_SRC / "firebotctl", repo / "integration" / "ros1" / "firebotctl")
    shutil.copytree(VEHICLE_EDGE_SRC, repo / "integration" / "ros1" / "vehicle-edge",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    return repo


class InstallerFixture:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.repo = make_repo(tmp)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.edge_home = tmp / "firebot-edge"
        self.edge_home.mkdir()
        self.bin_dir = tmp / "usr-local-bin"
        self.bin_dir.mkdir()
        self.state = tmp / "state"
        self.state.mkdir()
        (self.state / "bridge_active").write_text("active")
        (self.state / "edge_active").write_text("inactive")
        (self.state / "ctl_enabled").write_text("disabled")
        (self.state / "ctl_active").write_text("inactive")
        (self.state / "tailscaled_active").write_text("active")
        self.status_file = tmp / "bridge-status.json"
        self._write_status(connected=True)
        self.calls = tmp / "systemctl-mutations.log"
        self._write_fake_systemctl()

    def _write_status(self, connected: bool) -> None:
        self.status_file.write_text(json.dumps(
            {"mqtt_host": "100.110.31.112", "mqtt_connected": connected, "boot_id": "b1"}))

    def _write_fake_systemctl(self) -> None:
        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self.fake_systemctl = self.fakebin / "systemctl"
        self.fake_systemctl.write_text(textwrap.dedent(
            """\
            #!/usr/bin/env bash
            args="$*"
            emit_active() {
              printf '%s\\n' "$1"
              case "$1" in active) exit 0 ;; *) exit 3 ;; esac
            }
            emit_enabled() {
              printf '%s\\n' "$1"
              case "$1" in enabled|linked|linked-runtime|alias) exit 0 ;; *) exit 1 ;; esac
            }
            case "$args" in
              "is-active firebot-bridge") emit_active "$(cat "${FAKE_STATE:?}/bridge_active" 2>/dev/null || echo inactive)" ;;
              "is-active firebot-vehicle-edge") emit_active "$(cat "$FAKE_STATE/edge_active" 2>/dev/null || echo inactive)" ;;
              "is-enabled firebot-control") emit_enabled "$(cat "$FAKE_STATE/ctl_enabled" 2>/dev/null || echo disabled)" ;;
              "is-active firebot-control") emit_active "$(cat "$FAKE_STATE/ctl_active" 2>/dev/null || echo inactive)" ;;
              "is-active tailscaled") emit_active "$(cat "$FAKE_STATE/tailscaled_active" 2>/dev/null || echo inactive)" ;;
              *) echo "$args" >> "${FAKE_CALLS:?}"; exit 0 ;;
            esac
            """
        ))
        self.fake_systemctl.chmod(0o755)

    def env(self, **extra: str) -> dict:
        e = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_EDGE_HOME": str(self.edge_home),
            "FIREBOT_TEST_SYSTEMCTL": str(self.fake_systemctl),
            "FIREBOT_TEST_BRIDGE_STATUS_FILE": str(self.status_file),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_SKIP_CHOWN": "1",
            "FAKE_STATE": str(self.state),
            "FAKE_CALLS": str(self.calls),
        }
        e.update(extra)
        return e

    def installer(self) -> Path:
        return self.repo / "integration" / "ros1" / "vehicle-edge" / "install" / "install-tools-release.sh"

    def run_install(self, sha: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.installer()), "--sha", sha,
             "--edge-home", str(self.edge_home), "--bin-dir", str(self.bin_dir)],
            env=self.env(**extra), capture_output=True, text=True, timeout=180)

    def add_commit(self) -> str:
        marker = self.repo / f"marker-{len(list(self.repo.glob('marker-*'))) + 1}.txt"
        marker.write_text("x\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "next")
        return git(self.repo, "rev-parse", "HEAD")

    def current_sha(self) -> str:
        return os.path.basename(os.readlink(str(self.edge_home / "tools" / "current")))

    def previous_sha(self) -> str:
        return os.path.basename(os.readlink(str(self.edge_home / "tools" / "previous")))

    def no_service_mutations(self) -> bool:
        return not self.calls.exists() or self.calls.read_text().strip() == ""


def test_normal_install_mqtt_zero_downtime() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="installer-txn-"))
    try:
        fx = InstallerFixture(tmp)
        r = fx.run_install(fx.sha)
        check("正常安装 TOOLS_INSTALL=PASS", r.returncode == 0 and "TOOLS_INSTALL=PASS" in r.stdout, r.stdout[-400:])
        check("LEGACY_BRIDGE_STATE_UNCHANGED=YES", "LEGACY_BRIDGE_STATE_UNCHANGED=YES" in r.stdout)
        check("LEGACY_MQTT_HOST_UNCHANGED=YES", "LEGACY_MQTT_HOST_UNCHANGED=YES" in r.stdout)
        check("LEGACY_MQTT_CONNECTED_UNCHANGED=YES", "LEGACY_MQTT_CONNECTED_UNCHANGED=YES" in r.stdout)
        check("不触发任何 systemctl mutation", fx.no_service_mutations(), fx.calls.read_text() if fx.calls.exists() else "")
        check("/usr/local/bin/firebotctl 是 symlink", (fx.bin_dir / "firebotctl").is_symlink())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_case_a_selftest_fail_keeps_old() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="installer-caseA-"))
    try:
        fx = InstallerFixture(tmp)
        fx.run_install(fx.sha)  # 建立 A
        sha_b = fx.add_commit()
        r = fx.run_install(sha_b, FIREBOT_TEST_FAIL_SELFTEST="1")
        check("CASE A TOOLS_INSTALL=FAIL", r.returncode != 0 and "TOOLS_INSTALL=FAIL" in r.stdout, r.stdout[-400:])
        check("CASE A NEW_TOOLING_SELFTEST_FAILED", "NEW_TOOLING_SELFTEST_FAILED" in r.stdout)
        check("CASE A tools/current 保持 A", fx.current_sha() == fx.sha)
        check("CASE A tools/previous 保持 none", not (fx.edge_home / "tools" / "previous").exists())
        check("CASE A /usr/local/bin/firebotctl 保持指向 A 链路",
              os.path.realpath(str(fx.bin_dir / "firebotctl"))
              == str(fx.edge_home / "tools" / "releases" / fx.sha / "firebotctl"))
        check("CASE A services 不变（无 mutation）", fx.no_service_mutations())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_case_b_tooling_status_fail_restores_pointers() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="installer-caseB-"))
    try:
        fx = InstallerFixture(tmp)
        fx.run_install(fx.sha)
        sha_b = fx.add_commit()
        fx.run_install(sha_b)  # current=B previous=A
        sha_c = fx.add_commit()
        r = fx.run_install(sha_c, FIREBOT_TEST_FAIL_TOOLING_STATUS="1")
        check("CASE B TOOLS_INSTALL=FAIL", r.returncode != 0 and "TOOLS_INSTALL=FAIL" in r.stdout, r.stdout[-400:])
        check("CASE B TOOLS_INSTALL_ROLLED_BACK=YES", "TOOLS_INSTALL_ROLLED_BACK=YES" in r.stdout)
        check("CASE B current 恢复 B", fx.current_sha() == sha_b)
        check("CASE B previous 恢复 A", fx.previous_sha() == fx.sha)
        check("CASE B /usr/local/bin/firebotctl 未动（仍指向 tools/current）",
              os.readlink(str(fx.bin_dir / "firebotctl")) == str(fx.edge_home / "tools" / "current" / "firebotctl"))
        check("CASE B services 不变", fx.no_service_mutations())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_case_c_postcheck_fail_restores_link_and_pointers() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="installer-caseC-"))
    try:
        fx = InstallerFixture(tmp)
        fx.run_install(fx.sha)
        sha_b = fx.add_commit()
        fx.run_install(sha_b)  # current=B previous=A, link -> tools/current
        sha_c = fx.add_commit()
        r = fx.run_install(sha_c, FIREBOT_TEST_FAIL_POSTCHECK="1")
        check("CASE C TOOLS_INSTALL=FAIL", r.returncode != 0 and "TOOLS_INSTALL=FAIL" in r.stdout, r.stdout[-400:])
        check("CASE C TOOLS_INSTALL_ROLLED_BACK=YES", "TOOLS_INSTALL_ROLLED_BACK=YES" in r.stdout)
        check("CASE C current 恢复 B", fx.current_sha() == sha_b)
        check("CASE C previous 恢复 A", fx.previous_sha() == fx.sha)
        check("CASE C link 恢复 tools/current/firebotctl",
              os.readlink(str(fx.bin_dir / "firebotctl")) == str(fx.edge_home / "tools" / "current" / "firebotctl"))
        check("CASE C services 不变", fx.no_service_mutations())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_case_d_mqtt_drift_fail() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="installer-caseD-"))
    try:
        fx = InstallerFixture(tmp)
        fx.run_install(fx.sha)
        sha_b = fx.add_commit()
        r = fx.run_install(sha_b, FIREBOT_TEST_MQTT_DRIFT="1")
        check("CASE D TOOLS_INSTALL=FAIL", r.returncode != 0 and "TOOLS_INSTALL=FAIL" in (r.stdout + r.stderr),
              r.stdout[-300:] + r.stderr[-300:])
        check("CASE D LEGACY_MQTT_CONNECTED_UNCHANGED=NO", "LEGACY_MQTT_CONNECTED_UNCHANGED=NO" in r.stdout)
        check("CASE D 不 restart/stop legacy、不 start edge（无 mutation）", fx.no_service_mutations(),
              fx.calls.read_text() if fx.calls.exists() else "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_normal_install_mqtt_zero_downtime()
    test_case_a_selftest_fail_keeps_old()
    test_case_b_tooling_status_fail_restores_pointers()
    test_case_c_postcheck_fail_restores_link_and_pointers()
    test_case_d_mqtt_drift_fail()
    print(f"\nINSTALLER_TRANSACTION_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
