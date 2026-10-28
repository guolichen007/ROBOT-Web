"""vehicle-connection-status 实现（人类输出 + --json；规格二十五/二十六）。

只读本地 canonical status.json + systemd；不含任何 secret。
"""

from __future__ import annotations

import json
import sys

from lib.edge_paths import edge_paths, read_kv
from monitor.health import evaluate, load_status, service_active


def collect(home: str | None = None) -> dict:
    paths = edge_paths(home)
    status = load_status(home)
    result = evaluate(home)
    identity = read_kv(paths["identity_env"], "DEVICE_ID") or status.get("vehicle_id", "")
    release_sha = status.get("release_sha") or read_kv(
        paths["config"] + "/release.env", "FINAL_SHA"
    )
    mqtt_host = status.get("mqtt_host") or read_kv(paths["bridge_env"], "FIREBOT_MQTT_HOST")
    mqtt_port = read_kv(paths["bridge_env"], "FIREBOT_MQTT_PORT") or "8883"
    return {
        "vehicle_id": identity,
        "release_sha": release_sha,
        "overall": result["overall"],
        "exit_code": result["exit_code"],
        "service_active": service_active("firebot-vehicle-edge"),
        "legacy_bridge_active": service_active("firebot-bridge"),
        "mqtt_connected": status.get("mqtt_connected", False),
        "mqtt_host": mqtt_host,
        "mqtt_port": int(mqtt_port) if str(mqtt_port).isdigit() else 0,
        "boot_id": status.get("boot_id", ""),
        "supported_commands": status.get("supported_commands") or [],
        "control_active": service_active("firebot-control"),
        "heartbeat_age_seconds": status.get("heartbeat_age_seconds"),
        "reconnect_count": status.get("mqtt_reconnect_count", 0),
        "last_error": status.get("last_error", ""),
    }


def human(data: dict) -> str:
    lines = [
        "Vehicle Edge",
        "",
        f"Vehicle ID        {data['vehicle_id'] or '<unknown>'}",
        f"Release           {data['release_sha'] or '<unknown>'}",
        "",
        f"Service           {'ACTIVE' if data['service_active'] else 'INACTIVE'}",
        f"MQTT              {'CONNECTED' if data['mqtt_connected'] else 'DISCONNECTED'}",
        f"Broker            {data['mqtt_host'] or '<unknown>'}:{data['mqtt_port']}",
        "",
        f"Boot ID           {data['boot_id'] or '<none>'}",
        f"Heartbeat Age     {data['heartbeat_age_seconds']}s"
        if data["heartbeat_age_seconds"] is not None
        else "Heartbeat Age     <none>",
        f"Reconnect Count   {data['reconnect_count']}",
        "",
        f"Commands          {'NONE' if not data['supported_commands'] else ','.join(data['supported_commands'])}",
        f"Control           {'UNSAFE' if data['control_active'] else 'SAFE'}",
        "",
        f"Overall           {data['overall']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    data = collect()
    if "--json" in argv:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(human(data))
    return data["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
