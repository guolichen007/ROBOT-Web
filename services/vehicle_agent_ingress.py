"""v1 normalized ingress extension; reuse platform DB, cache, task and WS transactions."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.events import get_redis, queue_event, queue_redis_set
from app.core.serialization import serialize_model
from app.core.service_identity import in_v1_scope
from app.db.models import (
    Command,
    Map,
    MapVersion,
    Robot,
    RobotDataChannel,
    SensorSample,
    Task,
    TaskEvent,
)
from app.db.session import SessionLocal
from app.modules.commands.readiness import robot_readiness
from sqlalchemy import select

from services.vehicle_agent_v1 import (
    NOT_EXECUTED_CODES,
    PROTOCOL,
    Ack,
    normalize,
    status_confirms,
)

logger = logging.getLogger("vehicle-agent-ingress")

INTERNAL = "_platform/vehicle-agent-v1/"


def fresh_status_time(robot: Robot, received: datetime) -> datetime | None:
    if (
        robot.online_state != "ONLINE"
        or (robot.agent_state_json or {}).get("availability") == "offline"
    ):
        return None
    value = (robot.agent_state_json or {}).get("status_received_at")
    if not value:
        return None
    timestamp = datetime.fromisoformat(value)
    age = (received - timestamp).total_seconds()
    return timestamp if -2 <= age <= get_settings().vehicle_agent_v1_status_stale_seconds else None


def converge_commands(db, robot: Robot, received: datetime) -> None:
    state = robot.agent_state_json or {}
    status = state.get("status", {})
    status_at = fresh_status_time(robot, received)
    for command in db.scalars(
        select(Command).where(
            Command.robot_id == robot.id,
            Command.lifecycle_status.in_(["ACK_ACCEPTED", "EXECUTING"]),
        )
    ).all():
        if command.payload_json.get("schema_version") != "vehicle-agent-v1":
            continue
        software = command.payload_json.get("execution_mode") == "SOFTWARE_TEST"
        if command.lifecycle_status != "ACK_ACCEPTED" and not software:
            continue
        if software:
            # 软件测试命令只由车端 ROS 软件反馈收敛（不得用底盘 programs 状态冒充）；
            # 车端在 ACK 后未给出终态反馈 → 超时失败（结果未确认，不宣称成功）。
            # 截止从最后一次真实软件反馈起算：长巡检持续有 RUNNING 反馈时不得被判超时失败
            history = (command.ack_payload_json or {}).get("software_feedback") or []
            last = max(
                [command.expires_at]
                + [datetime.fromisoformat(item["received_at"]) for item in history],
            )
            deadline = last + timedelta(seconds=get_settings().vehicle_agent_v1_ack_timeout_seconds)
            if received > deadline:
                command.lifecycle_status = "FAILED"
                command.ack_reason = "SOFTWARE_FEEDBACK_TIMEOUT"
                command.terminal_at = received
                queue_event(db, "command.updated", serialize_model(command))
            continue
        query = command.cmd in {"ping", "get_status", "get_telemetry"}
        if not query and (not status_at or status_at < command.issued_at):
            continue
        if status_confirms(command.cmd, status):
            moving = command.cmd in {"patrol", "resume_patrol", "homing", "navigate"}
            command.lifecycle_status = "EXECUTING" if moving else "SUCCEEDED"
            command.terminal_at = None if moving else received
            queue_event(db, "command.updated", serialize_model(command))


def converge_tasks(db, robot: Robot, received: datetime) -> None:
    status = (robot.agent_state_json or {}).get("status", {})
    status_at = fresh_status_time(robot, received)
    observed = status.get("task", {})
    for task in db.scalars(
        select(Task).where(
            Task.robot_id == robot.id,
            Task.type.like("V1_%"),
            Task.status.in_(["QUEUED", "ACCEPTED", "EXECUTING"]),
        )
    ).all():
        params = dict(task.parameters_json or {})
        expected = {"V1_PATROL": "patrol", "V1_HOMING": "homing", "V1_NAVIGATE": "spot"}.get(
            task.type
        )
        commands = db.scalars(select(Command).where(Command.task_id == task.id)).all()
        initiating = next(
            (c for c in commands if c.cmd in {"patrol", "resume_patrol", "navigate", "homing"}),
            None,
        )
        if not initiating:
            continue
        if (
            not status_at
            or status_at < initiating.issued_at
            or initiating.payload_json.get("target_boot_id") != robot.boot_id
            or not initiating.published_at
        ):
            continue
        stop_confirmed = any(
            c.cmd in {"stop_patrol", "stop_navigate"}
            and c.ack_status == "accepted"
            and status_at >= c.issued_at
            and status_confirms(c.cmd, status)
            for c in commands
        )
        if initiating.ack_status == "rejected" and not stop_confirmed:
            continue
        ack_missing = initiating.ack_status != "accepted"
        if ack_missing:
            params["ack_diagnostic"] = "ACK_MISSING"
            # Preserve transport truth on Command; only Task reflects observation.
            initiating.ack_reason = "ACK_MISSING"
            queue_event(db, "command.updated", serialize_model(initiating))
        # An ACK only accepts a goal. Observing spot/homing/patrol proves execution;
        # only a subsequent idle status after execution can complete the task.
        old_status = task.status
        if stop_confirmed:
            task.status = "CANCELLED"
            task.phase = "PAUSED" if observed.get("patrol_paused") else "STOPPED"
            task.completed_at = received
        elif observed.get("state") == expected:
            params["observed_running"] = True
            task.status = "EXECUTING"
            task.phase = expected.upper()
            task.started_at = task.started_at or received
        elif observed.get("patrol_paused") and task.type == "V1_PATROL":
            task.status = "CANCELLED"
            task.phase = "PAUSED"
            task.completed_at = received
        elif params.get("observed_running") and observed.get("state") == "idle":
            stopped = any(
                c.cmd in {"stop_navigate", "stop_patrol", "close"} and c.ack_status == "accepted"
                for c in commands
            )
            task.status = "CANCELLED" if stopped else "SUCCEEDED"
            task.phase = "STOPPED" if stopped else "COMPLETED"
            task.progress = task.progress if stopped else 100
            task.completed_at = received
        task.parameters_json = params
        if old_status != task.status:
            if task.status in {"SUCCEEDED", "CANCELLED"} and not ack_missing:
                initiating.lifecycle_status = task.status
                initiating.terminal_at = received
                queue_event(db, "command.updated", serialize_model(initiating))
            db.add(
                TaskEvent(
                    task_id=task.id,
                    status=task.status,
                    phase=task.phase,
                    progress=task.progress,
                    payload_json={"v1_status": status},
                )
            )
            queue_event(db, "task.updated", serialize_model(task))


def handle_v1_ack(db, robot: Robot, data: dict, received: datetime, handle_ack) -> None:
    ack = Ack.model_validate({k: v for k, v in data.items() if k in Ack.model_fields})
    command = db.scalar(
        select(Command)
        .where(
            Command.robot_id == robot.id,
            Command.command_id == data["command_id"],
        )
        .with_for_update()
    )
    if (
        not command
        or command.ack_at
        or command.payload_json.get("schema_version") != "vehicle-agent-v1"
    ):
        return
    payload = command.payload_json
    if payload["message_id"] != str(ack.msg_id) or payload["message_id"] != data["message_id"]:
        return
    if payload["target_boot_id"] != data["target_boot_id"]:
        return
    # 车端确定未执行的拒绝（旧会话/过期/跨设备/未授权）在会话已切换后仍须落库，
    # 否则命令会永久停留 PUBLISHED；拒绝不宣称任何执行，因此不受当前会话限制。
    definitive_reject = (
        data.get("ack_source", "vehicle") == "vehicle"
        and not ack.accepted
        and ack.code in NOT_EXECUTED_CODES
    )
    if robot.boot_id != data["target_boot_id"] and not definitive_reject:
        return
    synthetic = data.get("ack_source") == "transport_timeout"
    if synthetic:
        command.ack_payload_json = {
            **ack.model_dump(mode="json"),
            "ack_source": "transport_timeout",
        }
        command.lifecycle_status = "PUBLISHED_UNCONFIRMED"
        command.ack_reason = "ACK_MISSING"
        command.terminal_at = None
        queue_event(db, "command.updated", serialize_model(command))
        converge_tasks(db, robot, received)
        return
    previous_feedback = (command.ack_payload_json or {}).get("software_feedback")
    command.ack_payload_json = {
        **ack.model_dump(mode="json"),
        "ack_source": "vehicle",
        **({"software_feedback": previous_feedback} if previous_feedback else {}),
    }
    handle_ack(
        db,
        robot,
        {
            "command_id": command.command_id,
            "status": "accepted" if ack.accepted else "rejected",
            "reason_code": ack.code,
            "reason": ack.message,
        },
        received,
    )
    if command.task_id:
        task = db.get(Task, command.task_id)
        if (
            task
            and task.status in {"CANCELLED", "SUCCEEDED"}
            and command.cmd in {"patrol", "resume_patrol", "homing", "navigate"}
        ):
            command.lifecycle_status = task.status if ack.accepted else "ACK_REJECTED"
            command.terminal_at = task.completed_at
            queue_event(db, "command.updated", serialize_model(command))
        elif (
            task
            and task.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}
            and command.cmd in {"patrol", "resume_patrol", "homing", "navigate"}
        ):
            task.status = "ACCEPTED" if ack.accepted else "FAILED"
            task.accepted_at = received if ack.accepted else None
            if not ack.accepted:
                task.failure_code, task.failure_message = ack.code, ack.message
                task.completed_at = received
            queue_event(db, "task.updated", serialize_model(task))
    if ack.accepted and previous_feedback:
        # 软件终态反馈先于 ACK 到达（不同 topic 无序）：ACK 落库后补收敛
        apply_software_feedback(db, command, previous_feedback[-1], received)
    converge_tasks(db, robot, received)
    converge_commands(db, robot, received)


# STOPPED：被停止命令的真实终态（不是失败，不以超时代替）
SOFTWARE_TERMINAL = {
    "COMPLETED": "SUCCEEDED",
    "FAILED": "FAILED",
    "REJECTED": "FAILED",
    "STOPPED": "CANCELLED",
}


def apply_software_feedback(db, command: Command, feedback: dict, received: datetime) -> None:
    terminal = SOFTWARE_TERMINAL.get(feedback.get("state"))
    if command.lifecycle_status not in {"ACK_ACCEPTED", "EXECUTING"}:
        return
    if terminal:
        command.lifecycle_status = terminal
        command.terminal_at = received
        if terminal == "FAILED":
            command.ack_reason = feedback.get("reason_code") or "SOFTWARE_FAILED"
        elif terminal == "CANCELLED":
            command.ack_reason = "STOPPED"
    else:
        command.lifecycle_status = "EXECUTING"
    queue_event(db, "command.updated", serialize_model(command))


def handle_software_feedback(db, robot: Robot, data: dict, received: datetime) -> None:
    """ROS 软件反馈 → Command（仅 SOFTWARE_TEST；必须与已下发命令及当前会话精确关联）。"""
    command = db.scalar(
        select(Command)
        .where(Command.robot_id == robot.id, Command.command_id == data.get("command_id"))
        .with_for_update()
    )
    if not command or command.payload_json.get("execution_mode") != "SOFTWARE_TEST":
        return
    payload = command.payload_json
    if (
        payload["message_id"] != data.get("message_id")
        or str(data.get("msg_id")) != payload["message_id"]
        or payload["target_boot_id"] != data.get("target_boot_id")
    ):
        return
    if data.get("software_test") is not True and data.get("state") in {"COMPLETED", "STOPPED"}:
        # 不可信反馈不得驱动成功/停止终态
        data = {**data, "state": "FAILED", "reason_code": "UNTRUSTED_SOFTWARE_FEEDBACK"}
    entry = {
        key: data.get(key)
        for key in (
            "state",
            "ros_status",
            "reason_code",
            "message",
            "phase",
            "ros_ts",
            "duration_ms",
            "stopped_command_id",
        )
    }
    if entry.get("stopped_command_id"):
        # 车端以 V1 msg_id 关联被停止的命令；云端追溯到业务 command_id
        stopped = next(
            (
                row
                for row in db.scalars(
                    select(Command).where(Command.robot_id == robot.id, Command.cmd == "patrol")
                ).all()
                if row.payload_json.get("message_id") == entry["stopped_command_id"]
                and row.payload_json.get("target_boot_id") == payload["target_boot_id"]
                and row.payload_json.get("execution_mode") == "SOFTWARE_TEST"
            ),
            None,
        )
        entry["stopped_cloud_command_id"] = stopped.command_id if stopped else None
    if command.cmd == "stop_patrol" and entry["state"] == "COMPLETED":
        # 云端独立复核：stop 成功只能来自可信 STOPPED，且必须精确关联到同车同会话的
        # SOFTWARE_TEST patrol；缺失/未知/跨会话/REAL 目标一律不得宣称停止成功。
        if entry.get("ros_status") != "STOPPED" or not entry.get("stopped_cloud_command_id"):
            entry["state"] = "FAILED"
            entry["reason_code"] = "STOP_TARGET_MISMATCH"
    entry["received_at"] = received.isoformat()
    ack_payload = dict(command.ack_payload_json or {})
    history = ack_payload.get("software_feedback", [])
    if any(
        item.get("state") == entry["state"] and item.get("ros_ts") == entry["ros_ts"]
        for item in history
    ):
        return  # 车端重放（ACK 丢失后的原结果重发）不重复记录
    ack_payload["software_feedback"] = [*history, entry][-10:]
    command.ack_payload_json = ack_payload
    if command.ack_at:
        apply_software_feedback(db, command, entry, received)
    else:
        queue_event(db, "command.updated", serialize_model(command))


def process(name: str, payload: bytes, handle_ack) -> bool:
    if not name.startswith(INTERNAL):
        return False
    parts = name.split("/")
    if len(parts) != 4 or parts[-1] == "command":
        return True
    envelope = json.loads(payload)
    UUID(envelope["event_id"])
    vehicle, kind = parts[2], parts[3]
    if not in_v1_scope(vehicle):
        return True
    if vehicle != envelope["vehicle_id"] or kind != envelope["kind"]:
        raise ValueError("internal vehicle/topic mismatch")
    if len(payload) > 262144:
        raise ValueError("internal payload too large")
    redis = get_redis()
    dedup = f"vehicle-agent:ingress:{envelope['event_id']}"
    if redis.exists(dedup):
        return True
    received = datetime.now(UTC)
    data = envelope["data"]
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle).with_for_update())
        if not robot or robot.agent_protocol != PROTOCOL:
            return True
        if kind == "ack":
            handle_v1_ack(db, robot, data, received, handle_ack)
        else:
            data = normalize(kind, data)
            state = dict(robot.agent_state_json or {})
            previous_online = robot.online_state
            # G2 会话合同：车端自报会话 ID（双向执行器每次 MQTT 会话新生成）为唯一事实源，
            # 云端直接采纳——车端只校验自己生成过的 UUID。retained 旧会话不得采纳。
            # 未上报（只读 Agent）沿用原云端生成逻辑。
            vehicle_boot = data.get("boot_id") if kind in {"status", "availability"} else None
            if vehicle_boot and not envelope.get("retained"):
                if robot.boot_id != vehicle_boot:
                    logger.info(
                        "v1 vehicle session adopted (vehicle=%s boot=%s)", vehicle, vehicle_boot
                    )
                    robot.boot_id = vehicle_boot
                    if kind == "availability":
                        # 新会话：旧 status 不代表新会话事实
                        state["status_received_at"] = None
                state["boot_source"] = "vehicle"
            elif (
                kind == "status"
                and not envelope.get("retained")
                and state.get("boot_source") == "vehicle"
            ):
                # 回退到不上报会话的 Agent（如只读模式）：退出车端会话合同，新建云端会话，
                # 旧车端会话的待发命令随 boot 变化失效。
                robot.boot_id = str(uuid4())
                state["boot_source"] = "cloud"
            if not robot.boot_id:
                robot.boot_id = str(uuid4())
            if kind in {"status", "availability"}:
                # G1-P2-L0 A：旧 retained 数据不是当前事实。retained online/status
                # 一律不得复活 OFFLINE、不得刷新 last_seen_at/server_received_at；
                # 仅 retained LWT offline（权威离线）继续生效。新鲜 live 消息恢复 ONLINE。
                if envelope.get("retained"):
                    if kind == "status":
                        logger.warning(
                            "v1 retained status dropped (stale data, vehicle=%s)", vehicle
                        )
                        return True
                    if data.get("state") == "online":
                        logger.warning(
                            "v1 retained online dropped (stale data, vehicle=%s)", vehicle
                        )
                        return True
                online = data.get("state") == "online" if kind == "availability" else data["online"]
                # Explicit offline remains authoritative until availability online.
                if kind == "status" and state.get("availability") == "offline":
                    online = False
                if kind == "availability":
                    state["availability"] = data["state"]
                robot.online_state = "ONLINE" if online else "OFFLINE"
                if not online:
                    state["telemetry_online"] = False
                robot.last_seen_at = received if online else None
                if online and previous_online == "OFFLINE":
                    if state.get("boot_source") != "vehicle":
                        robot.boot_id = str(uuid4())
                    # Availability proves connectivity, never the new session's
                    # chassis/navigation state. Wait for a post-reconnect status.
                    state["status_received_at"] = None
                if kind == "status":
                    state["status"] = data
                    state["status_received_at"] = data.get("ts") or (
                        None if envelope.get("retained") else received.isoformat()
                    )
                    task_state = data["task"]["state"]
                    robot.current_mode = {
                        "idle": "IDLE",
                        "patrol": "PATROL",
                        "homing": "RETURN_DOCK",
                        "spot": "NAVIGATING",
                    }[task_state]
            elif kind == "telemetry":
                source_at = datetime.fromisoformat(data["ts"]) if data.get("ts") else received
                age = (received - source_at).total_seconds()
                # G3：遥测“实时值”只接受新鲜、非 retained、且不早于当前实时值的消息。
                # 过期（> stale 门限）/ 时钟超前 / retained / 乱序更旧的遥测一律不覆盖
                # 实时 telemetry、Redis latest（top_ir 等）与实时 SensorSample。
                previous_ts = (state.get("telemetry") or {}).get("ts")
                older = bool(
                    previous_ts
                    and data.get("ts")
                    and datetime.fromisoformat(data["ts"]) < datetime.fromisoformat(previous_ts)
                )
                stale_limit = get_settings().vehicle_agent_v1_status_stale_seconds
                if envelope.get("retained") or not -2 <= age <= stale_limit or older:
                    logger.warning(
                        "v1 telemetry not applied as live (vehicle=%s retained=%s age=%.1fs "
                        "older=%s)",
                        vehicle,
                        bool(envelope.get("retained")),
                        age,
                        older,
                    )
                    queue_redis_set(db, dedup, "1", ttl_seconds=300)
                    return True
                if (
                    not envelope.get("retained")
                    and state.get("availability") != "offline"
                    and -2 <= age <= get_settings().vehicle_agent_v1_status_stale_seconds
                ):
                    robot.last_seen_at = received
                    robot.online_state = "ONLINE"
                state["telemetry_online"] = robot.online_state == "ONLINE"
                state["telemetry"] = data
                state["telemetry_received_at"] = received.isoformat()
                timestamp = datetime.fromisoformat(data["ts"]) if data.get("ts") else None
                delay = (received - timestamp).total_seconds() * 1000 if timestamp else None
                state["receive_latency_ms"] = (
                    round(delay, 1) if delay is not None and delay >= 0 else None
                )
                robot.battery = (data.get("battery") or {}).get("percentage")
                if redis.set(f"vehicle-agent:sample:{vehicle}", "1", ex=1, nx=True):
                    db.add(
                        SensorSample(
                            robot_id=robot.id,
                            source_timestamp=timestamp or received,
                            server_received_at=received,
                            smoke=(data.get("smog") or {}).get("percentage"),
                            top_ir_max=(data.get("thermal") or {}).get("max_temp"),
                            payload_json=data,
                            boot_id=robot.boot_id,
                            seq=0,
                        )
                    )
            elif kind == "event":
                if data.get("type") == "software_feedback":
                    handle_software_feedback(db, robot, data, received)
                write_audit(
                    db,
                    action="VEHICLE_AGENT_EVENT",
                    resource_type="ROBOT",
                    resource_id=robot.id,
                    robot_id=robot.id,
                    actor_type="SYSTEM",
                    after=data,
                )
                queue_event(db, "vehicle.agent_event", {"vehicle_id": vehicle, "event": data})
            robot.agent_state_json = state
            db.flush()
            raw = redis.get(f"robot:{vehicle}:latest")
            latest = json.loads(raw) if raw else {}
            latest.update(
                {
                    "id": robot.id,
                    "robot_id": robot.id,
                    "vehicle_id": vehicle,
                    "agent_protocol": PROTOCOL,
                    "agent_state": state,
                    "online_state": robot.online_state,
                    "boot_id": robot.boot_id,
                    "mode": robot.current_mode,
                    # 链路 freshness 依据：任何一条被服务端确认的上行消息都刷新
                    # （status 3s / telemetry 30s 解耦；硬件字段 freshness 见 data_channels）
                    "server_received_at": received.isoformat(),
                }
            )
            if kind == "telemetry":
                latest.update(
                    {
                        "battery": robot.battery,
                        "smoke": (data.get("smog") or {}).get("percentage"),
                        "top_ir": (data.get("thermal") or {}).get("max_temp"),
                        "source_timestamp": data.get("ts"),
                    }
                )
                pose = data.get("pose")
                map_row = db.get(Map, robot.current_map_id) if robot.current_map_id else None
                version = (
                    db.get(MapVersion, map_row.active_version_id)
                    if map_row and map_row.active_version_id
                    else None
                )
                if pose and version and pose["frame_id"] == version.frame_id:
                    latest.update(
                        {
                            "x": pose["x"],
                            "y": pose["y"],
                            "theta": pose["yaw"],
                            "frame_id": pose["frame_id"],
                        }
                    )
                else:
                    for field in ("x", "y", "theta", "frame_id"):
                        latest.pop(field, None)
            channel = db.scalar(
                select(RobotDataChannel).where(
                    RobotDataChannel.robot_id == robot.id, RobotDataChannel.channel == kind
                )
            )
            if not channel:
                channel = RobotDataChannel(robot_id=robot.id, channel=kind)
                db.add(channel)
            channel.support_state = (
                "CONNECTED" if robot.online_state == "ONLINE" else "NOT_CONNECTED"
            )
            channel.source_kind, channel.quality = PROTOCOL, "GOOD"
            channel.last_received_at = received
            latest.update(robot_readiness(db, robot))
            queue_redis_set(db, f"robot:{vehicle}:latest", json.dumps(latest, ensure_ascii=False))
            queue_event(db, "vehicle.agent_state", latest)
            if robot.online_state != previous_online:
                queue_event(db, f"vehicle.{robot.online_state.lower()}", latest)
            if kind == "status":
                converge_tasks(db, robot, received)
                converge_commands(db, robot, received)
        queue_redis_set(db, dedup, "1", ttl_seconds=300)
    return True
