#!/usr/bin/env python3
"""location provider freshness / revision 测试：旧位置不得被重新包装成新 MQTT location。

无真实 ROS/MQTT 依赖，直接测 BridgeState 的 revision 去重与 stale 清除语义。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firebot_bridge.protocol import Protocol
from firebot_bridge.state import BridgeState
from firebot_bridge.uplink import location as loc_uplink


class _LocCfg:
    location_enabled = True
    site_code = "SITE"
    map_code = "MAP"
    map_version = "1"
    map_checksum = "sum"


def _loc(**kw) -> dict:
    base = {
        "position": {"x": 1.0, "y": 2.0, "theta": 0.0},
        "linear": 0.0,
        "angular": 0.0,
        "localization_status": "OK",
    }
    base.update(kw)
    return base


PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def main() -> int:
    proto = Protocol("firebot-vehicle-01", "boot")
    cfg = _LocCfg()
    state = BridgeState()

    # 初始无 location → 不发
    check("无 location 时不发", loc_uplink.make_location(proto, state, cfg) is None)

    # 真实 ROS 观测 → revision 递增，允许发一次
    state.set_location(_loc())
    r1 = state.get_location_revision()
    check("set_location 后 revision=1", r1 == 1)
    check("有新 location 可发", loc_uplink.make_location(proto, state, cfg) is not None)

    # 同一观测（不再 set_location）→ revision 不变，location_loop 不会重发
    check("未更新时 revision 不变", state.get_location_revision() == r1)

    # 纯横移（adapter 已算 hypot → linear>0）必须作为运动透传，不得判为 0
    state.set_location(_loc(linear=0.1))
    msg = loc_uplink.make_location(proto, state, cfg)
    check("纯横移 planar linear 透传 >0", msg is not None and msg["linear_speed"] > 0)

    state.set_location(_loc(linear=0.0))
    check("revision 单调递增", state.get_location_revision() == 3)

    # stale 清除：provider 断源超过 TTL 后 last_location=None，不能再发旧位置
    now = state.location_updated_monotonic + 10.0
    check("stale 后清除", state.expire_stale_location(3.0, now=now) is True)
    check("stale 后不发旧位置", loc_uplink.make_location(proto, state, cfg) is None)

    # recovered：新观测到达后恢复
    state.set_location(_loc(linear=0.0))
    check("recovered 后恢复可发", loc_uplink.make_location(proto, state, cfg) is not None)

    # TTL<=0 不清除（Config 层默认 fail-closed 正数，生产不会落入 0）
    state2 = BridgeState()
    state2.set_location(_loc())
    check(
        "TTL<=0 不清除（防御性）",
        state2.expire_stale_location(0.0, now=state2.location_updated_monotonic + 100) is False,
    )

    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
