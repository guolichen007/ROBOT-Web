"""Generated-facing protocol models for contract 1.2.0 / schema 1.2."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


CommandName = Literal[
    "manual_control",
    "stop_motion",
    "emergency_stop",
    "reset_estop",
    "return_dock",
    "patrol",
    "extinguish",
    "cancel_task",
]


class VehicleEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal["1.2"] = "1.2"
    message_id: UUID
    type: str
    vehicle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    boot_id: UUID
    timestamp: datetime
    seq: int = Field(ge=0)


class CommandMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.2"] = "1.2"
    message_id: UUID
    type: Literal["command"] = "command"
    vehicle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    target_boot_id: UUID | None
    command_id: str
    correlation_id: UUID
    task_id: str | None = None
    lease_id: str | None = None
    control_session_id: str | None = None
    seq: int | None = Field(default=None, ge=0)
    issued_at: datetime
    expires_at: datetime
    ttl_ms: int = Field(gt=0)
    priority: int = Field(ge=0, le=100)
    source: Literal["WEB"] = "WEB"
    operator_id: str
    cmd: CommandName
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_boot_and_manual(self) -> "CommandMessage":
        if self.cmd != "emergency_stop" and self.target_boot_id is None:
            raise ValueError("target_boot_id is required outside emergency_stop")
        if self.cmd == "manual_control" and (
            not self.lease_id or not self.control_session_id or self.seq is None
        ):
            raise ValueError("manual control identity is incomplete")
        return self


class CommandAck(VehicleEnvelope):
    type: Literal["command_ack"] = "command_ack"
    command_id: str
    task_id: str | None = None
    status: Literal["accepted", "rejected", "unsupported"]
    reason_code: str | None = None
    reason: str | None = None
