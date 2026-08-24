"""电池值提供（真实来源：/robot_status.battery_percentage → state.last_battery）。"""
from __future__ import annotations

from ..state import BridgeState


def battery_value(state: BridgeState) -> float | None:
    return state.snapshot_telemetry()["battery"]
