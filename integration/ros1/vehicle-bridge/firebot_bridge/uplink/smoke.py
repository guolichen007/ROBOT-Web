"""烟雾值提供（真实来源：/firebot_bridge/smoke ← Modbus/standalone 脚本 → state.last_smoke）。"""
from __future__ import annotations

from ..state import BridgeState


def smoke_value(state: BridgeState) -> float | None:
    return state.snapshot_telemetry()["smoke"]
