"""模拟设备在线状态源（仅隔离/测试环境，G3-R2）。

硬件契约（G3-R2）：不配置独立红外测温传感器，**不发送任何 telemetry（含温度）**。
模拟视频设备如需在 Web 显示在线，只用本工具以独立模拟身份上报必要的 availability / status。

- 身份必须位于合成命名空间 ``firebot-sim-<name>``（与云端 is_synthetic_namespace 同规则），
  且不在真实车辆集合（R001/R002/firebot-vehicle-01/02）；否则退出码 2，不连接 Broker。
- 复用只读 Agent 的 MqttTransport（TLS/CA 校验、per-device 凭证、LWT、结构性零订阅）；
  本模块结构上不调用 publish_telemetry。
- 模拟标识仍由云端 SIMULATED_VEHICLE_IDS 登记决定，车端自报不具效力。

用法：
  python -m services.sim_presence.main --config identity.json [--hold-seconds 0]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from services.vehicle_agent_v1_readonly.identity import IdentityError, load_identity
from services.vehicle_agent_v1_readonly.transport import MqttTransport, TransportConfig

# 与 apps/api/app/core/config.py 的 SYNTHETIC_ID_PATTERN / REAL_VEHICLE_IDS 保持一致
# （不 import 云端配置；apps/api/tests/test_g3r2_temperature_disabled.py 校验两者一致）
SYNTHETIC_ID_PATTERN = re.compile(r"^firebot-sim-[a-z0-9]+(?:-[a-z0-9]+)*$")
REAL_VEHICLE_IDS = frozenset({"firebot-vehicle-01", "firebot-vehicle-02", "R001", "R002"})


def is_sim_identity(vehicle_id: str) -> bool:
    return bool(SYNTHETIC_ID_PATTERN.fullmatch(vehicle_id)) and vehicle_id not in REAL_VEHICLE_IDS


def status_payload() -> dict:
    # 只声明存在模拟相机；不声明 thermal 测温程序（无独立温度传感器）。
    return {
        "online": True,
        "programs": {"agent": True, "camera": "SIMULATED"},
        "task": {"state": "idle", "patrol_paused": False},
        "ts": datetime.now(UTC).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sim-presence")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=0.0,
        help="保持连接并每秒刷新 live availability 的秒数",
    )
    parser.add_argument("--allow-placeholder-credentials", action="store_true")
    args = parser.parse_args(argv)
    try:
        identity = load_identity(
            Path(args.config), allow_placeholder_password=args.allow_placeholder_credentials
        )
    except IdentityError as exc:
        print(f"identity rejected: {exc}", file=sys.stderr)
        return 2
    if not is_sim_identity(identity.vehicle_id):
        print("refusing: simulator identity must be firebot-sim-<name>", file=sys.stderr)
        return 2
    transport = MqttTransport(identity, TransportConfig())
    transport.connect()
    if not transport.wait_for_connect(10):
        print("connect failed", file=sys.stderr)
        return 4
    sent = {
        "availability": transport.publish_availability_live(),
        "status": transport.publish_status(status_payload()),
    }
    deadline = time.monotonic() + max(0.0, args.hold_seconds)
    next_tick = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_tick:
            transport.publish_availability_live()
            next_tick += 1.0
        transport.loop(0.05)
    for _ in range(10):
        transport.loop(0.05)
    print(
        json.dumps(
            {"vehicle_id": identity.vehicle_id, "simulated": True, "telemetry_sent": 0, **sent}
        )
    )
    transport.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
