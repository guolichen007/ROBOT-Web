"""WEB-R1B 0011：V2.1 地图场景持久化第一批（只加不改）。

设计依据 docs/技术合同/web-v2.1/SCENE_0011_MIGRATION_DESIGN.md。
- map_versions.storage_version：1 = 旧表（parking_slots / inspection_points / trajectories），
  2 = V2.1 场景表（scene_points / scene_routes / scene_route_points）。显式识别，
  **不按新表是否为空隐式回退**。已有数据全部为 1，旧表与历史数据原样保留。
- storage_version=2 的版本以新表为唯一写入权威；旧写接口对其拒绝（应用层）。
- 跨版本引用由**复合外键 (map_version_id, ...)** 在数据库层保证，不依赖应用自觉。
- revision：草稿乐观锁（UPDATE ... WHERE revision=? AND status='DRAFT'），与保存同事务。
- 导航地图身份：image/yaml sha256、像素尺寸、阈值；nav_map_checksum 待车端算法确认（可空）。
- 不涉及任何下行、任务、调度；不执行生产迁移。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261127_0011"
down_revision = "20261122_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "map_versions",
        sa.Column("storage_version", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "map_versions", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    for name, column_type in (
        ("nav_image_sha256", sa.String(64)),
        ("nav_yaml_sha256", sa.String(64)),
        ("nav_map_checksum", sa.String(128)),
        ("nav_image_asset_id", sa.String(36)),
        ("nav_yaml_asset_id", sa.String(36)),
        ("width_px", sa.Integer()),
        ("height_px", sa.Integer()),
        ("occupied_thresh", sa.Float()),
        ("free_thresh", sa.Float()),
        ("negate", sa.SmallInteger()),
        ("origin_yaw", sa.Float()),
    ):
        op.add_column("map_versions", sa.Column(name, column_type, nullable=True))
    op.add_column(
        "map_versions",
        sa.Column(
            "traversability",
            sa.String(24),
            nullable=False,
            server_default="PENDING_VERIFICATION",
        ),
    )
    op.create_check_constraint(
        "ck_map_versions_storage_version", "map_versions", "storage_version IN (1, 2)"
    )
    op.create_check_constraint(
        "ck_map_versions_traversability",
        "map_versions",
        "traversability IN ('VERIFIED','PENDING_VERIFICATION','FAILED')",
    )
    op.create_check_constraint(
        "ck_map_versions_v2_nav_identity",
        "map_versions",
        "storage_version = 1 OR (nav_image_sha256 IS NOT NULL AND nav_yaml_sha256 IS NOT NULL "
        "AND width_px > 0 AND height_px > 0 AND free_thresh < occupied_thresh)",
    )
    op.create_foreign_key(
        "fk_map_versions_nav_image_asset", "map_versions", "assets", ["nav_image_asset_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_map_versions_nav_yaml_asset", "map_versions", "assets", ["nav_yaml_asset_id"], ["id"]
    )
    # composite-FK targets: (map_version_id, id) on the referenced tables
    op.create_unique_constraint(
        "uq_parking_slots_version_id", "parking_slots", ["map_version_id", "id"]
    )

    op.create_table(
        "scene_points",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "map_version_id",
            sa.String(36),
            sa.ForeignKey("map_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("point_uid", sa.String(36), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("yaw", sa.Float(), nullable=False),
        sa.Column("xy_tolerance_m", sa.Float(), nullable=True),
        sa.Column("yaw_tolerance_rad", sa.Float(), nullable=True),
        sa.Column("parking_slot_id", sa.String(36), nullable=True),
        sa.Column("taught_from", sa.String(24), nullable=False, server_default="MANUAL_INPUT"),
        sa.UniqueConstraint("map_version_id", "point_uid", name="uq_scene_points_version_uid"),
        sa.UniqueConstraint("map_version_id", "code", name="uq_scene_points_version_code"),
        sa.ForeignKeyConstraint(
            ["map_version_id", "parking_slot_id"],
            ["parking_slots.map_version_id", "parking_slots.id"],
            name="fk_scene_points_slot_same_version",
        ),
        sa.CheckConstraint(
            "kind IN ('INSPECTION','FIRE_OPERATION','RETURN_HOME','WAITING','TRANSIT')",
            name="ck_scene_points_kind",
        ),
        sa.CheckConstraint(
            "yaw >= -3.1415927 AND yaw <= 3.1415927", name="ck_scene_points_yaw_range"
        ),
        sa.CheckConstraint(
            "abs(x) <= 100000 AND abs(y) <= 100000", name="ck_scene_points_coord_range"
        ),
        sa.CheckConstraint(
            "taught_from IN ('MANUAL_INPUT','MAP_CLICK','VEHICLE_POSE')",
            name="ck_scene_points_taught_from",
        ),
    )
    op.create_index("ix_scene_points_map_version_id", "scene_points", ["map_version_id"])

    op.create_table(
        "scene_routes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "map_version_id",
            sa.String(36),
            sa.ForeignKey("map_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("route_uid", sa.String(36), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("map_version_id", "route_uid", name="uq_scene_routes_version_uid"),
        sa.UniqueConstraint("map_version_id", "code", name="uq_scene_routes_version_code"),
    )
    op.create_index("ix_scene_routes_map_version_id", "scene_routes", ["map_version_id"])

    op.create_table(
        "scene_route_points",
        sa.Column("map_version_id", sa.String(36), nullable=False),
        sa.Column("route_uid", sa.String(36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("point_uid", sa.String(36), nullable=False),
        sa.Column("arrival", sa.String(16), nullable=False, server_default="PASS_THROUGH"),
        sa.PrimaryKeyConstraint("map_version_id", "route_uid", "seq", name="pk_scene_route_points"),
        sa.ForeignKeyConstraint(
            ["map_version_id", "route_uid"],
            ["scene_routes.map_version_id", "scene_routes.route_uid"],
            ondelete="CASCADE",
            name="fk_scene_route_points_route_same_version",
        ),
        # DEFERRABLE INITIALLY DEFERRED (checked at COMMIT): a referenced point can never be
        # deleted on its own, while a whole-version delete cascades cleanly. Plain NO ACTION is
        # NOT enough on PostgreSQL: the cascade into scene_points is checked before the cascaded
        # scene_route_points rows are gone (found by test teardown, WEB-R1B).
        sa.ForeignKeyConstraint(
            ["map_version_id", "point_uid"],
            ["scene_points.map_version_id", "scene_points.point_uid"],
            name="fk_scene_route_points_point_same_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("seq >= 0", name="ck_scene_route_points_seq"),
        sa.CheckConstraint(
            "arrival IN ('PASS_THROUGH','STOP')", name="ck_scene_route_points_arrival"
        ),
    )


def downgrade() -> None:
    op.drop_table("scene_route_points")
    op.drop_index("ix_scene_routes_map_version_id", table_name="scene_routes")
    op.drop_table("scene_routes")
    op.drop_index("ix_scene_points_map_version_id", table_name="scene_points")
    op.drop_table("scene_points")
    op.drop_constraint("uq_parking_slots_version_id", "parking_slots", type_="unique")
    op.drop_constraint("fk_map_versions_nav_yaml_asset", "map_versions", type_="foreignkey")
    op.drop_constraint("fk_map_versions_nav_image_asset", "map_versions", type_="foreignkey")
    for name in (
        "ck_map_versions_v2_nav_identity",
        "ck_map_versions_traversability",
        "ck_map_versions_storage_version",
    ):
        op.drop_constraint(name, "map_versions", type_="check")
    for name in (
        "traversability",
        "origin_yaw",
        "negate",
        "free_thresh",
        "occupied_thresh",
        "height_px",
        "width_px",
        "nav_yaml_asset_id",
        "nav_image_asset_id",
        "nav_map_checksum",
        "nav_yaml_sha256",
        "nav_image_sha256",
        "revision",
        "storage_version",
    ):
        op.drop_column("map_versions", name)
