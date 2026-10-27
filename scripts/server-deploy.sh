#!/usr/bin/env bash
# server-deploy.sh — SERVER 模块化部署（exact SHA + immutable image + manifest + rollback）
#
# 用法：
#   TARGET_SHA=<40SHA> ./server-deploy.sh preflight
#   TARGET_SHA=<40SHA> ./server-deploy.sh bootstrap        # fresh server：基础服务 + fresh DB + SSOT(previous=none)
#   TARGET_SHA=<40SHA> ./server-deploy.sh migrate          # 需 BACKUP_VERIFIED=1
#   TARGET_SHA=<40SHA> ./server-deploy.sh api|web|worker|control-plane|all-app
#   TARGET_SHA=<40SHA> ./server-deploy.sh verify
#   TARGET_SHA=<40SHA> ./server-deploy.sh rollback
#   TARGET_SHA=<40SHA> ./server-deploy.sh refresh-ros-compat # 同 SHA 维护，不改 SSOT
#   TARGET_SHA=<40SHA> ./server-deploy.sh backup           # pg_dump + manifest
#   TARGET_SHA=<40SHA> ./server-deploy.sh backup-verify <file>  # 隔离临时库 restore 验证
#   ./server-deploy.sh status                              # 读 current.json，不需 TARGET_SHA
#
# U-Dao 云模式（release 模式）环境：
#   FIREBOT_DEPLOY_ROOT 部署状态根（默认 /opt/firebot/deployments，legacy 兼容）
#   FIREBOT_RELEASE_ROOT immutable release 目录（releases/<40SHA>，禁止 checkout 修改）
#   FIREBOT_COMPOSE_FILES 空格分隔 compose 文件（release 模式默认基座+U-Dao override）
#   U_DAO_MODE=1 由 FIREBOT_COMPOSE_FILES 含 deploy/u-dao/compose.u-dao.yml 自动推导
#
# 服务分组：control-plane = api+mqtt-ingress+command-dispatcher+task-worker
# 绝不默认重启 postgres/redis/mosquitto/nginx/mediamtx。
# 版本 SSOT：$DEPLOY_ROOT/current.json（current/previous runtime；fresh 首发布 previous=none）
set -euo pipefail

ENV_FILE="${SERVER_ENV_FILE:-.env.server}"
DEPLOY_ROOT="${FIREBOT_DEPLOY_ROOT:-/opt/firebot/deployments}"
CURRENT_JSON="$DEPLOY_ROOT/current.json"
RELEASE_ROOT="${FIREBOT_RELEASE_ROOT:-}"
COMPOSE_FILES="${FIREBOT_COMPOSE_FILES:-docker-compose.server.yml}"
if [ -n "$RELEASE_ROOT" ] && [ -z "${FIREBOT_COMPOSE_FILES:-}" ]; then
  COMPOSE_FILES="$RELEASE_ROOT/docker-compose.server.yml $RELEASE_ROOT/deploy/u-dao/compose.u-dao.yml"
fi
case " $COMPOSE_FILES " in
  *compose.u-dao.yml*) U_DAO_MODE=1 ;;
  *) U_DAO_MODE=0 ;;
esac
source scripts/docker_cmd.sh
docker_cmd_detect || exit 1
COMPOSE=("${DOCKER[@]}" compose --env-file "$ENV_FILE")
for f in $COMPOSE_FILES; do COMPOSE+=(-f "$f"); done
TARGET_SHA="${TARGET_SHA:-}"
PY_IMAGE="firebot-python:${TARGET_SHA}"
WEB_IMAGE="firebot-web:${TARGET_SHA}"

# sudo 会重置环境变量（env_reset）：compose 插值所需的镜像变量必须经 `sudo -n env` 显式传递
# （其余插值变量一律以 --env-file 为唯一来源，见 .env.server 内的 SERVER_ENV_FILE）。
compose_env_run() {
  local pairs=() var
  for var in FIREBOT_PYTHON_IMAGE FIREBOT_WEB_IMAGE FIREBOT_MEDIAMTX_IMAGE; do
    [ -n "${!var:-}" ] && pairs+=("$var=${!var}")
  done
  if [ "${DOCKER[0]}" = "sudo" ]; then
    # DOCKER=(sudo -n docker)：env 必须在 docker 之前 → sudo -n env K=V docker compose ...
    "${DOCKER[@]:0:${#DOCKER[@]}-1}" env "${pairs[@]}" "${DOCKER[@]: -1}" \
      "${COMPOSE[@]:${#DOCKER[@]}}" "$@"
  else
    env "${pairs[@]}" "${COMPOSE[@]}" "$@"
  fi
}
# Exact rollback may check out a historical revision without this new helper.
# Keep its code in the running process so SSOT can still be written afterwards.
DEPLOYMENT_STATE_CODE="$(cat scripts/deployment_state.py)"
deployment_state() { python3 -c "$DEPLOYMENT_STATE_CODE" "$@"; }

