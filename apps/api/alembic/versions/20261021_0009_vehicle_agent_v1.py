"""每车唯一控制协议；旧车默认保持 CANONICAL_MQTT，不自动启用 v1。"""

import sqlalchemy as sa
from alembic import op

revision = "20261021_0009"
down_revision = "20260902_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "robots",
        sa.Column("agent_protocol", sa.String(32), nullable=False, server_default="CANONICAL_MQTT"),
    )
    op.add_column(
        "robots",
        sa.Column("agent_state_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_check_constraint(
        "ck_robot_agent_protocol",
        "robots",
        "agent_protocol IN ('CANONICAL_MQTT','VEHICLE_AGENT_V1','ROS_COMPAT')",
    )
    op.add_column("commands", sa.Column("ack_payload_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("commands", "ack_payload_json")
    op.drop_constraint("ck_robot_agent_protocol", "robots", type_="check")
    op.drop_column("robots", "agent_state_json")
    op.drop_column("robots", "agent_protocol")
