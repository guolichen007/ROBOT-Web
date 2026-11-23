#!/usr/bin/env bash
# WEB-R1-MIN-FINAL P1-04：隔离数据库回滚演练（只用临时库，绝不连接生产）。
#
# 需要：PG 18 客户端（pg_dump/pg_restore/psql）在 PATH；PGHOST/PGPORT/PGUSER/PGPASSWORD 指向
#       一个可建库的隔离 PostgreSQL；在仓库根目录执行；BOOTSTRAP_ADMIN_PASSWORD 等测试变量已设置。
# 流程：
#   1. 新建临时库 → head + seed → 降到 0009（无 UNMATCHED，合法）→ 备份 B0009（模拟“发布前备份”）
#   2. 升级到 0010（WEB-R1-MIN）→ 经 mqtt-ingress handle_alarm 写入 MATCHED + UNMATCHED 火警
#   3. 备份 B0010
#   4. 反证：alembic downgrade 0009 必须失败；库仍在 0010，火警行数 / 内容哈希不变
#   5. 恢复 B0010 到新库：版本、火警行数、内容哈希一致（证明备份可用）
#   6. 恢复 B0009 到新库：版本 = 0009，再 upgrade head 成功（应用回滚 + 数据回滚路径）
#   7. 导出 UNMATCHED 火警 CSV（降级前人工归档的依据），不删除任何火警
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STAMP="$(date +%s)"
SRC="drill_src_${STAMP}"; R10="drill_r10_${STAMP}"; R09="drill_r09_${STAMP}"
OUT="${DRILL_OUT:-$ROOT/rollback-drill-${STAMP}}"
mkdir -p "$OUT"
base_url() { echo "postgresql+psycopg://${PGUSER}:${PGPASSWORD}@${PGHOST}:${PGPORT}/$1"; }
q() { psql -X -q -At -d "$1" -c "$2"; }
alembic_on() { (cd "$ROOT/apps/api" && DATABASE_URL="$(base_url "$1")" alembic "${@:2}"); }
fire_digest() { q "$1" "SELECT count(*) || ':' || coalesce(md5(string_agg(id||event_code||location_state||coalesce(parking_slot_id,'-')||state||occurrence_count, ',' ORDER BY id)),'') FROM fire_events"; }
cleanup() { for d in "$SRC" "$R10" "$R09"; do dropdb --if-exists "$d" >/dev/null 2>&1 || true; done; }
trap cleanup EXIT

createdb "$SRC"
# 种子数据用当前代码写入（head），再降到 0009 —— 此时尚无 UNMATCHED 火警，降级合法，
# 得到的就是“发布前（0009）”结构的真实库。
alembic_on "$SRC" upgrade head >/dev/null 2>&1
(cd "$ROOT/apps/api" && DATABASE_URL="$(base_url "$SRC")" python -m app.db.seed >/dev/null)
alembic_on "$SRC" downgrade 20261021_0009 >/dev/null 2>&1
pg_dump -Fc -d "$SRC" -f "$OUT/B0009.dump"
echo "STEP1 B0009 version=$(q "$SRC" 'select version_num from alembic_version') size=$(stat -c %s "$OUT/B0009.dump")"

alembic_on "$SRC" upgrade head >/dev/null 2>&1
DATABASE_URL="$(base_url "$SRC")" python - "$ROOT" <<'PY'
import importlib.util, sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
root = Path(sys.argv[1]); sys.path[:0] = [str(root / "apps/api"), str(root)]
from sqlalchemy import select
from app.db.models import Robot
from app.db.session import SessionLocal
spec = importlib.util.spec_from_file_location("drill_ingress", root / "services/mqtt-ingress/main.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
with SessionLocal.begin() as db:
    robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
    for code in ("A-12", "ZZ-DRILL-404", None):
        mod.handle_alarm(db, robot, {"message_id": str(uuid4()), "event_id": f"drill-{uuid4().hex[:6]}",
            "fire_type": "smoke", "severity": "HIGH", "parking_slot_code": code,
            "position": {"x": 1, "y": 1, "theta": 0}, "media": {"label": "SIMULATED"}}, datetime.now(UTC))
PY
BEFORE="$(fire_digest "$SRC")"
UNMATCHED="$(q "$SRC" "select count(*) from fire_events where location_state='UNMATCHED'")"
echo "STEP2 version=$(q "$SRC" 'select version_num from alembic_version') fire=$BEFORE unmatched=$UNMATCHED"
[ "$UNMATCHED" -ge 1 ] || { echo "FAIL: 未生成 UNMATCHED 火警"; exit 1; }

pg_dump -Fc -d "$SRC" -f "$OUT/B0010.dump"
echo "STEP3 B0010 size=$(stat -c %s "$OUT/B0010.dump")"

if alembic_on "$SRC" downgrade 20261021_0009 >"$OUT/downgrade.log" 2>&1; then
  echo "FAIL: 存在 UNMATCHED 火警时降级不应成功"; exit 1
fi
grep -q "未匹配车位火警" "$OUT/downgrade.log" || { echo "FAIL: 降级失败原因不符"; exit 1; }
AFTER="$(fire_digest "$SRC")"; V="$(q "$SRC" 'select version_num from alembic_version')"
[ "$AFTER" = "$BEFORE" ] && [ "$V" = "20261122_0010" ] || { echo "FAIL: 降级失败后数据/版本被改动"; exit 1; }
echo "STEP4 DOWNGRADE_REFUSED=PASS version=$V fire=$AFTER"

createdb "$R10"; pg_restore -d "$R10" --no-owner "$OUT/B0010.dump"
[ "$(fire_digest "$R10")" = "$BEFORE" ] && [ "$(q "$R10" 'select version_num from alembic_version')" = "20261122_0010" ] \
  || { echo "FAIL: B0010 恢复不一致"; exit 1; }
echo "STEP5 RESTORE_B0010=PASS fire=$(fire_digest "$R10")"

createdb "$R09"; pg_restore -d "$R09" --no-owner "$OUT/B0009.dump"
[ "$(q "$R09" 'select version_num from alembic_version')" = "20261021_0009" ] || { echo "FAIL: B0009 版本不符"; exit 1; }
alembic_on "$R09" upgrade head >/dev/null 2>&1
echo "STEP6 RESTORE_B0009=PASS then_upgrade=$(q "$R09" 'select version_num from alembic_version')"

psql -X -q -d "$SRC" -c "\copy (SELECT id,event_code,robot_id,reported_slot_code,fire_type,severity,state,first_seen_at,last_seen_at,occurrence_count FROM fire_events WHERE location_state='UNMATCHED' ORDER BY first_seen_at) TO '$OUT/unmatched_fire_events.csv' WITH CSV HEADER"
echo "STEP7 UNMATCHED_EXPORT rows=$(($(wc -l < "$OUT/unmatched_fire_events.csv") - 1)) remaining_in_db=$(q "$SRC" "select count(*) from fire_events where location_state='UNMATCHED'")"
echo "ROLLBACK_DRILL=PASS out=$OUT"
