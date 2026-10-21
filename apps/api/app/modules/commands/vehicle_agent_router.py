from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.dependencies import (
    AuthContext,
    CurrentAuth,
    DbSession,
    request_meta,
    require_permission,
)
from app.core.errors import PlatformError
from app.core.events import get_redis, queue_event, queue_redis_delete, queue_redis_set
from app.core.idempotency import lookup, store
from app.core.serialization import serialize_model
from app.db.models import (
    Command,
    Map,
    MapVersion,
    Robot,
    RobotCapability,
    RobotIntegrationProfile,
    Task,
)
from app.modules.commands.service import create_durable_command, task_code
from app.modules.robots.router import find_robot
from services.vehicle_agent_v1 import COMMANDS, PROTOCOL, WireCommand

router = APIRouter(prefix="/api/v1/robots", tags=["vehicle-agent"])


class ProtocolBinding(BaseModel):
    protocol: Literal["CANONICAL_MQTT", "VEHICLE_AGENT_V1", "ROS_COMPAT"]


class AgentCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: dict = Field(default_factory=dict)


@router.put("/{robot_id}/agent-protocol")
def bind_protocol(
    robot_id: str,
    payload: ProtocolBinding,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("settings.manage")),
) -> dict:
    found = find_robot(db, robot_id)
    robot = db.scalar(select(Robot).where(Robot.id == found.id).with_for_update())
    assert robot is not None
    if robot.agent_protocol == payload.protocol:
        return serialize_model(robot)
    if robot.online_state != "OFFLINE" or get_redis().exists(f"manual:lease:{robot.id}"):
        raise PlatformError("AGENT_BINDING_BUSY", "仅允许离线、无控制租约的车辆切换协议")
    pending = db.scalar(
        select(Command).where(
            Command.robot_id == robot.id,
            Command.lifecycle_status.in_(
                [
                    "CREATED",
                    "QUEUED",
                    "PUBLISHED",
                    "ACK_ACCEPTED",
                    "EXECUTING",
                    "PUBLISHED_UNCONFIRMED",
                ]
            ),
        )
    )
    active = db.scalar(
        select(Task).where(
            Task.robot_id == robot.id,
            Task.status.in_(["CREATED", "QUEUED", "ACCEPTED", "EXECUTING"]),
        )
    )
    if pending or active:
        raise PlatformError("AGENT_BINDING_BUSY", "请先完成或人工核销现有命令和任务")
    previous = robot.agent_protocol
    robot.agent_protocol = payload.protocol
    robot.agent_state_json, robot.boot_id, robot.last_seen_at = {}, None, None
    integration = db.get(RobotIntegrationProfile, robot.id)
    if not integration:
        integration = RobotIntegrationProfile(robot_id=robot.id)
        db.add(integration)
    integration.source_kind = payload.protocol
    integration.control_contract_verified = False
    integration.ack_contract_verified = False
    integration.map_contract_verified = False
    integration.bidirectional_bridge_verified = False
    integration.command_path_verified = False
    integration.cmd_vel_arbitration_verified = False
    integration.read_only_reason = "控制协议已切换，需重新验证控制、ACK、地图及安全合同"
    capability = db.get(RobotCapability, robot.id)
    if not capability:
        capability = RobotCapability(robot_id=robot.id)
        db.add(capability)
    capability.protocol_version = "vehicle-agent-v1" if payload.protocol == PROTOCOL else "unknown"
    capability.supported_commands_json = list(COMMANDS) if payload.protocol == PROTOCOL else []
    capability.sensors_json, capability.media_json = [], []
    queue_redis_delete(db, f"robot:{robot.vehicle_id}:latest")
    queue_redis_delete(db, f"vehicle-agent:binding:{robot.vehicle_id}")
    queue_redis_set(db, f"vehicle-agent:protocol:{robot.vehicle_id}", payload.protocol)
    write_audit(
        db,
        action="AGENT_PROTOCOL_BINDING",
        resource_type="ROBOT",
        resource_id=robot.id,
        robot_id=robot.id,
        user_id=auth.user.id,
        before={"agent_protocol": previous},
        after={"agent_protocol": payload.protocol},
        **request_meta(request),
    )
    db.flush()
    result = serialize_model(robot)
    queue_event(db, "robot.updated", result)
    db.commit()
    return result