CONTROL_PLANE=(api mqtt-ingress command-dispatcher task-worker)
APP_ALL=(api mqtt-ingress command-dispatcher task-worker web)
source scripts/vehicle-agent-rollout.sh
init_deploy_context() {
  V1_REQUIRED="$(v1_required)" || { echo 'ERROR: cannot determine v1 rollout requirement' >&2; return 1; }
  if [ "$V1_REQUIRED" = true ]; then
    COMPOSE+=(--profile vehicle-agent)
    CONTROL_PLANE+=(vehicle-agent-adapter)
    APP_ALL+=(vehicle-agent-adapter)
  fi
}

# ---- 版本 SSOT ----
current_sha() {
  runtime_value "$(runtime_descriptor current)" sha
}
previous_sha() {
  runtime_value "$(runtime_descriptor previous)" sha
}
runtime_descriptor() {
  deployment_state read "$CURRENT_JSON" "$1"
}
runtime_value() {
  python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2],"UNKNOWN"))' "$1" "$2"
}
exact_runtime() {
  printf '{"kind":"exact_sha","sha":"%s"}\n' "$1"
}
none_runtime() {
  printf '{"kind":"none"}\n'
}
write_ssot() {
  # $1=current descriptor $2=previous descriptor; provenance is never an exact SHA.
  deployment_state write "$CURRENT_JSON" "$1" "$2"
}
finalization_preflight() {
  FINALIZATION_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)" || return 1
  FINALIZATION_MANIFEST_DIR="$DEPLOY_ROOT/${FINALIZATION_TIMESTAMP}-${1:-rollback}"
  if ! deployment_state probe-write "$CURRENT_JSON" "$FINALIZATION_MANIFEST_DIR/manifest.json"; then
    echo 'FINALIZATION_PREFLIGHT=FAIL' >&2
    return 1
  fi
  echo 'FINALIZATION_PREFLIGHT=PASS'
}
rollback_after_finalize_failure() {
  # One compensation path for both exact_sha and legacy_snapshot.
  # A failed compensation never rewrites SSOT as though runtime recovery passed.
  if restore_runtime "$1"; then
    echo 'RUNTIME_COMPENSATION=PASS' >&2
    if write_ssot "$1" "$2"; then
      echo 'SSOT_REPAIR=PASS' >&2
    else
      echo 'SSOT_REPAIR=FAIL' >&2
      echo 'HIGH_PRIORITY_MANUAL_INTERVENTION=YES' >&2
    fi
  else
    echo 'RUNTIME_COMPENSATION=FAIL' >&2
    echo 'SSOT_REPAIR=FAIL' >&2
    echo 'HIGH_PRIORITY_MANUAL_INTERVENTION=YES' >&2
  fi
  return 1
}
rollback_to_legacy_snapshot() {
  python3 scripts/legacy_runtime.py restore "$1"
}
restore_runtime() {
  case "$(runtime_value "$1" kind)" in
    none)
      echo 'ROLLBACK_RUNTIME_KIND=FAIL（fresh 首发布无历史 runtime，禁止伪造回滚）' >&2
      return 1 ;;
    legacy_snapshot) rollback_to_legacy_snapshot "$(runtime_value "$1" manifest)" ;;
    exact_sha) rollback_to "$(runtime_value "$1" sha)" ;;
    *) echo 'ROLLBACK_RUNTIME_KIND=FAIL' >&2; return 1 ;;
  esac
}

require_sha() {
  if [ -z "$TARGET_SHA" ] || ! echo "$TARGET_SHA" | grep -qE '^[0-9a-f]{40}$'; then
    echo "ERROR: TARGET_SHA 必须是 40 位 hex（当前: ${TARGET_SHA:-<空>}）" >&2
    exit 1
  fi
  if [ -n "$RELEASE_ROOT" ]; then
    release_guard || exit 1
    return 0
  fi
  local head
  head="$(git rev-parse HEAD)"
  if [ "$head" != "$TARGET_SHA" ]; then
    echo "ERROR: git HEAD($head) != TARGET_SHA($TARGET_SHA)，禁止部署非精确版本" >&2
    exit 1
  fi
  local dirty
  dirty="$(git status --porcelain)"
  # exact-SHA release 必须完全 clean，包含所有 untracked/staged 文件。
  if [ -n "$dirty" ]; then
    echo "ERROR: 工作区不干净（含 untracked/staged 变更），先提交/清理再部署" >&2
    exit 1
  fi
}

# ---- immutable release 守卫（R2-11）----
release_guard() {
  [ -n "$RELEASE_ROOT" ] || return 0
  local dirname head branch
  dirname="$(basename "$RELEASE_ROOT")"
  [ "$dirname" = "$TARGET_SHA" ] || { echo "ERROR: release 目录名($dirname) != TARGET_SHA($TARGET_SHA)" >&2; return 1; }
  head="$(git -C "$RELEASE_ROOT" rev-parse HEAD 2>/dev/null)" || { echo "ERROR: release 目录不是 git worktree: $RELEASE_ROOT" >&2; return 1; }
  [ "$head" = "$TARGET_SHA" ] || { echo "ERROR: release HEAD($head) != TARGET_SHA($TARGET_SHA)" >&2; return 1; }
  [ -z "$(git -C "$RELEASE_ROOT" status --porcelain)" ] || { echo "ERROR: release 工作树不干净（immutable 违例）" >&2; return 1; }
  branch="$(git -C "$RELEASE_ROOT" symbolic-ref -q HEAD 2>/dev/null || true)"
  [ -z "$branch" ] || { echo "ERROR: release 必须 detached HEAD（当前在 $branch）" >&2; return 1; }
  echo "RELEASE_IMMUTABLE=PASS sha=$TARGET_SHA"
}

