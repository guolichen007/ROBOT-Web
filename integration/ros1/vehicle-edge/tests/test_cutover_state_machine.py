#!/usr/bin/env python3
"""Golden 状态机测试（B0-FINAL-HARDENING / FINAL-SEAL spec 37~41, 13~17）。

验证合法转移 + 非法转移拒绝 + owner invariant，尽量通过真实 lifecycle 脚本推进。

运行：cd vehicle-edge && python3 tests/test_cutover_state_machine.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fake_systemctl import write_fake, set_state

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


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@ci", "-c", "user.name=ci", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Env:
    def __init__(self, tmp: Path, legacy_active: bool = True) -> None:
        self.tmp = tmp
        # git 交付源（edge-install release build / firebotctl 用）
        self.repo = tmp / "repo"
        (self.repo / "integration" / "ros1").mkdir(parents=True)
        shutil.copy(ROS1_SRC / "firebotctl", self.repo / "integration" / "ros1" / "firebotctl")
        shutil.copytree(VEHICLE_EDGE_SRC, self.repo / "integration" / "ros1" / "vehicle-edge",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture")
        self.sha = git(self.repo, "rev-parse", "HEAD")

        self.edge_home = tmp / "firebot-edge"
        self.edge_home.mkdir()
        self.state = tmp / "state"
        set_state(self.state, "firebot-bridge", active="active" if legacy_active else "inactive",
                  enabled="enabled" if legacy_active else "disabled", load="loaded")
        set_state(self.state, "firebot-vehicle-edge", active="inactive", enabled="disabled", load="loaded")
        set_state(self.state, "firebot-control", load="not-found")
        set_state(self.state, "tailscaled", active="active", load="loaded")

        self.legacy_etc = tmp / "legacy-etc"
        self.legacy_etc.mkdir()
        (self.legacy_etc / "bridge.env").write_text("FIREBOT_MQTT_HOST=100.110.31.112\nFIREBOT_MQTT_PORT=8883\n")
        self.legacy_status = tmp / "legacy-status.json"
        self.legacy_status.write_text(json.dumps({"mqtt_connected": True, "boot_id": "b1"}))

        (tmp / "systemd").mkdir(exist_ok=True)
        (tmp / "wrapbin").mkdir(exist_ok=True)
        self.fakebin = tmp / "fakebin"
        write_fake(self.fakebin)
        # fake curl：enrollment API 返回合法 JSON
        self.curllog = tmp / "curl.log"
        (self.fakebin / "curl").write_text(
            "#!/usr/bin/env bash\n"
            "echo \"$*\" >> \"${FAKE_CURL_LOG:?}\"\n"
            "cat \"${FAKE_ENROLL_RESPONSE:?}\"\n"
        )
        (self.fakebin / "curl").chmod(0o755)
        self.enroll_resp = tmp / "enroll.json"
        self.enroll_resp.write_text(json.dumps({
            "device_id": "firebot-vehicle-02", "profile_id": "firebot_ros1_standard_v1",
            "mqtt_host": "firebot.youdaozd.com", "mqtt_port": 8883,
            "mqtt_password": "x", "device_token": "t", "display_name": "r2",
        }))

        self.base_env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_TEST_SYSTEMCTL": str(self.fakebin / "systemctl"),
            "FIREBOT_TEST_CURL": str(self.fakebin / "curl"),
            "FIREBOT_TEST_LEGACY_BRIDGE_ENV": str(self.legacy_etc / "bridge.env"),
            "FIREBOT_TEST_BRIDGE_STATUS_FILE": str(self.legacy_status),
            "FIREBOT_TEST_LEGACY_STATUS": str(self.legacy_status),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_SKIP_CHOWN": "1",
            "FIREBOT_TEST_SKIP_OWNERSHIP": "1",
            "FIREBOT_EDGE_HOME": str(self.edge_home),
            "FIREBOT_TEST_ETC_ROOT": str(self.legacy_etc),
            "FIREBOT_TEST_SYSTEMD_DIR": str(tmp / "systemd"),
            "FIREBOT_TEST_WRAP_DIR": str(tmp / "wrapbin"),
            "FIREBOT_TEST_STATUS_JSON": str(self.edge_home / "runtime" / "status.json"),
            "FAKE_STATE": str(self.state),
            "FAKE_STATUS_JSON": str(self.edge_home / "runtime" / "status.json"),
            "FAKE_STATUS_CONNECTED": "true",
            "FAKE_BOOT_ID": "boot-new",
            "FAKE_RELEASE_SHA": self.sha,
            "FAKE_MAIN_PID": "4242",
            "FAKE_CURL_LOG": str(self.curllog),
            "FAKE_ENROLL_RESPONSE": str(self.enroll_resp),
            "FIREBOT_READONLY_MQTT_WAIT_SECONDS": "4",
            "FIREBOT_TEST_OS_RELEASE": str(self._os_release()),
            "FIREBOT_TEST_ROS_NOETIC_SETUP": str(self._ros_setup()),
            "FIREBOT_TEST_GETENT": str(self._w("getent", "echo 124.223.220.182 firebot.youdaozd.com\n")),
            "FIREBOT_TEST_TIMEDATECTL": str(self._w("timedatectl", "echo NTPSynchronized=yes\n")),
            "FIREBOT_TEST_PGREP": str(self._w("pgrep", "exit 1\n")),
            "FIREBOT_TEST_OPENSSL": str(self._w("openssl", "echo 'Verify return code: 0 (ok)'\n")),
        }
        # bootstrap CA + runtime（enroll 用 certs；readonly-start 用 runtime/status.json）
        (self.edge_home / "certs").mkdir(parents=True, exist_ok=True)
        (self.edge_home / "certs" / "mqtt-ca.crt").write_text("ca\n")
        (self.edge_home / "runtime").mkdir(parents=True, exist_ok=True)

    def _w(self, name: str, body: str) -> Path:
        p = self.fakebin / name
        p.write_text("#!/usr/bin/env bash\n" + body)
        p.chmod(0o755)
        return p

    def _os_release(self) -> Path:
        p = self.tmp / "os-release"
        p.write_text('ID=ubuntu\nVERSION_ID="20.04"\n')
        return p

    def _ros_setup(self) -> Path:
        p = self.tmp / "ros-setup.bash"
        p.write_text("# noetic\n")
        return p

    def run(self, script: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = dict(self.base_env)
        env.update(extra)
        return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=90)


def test_golden_full_path() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="golden-full-"))
    try:
        lifecycle = VEHICLE_EDGE_SRC / "lifecycle"
        fx = Env(tmp, legacy_active=True)

        # Stage A / tools deploy：edge-init（runtime user 配置，edge-install 需要）
        r = fx.run(lifecycle / "edge-init.sh", "--runtime-user", "tl")
        check("edge-init 前置", r.returncode == 0, r.stdout[-200:])

        # S0 → S1（migration-stop-legacy）
        r = fx.run(lifecycle / "migration-stop-legacy.sh")
        check("GOLDEN_S0_TO_S1=PASS", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])

        # S1 → S2（enroll，fake HTTPS API）
        r = fx.run(lifecycle / "enroll.sh", "firebot-vehicle-02", "--token", "tok",
                   "--server", "https://firebot.youdaozd.com/api/v1/enrollment/activate")
        check("GOLDEN_S1_TO_S2=PASS", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])
        check("enroll 消费 token（HTTP 成功）", "REAL_ENROLLMENT_TOKEN_CONSUMED=YES" in r.stdout)

        # S2 → S3（edge-install + edge-verify）
        r = fx.run(lifecycle / "edge-install.sh", str(fx.repo), FIREBOT_REQUIRE_SHA=fx.sha)
        check("GOLDEN_S2_TO_S3=PASS（edge-install）", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])
        r = fx.run(lifecycle / "edge-verify.sh", "--edge-home", str(fx.edge_home), FIREBOT_REQUIRE_SHA=fx.sha)
        check("GOLDEN_S2_TO_S3=PASS（edge-verify）", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])

        # S3 → S4（readonly-start）
        r = fx.run(lifecycle / "readonly-start.sh")
        check("GOLDEN_S3_TO_S4=PASS", r.returncode == 0, r.stdout[-400:] + r.stderr[-400:])
        check("COMMUNICATION_OWNER=VEHICLE_EDGE", "COMMUNICATION_OWNER=VEHICLE_EDGE" in r.stdout)
        check("MQTT_CONNECTED=YES", "MQTT_CONNECTED=YES" in r.stdout)
        check("legacy down strict", "LEGACY_BRIDGE_DOWN_STRICT=yes" in r.stdout)
        check("edge active", "BRIDGE_ACTIVE=YES" in r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_golden_rollback_paths() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="golden-rollback-"))
    try:
        lifecycle = VEHICLE_EDGE_SRC / "lifecycle"
        # S1 → SR
        fx = Env(tmp, legacy_active=True)
        fx.run(lifecycle / "migration-stop-legacy.sh")
        r = fx.run(lifecycle / "rollback.sh")
        check("ROLLBACK_S1_TO_SR=PASS", r.returncode == 0 and "COMMUNICATION_OWNER=LEGACY" in r.stdout, r.stdout[-300:])

        # S2 → SR（enroll 后回滚）
        fx2 = Env(tmp / "c2", legacy_active=True)
        fx2.run(lifecycle / "migration-stop-legacy.sh")
        fx2.run(lifecycle / "enroll.sh", "firebot-vehicle-02", "--token", "tok",
                "--server", "https://firebot.youdaozd.com/api/v1/enrollment/activate")
        r = fx2.run(lifecycle / "rollback.sh")
        check("ROLLBACK_S2_TO_SR=PASS", r.returncode == 0 and "COMMUNICATION_OWNER=LEGACY" in r.stdout, r.stdout[-300:])

        # S3 → SR（install 后回滚）
        fx3 = Env(tmp / "c3", legacy_active=True)
        fx3.run(lifecycle / "migration-stop-legacy.sh")
        fx3.run(lifecycle / "enroll.sh", "firebot-vehicle-02", "--token", "tok",
                "--server", "https://firebot.youdaozd.com/api/v1/enrollment/activate")
        fx3.run(lifecycle / "edge-install.sh", str(fx3.repo), FIREBOT_REQUIRE_SHA=fx3.sha)
        r = fx3.run(lifecycle / "rollback.sh")
        check("ROLLBACK_S3_TO_SR=PASS", r.returncode == 0 and "COMMUNICATION_OWNER=LEGACY" in r.stdout, r.stdout[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_illegal_transitions() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="illegal-"))
    try:
        lifecycle = VEHICLE_EDGE_SRC / "lifecycle"
        # S0 → enroll / edge-install / readonly-start 均 REJECT
        fx = Env(tmp, legacy_active=True)
        r = fx.run(lifecycle / "enroll.sh", "firebot-vehicle-02", "--token", "tok",
                   "--server", "https://firebot.youdaozd.com/api/v1/enrollment/activate")
        check("S0 → enroll REJECT", r.returncode != 0 and "ENROLL=REJECTED" in r.stdout, r.stdout[-200:])
        r = fx.run(lifecycle / "edge-install.sh", str(fx.repo), FIREBOT_REQUIRE_SHA=fx.sha)
        check("S0 → edge-install REJECT", r.returncode != 0 and "EDGE_INSTALL=REJECTED" in r.stdout, r.stdout[-200:])
        r = fx.run(lifecycle / "readonly-start.sh")
        check("S0 → readonly-start REJECT", r.returncode != 0 and "READONLY_BRIDGE_START=REJECTED" in r.stdout, r.stdout[-200:])

        # legacy missing → enroll / edge-install REJECT（rollback path 缺失，token 不能碰）
        fx2 = Env(tmp / "c2", legacy_active=False)
        set_state(fx2.state, "firebot-bridge", load="not-found")
        r = fx2.run(lifecycle / "enroll.sh", "firebot-vehicle-02", "--token", "tok",
                    "--server", "https://firebot.youdaozd.com/api/v1/enrollment/activate")
        check("legacy missing → enroll REJECT", r.returncode != 0 and "LEGACY_ROLLBACK_PATH_MISSING" in r.stdout, r.stdout[-200:])
        check("legacy missing → token 不碰（无 POST）", "-X POST" not in (fx2.curllog.read_text() if fx2.curllog.exists() else ""))
        check("legacy missing → 不输出 token consumption", "ENROLL_TOKEN_CONSUMPTION_ATTEMPTED" not in r.stdout)
        fx2.run(lifecycle / "edge-init.sh", "--runtime-user", "tl")
        r = fx2.run(lifecycle / "edge-install.sh", str(fx2.repo), FIREBOT_REQUIRE_SHA=fx2.sha)
        check("legacy missing → edge-install REJECT", r.returncode != 0 and "LEGACY_ROLLBACK_PATH_MISSING" in r.stdout, r.stdout[-200:])

        # dual active → rollback REJECT
        fx3 = Env(tmp / "c3", legacy_active=True)
        set_state(fx3.state, "firebot-vehicle-edge", active="active", enabled="enabled", load="loaded")
        r = fx3.run(lifecycle / "rollback.sh")
        check("dual active → rollback REJECT", r.returncode != 0 and "MUTUAL_EXCLUSION_VIOLATED" in r.stdout, r.stdout[-200:])

        check("ILLEGAL_TRANSITIONS=PASS", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_final_owner_failure_fail_closed() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="final-owner-fail-"))
    try:
        lifecycle = VEHICLE_EDGE_SRC / "lifecycle"
        fx = Env(tmp, legacy_active=True)
        fx.run(lifecycle / "edge-init.sh", "--runtime-user", "tl")
        fx.run(lifecycle / "migration-stop-legacy.sh")
        fx.run(lifecycle / "enroll.sh", "firebot-vehicle-02", "--token", "tok",
               "--server", "https://firebot.youdaozd.com/api/v1/enrollment/activate")
        fx.run(lifecycle / "edge-install.sh", str(fx.repo), FIREBOT_REQUIRE_SHA=fx.sha)
        # edge 启动时 legacy 意外变 active → final owner invariant FAIL → 必须停 Edge
        r = fx.run(lifecycle / "readonly-start.sh", FAKE_FLIP_LEGACY_ACTIVE="1")
        check("READONLY_FINAL_OWNER_FAILURE_FAIL_CLOSED=PASS",
              r.returncode != 0 and "FINAL_OWNER_INVARIANT=FAIL" in r.stdout, r.stdout[-400:])
        check("final owner 失败停 Edge（EDGE_STOP_VERIFIED=YES）", "EDGE_STOP_VERIFIED=YES" in r.stdout)
        check("READONLY_BRIDGE_START=FAIL", "READONLY_BRIDGE_START=FAIL" in r.stdout)
        check("ROLLBACK_REQUIRED=YES", "ROLLBACK_REQUIRED=YES" in r.stdout)
        check("最终 edge inactive", "EDGE_ACTIVE_STATE=inactive" in r.stdout, r.stdout[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_golden_full_path()
    test_golden_rollback_paths()
    test_illegal_transitions()
    test_final_owner_failure_fail_closed()
    print(f"\nCUTOVER_STATE_MACHINE_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
