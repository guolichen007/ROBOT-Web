"""Frozen industrial operations, patrol and compatibility schema.

Revision ID: 20260812_0003
Revises: 20260811_0002
"""

# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op

revision = "20260812_0003"
down_revision = "20260811_0002"
branch_labels = None
depends_on = None


FROZEN_OPERATIONS_DDL = (
    """
CREATE TABLE robot_integration_profiles (
	robot_id VARCHAR(36) NOT NULL,
	source_kind VARCHAR(24) NOT NULL,
	upstream_protocol VARCHAR(32),
	control_contract_verified BOOLEAN NOT NULL,
	ack_contract_verified BOOLEAN NOT NULL,
	map_contract_verified BOOLEAN NOT NULL,
	read_only_reason TEXT,
	stale_seconds INTEGER NOT NULL,
	offline_seconds INTEGER NOT NULL,
	forward_only BOOLEAN NOT NULL,
	reverse_precision_navigation BOOLEAN NOT NULL,
	verified_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (robot_id),
	FOREIGN KEY(robot_id) REFERENCES robots (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE TABLE robot_external_aliases (
	id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	source_kind VARCHAR(24) NOT NULL,
	external_id VARCHAR(128) NOT NULL,
	state VARCHAR(24) NOT NULL,
	confirmed_by VARCHAR(36),
	confirmed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id) ON DELETE CASCADE,
	FOREIGN KEY(confirmed_by) REFERENCES users (id)
)
""".strip(),
    """
CREATE UNIQUE INDEX ix_robot_external_aliases_external_id ON robot_external_aliases (external_id)
""".strip(),
    """
CREATE INDEX ix_robot_external_aliases_robot_id ON robot_external_aliases (robot_id)
""".strip(),
    """
CREATE TABLE robot_data_channels (
	id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	channel VARCHAR(64) NOT NULL,
	support_state VARCHAR(24) NOT NULL,
	quality VARCHAR(24) NOT NULL,
	source_kind VARCHAR(24) NOT NULL,
	last_source_timestamp TIMESTAMP WITH TIME ZONE,
	last_received_at TIMESTAMP WITH TIME ZONE,
	error_code VARCHAR(64),
	metadata_json JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_robot_data_channel UNIQUE (robot_id, channel),
	FOREIGN KEY(robot_id) REFERENCES robots (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX ix_robot_data_channels_robot_id ON robot_data_channels (robot_id)
""".strip(),
    """
CREATE TABLE robot_sensor_profiles (
	id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	channel VARCHAR(64) NOT NULL,
	support_state VARCHAR(24) NOT NULL,
	nominal_side VARCHAR(16) NOT NULL,
	sensor_mount_x_m FLOAT NOT NULL,
	sensor_mount_y_m FLOAT NOT NULL,
	sensor_mount_yaw_rad FLOAT NOT NULL,
	coverage_range_m FLOAT NOT NULL,
	coverage_fov_rad FLOAT NOT NULL,
	config_source VARCHAR(32) NOT NULL,
	verified_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_robot_sensor_profile UNIQUE (robot_id, channel),
	FOREIGN KEY(robot_id) REFERENCES robots (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX ix_robot_sensor_profiles_robot_id ON robot_sensor_profiles (robot_id)
""".strip(),
    """
CREATE TABLE navigation_presets (
	id VARCHAR(36) NOT NULL,
	map_version_id VARCHAR(36) NOT NULL,
	code VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	category VARCHAR(32) NOT NULL,
	pose_json JSONB NOT NULL,
	position_tolerance_m FLOAT NOT NULL,
	yaw_tolerance_rad FLOAT NOT NULL,
	allowed_approach_json JSONB NOT NULL,
	requires_reverse BOOLEAN NOT NULL,
	is_default BOOLEAN NOT NULL,
	enabled BOOLEAN NOT NULL,
	semantic_revision INTEGER NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_navigation_preset_code UNIQUE (map_version_id, code),
	FOREIGN KEY(map_version_id) REFERENCES map_versions (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX ix_navigation_presets_map_version_id ON navigation_presets (map_version_id)
""".strip(),
    """
CREATE TABLE patrol_plans (
	id VARCHAR(36) NOT NULL,
	code VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	map_version_id VARCHAR(36) NOT NULL,
	trajectory_id VARCHAR(36),
	enabled BOOLEAN NOT NULL,
	created_by VARCHAR(36) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id),
	FOREIGN KEY(map_version_id) REFERENCES map_versions (id),
	FOREIGN KEY(trajectory_id) REFERENCES trajectories (id),
	FOREIGN KEY(created_by) REFERENCES users (id)
)
""".strip(),
    """
CREATE INDEX ix_patrol_plans_robot_id ON patrol_plans (robot_id)
""".strip(),
    """
CREATE UNIQUE INDEX ix_patrol_plans_code ON patrol_plans (code)
""".strip(),
    """
CREATE TABLE patrol_reports (
	id VARCHAR(36) NOT NULL,
	report_code VARCHAR(64) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	status VARCHAR(24) NOT NULL,
	summary_json JSONB NOT NULL,
	html_object_name VARCHAR(255),
	pdf_object_name VARCHAR(255),
	xlsx_object_name VARCHAR(255),
	failure_message TEXT,
	generated_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (task_id),
	FOREIGN KEY(task_id) REFERENCES tasks (id)
)
""".strip(),
    """
CREATE UNIQUE INDEX ix_patrol_reports_report_code ON patrol_reports (report_code)
""".strip(),
    """
CREATE TABLE stop_operations (
	id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36),
	cancel_command_id VARCHAR(64),
	stop_command_id VARCHAR(64) NOT NULL,
	state VARCHAR(40) NOT NULL,
	stationary_frames INTEGER NOT NULL,
	linear_threshold FLOAT NOT NULL,
	angular_threshold FLOAT NOT NULL,
	telemetry_freshness_ms INTEGER NOT NULL,
	requested_by VARCHAR(36) NOT NULL,
	requested_at TIMESTAMP WITH TIME ZONE NOT NULL,
	terminal_at TIMESTAMP WITH TIME ZONE,
	failure_reason TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id),
	FOREIGN KEY(cancel_command_id) REFERENCES commands (command_id),
	FOREIGN KEY(stop_command_id) REFERENCES commands (command_id),
	FOREIGN KEY(requested_by) REFERENCES users (id)
)
""".strip(),
    """
CREATE INDEX ix_stop_operations_robot_id ON stop_operations (robot_id)
""".strip(),
    """
CREATE INDEX ix_stop_operations_state ON stop_operations (state)
""".strip(),
    """
CREATE TABLE robot_operation_events (
	id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36),
	operation_type VARCHAR(64) NOT NULL,
	state VARCHAR(40) NOT NULL,
	payload_json JSONB NOT NULL,
	source VARCHAR(24) NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id)
)
""".strip(),
    """
CREATE INDEX ix_robot_operation_events_robot_id ON robot_operation_events (robot_id)
""".strip(),
    """
CREATE INDEX ix_robot_operation_events_operation_type ON robot_operation_events (operation_type)
""".strip(),
    """
CREATE INDEX ix_robot_operation_events_occurred_at ON robot_operation_events (occurred_at)
""".strip(),
    """
CREATE TABLE patrol_plan_points (
	id VARCHAR(36) NOT NULL,
	patrol_plan_id VARCHAR(36) NOT NULL,
	navigation_preset_id VARCHAR(36) NOT NULL,
	sequence INTEGER NOT NULL,
	dwell_seconds INTEGER NOT NULL,
	required_observations_json JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_patrol_plan_point_sequence UNIQUE (patrol_plan_id, sequence),
	FOREIGN KEY(patrol_plan_id) REFERENCES patrol_plans (id) ON DELETE CASCADE,
	FOREIGN KEY(navigation_preset_id) REFERENCES navigation_presets (id)
)
""".strip(),
    """
CREATE INDEX ix_patrol_plan_points_patrol_plan_id ON patrol_plan_points (patrol_plan_id)
""".strip(),
    """
CREATE TABLE patrol_schedules (
	id VARCHAR(36) NOT NULL,
	patrol_plan_id VARCHAR(36) NOT NULL,
	cron_expression VARCHAR(64) NOT NULL,
	timezone VARCHAR(64) NOT NULL,
	enabled BOOLEAN NOT NULL,
	misfire_policy VARCHAR(32) NOT NULL,
	misfire_grace_seconds INTEGER NOT NULL,
	overlap_policy VARCHAR(24) NOT NULL,
	require_robot_online BOOLEAN NOT NULL,
	require_control_contract_verified BOOLEAN NOT NULL,
	require_map_contract_verified BOOLEAN NOT NULL,
	next_run_at TIMESTAMP WITH TIME ZONE,
	last_run_at TIMESTAMP WITH TIME ZONE,
	created_by VARCHAR(36) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(patrol_plan_id) REFERENCES patrol_plans (id) ON DELETE CASCADE,
	FOREIGN KEY(created_by) REFERENCES users (id)
)
""".strip(),
    """
CREATE INDEX ix_patrol_schedules_patrol_plan_id ON patrol_schedules (patrol_plan_id)
""".strip(),
    """
CREATE TABLE inspection_observations (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	navigation_preset_id VARCHAR(36),
	parking_slot_id VARCHAR(36),
	observation_type VARCHAR(64) NOT NULL,
	result VARCHAR(24) NOT NULL,
	value_json JSONB NOT NULL,
	data_state VARCHAR(24) NOT NULL,
	source_timestamp TIMESTAMP WITH TIME ZONE,
	server_received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id),
	FOREIGN KEY(navigation_preset_id) REFERENCES navigation_presets (id),
	FOREIGN KEY(parking_slot_id) REFERENCES parking_slots (id)
)
""".strip(),
    """
CREATE INDEX ix_inspection_observations_task_id ON inspection_observations (task_id)
""".strip(),
    """
CREATE INDEX ix_inspection_observations_robot_id ON inspection_observations (robot_id)
""".strip(),
    """
CREATE TABLE patrol_schedule_occurrences (
	id VARCHAR(36) NOT NULL,
	schedule_id VARCHAR(36) NOT NULL,
	scheduled_for TIMESTAMP WITH TIME ZONE NOT NULL,
	state VARCHAR(24) NOT NULL,
	reason_code VARCHAR(64),
	task_id VARCHAR(36),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_patrol_schedule_occurrence UNIQUE (schedule_id, scheduled_for),
	FOREIGN KEY(schedule_id) REFERENCES patrol_schedules (id) ON DELETE CASCADE,
	FOREIGN KEY(task_id) REFERENCES tasks (id)
)
""".strip(),
    """
CREATE INDEX ix_patrol_schedule_occurrences_schedule_id ON patrol_schedule_occurrences (schedule_id)
""".strip(),
    """
CREATE INDEX ix_patrol_schedule_occurrences_scheduled_for ON patrol_schedule_occurrences (scheduled_for)
""".strip(),
)

NEW_TABLES = (
    "robot_integration_profiles",
    "robot_external_aliases",
    "robot_data_channels",
    "robot_sensor_profiles",
    "navigation_presets",
    "patrol_plans",
    "patrol_plan_points",
    "patrol_schedules",
    "patrol_schedule_occurrences",
    "inspection_observations",
    "patrol_reports",
    "stop_operations",
    "robot_operation_events",
)

NULLABLE_COLUMNS = (
    ("robots", "battery"),
    ("telemetry_samples", "linear_speed"),
    ("telemetry_samples", "angular_speed"),
    ("telemetry_samples", "battery"),
    ("sensor_samples", "smoke"),
    ("sensor_samples", "bottom_ir"),
    ("sensor_samples", "top_ir_max"),
)


def upgrade() -> None:
    for statement in FROZEN_OPERATIONS_DDL:
        op.execute(statement)
    for table, column in NULLABLE_COLUMNS:
        op.alter_column(table, column, existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    for table, column in NULLABLE_COLUMNS:
        op.execute(f"UPDATE {table} SET {column} = 0 WHERE {column} IS NULL")
        op.alter_column(table, column, existing_type=sa.Float(), nullable=False)
    for name in reversed(NEW_TABLES):
        op.drop_table(name)
