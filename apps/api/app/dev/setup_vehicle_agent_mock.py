"""显式 test fixture，永不用于生产 migration/seed。"""

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Robot, RobotCapability, RobotIntegrationProfile, User
from app.db.session import SessionLocal
from services.vehicle_agent_v1 import COMMANDS, PROTOCOL


def main() -> None:
    settings = get_settings()
    if (
        settings.app_env != "test"
        or not settings.mock_enabled
        or not settings.vehicle_agent_v1_downlink_enabled
    ):
        raise RuntimeError("v1 fixture requires explicit mock-only test settings")
    with SessionLocal.begin() as db:
        admin = db.scalar(select(User).where(User.username == settings.bootstrap_admin_username))
        if admin:
            admin.must_change_password = False
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        if not robot:
            raise RuntimeError("seed test robot first")
        robot.agent_protocol, robot.agent_state_json = PROTOCOL, {}
        robot.online_state, robot.last_seen_at, robot.boot_id = "OFFLINE", None, None
        robot.estop_active = False
        integration = db.get(RobotIntegrationProfile, robot.id)
        assert integration is not None
        integration.source_kind = "MOCK"
        integration.stale_seconds = settings.vehicle_agent_v1_status_stale_seconds
        integration.offline_seconds = settings.vehicle_agent_v1_offline_seconds
        integration.control_contract_verified = True
        integration.ack_contract_verified = True
        integration.map_contract_verified = True
        integration.read_only_reason = None
        capability = db.get(RobotCapability, robot.id)
        assert capability is not None
        capability.protocol_version = "vehicle-agent-v1"
        capability.supported_commands_json = list(COMMANDS)


if __name__ == "__main__":
    main()
