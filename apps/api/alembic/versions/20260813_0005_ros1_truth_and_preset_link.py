"""ROS1 source truth and explicit parking-slot preset link

Revision ID: 20260813_0005
Revises: 20260813_0004
"""

import sqlalchemy as sa
from alembic import op

revision = "20260813_0005"
down_revision = "20260813_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, length in (
        ("external_id", 128),
        ("bridge_boot_id", 36),
        ("availability_state", 16),
        ("reported_site_code", 64),
        ("reported_map_code", 64),
        ("reported_map_version", 32),
        ("reported_map_checksum", 128),
    ):
        op.add_column(
            "robot_integration_profiles", sa.Column(name, sa.String(length), nullable=True)
        )
    op.add_column(
        "robot_integration_profiles",
        sa.Column("last_source_timestamp", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "robot_integration_profiles",
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "robot_integration_profiles",
        sa.Column(
            "compat_sequence_state_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column("navigation_presets", sa.Column("parking_slot_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_navigation_presets_parking_slot_id",
        "navigation_presets",
        "parking_slots",
        ["parking_slot_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_navigation_presets_parking_slot_id", "navigation_presets", ["parking_slot_id"]
    )
    op.create_unique_constraint(
        "uq_navigation_preset_slot_category",
        "navigation_presets",
        ["map_version_id", "parking_slot_id", "category"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_navigation_preset_slot_category", "navigation_presets", type_="unique")
    op.drop_index("ix_navigation_presets_parking_slot_id", table_name="navigation_presets")
    op.drop_constraint(
        "fk_navigation_presets_parking_slot_id", "navigation_presets", type_="foreignkey"
    )
    op.drop_column("navigation_presets", "parking_slot_id")
    for name in (
        "compat_sequence_state_json",
        "last_received_at",
        "last_source_timestamp",
        "reported_map_checksum",
        "reported_map_version",
        "reported_map_code",
        "reported_site_code",
        "availability_state",
        "bridge_boot_id",
        "external_id",
    ):
        op.drop_column("robot_integration_profiles", name)
