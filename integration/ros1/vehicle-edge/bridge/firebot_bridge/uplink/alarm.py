"""alarm 消息（火警，接口冻结，本轮占位）。"""
from __future__ import annotations

from ..protocol import Protocol


def make_alarm(
    proto: Protocol,
    event_id: str,
    fire_type: str,
    severity: str,
    position: dict,
    confidence: float | None = None,
    parking_slot_code: str | None = None,
) -> dict:
    msg = proto.base("alarm")
    msg.update(
        {
            "event_id": event_id,
            "fire_type": fire_type,
            "severity": severity,
            "confidence": confidence,
            "parking_slot_code": parking_slot_code,
            "position": position,
            "media": {},
        }
    )
    return msg
