#!/usr/bin/env python3
"""Vehicle Edge 工程化行为测试（VEHICLE_EDGE_HOME_ROOT_CONSOLIDATION）。

fixture/mock：不启动真实 ROS/systemd、不触真实车、不建 /home/tl——
  - Edge Home 经 FIREBOT_EDGE_HOME 注入临时目录
  - fake systemctl 经 FIREBOT_TEST_SYSTEMCTL 注入
  - fake repo：vehicle-edge 源码树拷贝进临时 git 仓库（edge-install 的交付源）
  - FIREBOT_TEST_SKIP_ROOT=1（CI 无 root）、FIREBOT_TEST_SKIP_CHOWN=1（无 chown 权限）

覆盖：
  EDGE_INIT_LAYOUT / EDGE_INIT_IDEMPOTENT / EDGE_INIT_NO_SECRET
  EDGE_RUNTIME_USER_NOT_CONFIGURED
  EDGE_INSTALL_LEGACY_ACTIVE_REJECTED / EDGE_SERVICE_ACTIVE_REJECTED（互斥）
  EDGE_INSTALL_NO_CONTROL_TOUCH / EDGE_INSTALL_NO_CATKIN
  EDGE_INSTALL_SUCCESS（release + current symlink + unit + wrappers + inactive）
  RELEASE_ATOMIC_SWITCH / RELEASE_SWITCH_FAIL_KEEPS_CURRENT
  LEGACY_AND_EDGE_MUTUAL_EXCLUSION

运行：cd vehicle-edge && python3 tests/test_edge_home.py
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

VEHICLE_EDGE_SRC = Path(__file__).resolve().parents[1]  # integration/ros1/vehicle-edge

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
    return subprocess.run(
        ["git", "-c", "user.email=test@ci.local", "-c", "user.name=ci", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class Fixture:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="edge-home-"))
        # fake repo：vehicle-edge 源码树（交付源）
        self.repo = self.tmp / "fake-repo"
        (self.repo / "integration" / "ros1").mkdir(parents=True)
        shutil.copytree(
            VEHICLE_EDGE_SRC,
            self.repo / "integration" / "ros1" / "vehicle-edge",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture")
        self.sha = git(self.repo, "rev-parse", "HEAD")

        self.edge_home = self.tmp / "firebot-edge"
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "bridge_active").write_text("inactive")
        (self.state / "edge_active").write_text("inactive")
        (self.state / "edge_enabled").write_text("disabled")
        (self.state / "ctl_active").write_text("inactive")
        (self.state / "ctl_enabled").write_text("disabled")
        self.syslog = self.tmp / "systemctl.log"
        self.systemd_dir = self.tmp / "systemd"
        self.systemd_dir.mkdir()
        self.wrap_dir = self.tmp / "wrapbin"
        self.wrap_dir.mkdir()

        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self._write_fake_systemctl()
        # preflight 所需只读命令（fixture；preflight 纯只读语义）
        (self.fakebin / "curl").write_text("#!/usr/bin/env bash\nexit 0\n")
        (self.fakebin / "getent").write_text("#!/usr/bin/env bash\necho 124.223.220.182 firebot.youdaozd.com\n")
        (self.fakebin / "timedatectl").write_text("#!/usr/bin/env bash\necho NTPSynchronized=yes\n")
        (self.fakebin / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n")
        (self.fakebin / "openssl").write_text(
            "#!/usr/bin/env bash\ncat >/dev/null\necho 'Verify return code: 0 (ok)'\n")
        self.os_release = self.tmp / "os-release"
        self.os_release.write_text('ID=ubuntu\nVERSION_ID="20.04"\n')
        self.ros_setup_file = self.tmp / "ros-noetic-setup.bash"
        self.ros_setup_file.write_text("# fixture\n")
        for f in self.fakebin.iterdir():
            f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def _write_fake_systemctl(self) -> None:
        self.fake_systemctl = self.fakebin / "systemctl"
        self.fake_systemctl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                echo "$*" >> "${FAKE_LOG:?}"
                args="$*"
                case "$args" in
                  "is-active firebot-bridge") cat "${FAKE_STATE:?}/bridge_active" 2>/dev/null || echo inactive ;;
                  "is-active firebot-vehicle-edge") cat "$FAKE_STATE/edge_active" 2>/dev/null || echo inactive ;;
                  "is-enabled firebot-vehicle-edge") cat "$FAKE_STATE/edge_enabled" 2>/dev/null || echo disabled ;;
                  "is-enabled firebot-control") cat "$FAKE_STATE/ctl_enabled" 2>/dev/null || echo disabled ;;
                  "is-active firebot-control") cat "$FAKE_STATE/ctl_active" 2>/dev/null || echo inactive ;;
                  "is-active firebot-bridge.service"*) echo inactive ;;
                  "show firebot-vehicle-edge -p MainPID --value") cat "$FAKE_STATE/main_pid" 2>/dev/null || echo 0 ;;
                  "enable --now firebot-vehicle-edge")
                    echo enabled > "$FAKE_STATE/edge_enabled"; echo active > "$FAKE_STATE/edge_active"
                    echo "${FAKE_MAIN_PID:-4242}" > "$FAKE_STATE/main_pid"
                    printf '{"mqtt_connected": %s, "boot_id": "%s", "pid": %s, "release_sha": "%s", "process_started_at": "2026-10-28T00:00:01Z", "status_updated_at": "2026-10-28T00:00:01Z"}' \
                      "${FAKE_STATUS_CONNECTED:-true}" "${FAKE_BOOT_ID:-boot-fixture}" "${FAKE_MAIN_PID:-4242}" "${FAKE_RELEASE_SHA:-}" > "${FAKE_STATUS_JSON:?}"
                    ;;
                  "disable --now firebot-vehicle-edge")
                    echo disabled > "$FAKE_STATE/edge_enabled"; echo inactive > "$FAKE_STATE/edge_active"
                    echo 0 > "$FAKE_STATE/main_pid"
                    ;;
                  "daemon-reload") ;;
                  *) echo "unexpected: $args" >&2; exit 1 ;;
                esac
                exit 0
                """
            )
        )

    def env(self) -> dict:
        return {
            "PATH": f"{self.fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_TEST_SYSTEMCTL": str(self.fake_systemctl),
            "FIREBOT_EDGE_HOME": str(self.edge_home),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_SKIP_CHOWN": "1",
            "FIREBOT_TEST_SYSTEMD_DIR": str(self.systemd_dir),
            "FIREBOT_TEST_WRAP_DIR": str(self.wrap_dir),
            "FIREBOT_TEST_STATUS_JSON": str(self.edge_home / "runtime" / "status.json"),
            "FAKE_STATE": str(self.state),
            "FAKE_LOG": str(self.syslog),
            "FAKE_STATUS_JSON": str(self.edge_home / "runtime" / "status.json"),
            "FIREBOT_TEST_CURL": str(self.fakebin / "curl"),
            "FIREBOT_TEST_GETENT": str(self.fakebin / "getent"),
            "FIREBOT_TEST_TIMEDATECTL": str(self.fakebin / "timedatectl"),
            "FIREBOT_TEST_PGREP": str(self.fakebin / "pgrep"),
            "FIREBOT_TEST_OPENSSL": str(self.fakebin / "openssl"),
            "FIREBOT_TEST_OS_RELEASE": str(self.os_release),
            "FIREBOT_TEST_ROS_NOETIC_SETUP": str(self.ros_setup_file),
            "FAKE_MAIN_PID": os.environ.get("FAKE_MAIN_PID", "4242"),
            "FAKE_BOOT_ID": os.environ.get("FAKE_BOOT_ID", "boot-fixture"),
            "FAKE_RELEASE_SHA": os.environ.get("FAKE_RELEASE_SHA", ""),
            "FAKE_STATUS_CONNECTED": os.environ.get("FAKE_STATUS_CONNECTED", "true"),
        }

    def run(self, script: Path, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        env = self.env()
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=120
        )

    def lifecycle(self, name: str) -> Path:
        return self.repo / "integration" / "ros1" / "vehicle-edge" / "lifecycle" / name

    def installer(self, name: str) -> Path:
        return self.repo / "integration" / "ros1" / "vehicle-edge" / "install" / name


