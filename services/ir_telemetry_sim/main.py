"""红外模拟数值源——G3-R2 起**产品停用**，仅保留为旧协议兼容性测试工具。

G3-R2 硬件契约：不配置独立红外测温传感器，没有独立温度数值来源；
产品 / 预览 / 部署不得运行本工具。
本工具只在隔离兼容性测试中、显式同时满足以下两项时运行，否则退出码 3 且不连接 Broker：
  - 环境变量 FIREBOT_LEGACY_THERMAL_COMPAT_TEST=1
  - 命令行 --legacy-compat-test
用途仅为证明云端对历史/兼容 telemetry.thermal 的采集、幂等、冲突、鉴权行为不回归，
以及 Web 收到此类数据也不显示温度。
模拟设备在线状态请改用 services.sim_presence（不发送 telemetry）。

- 身份固定 firebot-sim-ir-01（硬编码校验），拒绝冒用任何其他 vehicle_id（含 firebot-vehicle-01）；
- 复用 V1 现有合同：availability / status / telemetry.thermal.max_temp（不新增协议字段）；
- 复用只读 Agent 的 MqttTransport（TLS/CA 校验、per-device 凭证、LWT、结构性零订阅）；
- 模拟标识由云端 SIMULATED_VEHICLE_IDS 登记决定，车端自报不具效力。

用法（仅兼容性测试）：
  FIREBOT_LEGACY_THERMAL_COMPAT_TEST=1 python -m services.ir_telemetry_sim.main \\
      --legacy-compat-test --config identity.json --values 27.5,42.0,67.8,null \\
      [--interval 1.0] [--stale-seconds 600] [--raw '{"max_temp":"hot"}']
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta

from services.vehicle_agent_v1_readonly.identity import IdentityError, load_identity
from services.vehicle_agent_v1_readonly.transport import MqttTransport, TransportConfig

SIM_VEHICLE_ID = "firebot-sim-ir-01"
COMPAT_ENV = "FIREBOT_LEGACY_THERMAL_COMPAT_TEST"
DISABLED_MESSAGE = (
    "ir-telemetry-sim disabled (G3-R2: no independent temperature sensor); "
    f"legacy compatibility tests only: {COMPAT_ENV}=1 and --legacy-compat-test"
)


def telemetry(max_temp, *, ts: datetime) -> dict:
    return {
        "pose": None,
        "battery": None,
        "smog": None,
        "thermal": None if max_temp is None else {"max_temp": max_temp},
        "ts": ts.isoformat(),
    }


def parse_values(raw: str) -> list:
    values = []
    for item in raw.split(","):
        item = item.strip()
        values.append(None if item == "null" else float(item))
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ir-telemetry-sim")
    parser.add_argument("--config", required=True)
    parser.add_argument("--values", default="27.5,42.0,67.8,null")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--stale-seconds", type=float, default=0.0, help="ts 回拨秒数（过期反证）")
    parser.add_argument("--raw-thermal", help="原样发送的 thermal JSON（非法类型反证）")
    parser.add_argument("--allow-placeholder-credentials", action="store_true")
    parser.add_argument(
        "--legacy-compat-test", action="store_true", help="仅旧协议兼容性测试（需同时设置环境变量）"
    )
    args = parser.parse_args(argv)
    if not (args.legacy_compat_test and os.environ.get(COMPAT_ENV) == "1"):
        # G3-R2：产品停用。门禁在读取身份/连接 Broker 之前，停用状态下零网络副作用。
        print(DISABLED_MESSAGE, file=sys.stderr)
        return 3
    try:
        identity = load_identity(
            __import__("pathlib").Path(args.config),
            allow_placeholder_password=args.allow_placeholder_credentials,
        )
    except IdentityError as exc:
        print(f"identity rejected: {exc}", file=sys.stderr)
        return 2
    if identity.vehicle_id != SIM_VEHICLE_ID:
        print(f"refusing: simulator identity must be {SIM_VEHICLE_ID}", file=sys.stderr)
        return 2
    transport = MqttTransport(identity, TransportConfig())
    transport.connect()
    if not transport.wait_for_connect(10):
        print("connect failed", file=sys.stderr)
        return 4
    transport.publish_availability_live()
    transport.publish_status(
        {
            "online": True,
            "programs": {"agent": True, "thermal": "SIMULATED"},
            "task": {"state": "idle", "patrol_paused": False},
            "ts": datetime.now(UTC).isoformat(),
        }
    )
    sent = []
    if args.raw_thermal is not None:
        payload = telemetry(None, ts=datetime.now(UTC))
        payload["thermal"] = json.loads(args.raw_thermal)
        ok = transport.publish_telemetry(payload)
        sent.append({"thermal": payload["thermal"], "queued": ok})
    else:
        for value in parse_values(args.values):
            ts = datetime.now(UTC) - timedelta(seconds=args.stale_seconds)
            ok = transport.publish_telemetry(telemetry(value, ts=ts))
            sent.append({"max_temp": value, "ts": ts.isoformat(), "queued": ok})
            deadline = time.monotonic() + args.interval
            while time.monotonic() < deadline:
                transport.loop(0.05)
    for _ in range(10):
        transport.loop(0.05)
    print(json.dumps({"vehicle_id": SIM_VEHICLE_ID, "simulated": True, "sent": sent}))
    transport.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
