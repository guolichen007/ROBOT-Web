from app.db.models import Robot
from app.modules.commands.service import build_command_payload


def test_manual_command_ttl_and_identity() -> None:
    robot = Robot(
        id="robot-id",
        vehicle_id="R001",
        site_id="site-id",
        name="R001",
        boot_id="00000000-0000-4000-8000-000000000002",
    )
    payload = build_command_payload(
        robot=robot,
        operator_id="user-id",
        cmd="manual_control",
        params={"linear": 0.1, "angular": 0},
        ttl_ms=500,
        priority=80,
        lease_id="lease-id",
        control_session_id="session-id",
        seq=9,
    )
    assert payload["ttl_ms"] == 500
    assert payload["lease_id"] == "lease-id"
    assert payload["seq"] == 9
    assert payload["cmd"] == "manual_control"