def test_edge_init_layout_and_idempotent() -> None:
    fx = Fixture()
    try:
        r = fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        check("EDGE_INIT_LAYOUT=PASS", r.returncode == 0 and "EDGE_INIT=PASS" in r.stdout, r.stdout[-200:])
        for d, mode in (("", "755"), ("secrets", "700"), ("backups", "700"),
                        ("runtime", "750"), ("data", "750"), ("logs", "750"),
                        ("config", "755"), ("certs", "755"), ("releases", "755")):
            p = fx.edge_home / d
            actual = oct(stat.S_IMODE(p.stat().st_mode))[2:] if p.exists() else "-"
            check(f"EDGE_INIT 权限 {d or '<root>'}={mode}", actual == mode, f"actual={actual}")
        install_env = fx.edge_home / "config" / "install.env"
        text = install_env.read_text()
        check("install.env 显式 runtime user", "FIREBOT_EDGE_USER=tl" in text and "FIREBOT_EDGE_HOME=" in text)
        # 幂等
        r2 = fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        check("EDGE_INIT_IDEMPOTENT（EXISTS 不重写）", r2.returncode == 0
              and "EDGE_INIT=EXISTS" in r2.stdout and "EDGE_INIT_IDEMPOTENT=PASS" in r2.stdout)
        # 绝不生成 credential（无 mqtt.env/device-token）
        check("EDGE_INIT_NO_SECRET", not (fx.edge_home / "secrets" / "mqtt.env").exists()
              and not (fx.edge_home / "secrets" / "device-token").exists())
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_edge_install_runtime_user_required() -> None:
    fx = Fixture()
    try:
        # 未 edge-init（无 install.env）→ EDGE_RUNTIME_USER_NOT_CONFIGURED
        r = fx.run(fx.lifecycle("edge-install.sh"), str(fx.repo),
                   FIREBOT_REQUIRE_SHA=fx.sha)
        check("EDGE_RUNTIME_USER_NOT_CONFIGURED", r.returncode != 0
              and "EDGE_RUNTIME_USER_NOT_CONFIGURED" in r.stdout, r.stdout[-200:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_edge_install_legacy_active_rejected() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        (fx.state / "bridge_active").write_text("active")
        r = fx.run(fx.lifecycle("edge-install.sh"), str(fx.repo),
                   FIREBOT_REQUIRE_SHA=fx.sha)
        check("EDGE_INSTALL_LEGACY_ACTIVE_REJECTED", r.returncode != 0
              and "EDGE_INSTALL=REJECTED" in r.stdout and "LEGACY_BRIDGE_ACTIVE" in r.stdout,
              r.stdout[-200:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_edge_install_edge_active_rejected() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        (fx.state / "edge_active").write_text("active")
        r = fx.run(fx.lifecycle("edge-install.sh"), str(fx.repo),
                   FIREBOT_REQUIRE_SHA=fx.sha)
        check("EDGE_INSTALL_EDGE_ACTIVE_REJECTED（安装≠启动）", r.returncode != 0
              and "EDGE_SERVICE_ACTIVE" in r.stdout, r.stdout[-200:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_edge_install_success_and_no_control_touch() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        r = fx.run(fx.lifecycle("edge-install.sh"), str(fx.repo),
                   FIREBOT_REQUIRE_SHA=fx.sha)
        check("EDGE_INSTALL_SUCCESS", r.returncode == 0 and "EDGE_INSTALL=PASS" in r.stdout, r.stdout[-300:])
        check("EDGE_INSTALL_NO_CONTROL_TOUCH 输出", "EDGE_INSTALL_NO_CONTROL_TOUCH=PASS" in r.stdout)
        check("EDGE_INSTALL_NO_CATKIN 输出", "EDGE_INSTALL_NO_CATKIN=PASS" in r.stdout)
        # release + current symlink + manifest
        current = fx.edge_home / "current"
        check("current symlink 指向 releases/<SHA>", current.is_symlink()
              and current.readlink() == Path("releases") / fx.sha)
        manifest = json.loads((current / "manifest.json").read_text())
        check("manifest product/source_sha", manifest["product"] == "firebot-edge"
              and manifest["source_sha"] == fx.sha)
        # systemd unit 安装（新 unit，不改 legacy）
        unit = fx.systemd_dir / "firebot-vehicle-edge.service"
        text = unit.read_text()
        check("systemd unit User=tl", "User=tl" in text)
        check("systemd unit ExecStart=firebot-edge run", "firebot-edge run" in text)
        check("systemd unit 注入 FIREBOT_EDGE_HOME", f"FIREBOT_EDGE_HOME={fx.edge_home}" in text)
        check("systemd unit 注入 status 文件", "FIREBOT_BRIDGE_STATUS_FILE=" in text)
        check("systemd unit Conflicts=legacy", "Conflicts=firebot-bridge.service" in text)
        # wrappers
        for name in ("vehicle-connection-status", "vehicle-connection-watch",
                     "vehicle-connectivity-selftest", "vehicle-edge-doctor"):
            check(f"wrapper {name}", (fx.wrap_dir / name).exists())
        # 安装后 inactive+disabled
        check("EDGE 安装后 inactive+disabled", "EDGE_SERVICE_STATE=inactive+disabled" in r.stdout)
        # 绝不触碰 ROS workspace / control（可执行行无 firerobot_ws/catkin/firebot-control 安装动作）
        sh = (fx.repo / "integration/ros1/vehicle-edge/lifecycle/edge-install.sh").read_text()
        active = [ln for ln in sh.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        check("EDGE_INSTALL_NO_CONTROL_TOUCH（无 firerobot_ws/firebot-control/catkin）",
              not any(t in ln for ln in active for t in ("firerobot_ws", "catkin_make", "firebot-control", "roscore")))
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_release_atomic_switch() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        # 第一次 build+switch
        b1 = fx.run(fx.installer("release-manager.sh"), "build",
                    str(fx.repo / "integration/ros1/vehicle-edge"), fx.sha, str(fx.edge_home))
        check("RELEASE_BUILD 第一次", b1.returncode == 0, b1.stdout[-200:])
        s1 = fx.run(fx.installer("release-manager.sh"), "switch", fx.sha, str(fx.edge_home))
        check("RELEASE_SWITCH 第一次", s1.returncode == 0 and "previous=none" in s1.stdout, s1.stdout[-200:])
        # 第二个 SHA（commit 一个新文件）
        (fx.repo / "second.txt").write_text("2\n")
        git(fx.repo, "add", ".")
        git(fx.repo, "commit", "-qm", "second")
        sha2 = git(fx.repo, "rev-parse", "HEAD")
        b2 = fx.run(fx.installer("release-manager.sh"), "build",
                    str(fx.repo / "integration/ros1/vehicle-edge"), sha2, str(fx.edge_home))
        s2 = fx.run(fx.installer("release-manager.sh"), "switch", sha2, str(fx.edge_home))
        check("RELEASE_SWITCH 第二次（previous 轮转）", s2.returncode == 0
              and f"previous={fx.sha}" in s2.stdout, s2.stdout[-200:])
        check("current → sha2", (fx.edge_home / "current").readlink() == Path("releases") / sha2)
        check("previous → sha1", (fx.edge_home / "previous").readlink() == Path("releases") / fx.sha)
        # 失败 switch（不存在 SHA）current 不变
        bad = fx.run(fx.installer("release-manager.sh"), "switch", "0" * 40, str(fx.edge_home))
        check("RELEASE_SWITCH_FAIL_KEEPS_CURRENT", bad.returncode != 0
              and (fx.edge_home / "current").readlink() == Path("releases") / sha2)
        # 车辆特定目录禁止组装
        ve = fx.repo / "integration/ros1/vehicle-edge"
        (ve / "R002").mkdir()
        (ve / "R002" / "vehicle_specific.py").write_text("# forbidden\n")
        git(fx.repo, "add", ".")
        git(fx.repo, "commit", "-qm", "vehicle-specific")
        sha3 = git(fx.repo, "rev-parse", "HEAD")
        b3 = fx.run(fx.installer("release-manager.sh"), "build",
                    str(ve), sha3, str(fx.edge_home))
        check("NO_VEHICLE_SPECIFIC_PRODUCTION_CODE（R002 目录拒绝组装）",
              b3.returncode != 0 and "车辆特定" in (b3.stdout + b3.stderr), b3.stdout[-200:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_legacy_and_edge_mutual_exclusion() -> None:
    fx = Fixture()
    try:
        # readonly-start 前置门：legacy active → REJECTED
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        (fx.edge_home / "config").mkdir(exist_ok=True)
        (fx.edge_home / "certs").mkdir(exist_ok=True)
        (fx.edge_home / "config" / "bridge.env").write_text(
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com\nFIREBOT_MQTT_PORT=8883\n"
            f"FIREBOT_CA_CERT={fx.edge_home}/certs/mqtt-ca.crt\nFIREBOT_SUPPORTED_COMMANDS=\n")
        (fx.state / "bridge_active").write_text("active")
        r = fx.run(fx.lifecycle("readonly-start.sh"))
        check("LEGACY_AND_EDGE_MUTUAL_EXCLUSION（legacy active → readonly-start REJECTED）",
              r.returncode != 0 and "LEGACY_BRIDGE_INACTIVE=no" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


if __name__ == "__main__":
    test_edge_init_layout_and_idempotent()
    test_edge_install_runtime_user_required()
    test_edge_install_legacy_active_rejected()
    test_edge_install_edge_active_rejected()
    test_edge_install_success_and_no_control_touch()
    test_release_atomic_switch()
    test_legacy_and_edge_mutual_exclusion()
    print(f"\nEDGE_HOME_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
