"""initial Firebot V2 baseline schema

Revision ID: 20260810_0001
Revises:
"""

from alembic import op
from app.db.models import Base

revision = "20260810_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    op.execute(
        "CREATE TABLE IF NOT EXISTS telemetry_samples_default "
        "PARTITION OF telemetry_samples DEFAULT"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS sensor_samples_default PARTITION OF sensor_samples DEFAULT"
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP TABLE IF EXISTS telemetry_samples_default")
    op.execute("DROP TABLE IF EXISTS sensor_samples_default")
    Base.metadata.drop_all(bind=bind)