# U-Dao 守卫：云模式回滚目标必须含 U-Dao override（fail-closed）
u_dao_guard() {
  [ "$U_DAO_MODE" = 1 ] || return 0
  local sha="$1"
  if ! git cat-file -e "$sha:deploy/u-dao/compose.u-dao.yml" 2>/dev/null; then
    echo "U_DAO_GUARD=FAIL: 回滚目标 $sha 缺少 deploy/u-dao/compose.u-dao.yml" >&2
    return 1
  fi
  echo "U_DAO_GUARD=PASS sha=$sha"
}

build_images() {
  local services=("$@")
  echo "  构建不可变镜像（tag=$TARGET_SHA）：${services[*]}"
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" FIREBOT_WEB_IMAGE="$WEB_IMAGE" \
    compose_env_run build --build-arg SOURCE_REVISION="$TARGET_SHA" "${services[@]}"
}

get_service_container_id() {
  "${COMPOSE[@]}" ps -q "$1" 2>/dev/null | head -1
}

service_healthy() {
  local cid
  cid="$(get_service_container_id "$1")"
  [ -n "$cid" ] || return 1
  "${DOCKER[@]}" inspect --format '{{.State.Running}} {{.State.Health.Status}}' "$cid" 2>/dev/null | grep -q '^true healthy$'
}

recreate() {
  local services=("$@")
  echo "  重建服务：${services[*]}"
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" FIREBOT_WEB_IMAGE="$WEB_IMAGE" \
    compose_env_run up -d --no-deps --wait "${services[@]}" || return 1
  # --wait 后检查 required services healthy（fail-closed，统一走 docker inspect）
  for svc in "${services[@]}"; do
    service_healthy "$svc" || { echo "ERROR: $svc 未 healthy" >&2; return 1; }
  done
}

compose_project_name() {
  "${COMPOSE[@]}" config --format json |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])'
}

rollback_service_set() {
  # Read the checked-out rollback target, including services behind profiles.
  local defined svc
  defined="$("${COMPOSE[@]}" --profile '*' config --services)" || return 1
  ROLLBACK_SERVICES=()
  for svc in api mqtt-ingress command-dispatcher task-worker web; do
    grep -Fxq "$svc" <<< "$defined" || { echo "ROLLBACK_SERVICE_MISSING=$svc" >&2; return 1; }
    ROLLBACK_SERVICES+=("$svc")
  done
  ROLLBACK_HAS_ADAPTER=false
  if grep -Fxq vehicle-agent-adapter <<< "$defined"; then
    ROLLBACK_SERVICES+=(vehicle-agent-adapter)
    ROLLBACK_HAS_ADAPTER=true
  fi
  echo "ROLLBACK_SERVICE_SET=${ROLLBACK_SERVICES[*]}"
}

# Project labels also find orphaned/stopped instances after the service is
# removed from the old Compose file; never inspect or stop another project.
rollback_adapter_ids() {
  "${DOCKER[@]}" ps -a -q --filter "label=com.docker.compose.project=$1" \
    --filter label=com.docker.compose.service=vehicle-agent-adapter
}

rollback_adapter_absent() {
  local remaining
  remaining="$(rollback_adapter_ids "$1")" || return 1
  [ -z "$remaining" ] || { echo 'ADAPTER_POST_ROLLBACK=FAIL_PRESENT' >&2; return 1; }
  echo 'ADAPTER_POST_ROLLBACK=ABSENT'
}

verify_rollout_sha() {
  local prev="$1" svc cid revision
  for svc in "${ROLLBACK_SERVICES[@]}"; do
    cid="$(get_service_container_id "$svc")" || return 1
    revision="$("${DOCKER[@]}" inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$cid")" || return 1
    [ "$revision" = "$prev" ] || { echo "ROLLBACK_IMAGE_SHA_MATCH=FAIL service=$svc" >&2; return 1; }
  done
}

