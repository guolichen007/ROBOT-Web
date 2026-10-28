#!/usr/bin/env python3
"""monitor 模块单测（health 状态模型 / status / redact / edge_paths）。

运行：cd vehicle-edge && python3 tests/test_monitor.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from lib import edge_paths  # noqa: E402
from monitor import health, redact, status  # noqa: E402

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


def test_health_state_model() -> None:
    check("状态集完整", set(health.STATUS_EXIT) == {
        "HEALTHY", "DEGRADED", "OFFLINE", "UNSAFE", "CONFIG_ERROR", "INTERNAL_ERROR"})
    check("exit code 映射", health.STATUS_EXIT["HEALTHY"] == 0
          and health.STATUS_EXIT["DEGRADED"] == 10 and health.STATUS_EXIT["OFFLINE"] == 20
          and health.STATUS_EXIT["UNSAFE"] == 30 and health.STATUS_EXIT["CONFIG_ERROR"] == 40
          and health.STATUS_EXIT["INTERNAL_ERROR"] == 50)
    check("worse 取更严重", health.worse("HEALTHY", "DEGRADED") == "DEGRADED"
          and health.worse("UNSAFE", "OFFLINE") == "UNSAFE")


def test_health_evaluate_with_fixture() -> None:
    with tempfile.TemporaryDirectory(prefix="monitor-") as tmp:
        home = Path(tmp)
        (home / "config").mkdir()
        (home / "runtime").mkdir()
        (home / "config" / "install.env").write_text(
            "FIREBOT_EDGE_USER=tl\nFIREBOT_EDGE_HOME=%s\n" % home)
        (home / "runtime" / "status.json").write_text(json.dumps({
            "mqtt_connected": True, "supported_commands": [], "heartbeat_age_seconds": 1.0}))
        # 环境注入 Edge Home + fake systemctl 不可用环境（service_active 返回 False）
        os.environ["FIREBOT_EDGE_HOME"] = str(home)
        result = health.evaluate(str(home))
        check("配置缺失 runtime user → CONFIG_ERROR 语义（正常配置则非 CONFIG_ERROR）",
              result["overall"] in ("OFFLINE", "DEGRADED", "HEALTHY"))
        # UNSAFE：supported_commands 非空
        (home / "runtime" / "status.json").write_text(json.dumps({
            "mqtt_connected": True, "supported_commands": ["patrol"]}))
        result2 = health.evaluate(str(home))
        check("supported_commands 非空 → UNSAFE", result2["overall"] == "UNSAFE")
        del os.environ["FIREBOT_EDGE_HOME"]


def test_status_collect_json_no_secret() -> None:
    with tempfile.TemporaryDirectory(prefix="monitor-") as tmp:
        home = Path(tmp)
        (home / "config").mkdir()
        (home / "runtime").mkdir()
        (home / "config" / "install.env").write_text(
            "FIREBOT_EDGE_USER=tl\nFIREBOT_EDGE_HOME=%s\n" % home)
        (home / "config" / "identity.env").write_text("DEVICE_ID=firebot-vehicle-02\n")
        (home / "runtime" / "status.json").write_text(json.dumps({
            "mqtt_connected": True, "boot_id": "b-1", "supported_commands": []}))
        data = status.collect(str(home))
        for key in ("vehicle_id", "release_sha", "overall", "service_active", "mqtt_connected",
                    "mqtt_host", "mqtt_port", "boot_id", "supported_commands", "control_active"):
            check(f"status 含 {key}", key in data)
        blob = json.dumps(data, ensure_ascii=False)
        check("status 不含 secret 字段", all(t not in blob for t in ("password", "token", "secret")))


def test_redact() -> None:
    text = ("streamid=publish:R001-roof_rgb:R001:supersecret stream&"
            "passphrase=abc123&token=tok456 Authorization: Bearer deadbeef")
    out = redact.redact(text)
    check("streamid credential 脱敏", "supersecret" not in out and "publish:R001-roof_rgb:R001:***" in out)
    check("passphrase/token 脱敏", "abc123" not in out and "tok456" not in out)
    check("Bearer 脱敏", "deadbeef" not in out)
    check("hostname/path 保留", "R001-roof_rgb" in out)


def test_edge_paths_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="edge-paths-") as tmp:
        home = Path(tmp)
        (home / "config").mkdir()
        (home / "config" / "install.env").write_text(
            f"FIREBOT_EDGE_USER=tl\nFIREBOT_EDGE_HOME={home}\n")
        paths = edge_paths.edge_paths(str(home))
        check("edge_paths 含 canonical 路径",
              all(k in paths for k in ("home", "config", "identity_env", "bridge_env",
                                       "secrets", "mqtt_env", "device_token", "certs",
                                       "mqtt_ca", "runtime", "status_json", "logs",
                                       "connection_log", "audit_log", "backups",
                                       "releases", "current", "previous", "manifest")))
        check("runtime_user 读取", edge_paths.runtime_user(str(home)) == "tl")


if __name__ == "__main__":
    test_health_state_model()
    test_health_evaluate_with_fixture()
    test_status_collect_json_no_secret()
    test_redact()
    test_edge_paths_layout()
    print(f"\nMONITOR_TEST total={PASS + FAIL} pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)