@router.post("/{robot_id}/agent/commands/{action}", status_code=202)
def command(
    robot_id: str,
    action: str,
    payload: AgentCommandRequest,
    request: Request,
    db: DbSession,
    auth: CurrentAuth,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    if action not in COMMANDS:
        raise PlatformError("UNKNOWN_COMMAND", "未知 vehicle_agent 命令")
    permission = (
        "robot.read"
        if action in {"ping", "get_status", "get_telemetry"}
        else "robot.control.stop"
        if action in {"close", "stop_patrol", "stop_navigate"}
        else "patrol.create"
        if action in {"patrol", "resume_patrol"}
        else "robot.control.task"
    )
    if permission not in auth.permissions:
        raise PlatformError("PERMISSION_DENIED", "当前用户没有执行此操作的权限", status_code=403)
    found = find_robot(db, robot_id)
    robot = db.scalar(select(Robot).where(Robot.id == found.id).with_for_update())
    assert robot is not None
    endpoint = f"/robots/{robot.id}/agent/commands/{action}"
    body = payload.model_dump()
    cached = lookup(db, actor_id=auth.user.id, endpoint=endpoint, key=idempotency_key, payload=body)
    if cached:
        return cached.response_json
    # Validate before creating Task/Outbox; a malformed command has no side effects.
    from datetime import UTC, datetime
    from uuid import uuid4

    try:
        WireCommand(msg_id=uuid4(), command=action, params=payload.params, ts=datetime.now(UTC))
    except ValueError as exc:
        raise PlatformError("INVALID_PARAM", "v1 命令参数不符合合同") from exc
    if robot.agent_protocol != PROTOCOL:
        raise PlatformError("AGENT_PROTOCOL_MISMATCH", "该车辆未绑定 vehicle_agent v1")
    outstanding = db.scalar(
        select(Command).where(
            Command.robot_id == robot.id,
            Command.lifecycle_status.in_(["CREATED", "QUEUED", "PUBLISHED", "ACK_ACCEPTED"]),
        )
    )
    same_pending = db.scalar(
        select(Command).where(
            Command.robot_id == robot.id,
            Command.cmd == action,
            Command.lifecycle_status.in_(["CREATED", "QUEUED", "PUBLISHED", "ACK_ACCEPTED"]),
        )
    )
    if same_pending or (
        outstanding
        and action not in {"ping", "get_status", "get_telemetry", "stop_patrol", "stop_navigate"}
    ):
        raise PlatformError("COMMAND_PENDING", "已有命令等待 ACK 或状态确认")
    uncertain = db.scalar(
        select(Command).where(
            Command.robot_id == robot.id,
            Command.cmd == action,
            Command.lifecycle_status == "PUBLISHED_UNCONFIRMED",
        )
    )
    if uncertain:
        raise PlatformError("COMMAND_UNCONFIRMED", "同一操作的执行结果尚未确认，禁止重复发送")
    active = db.scalar(
        select(Task).where(
            Task.robot_id == robot.id,
            Task.status.in_(["CREATED", "QUEUED", "ACCEPTED", "EXECUTING"]),
        )
    )
    task_id = None
    if action in {"patrol", "resume_patrol", "navigate", "homing"}:
        if active:
            raise PlatformError("ACTIVE_TASK_CONFLICT", "机器人已有活动业务任务")
        map_row = db.get(Map, robot.current_map_id) if robot.current_map_id else None
        version = (
            db.get(MapVersion, map_row.active_version_id)
            if map_row and map_row.active_version_id
            else None
        )
        if not version or not map_row:
            raise PlatformError("MAP_CONTRACT_NOT_VERIFIED", "车辆没有已发布地图")
        slot_id = None
        if action == "navigate":
            from app.db.models import ParkingSlot

            slot = db.scalar(
                select(ParkingSlot).where(
                    ParkingSlot.map_version_id == version.id,
                    ParkingSlot.code == payload.params["spot"],
                    ParkingSlot.enabled.is_(True),
                )
            )
            if not slot:
                raise PlatformError("INVALID_PARAM", "目标车位不属于当前已发布地图")
            slot_id = slot.id
        task = Task(
            task_code=task_code(),
            robot_id=robot.id,
            type={
                "patrol": "V1_PATROL",
                "resume_patrol": "V1_PATROL",
                "homing": "V1_HOMING",
                "navigate": "V1_NAVIGATE",
            }[action],
            status="QUEUED",
            phase="WAITING_ACK",
            target_parking_slot_id=slot_id,
            map_id_snapshot=map_row.id,
            map_version_snapshot=version.version,
            semantic_revision_snapshot=version.semantic_revision,
            parameters_json={"v1_action": action},
            created_by=auth.user.id,
        )
        db.add(task)
        db.flush()
        task_id = task.id
        queue_event(db, "task.updated", serialize_model(task))
    elif active:
        task_id = active.id
    row = create_durable_command(
        db,
        robot=robot,
        operator_id=auth.user.id,
        cmd=action,
        params=payload.params,
        task_id=task_id,
        ttl_ms=get_settings().vehicle_agent_v1_ack_timeout_seconds * 1000,
    )
    db.flush()
    result = serialize_model(row)
    store(
        db,
        actor_id=auth.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        payload=body,
        response=result,
        status_code=202,
    )
    write_audit(
        db,
        action=f"VEHICLE_AGENT_{action.upper()}",
        resource_type="COMMAND",
        resource_id=row.command_id,
        robot_id=robot.id,
        user_id=auth.user.id,
        after={"command": action, "params": payload.params},
        **request_meta(request),
    )
    queue_event(db, "command.updated", result)
    db.commit()
    return result
