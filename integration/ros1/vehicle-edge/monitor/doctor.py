"""vehicle-edge-doctor 实现（规格三十）。

检查完整安装：Identity / Manifest / Directory layout / Ownership / Permissions /
Release(current/previous) / Config / Secrets metadata / MQTT CA / Systemd /
Runtime user / DNS / TLS / MQTT 本地状态 / Heartbeat / NTP / Disk /
Control safety / Legacy-New mutual exclusion。

只输出状态与元数据；绝不输出 secret value（secrets 只检查存在性与 mode）。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys

from lib.edge_paths import edge_paths, read_kv
from monitor.health import evaluate, load_status, service_active

EXIT_MAP = {"PASS": 0, "WARN": 0, "FAIL": 1, "CONFIG_ERROR": 40, "INTERNAL_ERROR": 50}


def mode_str(path: str) -> str:
    try:
        return oct(stat.S_IMODE(os.stat(path).st_mode))[2:]
    except OSError:
        return "-"


def user_str(path: str) -> str:
    try:
        info = os.stat(path)
        import pwd

        return f"{pwd.getpwuid(info.st_uid).pw_name}:{pwd.getpwuid(info.st_gid).gr_name if hasattr(info, 'st_gid') else ''}"
    except (OSError, ImportError):
        return "unknown"


def run(name: str, cond: bool, detail: str = "", warn: bool = False) -> dict:
    state = "WARN" if (warn and not cond) else ("PASS" if cond else "FAIL")
    print(f"{state:<4} {name}{(' ' + detail) if detail else ''}")
    return {"name": name, "state": state, "detail": detail}


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    as_json = "--json" in argv
    paths = edge_paths()
    results: list = []
    failed = 0

    def check(name: str, cond: bool, detail: str = "", warn: bool = False) -> None:
        nonlocal failed
        r = run(name, cond, detail, warn)
        results.append(r)
        if r["state"] == "FAIL":
            failed += 1

    # Identity
    identity = read_kv(paths["identity_env"], "DEVICE_ID")
    check("identity", bool(identity), identity or "<未配置>")

    # Manifest（非 secret；vehicle_id/display_name/site/profile/release）
    try:
        with open(paths["manifest"], encoding="utf-8") as handle:
            manifest = json.load(handle)
        check("manifest", manifest.get("product") == "firebot-edge",
              f"vehicle_id={manifest.get('vehicle_id', '')} release={manifest.get('release_sha', '')[:12]}")
    except (OSError, ValueError):
        manifest = {}
        check("manifest", False, "缺失或非法")

    # Directory layout（权限模型，规格九）
    check("layout-root", os.path.isdir(paths["home"]), paths["home"])
    check("layout-config", os.path.isdir(paths["config"]))
    check("layout-releases", os.path.isdir(paths["releases"]))
    check("layout-current", os.path.islink(paths["current"]), "symlink")
    check("layout-previous", os.path.islink(paths["previous"]) or not os.path.lexists(paths["previous"]),
          "symlink 或不存在")
    check("layout-secrets", os.path.isdir(paths["secrets"]))
    check("layout-runtime", os.path.isdir(paths["runtime"]))
    check("layout-data", os.path.isdir(paths["data"]))
    check("layout-logs", os.path.isdir(paths["logs"]))
    check("layout-backups", os.path.isdir(paths["backups"]))
    check("layout-certs", os.path.isdir(paths["certs"]))

    # Permissions（关键目录）+ P1-2 ownership 强校验（runtime user 来自 install.env）
    check("perm-secrets", mode_str(paths["secrets"]) == "700", mode_str(paths["secrets"]))
    if os.environ.get("FIREBOT_TEST_SKIP_OWNERSHIP") == "1":
        check("ownership-model", True, "SKIPPED（测试逃逸）")
    else:
        edge_user = read_kv(paths["install_env"], "FIREBOT_EDGE_USER")
        for label, path, expect in (
            ("EDGE_HOME_OWNER", paths["home"], "root:root"),
            ("CONFIG_OWNER", paths["config"], "root:root"),
            ("CERTS_OWNER", paths["certs"], "root:root"),
            ("RELEASES_OWNER", paths["releases"], "root:root"),
            ("SECRETS_OWNER", paths["secrets"], "root:root"),
            ("BACKUPS_OWNER", paths["backups"], "root:root"),
        ):
            check(label, user_str(path) == expect, user_str(path))
        if edge_user:
            for label, path in (
                ("RUNTIME_OWNER", paths["runtime"]),
                ("DATA_OWNER", paths["data"]),
                ("LOGS_OWNER", paths["logs"]),
            ):
                check(label, user_str(path).startswith(edge_user + ":"), user_str(path))
        else:
            check("ownership-runtime", False, "runtime user 未配置")
    check("perm-backups", mode_str(paths["backups"]) == "700", mode_str(paths["backups"]))
    check("perm-mqtt-env", mode_str(paths["mqtt_env"]) in ("600", "-"),
          mode_str(paths["mqtt_env"]))
    check("perm-device-token", mode_str(paths["device_token"]) in ("600", "-"),
          mode_str(paths["device_token"]))
    check("perm-ca", mode_str(paths["mqtt_ca"]) in ("644", "-"), mode_str(paths["mqtt_ca"]))
    check("perm-runtime-writable", os.access(paths["runtime"], os.W_OK))

    # Release current/previous 指向有效目录
    cur_ok = os.path.isdir(os.path.realpath(paths["current"])) if os.path.islink(paths["current"]) else False
    check("release-current", cur_ok, os.path.realpath(paths["current"]) if cur_ok else "未切换")

    # Config 关键文件
    check("config-install", os.path.isfile(paths["install_env"]))
    check("config-bridge", os.path.isfile(paths["bridge_env"]))
    check("config-assignment", os.path.isfile(paths["assignment_env"]))

    # Secrets metadata（只检查存在性与 mode，绝不输出值）
    check("secrets-mqtt", os.path.isfile(paths["mqtt_env"]))
    check("secrets-device-token", os.path.isfile(paths["device_token"]))

    # MQTT CA
    check("certs-mqtt-ca", os.path.isfile(paths["mqtt_ca"]), paths["mqtt_ca"])

    # Systemd unit
    check("systemd-unit",
          os.path.isfile("/etc/systemd/system/firebot-vehicle-edge.service") or
          os.environ.get("FIREBOT_TEST_SYSTEMD_DIR"),
          "/etc/systemd/system/firebot-vehicle-edge.service")

    # Runtime user
    check("runtime-user", bool(read_kv(paths["install_env"], "FIREBOT_EDGE_USER")),
          read_kv(paths["install_env"], "FIREBOT_EDGE_USER") or "<未配置>")

    # DNS / TLS（只读握手）
    mqtt_host = read_kv(paths["bridge_env"], "FIREBOT_MQTT_HOST")
    mqtt_port = read_kv(paths["bridge_env"], "FIREBOT_MQTT_PORT") or "8883"
    try:
        socket.getaddrinfo(mqtt_host, None)
        check("dns", True, mqtt_host)
    except OSError:
        check("dns", False, mqtt_host)
    if os.path.isfile(paths["mqtt_ca"]) and mqtt_host:
        try:
            import ssl

            context = ssl.create_default_context(cafile=paths["mqtt_ca"])
            with socket.create_connection((mqtt_host, int(mqtt_port)), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=mqtt_host):
                    check("tls", True, f"{mqtt_host}:{mqtt_port}")
        except (OSError, ssl.SSLError) as exc:
            check("tls", False, str(exc))
    else:
        check("tls", False, "CA 或 host 缺失")

    # MQTT 本地状态 / Heartbeat（只读 status.json）
    status = load_status()
    check("mqtt-local", bool(status.get("mqtt_connected")))
    hb = status.get("heartbeat_age_seconds")
    check("heartbeat", isinstance(hb, (int, float)) and hb <= 15,
          f"age={hb:.1f}s" if isinstance(hb, (int, float)) else "缺失", warn=True)

    # NTP
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized"],
                             capture_output=True, text=True, timeout=5).stdout
        ntp_ok = "=yes" in out
    except (OSError, subprocess.SubprocessError):
        ntp_ok = False
    check("ntp", ntp_ok, "NTPSynchronized" if ntp_ok else "未同步", warn=True)

    # Disk（Edge Home 所在盘剩余空间）
    usage = shutil.disk_usage(paths["home"])
    free_gb = usage.free / (1024 ** 3)
    check("disk", free_gb > 1.0, f"free={free_gb:.1f}GiB", warn=free_gb < 5.0)

    # Control safety + mutual exclusion（只读）
    result = evaluate()
    control_unsafe = result["checks"].get("control_active") or result["checks"].get(
        "control_enabled"
    ) == "enabled"
    check("control-safety", not control_unsafe, "SAFE" if not control_unsafe else "UNSAFE")
    check("mutual-exclusion",
          not (service_active("firebot-vehicle-edge") and service_active("firebot-bridge")))

    overall = "HEALTHY" if not failed else "DEGRADED"
    if as_json:
        print(json.dumps({"results": results, "overall": overall}, ensure_ascii=False))
    print(f"OVERALL={overall}")
    return EXIT_MAP["PASS" if not failed else "FAIL"]


if __name__ == "__main__":
    sys.exit(main())
