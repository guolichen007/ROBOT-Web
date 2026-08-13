"""pre-real-vehicle readiness, deterministic stop and report assets

Revision ID: 20260813_0004
Revises: 20260812_0003
"""

import sqlalchemy as sa
from alembic import op

revision = "20260813_0004"
down_revision = "20260812_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "robots",
        "estop_active",
        existing_type=sa.Boolean(),
        nullable=True,
    )
    for name in (
        "bidirectional_bridge_verified",
        "command_path_verified",
        "cmd_vel_arbitration_verified",
    ):
        op.add_column(
            "robot_integration_profiles",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    op.add_column(
        "robot_integration_profiles", sa.Column("ros_control_mode", sa.Integer(), nullable=True)
    )
    op.create_table(
        "robot_motion_profiles",
        sa.Column("robot_id", sa.String(36), nullable=False),
        sa.Column("max_manual_forward_mps", sa.Float(), nullable=True),
        sa.Column("max_manual_reverse_mps", sa.Float(), nullable=True),
        sa.Column("max_manual_angular_radps", sa.Float(), nullable=True),
        sa.Column(
            "manual_watchdog_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("reverse_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "reverse_precision_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["robot_id"], ["robots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("robot_id"),
    )
    op.create_table(
        "robot_navigation_diagnostics",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("robot_id", sa.String(36), nullable=False),
        sa.Column("external_goal_id", sa.String(128), nullable=True),
        sa.Column("diagnostic_type", sa.String(24), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("server_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["robot_id"], ["robots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_robot_navigation_diagnostics_robot_id",
        "robot_navigation_diagnostics",
        ["robot_id"],
    )
    op.create_index(
        "ix_robot_navigation_diagnostics_external_goal_id",
        "robot_navigation_diagnostics",
        ["external_goal_id"],
    )
    op.create_index(
        "ix_robot_navigation_diagnostics_server_received_at",
        "robot_navigation_diagnostics",
        ["server_received_at"],
    )
    op.add_column(
        "patrol_schedules",
        sa.Column("queue_expiry_seconds", sa.Integer(), nullable=False, server_default="300"),
    )
    for format_name in ("html", "pdf", "xlsx"):
        op.add_column(
            "patrol_reports", sa.Column(f"{format_name}_asset_id", sa.String(36), nullable=True)
        )
        op.create_foreign_key(
            f"fk_patrol_reports_{format_name}_asset_id",
            "patrol_reports",
            "assets",
            [f"{format_name}_asset_id"],
            ["id"],
        )
    op.add_column(
        "stop_operations",
        sa.Column("motion_stop_state", sa.String(40), nullable=False, server_default="WAITING_ACK"),
    )
    op.add_column(
        "stop_operations",
        sa.Column(
            "mission_cancel_state",
            sa.String(40),
            nullable=False,
            server_default="NOT_REQUIRED",
        ),
    )
    for name in ("stop_ack_deadline_at", "cancel_deadline_at", "stationary_verify_deadline_at"):
        op.add_column("stop_operations", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE stop_operations SET "
        "stop_ack_deadline_at = requested_at + interval '5 seconds', "
        "stationary_verify_deadline_at = requested_at + interval '15 seconds'"
    )
    op.alter_column("stop_operations", "stop_ack_deadline_at", nullable=False)
    op.alter_column("stop_operations", "stationary_verify_deadline_at", nullable=False)


def downgrade() -> None:
    op.execute("UPDATE robots SET estop_active = false WHERE estop_active IS NULL")
    op.alter_column(
        "robots",
        "estop_active",
        existing_type=sa.Boolean(),
        nullable=False,
    )
    for column in (
        "stationary_verify_deadline_at",
        "cancel_deadline_at",
        "stop_ack_deadline_at",
        "mission_cancel_state",
        "motion_stop_state",
    ):
        op.drop_column("stop_operations", column)
    for format_name in ("xlsx", "pdf", "html"):
        op.drop_constraint(
            f"fk_patrol_reports_{format_name}_asset_id", "patrol_reports", type_="foreignkey"
        )
        op.drop_column("patrol_reports", f"{format_name}_asset_id")
    op.drop_column("patrol_schedules", "queue_expiry_seconds")
    op.drop_index(
        "ix_robot_navigation_diagnostics_server_received_at",
        table_name="robot_navigation_diagnostics",
    )
    op.drop_index(
        "ix_robot_navigation_diagnostics_external_goal_id",
        table_name="robot_navigation_diagnostics",
    )
    op.drop_index(
        "ix_robot_navigation_diagnostics_robot_id",
        table_name="robot_navigation_diagnostics",
    )
    op.drop_table("robot_navigation_diagnostics")
    op.drop_table("robot_motion_profiles")
    for column in (
        "ros_control_mode",
        "cmd_vel_arbitration_verified",
        "command_path_verified",
        "bidirectional_bridge_verified",
    ):
        op.drop_column("robot_integration_profiles", column)
