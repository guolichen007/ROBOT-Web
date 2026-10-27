from pathlib import Path

import pytest

from scripts.check_vehicle_agent_acl import validate_acl

ROOT = Path(__file__).resolve().parents[3]


def template():
    return (ROOT / "infra/mosquitto/acl.example").read_text(encoding="utf-8")


def test_v1_acl_templates():
    assert validate_acl(template())
    assert validate_acl(
        (ROOT / "integration/ros1/acl/firebot-fleet.acl").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "line",
    [
        "topic readwrite _platform/vehicle-agent-v1/+/+",
        "topic readwrite v1/robot/+/+",
        "pattern read v1/robot/%u/cmd",
        *[
            f"pattern write v1/robot/%u/{kind}"
            for kind in ("ack", "status", "telemetry", "availability", "event")
        ],
    ],
)
def test_missing_any_required_runtime_acl_permission_fails_closed(line):
    assert not validate_acl(template().replace(line, "# missing permission"))


def test_wrong_platform_user_or_conflicting_deny_fails_closed():
    assert not validate_acl(template().replace("user platform", "user historical"))
    assert not validate_acl(template() + "\npattern deny v1/robot/%u/cmd")
    assert not validate_acl(template() + "\npattern deny #")


def test_downlink_allowlist_gate() -> None:
    """二级下行安全门（R2/Stage E）：global + allowlist 双条件。"""
    from app.core.config import Settings

    def settings_with(enabled: bool, allowlist: str) -> Settings:
        return Settings(
            _env_file=None,
            app_env="test",
            vehicle_agent_v1_downlink_enabled=enabled,
            vehicle_agent_v1_downlink_allowlist=allowlist,
        )

    # global=false + R001 allowlist → DENY
    assert settings_with(False, "R001").vehicle_downlink_allowed("R001") is False
    # global=true + empty → DENY（禁止一开总开关全部车辆下行）
    assert settings_with(True, "").vehicle_downlink_allowed("R001") is False
    # global=true + R001 → R001 ALLOW
    assert settings_with(True, "R001").vehicle_downlink_allowed("R001") is True
    # global=true + R001 → R002 DENY
    assert settings_with(True, "R001").vehicle_downlink_allowed("R002") is False
