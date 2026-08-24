"""location 消息（map 系位姿）。

以 /firebot_bridge/location（amcl/map）为准；无数据不发。每次发布走新 seq。
"""
from __future__ import annotations

from ..config import Config
from ..protocol import Protocol
from ..state import BridgeState


def make_location(proto: Protocol, state: BridgeState, config: Config) -> dict | None:
    # 默认 FIREBOT_LOCATION_ENABLED=false：地图身份未明确确认前不发布 location。
    if not config.location_enabled:
        return None
    loc = state.last_location
    if not loc:
        return None
    pos = loc.get("position") or loc  # 兼容 {"x","y","theta"} 直接给出或嵌套
    try:
        x = float(pos.get("x", 0.0))
        y = float(pos.get("y", 0.0))
        theta = float(pos.get("theta", 0.0))
    except (TypeError, ValueError):
        return None
    msg = proto.base("location")
    msg.update(
        {
            "position": {"x": x, "y": y, "theta": theta},
            "linear_speed": loc.get("linear", 0.0),
            "angular_speed": loc.get("angular", 0.0),
            "site_code": config.site_code,
            "map_code": config.map_code,
            "map_version": config.map_version,
            "map_checksum": config.map_checksum,
            "frame_id": "map",
            "localization_status": loc.get("localization_status", "OK"),
        }
    )
    bat = state.snapshot_telemetry()["battery"]
    if bat is not None:
        msg["battery"] = bat
    return msg
