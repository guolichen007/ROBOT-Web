from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.modules.robots.channel_freshness import effective_channel_state

NOW = datetime.now(UTC)


class _Channel:
    def __init__(
        self, channel: str, support_state: object, last_received_at: datetime | None = None
    ):
        self.channel = channel
        self.support_state = support_state
        self.last_received_at = last_received_at


class _Profile:
    def __init__(self, stale_seconds: float, offline_seconds: float):
        self.stale_seconds = stale_seconds
        self.offline_seconds = offline_seconds


def test_heartbeat_still_decays() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(
            _Channel("heartbeat", "CONNECTED", NOW - timedelta(seconds=7)), p, NOW
        )
        == "STALE"
    )


def test_battery_still_decays() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(
            _Channel("battery", "CONNECTED", NOW - timedelta(seconds=12)), p, NOW
        )
        == "NOT_CONNECTED"
    )


def test_smoke_still_decays() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(_Channel("smoke", "CONNECTED", NOW - timedelta(seconds=7)), p, NOW)
        == "STALE"
    )


def test_availability_not_time_decayed() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(
            _Channel("availability", "CONNECTED", NOW - timedelta(seconds=999)), p, NOW
        )
        == "CONNECTED"
    )


def test_capabilities_not_time_decayed() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(
            _Channel("capabilities", "CONNECTED", NOW - timedelta(seconds=999)), p, NOW
        )
        == "CONNECTED"
    )


def test_availability_explicit_offline_preserved() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(
            _Channel("availability", "NOT_CONNECTED", NOW - timedelta(seconds=999)), p, NOW
        )
        == "NOT_CONNECTED"
    )


def test_error_not_time_decayed() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(_Channel("battery", "ERROR", NOW - timedelta(seconds=999)), p, NOW)
        == "ERROR"
    )


def test_unsupported_not_time_decayed() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(
            _Channel("roof_rgb", "UNSUPPORTED", NOW - timedelta(seconds=999)), p, NOW
        )
        == "UNSUPPORTED"
    )


def test_support_state_none_is_fail_closed_not_connected() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(_Channel("battery", None, NOW - timedelta(seconds=1)), p, NOW)
        == "NOT_CONNECTED"
    )


def test_support_state_non_string_is_fail_closed_not_connected() -> None:
    p = _Profile(5, 10)
    assert (
        effective_channel_state(_Channel("battery", 12345, NOW - timedelta(seconds=1)), p, NOW)
        == "NOT_CONNECTED"
    )
