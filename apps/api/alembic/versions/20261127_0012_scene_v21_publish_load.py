"""WEB-R1C 0012：V2.1 场景第二批——车位/走廊、原子发布与不可变资产、加载请求与回执（只加不改）。

沿用 0011 增量架构（docs/技术合同/web-v2.1/SCENE_0011_MIGRATION_DESIGN.md §2/§5）：
- scene_parking_slots / scene_corridors：storage_version=2 版本的车位、右车道走廊；
  scene_route_corridors：路线→走廊有序引用。全部用复合外键 (map_version_id, uid) 保证同版本。
- scene_points.slot_uid：点位→车位同版本复合外键（0011 的 parking_slot_id 指向旧表，V2 不用）。
- map_versions.scene_digest / scene_asset_id / published_by：发布时在同一事务计算
  contracts_v21.scene_digest 并把完整 SceneManifest 写成不可变资产（sha256 落库）。
  CHECK：storage_version=2 且 status<>'DRAFT' 时 digest 与资产必须存在（旧版本不受影响，不回填）。
- 已发布 V2 版本内容不可修改：数据库触发器拒绝对 PUBLISHED/ARCHIVED 版本的子表任何
  INSERT/UPDATE/DELETE，以及对其 map_versions 内容列的 UPDATE（应用层之外的兜底）。
  例外：整版本删除的级联（父行已不存在）与仅改 status→ARCHIVED。
- scene_load_requests / scene_load_receipts：加载请求与回执，每次加载只有一个最终回执
  （UNIQUE load_id）；robots.loaded_scene_* 只由确认回执写入，最新 load_id 记录在 robots。
- 不涉及任何生产下行；REAL 车辆不创建加载请求（应用层拒绝）。不执行生产迁移。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

JSON_T = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

revision = "20261127_0012"
down_revision = "20261127_0011"
branch_labels = None
depends_on = None

CHILD_TABLES = (
    "scene_points",
    "scene_routes",
    "scene_route_points",
    "scene_parking_slots",
    "scene_corridors",
    "scene_route_corridors",
)

IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION scene_v2_immutable_children() RETURNS trigger AS $$
DECLARE
  vid varchar(36);
  st varchar(16);
BEGIN
  vid := COALESCE(NEW.map_version_id, OLD.map_version_id);
  IF TG_OP = 'UPDATE' AND NEW.map_version_id IS DISTINCT FROM OLD.map_version_id THEN
    RAISE EXCEPTION 'SCENE_PUBLISHED_IMMUTABLE: version move forbidden' USING ERRCODE = 'P0001';
  END IF;
  SELECT status INTO st FROM map_versions WHERE id = vid;
  -- parent already gone = whole-version cascade delete: allowed
  IF st IS NOT NULL AND st <> 'DRAFT' THEN
    RAISE EXCEPTION 'SCENE_PUBLISHED_IMMUTABLE: % on % of % version %',
      TG_OP, TG_TABLE_NAME, st, vid
      USING ERRCODE = 'P0001';
  END IF;
  RETURN COALESCE(NEW, OLD);
END $$ LANGUAGE plpgsql;
"""

