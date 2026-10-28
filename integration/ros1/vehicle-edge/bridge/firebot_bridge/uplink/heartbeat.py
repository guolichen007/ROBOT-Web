"""heartbeat 消息（心跳 1Hz）。"""
from __future__ import annotations

from ..protocol import Protocol


def make_heartbeat(proto: Protocol, uptime_seconds: float) -> dict:
    msg = proto.base("heartbeat")
    msg["uptime_seconds"] = round(uptime_seconds, 2)
    return msg
