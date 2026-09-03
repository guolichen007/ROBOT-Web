"""Fleet assignment + stop evidence boot snapshot

Revision ID: 20260902_0008
Revises: 20260902_0007
"""

import sqlalchemy as sa
from alembic import op

revision = "20260902_0008"
down_revision = "20260902_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stop_operations", sa.Column("boot_id_snapshot", sa.String(36), nullable=True))
    op.create_table(
        "robot_fleet_assignments",
        sa.Column("robot_id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column(
            "location_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "supported_commands_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("device_token_hash", sa.String(64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_foreign_key(
        "fk_robot_fleet_assignments_robot_id",
        "robot_fleet_assignments",
        "robots",
        ["robot_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_robot_fleet_assignments_robot_id", "robot_fleet_assignments", type_="foreignkey"
    )
    op.drop_table("robot_fleet_assignments")
    op.drop_column("stop_operations", "boot_id_snapshot")
