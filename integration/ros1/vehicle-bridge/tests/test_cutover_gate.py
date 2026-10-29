#!/usr/bin/env python3
"""跨 Broker 迁移 cutover 事务门行为测试（REAL_R002_CUTOVER_TRANSACTION_FINAL_FIX）。

fixture/mock 策略（不启动真实 ROS / systemd / 不触真实车）：
  - fake systemctl：经 FIREBOT_TEST_SYSTEMCTL 注入（记录全部调用供断言）。
  - fake curl：经 FIREBOT_TEST_CURL 注入（记录 POST 调用；TOKEN_NOT_CONSUMED 断言依据）。
  - /etc/firebot：经 FIREBOT_TEST_ETC_ROOT 注入临时目录（断言“不写文件”）。
  - firebotctl：拷贝进临时 fake git 仓库（REPO_DIR 隔离；可验证拒绝发生在 release_sync 之前）。

覆盖：
  ENROLL_ACTIVE_BRIDGE_REJECTED / ENROLL_ACTIVE_BRIDGE_TOKEN_NOT_CONSUMED
  INSTALL_ACTIVE_BRIDGE_REJECTED
  MIGRATION_STOP_LEGACY_BRIDGE_ONLY / MIGRATION_STOP_CONTROL_UNTOUCHED / CONTROL_NOT_SAFE
  READONLY_START_FAILURE_AUTO_STOP / READONLY_START_FAILURE_AUTO_DISABLE
  ENROLLED_EXACT_SHA_MATCH_REQUIRED / BOOTSTRAP_OLD_SHA_ALLOWED

运行：cd vehicle-bridge && python3 tests/test_cutover_gate.py
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

ROS1_DIR = Path(__file__).resolve().parents[2]  # integration/ros1
FIREBOTCTL = ROS1_DIR / "firebotctl"
MIG_STOP = ROS1_DIR / "vehicle-edge" / "lifecycle" / "migration-stop-legacy.sh"
PREFLIGHT = ROS1_DIR / "readonly-preflight.sh"
START = ROS1_DIR / "vehicle-edge" / "lifecycle" / "readonly-start.sh"

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
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cutover-gate-"))
        # fake git repo（firebotctl 拷贝于此；隔离真实工作区）
        self.repo = self.tmp / "fake-repo"
        (self.repo / "integration" / "ros1").mkdir(parents=True)
        shutil.copy(FIREBOTCTL, self.repo / "integration" / "ros1" / "firebotctl")
        shutil.copy(PREFLIGHT, self.repo / "integration" / "ros1" / "readonly-preflight.sh")
        # profiles（真实内容拷贝；firebotctl/enroll.sh 经 PROFILE_DIR 使用）
        shutil.copytree(
            ROS1_DIR / "vehicle-edge" / "config" / "profiles",
            self.repo / "integration" / "ros1" / "vehicle-edge" / "config" / "profiles",
        )
        # lifecycle（enroll/readonly/migration-stop-legacy 唯一 source of truth）
        lifecycle_dst = self.repo / "integration" / "ros1" / "vehicle-edge" / "lifecycle"
        lifecycle_dst.mkdir(parents=True)
        for name in ("enroll.sh", "readonly-preflight.sh", "readonly-start.sh",
                     "migration-stop-legacy.sh", "edge-init.sh", "edge-install.sh",
                     "edge-verify.sh", "rollback.sh"):
            shutil.copy(ROS1_DIR / "vehicle-edge" / "lifecycle" / name, lifecycle_dst / name)
        # lifecycle 依赖的共享 systemd 状态 helper
        lib_dst = self.repo / "integration" / "ros1" / "vehicle-edge" / "lib"
        lib_dst.mkdir(parents=True)
        shutil.copy(ROS1_DIR / "vehicle-edge" / "lib" / "systemd_state.sh", lib_dst / "systemd_state.sh")
        shutil.copy(ROS1_DIR / "vehicle-edge" / "lib" / "legacy_health.sh", lib_dst / "legacy_health.sh")
        shutil.copy(
            ROS1_DIR / "profile-render.py",
            self.repo / "integration" / "ros1" / "profile-render.py",
        )
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture-1")
        self.sha1 = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "fixture.txt").write_text("second\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture-2")
        self.sha2 = git(self.repo, "rev-parse", "HEAD")

        self.etc = self.tmp / "etc-firebot"
        self.etc.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "ctl_enabled").write_text("disabled")
        (self.state / "ctl_active").write_text("inactive")
        (self.state / "bridge_enabled").write_text("disabled")
        (self.state / "bridge_active").write_text("inactive")
        (self.state / "edge_enabled").write_text("disabled")
        (self.state / "edge_active").write_text("inactive")
        self.status_json = self.tmp / "status.json"
        self.syslog = self.tmp / "systemctl.log"
        self.curllog = self.tmp / "curl.log"
        # legacy 健康默认 fixture（migration-stop/rollback 的 legacy_link_health 需要）
        self.legacy_bridge_env = self.tmp / "legacy-bridge.env"
        self.legacy_bridge_env.write_text("FIREBOT_MQTT_HOST=100.110.31.112\nFIREBOT_MQTT_PORT=8883\n")
        self.legacy_status = self.tmp / "legacy-status.json"
        self.legacy_status.write_text('{"mqtt_connected": true, "boot_id": "b1"}\n')

        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self._write_fake_systemctl()
        (self.fakebin / "curl").write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                echo "$*" >> "${FAKE_CURL_LOG:?}"
                if [[ "$*" == *"-X POST"* ]]; then
                  printf '{"device_id":"firebot-vehicle-02","profile_id":"firebot_ros1_standard_v1","mqtt_password":"x","device_token":"y","mqtt_host":"firebot.youdaozd.com","mqtt_port":8883}'
                fi
                exit 0
                """
            )
        )
        (self.fakebin / "getent").write_text("#!/usr/bin/env bash\necho 124.223.220.182 firebot.youdaozd.com\n")
        (self.fakebin / "timedatectl").write_text("#!/usr/bin/env bash\necho NTPSynchronized=yes\n")
        (self.fakebin / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n")
        (self.fakebin / "openssl").write_text(
            "#!/usr/bin/env bash\ncat >/dev/null\necho 'Verify return code: 0 (ok)'\n"
        )
        self.os_release = self.tmp / "os-release"
        self.os_release.write_text('ID=ubuntu\nVERSION_ID="20.04"\n')
        self.ros_setup = self.tmp / "ros-noetic-setup.bash"
        self.ros_setup.write_text("# fixture\n")
        for f in self.fakebin.iterdir():
            f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def _write_fake_systemctl(self) -> None:
        self.fake_systemctl = self.fakebin / "systemctl"
        shutil.copy(ROS1_DIR / "vehicle-edge" / "tests" / "fake_systemctl.sh", self.fake_systemctl)
        self.fake_systemctl.chmod(self.fake_systemctl.stat().st_mode | stat.S_IXUSR)

    def env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_TEST_SYSTEMCTL": str(self.fake_systemctl),
            "FIREBOT_TEST_CURL": str(self.fakebin / "curl"),
            "FIREBOT_TEST_ETC_ROOT": str(self.etc),
            "FIREBOT_EDGE_HOME": str(self.etc),
            "FIREBOT_TEST_LEGACY_BRIDGE_ENV": str(self.legacy_bridge_env),
            "FIREBOT_TEST_BRIDGE_STATUS_FILE": str(self.legacy_status),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_STATUS_JSON": str(self.status_json),
            "FIREBOT_READONLY_MQTT_WAIT_SECONDS": "4",
            "FAKE_STATE": str(self.state),
            "FAKE_STATUS_JSON": str(self.status_json),
            "FAKE_LOG": str(self.syslog),
            "FAKE_RELEASE_SHA": os.environ.get("FAKE_RELEASE_SHA", ""),
            "FAKE_CURL_LOG": str(self.curllog),
            "FAKE_STATUS_CONNECTED": os.environ.get("FAKE_STATUS_CONNECTED", "true"),
            "FIREBOT_TEST_OS_RELEASE": str(self.os_release),
            "FIREBOT_TEST_ROS_NOETIC_SETUP": str(self.ros_setup),
            "FIREBOT_TEST_GETENT": str(self.fakebin / "getent"),
            "FIREBOT_TEST_TIMEDATECTL": str(self.fakebin / "timedatectl"),
            "FIREBOT_TEST_PGREP": str(self.fakebin / "pgrep"),
            "FIREBOT_TEST_OPENSSL": str(self.fakebin / "openssl"),
        }

    def run_firebotctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.repo / "integration" / "ros1" / "firebotctl"), *args],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def run_script(self, script: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(script), *args],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def set_bridge(self, active: str = "inactive", enabled: str = "disabled") -> None:
        (self.state / "bridge_active").write_text(active)
        (self.state / "bridge_enabled").write_text(enabled)


