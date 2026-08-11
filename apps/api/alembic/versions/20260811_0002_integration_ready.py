"""integration-ready boot sessions and real monthly partitions

Revision ID: 20260811_0002
Revises: 20260810_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20260811_0002"
down_revision = "20260810_0001"
branch_labels = None
depends_on = None


PARTITION_FUNCTION = r"""
CREATE OR REPLACE FUNCTION ensure_firebot_month_partitions(
    reference_time timestamptz DEFAULT now(), months_ahead integer DEFAULT 2
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    month_start timestamptz;
    offset_value integer;
    suffix text;
    parent_name text;
BEGIN
    FOREACH parent_name IN ARRAY ARRAY['telemetry_samples', 'sensor_samples'] LOOP
        FOR offset_value IN 0..months_ahead LOOP
            month_start := date_trunc('month', reference_time AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                           + make_interval(months => offset_value);
            suffix := to_char(month_start AT TIME ZONE 'UTC', 'YYYY_MM');
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                parent_name || '_' || suffix,
                parent_name,
                month_start,
                month_start + interval '1 month'
            );
        END LOOP;
    END LOOP;
END;
$$;
"""


def _migrate_parent(parent: str) -> None:
    default = f"{parent}_default"
    legacy = f"{parent}_legacy_default"
    op.execute(f"ALTER TABLE {parent} DETACH PARTITION {default}")
    op.execute(f"ALTER TABLE {default} RENAME TO {legacy}")
    op.execute(
        f"""
        DO $$
        DECLARE month_start timestamptz; suffix text;
        BEGIN
          FOR month_start IN
            SELECT DISTINCT
              date_trunc('month', server_received_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            FROM {legacy}
          LOOP
            suffix := to_char(month_start AT TIME ZONE 'UTC', 'YYYY_MM');
            EXECUTE format(
              'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
              '{parent}_' || suffix,
              '{parent}',
              month_start,
              month_start + interval '1 month'
            );
          END LOOP;
        END $$;
        """
    )
    op.execute(f"CREATE TABLE {default} PARTITION OF {parent} DEFAULT")
    op.execute(f"INSERT INTO {parent} SELECT * FROM {legacy}")
    op.execute(f"DROP TABLE {legacy}")


def upgrade() -> None:
    # The legacy baseline migration calls current ``Base.metadata.create_all``.
    # Therefore a clean install may already contain this table, while an
    # in-place upgrade from the released baseline will not. Support both paths.
    if not sa.inspect(op.get_bind()).has_table("robot_boot_sessions"):
        op.create_table(
            "robot_boot_sessions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("robot_id", sa.String(36), sa.ForeignKey("robots.id"), nullable=False),
            sa.Column("boot_id", sa.String(36), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("robot_id", "boot_id", name="uq_robot_boot_session"),
        )
    index_names = {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("robot_boot_sessions")
    }
    if "ix_robot_boot_sessions_robot_id" not in index_names:
        op.create_index("ix_robot_boot_sessions_robot_id", "robot_boot_sessions", ["robot_id"])
    op.execute(PARTITION_FUNCTION)
    _migrate_parent("telemetry_samples")
    _migrate_parent("sensor_samples")
    op.execute("SELECT ensure_firebot_month_partitions(now(), 2)")


def _collapse_parent(parent: str) -> None:
    stage = f"{parent}_downgrade_stage"
    op.execute(f"CREATE TABLE {stage} AS TABLE {parent} WITH DATA")
    op.execute(
        f"""
        DO $$
        DECLARE child_name text;
        BEGIN
          FOR child_name IN
            SELECT c.relname
            FROM pg_inherits i
            JOIN pg_class p ON p.oid = i.inhparent
            JOIN pg_class c ON c.oid = i.inhrelid
            WHERE p.relname = '{parent}'
          LOOP
            EXECUTE format('ALTER TABLE {parent} DETACH PARTITION %I', child_name);
            EXECUTE format('DROP TABLE %I', child_name);
          END LOOP;
        END $$;
        """
    )
    op.execute(f"CREATE TABLE {parent}_default PARTITION OF {parent} DEFAULT")
    op.execute(f"INSERT INTO {parent} SELECT * FROM {stage}")
    op.execute(f"DROP TABLE {stage}")


def downgrade() -> None:
    _collapse_parent("telemetry_samples")
    _collapse_parent("sensor_samples")
    op.execute("DROP FUNCTION IF EXISTS ensure_firebot_month_partitions(timestamptz, integer)")
    op.drop_index("ix_robot_boot_sessions_robot_id", table_name="robot_boot_sessions")
    op.drop_table("robot_boot_sessions")
