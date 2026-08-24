"""availability 消息（在线状态，QoS1 retain）。"""
from __future__ import annotations

from ..protocol import Protocol


def make_availability(proto: Protocol, state_val: str, reason: str | None = None) -> dict:
    msg = proto.base("availability")
    msg["state"] = state_val
    msg["reason"] = reason
    return msg
