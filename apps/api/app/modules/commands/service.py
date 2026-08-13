from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import PlatformError
from app.core.events import get_redis
from app.db.models import (
    Command,
    OutboxEvent,
    Robot,
    RobotCapability,
    RobotIntegrationProfile,
    Task,
)
from app.modules.commands.readiness import ROS_COMPAT_DOWNLINK_IMPLEMENTED, robot_readiness

ACTIVE_TASK_STATES = {"CREATED", "QUEUED", "ACCEPTED", "EXECUTING"}


def command_code() -> str:
    return f"C{datetime.now(UTC):%Y%m%d%H%M%S}-{str(uuid4())[:8]}"


def task_code(prefix: str = "T") -> str:
    return f"{prefix}{datetime.now(UTC):%Y%m%d%H%M%S}-{str(uuid4())[:8]}"


def assert_robot_can_execute(
    db: Session, robot: Robot, action: str, ignore_task_id: str | None = None
) -> None:
    if not robot.enabled:
        raise PlatformError("ROBOT_DISABLED", "机器人已禁用")
    if robot.online_state in {"STALE", "OFFLINE"} and action not in {
        "emergency_stop",
        "stop_motion",
    }:
        raise PlatformError(
            f"ROBOT_{robot.online_state}",
            f"机器人状态为 {robot.online_state}，拒绝执行 {action}",
            details={"state": robot.online_state, "action": action},
        )
    if robot.estop_active and action not in {"emergency_stop", "reset_estop"}:
        raise PlatformError("ROBOT_ESTOP_ACTIVE", "软件急停已锁存")
    integration = db.get(RobotIntegrationProfile, robot.id)
    if not integration:
        raise PlatformError(
            "INTEGRATION_PROFILE_MISSING",
            "机器人尚未建立可信集成档案，拒绝执行车辆命令",
            details={"action": action},
        )
    if (
        integration
        and integration.source_kind == "ROS_COMPAT"
        and not all(
            (
                ROS_COMPAT_DOWNLINK_IMPLEMENTED,
                integration.bidirectional_bridge_verified,
                integration.command_path_verified,
                integration.cmd_vel_arbitration_verified,
                integration.ros_control_mode == 3,
            )
        )
    ):
        raise PlatformError(
            "ROS_COMPAT_READ_ONLY",
            integration.read_only_reason or "ROS1 兼容链路的下行、仲裁或 control_mode 尚未验证",
            details={
                "source_kind": integration.source_kind,
                "action": action,
                "bidirectional_bridge_verified": integration.bidirectional_bridge_verified,
                "command_path_verified": integration.command_path_verified,
                "cmd_vel_arbitration_verified": integration.cmd_vel_arbitration_verified,
                "ros_control_mode": integration.ros_control_mode,
            },
        )
    capability = db.get(RobotCapability, robot.id)
    if not capability:
        raise PlatformError(
            "CAPABILITY_DECLARATION_MISSING",
            "机器人尚未提供可信能力声明，拒绝执行车辆命令",
            details={"action": action},
        )
    readiness = robot_readiness(db, robot)
    if integration:
        if not integration.control_contract_verified:
            raise PlatformError(
                "CONTROL_CONTRACT_NOT_VERIFIED",
                integration.read_only_reason or "实车控制合同尚未验证，当前仅允许只读监控",
                details={"source_kind": integration.source_kind, "action": action},
            )
        if action in {"patrol", "extinguish", "return_dock", "cancel_task"} and not (
            integration.ack_contract_verified and integration.map_contract_verified
        ):
            raise PlatformError(
                "INTEGRATION_CONTRACT_INCOMPLETE",
                "ACK 或地图合同尚未验证，拒绝自动运动任务",
                details={
                    "ack_contract_verified": integration.ack_contract_verified,
                    "map_contract_verified": integration.map_contract_verified,
                },
            )
        if (
            action in {"emergency_stop", "stop_motion", "reset_estop"}
            and robot.online_state == "ONLINE"
            and not readiness["safety_command_ready"].get(action, False)
        ):
            raise PlatformError(
                "SAFETY_COMMAND_NOT_READY",
                "安全命令链路或对应车辆能力尚未验证",
                details={"action": action, "reasons": readiness["readiness_reasons"]},
            )
        if action == "manual_control" and not readiness["manual_control_ready"]:
            raise PlatformError(
                "MANUAL_CONTROL_NOT_READY",
                "手动控制链路、500ms watchdog 或运动参数尚未验证",
                details={"reasons": readiness["readiness_reasons"]},
            )
        if action in {"patrol", "extinguish", "return_dock", "cancel_task"} and not readiness[
            "autonomous_task_ready"
        ].get(action, False):
            raise PlatformError(
                "AUTONOMOUS_TASK_NOT_READY",
                "自动任务链路、地图或对应车辆能力尚未验证",
                details={"action": action, "reasons": readiness["readiness_reasons"]},
            )
    if action in {"patrol", "extinguish", "return_dock"} and get_redis().exists(
        f"manual:lease:{robot.id}"
    ):
        raise PlatformError("MANUAL_LEASE_CONFLICT", "机器人已有有效手动控制租约，请先显式释放")
    if action not in capability.supported_commands_json:
        raise PlatformError(
            "ROBOT_CAPABILITY_UNSUPPORTED",
            f"机器人能力声明不支持 {action}",
            details={"command": action},
        )
    active_query = select(Task).where(
        Task.robot_id == robot.id, Task.status.in_(ACTIVE_TASK_STATES)
    )
    if ignore_task_id:
        active_query = active_query.where(Task.id != ignore_task_id)
    active_task = db.scalar(active_query)
    if not active_task:
        return
    if action == "manual_control":
        raise PlatformError("ACTIVE_TASK_CONFLICT", "存在自动任务，请先显式取消任务再进入手动模式")
    if action == "patrol":
        raise PlatformError("ACTIVE_TASK_CONFLICT", "机器人已有活动业务任务")
    if action == "return_dock" and active_task.type == "EXTINGUISH":
        raise PlatformError("ACTIVE_TASK_CONFLICT", "灭火任务活动时不能回充")
    if action == "extinguish":
        raise PlatformError("ACTIVE_TASK_CONFLICT", "灭火任务不得静默覆盖现有任务，请先显式取消")