rollback_to() {
  # Shared by explicit rollback and failed-rollout recovery. No DB downgrade.
  local prev="$1" project target_project ids cid revision svc
  local adapter_ids=()
  [[ "$prev" =~ ^[0-9a-f]{40}$ ]] || return 1
  if [ "$U_DAO_MODE" = 1 ]; then
    # U-Dao release 模式：绝不 checkout immutable release；切换到 releases/<prev>
    local prev_root
    prev_root="$(dirname "$RELEASE_ROOT")/$prev"
    [ -d "$prev_root" ] || { echo "ERROR: releases/$prev 不存在（回滚目标必须已有 immutable release）" >&2; return 1; }
    u_dao_guard "$prev" || return 1
    TARGET_SHA="$prev" RELEASE_ROOT="$prev_root" release_guard || return 1
    FIREBOT_COMPOSE_FILES="$prev_root/docker-compose.server.yml $prev_root/deploy/u-dao/compose.u-dao.yml"
    COMPOSE=("${DOCKER[@]}" compose --env-file "$ENV_FILE")
    for f in $FIREBOT_COMPOSE_FILES; do COMPOSE+=(-f "$f"); done
    PY_IMAGE="firebot-python:$prev"
    WEB_IMAGE="firebot-web:$prev"
    rollback_service_set || return 1
    APP_ALL=("${ROLLBACK_SERVICES[@]}")
    recreate "${ROLLBACK_SERVICES[@]}" || return 1
    verify_rollout_sha "$prev" || return 1
    echo "ROLLBACK_TO_RELEASE=PASS sha=$prev root=$prev_root"
    return 0
  fi
  git cat-file -e "$prev:docker-compose.server.yml" || return 1
  project="$(compose_project_name)" || return 1
  [ -n "$project" ] || return 1
  ids="$(rollback_adapter_ids "$project")" || return 1
  if [ -n "$ids" ]; then
    mapfile -t adapter_ids <<< "$ids"
    # Stop/remove the new adapter BEFORE any old control-plane service starts.
    # A post-adapter target recreates its own exact-SHA adapter below.
    "${DOCKER[@]}" stop "${adapter_ids[@]}" || return 1
    "${DOCKER[@]}" rm "${adapter_ids[@]}" || return 1
  fi
  rollback_adapter_absent "$project" || return 1
  git checkout "$prev" || return 1
  [ "$(git rev-parse HEAD)" = "$prev" ] || return 1
  TARGET_SHA="$prev"
  PY_IMAGE="firebot-python:$prev"
  WEB_IMAGE="firebot-web:$prev"
  target_project="$(compose_project_name)" || return 1
  [ "$target_project" = "$project" ] || { echo 'ROLLBACK_PROJECT_MISMATCH=FAIL' >&2; return 1; }
  rollback_service_set || return 1
  APP_ALL=("${ROLLBACK_SERVICES[@]}")
  recreate "${ROLLBACK_SERVICES[@]}" || return 1
  for svc in "${ROLLBACK_SERVICES[@]}"; do
    cid="$(get_service_container_id "$svc")" || return 1
    revision="$("${DOCKER[@]}" inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$cid")" || return 1
    [ "$revision" = "$prev" ] || { echo "ROLLBACK_IMAGE_SHA_MATCH=FAIL service=$svc" >&2; return 1; }
  done
  if [ "$ROLLBACK_HAS_ADAPTER" = false ]; then
    rollback_adapter_absent "$project" || return 1
  else
    echo 'ADAPTER_POST_ROLLBACK=HEALTHY_PREVIOUS_SHA'
  fi
}

migration_revision() {
  "${COMPOSE[@]}" exec -T api python -c "
from app.db.session import SessionLocal
from sqlalchemy import text
db = SessionLocal()
print(db.scalar(text('SELECT version_num FROM alembic_version')) or 'unknown')
" 2>/dev/null || echo "unknown"
}

write_manifest() {
  local scope="$1" prev="$2" mig_before="$3" ts
  ts="${FINALIZATION_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
  local dir="${FINALIZATION_MANIFEST_DIR:-$DEPLOY_ROOT/${ts}-${TARGET_SHA}}"
  mkdir -p "$dir" || return 1
  local mig_after
  mig_after="$(migration_revision)"
  python3 - "$TARGET_SHA" "$scope" "$prev" "$ts" "$dir" "$mig_before" "$mig_after" "$ENV_FILE" "$CURRENT_JSON" "$COMPOSE_FILES" "${DOCKER[@]}" <<'PY' | deployment_state write-json "$dir/manifest.json" || return 1
import json, subprocess, sys
target_sha, scope, prev, ts, d, mig_before, mig_after, env_file = sys.argv[1:9]
compose_files = sys.argv[9].split()
docker_cmd = sys.argv[10:]
with open(sys.argv[8], encoding="utf-8") as stream:
    runtime_state = json.load(stream)

def inspect(args):
    try:
        return subprocess.check_output(args, text=True).strip()
    except Exception:
        return ""

def compose_args():
    out = docker_cmd + ["compose", "--env-file", env_file]
    for f in compose_files:
        out += ["-f", f]
    return out

def cid_of(svc):
    return inspect(compose_args() + ["ps", "-q", svc])

manifest = {
    "requested_sha": target_sha,
    "target_sha": runtime_state.get("current_sha"),
    "runtime_state": runtime_state,
    "branch": inspect(["git", "branch", "--show-current"]),
    "previous_sha": prev,
    "deploy_scope": scope,
    "timestamp": ts,
    "migration_before": mig_before,
    "migration_after": mig_after,
    "containers": {},
    "third_party_images": {},
}
for svc in ("api", "mqtt-ingress", "command-dispatcher", "task-worker", "web", "ros-compat-adapter", "vehicle-agent-adapter"):
    cid = cid_of(svc)
    if cid:
        manifest["containers"][svc] = {
            "container_id": cid,
            "image_id": inspect(docker_cmd + ["inspect", "--format", "{{.Image}}", cid]),
            "image_revision": inspect(docker_cmd + ["inspect", "--format",
                                       '{{ index .Config.Labels "org.opencontainers.image.revision" }}', cid]),
            "started_at": inspect(docker_cmd + ["inspect", "--format", "{{.State.StartedAt}}", cid]),
        }
# 第三方镜像不可变性（R2-22）：exact Git SHA 不单独宣称为完整 runtime reproducibility
for image in ("postgres:18.4-alpine", "redis:8.4.5-alpine", "eclipse-mosquitto:2.1.2-alpine",
              "nginx:1.30.4-alpine", "busybox:1.37.0-musl"):
    manifest["third_party_images"][image] = inspect(
        docker_cmd + ["image", "inspect", "--format", "{{.Id}} {{index .RepoDigests 0}}", image])
print(json.dumps(manifest, ensure_ascii=False))
PY
  echo "  manifest 已写入 $dir/manifest.json"
}

