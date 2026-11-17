"""vehicle_agent v1 wire contract. Pure translation; no authorization or business DB."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

PROTOCOL = "VEHICLE_AGENT_V1"
SCHEMA = "vehicle-agent-v1"
COMMANDS = (
    "start",
    "close",
    "patrol",
    "stop_patrol",
    "resume_patrol",
    "homing",
    "navigate",
    "stop_navigate",
    "ping",
    "get_status",
    "get_telemetry",
)
BUSINESS_COMMANDS = COMMANDS[:8]
ALREADY_CODES = {"ALREADY_RUNNING", "ALREADY_CLOSED", "ALREADY_STOPPED"}
ACK_CODES = (
    "OK",
    "ALREADY_RUNNING",
    "ALREADY_CLOSED",
    "ALREADY_STOPPED",
    "INVALID_PARAM",
    "UNKNOWN_COMMAND",
    "NOT_READY",
    "BUSY",
    "START_FAILED",
    "TIMEOUT",
    "ROS_UNAVAILABLE",
    "INTERNAL",
    # G2 车端独立校验拒绝码（协议扩展；旧码语义不变）
    "EXPIRED",
    "STALE_BOOT",
    "WRONG_VEHICLE",
    "NOT_AUTHORIZED",
    "MODE_MISMATCH",
    "NO_HARDWARE",
)
# 车端会话/时效拒绝：命令确定未执行（与 TIMEOUT“结果未知”不同）。
NOT_EXECUTED_CODES = {"EXPIRED", "STALE_BOOT", "WRONG_VEHICLE", "NOT_AUTHORIZED", "MODE_MISMATCH"}
DIAGNOSTIC_COMMANDS = ("ping", "get_status", "get_telemetry")
SOFTWARE_TEST_COMMANDS = ("start", "close", "patrol", "stop_patrol")
# 执行模式：DIAGNOSTIC（只读诊断）/ SOFTWARE_TEST（仅 ROS 软件状态机，无硬件）/ REAL（真实整车）
EXECUTION_MODES = ("DIAGNOSTIC", "SOFTWARE_TEST", "REAL")
# 逐车执行能力档（G2）：命令 → 执行模式。未列出的命令一律拒绝。
# 档位只允许逐级显式授予；software-test 档绝不包含真实底盘/导航动作。
EXECUTION_PROFILES: dict[str, dict[str, str]] = {
    "telemetry-only": {},
    "diagnostic": dict.fromkeys(DIAGNOSTIC_COMMANDS, "DIAGNOSTIC"),
    "ros-software-test": {
        **dict.fromkeys(DIAGNOSTIC_COMMANDS, "DIAGNOSTIC"),
        **dict.fromkeys(SOFTWARE_TEST_COMMANDS, "SOFTWARE_TEST"),
    },
}
# 车端软件执行反馈（沿用 FIREBOT_BRIDGE_CONTRACT §3.2 枚举）
SOFTWARE_FEEDBACK_STATES = ("ACCEPTED", "EXECUTING", "COMPLETED", "REJECTED", "FAILED", "STOPPED")
TOPIC_CONTRACT = {
    "cmd": (1, False),
    "ack": (1, False),
    "status": (1, True),
    "telemetry": (0, False),
    "availability": (1, True),
    "event": (1, False),
}
VEHICLE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def topic(vehicle_id: str, kind: str) -> str:
    if not VEHICLE_ID.fullmatch(vehicle_id) or kind not in TOPIC_CONTRACT:
        raise ValueError("invalid vehicle_agent topic")
    return f"v1/robot/{vehicle_id}/{kind}"


def parse_topic(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 4 or parts[:2] != ["v1", "robot"]:
        raise ValueError("invalid vehicle_agent topic")
    topic(parts[2], parts[3])
    return parts[2], parts[3]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WireCommand(WireModel):
    msg_id: UUID
    command: Literal[
        "start",
        "close",
        "patrol",
        "stop_patrol",
        "resume_patrol",
        "homing",
        "navigate",
        "stop_navigate",
        "ping",
        "get_status",
        "get_telemetry",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    ts: AwareDatetime
    # G2 会话合同（向后兼容的可选扩展）：车端据此独立拒绝跨设备/旧会话/过期命令，
    # 不得仅依赖云端校验。新执行器要求四项全部存在（缺失即拒绝）。
    vehicle_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    target_boot_id: UUID | None = None
    expires_at: AwareDatetime | None = None
    mode: Literal["DIAGNOSTIC", "SOFTWARE_TEST", "REAL"] | None = None

    @model_validator(mode="after")
    def validate_params(self) -> WireCommand:
        if self.expires_at is not None and self.expires_at <= self.ts:
            raise ValueError("expires_at must be after ts")
        if self.command == "start":
            if (
                set(self.params) != {"with_navigation"}
                or self.params.get("with_navigation") is not True
            ):
                raise ValueError("start requires with_navigation=true")
        elif self.command == "navigate":
            if set(self.params) != {"spot"} or not isinstance(self.params["spot"], str):
                raise ValueError("navigate requires a spot name")
            if not self.params["spot"].strip() or len(self.params["spot"]) > 128:
                raise ValueError("invalid spot")
        elif self.command == "patrol" and self.mode == "SOFTWARE_TEST" and self.params:
            # G2-R1：001 软件巡检契约支持 {"rounds": N}（仅 SOFTWARE_TEST；真实巡检仍要求空参数）
            rounds = self.params.get("rounds")
            if (
                set(self.params) != {"rounds"}
                or isinstance(rounds, bool)
                or not isinstance(rounds, int)
                or not 1 <= rounds <= 1000
            ):
                raise ValueError("software patrol accepts only rounds (1..1000)")
        elif self.params:
            raise ValueError("command requires empty params")
        return self


class Ack(WireModel):
    msg_id: UUID
    success: bool = Field(strict=True)
    code: str
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = Field(default=0, ge=0)
    ts: AwareDatetime

    @model_validator(mode="after")
    def validate_code(self) -> Ack:
        if self.code not in ACK_CODES:
            raise ValueError("unknown ACK code")
        return self

    @property
    def accepted(self) -> bool:
        return self.code in ALREADY_CODES or (self.success and self.code == "OK")


class CanonicalCommand(WireModel):
    schema_version: Literal["vehicle-agent-v1"]
    type: Literal["command"]
    message_id: UUID
    vehicle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    target_boot_id: UUID
    command_id: str = Field(min_length=1)
    correlation_id: UUID
    task_id: str | None = None
    lease_id: str | None = None
    control_session_id: str | None = None
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    ttl_ms: int = Field(gt=0, le=240000)
    priority: int = Field(ge=0, le=100)
    source: Literal["WEB"]
    operator_id: str = Field(min_length=1)
    cmd: str
    params: dict[str, Any]
    # G2：执行模式由 API 依据逐车执行档计算；Dispatcher/Adapter/车端各自复核。
    execution_mode: Literal["DIAGNOSTIC", "SOFTWARE_TEST", "REAL"] | None = None


class UplinkModel(WireModel):
    # status/telemetry 合同列出的是最小字段集；忽略扩展元数据，不赋予其控制含义。
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class AgentTask(UplinkModel):
    state: Literal["idle", "patrol", "homing", "spot"]
    patrol_next: str | int | None = None
    patrol_count: int | None = Field(default=None, ge=0)
    patrol_paused: bool = Field(default=False, strict=True)


class Status(UplinkModel):
    online: bool = Field(strict=True)
    programs: dict[str, bool | str | None] = Field(default_factory=dict)
    task: AgentTask
    ts: AwareDatetime | None = None
    # G2：车端自生成的会话 ID（每次 MQTT 会话建立时新生成）；云端采纳为 robot.boot_id。
    boot_id: UUID | None = None
    # G2：ROS 软件测试状态（SOFTWARE_TEST，非真实底盘）；仅展示，不参与任务收敛。
    software: dict[str, Any] | None = None


class Pose(UplinkModel):
    x: float
    y: float
    yaw: float
    frame_id: str


class Battery(UplinkModel):
    percentage: float | None = Field(default=None, ge=0, le=100)
    voltage: float | None = None
    current: float | None = None
    temperature: float | None = None
    capacity: float | None = None
    charge_state: str | int | None = None


class Smog(UplinkModel):
    percentage: float | None = Field(default=None, ge=0, le=100)
    raw: float | None = None
    alarm: bool | None = None


class Thermal(UplinkModel):
    # G3：严格数值（拒绝字符串/布尔隐式转换），物理合理范围（热像仪测温量程外即非法）
    max_temp: float | None = Field(default=None, strict=True, ge=-50, le=1200)

    @model_validator(mode="before")
    @classmethod
    def int_is_number(cls, values: Any) -> Any:
        # strict float 不接受 int；JSON 整数温度（如 42）是合法数值，布尔不是
        if isinstance(values, dict):
            value = values.get("max_temp")
            if isinstance(value, int) and not isinstance(value, bool):
                values = {**values, "max_temp": float(value)}
        return values


class Telemetry(UplinkModel):
    pose: Pose | None = None
    battery: Battery | None = None
    smog: Smog | None = None
    thermal: Thermal | None = None
    ts: AwareDatetime | None = None


def program_ready(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() in {"running", "ready"})


def status_confirms(action: str, status: dict) -> bool:
    task = status.get("task", {})
    current = task.get("state")
    paused = task.get("patrol_paused") is True
    chassis = status.get("programs", {}).get("chassis")
    if action == "start":
        return program_ready(chassis)
    if action == "close":
        return chassis is False or (isinstance(chassis, str) and chassis.lower() == "stopped")
    if action in {"patrol", "resume_patrol"}:
        return current == "patrol" and not paused
    if action == "stop_patrol":
        return paused or current == "idle"
    if action == "homing":
        return current == "homing"
    if action == "navigate":
        return current == "spot"
    if action == "stop_navigate":
        return current == "idle"
    return action in {"ping", "get_status", "get_telemetry"}


def command_from_canonical(payload: dict[str, Any]) -> dict[str, Any]:
    canonical = CanonicalCommand.model_validate(payload)
    if canonical.expires_at <= canonical.issued_at:
        raise ValueError("invalid command expiry")
    return WireCommand(
        msg_id=payload["message_id"],
        command=payload["cmd"],
        params=payload["params"],
        ts=payload["issued_at"],
        vehicle_id=canonical.vehicle_id,
        target_boot_id=canonical.target_boot_id,
        expires_at=canonical.expires_at,
        mode=canonical.execution_mode or "REAL",
    ).model_dump(mode="json")


def normalize(kind: str, payload: Any) -> dict[str, Any]:
    if kind == "status":
        return Status.model_validate(payload).model_dump(mode="json")
    if kind == "telemetry":
        return Telemetry.model_validate(payload).model_dump(mode="json")
    if kind == "ack":
        ack = Ack.model_validate(payload)
        return {**ack.model_dump(mode="json"), "accepted": ack.accepted}
    if kind == "availability":
        state = payload if isinstance(payload, str) else payload.get("state")
        if state not in {"online", "offline"}:
            raise ValueError("invalid availability")
        result: dict[str, Any] = {"state": state}
        if isinstance(payload, dict) and payload.get("boot_id") is not None:
            result["boot_id"] = str(UUID(str(payload["boot_id"])))
        return result
    if kind == "event" and isinstance(payload, dict):
        return payload
    raise ValueError("unsupported uplink")
