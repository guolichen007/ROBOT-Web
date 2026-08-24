"""sensor 消息（capability-driven：smoke 必须，IR 可选）。

无真实 smoke 源时不发布 sensor（不伪造 0）。
"""
from __future__ import annotations

from ..protocol import Protocol
from ..state import BridgeState


def make_sensor(proto: Protocol, state: BridgeState) -> dict | None:
    tele = state.snapshot_telemetry()
    if tele["smoke"] is None:
        return None  # 无真实 smoke 源，不发布
    msg = proto.base("sensor")
    msg["smoke"] = tele["smoke"]
    if tele["bottom_ir"] is not None:
        msg["bottom_ir"] = tele["bottom_ir"]
    if tele["top_ir_max"] is not None:
        msg["top_ir_max"] = tele["top_ir_max"]
    msg["payload"] = {}
    return msg
