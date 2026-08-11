from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uuid_str() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


JsonType = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_id",
        String(36),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE")
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))


class Permission(Base):
    __tablename__ = "permissions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(96), unique=True)
    name: Mapped[str] = mapped_column(String(128))


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[str] = mapped_column(String(36), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Map(Base):
    __tablename__ = "maps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    active_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    __table_args__ = (UniqueConstraint("site_id", "code", name="uq_map_site_code"),)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    object_name: Mapped[str] = mapped_column(String(255), unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MapVersion(Base):
    __tablename__ = "map_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    map_id: Mapped[str] = mapped_column(String(36), ForeignKey("maps.id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    semantic_revision: Mapped[int] = mapped_column(Integer, default=1)
    width_m: Mapped[float] = mapped_column(Float, default=30)
    height_m: Mapped[float] = mapped_column(Float, default=20)
    origin_x: Mapped[float] = mapped_column(Float, default=0)
    origin_y: Mapped[float] = mapped_column(Float, default=0)
    rotation_rad: Mapped[float] = mapped_column(Float, default=0)
    resolution_m_per_pixel: Mapped[float] = mapped_column(Float, default=0.05)
    background_asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("assets.id"), nullable=True
    )
    frame_id: Mapped[str] = mapped_column(String(64), default="map")
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("map_id", "version", name="uq_map_version"),)


class ParkingSlot(Base):
    __tablename__ = "parking_slots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    map_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("map_versions.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64))
    polygon_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    center_pose_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("map_version_id", "code", name="uq_slot_version_code"),)


class InspectionPoint(Base):
    __tablename__ = "inspection_points"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    map_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("map_versions.id", ondelete="CASCADE"), index=True
    )
    parking_slot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("parking_slots.id", ondelete="CASCADE")
    )
    pose_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    sensor_orientation_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=1)


class ExtinguishPoint(Base):
    __tablename__ = "extinguish_points"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    map_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("map_versions.id", ondelete="CASCADE"), index=True
    )
    parking_slot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("parking_slots.id", ondelete="CASCADE")
    )
    pose_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    approach_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    nozzle_config_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class Trajectory(Base):
    __tablename__ = "trajectories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    map_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("map_versions.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32), default="1")
    path_json: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Robot(Base):
    __tablename__ = "robots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    vehicle_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128), default="UNKNOWN")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    online_state: Mapped[str] = mapped_column(String(16), default="OFFLINE")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_map_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("maps.id"), nullable=True
    )
    current_map_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_mode: Mapped[str] = mapped_column(String(24), default="IDLE")
    current_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    battery: Mapped[float] = mapped_column(Float, default=0)
    estop_active: Mapped[bool] = mapped_column(Boolean, default=False)
    boot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RobotCredential(Base):
    __tablename__ = "robot_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    robot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("robots.id", ondelete="CASCADE"), index=True
    )
    credential_type: Mapped[str] = mapped_column(String(32))
    credential_ref: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RobotCapability(Base):
    __tablename__ = "robot_capabilities"
    robot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("robots.id", ondelete="CASCADE"), primary_key=True
    )
    protocol_version: Mapped[str] = mapped_column(String(16), default="1.2.0")
    supported_commands_json: Mapped[list[str]] = mapped_column(JsonType)
    sensors_json: Mapped[list[str]] = mapped_column(JsonType)
    media_json: Mapped[list[str]] = mapped_column(JsonType)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RobotConnectionLog(Base):
    __tablename__ = "robot_connection_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    robot_id: Mapped[str] = mapped_column(String(36), ForeignKey("robots.id"), index=True)
    state: Mapped[str] = mapped_column(String(16))
    boot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    server_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class RobotBootSession(Base):
    __tablename__ = "robot_boot_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    robot_id: Mapped[str] = mapped_column(String(36), ForeignKey("robots.id"), index=True)
    boot_id: Mapped[str] = mapped_column(String(36))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("robot_id", "boot_id", name="uq_robot_boot_session"),)


