"""v1 服务端门禁，基于已入库的状态和既有集成合同。"""

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.db.models import Robot, RobotCapability, RobotIntegrationProfile
from services.vehicle_agent_v1 import COMMANDS, PROTOCOL, program_ready


def gate_reason(db, robot: Robot, action: str, now: datetime | None = None) -> str:
    if robot.agent_protocol != PROTOCOL:
        return "AGENT_PROTOCOL_MISMATCH"
    if action not in COMMANDS:
        return "ROBOT_CAPABILITY_UNSUPPORTED"
    if not get_settings().vehicle_agent_v1_downlink_enabled:
        return "VEHICLE_AGENT_DOWNLINK_DISABLED"
    if not robot.enabled or robot.online_state != "ONLINE":
        return "ROBOT_OFFLINE"
    integration = db.get(RobotIntegrationProfile, robot.id)
    if not integration or integration.source_kind not in {PROTOCOL, "MOCK"}:
        return "INTEGRATION_PROFILE_MISSING"
    capability = db.get(RobotCapability, robot.id)
    if not capability or action not in (capability.supported_commands_json or []):
        return "ROBOT_CAPABILITY_UNSUPPORTED"
    if action in {"ping", "get_status", "get_telemetry"}:
        return ""
    if not integration.control_contract_verified or not integration.ack_contract_verified:
        return "CONTROL_CONTRACT_NOT_VERIFIED"
    if robot.estop_active is not False:
        return "ROBOT_ESTOP_ACTIVE" if robot.estop_active else "ESTOP_STATE_UNKNOWN"
    state = robot.agent_state_json or {}
    received = state.get("status_received_at")
    if not received or (now or datetime.now(UTC)) - datetime.fromisoformat(received) > timedelta(
        seconds=integration.stale_seconds
    ):
        return "VEHICLE_AGENT_STATUS_STALE"
    if datetime.fromisoformat(received) > (now or datetime.now(UTC)) + timedelta(seconds=2):
        return "VEHICLE_AGENT_STATUS_STALE"
    status = state.get("status", {})
    if status.get("online") is not True:
        return "ROBOT_OFFLINE"
    programs = status.get("programs", {})
    task = status.get("task", {})
    active = task.get("state")
    paused = task.get("patrol_paused") is True
    navigation = program_ready(programs.get("navigation"))
    chassis = program_ready(programs.get("chassis"))
    if action == "start":
        return "" if active == "idle" and not paused else "BUSY"
    if action == "close":
        return "" if active == "idle" or paused else "BUSY"
    if action == "stop_patrol":
        return "" if active in {"idle", "patrol"} or paused else "BUSY"
    if action == "stop_navigate":
        return "" if active in {"idle", "spot"} and not paused else "BUSY"
    if not chassis or not navigation:
        return "NOT_READY"
    if not integration.map_contract_verified:
        return "MAP_CONTRACT_NOT_VERIFIED"
    if action == "resume_patrol":
        return "" if paused and active in {"idle", "patrol"} else "PATROL_NOT_PAUSED"
    return "" if active == "idle" and not paused else "BUSY"
