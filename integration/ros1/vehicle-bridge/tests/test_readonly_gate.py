#!/usr/bin/env python3
"""readonly-preflight / readonly-start 只读门行为测试（REAL_VEHICLE_CONNECTIVITY_FIELD_GATE）。

fixture/mock 策略：不启动真实 ROS / 机器人 / systemd ——
  - fake systemctl/curl/openssl/getent/timedatectl/pgrep 脚本置于临时 fakebin，
    经 FIREBOT_TEST_* 环境变量注入（脚本内已支持覆盖，生产默认走真实命令）。
  - /etc/firebot 经 FIREBOT_TEST_ETC_ROOT 注入临时目录。
  - repo 检查：readonly-preflight.sh 拷贝进临时 git 仓库（1 个 commit），隔离真实工作区状态。
  - status.json 经 FIREBOT_TEST_STATUS_JSON 注入。

运行：cd vehicle-bridge && python3 tests/test_readonly_gate.py
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

SRC_DIR = Path(__file__).resolve().parents[2]  # integration/ros1
SCRIPT = SRC_DIR / "readonly-preflight.sh"
START_SCRIPT = SRC_DIR / "readonly-start.sh"

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


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@ci.local", "-c", "user.name=ci", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


import json as _json


def _setup_current_release(fx, sha="9" * 40):
    """readonly-start 的 STATUS_RELEASE_MATCH 需要 current symlink + manifest。"""
    rel = fx.etc / "releases" / sha
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "manifest.json").write_text(_json.dumps({"source_sha": sha}))
    cur = fx.etc / "current"
    if cur.is_symlink() or cur.exists():
        cur.unlink()
    cur.symlink_to(f"releases/{sha}")
    os.environ["FAKE_RELEASE_SHA"] = sha


class Fixture:
    """fake repo + fakebin + fake /etc/firebot + fake systemctl 状态。"""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="readonly-gate-"))
        # fake git repo（readonly-preflight 要求 REPO_DIR 干净且 SHA 可 cat-file）
        self.repo = self.tmp / "fake-repo"
        ros1_fake = self.repo / "integration" / "ros1"
        ros1_fake.mkdir(parents=True)
        shutil.copy(SCRIPT, ros1_fake / "readonly-preflight.sh")
        # shim 转发目标：vehicle-edge/lifecycle（生产逻辑唯一 source of truth）
        lifecycle_dst = ros1_fake / "vehicle-edge" / "lifecycle"
        lifecycle_dst.mkdir(parents=True)
        for name in ("readonly-preflight.sh", "readonly-start.sh", "migration-stop-legacy.sh"):
            shutil.copy(SRC_DIR / "vehicle-edge" / "lifecycle" / name, lifecycle_dst / name)
        # lifecycle 依赖的共享 systemd 状态 helper
        lib_dst = ros1_fake / "vehicle-edge" / "lib"
        lib_dst.mkdir(parents=True)
        shutil.copy(SRC_DIR / "vehicle-edge" / "lib" / "systemd_state.sh", lib_dst / "systemd_state.sh")
        shutil.copy(SRC_DIR / "vehicle-edge" / "lib" / "legacy_health.sh", lib_dst / "legacy_health.sh")
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture")
        self.sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # fake /etc/firebot
        self.etc = self.tmp / "etc-firebot"
        self.etc.mkdir()

        # fake systemctl 状态
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.ctl_enabled = self.state / "ctl_enabled"  # 默认 disabled
        self.ctl_active = self.state / "ctl_active"  # 默认 inactive
        self.bridge_active = self.state / "bridge_active"
        (self.state / "edge_active").write_text("inactive")
        (self.state / "edge_enabled").write_text("disabled")
        self.status_json = self.tmp / "status.json"
        self.fake_log = self.tmp / "systemctl.log"
        (self.state / "ctl_enabled").write_text("disabled")
        (self.state / "ctl_active").write_text("inactive")
        (self.state / "bridge_active").write_text("inactive")
        # legacy 健康默认 fixture（bootstrap legacy-active 时 legacy_link_health 需要）
        self.legacy_bridge_env = self.tmp / "legacy-bridge.env"
        self.legacy_bridge_env.write_text("FIREBOT_MQTT_HOST=100.110.31.112\nFIREBOT_MQTT_PORT=8883\n")
        self.legacy_status = self.tmp / "legacy-status.json"
        self.legacy_status.write_text('{"mqtt_connected": true, "boot_id": "b1"}\n')

        # fakebin
        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self._write_fake_systemctl()
        (self.fakebin / "curl").write_text("#!/usr/bin/env bash\nexit 0\n")
        (self.fakebin / "getent").write_text(
            "#!/usr/bin/env bash\necho 124.223.220.182 firebot.youdaozd.com\n"
        )
        (self.fakebin / "openssl").write_text(
            "#!/usr/bin/env bash\ncat >/dev/null\necho 'Verify return code: 0 (ok)'\n"
        )
        (self.fakebin / "timedatectl").write_text(
            "#!/usr/bin/env bash\necho NTPSynchronized=yes\n"
        )
        (self.fakebin / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n")
        for f in self.fakebin.iterdir():
            f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def _write_fake_systemctl(self) -> None:
        self.fake_systemctl = self.fakebin / "systemctl"
        shutil.copy(SRC_DIR / "vehicle-edge" / "tests" / "fake_systemctl.sh", self.fake_systemctl)
        self.fake_systemctl.chmod(self.fake_systemctl.stat().st_mode | stat.S_IXUSR)

    def write_os_release(self, version: str = "20.04") -> Path:
        p = self.tmp / "os-release"
        p.write_text(f'ID=ubuntu\nVERSION_ID="{version}"\n')
        return p

    def write_ros_noetic(self) -> Path:
        p = self.tmp / "ros-noetic-setup.bash"
        p.write_text("# fixture\n")
        return p

    def env(self, **over) -> dict[str, str]:
        e = {
            "PATH": f"{self.fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_TEST_SYSTEMCTL": str(self.fake_systemctl),
            "FIREBOT_TEST_CURL": str(self.fakebin / "curl"),
            "FIREBOT_TEST_GETENT": str(self.fakebin / "getent"),
            "FIREBOT_TEST_OPENSSL": str(self.fakebin / "openssl"),
            "FIREBOT_TEST_TIMEDATECTL": str(self.fakebin / "timedatectl"),
            "FIREBOT_TEST_PGREP": str(self.fakebin / "pgrep"),
            "FIREBOT_TEST_ETC_ROOT": str(self.etc),
            "FIREBOT_EDGE_HOME": str(self.etc),
            "FIREBOT_TEST_OS_RELEASE": str(self.write_os_release()),
            "FIREBOT_TEST_ROS_NOETIC_SETUP": str(self.write_ros_noetic()),
            "FIREBOT_TEST_STATUS_JSON": str(self.status_json),
            "FIREBOT_TEST_LEGACY_BRIDGE_ENV": str(self.legacy_bridge_env),
            "FIREBOT_TEST_BRIDGE_STATUS_FILE": str(self.legacy_status),
            "FIREBOT_READONLY_MQTT_WAIT_SECONDS": "6",
            "FAKE_STATE": str(self.state),
            "FAKE_STATUS_JSON": str(self.status_json),
            "FAKE_LOG": str(self.fake_log),
            "FAKE_RELEASE_SHA": os.environ.get("FAKE_RELEASE_SHA", ""),
        }
        e.update(over)
        return e

    def write_bootstrap_files(self) -> None:
        # Vehicle Edge 布局：EDGE_HOME=etc（config/ certs/ secrets/）
        (self.etc / "config").mkdir(exist_ok=True)
        (self.etc / "certs").mkdir(exist_ok=True)
        (self.etc / "secrets").mkdir(exist_ok=True)
        (self.etc / "config" / "bootstrap.env").write_text(
            "FIREBOT_SERVER_URL=https://firebot.youdaozd.com\n"
        )
        (self.etc / "certs" / "mqtt-ca.crt").write_text("fixture-ca\n")

    def write_enrolled_files(self, supported_commands: str = "") -> None:
        self.write_bootstrap_files()
        ca_path = self.etc / "certs" / "mqtt-ca.crt"
        (self.etc / "config" / "identity.env").write_text(
            "DEVICE_ID=firebot-vehicle-02\nPROFILE_ID=firebot_ros1_standard_v1\n"
        )
        lines = [
            "FIREBOT_VEHICLE_ID=firebot-vehicle-02",
            "FIREBOT_MQTT_USERNAME=firebot-vehicle-02",
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com",
            "FIREBOT_MQTT_PORT=8883",
            f"FIREBOT_CA_CERT={ca_path}",
            f"FIREBOT_SUPPORTED_COMMANDS={supported_commands}",
        ]
        (self.etc / "config" / "bridge.env").write_text("\n".join(lines) + "\n")
        (self.etc / "secrets" / "mqtt.env").write_text("FIREBOT_MQTT_PASSWORD=x\n")
        (self.etc / "secrets" / "mqtt.env").chmod(0o600)
        (self.etc / "config" / "assignment.env").write_text("SUPPORTED_COMMANDS=\n")

    def run_preflight(self, phase: str, device_id: str = "firebot-vehicle-02") -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "bash",
                str(self.repo / "integration" / "ros1" / "readonly-preflight.sh"),
                "--phase",
                phase,
                "--device-id",
                device_id,
                "--expected-sha",
                self.sha,
            ],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def run_start(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(START_SCRIPT)],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_readonly_bootstrap_preflight_pass() -> None:
    fx = Fixture()
    try:
        fx.write_bootstrap_files()
        r = fx.run_preflight("bootstrap")
        check("READONLY_BOOTSTRAP_PREFLIGHT=PASS", r.returncode == 0 and "READONLY_BOOTSTRAP_PREFLIGHT=PASS" in r.stdout, r.stdout[-300:])
        check("bootstrap 输出含 HTTPS_SYSTEM_CA", "HTTPS_SYSTEM_CA=PASS" in r.stdout)
        check("bootstrap 输出含 MQTT_TLS_HOSTNAME", "MQTT_TLS_HOSTNAME=PASS" in r.stdout)
        check("bootstrap 输出 CONTROL_ENABLED=no", "CONTROL_ENABLED=no" in r.stdout)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_bootstrap_active_but_disabled_control_rejected() -> None:
    """ACTIVE_BUT_DISABLED_CONTROL=REJECTED：control disabled 但 active（手动启动）→ 拒绝。"""
    fx = Fixture()
    try:
        fx.write_bootstrap_files()
        (fx.state / "ctl_active").write_text("active")
        r = fx.run_preflight("bootstrap")
        check("ACTIVE_BUT_DISABLED_CONTROL=REJECTED", r.returncode != 0 and "CONTROL_ACTIVE=yes" in r.stdout, r.stdout[-200:])
        check("bootstrap FAIL 输出", "READONLY_BOOTSTRAP_PREFLIGHT=FAIL" in r.stdout)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_bootstrap_control_enabled_rejected() -> None:
    fx = Fixture()
    try:
        fx.write_bootstrap_files()
        (fx.state / "ctl_enabled").write_text("enabled")
        r = fx.run_preflight("bootstrap")
        check("VERIFY_CONTROL_ACTIVE_GATE=enabled 拒绝", r.returncode != 0 and "CONTROL_ENABLED=yes" in r.stdout, r.stdout[-200:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_enrolled_preflight_pass() -> None:
    fx = Fixture()
    try:
        fx.write_bootstrap_files()
        fx.write_enrolled_files()
        r = fx.run_preflight("enrolled")
        check("READONLY_ENROLLED_PREFLIGHT=PASS", r.returncode == 0 and "READONLY_ENROLLED_PREFLIGHT=PASS" in r.stdout, r.stdout[-300:])
        check("enrolled MQTT_ENV mode=600", "MQTT_ENV=yes mode=600" in r.stdout)
        check("enrolled MQTT_HOST=firebot.youdaozd.com", "MQTT_HOST=firebot.youdaozd.com" in r.stdout)
        check("enrolled SUPPORTED_COMMANDS_EMPTY=yes", "SUPPORTED_COMMANDS_EMPTY=yes" in r.stdout)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_enrolled_supported_commands_not_empty_rejected() -> None:
    fx = Fixture()
    try:
        fx.write_bootstrap_files()
        fx.write_enrolled_files(supported_commands="patrol")
        r = fx.run_preflight("enrolled")
        check(
            "SUPPORTED_COMMANDS_EMPTY_REQUIRED（patrol → FAIL）",
            r.returncode != 0 and "SUPPORTED_COMMANDS_NOT_EMPTY" in r.stdout,
            r.stdout[-200:],
        )
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_start_bridge_only() -> None:
    fx = Fixture()
    try:
        fx.write_enrolled_files()
        _setup_current_release(fx)
        r = fx.run_start()
        check("READONLY_START_BRIDGE_ONLY=PASS", r.returncode == 0 and "READONLY_BRIDGE_START=PASS" in r.stdout, r.stdout[-300:])
        check("BRIDGE_ACTIVE=YES 输出", "BRIDGE_ACTIVE=YES" in r.stdout)
        check("MQTT_CONNECTED=YES 输出", "MQTT_CONNECTED=YES" in r.stdout)
        check("CONTROL_ACTIVE=NO 输出", "CONTROL_ACTIVE=NO" in r.stdout)
        log = fx.fake_log.read_text()
        check("只调用过 firebot-vehicle-edge enable", "enable --now firebot-vehicle-edge" in log)
        check(
            "READONLY_START_CONTROL_UNTOUCHED（无 control enable/start/stop）",
            not any(c in log for c in ("enable firebot-control", "start firebot-control", "stop firebot-control")),
            log,
        )
        # 只检查可执行行（注释中的“绝不触碰”声明是合法负向语义）
        active = [ln for ln in START_SCRIPT.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        check(
            "不触碰 roscore/base/navigation/move_base/cmd_vel（可执行行无此类调用）",
            not any(term in ln for ln in active for term in ("roscore", "move_base", "cmd_vel", "roslaunch")),
        )
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_start_rejected_control_active() -> None:
    fx = Fixture()
    try:
        fx.write_enrolled_files()
        (fx.state / "ctl_active").write_text("active")
        r = fx.run_start()
        check("READONLY_START 控制 active → REJECTED", r.returncode != 0 and "READONLY_BRIDGE_START=REJECTED" in r.stdout, r.stdout[-200:])
        check("REJECTED 时未启动 bridge", "enable --now firebot-bridge" not in fx.fake_log.read_text())
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_start_rejected_supported_commands() -> None:
    fx = Fixture()
    try:
        fx.write_enrolled_files(supported_commands="patrol,stop_motion")
        r = fx.run_start()
        check(
            "READONLY_START patrol → REJECTED（SUPPORTED_COMMANDS_NOT_EMPTY）",
            r.returncode != 0 and "SUPPORTED_COMMANDS_NOT_EMPTY" in r.stdout,
            r.stdout[-200:],
        )
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_start_rejected_wrong_mqtt_host() -> None:
    fx = Fixture()
    try:
        fx.write_enrolled_files()
        (fx.etc / "config").mkdir(exist_ok=True)
        (fx.etc / "config" / "bridge.env").write_text(
            "FIREBOT_VEHICLE_ID=firebot-vehicle-02\n"
            "FIREBOT_MQTT_HOST=100.110.31.112\n"
            "FIREBOT_MQTT_PORT=8883\n"
            "FIREBOT_CA_CERT=%s/certs/mqtt-ca.crt\n"
            "FIREBOT_SUPPORTED_COMMANDS=\n" % fx.etc
        )
        r = fx.run_start()
        check("READONLY_START 旧 IP host → REJECTED", r.returncode != 0 and "MQTT_HOST 必须 firebot.youdaozd.com" in r.stdout, r.stdout[-200:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


if __name__ == "__main__":
    test_readonly_bootstrap_preflight_pass()
    test_readonly_bootstrap_active_but_disabled_control_rejected()
    test_readonly_bootstrap_control_enabled_rejected()
    test_readonly_enrolled_preflight_pass()
    test_readonly_enrolled_supported_commands_not_empty_rejected()
    test_readonly_start_bridge_only()
    test_readonly_start_rejected_control_active()
    test_readonly_start_rejected_supported_commands()
    test_readonly_start_rejected_wrong_mqtt_host()
    print(f"\nREADONLY_GATE_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
