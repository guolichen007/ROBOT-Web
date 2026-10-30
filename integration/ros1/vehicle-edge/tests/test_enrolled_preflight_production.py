#!/usr/bin/env python3
"""B6：生产 tools 模式 enrolled preflight 的 data release 完整性合同回归测试。

data/runtime release 无 checksums.sha256（与 tools release 严格分离）。
生产 enrolled preflight 必须：
  - tools release 仍严格 manifest + checksum
  - data release 用 current exact SHA + manifest exact SHA + 关键运行文件证明完整性

运行：cd vehicle-edge && python3 tests/test_enrolled_preflight_production.py
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


def run(args: list, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", *args], env=env, capture_output=True, text=True, timeout=90)


class Fixture:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="b6-prod-"))
        self.edge_home = self.tmp / "firebot-edge"
        (self.edge_home / "tools" / "releases").mkdir(parents=True)

        # 交付源：firebotctl + vehicle-edge（含 VERSION）
        self.src = self.tmp / "src"
        self.src.mkdir()
        shutil.copy2(ROS1_SRC / "firebotctl", self.src / "firebotctl")
        shutil.copytree(VEHICLE_EDGE_SRC, self.src / "vehicle-edge",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "docs"))
        self.sha = "a" * 40

        # 统一 fake systemctl + 网络/TLS/NTP 命令
        self.fakebin = self.tmp / "fakebin"
        write_fake(self.fakebin)
        self.state = self.tmp / "state"
        set_state(self.state, "firebot-bridge", load="loaded", active="inactive", enabled="disabled")
        set_state(self.state, "firebot-vehicle-edge", load="loaded", active="inactive", enabled="disabled")
        set_state(self.state, "firebot-control", load="not-found")
        set_state(self.state, "tailscaled", active="active", load="loaded")
        for name, body in (
            ("curl", "exit 0\n"),
            ("getent", "echo 124.223.220.182 firebot.youdaozd.com\n"),
            ("openssl", "cat >/dev/null\necho 'Verify return code: 0 (ok)'\n"),
            ("timedatectl", "echo NTPSynchronized=yes\n"),
            ("pgrep", "exit 1\n"),
        ):
            p = self.fakebin / name
            p.write_text("#!/usr/bin/env bash\n" + body)
            p.chmod(0o755)

        self.os_release = self.tmp / "os-release"
        self.os_release.write_text('ID=ubuntu\nVERSION_ID="20.04"\n')
        self.ros_setup = self.tmp / "ros-setup.bash"
        self.ros_setup.write_text("# noetic\n")

        # enrollment 数据（非 secret）
        cfg = self.edge_home / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "bootstrap.env").write_text("FIREBOT_SERVER_URL=https://firebot.youdaozd.com\n")
        (cfg / "identity.env").write_text("DEVICE_ID=firebot-vehicle-02\nPROFILE_ID=firebot_ros1_standard_v1\n")
        (cfg / "assignment.env").write_text("MQTT_HOST=firebot.youdaozd.com\nMQTT_PORT=8883\nSUPPORTED_COMMANDS=\n")
        (cfg / "bridge.env").write_text(
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com\nFIREBOT_MQTT_PORT=8883\n"
            f"FIREBOT_CA_CERT={self.edge_home}/certs/mqtt-ca.crt\nFIREBOT_SUPPORTED_COMMANDS=\n")
        sec = self.edge_home / "secrets"
        sec.mkdir(parents=True, exist_ok=True)
        (sec / "mqtt.env").write_text("FIREBOT_MQTT_PASSWORD=x\n")
        (sec / "mqtt.env").chmod(0o600)
        certs = self.edge_home / "certs"
        certs.mkdir(parents=True, exist_ok=True)
        (certs / "mqtt-ca.crt").write_text("fixture-ca\n")

        self.base_env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ.get('PATH', '')}",
            "FIREBOT_TEST_SYSTEMCTL": str(self.fakebin / "systemctl"),
            "FIREBOT_TEST_CURL": str(self.fakebin / "curl"),
            "FIREBOT_TEST_GETENT": str(self.fakebin / "getent"),
            "FIREBOT_TEST_OPENSSL": str(self.fakebin / "openssl"),
            "FIREBOT_TEST_TIMEDATECTL": str(self.fakebin / "timedatectl"),
            "FIREBOT_TEST_PGREP": str(self.fakebin / "pgrep"),
            "FIREBOT_TEST_OS_RELEASE": str(self.os_release),
            "FIREBOT_TEST_ROS_NOETIC_SETUP": str(self.ros_setup),
            "FAKE_STATE": str(self.state),
            "FAKE_STATUS_JSON": str(self.edge_home / "runtime" / "status.json"),
            "FIREBOT_TEST_SKIP_ROOT": "1",
            "FIREBOT_TEST_SKIP_CHOWN": "1",
            "FIREBOT_TEST_SKIP_OWNERSHIP": "1",
            "FIREBOT_EDGE_HOME": str(self.edge_home),
        }

    def build_releases(self) -> None:
        env = dict(self.base_env)
        env["FIREBOT_EDGE_HOME"] = str(self.edge_home)
        r = run([str(TOOLS_MANAGER), "build", str(self.src), self.sha, str(self.edge_home)], env)
        assert r.returncode == 0, r.stdout[-300:]
        r = run([str(TOOLS_MANAGER), "switch", self.sha, str(self.edge_home)], env)
        assert r.returncode == 0, r.stdout[-300:]
        # data release build + switch（从 tools bundle 的 vehicle-edge 组装）
        bundle = self.edge_home / "tools" / "releases" / self.sha / "vehicle-edge"
        rm = bundle / "install" / "release-manager.sh"
        (self.edge_home / "releases").mkdir(exist_ok=True)
        r = run([str(rm), "build", str(bundle), self.sha, str(self.edge_home)], env)
        assert r.returncode == 0, r.stdout[-300:]
        r = run([str(rm), "switch", self.sha, str(self.edge_home)], env)
        assert r.returncode == 0, r.stdout[-300:]

    def preflight(self, **extra: str) -> subprocess.CompletedProcess:
        env = dict(self.base_env)
        env["FIREBOT_EDGE_TOOLS_MODE"] = "1"
        env["FIREBOT_TOOLS_SOURCE_SHA"] = self.sha
        env["FIREBOT_EDGE_HOME"] = str(self.edge_home)
        env.update(extra)
        ctl = self.edge_home / "tools" / "current" / "firebotctl"
        return run([str(ctl), "vehicle", "readonly-preflight", "--phase", "enrolled",
                    "--device-id", "firebot-vehicle-02", "--expected-sha", self.sha], env)


def test_production_enrolled_preflight_without_data_checksums() -> None:
    fx = Fixture()
    try:
        fx.build_releases()
        check("DATA_RELEASE_CHECKSUM_REQUIRED=NO（current 无 checksums.sha256）",
              not (fx.edge_home / "current" / "checksums.sha256").exists())

        r = fx.preflight()
        check("PRODUCTION_ENROLLED_PREFLIGHT_NO_DATA_CHECKSUM=PASS",
              r.returncode == 0 and "READONLY_ENROLLED_PREFLIGHT=PASS" in r.stdout,
              r.stdout[-600:] + r.stderr[-600:])
        for needle in ("EXPECTED_SHA_MATCHES_TOOLS=yes", "TOOLS_MANIFEST_VALID=YES", "TOOLS_CHECKSUM_VALID=YES",
                       "LEGACY_ROLLBACK_READY=YES", "EDGE_INSTALLATION_READY=YES", "COMMUNICATION_OWNER=NONE",
                       "DATA_RELEASE_INTEGRITY=YES", "EXACT_SHA_MATCH=YES"):
            check(needle, needle in r.stdout, r.stdout[-600:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_negative_cases() -> None:
    # wrong current SHA
    tmp = Path(tempfile.mkdtemp(prefix="b6-neg-"))
    try:
        fx = Fixture()
        fx.build_releases()
        fx.sha_other = "b" * 40
        cur = fx.edge_home / "current"
        cur.unlink()
        cur.symlink_to(f"releases/{fx.sha_other}")
        r = fx.preflight()
        check("WRONG_CURRENT_SHA_REJECTED", r.returncode != 0 and "EXACT_SHA_MATCH=NO" in r.stdout, r.stdout[-400:])
        check("WRONG_CURRENT_SHA_DATA_INTEGRITY_NO", "DATA_RELEASE_INTEGRITY=NO" in r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # manifest SHA mismatch
    tmp = Path(tempfile.mkdtemp(prefix="b6-neg-"))
    try:
        fx = Fixture()
        fx.build_releases()
        mf = fx.edge_home / "current" / "manifest.json"
        mf.write_text(json.dumps({"product": "firebot-edge", "schema_version": 1, "source_sha": "c" * 40}))
        r = fx.preflight()
        check("WRONG_DATA_MANIFEST_SHA_REJECTED", r.returncode != 0 and "EXACT_SHA_MATCH=NO" in r.stdout, r.stdout[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # missing runtime file
    tmp = Path(tempfile.mkdtemp(prefix="b6-neg-"))
    try:
        fx = Fixture()
        fx.build_releases()
        (fx.edge_home / "current" / "monitor" / "health.py").unlink()
        r = fx.preflight()
        check("MISSING_DATA_RUNTIME_FILE_REJECTED", r.returncode != 0 and "EXACT_SHA_MATCH=NO" in r.stdout, r.stdout[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # corrupt tools checksum
    tmp = Path(tempfile.mkdtemp(prefix="b6-neg-"))
    try:
        fx = Fixture()
        fx.build_releases()
        # 篡改 tools release 中一个受 checksum 管理的非入口文件（不改 firebotctl 本身）
        (fx.edge_home / "tools" / "current" / "VERSION").write_text("tampered-version\n")
        r = fx.preflight()
        check("CORRUPT_TOOLS_CHECKSUM_REJECTED",
              r.returncode != 0 and "TOOLS_CHECKSUM_VALID=NO" in r.stdout, r.stdout[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_production_enrolled_preflight_without_data_checksums()
    test_negative_cases()
    print(f"\nENROLLED_PREFLIGHT_PRODUCTION_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