def build_command_payload(
    *,
    robot: Robot,
    operator_id: str,
    cmd: str,
    params: dict[str, Any],
    ttl_ms: int,
    priority: int,
    task_id: str | None = None,
    command_id: str | None = None,
    lease_id: str | None = None,
    control_session_id: str | None = None,
    seq: int = 0,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    if not robot.boot_id and cmd != "emergency_stop":
        raise PlatformError(
            "ROBOT_BOOT_SESSION_UNKNOWN",
            "机器人当前 boot 会话未知，拒绝发送命令",
            details={"vehicle_id": robot.vehicle_id, "command": cmd},
        )
    return {
        "schema_version": "1.2",
        "message_id": str(uuid4()),
        "type": "command",
        "vehicle_id": robot.vehicle_id,
        "target_boot_id": robot.boot_id,
        "command_id": command_id or command_code(),
        "correlation_id": str(uuid4()),
        "task_id": task_id,
        "lease_id": lease_id,
        "control_session_id": control_session_id,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(milliseconds=ttl_ms)).isoformat(),
        "ttl_ms": ttl_ms,
        "priority": priority,
        "source": "WEB",
        "operator_id": operator_id,
        "cmd": cmd,
        "params": params,
        **({"seq": seq} if cmd == "manual_control" else {}),
    }


def create_durable_command(
    db: Session,
    *,
    robot: Robot,
    operator_id: str,
    cmd: str,
    params: dict[str, Any],
    task_id: str | None = None,
    ttl_ms: int = 30_000,
    priority: int = 50,
) -> Command:
    assert_robot_can_execute(db, robot, cmd, ignore_task_id=task_id)
    payload = build_command_payload(
        robot=robot,
        operator_id=operator_id,
        cmd=cmd,
        params=params,
        ttl_ms=ttl_ms,
        priority=priority,
        task_id=task_id,
    )
    command = Command(
        command_id=payload["command_id"],
        correlation_id=payload["correlation_id"],
        robot_id=robot.id,
        task_id=task_id,
        cmd=cmd,
        priority=priority,
        payload_json=payload,
        lifecycle_status="QUEUED",
        issued_by=operator_id,
        issued_at=datetime.fromisoformat(payload["issued_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )
    db.add(command)
    db.flush()
    db.add(
        OutboxEvent(
            aggregate_type="COMMAND",
            aggregate_id=command.command_id,
            event_type="COMMAND_PUBLISH",
            payload_json=payload,
        )
    )
    return command


def create_safety_command(
    db: Session,
    *,
    robot: Robot,
    operator_id: str,
    cmd: str,
    params: dict[str, Any] | None = None,
    task_id: str | None = None,
    ttl_ms: int = 3000,
    priority: int = 95,
) -> tuple[Command, dict[str, Any]]:
    """Authorize and persist a safety command without publishing it.

    Publishing is deliberately performed only after the caller commits the
    database transaction, so an authorization or commit failure has zero
    broker/Redis side effects.
    """

    assert_robot_can_execute(db, robot, cmd, ignore_task_id=task_id)
    payload = build_command_payload(
        robot=robot,
        operator_id=operator_id,
        cmd=cmd,
        params=params or {},
        ttl_ms=ttl_ms,
        priority=priority,
        task_id=task_id,
    )
    deliverable = robot.online_state not in {"STALE", "OFFLINE"}
    command = Command(
        command_id=payload["command_id"],
        correlation_id=payload["correlation_id"],
        robot_id=robot.id,
        task_id=task_id,
        cmd=cmd,
        priority=priority,
        payload_json=payload,
        lifecycle_status="CREATED" if deliverable else "PUBLISHED_UNCONFIRMED",
        ack_reason=None if deliverable else "OFFLINE_NOT_DELIVERED",
        issued_by=operator_id,
        issued_at=datetime.fromisoformat(payload["issued_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )
    db.add(command)
    db.flush()
    return command, payload


def enqueue_safety_command(payload: dict[str, Any]) -> str:
    return str(
        get_redis().xadd(
            "firebot:safety_commands",
            {"command": json.dumps(payload, ensure_ascii=False)},
            maxlen=1000,
            approximate=True,
        )
    )
