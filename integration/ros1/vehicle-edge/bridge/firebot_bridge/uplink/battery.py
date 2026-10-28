"""电池值提供（canonical 来源：/firebot_bridge/battery std_msgs/Float32 → state.last_battery）。"""
from __future__ import annotations

from ..state import BridgeState


def battery_value(state: BridgeState) -> float | None:
    return state.snapshot_telemetry()["battery"]
