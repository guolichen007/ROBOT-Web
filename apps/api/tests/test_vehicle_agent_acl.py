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
