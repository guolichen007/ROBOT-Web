"""vehicle-connection-watch 实现（规格二十八）。

--interval N / --json / --once。主要观察本地 canonical status.json；
绝不新建 MQTT client（不把真实 Bridge client 顶下线）。
"""

from __future__ import annotations

import json
import sys
import time

from monitor.health import evaluate, load_status


def snapshot() -> dict:
    status = load_status()
    result = evaluate()
    return {
        "ts": int(time.time()),
        "overall": result["overall"],
        "mqtt_connected": status.get("mqtt_connected", False),
        "heartbeat_age_seconds": status.get("heartbeat_age_seconds"),
        "reconnect_count": status.get("mqtt_reconnect_count", 0),
        "boot_id": status.get("boot_id", ""),
        "control_active": result["checks"].get("control_active", False),
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    interval = 2
    once = False
    as_json = False
    i = 0
    while i < len(argv):
        if argv[i] == "--interval" and i + 1 < len(argv):
            try:
                interval = int(argv[i + 1])
            except ValueError:
                interval = 2
            i += 2
        elif argv[i] == "--json":
            as_json = True
            i += 1
        elif argv[i] == "--once":
            once = True
            i += 1
        else:
            i += 1

    while True:
        snap = snapshot()
        if as_json:
            print(json.dumps(snap, ensure_ascii=False), flush=True)
        else:
            ts = time.strftime("%H:%M:%S")
            hb = snap["heartbeat_age_seconds"]
            hb_s = f"{hb:.1f}s" if isinstance(hb, (int, float)) else "-"
            print(
                f"{ts} {snap['overall']:<8} mqtt={'connected' if snap['mqtt_connected'] else 'disconnected'} "
                f"hb={hb_s} reconnects={snap['reconnect_count']}",
                flush=True,
            )
        if once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