VERSION_FN = """
CREATE OR REPLACE FUNCTION scene_v2_immutable_version() RETURNS trigger AS $$
BEGIN
  IF OLD.storage_version = 2 AND OLD.status <> 'DRAFT' THEN
    -- only PUBLISHED -> ARCHIVED (status / nothing else) may change on a published V2 version
    IF NEW.status NOT IN ('PUBLISHED', 'ARCHIVED')
       OR (OLD.status = 'ARCHIVED' AND NEW.status <> 'ARCHIVED')
       OR NEW.scene_digest IS DISTINCT FROM OLD.scene_digest
       OR NEW.scene_asset_id IS DISTINCT FROM OLD.scene_asset_id
       OR NEW.revision IS DISTINCT FROM OLD.revision
       OR NEW.nav_image_sha256 IS DISTINCT FROM OLD.nav_image_sha256
       OR NEW.nav_yaml_sha256 IS DISTINCT FROM OLD.nav_yaml_sha256
       OR NEW.nav_map_checksum IS DISTINCT FROM OLD.nav_map_checksum
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.map_id IS DISTINCT FROM OLD.map_id
       OR NEW.resolution_m_per_pixel IS DISTINCT FROM OLD.resolution_m_per_pixel
       OR NEW.origin_x IS DISTINCT FROM OLD.origin_x
       OR NEW.origin_y IS DISTINCT FROM OLD.origin_y
       OR NEW.origin_yaw IS DISTINCT FROM OLD.origin_yaw
       OR NEW.traversability IS DISTINCT FROM OLD.traversability THEN
      RAISE EXCEPTION 'SCENE_PUBLISHED_IMMUTABLE: map_versions % (%)', OLD.id, OLD.status
        USING ERRCODE = 'P0001';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column("map_versions", sa.Column("scene_digest", sa.String(64), nullable=True))
    op.add_column("map_versions", sa.Column("scene_asset_id", sa.String(36), nullable=True))
    op.add_column("map_versions", sa.Column("published_by", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_map_versions_scene_asset", "map_versions", "assets", ["scene_asset_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_map_versions_published_by", "map_versions", "users", ["published_by"], ["id"]
    )
    op.create_check_constraint(
        "ck_map_versions_v2_published_digest",
        "map_versions",
        "storage_version = 1 OR status = 'DRAFT' OR "
        "(scene_digest IS NOT NULL AND scene_asset_id IS NOT NULL AND published_at IS NOT NULL)",
    )

    op.create_table(
        "scene_parking_slots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "map_version_id",
            sa.String(36),
            sa.ForeignKey("map_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slot_uid", sa.String(36), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("polygon_json", JSON_T, nullable=False),
        sa.Column("center_x", sa.Float(), nullable=False),
        sa.Column("center_y", sa.Float(), nullable=False),
        sa.Column("center_yaw", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("map_version_id", "slot_uid", name="uq_scene_slots_version_uid"),
        sa.UniqueConstraint("map_version_id", "code", name="uq_scene_slots_version_code"),
        sa.CheckConstraint(
            "center_yaw >= -3.1415927 AND center_yaw <= 3.1415927", name="ck_scene_slots_yaw"
        ),
        sa.CheckConstraint(
            "abs(center_x) <= 100000 AND abs(center_y) <= 100000", name="ck_scene_slots_coord"
        ),
    )
    op.create_index("ix_scene_slots_map_version_id", "scene_parking_slots", ["map_version_id"])
    op.add_column("scene_points", sa.Column("slot_uid", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_scene_points_slot_uid_same_version",
        "scene_points",
        "scene_parking_slots",
        ["map_version_id", "slot_uid"],
        ["map_version_id", "slot_uid"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "scene_corridors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "map_version_id",
            sa.String(36),
            sa.ForeignKey("map_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("corridor_uid", sa.String(36), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="RIGHT_LANE"),
        sa.Column("centerline_json", JSON_T, nullable=False),
        sa.Column("width_m", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "map_version_id", "corridor_uid", name="uq_scene_corridors_version_uid"
        ),
        sa.UniqueConstraint("map_version_id", "code", name="uq_scene_corridors_version_code"),
        sa.CheckConstraint("kind = 'RIGHT_LANE'", name="ck_scene_corridors_kind"),
        sa.CheckConstraint("width_m >= 0.5 AND width_m <= 10", name="ck_scene_corridors_width"),
    )
    op.create_index("ix_scene_corridors_map_version_id", "scene_corridors", ["map_version_id"])

    op.create_table(
        "scene_route_corridors",
        sa.Column("map_version_id", sa.String(36), nullable=False),
        sa.Column("route_uid", sa.String(36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("corridor_uid", sa.String(36), nullable=False),
        sa.PrimaryKeyConstraint(
            "map_version_id", "route_uid", "seq", name="pk_scene_route_corridors"
        ),
        sa.ForeignKeyConstraint(
            ["map_version_id", "route_uid"],
            ["scene_routes.map_version_id", "scene_routes.route_uid"],
            ondelete="CASCADE",
            name="fk_scene_route_corridors_route_same_version",
        ),
        sa.ForeignKeyConstraint(
            ["map_version_id", "corridor_uid"],
            ["scene_corridors.map_version_id", "scene_corridors.corridor_uid"],
            name="fk_scene_route_corridors_corridor_same_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("seq >= 0", name="ck_scene_route_corridors_seq"),
    )

    op.create_table(
        "scene_load_requests",
        sa.Column("load_id", sa.String(36), primary_key=True),
        sa.Column("robot_id", sa.String(36), sa.ForeignKey("robots.id"), nullable=False),
        sa.Column("vehicle_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column(
            "map_version_id", sa.String(36), sa.ForeignKey("map_versions.id"), nullable=False
        ),
        sa.Column("scene_version", sa.Integer(), nullable=False),
        sa.Column("scene_digest", sa.String(64), nullable=False),
        sa.Column("nav_map_checksum", sa.String(128), nullable=False),
        sa.Column("target_boot_id", sa.String(36), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("payload_json", JSON_T, nullable=False),
        sa.CheckConstraint("environment IN ('SIM','REAL')", name="ck_scene_load_env"),
        sa.CheckConstraint(
            "state IN ('PENDING','LOADED_CONFIRMED','LOAD_REJECTED','SUPERSEDED','EXPIRED')",
            name="ck_scene_load_state",
        ),
        sa.CheckConstraint("expires_at > issued_at", name="ck_scene_load_window"),
    )
    op.create_index(
        "ix_scene_load_requests_robot", "scene_load_requests", ["robot_id", "issued_at"]
    )
    op.create_table(
        "scene_load_receipts",
        sa.Column("receipt_id", sa.String(36), primary_key=True),
        sa.Column(
            "load_id", sa.String(36), sa.ForeignKey("scene_load_requests.load_id"), nullable=False
        ),
        sa.Column("robot_id", sa.String(36), sa.ForeignKey("robots.id"), nullable=False),
        sa.Column("boot_id", sa.String(36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirm_code", sa.String(64), nullable=False),
        sa.Column("payload_json", JSON_T, nullable=False),
        sa.UniqueConstraint("load_id", name="uq_scene_load_receipts_load"),
        sa.CheckConstraint(
            "result IN ('LOADED','REJECTED','FAILED')", name="ck_scene_receipt_result"
        ),
    )
    op.add_column("robots", sa.Column("loaded_scene_version_id", sa.String(36), nullable=True))
    op.add_column("robots", sa.Column("loaded_scene_digest", sa.String(64), nullable=True))
    op.add_column("robots", sa.Column("loaded_scene_boot_id", sa.String(36), nullable=True))
    op.add_column("robots", sa.Column("latest_scene_load_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_robots_loaded_scene_version",
        "robots",
        "map_versions",
        ["loaded_scene_version_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_robots_latest_scene_load",
        "robots",
        "scene_load_requests",
        ["latest_scene_load_id"],
        ["load_id"],
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(IMMUTABLE_FN)
        op.execute(VERSION_FN)
        for table in CHILD_TABLES:
            op.execute(
                f"CREATE TRIGGER trg_{table}_immutable "
                f"BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION scene_v2_immutable_children()"
            )
        op.execute(
            "CREATE TRIGGER trg_map_versions_v2_immutable BEFORE UPDATE ON map_versions "
            "FOR EACH ROW EXECUTE FUNCTION scene_v2_immutable_version()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_map_versions_v2_immutable ON map_versions")
        for table in CHILD_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute("DROP FUNCTION IF EXISTS scene_v2_immutable_version()")
        op.execute("DROP FUNCTION IF EXISTS scene_v2_immutable_children()")
    op.drop_constraint("fk_robots_latest_scene_load", "robots", type_="foreignkey")
    op.drop_constraint("fk_robots_loaded_scene_version", "robots", type_="foreignkey")
    for name in (
        "latest_scene_load_id",
        "loaded_scene_boot_id",
        "loaded_scene_digest",
        "loaded_scene_version_id",
    ):
        op.drop_column("robots", name)
    op.drop_table("scene_load_receipts")
    op.drop_index("ix_scene_load_requests_robot", table_name="scene_load_requests")
    op.drop_table("scene_load_requests")
    op.drop_table("scene_route_corridors")
    op.drop_index("ix_scene_corridors_map_version_id", table_name="scene_corridors")
    op.drop_table("scene_corridors")
    op.drop_constraint("fk_scene_points_slot_uid_same_version", "scene_points", type_="foreignkey")
    op.drop_column("scene_points", "slot_uid")
    op.drop_index("ix_scene_slots_map_version_id", table_name="scene_parking_slots")
    op.drop_table("scene_parking_slots")
    op.drop_constraint("ck_map_versions_v2_published_digest", "map_versions", type_="check")
    op.drop_constraint("fk_map_versions_published_by", "map_versions", type_="foreignkey")
    op.drop_constraint("fk_map_versions_scene_asset", "map_versions", type_="foreignkey")
    for name in ("published_by", "scene_asset_id", "scene_digest"):
        op.drop_column("map_versions", name)