do_status() {
  echo "== SERVER 部署状态 =="
  local current
  current="$(runtime_descriptor current)" || return 1
  echo "RUNTIME_KIND=$(runtime_value "$current" kind)"
  if [ "$(runtime_value "$current" kind)" = legacy_snapshot ]; then
    echo "current_git_provenance_sha=$(runtime_value "$current" git_provenance_sha)"
    echo "current_runtime_manifest=$(runtime_value "$current" manifest)"
  fi
  echo "current_sha=$(current_sha)"
  echo "previous_sha=$(previous_sha)"
  if [ -n "$RELEASE_ROOT" ]; then
    echo "release_root=$RELEASE_ROOT"
  else
    echo "git HEAD=$(git rev-parse HEAD)"
  fi
  "${COMPOSE[@]}" ps --format '{{.Name}} {{.Image}} {{.State}} {{.Health}}'
}

do_preflight() {
  require_sha
  SERVER_ENV_FILE="$ENV_FILE" UPDATE_MODE=true scripts/server-preflight.sh
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" FIREBOT_WEB_IMAGE="$WEB_IMAGE" compose_env_run config --quiet
  echo "PREFLIGHT=PASS sha=$TARGET_SHA"
}

db_has_schema() {
  local out
  out="$("${COMPOSE[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT to_regclass('"'"'public.alembic_version'"'"')"' 2>/dev/null || true)"
  printf '%s\n' "$out" | grep -q alembic_version
}

do_bootstrap() {
  # fresh server 显式流程：基础服务 + fresh DB（alembic upgrade head + seed）+ SSOT(previous=none)。
  # 禁止伪造 BACKUP_VERIFIED / legacy_snapshot / previous SHA（R2-8）。
  require_sha
  if [ -f "$CURRENT_JSON" ]; then
    echo "ERROR: bootstrap 仅限 fresh server（$CURRENT_JSON 已存在）；后续更新走 deploy 子命令" >&2
    exit 1
  fi
  if [ -n "${FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST:-}" ]; then
    echo "ERROR: fresh install 禁止 FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST（禁止伪造 legacy 回滚基线）" >&2
    exit 1
  fi
  "${COMPOSE[@]}" config --quiet || exit 1
  echo "  启动基础服务（postgres/redis/mosquitto）"
  "${COMPOSE[@]}" up -d --wait postgres redis mosquitto || exit 1
  if db_has_schema; then
    echo "ERROR: 数据库已存在 alembic_version（非 fresh DB）。禁止 bootstrap 覆盖，需人工审查。" >&2
    exit 1
  fi
  echo "  执行 fresh migration（alembic upgrade head + seed）"
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" compose_env_run run --rm migrate || exit 1
  write_ssot "$(exact_runtime "$TARGET_SHA")" "$(none_runtime)"
  echo "BOOTSTRAP=PASS sha=$TARGET_SHA previous=none"
}

do_migrate() {
  require_sha
  if [ "${BACKUP_VERIFIED:-0}" != "1" ]; then
    echo "ERROR: 生产 migration 前必须 backup 且验证（backup + backup-verify）；确认后 BACKUP_VERIFIED=1 重跑" >&2
    exit 1
  fi
  build_images api
  echo "  执行 one-shot migrate（alembic upgrade head + seed）"
  FIREBOT_PYTHON_IMAGE="$PY_IMAGE" compose_env_run run --rm migrate
  echo "MIGRATE=PASS sha=$TARGET_SHA"
}

migration_head() {
  # 从目标 SHA 工作树的 Alembic migration 图计算 head（不读旧 API 容器，避免把旧代码 head 当目标 head）。
  local versions_dir="${FIREBOT_MIGRATIONS_DIR:-apps/api/alembic/versions}"
  python3 - "$versions_dir" <<'PY'
import re, sys
from pathlib import Path

versions = Path(sys.argv[1])
revisions: dict[str, str | None] = {}
for p in sorted(versions.glob("2026*.py")):
    text = p.read_text(encoding="utf-8")
    rev = re.search(r'revision\s*=\s*["\']([^"\']+)["\']', text)
    down = re.search(r'down_revision\s*=\s*["\']([^"\']+)["\']', text)
    if rev:
        revisions[rev.group(1)] = down.group(1) if down else None
downs = {v for v in revisions.values() if v}
heads = [r for r in revisions if r not in downs]
print(heads[-1] if heads else "unknown")
PY
}

do_migration_needed() {
  require_sha
  local db head
  db="$(migration_revision)"
  head="$(migration_head)"
  if [ "$db" = "$head" ]; then
    echo "MIGRATION=SKIP（db=$db == head=$head）"
    return 0
  fi
  echo "MIGRATION=NEEDED（db=$db != head=$head）"
  return 1
}

do_backup() {
  # 真实 pg_dump 备份（gzip + sha256 + 0600 + manifest），禁止假 BACKUP_VERIFIED。
  require_sha
  local dir="$DEPLOY_ROOT/backups"
  mkdir -p "$dir"
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local file="$dir/firebot-$ts.sql.gz"
  "${COMPOSE[@]}" exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip' > "$file"
  [ -s "$file" ] || { echo "BACKUP=FAIL（备份文件为空）" >&2; exit 1; }
  chmod 600 "$file"
  local sum
  sum="$(sha256sum "$file" | cut -d' ' -f1)"
  printf '{"backup_file":"%s","sha256":"%s","timestamp":"%s","sha":"%s"}\n' \
    "$file" "$sum" "$ts" "$TARGET_SHA" > "$file.manifest.json"
  echo "BACKUP=PASS file=$file sha256=$sum"
  echo "BACKUP_FILE=$file"
}

do_backup_verify() {
  # R2-21：backup VERIFIED 必须含 restore test（隔离临时库 pg_restore + 基础一致性检查）。
  local file="${1:-}" dbname="firebot_backup_verify"
  [ -s "$file" ] || { echo "BACKUP_VERIFY=FAIL（文件缺失或为空）" >&2; exit 1; }
  "${COMPOSE[@]}" exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists firebot_backup_verify' >/dev/null 2>&1 || true
  "${COMPOSE[@]}" exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" firebot_backup_verify' \
    || { echo "BACKUP_VERIFY=FAIL（createdb 失败）" >&2; exit 1; }
  if ! gzip -dc "$file" | "${COMPOSE[@]}" exec -T postgres sh -c 'psql -q -U "$POSTGRES_USER" -d firebot_backup_verify'; then
    "${COMPOSE[@]}" exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists firebot_backup_verify' >/dev/null 2>&1 || true
    echo "BACKUP_VERIFY=FAIL（restore 失败）" >&2
    exit 1
  fi
  local tables robots users commands
  tables="$("${COMPOSE[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d firebot_backup_verify -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='"'"'public'"'"'"')"
  robots="$("${COMPOSE[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d firebot_backup_verify -tAc "SELECT count(*) FROM robots"')"
  users="$("${COMPOSE[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d firebot_backup_verify -tAc "SELECT count(*) FROM users"')"
  commands="$("${COMPOSE[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d firebot_backup_verify -tAc "SELECT count(*) FROM commands"')"
  "${COMPOSE[@]}" exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists firebot_backup_verify' >/dev/null 2>&1 || true
  if [ "${tables:-0}" -lt 10 ] || [ -z "${robots:-}" ] || [ -z "${users:-}" ] || [ -z "${commands:-}" ]; then
    echo "BACKUP_VERIFY=FAIL（一致性检查不过：tables=$tables robots=$robots users=$users commands=$commands）" >&2
    exit 1
  fi
  echo "BACKUP_VERIFY=PASS tables=$tables robots=$robots users=$users commands=$commands"
}

rollback_schema_gate() {
  # R2-21：回滚目标 schema 与当前 DB revision 不兼容时禁止回滚，不伪造 PASS。
  local db head
  db="$(migration_revision)" || { echo 'ROLLBACK_SCHEMA_GATE=FAIL（无法读取 DB revision）' >&2; return 1; }
  [ "$db" != "unknown" ] || { echo 'ROLLBACK_SCHEMA_GATE=FAIL（无法读取 DB revision）' >&2; return 1; }
  head="$(migration_head)" || { echo 'ROLLBACK_SCHEMA_GATE=FAIL（无法计算目标 head）' >&2; return 1; }
  if [ "$db" != "$head" ]; then
    echo "ROLLBACK_SCHEMA_GATE=FAIL db=$db target_head=$head（禁止回滚到不兼容 schema 的 runtime）" >&2
    return 1
  fi
  echo "ROLLBACK_SCHEMA_GATE=PASS db=$db"
}

deploy_scope() {
  local scope="$1"
  shift
  require_sha
  local prev prev_runtime prior_previous kind mig_before prev_descriptor
  if [ "$V1_REQUIRED" = true ]; then
    v1_acl_gate || { echo 'ERROR: active broker v1 ACL gate failed' >&2; exit 1; }
  fi
  prev_runtime="$(runtime_descriptor current)" || return 1
  prior_previous="$(runtime_descriptor previous)" || return 1
  if [ -n "${FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST:-}" ]; then
    [ "$scope" = all-app ] || { echo 'ERROR: first clean cutover requires all-app' >&2; return 1; }
    python3 scripts/legacy_runtime.py validate "$FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST" || return 1
    prev_runtime="$(python3 scripts/legacy_runtime.py descriptor "$FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST")" || return 1
    deployment_state canary "$CURRENT_JSON" "$prev_runtime" || return 1
  elif [ "$(runtime_value "$prev_runtime" kind)" = legacy_snapshot ]; then
    echo 'ERROR: legacy cutover requires explicit FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST' >&2
    return 1
  fi
  kind="$(runtime_value "$prev_runtime" kind)"
  prev="$(runtime_value "$prev_runtime" sha)"
  prev_descriptor="$(runtime_descriptor previous)" || return 1
  finalization_preflight "$TARGET_SHA" || return 1
  # migration_before 必须在整个 server deploy 开始时就记录（firebotctl 传 DEPLOY_MIGRATION_BEFORE），
  # 不能等 migrate 之后才取（否则会误记 migration 后的 revision）。
  mig_before="${DEPLOY_MIGRATION_BEFORE:-$(migration_revision)}"
  if ! build_images "$@" || ! recreate "$@"; then
    echo "SERVER_DEPLOY=FAIL（rollout 失败，自动回滚 runtime=$kind）" >&2
    if [ "$prev" = "$TARGET_SHA" ] && [ "$(runtime_value "$prev_descriptor" kind)" = none ]; then
      echo 'AUTO_ROLLBACK=FAIL（fresh 首发布无回滚目标）：清理失败 runtime 后重新 bootstrap，禁止伪造 previous' >&2
    elif [ "$kind" = none ]; then
      echo 'AUTO_ROLLBACK=FAIL（无历史 runtime，禁止伪造回滚）' >&2
    elif [ "$kind" = legacy_snapshot ]; then
      echo 'AUTO_ROLLBACK_MODE=LEGACY_SNAPSHOT' >&2
      if restore_runtime "$prev_runtime" && write_ssot "$prev_runtime" "$prior_previous"; then
        echo 'AUTO_ROLLBACK_LEGACY=PASS' >&2
      else
        echo 'AUTO_ROLLBACK_LEGACY=FAIL' >&2
      fi
    elif [ "$prev" != "UNKNOWN" ]; then
      echo 'AUTO_ROLLBACK_MODE=EXACT_SHA' >&2
      if restore_runtime "$prev_runtime"; then
        echo "AUTO_ROLLBACK=PASS（回 $prev）" >&2
      else
        echo "AUTO_ROLLBACK=FAIL（高优先级报警，需人工介入）" >&2
      fi
    else
      echo "AUTO_ROLLBACK=FAIL（无 previous SHA）" >&2
    fi
    exit 1
  fi
  # fresh 首发布（bootstrap 后同 SHA 的 rollout 且 previous descriptor 为 none）保持 previous=none；
  # 新 SHA 的第二次 exact-SHA 发布轮转 previous=旧 current，形成正常回滚链。
  if [ "$prev" = "$TARGET_SHA" ] && [ "$(runtime_value "$prev_descriptor" kind)" = none ]; then
    if ! write_ssot "$(exact_runtime "$TARGET_SHA")" "$(none_runtime)"; then
      echo 'FINALIZE_SSOT=FAIL' >&2
      return 1
    fi
  elif ! write_ssot "$(exact_runtime "$TARGET_SHA")" "$prev_runtime"; then
    echo 'FINALIZE_SSOT=FAIL' >&2
    rollback_after_finalize_failure "$prev_runtime" "$prior_previous"
    return 1
  fi
  if ! write_manifest "$scope" "$prev" "$mig_before"; then
    echo 'FINALIZE_MANIFEST=FAIL' >&2
    rollback_after_finalize_failure "$prev_runtime" "$prior_previous"
    return 1
  fi
  echo "DEPLOY=PASS scope=$scope sha=$TARGET_SHA previous=$prev"
}

do_verify() {
  # verify 不需要外部 TARGET_SHA：从 current.json 读期望 SHA
  local current expected
  current="$(runtime_descriptor current)" || return 1
  if [ "$(runtime_value "$current" kind)" = none ]; then
    echo 'SERVER_VERIFY=FAIL（无 current runtime，先 bootstrap/deploy）' >&2
    exit 1
  fi
  if [ "$(runtime_value "$current" kind)" = legacy_snapshot ]; then
    python3 scripts/legacy_runtime.py verify "$(runtime_value "$current" manifest)" || return 1
    echo 'SERVER_VERIFY=PASS runtime=legacy_snapshot'
    return 0
  fi
  expected="$(current_sha)"
  if [ "$expected" = "UNKNOWN" ]; then
    echo "SERVER_VERIFY=FAIL（无 current.json，先 deploy）" >&2
    exit 1
  fi
  # 传 VERIFY_SHA 给 server-verify.sh，且失败必须 exit != 0
  VERIFY_SHA="$expected" scripts/server-verify.sh
}

do_refresh_ros_compat() {
  require_sha
  local current before after cid revision
  current="$(runtime_descriptor current)" || return 1
  if [ "$(runtime_value "$current" kind)" != exact_sha ] || \
     [ "$(runtime_value "$current" sha)" != "$TARGET_SHA" ]; then
    echo 'ROS_COMPAT_REFRESH=FAIL (requires current exact_sha == TARGET_SHA)' >&2
    return 1
  fi
  before="$(sha256sum "$CURRENT_JSON" | cut -d' ' -f1)" || return 1
  # Maintenance never invokes deploy_scope/write_ssot or changes previous runtime.
  if ! build_images ros-compat-adapter || ! recreate ros-compat-adapter; then
    echo 'ROS_COMPAT_REFRESH=FAIL (build or health)' >&2
    return 1
  fi
  cid="$(get_service_container_id ros-compat-adapter)" || return 1
  revision="$("${DOCKER[@]}" inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$cid")" || return 1
  [ "$revision" = "$TARGET_SHA" ] || { echo 'ROS_COMPAT_REFRESH=FAIL (revision)' >&2; return 1; }
  after="$(sha256sum "$CURRENT_JSON" | cut -d' ' -f1)" || return 1
  [ "$before" = "$after" ] || { echo 'ROS_COMPAT_REFRESH=FAIL SSOT_CHANGED=YES' >&2; return 1; }
  echo 'ROS_COMPAT_REFRESH=PASS'
  echo 'SSOT_CHANGED=NO'
}

do_rollback() {
  local prev previous old_current kind mig_before
  previous="$(runtime_descriptor previous)" || return 1
  old_current="$(runtime_descriptor current)" || return 1
  kind="$(runtime_value "$previous" kind)"
  prev="$(runtime_value "$previous" sha)"
  if [ "$kind" = none ]; then
    echo "ERROR: previous=none（fresh 首发布无回滚目标，禁止伪造回滚链）" >&2
    exit 1
  fi
  if [ "$kind" = exact_sha ] && { [ "$prev" = UNKNOWN ] || [ "$prev" = "$(current_sha)" ]; }; then
    echo "ERROR: 没有可回滚的 previous SHA（$prev）" >&2
    exit 1
  fi
  echo "  回滚到 previous runtime: $kind"
  u_dao_guard "${prev:-unknown}" || exit 1
  # U-Dao release 模式：schema gate 用目标 release 的 migration 图计算 head
  if [ "$U_DAO_MODE" = 1 ] && [ "$kind" = exact_sha ]; then
    local prev_release="$([ -n "$RELEASE_ROOT" ] && dirname "$RELEASE_ROOT")/$prev"
    if [ -d "$prev_release" ]; then
      FIREBOT_MIGRATIONS_DIR="$prev_release/apps/api/alembic/versions" rollback_schema_gate || exit 1
    else
      echo 'ROLLBACK_SCHEMA_GATE=FAIL（目标 release 不存在，无法计算 schema head）' >&2
      exit 1
    fi
  fi
  finalization_preflight "$(runtime_value "$previous" sha)" || return 1
  mig_before="$(migration_revision)"
  restore_runtime "$previous" || { echo 'ROLLBACK=FAIL' >&2; return 1; }
  if ! write_ssot "$previous" "$old_current"; then
    echo 'FINALIZE_SSOT=FAIL ROLLBACK=FAIL' >&2
    rollback_after_finalize_failure "$old_current" "$previous"
    return 1
  fi
  if ! write_manifest rollback "$(runtime_value "$old_current" sha)" "$mig_before"; then
    echo 'FINALIZE_MANIFEST=FAIL ROLLBACK=FAIL' >&2
    rollback_after_finalize_failure "$old_current" "$previous"
    return 1
  fi
  echo "ROLLBACK=PASS runtime=$kind sha=$prev"
}

if [[ "${BASH_SOURCE[0]}" = "$0" ]]; then
case "${1:-}" in
  status|verify|rollback|refresh-ros-compat|bootstrap|backup-verify) ;; # Do not guess rollout from the DB for recovery/maintenance/fresh.
  *) init_deploy_context ;;
esac
case "${1:-}" in
  status) do_status ;;
  preflight) do_preflight ;;
  bootstrap) do_bootstrap ;;
  migrate) do_migrate ;;
  backup) do_backup ;;
  backup-verify) do_backup_verify "${2:-}" ;;
  migration-needed) do_migration_needed ;;
  current-revision) echo "$(migration_revision)" ;;
  api) deploy_scope "api" api ;;
  worker) deploy_scope "worker" task-worker ;;
  adapter)
    [ "$V1_REQUIRED" = true ] || { echo 'ERROR: set VEHICLE_AGENT_V1_ROLLOUT=true for first adapter rollout' >&2; exit 1; }
    deploy_scope "adapter" vehicle-agent-adapter ;;
  web) deploy_scope "web" web ;;
  control-plane) deploy_scope "control-plane" "${CONTROL_PLANE[@]}" ;;
  all-app) deploy_scope "all-app" "${APP_ALL[@]}" ;;
  verify) do_verify ;;
  refresh-ros-compat) do_refresh_ros_compat ;;
  rollback) do_rollback ;;
  *)
    echo "用法: TARGET_SHA=<40SHA> $0 status|preflight|bootstrap|migrate|backup|backup-verify|api|worker|web|control-plane|all-app|verify|rollback|refresh-ros-compat" >&2
    exit 1
    ;;
esac
fi
