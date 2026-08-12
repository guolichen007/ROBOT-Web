"""industrial operations, patrol and compatibility data model

Revision ID: 20260812_0003
Revises: 20260811_0002
"""

import sqlalchemy as sa
from alembic import op
from app.db.models import Base

revision = "20260812_0003"
down_revision = "20260811_0002"
branch_labels = None
depends_on = None


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


def _nullable(table: str, column: str) -> bool:
    info = {item["name"]: item for item in sa.inspect(op.get_bind()).get_columns(table)}
    return bool(info[column]["nullable"])


def upgrade() -> None:
    # The released baseline's first migration creates current metadata. checkfirst keeps
    # both clean installs and upgrades from 0002 deterministic.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)
    for table, column in (
        ("robots", "battery"),
        ("telemetry_samples", "linear_speed"),
        ("telemetry_samples", "angular_speed"),
        ("telemetry_samples", "battery"),
        ("sensor_samples", "smoke"),
        ("sensor_samples", "bottom_ir"),
        ("sensor_samples", "top_ir_max"),
    ):
        if not _nullable(table, column):
            op.alter_column(table, column, existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    for table, column in (
        ("robots", "battery"),
        ("telemetry_samples", "linear_speed"),
        ("telemetry_samples", "angular_speed"),
        ("telemetry_samples", "battery"),
        ("sensor_samples", "smoke"),
        ("sensor_samples", "bottom_ir"),
        ("sensor_samples", "top_ir_max"),
    ):
        if _nullable(table, column):
            op.execute(f"UPDATE {table} SET {column} = 0 WHERE {column} IS NULL")
            op.alter_column(table, column, existing_type=sa.Float(), nullable=False)
    for name in reversed(NEW_TABLES):
        Base.metadata.tables[name].drop(bind=op.get_bind(), checkfirst=True)