class ManualControlSession(Base):
    __tablename__ = "manual_control_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    lease_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    robot_id: Mapped[str] = mapped_column(String(36), ForeignKey("robots.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(24), default="HELD")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    task_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    robot_id: Mapped[str] = mapped_column(String(36), ForeignKey("robots.id"), index=True)
    fire_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    phase: Mapped[str] = mapped_column(String(64), default="CREATED")
    progress: Mapped[float] = mapped_column(Float, default=0)
    target_parking_slot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("parking_slots.id"), nullable=True
    )
    target_pose_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    map_id_snapshot: Mapped[str] = mapped_column(String(36))
    map_version_snapshot: Mapped[str] = mapped_column(String(32))
    semantic_revision_snapshot: Mapped[int] = mapped_column(Integer)
    trajectory_snapshot_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JsonType, nullable=True
    )
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaskEvent(Base):
    __tablename__ = "task_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32))
    phase: Mapped[str] = mapped_column(String(64))
    progress: Mapped[float] = mapped_column(Float, default=0)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class Command(Base):
    __tablename__ = "commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    command_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    robot_id: Mapped[str] = mapped_column(String(36), ForeignKey("robots.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id"), nullable=True, index=True
    )
    cmd: Mapped[str] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer, default=50)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    lifecycle_status: Mapped[str] = mapped_column(String(32), default="CREATED", index=True)
    issued_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ack_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    aggregate_type: Mapped[str] = mapped_column(String(32))
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FireEvent(Base):
    __tablename__ = "fire_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    event_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    robot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("robots.id"), nullable=True, index=True
    )
    parking_slot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("parking_slots.id"), index=True
    )
    detection_method: Mapped[str] = mapped_column(String(16))
    fire_type: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity: Mapped[str] = mapped_column(String(16), default="HIGH")
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(24), default="NEW", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    ack_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_position_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    sensor_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    media_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class TelemetrySample(Base):
    __tablename__ = "telemetry_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    robot_id: Mapped[str] = mapped_column(String(36), ForeignKey("robots.id"), index=True)
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    server_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    theta: Mapped[float] = mapped_column(Float)
    linear_speed: Mapped[float] = mapped_column(Float, default=0)
    angular_speed: Mapped[float] = mapped_column(Float, default=0)
    battery: Mapped[float] = mapped_column(Float, default=0)
    parking_slot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    localization_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    map_version: Mapped[str] = mapped_column(String(32))
    boot_id: Mapped[str] = mapped_column(String(36))
    seq: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        Index("ix_telemetry_robot_received", "robot_id", "server_received_at"),
        {"postgresql_partition_by": "RANGE (server_received_at)"},
    )


class SensorSample(Base):
    __tablename__ = "sensor_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    robot_id: Mapped[str] = mapped_column(String(36), ForeignKey("robots.id"), index=True)
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    server_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    smoke: Mapped[float] = mapped_column(Float)
    bottom_ir: Mapped[float] = mapped_column(Float)
    top_ir_max: Mapped[float] = mapped_column(Float)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    boot_id: Mapped[str] = mapped_column(String(36))
    seq: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        Index("ix_sensor_robot_received", "robot_id", "server_received_at"),
        {"postgresql_partition_by": "RANGE (server_received_at)"},
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    actor_type: Mapped[str] = mapped_column(String(24), default="USER")
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    robot_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(96), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    result: Mapped[str] = mapped_column(String(32), default="SUCCESS")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class MediaRecord(Base):
    __tablename__ = "media_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    robot_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("robots.id"), nullable=True)
    fire_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("fire_events.id"), nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=True)
    media_type: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class StreamRegistry(Base):
    __tablename__ = "stream_registry"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    stream_id: Mapped[str] = mapped_column(String(96), unique=True)
    robot_id: Mapped[str] = mapped_column(String(36), ForeignKey("robots.id"), index=True)
    camera_type: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32), default="MEDIAMTX")
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    playback_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    codec: Mapped[str] = mapped_column(String(16), default="H264")
    state: Mapped[str] = mapped_column(String(16), default="DISABLED")
    last_frame_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    actor_id: Mapped[str] = mapped_column(String(36), index=True)
    endpoint: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("actor_id", "endpoint", "idempotency_key", name="uq_idempotency_scope"),
    )
