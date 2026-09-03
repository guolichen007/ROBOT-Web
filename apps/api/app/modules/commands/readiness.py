from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    Robot,
    RobotCapability,
    RobotIntegrationProfile,
    RobotMotionProfile,
)

SAFETY_COMMANDS = ("stop_motion", "emergency_stop", "reset_estop")
AUTONOMOUS_COMMANDS = ("patrol", "extinguish", "return_dock", "cancel_task")
# This repository contains only ROS-native uplink normalization. There is no
# ROS1 command publisher/ACK translator in this release, so database flags can
# describe planned verification but can never enable a path that code does not implement.
ROS_COMPAT_DOWNLINK_IMPLEMENTED = False


def _ros_motion_path_verified(integration: RobotIntegrationProfile | None) -> bool:
    """真实 ROS 后端车辆必须共同证明的底层 ROS 运动命令通路。

    MOCK 是测试模拟，不经过真实 ROS 命令通路，调用方单独处理其测试语义。
    """
    return bool(
        integration
        and integration.bidirectional_bridge_verified
        and integration.command_path_verified
        and integration.cmd_vel_arbitration_verified
        and integration.ros_control_mode == 3
    )


def _supports(capability: RobotCapability | None, command: str) -> bool:
    # A missing retained capability declaration is never control proof.
    return bool(capability and command in (capability.supported_commands_json or []))


def robot_readiness(db: Session, robot: Robot) -> dict[str, Any]:
    """Compute control readiness from server facts, never from client-provided flags."""

    integration = db.get(RobotIntegrationProfile, robot.id)
    capability = db.get(RobotCapability, robot.id)
    motion = db.get(RobotMotionProfile, robot.id)
    online = robot.enabled and robot.online_state == "ONLINE"
    bridge_ready = bool(
        integration
        and (
            integration.source_kind != "ROS_COMPAT"
            or (ROS_COMPAT_DOWNLINK_IMPLEMENTED and integration.bidirectional_bridge_verified)
        )
    )
    control_contract_ready = bool(
        integration and integration.control_contract_verified and bridge_ready
    )
    ack_contract_ready = bool(integration and integration.ack_contract_verified)
    # ROS_COMPAT 仍受 ROS_COMPAT_DOWNLINK_IMPLEMENTED 只读约束；
    # CANONICAL_MQTT 真实车辆 Bridge 不得用传输层作为绕过 ROS 运动门的理由，
    # 必须与 ROS_COMPAT 同级证明 ROS 命令通路。MOCK 保留独立测试语义。
    ros_command_path_ready = bool(
        integration
        and (
            integration.source_kind != "ROS_COMPAT"
            or (ROS_COMPAT_DOWNLINK_IMPLEMENTED and _ros_motion_path_verified(integration))
        )
        and (integration.source_kind != "CANONICAL_MQTT" or _ros_motion_path_verified(integration))
    )
    safety = {
        command: bool(
            online
            and control_contract_ready
            and ack_contract_ready
            and ros_command_path_ready
            and _supports(capability, command)
        )
        for command in SAFETY_COMMANDS
    }
    motion_envelope_ready = bool(
        motion
        and motion.manual_watchdog_verified
        and motion.max_manual_forward_mps is not None
        and motion.max_manual_angular_radps is not None
    )
    manual_ready = bool(
        online
        and control_contract_ready
        and ack_contract_ready
        and ros_command_path_ready
        and motion_envelope_ready
        and _supports(capability, "manual_control")
        and safety["stop_motion"]
        and robot.estop_active is False
    )
    autonomous = {
        command: bool(
            online
            and control_contract_ready
            and ack_contract_ready
            and ros_command_path_ready
            and integration
            and integration.map_contract_verified
            and _supports(capability, command)
            and robot.estop_active is False
        )
        for command in AUTONOMOUS_COMMANDS
    }
    reasons: list[str] = []
    if not robot.enabled:
        reasons.append("ROBOT_DISABLED")
    if robot.online_state != "ONLINE":
        reasons.append(f"ROBOT_{robot.online_state}")
    if not integration:
        reasons.append("INTEGRATION_PROFILE_MISSING")
    elif integration.source_kind == "ROS_COMPAT" and not bridge_ready:
        reasons.append("ROS_COMPAT_READ_ONLY")
    if integration and not integration.control_contract_verified:
        reasons.append("CONTROL_CONTRACT_NOT_VERIFIED")
    if integration and not integration.ack_contract_verified:
        reasons.append("ACK_CONTRACT_NOT_VERIFIED")
    if integration and not integration.map_contract_verified:
        reasons.append("MAP_CONTRACT_NOT_VERIFIED")
    if integration and integration.source_kind in {"ROS_COMPAT", "CANONICAL_MQTT"}:
        if not integration.bidirectional_bridge_verified:
            reasons.append("BIDIRECTIONAL_BRIDGE_NOT_VERIFIED")
        if not integration.command_path_verified:
            reasons.append("COMMAND_PATH_NOT_VERIFIED")
        if not integration.cmd_vel_arbitration_verified:
            reasons.append("CMD_VEL_ARBITRATION_NOT_VERIFIED")
        if integration.ros_control_mode != 3:
            reasons.append("CONTROL_MODE_NOT_ROS")
    if not motion_envelope_ready:
        reasons.append("MANUAL_MOTION_PROFILE_NOT_VERIFIED")
    if not capability:
        reasons.append("CAPABILITY_DECLARATION_MISSING")
    if not safety["stop_motion"]:
        reasons.append("STOP_MOTION_NOT_READY")
    if robot.estop_active:
        reasons.append("ROBOT_ESTOP_ACTIVE")
    unsupported = [
        command
        for command in (*SAFETY_COMMANDS, "manual_control", *AUTONOMOUS_COMMANDS)
        if not _supports(capability, command)
    ]
    if unsupported:
        reasons.append("UNSUPPORTED_COMMANDS:" + ",".join(unsupported))
    return {
        "monitor_ready": online,
        "safety_command_ready": safety,
        "manual_control_ready": manual_ready,
        "autonomous_task_ready": autonomous,
        # Deprecated compatibility alias: autonomous patrol readiness only.
        "control_enabled": autonomous["patrol"],
        "readiness_reasons": reasons,
        "motion_profile": None
        if motion is None
        else {
            "max_manual_forward_mps": motion.max_manual_forward_mps,
            "max_manual_reverse_mps": motion.max_manual_reverse_mps,
            "max_manual_angular_radps": motion.max_manual_angular_radps,
            "manual_watchdog_verified": motion.manual_watchdog_verified,
            "reverse_allowed": motion.reverse_allowed,
            "reverse_precision_verified": motion.reverse_precision_verified,
        },
    }
