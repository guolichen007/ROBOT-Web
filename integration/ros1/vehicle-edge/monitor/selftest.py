"""vehicle-connectivity-selftest 实现（规格二十九）。

主动但严格只读：layout / config / DNS / TCP / TLS / hostname / service /
本地 MQTT session 状态 / 心跳新鲜度。

绝对禁止：使用生产密码新建第二 MQTT session、MQTT publish/subscribe probe、
修改 assignment、发送 command（避免把真实 Bridge client 顶下线）。
"""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys

from lib.edge_paths import edge_paths, read_kv
from monitor.health import evaluate, load_status, service_active

EXIT_MAP = {"PASS": 0, "WARN": 0, "FAIL": 1}


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

    # layout / config
    results.append(run("layout", os.path.isdir(paths["home"]), paths["home"]))
    install = {}
    try:
        for line in open(paths["install_env"], encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                install[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    results.append(
        run("runtime-user", bool(install.get("FIREBOT_EDGE_USER")),
            install.get("FIREBOT_EDGE_USER", "<未配置>"))
    )
    mqtt_host = read_kv(paths["bridge_env"], "FIREBOT_MQTT_HOST")
    mqtt_port = read_kv(paths["bridge_env"], "FIREBOT_MQTT_PORT") or "8883"
    results.append(run("config", bool(mqtt_host), f"{mqtt_host}:{mqtt_port}"))

    # DNS（只读解析）
    try:
        socket.getaddrinfo(mqtt_host, None)
        results.append(run("dns", True, mqtt_host))
    except OSError:
        results.append(run("dns", False, mqtt_host))

    # TCP（只读 connect 后立即关闭；不发送任何协议字节）
    try:
        with socket.create_connection((mqtt_host, int(mqtt_port)), timeout=10):
            results.append(run("tcp", True, f"{mqtt_host}:{mqtt_port}"))
    except OSError as exc:
        results.append(run("tcp", False, str(exc)))

    # TLS（仅握手 hostname verify；不 MQTT 报文）
    ca = paths["mqtt_ca"]
    if os.path.isfile(ca):
        try:
            context = ssl.create_default_context(cafile=ca)
            with socket.create_connection((mqtt_host, int(mqtt_port)), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=mqtt_host) as tls:
                    results.append(run("tls", True, f"verify={mqtt_host}"))
        except (OSError, ssl.SSLError) as exc:
            results.append(run("tls", False, str(exc)))
    else:
        results.append(run("tls", False, f"CA 缺失 {ca}"))

    # service（只读 is-active；edge 与 legacy 互斥）
    edge_active = service_active("firebot-vehicle-edge")
    legacy_active = service_active("firebot-bridge")
    results.append(run("edge-service", edge_active, "firebot-vehicle-edge"))
    results.append(
        run("mutual-exclusion", not (edge_active and legacy_active),
            f"edge={edge_active} legacy={legacy_active}")
    )

    # 本地 MQTT session 状态 + 心跳新鲜度（只读 status.json；不建第二 session）
    status = load_status()
    results.append(
        run("mqtt-local", bool(status.get("mqtt_connected")),
            "connected" if status.get("mqtt_connected") else "disconnected")
    )
    hb = status.get("heartbeat_age_seconds")
    if isinstance(hb, (int, float)) and edge_active:
        results.append(run("heartbeat-fresh", hb <= 15, f"age={hb:.1f}s", warn=(hb > 5)))
    else:
        results.append(run("heartbeat-fresh", False, "无心跳数据（服务未运行或字段缺失）"))

    # hostname
    results.append(run("hostname", bool(socket.gethostname()), socket.gethostname()))

    # control 安全（只读）
    result = evaluate()
    control_unsafe = result["checks"].get("control_active") or result["checks"].get(
        "control_enabled"
    ) == "enabled"
    results.append(run("control-safety", not control_unsafe, "SAFE" if not control_unsafe else "UNSAFE"))

    for item in results:
        if item["state"] == "FAIL":
            failed += 1

    if as_json:
        import json

        print(json.dumps({"results": results, "overall": "PASS" if not failed else "FAIL"},
                         ensure_ascii=False))
    print(f"SELFTEST={'FAIL' if failed else 'PASS'} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
