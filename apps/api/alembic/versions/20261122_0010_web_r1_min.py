"""WEB-R1-MIN：账号会话版本、巡检计划草稿/右侧作业、未匹配位置火警。

- users.token_version：权限/状态/密码变化后递增，旧访问令牌、WS、媒体票据立即失效。
- users.deactivated_at / deactivated_by：逻辑停用留痕（不物理删除，保留历史关联）。
- patrol_plans：execution_mode（PREVIEW_ONLY 默认）、status、作业方向与目标侧向间距、
  右侧防火罩机构状态（仅展示）；全部为配置字段，不进入导航/机械控制。
- patrol_plans.trajectory_id 原样保留；新增 created_via 区分 Web 草稿与历史计划。
- fire_events.parking_slot_id 允许为空 + location_state（MATCHED / UNMATCHED）。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261122_0010"
down_revision = "20261021_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("users", sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("deactivated_by", sa.String(36), nullable=True))

    op.add_column(
        "patrol_plans",
        sa.Column(
            "execution_mode", sa.String(24), nullable=False, server_default="LEGACY_EXECUTABLE"
        ),
    )
    op.add_column(
        "patrol_plans",
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
    )
    op.add_column(
        "patrol_plans",
        sa.Column("work_side", sa.String(8), nullable=False, server_default="RIGHT"),
    )
    op.add_column("patrol_plans", sa.Column("target_lateral_min_m", sa.Float(), nullable=True))
    op.add_column("patrol_plans", sa.Column("target_lateral_max_m", sa.Float(), nullable=True))
    op.add_column(
        "patrol_plans",
        sa.Column("schedule_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column("patrol_plans", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("patrol_plans", sa.Column("updated_by", sa.String(36), nullable=True))
    op.add_column(
        "patrol_plans",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_patrol_plan_execution_mode",
        "patrol_plans",
        "execution_mode IN ('PREVIEW_ONLY','LEGACY_EXECUTABLE')",
    )
    op.create_check_constraint(
        "ck_patrol_plan_status", "patrol_plans", "status IN ('DRAFT','PAUSED','ACTIVE')"
    )
    op.create_check_constraint("ck_patrol_plan_work_side", "patrol_plans", "work_side = 'RIGHT'")
    op.create_check_constraint(
        "ck_patrol_plan_lateral",
        "patrol_plans",
        "(target_lateral_min_m IS NULL AND target_lateral_max_m IS NULL) OR "
        "(target_lateral_min_m >= 0.20 AND target_lateral_max_m <= 0.30 "
        "AND target_lateral_min_m <= target_lateral_max_m)",
    )
    # PREVIEW_ONLY 计划在数据库层面也不得处于可执行（enabled）状态。
    op.create_check_constraint(
        "ck_patrol_plan_preview_not_enabled",
        "patrol_plans",
        "execution_mode <> 'PREVIEW_ONLY' OR enabled = false",
    )

    op.alter_column("fire_events", "parking_slot_id", existing_type=sa.String(36), nullable=True)
    op.add_column(
        "fire_events",
        sa.Column("location_state", sa.String(16), nullable=False, server_default="MATCHED"),
    )
    op.add_column("fire_events", sa.Column("reported_slot_code", sa.String(64), nullable=True))
    op.create_check_constraint(
        "ck_fire_event_location_state",
        "fire_events",
        "(location_state = 'MATCHED' AND parking_slot_id IS NOT NULL) OR "
        "(location_state = 'UNMATCHED' AND parking_slot_id IS NULL)",
    )


def downgrade() -> None:
    # 存在未匹配位置火警时禁止静默降级（会丢失事件或违反 NOT NULL）。
    bind = op.get_bind()
    unmatched = bind.execute(
        sa.text("SELECT count(*) FROM fire_events WHERE parking_slot_id IS NULL")
    ).scalar()
    if unmatched:
        raise RuntimeError(
            f"存在 {unmatched} 条未匹配车位火警，降级前必须先人工归档或导出，禁止静默删除"
        )
    op.drop_constraint("ck_fire_event_location_state", "fire_events", type_="check")
    op.drop_column("fire_events", "reported_slot_code")
    op.drop_column("fire_events", "location_state")
    op.alter_column("fire_events", "parking_slot_id", existing_type=sa.String(36), nullable=False)

    op.drop_constraint("ck_patrol_plan_preview_not_enabled", "patrol_plans", type_="check")
    op.drop_constraint("ck_patrol_plan_lateral", "patrol_plans", type_="check")
    op.drop_constraint("ck_patrol_plan_work_side", "patrol_plans", type_="check")
    op.drop_constraint("ck_patrol_plan_status", "patrol_plans", type_="check")
    op.drop_constraint("ck_patrol_plan_execution_mode", "patrol_plans", type_="check")
    for column in (
        "revision",
        "updated_by",
        "description",
        "schedule_json",
        "target_lateral_max_m",
        "target_lateral_min_m",
        "work_side",
        "status",
        "execution_mode",
    ):
        op.drop_column("patrol_plans", column)

    op.drop_column("users", "deactivated_by")
    op.drop_column("users", "deactivated_at")
    op.drop_column("users", "token_version")
