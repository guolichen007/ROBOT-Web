"""status 消息（partial，只发真实字段）。

v1.3 允许 status 只带车端真实拥有的字段。无真实源的字段不出现（不伪造 mode/estop）。
"""
from __future__ import annotations

from ..protocol import Protocol
from ..state import BridgeState


def make_status(proto: Protocol, state: BridgeState) -> dict | None:
    tele = state.snapshot_telemetry()
    # 无任何真实业务字段则不发（避免空 status 噪音）
    has_field = (
        tele["battery"] is not None
        or tele["mode"] is not None
        or tele["estop_active"] is not None
        or tele["active_task_id"] is not None
    )
    if not has_field:
        return None
    msg = proto.base("status")
    if tele["battery"] is not None:
        msg["battery"] = tele["battery"]
    if tele["mode"] is not None:
        msg["mode"] = tele["mode"]
    if tele["estop_active"] is not None:
        msg["estop_active"] = tele["estop_active"]
    if tele["active_task_id"] is not None:
        msg["active_task_id"] = tele["active_task_id"]
    return msg
