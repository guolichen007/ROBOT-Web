"""字段级 channel freshness：根据最后真实接收时间派生 effective support_state。

U2B 语义：
- CONNECTED 按时间退化为 STALE → NOT_CONNECTED（依据 integration profile 的
  stale_seconds / offline_seconds，不是车端测试 TTL）。
- ERROR / UNSUPPORTED 等是车端/系统显式声明的状态，不得被时间算法覆盖。
- 不引入第二套 battery_fresh/smoke_fresh boolean；data_channels 是唯一事实源。
"""

from __future__ import annotations

from datetime import datetime

_TIME_DECAYABLE = {"CONNECTED", "STALE", "NOT_CONNECTED"}
# availability / capabilities 是事件/声明状态，不是周期数据，不随时间衰减：
# 收到一次 CONNECTED 就保持 CONNECTED，直到显式 offline / 能力声明变化。
_EVENT_STATE_CHANNELS = {"availability", "capabilities"}


def effective_channel_state(channel, profile, now: datetime) -> str:
    """返回该 channel 的 effective support_state。

    channel: RobotDataChannel
    profile: RobotIntegrationProfile | None
    now: 当前 server 时间（UTC aware）
    """
    state = getattr(channel, "support_state", None)
    if not isinstance(state, str):
        # 未知/缺失状态：fail-closed，不伪造 CONNECTED（channel 状态只允许合法枚举）
        return "NOT_CONNECTED"
    if state not in _TIME_DECAYABLE:
        # 显式 ERROR / UNSUPPORTED 等状态不允许被时间逻辑覆盖
        return state
    channel_name = getattr(channel, "channel", None)
    if channel_name in _EVENT_STATE_CHANNELS:
        # availability / capabilities 不随时间衰减，保持显式状态
        return state
    last = getattr(channel, "last_received_at", None)
    if last is None:
        return state
    stale_seconds = getattr(profile, "stale_seconds", None) if profile else None
    offline_seconds = getattr(profile, "offline_seconds", None) if profile else None
    if stale_seconds is None and offline_seconds is None:
        return state
    # last_received_at 可能是 naive/aware 混用，容错处理
    age = (now - last).total_seconds()
    if offline_seconds is not None and age >= offline_seconds:
        return "NOT_CONNECTED"
    if stale_seconds is not None and age >= stale_seconds:
        return "STALE"
    return "CONNECTED"
