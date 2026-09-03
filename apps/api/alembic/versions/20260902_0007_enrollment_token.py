"""Enrollment token store

Revision ID: 20260902_0007
Revises: 20260902_0006
"""

import sqlalchemy as sa
from alembic import op

revision = "20260902_0007"
down_revision = "20260902_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enrollment_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("device_id", sa.String(64), unique=True, index=True),
        sa.Column("token_hash", sa.String(64)),
        sa.Column("credential_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("enrollment_tokens")