def test_enroll_active_bridge_rejected_and_token_not_consumed() -> None:
    fx = Fixture()
    try:
        fx.set_bridge(active="active", enabled="enabled")
        r = fx.run_firebotctl(
            "vehicle", "enroll", "firebot-vehicle-02",
            "--token", "TESTTOKEN", "--server", "https://firebot.youdaozd.com",
        )
        check("ENROLL_ACTIVE_BRIDGE_REJECTED", r.returncode != 0 and "ENROLL=REJECTED" in r.stdout, r.stdout[-200:])
        check("输出 LEGACY_BRIDGE_STILL_ACTIVE", "LEGACY_BRIDGE_STILL_ACTIVE" in r.stdout)
        # TOKEN_NOT_CONSUMED：fake curl 无任何 POST 记录
        check(
            "ENROLL_ACTIVE_BRIDGE_TOKEN_NOT_CONSUMED",
            not fx.curllog.exists() or "-X POST" not in fx.curllog.read_text(),
        )
        # 不写任何文件
        check(
            "REJECTED 不写 identity/assignment/mqtt.env/device-token（Edge Home）",
            not any((fx.etc / sub / n).exists()
                    for sub, n in (("config", "identity.env"), ("config", "assignment.env"),
                                   ("secrets", "mqtt.env"), ("secrets", "device-token"))),
        )
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_enroll_inactive_bridge_continues() -> None:
    fx = Fixture()
    try:
        fx._write_bootstrap_files()  # noqa: SLF001
        fx.set_bridge(active="inactive", enabled="disabled")
        r = fx.run_firebotctl(
            "vehicle", "enroll", "firebot-vehicle-02",
            "--token", "TESTTOKEN", "--server", "https://firebot.youdaozd.com",
        )
        check("ENROLL inactive 正常继续", r.returncode == 0 and "ENROLL=PASS" in r.stdout, r.stdout[-300:])
        check("inactive 场景 token 被消费（POST 恰好 1 次）", fx.curllog.read_text().count("-X POST") == 1)
        check("enroll 输出到 Edge Home（identity + mqtt.env）",
              (fx.etc / "config" / "identity.env").exists() and (fx.etc / "secrets" / "mqtt.env").exists())
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_install_active_bridge_rejected() -> None:
    fx = Fixture()
    try:
        fx.set_bridge(active="active", enabled="disabled")
        sha = "0" * 40  # fake-repo 不存在的 SHA：若门失效会走到 release_sync 报“目标 SHA 不存在”
        r = fx.run_firebotctl("vehicle", "install", "--sha", sha)
        check("INSTALL_ACTIVE_BRIDGE_REJECTED", r.returncode != 0 and "VEHICLE_INSTALL=REJECTED" in r.stdout, r.stdout[-200:])
        check("输出 BRIDGE_ACTIVE_BEFORE_INSTALL", "BRIDGE_ACTIVE_BEFORE_INSTALL" in r.stdout)
        # 拒绝发生在 release_sync 之前：不会出现 release_sync 的“目标 SHA 在本地仓库不存在”
        check("未进入 release_sync（无 cat-file 报错）", "目标 SHA" not in r.stderr and "目标 SHA" not in r.stdout)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_migration_stop_legacy_bridge_only() -> None:
    fx = Fixture()
    try:
        fx.set_bridge(active="active", enabled="enabled")
        r = fx.run_script(MIG_STOP)
        check("MIGRATION_STOP_LEGACY 正常路径 PASS", r.returncode == 0 and "MIGRATION_CUTOVER_BOUNDARY=ENTERED" in r.stdout, r.stdout[-200:])
        check("LEGACY_BRIDGE_WAS_ACTIVE=YES", "LEGACY_BRIDGE_WAS_ACTIVE=YES" in r.stdout)
        check("LEGACY_BRIDGE_WAS_ENABLED=YES", "LEGACY_BRIDGE_WAS_ENABLED=YES" in r.stdout)
        check("LEGACY_BRIDGE_STOPPED=YES", "LEGACY_BRIDGE_STOPPED=YES" in r.stdout)
        check("CONTROL_ACTIVE=NO", "CONTROL_ACTIVE=NO" in r.stdout)
        log = fx.syslog.read_text()
        check("MIGRATION_STOP_LEGACY_BRIDGE_ONLY（只 disable --now firebot-bridge）", "disable --now firebot-bridge" in log)
        check(
            "MIGRATION_STOP_CONTROL_UNTOUCHED（无 control enable/start/stop）",
            not any(c in log for c in ("enable firebot-control", "start firebot-control", "stop firebot-control")),
            log,
        )
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_migration_stop_legacy_control_not_safe_rejected() -> None:
    fx = Fixture()
    try:
        (fx.state / "ctl_active").write_text("active")
        fx.set_bridge(active="active", enabled="enabled")
        r = fx.run_script(MIG_STOP)
        check("CONTROL_NOT_SAFE 拒绝", r.returncode != 0 and "MIGRATION_STOP_LEGACY=REJECTED" in r.stdout and "CONTROL_NOT_SAFE" in r.stdout, r.stdout[-200:])
        check("Control 不安全时 bridge 未被 stop", "disable --now firebot-bridge" not in fx.syslog.read_text())
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_start_failure_auto_stop_and_disable() -> None:
    fx = Fixture()
    try:
        # bridge.env 满足前置门；status.json 由 fake systemctl 写 mqtt_connected=false → 验收失败
        (fx.etc / "bridge.env").write_text(
            "FIREBOT_VEHICLE_ID=firebot-vehicle-02\n"
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com\n"
            "FIREBOT_MQTT_PORT=8883\n"
            "FIREBOT_CA_CERT=/etc/firebot/mqtt-ca.crt\n"
            "FIREBOT_SUPPORTED_COMMANDS=\n"
        )
        os.environ["FAKE_STATUS_CONNECTED"] = "false"
        (fx.etc / "config").mkdir(exist_ok=True)
        (fx.etc / "certs").mkdir(exist_ok=True)
        fx.ca = fx.etc / "certs" / "mqtt-ca.crt"
        (fx.etc / "config" / "bridge.env").write_text(
            "FIREBOT_VEHICLE_ID=firebot-vehicle-02\n"
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com\n"
            "FIREBOT_MQTT_PORT=8883\n"
            f"FIREBOT_CA_CERT={fx.ca}\n"
            "FIREBOT_SUPPORTED_COMMANDS=\n"
        )
        _setup_current_release(fx)
        r = fx.run_script(START)
        check("READONLY_START_FAILURE_AUTO_STOP（FAIL + 自动退回）", r.returncode != 0 and "READONLY_BRIDGE_START=FAIL" in r.stdout, r.stdout[-300:])
        check("FAIL_CLOSED_EDGE_STOP=PASS", "FAIL_CLOSED_EDGE_STOP=PASS" in r.stdout)
        check("BRIDGE_ACTIVE=NO 输出", "BRIDGE_ACTIVE=NO" in r.stdout)
        check("EDGE_ENABLED_STATE 输出", "EDGE_ENABLED_STATE=" in r.stdout)
        check("CONTROL_ACTIVE=NO 输出", "CONTROL_ACTIVE=NO" in r.stdout)
        log = fx.syslog.read_text()
        check("READONLY_START_FAILURE_AUTO_DISABLE（disable --now 被调用）", "disable --now firebot-vehicle-edge" in log)
        check("失败路径不触碰 control", not any(c in log for c in ("enable firebot-control", "start firebot-control", "stop firebot-control")), log)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_start_success_no_auto_stop() -> None:
    fx = Fixture()
    try:
        (fx.etc / "bridge.env").write_text(
            "FIREBOT_VEHICLE_ID=firebot-vehicle-02\n"
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com\n"
            "FIREBOT_MQTT_PORT=8883\n"
            "FIREBOT_CA_CERT=/etc/firebot/mqtt-ca.crt\n"
            "FIREBOT_SUPPORTED_COMMANDS=\n"
        )
        os.environ["FAKE_STATUS_CONNECTED"] = "true"
        (fx.etc / "config").mkdir(exist_ok=True)
        (fx.etc / "certs").mkdir(exist_ok=True)
        fx.ca = fx.etc / "certs" / "mqtt-ca.crt"
        (fx.etc / "config" / "bridge.env").write_text(
            "FIREBOT_VEHICLE_ID=firebot-vehicle-02\n"
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com\n"
            "FIREBOT_MQTT_PORT=8883\n"
            f"FIREBOT_CA_CERT={fx.ca}\n"
            "FIREBOT_SUPPORTED_COMMANDS=\n"
        )
        _setup_current_release(fx)
        r = fx.run_script(START)
        check("READONLY_START 成功路径（mqtt_connected=true）", r.returncode == 0 and "READONLY_BRIDGE_START=PASS" in r.stdout, r.stdout[-300:])
        log = fx.syslog.read_text()
        check("成功路径不自动 disable（无 disable 调用）", "disable --now firebot-bridge" not in log)
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_enrolled_exact_sha_required() -> None:
    fx = Fixture()
    try:
        fx._write_enrolled_files()  # noqa: SLF001
        # EXPECTED_SHA=sha1（存在于 repo 但 HEAD=sha2）→ EXACT_SHA_MATCH=NO → FAIL
        r = fx.run_script(
            fx.repo / "integration" / "ros1" / "readonly-preflight.sh",
            "--phase", "enrolled", "--device-id", "firebot-vehicle-02", "--expected-sha", fx.sha1,
        )
        check("ENROLLED_EXACT_SHA_MATCH_REQUIRED（HEAD≠SHA → FAIL）", r.returncode != 0 and "EXACT_SHA_MATCH=NO" in r.stdout, r.stdout[-300:])
        check("enrolled 输出 READONLY_ENROLLED_PREFLIGHT=FAIL", "READONLY_ENROLLED_PREFLIGHT=FAIL" in r.stdout)
        # EXPECTED_SHA=HEAD → PASS
        r2 = fx.run_script(
            fx.repo / "integration" / "ros1" / "readonly-preflight.sh",
            "--phase", "enrolled", "--device-id", "firebot-vehicle-02", "--expected-sha", fx.sha2,
        )
        check("ENROLLED_EXACT_SHA_MATCH_REQUIRED（HEAD==SHA → EXACT_SHA_MATCH=YES）", r2.returncode == 0 and "EXACT_SHA_MATCH=YES" in r2.stdout, r2.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_bootstrap_old_sha_allowed() -> None:
    fx = Fixture()
    try:
        fx._write_bootstrap_files()  # noqa: SLF001
        # bootstrap 阶段 EXPECTED_SHA=sha1（旧 release，HEAD=sha2）→ 允许 PASS
        r = fx.run_script(
            fx.repo / "integration" / "ros1" / "readonly-preflight.sh",
            "--phase", "bootstrap", "--device-id", "firebot-vehicle-02", "--expected-sha", fx.sha1,
        )
        check("BOOTSTRAP_OLD_SHA_ALLOWED（旧 SHA 不强制 HEAD 匹配）", r.returncode == 0 and "READONLY_BOOTSTRAP_PREFLIGHT=PASS" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def _add_env_fixture_methods(cls: type) -> None:
    """为 Fixture 注入 enrolled/bootstrap 文件构造（避免方法体重复）。"""
    def write_bootstrap_files(self) -> None:
        (self.etc / "config").mkdir(exist_ok=True)
        (self.etc / "certs").mkdir(exist_ok=True)
        (self.etc / "config" / "bootstrap.env").write_text(
            "FIREBOT_SERVER_URL=https://firebot.youdaozd.com\n"
        )
        (self.etc / "certs" / "mqtt-ca.crt").write_text("fixture\n")

    def write_enrolled_files(self) -> None:
        self._write_bootstrap_files()  # noqa: SLF001
        ca = self.etc / "certs" / "mqtt-ca.crt"
        (self.etc / "config" / "identity.env").write_text(
            "DEVICE_ID=firebot-vehicle-02\nPROFILE_ID=firebot_ros1_standard_v1\n"
        )
        (self.etc / "config" / "bridge.env").write_text(
            "FIREBOT_VEHICLE_ID=firebot-vehicle-02\n"
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com\n"
            "FIREBOT_MQTT_PORT=8883\n"
            f"FIREBOT_CA_CERT={ca}\n"
            "FIREBOT_SUPPORTED_COMMANDS=\n"
        )
        (self.etc / "secrets").mkdir(exist_ok=True)
        (self.etc / "secrets" / "mqtt.env").write_text("FIREBOT_MQTT_PASSWORD=x\n")
        (self.etc / "secrets" / "mqtt.env").chmod(0o600)
        (self.etc / "config" / "assignment.env").write_text("SUPPORTED_COMMANDS=\n")

    cls._write_bootstrap_files = write_bootstrap_files
    cls._write_enrolled_files = write_enrolled_files


_add_env_fixture_methods(Fixture)

if __name__ == "__main__":
    test_enroll_active_bridge_rejected_and_token_not_consumed()
    test_enroll_inactive_bridge_continues()
    test_install_active_bridge_rejected()
    test_migration_stop_legacy_bridge_only()
    test_migration_stop_legacy_control_not_safe_rejected()
    test_readonly_start_failure_auto_stop_and_disable()
    test_readonly_start_success_no_auto_stop()
    test_enrolled_exact_sha_required()
    test_bootstrap_old_sha_allowed()
    print(f"\nCUTOVER_GATE_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
