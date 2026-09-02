"""Stop operation stationary observation dedup

Revision ID: 20260902_0006
Revises: 20260813_0005
"""

import sqlalchemy as sa
from alembic import op

revision = "20260902_0006"
down_revision = "20260813_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stop_operations",
        sa.Column("last_stationary_observation_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stop_operations", "last_stationary_observation_at")
