"""Generated-facing protocol models for schema 1.1."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Envelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal["1.1"] = "1.1"
    message_id: UUID
    type: str
    vehicle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    boot_id: UUID
    timestamp: datetime
    seq: int = Field(ge=0)


class Command(Envelope):
    type: Literal["command"] = "command"
    command_id: str
    correlation_id: str
    task_id: str | None = None
    lease_id: str | None = None
    control_session_id: str | None = None
    issued_at: datetime
    expires_at: datetime
    ttl_ms: int = Field(gt=0)
    priority: int = Field(ge=0, le=100)
    source: Literal["WEB"] = "WEB"
    operator_id: str
    cmd: Literal[
        "manual_control", "stop_motion", "emergency_stop", "reset_estop",
        "return_dock", "patrol", "extinguish", "cancel_task",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
