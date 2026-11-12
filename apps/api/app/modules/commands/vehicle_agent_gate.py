"""v1 服务端门禁，基于已入库的状态和既有集成合同。"""

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.events import get_redis
from app.core.vehicle_agent_health import adapter_healthy
from app.core.vehicle_routing import (
    RoutingError,
    adapter_config_consistent,
    adapter_instance,
    execution_mode,
    execution_profile,
    transport_profile,
)
from app.db.models import Robot, RobotCapability, RobotIntegrationProfile
from services.vehicle_agent_v1 import COMMANDS, PROTOCOL, program_ready


def command_mode(robot: Robot, action: str) -> str:
    """命令执行模式（DIAGNOSTIC / SOFTWARE_TEST / REAL）；档外命令抛 RoutingError。"""
    return execution_mode(robot.vehicle_id, action)


def gate_reason(db, robot: Robot, action: str, now: datetime | None = None) -> str:
    if robot.agent_protocol != PROTOCOL:
        return "AGENT_PROTOCOL_MISMATCH"
    if action not in COMMANDS:
        return "ROBOT_CAPABILITY_UNSUPPORTED"
    settings = get_settings()
    if not settings.vehicle_agent_v1_downlink_enabled:
        return "VEHICLE_AGENT_DOWNLINK_DISABLED"
    # 逐车 allowlist：全局开关不能绕过（G2-E）
    if not settings.vehicle_downlink_allowed(robot.vehicle_id):
        return "VEHICLE_NOT_IN_DOWNLINK_ALLOWLIST"
    try:
        profile = transport_profile(robot.vehicle_id)
        instance = adapter_instance(profile)
        governed = execution_profile(robot.vehicle_id) is not None
        mode = command_mode(robot, action)
    except RoutingError:
        return "EXECUTION_PROFILE_DENIED"
    if governed and mode in {"DIAGNOSTIC", "SOFTWARE_TEST"}:
        # G2：逐车执行档授权的诊断/软件测试命令不触达任何硬件，不以 enabled（真实控制
        # 开关）或 supported_commands（真实控制能力声明）作为前提；真实动作（REAL）仍走
        # 下方完整原有门禁。急停 NULL 保持“未验证”，不得为软件测试改写。
        if robot.online_state != "ONLINE":
            return "ROBOT_OFFLINE"
        if not adapter_healthy(instance=instance):
            return "VEHICLE_AGENT_ADAPTER_UNAVAILABLE"
        if not adapter_config_consistent(get_redis(), instance):
            return "ROUTING_CONFIG_MISMATCH"
        integration = db.get(RobotIntegrationProfile, robot.id)
        if not integration or integration.source_kind not in {PROTOCOL, "MOCK"}:
            return "INTEGRATION_PROFILE_MISSING"
        if robot.estop_active is True:
            return "ROBOT_ESTOP_ACTIVE"
        return ""
    if not robot.enabled or robot.online_state != "ONLINE":
        return "ROBOT_OFFLINE"
    if not adapter_healthy(instance=instance):
        return "VEHICLE_AGENT_ADAPTER_UNAVAILABLE"
    if not adapter_config_consistent(get_redis(), instance):
        return "ROUTING_CONFIG_MISMATCH"
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
