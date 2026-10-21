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
)
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

    @model_validator(mode="after")
    def validate_params(self) -> WireCommand:
        if self.command == "start":
            if self.params != {"with_navigation": True}:
                raise ValueError("start requires with_navigation=true")
        elif self.command == "navigate":
            if set(self.params) != {"spot"} or not isinstance(self.params["spot"], str):
                raise ValueError("navigate requires a spot name")
            if not self.params["spot"].strip() or len(self.params["spot"]) > 128:
                raise ValueError("invalid spot")
        elif self.params:
            raise ValueError("command requires empty params")
        return self


class Ack(WireModel):
    msg_id: UUID
    success: bool
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


class AgentTask(WireModel):
    state: Literal["idle", "patrol", "homing", "spot"]
    patrol_next: str | int | None = None
    patrol_count: int | None = Field(default=None, ge=0)
    patrol_paused: bool = False


class Status(WireModel):
    online: bool
    programs: dict[str, bool | str | None] = Field(default_factory=dict)
    task: AgentTask
    ts: AwareDatetime | None = None


class Pose(WireModel):
    x: float
    y: float
    yaw: float
    frame_id: str


class Battery(WireModel):
    percentage: float | None = Field(default=None, ge=0, le=100)
    voltage: float | None = None
    current: float | None = None
    temperature: float | None = None
    capacity: float | None = None
    charge_state: str | int | None = None


class Smog(WireModel):
    percentage: float | None = Field(default=None, ge=0, le=100)
    raw: float | None = None
    alarm: bool | None = None


class Thermal(WireModel):
    max_temp: float | None = None


class Telemetry(WireModel):
    pose: Pose | None = None
    battery: Battery | None = None
    smog: Smog | None = None
    thermal: Thermal | None = None
    ts: AwareDatetime | None = None


def program_ready(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() in {"running", "ready"})


def command_from_canonical(payload: dict[str, Any]) -> dict[str, Any]:
    canonical = CanonicalCommand.model_validate(payload)
    if canonical.expires_at <= canonical.issued_at:
        raise ValueError("invalid command expiry")
    return WireCommand(
        msg_id=payload["message_id"],
        command=payload["cmd"],
        params=payload["params"],
        ts=payload["issued_at"],
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
        return {"state": state}
    if kind == "event" and isinstance(payload, dict):
        return payload
    raise ValueError("unsupported uplink")
