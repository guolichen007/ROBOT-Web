"""Frozen initial Firebot V2 baseline schema.

Revision ID: 20260810_0001
Revises:

This migration is intentionally independent from the current ORM metadata.
"""

# ruff: noqa: E501

from alembic import op

revision = "20260810_0001"
down_revision = None
branch_labels = None
depends_on = None


FROZEN_BASELINE_DDL = (
    """
CREATE TABLE users (
	id VARCHAR(36) NOT NULL,
	username VARCHAR(64) NOT NULL,
	password_hash TEXT NOT NULL,
	display_name VARCHAR(128) NOT NULL,
	status VARCHAR(24) NOT NULL,
	must_change_password BOOLEAN NOT NULL,
	failed_attempts INTEGER NOT NULL,
	locked_until TIMESTAMP WITH TIME ZONE,
	last_login_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)
""".strip(),
    """
CREATE UNIQUE INDEX ix_users_username ON users (username)
""".strip(),
    """
CREATE TABLE roles (
	id VARCHAR(36) NOT NULL,
	code VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (code)
)
""".strip(),
    """
CREATE TABLE permissions (
	id VARCHAR(36) NOT NULL,
	code VARCHAR(96) NOT NULL,
	name VARCHAR(128) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (code)
)
""".strip(),
    """
CREATE TABLE sites (
	id VARCHAR(36) NOT NULL,
	code VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	timezone VARCHAR(64) NOT NULL,
	active BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (code)
)
""".strip(),
    """
CREATE TABLE outbox_events (
	id VARCHAR(36) NOT NULL,
	aggregate_type VARCHAR(32) NOT NULL,
	aggregate_id VARCHAR(64) NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	payload_json JSONB NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	last_error TEXT,
	PRIMARY KEY (id)
)
""".strip(),
    """
CREATE INDEX ix_outbox_events_status ON outbox_events (status)
""".strip(),
    """
CREATE INDEX ix_outbox_events_aggregate_id ON outbox_events (aggregate_id)
""".strip(),
    """
CREATE TABLE audit_logs (
	id VARCHAR(36) NOT NULL,
	actor_type VARCHAR(24) NOT NULL,
	user_id VARCHAR(36),
	robot_id VARCHAR(36),
	action VARCHAR(96) NOT NULL,
	resource_type VARCHAR(64) NOT NULL,
	resource_id VARCHAR(64),
	request_id VARCHAR(36),
	ip VARCHAR(64),
	user_agent VARCHAR(255),
	before_json JSONB,
	after_json JSONB,
	result VARCHAR(32) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)
""".strip(),
    """
CREATE INDEX ix_audit_logs_created_at ON audit_logs (created_at)
""".strip(),
    """
CREATE INDEX ix_audit_logs_action ON audit_logs (action)
""".strip(),
    """
CREATE INDEX ix_audit_logs_user_id ON audit_logs (user_id)
""".strip(),
    """
CREATE INDEX ix_audit_logs_robot_id ON audit_logs (robot_id)
""".strip(),
    """
CREATE TABLE app_settings (
	key VARCHAR(128) NOT NULL,
	value_json JSONB NOT NULL,
	updated_by VARCHAR(36),
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (key)
)
""".strip(),
    """
CREATE TABLE system_events (
	id VARCHAR(36) NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	severity VARCHAR(16) NOT NULL,
	payload_json JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)
""".strip(),
    """
CREATE INDEX ix_system_events_created_at ON system_events (created_at)
""".strip(),
    """
CREATE INDEX ix_system_events_event_type ON system_events (event_type)
""".strip(),
    """
CREATE TABLE idempotency_records (
	id VARCHAR(36) NOT NULL,
	actor_id VARCHAR(36) NOT NULL,
	endpoint VARCHAR(128) NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	request_hash VARCHAR(64) NOT NULL,
	response_status INTEGER NOT NULL,
	response_json JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_idempotency_scope UNIQUE (actor_id, endpoint, idempotency_key)
)
""".strip(),
    """
CREATE INDEX ix_idempotency_records_actor_id ON idempotency_records (actor_id)
""".strip(),
    """
CREATE TABLE user_roles (
	user_id VARCHAR(36) NOT NULL,
	role_id VARCHAR(36) NOT NULL,
	PRIMARY KEY (user_id, role_id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(role_id) REFERENCES roles (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE TABLE role_permissions (
	role_id VARCHAR(36) NOT NULL,
	permission_id VARCHAR(36) NOT NULL,
	PRIMARY KEY (role_id, permission_id),
	FOREIGN KEY(role_id) REFERENCES roles (id) ON DELETE CASCADE,
	FOREIGN KEY(permission_id) REFERENCES permissions (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE TABLE refresh_sessions (
	id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	family_id VARCHAR(36) NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	csrf_hash VARCHAR(64) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	replaced_by VARCHAR(36),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	UNIQUE (token_hash)
)
""".strip(),
    """
CREATE INDEX ix_refresh_sessions_family_id ON refresh_sessions (family_id)
""".strip(),
    """
CREATE INDEX ix_refresh_sessions_user_id ON refresh_sessions (user_id)
""".strip(),
    """
CREATE TABLE maps (
	id VARCHAR(36) NOT NULL,
	site_id VARCHAR(36) NOT NULL,
	code VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	active_version_id VARCHAR(36),
	PRIMARY KEY (id),
	CONSTRAINT uq_map_site_code UNIQUE (site_id, code),
	FOREIGN KEY(site_id) REFERENCES sites (id)
)
""".strip(),
    """
CREATE INDEX ix_maps_site_id ON maps (site_id)
""".strip(),
    """
CREATE TABLE assets (
	id VARCHAR(36) NOT NULL,
	object_name VARCHAR(255) NOT NULL,
	original_filename VARCHAR(255) NOT NULL,
	mime_type VARCHAR(128) NOT NULL,
	size_bytes INTEGER NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	created_by VARCHAR(36) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (object_name),
	FOREIGN KEY(created_by) REFERENCES users (id)
)
""".strip(),
    """
CREATE INDEX ix_assets_sha256 ON assets (sha256)
""".strip(),
    """
CREATE TABLE map_versions (
	id VARCHAR(36) NOT NULL,
	map_id VARCHAR(36) NOT NULL,
	version VARCHAR(32) NOT NULL,
	status VARCHAR(16) NOT NULL,
	checksum VARCHAR(64) NOT NULL,
	semantic_revision INTEGER NOT NULL,
	width_m FLOAT NOT NULL,
	height_m FLOAT NOT NULL,
	origin_x FLOAT NOT NULL,
	origin_y FLOAT NOT NULL,
	rotation_rad FLOAT NOT NULL,
	resolution_m_per_pixel FLOAT NOT NULL,
	background_asset_id VARCHAR(36),
	frame_id VARCHAR(64) NOT NULL,
	created_by VARCHAR(36) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_map_version UNIQUE (map_id, version),
	FOREIGN KEY(map_id) REFERENCES maps (id),
	FOREIGN KEY(background_asset_id) REFERENCES assets (id),
	FOREIGN KEY(created_by) REFERENCES users (id)
)
""".strip(),
    """
CREATE INDEX ix_map_versions_map_id ON map_versions (map_id)
""".strip(),
    """
CREATE TABLE robots (
	id VARCHAR(36) NOT NULL,
	vehicle_id VARCHAR(64) NOT NULL,
	site_id VARCHAR(36) NOT NULL,
	name VARCHAR(128) NOT NULL,
	model VARCHAR(128) NOT NULL,
	enabled BOOLEAN NOT NULL,
	online_state VARCHAR(16) NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE,
	current_map_id VARCHAR(36),
	current_map_version VARCHAR(32),
	current_mode VARCHAR(24) NOT NULL,
	current_task_id VARCHAR(36),
	battery FLOAT NOT NULL,
	estop_active BOOLEAN NOT NULL,
	boot_id VARCHAR(36),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(site_id) REFERENCES sites (id),
	FOREIGN KEY(current_map_id) REFERENCES maps (id)
)
""".strip(),
    """
CREATE INDEX ix_robots_site_id ON robots (site_id)
""".strip(),
    """
CREATE UNIQUE INDEX ix_robots_vehicle_id ON robots (vehicle_id)
""".strip(),
    """
CREATE TABLE parking_slots (
	id VARCHAR(36) NOT NULL,
	map_version_id VARCHAR(36) NOT NULL,
	code VARCHAR(64) NOT NULL,
	polygon_json JSONB NOT NULL,
	center_pose_json JSONB NOT NULL,
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_slot_version_code UNIQUE (map_version_id, code),
	FOREIGN KEY(map_version_id) REFERENCES map_versions (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX ix_parking_slots_map_version_id ON parking_slots (map_version_id)
""".strip(),
    """
CREATE TABLE trajectories (
	id VARCHAR(36) NOT NULL,
	map_version_id VARCHAR(36) NOT NULL,
	code VARCHAR(64) NOT NULL,
	version VARCHAR(32) NOT NULL,
	path_json JSONB NOT NULL,
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(map_version_id) REFERENCES map_versions (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX ix_trajectories_map_version_id ON trajectories (map_version_id)
""".strip(),
    """
CREATE TABLE robot_credentials (
	id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	credential_type VARCHAR(32) NOT NULL,
	credential_ref VARCHAR(255) NOT NULL,
	enabled BOOLEAN NOT NULL,
	rotated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX ix_robot_credentials_robot_id ON robot_credentials (robot_id)
""".strip(),
    """
CREATE TABLE robot_capabilities (
	robot_id VARCHAR(36) NOT NULL,
	protocol_version VARCHAR(16) NOT NULL,
	supported_commands_json JSONB NOT NULL,
	sensors_json JSONB NOT NULL,
	media_json JSONB NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (robot_id),
	FOREIGN KEY(robot_id) REFERENCES robots (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE TABLE robot_connection_logs (
	id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	state VARCHAR(16) NOT NULL,
	boot_id VARCHAR(36),
	reason VARCHAR(255),
	server_received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id)
)
""".strip(),
    """
CREATE INDEX ix_robot_connection_logs_robot_id ON robot_connection_logs (robot_id)
""".strip(),
    """
CREATE INDEX ix_robot_connection_logs_server_received_at ON robot_connection_logs (server_received_at)
""".strip(),
    """
CREATE TABLE manual_control_sessions (
	id VARCHAR(36) NOT NULL,
	lease_id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	state VARCHAR(24) NOT NULL,
	acquired_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_renewed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seq INTEGER NOT NULL,
	ended_at TIMESTAMP WITH TIME ZONE,
	end_reason VARCHAR(128),
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)
""".strip(),
    """
CREATE UNIQUE INDEX ix_manual_control_sessions_lease_id ON manual_control_sessions (lease_id)
""".strip(),
    """
CREATE INDEX ix_manual_control_sessions_user_id ON manual_control_sessions (user_id)
""".strip(),
    """
CREATE INDEX ix_manual_control_sessions_robot_id ON manual_control_sessions (robot_id)
""".strip(),
    """
CREATE TABLE telemetry_samples (
	id SERIAL NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	source_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
	server_received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	x FLOAT NOT NULL,
	y FLOAT NOT NULL,
	theta FLOAT NOT NULL,
	linear_speed FLOAT NOT NULL,
	angular_speed FLOAT NOT NULL,
	battery FLOAT NOT NULL,
	parking_slot_id VARCHAR(36),
	localization_status VARCHAR(32) NOT NULL,
	map_version VARCHAR(32) NOT NULL,
	boot_id VARCHAR(36) NOT NULL,
	seq INTEGER NOT NULL,
	PRIMARY KEY (id, server_received_at),
	FOREIGN KEY(robot_id) REFERENCES robots (id)
)
 PARTITION BY RANGE (server_received_at)
""".strip(),
    """
CREATE INDEX ix_telemetry_samples_robot_id ON telemetry_samples (robot_id)
""".strip(),
    """
CREATE INDEX ix_telemetry_robot_received ON telemetry_samples (robot_id, server_received_at)
""".strip(),
    """
CREATE TABLE sensor_samples (
	id SERIAL NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	source_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
	server_received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	smoke FLOAT NOT NULL,
	bottom_ir FLOAT NOT NULL,
	top_ir_max FLOAT NOT NULL,
	payload_json JSONB NOT NULL,
	boot_id VARCHAR(36) NOT NULL,
	seq INTEGER NOT NULL,
	PRIMARY KEY (id, server_received_at),
	FOREIGN KEY(robot_id) REFERENCES robots (id)
)
 PARTITION BY RANGE (server_received_at)
""".strip(),
    """
CREATE INDEX ix_sensor_robot_received ON sensor_samples (robot_id, server_received_at)
""".strip(),
    """
CREATE INDEX ix_sensor_samples_robot_id ON sensor_samples (robot_id)
""".strip(),
    """
CREATE TABLE stream_registry (
	id VARCHAR(36) NOT NULL,
	stream_id VARCHAR(96) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	camera_type VARCHAR(32) NOT NULL,
	provider VARCHAR(32) NOT NULL,
	source_ref VARCHAR(255),
	playback_url VARCHAR(255),
	codec VARCHAR(16) NOT NULL,
	state VARCHAR(16) NOT NULL,
	last_frame_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (stream_id),
	FOREIGN KEY(robot_id) REFERENCES robots (id)
)
""".strip(),
    """
CREATE INDEX ix_stream_registry_robot_id ON stream_registry (robot_id)
""".strip(),
    """
CREATE TABLE inspection_points (
	id VARCHAR(36) NOT NULL,
	map_version_id VARCHAR(36) NOT NULL,
	parking_slot_id VARCHAR(36) NOT NULL,
	pose_json JSONB NOT NULL,
	sensor_orientation_json JSONB NOT NULL,
	priority INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(map_version_id) REFERENCES map_versions (id) ON DELETE CASCADE,
	FOREIGN KEY(parking_slot_id) REFERENCES parking_slots (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX ix_inspection_points_map_version_id ON inspection_points (map_version_id)
""".strip(),
    """
CREATE TABLE extinguish_points (
	id VARCHAR(36) NOT NULL,
	map_version_id VARCHAR(36) NOT NULL,
	parking_slot_id VARCHAR(36) NOT NULL,
	pose_json JSONB NOT NULL,
	approach_json JSONB NOT NULL,
	nozzle_config_json JSONB NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(map_version_id) REFERENCES map_versions (id) ON DELETE CASCADE,
	FOREIGN KEY(parking_slot_id) REFERENCES parking_slots (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX ix_extinguish_points_map_version_id ON extinguish_points (map_version_id)
""".strip(),
    """
CREATE TABLE tasks (
	id VARCHAR(36) NOT NULL,
	task_code VARCHAR(64) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	fire_event_id VARCHAR(36),
	type VARCHAR(32) NOT NULL,
	status VARCHAR(32) NOT NULL,
	phase VARCHAR(64) NOT NULL,
	progress FLOAT NOT NULL,
	target_parking_slot_id VARCHAR(36),
	target_pose_snapshot_json JSONB NOT NULL,
	map_id_snapshot VARCHAR(36) NOT NULL,
	map_version_snapshot VARCHAR(32) NOT NULL,
	semantic_revision_snapshot INTEGER NOT NULL,
	trajectory_snapshot_json JSONB,
	parameters_json JSONB NOT NULL,
	created_by VARCHAR(36) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	accepted_at TIMESTAMP WITH TIME ZONE,
	started_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	failure_code VARCHAR(64),
	failure_message TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id),
	FOREIGN KEY(target_parking_slot_id) REFERENCES parking_slots (id),
	FOREIGN KEY(created_by) REFERENCES users (id)
)
""".strip(),
    """
CREATE INDEX ix_tasks_fire_event_id ON tasks (fire_event_id)
""".strip(),
    """
CREATE UNIQUE INDEX ix_tasks_task_code ON tasks (task_code)
""".strip(),
    """
CREATE INDEX ix_tasks_robot_id ON tasks (robot_id)
""".strip(),
    """
CREATE TABLE fire_events (
	id VARCHAR(36) NOT NULL,
	event_code VARCHAR(64) NOT NULL,
	robot_id VARCHAR(36),
	parking_slot_id VARCHAR(36) NOT NULL,
	detection_method VARCHAR(16) NOT NULL,
	fire_type VARCHAR(16) NOT NULL,
	confidence FLOAT,
	severity VARCHAR(16) NOT NULL,
	fingerprint VARCHAR(128) NOT NULL,
	source_message_id VARCHAR(36),
	source_event_id VARCHAR(128),
	state VARCHAR(24) NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	occurrence_count INTEGER NOT NULL,
	ack_by VARCHAR(36),
	ack_at TIMESTAMP WITH TIME ZONE,
	confirmed_at TIMESTAMP WITH TIME ZONE,
	assigned_task_id VARCHAR(36),
	resolved_at TIMESTAMP WITH TIME ZONE,
	closed_at TIMESTAMP WITH TIME ZONE,
	source_position_json JSONB NOT NULL,
	sensor_snapshot_json JSONB NOT NULL,
	media_snapshot_json JSONB NOT NULL,
	note TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id),
	FOREIGN KEY(parking_slot_id) REFERENCES parking_slots (id),
	UNIQUE (source_message_id),
	FOREIGN KEY(ack_by) REFERENCES users (id)
)
""".strip(),
    """
CREATE UNIQUE INDEX ix_fire_events_event_code ON fire_events (event_code)
""".strip(),
    """
CREATE INDEX ix_fire_events_robot_id ON fire_events (robot_id)
""".strip(),
    """
CREATE INDEX ix_fire_events_source_event_id ON fire_events (source_event_id)
""".strip(),
    """
CREATE INDEX ix_fire_events_fingerprint ON fire_events (fingerprint)
""".strip(),
    """
CREATE INDEX ix_fire_events_parking_slot_id ON fire_events (parking_slot_id)
""".strip(),
    """
CREATE INDEX ix_fire_events_state ON fire_events (state)
""".strip(),
    """
CREATE TABLE task_events (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	status VARCHAR(32) NOT NULL,
	phase VARCHAR(64) NOT NULL,
	progress FLOAT NOT NULL,
	payload_json JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX ix_task_events_task_id ON task_events (task_id)
""".strip(),
    """
CREATE INDEX ix_task_events_created_at ON task_events (created_at)
""".strip(),
    """
CREATE TABLE commands (
	id VARCHAR(36) NOT NULL,
	command_id VARCHAR(64) NOT NULL,
	correlation_id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36),
	cmd VARCHAR(32) NOT NULL,
	priority INTEGER NOT NULL,
	payload_json JSONB NOT NULL,
	lifecycle_status VARCHAR(32) NOT NULL,
	issued_by VARCHAR(36) NOT NULL,
	issued_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	ack_at TIMESTAMP WITH TIME ZONE,
	ack_status VARCHAR(32),
	ack_reason TEXT,
	terminal_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id),
	FOREIGN KEY(issued_by) REFERENCES users (id)
)
""".strip(),
    """
CREATE INDEX ix_commands_correlation_id ON commands (correlation_id)
""".strip(),
    """
CREATE INDEX ix_commands_task_id ON commands (task_id)
""".strip(),
    """
CREATE UNIQUE INDEX ix_commands_command_id ON commands (command_id)
""".strip(),
    """
CREATE INDEX ix_commands_robot_id ON commands (robot_id)
""".strip(),
    """
CREATE INDEX ix_commands_lifecycle_status ON commands (lifecycle_status)
""".strip(),
    """
CREATE TABLE media_records (
	id VARCHAR(36) NOT NULL,
	robot_id VARCHAR(36),
	fire_event_id VARCHAR(36),
	task_id VARCHAR(36),
	media_type VARCHAR(32) NOT NULL,
	source VARCHAR(64) NOT NULL,
	url TEXT NOT NULL,
	captured_at TIMESTAMP WITH TIME ZONE NOT NULL,
	metadata_json JSONB NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(robot_id) REFERENCES robots (id),
	FOREIGN KEY(fire_event_id) REFERENCES fire_events (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id)
)
""".strip(),
)

FROZEN_TABLE_ORDER = (
    "app_settings",
    "audit_logs",
    "idempotency_records",
    "outbox_events",
    "permissions",
    "roles",
    "sites",
    "system_events",
    "users",
    "assets",
    "maps",
    "refresh_sessions",
    "role_permissions",
    "user_roles",
    "map_versions",
    "robots",
    "manual_control_sessions",
    "parking_slots",
    "robot_capabilities",
    "robot_connection_logs",
    "robot_credentials",
    "sensor_samples",
    "stream_registry",
    "telemetry_samples",
    "trajectories",
    "extinguish_points",
    "fire_events",
    "inspection_points",
    "tasks",
    "commands",
    "media_records",
    "task_events",
)


def upgrade() -> None:
    for statement in FROZEN_BASELINE_DDL:
        op.execute(statement)
    op.execute("CREATE TABLE telemetry_samples_default PARTITION OF telemetry_samples DEFAULT")
    op.execute("CREATE TABLE sensor_samples_default PARTITION OF sensor_samples DEFAULT")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS telemetry_samples_default")
    op.execute("DROP TABLE IF EXISTS sensor_samples_default")
    for table_name in reversed(FROZEN_TABLE_ORDER):
        op.drop_table(table_name)
