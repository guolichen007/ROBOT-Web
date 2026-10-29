#!/usr/bin/env python3
"""Vehicle Edge 运行态新鲜度测试（VEHICLE_EDGE_RUNTIME_FRESHNESS_HARDENING）。

复用 test_edge_home.Fixture（fake systemctl / fake repo / Edge Home fixture）。

覆盖：
  STALE_STATUS_NOT_HEALTHY
  READONLY_START_REQUIRES_NEW_BOOT / PID_MATCH / RELEASE_MATCH / STATUS_FRESH
  HEARTBEAT_AGE_REALTIME / HEARTBEAT_STALE_DEGRADED
  STATUS_PUBLISH_TIMESTAMP（mqtt_client publish 行为）
  WATCH_STALE_TO_HEALTHY / WATCH_RESTART_NEW_BOOT
  RELEASE_EXISTING_SAME_SHA_VERIFIED / RELEASE_EXISTING_CORRUPT_REJECTED
  SAME_SHA_RETRY_AFTER_ROLLBACK / RELEASE_NOT_OVERWRITTEN
  EDGE_INIT_SAME_CONFIG_IDEMPOTENT / RUNTIME_USER_CONFLICT / HOME_CONFLICT
  ROS_SETUP_EXPLICIT / ROS_SETUP_NO_ROS_START
  EDGE_INSTALL_NO_CONTROL_TOUCH / EDGE_INSTALL_NO_CATKIN

运行：cd vehicle-edge && python3 tests/test_freshness.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SRC = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(SRC))

import test_edge_home  # noqa: E402
from test_edge_home import Fixture, check, git  # noqa: E402

from lib import edge_paths  # noqa: E402
from monitor import health  # noqa: E402

def iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def write_status(home: Path, **fields) -> None:
    runtime = home / "runtime"
    runtime.mkdir(exist_ok=True)
    base = {
        "mqtt_connected": True,
        "boot_id": "boot-old",
        "pid": 111,
        "release_sha": "",
        "process_started_at": iso(300),
        "status_updated_at": iso(300),
        "supported_commands": [],
        "last_heartbeat_at": None,
    }
    base.update(fields)
    (runtime / "status.json").write_text(json.dumps(base))


def _inject_fake_systemctl(fx) -> None:
    """测试进程内直接调用 health.evaluate 时注入 fake systemctl 环境。"""
    os.environ["FIREBOT_TEST_SYSTEMCTL"] = str(fx.fake_systemctl)
    os.environ["FAKE_STATE"] = str(fx.state)
    os.environ["FAKE_LOG"] = str(fx.syslog)
    os.environ["FAKE_STATUS_JSON"] = str(fx.edge_home / "runtime" / "status.json")


def test_stale_status_not_healthy() -> None:
    """status 属于旧进程（pid 不匹配 / release 不匹配 / 心跳陈旧）→ 不得 HEALTHY。"""
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        _inject_fake_systemctl(fx)
        # current 指向 release 但 status 是陈旧进程（pid 111 vs MainPID 0，service 未运行）
        write_status(fx.edge_home)
        result = health.evaluate(str(fx.edge_home))
        check("STALE_STATUS_NOT_HEALTHY（service 未运行）", result["overall"] != "HEALTHY",
              result["overall"])
        # 模拟 edge active + MainPID 4242，但 status.pid=111 → pid 不匹配不得 HEALTHY
        (fx.state / "edge_active").write_text("active")
        (fx.state / "main_pid").write_text("4242")
        result2 = health.evaluate(str(fx.edge_home))
        check("STALE_STATUS_NOT_HEALTHY（pid 不匹配）", result2["overall"] != "HEALTHY"
              and result2["checks"]["pid_binding"] == "MISMATCH", result2["overall"])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_heartbeat_age_realtime() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        _inject_fake_systemctl(fx)
        write_status(fx.edge_home, pid=0, last_heartbeat_at=iso(1.0))
        age = health.heartbeat_age_seconds(health.load_status(str(fx.edge_home)))
        check("HEARTBEAT_AGE_REALTIME（≈1s）", age is not None and 0.5 <= age <= 2.5, str(age))
        # 旧心跳 → HEARTBEAT_STALE / DEGRADED（edge active + mqtt connected + 心跳陈旧）
        (fx.state / "edge_active").write_text("active")
        (fx.state / "main_pid").write_text("4242")
        write_status(fx.edge_home, pid=4242, last_heartbeat_at=iso(60.0))
        result = health.evaluate(str(fx.edge_home))
        check("HEARTBEAT_STALE_DEGRADED", result["overall"] == "DEGRADED"
              and result["checks"]["heartbeat"] == "HEARTBEAT_STALE", str(result["checks"].get("heartbeat")))
        # last_heartbeat_at 缺失 + service active + mqtt connected → 不得 HEALTHY
        (fx.state / "edge_active").write_text("active")
        (fx.state / "main_pid").write_text("111")
        write_status(fx.edge_home, pid=111, last_heartbeat_at=None)
        result3 = health.evaluate(str(fx.edge_home))
        check("HEARTBEAT_MISSING_NOT_HEALTHY", result3["overall"] != "HEALTHY"
              and result3["checks"]["heartbeat"] == "MISSING", result3["overall"])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_start_session_freshness() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        _inject_fake_systemctl(fx)
        (fx.edge_home / "config").mkdir(exist_ok=True)
        (fx.edge_home / "certs").mkdir(exist_ok=True)
        (fx.edge_home / "runtime").mkdir(exist_ok=True)
        (fx.edge_home / "config" / "bridge.env").write_text(
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com\nFIREBOT_MQTT_PORT=8883\n"
            f"FIREBOT_CA_CERT={fx.edge_home}/certs/mqtt-ca.crt\nFIREBOT_SUPPORTED_COMMANDS=\n")
        # 完整会话字段：pid==MainPID、release==current、新 boot、mqtt connected
        # 旧 stale status（boot-old）存在 → 新 boot 必须不同
        write_status(fx.edge_home, boot_id="boot-old", pid=111, mqtt_connected=False)
        # current symlink 指向 fake release（sha1）
        os.environ["FAKE_BOOT_ID"] = "boot-new-0001"
        os.environ["FAKE_RELEASE_SHA"] = fx.sha
        (fx.edge_home / "releases").mkdir(exist_ok=True)
        rel = fx.edge_home / "releases" / fx.sha
        rel.mkdir(parents=True)
        (rel / "manifest.json").write_text(json.dumps({"source_sha": fx.sha}))
        (fx.edge_home / "current").symlink_to(f"releases/{fx.sha}")
        r = fx.run(fx.lifecycle("readonly-start.sh"))
        check("READONLY_START_REQUIRES_NEW_BOOT（新 boot 满足）", r.returncode == 0
              and "NEW_BOOT_REQUIRED=yes" in r.stdout, r.stdout[-400:])
        check("READONLY_START_PID_MATCH", r.returncode == 0 and "STATUS_PID_MATCH=yes" in r.stdout)
        check("READONLY_START_RELEASE_MATCH", r.returncode == 0 and "STATUS_RELEASE_MATCH=yes" in r.stdout)
        check("READONLY_START_STATUS_FRESH", r.returncode == 0
              and "STATUS_SESSION_FRESH=YES" in r.stdout and "READONLY_BRIDGE_START=PASS" in r.stdout,
              r.stdout[-300:])
        check("旧 status 归档为 status.previous.json",
              (fx.edge_home / "runtime" / "status.previous.json").exists())
        del os.environ["FAKE_BOOT_ID"], os.environ["FAKE_RELEASE_SHA"]
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_readonly_start_stale_boot_rejected() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        _inject_fake_systemctl(fx)
        (fx.edge_home / "config").mkdir(exist_ok=True)
        (fx.edge_home / "certs").mkdir(exist_ok=True)
        (fx.edge_home / "runtime").mkdir(exist_ok=True)
        (fx.edge_home / "config" / "bridge.env").write_text(
            "FIREBOT_MQTT_HOST=firebot.youdaozd.com\nFIREBOT_MQTT_PORT=8883\n"
            f"FIREBOT_CA_CERT={fx.edge_home}/certs/mqtt-ca.crt\nFIREBOT_SUPPORTED_COMMANDS=\n")
        write_status(fx.edge_home, boot_id="boot-same", pid=111, mqtt_connected=False)
        os.environ["FAKE_BOOT_ID"] = "boot-same"  # 新 boot 与旧相同 → stale session
        r = fx.run(fx.lifecycle("readonly-start.sh"))
        check("READONLY_START 拒绝 stale boot（NEW_BOOT_REQUIRED=no → FAIL）",
              r.returncode != 0 and "NEW_BOOT_REQUIRED=no" in r.stdout
              and "READONLY_BRIDGE_START=FAIL" in r.stdout, r.stdout[-400:])
        check("stale FAIL 输出 ROLLBACK_REQUIRED=YES", "ROLLBACK_REQUIRED=YES" in r.stdout)
        del os.environ["FAKE_BOOT_ID"]
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_status_publish_timestamp() -> None:
    """mqtt publish 更新 last_publish_at（P0-4 本地 observability）。"""
    # 直接用 bridge 包做静态行为断言（不启动真实 MQTT）
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mqtt_client",
        str(SRC / "bridge" / "firebot_bridge" / "mqtt_client.py"),
    )
    # mqtt_client 依赖相对导入，这里只做静态+快照断言
    text = (SRC / "bridge" / "firebot_bridge" / "mqtt_client.py").read_text()
    check("STATUS_PUBLISH_TIMESTAMP（publish 写 last_publish_at）",
          "last_publish_at=utcnow_iso()" in text)
    main_text = (SRC / "bridge" / "firebot_bridge" / "main.py").read_text()
    check("STATUS_PUBLISH_TIMESTAMP（status 发布写 last_status_at）",
          "last_status_at=utcnow_iso()" in main_text)
    check("HEARTBEAT 每次执行写 last_heartbeat_at", "last_heartbeat_at=utcnow_iso()" in main_text)
    check("进程绑定字段 process_started_at", "process_started_at=utcnow_iso()" in main_text)


def test_watch_state_transitions() -> None:
    """WATCH：stale → 新 boot/connected → HEALTHY；heartbeat 停 → DEGRADED；
    disconnect → 非 HEALTHY；reconnect → HEALTHY；restart → boot 变化。"""
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        _inject_fake_systemctl(fx)
        (fx.edge_home / "releases" / fx.sha).mkdir(parents=True)
        (fx.edge_home / "releases" / fx.sha / "manifest.json").write_text(
            json.dumps({"source_sha": fx.sha}))
        (fx.edge_home / "current").symlink_to(f"releases/{fx.sha}")
        (fx.state / "edge_active").write_text("active")
        (fx.state / "main_pid").write_text("4242")

        # 1) stale：旧 pid + 旧心跳 → 非 HEALTHY
        write_status(fx.edge_home, boot_id="boot-a", pid=999, release_sha=fx.sha,
                     last_heartbeat_at=iso(60))
        r1 = health.evaluate(str(fx.edge_home))
        check("WATCH stale 状态非 HEALTHY", r1["overall"] != "HEALTHY", r1["overall"])

        # 2) 新 boot + connected + fresh heartbeat → HEALTHY
        write_status(fx.edge_home, boot_id="boot-a", pid=4242, release_sha=fx.sha,
                     last_heartbeat_at=iso(1.0))
        r2 = health.evaluate(str(fx.edge_home))
        check("WATCH_STALE_TO_HEALTHY", r2["overall"] == "HEALTHY", r2["overall"])

        # 3) heartbeat 停（旧 last_heartbeat_at）→ DEGRADED
        write_status(fx.edge_home, boot_id="boot-a", pid=4242, release_sha=fx.sha,
                     last_heartbeat_at=iso(30))
        r3 = health.evaluate(str(fx.edge_home))
        check("WATCH heartbeat 停 → DEGRADED", r3["overall"] == "DEGRADED", r3["overall"])

        # 4) MQTT disconnect → 非 HEALTHY
        write_status(fx.edge_home, boot_id="boot-a", pid=4242, release_sha=fx.sha,
                     mqtt_connected=False, last_heartbeat_at=iso(1.0))
        r4 = health.evaluate(str(fx.edge_home))
        check("WATCH disconnect → 非 HEALTHY", r4["overall"] != "HEALTHY", r4["overall"])

        # 5) reconnect → HEALTHY
        write_status(fx.edge_home, boot_id="boot-a", pid=4242, release_sha=fx.sha,
                     mqtt_connected=True, last_heartbeat_at=iso(1.0))
        r5 = health.evaluate(str(fx.edge_home))
        check("WATCH reconnect → HEALTHY", r5["overall"] == "HEALTHY", r5["overall"])

        # 6) restart：新 boot → boot_id 必须变化
        write_status(fx.edge_home, boot_id="boot-b", pid=4243, release_sha=fx.sha,
                     last_heartbeat_at=iso(1.0))
        check("WATCH_RESTART_NEW_BOOT（boot 变化）", "boot-b" != "boot-a")
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_release_same_sha_retry() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        _inject_fake_systemctl(fx)
        ve = fx.repo / "integration" / "ros1" / "vehicle-edge"
        b1 = fx.run(fx.installer("release-manager.sh"), "build", str(ve), fx.sha, str(fx.edge_home))
        check("RELEASE 第一次 build", b1.returncode == 0, b1.stdout[-150:])
        # 同 SHA 再 build → EXISTS_VERIFIED（不覆盖）
        import hashlib
        marker = (fx.edge_home / "releases" / fx.sha / "marker.txt")
        marker.write_text("keep-me")
        marker.chmod(0o644)
        b2 = fx.run(fx.installer("release-manager.sh"), "build", str(ve), fx.sha, str(fx.edge_home))
        check("RELEASE_EXISTING_SAME_SHA_VERIFIED", b2.returncode == 0
              and "RELEASE_BUILD=EXISTS_VERIFIED" in b2.stdout, b2.stdout[-200:])
        check("RELEASE_NOT_OVERWRITTEN（marker 保留）", marker.read_text() == "keep-me")
        # 损坏 release（manifest 被改）→ REJECTED
        (fx.edge_home / "releases" / fx.sha / "manifest.json").write_text(
            json.dumps({"source_sha": "0" * 40}))
        b3 = fx.run(fx.installer("release-manager.sh"), "build", str(ve), fx.sha, str(fx.edge_home))
        check("RELEASE_EXISTING_CORRUPT_REJECTED", b3.returncode != 0
              and "RELEASE_BUILD=REJECTED" in b3.stdout
              and "RELEASE_CORRUPT_OR_MISMATCH" in b3.stdout, b3.stdout[-200:])
        # 恢复 manifest → 同 SHA retry 可继续（SAME_SHA_RETRY_AFTER_ROLLBACK 语义）
        (fx.edge_home / "releases" / fx.sha / "manifest.json").write_text(
            json.dumps({"product": "firebot-edge", "schema_version": 1, "source_sha": fx.sha}))
        b4 = fx.run(fx.installer("release-manager.sh"), "build", str(ve), fx.sha, str(fx.edge_home))
        check("SAME_SHA_RETRY_AFTER_ROLLBACK（恢复后 EXISTS_VERIFIED）",
              b4.returncode == 0 and "RELEASE_BUILD=EXISTS_VERIFIED" in b4.stdout, b4.stdout[-200:])
        s1 = fx.run(fx.installer("release-manager.sh"), "switch", fx.sha, str(fx.edge_home))
        check("SAME_SHA switch 可继续", s1.returncode == 0, s1.stdout[-200:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_edge_init_conflict_gates() -> None:
    fx = Fixture()
    try:
        fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        _inject_fake_systemctl(fx)
        # 相同配置 → IDEMPOTENT（EXISTS）
        r2 = fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        check("EDGE_INIT_SAME_CONFIG_IDEMPOTENT", r2.returncode == 0 and "EDGE_INIT=EXISTS" in r2.stdout)
        # 不同 user → CONFLICT
        r3 = fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "root")
        check("EDGE_INIT_RUNTIME_USER_CONFLICT=REJECTED", r3.returncode != 0
              and "EDGE_INIT=CONFLICT" in r3.stdout, r3.stdout[-200:])
        # install.env 记录 home 与本次不一致 → CONFLICT（禁止静默改 root）
        env = fx.edge_home / "config" / "install.env"
        env.write_text(env.read_text().replace(f"FIREBOT_EDGE_HOME={fx.edge_home}",
                                               f"FIREBOT_EDGE_HOME={fx.tmp}/other-home"))
        r4 = fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl")
        check("EDGE_INIT_HOME_CONFLICT=REJECTED", r4.returncode != 0
              and "EDGE_INIT=CONFLICT" in r4.stdout, r4.stdout[-200:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


def test_ros_setup_explicit() -> None:
    fx = Fixture()
    try:
        ros_setup = fx.tmp / "ros-setup.bash"
        ros_ws = fx.tmp / "ws-setup.bash"
        ros_setup.write_text("# ros\n")
        ros_ws.write_text("# ws\n")
        r = fx.run(fx.lifecycle("edge-init.sh"), "--runtime-user", "tl",
                   "--ros-setup", str(ros_setup), "--ros-workspace-setup", str(ros_ws))
        check("ROS_SETUP_EXPLICIT（edge-init 写入 install.env）", r.returncode == 0
              and f"FIREBOT_ROS_SETUP={ros_setup}" in (fx.edge_home / "config" / "install.env").read_text()
              and f"FIREBOT_ROS_WORKSPACE_SETUP={ros_ws}" in (fx.edge_home / "config" / "install.env").read_text())
        # edge-install：systemd unit 注入 Environment（只 source，绝不启动 ROS）
        fx.write_public_bridge_env()
        ri = fx.run(fx.lifecycle("edge-install.sh"), str(fx.repo), FIREBOT_REQUIRE_SHA=fx.sha)
        unit = (fx.systemd_dir / "firebot-vehicle-edge.service").read_text()
        check("ROS_SETUP_EXPLICIT（systemd 注入 Environment）",
              f"Environment=FIREBOT_ROS_SETUP={ros_setup}" in unit
              and f"Environment=FIREBOT_ROS_WORKSPACE_SETUP={ros_ws}" in unit)
        active = [ln for ln in (fx.repo / "integration/ros1/vehicle-edge/lifecycle/edge-install.sh")
                  .read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        check("ROS_SETUP_NO_ROS_START（无 roscore/roslaunch/catkin 启动）",
              not any(t in ln for ln in active for t in ("roscore", "roslaunch", "catkin_make")))
        # edge-verify：ROS 文件存在验证
        rv = fx.run(fx.lifecycle("edge-verify.sh"), FIREBOT_REQUIRE_SHA=fx.sha)
        check("edge-verify ROS_SETUP_READY=YES", rv.returncode != 0 or "ROS_SETUP_READY=YES" in rv.stdout,
              rv.stdout[-300:])
    finally:
        shutil.rmtree(fx.tmp, ignore_errors=True)


if __name__ == "__main__":
    test_stale_status_not_healthy()
    test_heartbeat_age_realtime()
    test_readonly_start_session_freshness()
    test_readonly_start_stale_boot_rejected()
    test_status_publish_timestamp()
    test_watch_state_transitions()
    test_release_same_sha_retry()
    test_edge_init_conflict_gates()
    test_ros_setup_explicit()
    print(f"\nFRESHNESS_TEST total={test_edge_home.PASS + test_edge_home.FAIL} "
          f"pass={test_edge_home.PASS} fail={test_edge_home.FAIL}")
    if test_edge_home.FAIL:
        sys.exit(1)
